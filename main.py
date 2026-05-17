import asyncio
import json
import logging
import os
import re
import ssl
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LeGoTDP")

PLUGIN_DIR    = os.path.dirname(os.path.abspath(__file__))
BIN_DIR       = os.path.join(PLUGIN_DIR, "bin")
BIN_PATH      = os.path.join(BIN_DIR, "ryzenadj")
SETTINGS_FILE = os.path.join(PLUGIN_DIR, "settings.json")
PROFILES_FILE = os.path.join(PLUGIN_DIR, "profiles.json")
RYZENADJ_URL  = (
    "https://github.com/FlyGoat/RyzenAdj/releases/download/v0.19.0/"
    "ryzenadj-manylinux_2_28-x86_64.tar.gz"
)

# ── Lenovo WMI sysfs paths ─────────────────────────────────────────────────────
_WMI_BASE = "/sys/class/firmware-attributes/lenovo-wmi-other-0/attributes"
_WMI = {
    "spl":  f"{_WMI_BASE}/ppt_pl1_spl",
    "sppt": f"{_WMI_BASE}/ppt_pl2_sppt",
    "fppt": f"{_WMI_BASE}/ppt_pl3_fppt",
}
_PLATFORM_PROFILE_DIR = "/sys/class/platform-profile"

DEFAULT_SETTINGS = {"spl": 15000, "sppt": 15000, "fppt": 15000, "enabled": True}

_ROW_RE = re.compile(r"\|\s*(.+?)\s*\|\s*([\d.]+)\s*\|")


# ── Settings ───────────────────────────────────────────────────────────────────

def _load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()


def _save_settings(s: dict) -> None:
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f)


# ── Per-game profiles ──────────────────────────────────────────────────────────

def _load_profiles() -> dict:
    if os.path.exists(PROFILES_FILE):
        try:
            with open(PROFILES_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_profiles(profiles: dict) -> None:
    with open(PROFILES_FILE, "w") as f:
        json.dump(profiles, f)


# ── ryzenadj download ──────────────────────────────────────────────────────────

def _download_ryzenadj() -> None:
    logger.info("Downloading ryzenadj from %s", RYZENADJ_URL)
    os.makedirs(BIN_DIR, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz")
    os.close(tmp_fd)
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(RYZENADJ_URL, context=ssl_ctx) as resp, \
             open(tmp_path, "wb") as out:
            out.write(resp.read())
        with tarfile.open(tmp_path, "r:gz") as tar:
            binary_member = next(
                (m for m in tar.getmembers()
                 if os.path.basename(m.name) == "ryzenadj" and m.isfile()),
                None,
            )
            if binary_member is None:
                raise RuntimeError("ryzenadj binary not found inside tarball")
            binary_member.name = "ryzenadj"
            tar.extract(binary_member, BIN_DIR)
        os.chmod(BIN_PATH, 0o755)
        logger.info("ryzenadj installed at %s", BIN_PATH)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _ensure_ryzenadj() -> None:
    if not os.path.isfile(BIN_PATH):
        _download_ryzenadj()
    mode = os.stat(BIN_PATH).st_mode
    if not (mode & stat.S_IXUSR):
        os.chmod(BIN_PATH, mode | 0o111)


# ── Lenovo WMI helpers ─────────────────────────────────────────────────────────

def _wmi_available() -> bool:
    return all(
        os.path.exists(os.path.join(p, "current_value"))
        for p in _WMI.values()
    )


def _find_lenovo_profile_path() -> Optional[str]:
    if not os.path.isdir(_PLATFORM_PROFILE_DIR):
        return None
    for entry in os.listdir(_PLATFORM_PROFILE_DIR):
        name_file = os.path.join(_PLATFORM_PROFILE_DIR, entry, "name")
        if os.path.exists(name_file):
            with open(name_file) as f:
                if f.read().strip() == "lenovo-wmi-gamezone":
                    return os.path.join(_PLATFORM_PROFILE_DIR, entry, "profile")
    return None


def _set_platform_profile_custom() -> None:
    path = _find_lenovo_profile_path()
    if path:
        try:
            with open(path, "w") as f:
                f.write("custom")
        except Exception as e:
            logger.warning("Could not set platform profile: %s", e)


def _wmi_read_int(attr: str, filename: str) -> Optional[int]:
    p = os.path.join(_WMI[attr], filename)
    if os.path.exists(p):
        with open(p) as f:
            return int(f.read().strip())
    return None


def _wmi_write(attr: str, value: int) -> None:
    min_v = _wmi_read_int(attr, "min_value") or 0
    max_v = _wmi_read_int(attr, "max_value") or 9999
    clamped = max(min_v, min(max_v, value))
    path = os.path.join(_WMI[attr], "current_value")
    with open(path, "w") as f:
        f.write(str(clamped))
    logger.info("WMI %s = %d W (requested %d)", attr, clamped, value)


def _apply_wmi(spl_w: int, sppt_w: int, fppt_w: int) -> dict:
    _set_platform_profile_custom()
    time.sleep(0.3)
    _wmi_write("fppt", fppt_w)
    time.sleep(0.3)
    _wmi_write("sppt", sppt_w)
    time.sleep(0.3)
    _wmi_write("spl", spl_w)
    return {"success": True, "stdout": "WMI TDP applied", "stderr": "", "returncode": 0}


# ── ryzenadj output parser ─────────────────────────────────────────────────────

def _parse_ryzenadj_output(text: str) -> dict:
    values: dict = {}
    for line in text.splitlines():
        m = _ROW_RE.search(line)
        if not m:
            continue
        name  = m.group(1).strip().upper()
        value = float(m.group(2))

        if "STAPM" in name and "LIMIT" in name:
            values["spl_limit"] = value
        elif "STAPM" in name and "VALUE" in name:
            values["spl_value"] = value
        elif "FAST" in name and "LIMIT" in name:
            values["fppt_limit"] = value
        elif "FAST" in name and "VALUE" in name:
            values["fppt_value"] = value
        elif "SLOW" in name and "LIMIT" in name:
            values["sppt_limit"] = value
        elif "SLOW" in name and "VALUE" in name:
            values["sppt_value"] = value
        elif "PPT" in name and "LIMIT" in name and "APU" not in name and "sppt_limit" not in values:
            values["sppt_limit"] = value
        elif "PPT" in name and "VALUE" in name and "APU" not in name and "sppt_value" not in values:
            values["sppt_value"] = value
    return values


# ── Plugin class ───────────────────────────────────────────────────────────────

class Plugin:
    _ready: bool = False
    _setup_error: Optional[str] = None

    async def is_ready(self) -> dict:
        return {"ready": self._ready, "error": self._setup_error, "wmi": _wmi_available()}

    async def get_settings(self) -> dict:
        return _load_settings()

    async def get_game_profile(self, app_id: str) -> dict:
        profiles = _load_profiles()
        profile = profiles.get(app_id)
        return {"exists": profile is not None, "profile": profile or {}}

    async def delete_game_profile(self, app_id: str) -> None:
        profiles = _load_profiles()
        profiles.pop(app_id, None)
        _save_profiles(profiles)
        logger.info("Deleted game profile: app=%s", app_id)

    async def set_plugin_enabled(self, enabled: bool) -> None:
        s = _load_settings()
        s["enabled"] = enabled
        _save_settings(s)
        logger.info("Plugin enabled=%s", enabled)

    async def restore_defaults(self) -> dict:
        """Write factory default_value to each WMI TDP attribute."""
        if not _wmi_available():
            return {"success": False, "stderr": "WMI not available", "stdout": "", "returncode": -1}
        try:
            _set_platform_profile_custom()
            time.sleep(0.3)
            for key in ("fppt", "sppt", "spl"):
                default = _wmi_read_int(key, "default_value")
                if default is not None:
                    _wmi_write(key, default)
                    time.sleep(0.3)
            logger.info("WMI defaults restored")
            return {"success": True, "stderr": "", "stdout": "Defaults restored", "returncode": 0}
        except Exception as e:
            logger.error("restore_defaults failed: %s", e)
            return {"success": False, "stderr": str(e), "stdout": "", "returncode": -1}

    async def get_limits(self) -> dict:
        """Return per-parameter min/max in watts for the current device."""
        if _wmi_available():
            result = {}
            for key in ("spl", "sppt", "fppt"):
                result[key] = {
                    "min": _wmi_read_int(key, "min_value") or 1,
                    "max": _wmi_read_int(key, "max_value") or 54,
                }
            return result
        return {k: {"min": 1, "max": 54} for k in ("spl", "sppt", "fppt")}

    async def get_tdp_info(self) -> dict:
        if not self._ready:
            return {"success": False, "values": {}, "error": "not ready"}

        values: dict = {}

        # Limits: prefer WMI (accurate); fall back to ryzenadj --info
        if _wmi_available():
            for key in ("spl", "sppt", "fppt"):
                v = _wmi_read_int(key, "current_value")
                if v is not None:
                    values[f"{key}_limit"] = float(v)

        # Current usage: always from ryzenadj --info (WMI has no live draw data)
        if os.path.isfile(BIN_PATH):
            try:
                result = subprocess.run(
                    [BIN_PATH, "--info"], capture_output=True, text=True, timeout=5
                )
                parsed = _parse_ryzenadj_output(result.stdout + result.stderr)
                # Merge only VALUE fields; don't overwrite WMI limits
                for k, v in parsed.items():
                    if k.endswith("_value"):
                        values[k] = v
                    elif not _wmi_available() and k.endswith("_limit"):
                        values[k] = v
            except Exception as e:
                logger.warning("ryzenadj --info failed: %s", e)

        return {"success": True, "values": values}

    async def apply_tdp(self, spl: int, sppt: int, fppt: int, app_id: str = "") -> dict:
        """Apply TDP limits. spl/sppt/fppt are in milliwatts. app_id saves a game profile."""
        if not self._ready:
            return {"success": False, "stderr": "not ready", "stdout": "", "returncode": -1}

        spl_w  = spl  // 1000
        sppt_w = sppt // 1000
        fppt_w = fppt // 1000

        if _wmi_available():
            result = _apply_wmi(spl_w, sppt_w, fppt_w)
        else:
            cmd = [
                BIN_PATH,
                f"--stapm-limit={spl}",
                f"--slow-limit={sppt}",
                f"--fast-limit={fppt}",
            ]
            logger.info("Running: %s", " ".join(cmd))
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                result = {
                    "success": r.returncode == 0,
                    "stdout":  r.stdout,
                    "stderr":  r.stderr,
                    "returncode": r.returncode,
                }
            except subprocess.TimeoutExpired:
                result = {"success": False, "stderr": "timeout", "stdout": "", "returncode": -1}

        if result["success"]:
            if app_id:
                profiles = _load_profiles()
                profiles[app_id] = {"spl": spl, "sppt": sppt, "fppt": fppt}
                _save_profiles(profiles)
                logger.info("Saved game profile: app=%s", app_id)
            else:
                s = _load_settings()
                s["spl"] = spl
                s["sppt"] = sppt
                s["fppt"] = fppt
                _save_settings(s)

        return result

    async def _main(self):
        logger.info("RyzenADJ Set TDP: initialising (WMI=%s)", _wmi_available())
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _ensure_ryzenadj)
            self._ready = True
            logger.info("RyzenADJ Set TDP: ready")
        except Exception as e:
            self._setup_error = str(e)
            logger.error("RyzenADJ Set TDP: setup failed: %s", e)

    async def _unload(self):
        logger.info("RyzenADJ Set TDP: unloaded")
