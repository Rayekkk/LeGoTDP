import asyncio
import glob
import json
import logging
import os
import re
import ssl
import stat
import subprocess
import tarfile
import tempfile
import threading
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

DEFAULT_SETTINGS = {"spl": 15000, "sppt": 15000, "fppt": 15000, "enabled": True}

_ryzenadj_lock = threading.Lock()

# Cache of last successful --info parse - keeps UI responsive when lock is held
_info_cache: dict = {}
_info_cache_lock = threading.Lock()

_ROW_RE = re.compile(r"\|\s*(.+?)\s*\|\s*([\d.]+)\s*\|")

_current_game_id: str = ""


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


# ── ryzenadj binary ────────────────────────────────────────────────────────────

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
            member = next(
                (m for m in tar.getmembers()
                 if os.path.basename(m.name) == "ryzenadj" and m.isfile()),
                None,
            )
            if member is None:
                raise RuntimeError("ryzenadj binary not found inside tarball")
            member.name = "ryzenadj"
            tar.extract(member, BIN_DIR)
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


# ── ryzenadj helpers ───────────────────────────────────────────────────────────

def _run_ryzenadj(args: list, timeout: float = 5.0) -> tuple:
    """Run ryzenadj, return (returncode, stdout, stderr).
    Uses Popen so kill() after timeout never calls communicate() and blocks."""
    if not os.path.isfile(BIN_PATH):
        return -1, "", "ryzenadj not found"
    proc = subprocess.Popen([BIN_PATH] + args,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out.decode(), err.decode()
    except subprocess.TimeoutExpired:
        proc.kill()
        logger.warning("ryzenadj timed out: %s", args)
        return -1, "", "timeout"


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


def _apply_ryzenadj(spl_mw: int, sppt_mw: int, fppt_mw: int) -> dict:
    if not _ryzenadj_lock.acquire(timeout=4.0):
        return {"success": False, "stdout": "", "stderr": "ryzenadj busy", "returncode": -1}
    try:
        rc, out, err = _run_ryzenadj([
            f"--stapm-limit={spl_mw}",
            f"--slow-limit={sppt_mw}",
            f"--fast-limit={fppt_mw}",
        ])
        logger.info("ryzenadj apply %dW/%dW/%dW -> rc=%d",
                    spl_mw // 1000, sppt_mw // 1000, fppt_mw // 1000, rc)
        return {"success": rc == 0, "stdout": out, "stderr": err, "returncode": rc}
    finally:
        _ryzenadj_lock.release()


def _read_tdp_live() -> dict:
    """Run ryzenadj --info and update cache. Non-blocking - returns cache if busy."""
    if not _ryzenadj_lock.acquire(blocking=False):
        with _info_cache_lock:
            return dict(_info_cache)
    try:
        rc, out, err = _run_ryzenadj(["--info"], timeout=3.0)
        if rc != 0:
            with _info_cache_lock:
                return dict(_info_cache)
        parsed = _parse_ryzenadj_output(out + err)
        with _info_cache_lock:
            _info_cache.clear()
            _info_cache.update(parsed)
        return parsed
    except Exception as e:
        logger.warning("ryzenadj --info failed: %s", e)
        with _info_cache_lock:
            return dict(_info_cache)
    finally:
        _ryzenadj_lock.release()


# ── Game detection ─────────────────────────────────────────────────────────────

def _get_running_appid() -> str:
    """Scan /proc/*/environ for a running Steam game. Returns appid or ''."""
    for path in glob.glob("/proc/*/environ"):
        try:
            with open(path, "rb") as f:
                for entry in f.read().split(b"\x00"):
                    if entry.startswith(b"SteamAppId="):
                        appid = entry[len(b"SteamAppId="):].decode()
                        if appid and appid != "0":
                            return appid
        except OSError:
            continue
    return ""


# ── TDP enforce ────────────────────────────────────────────────────────────────

def _check_and_enforce() -> None:
    global _current_game_id

    s = _load_settings()
    if not s.get("enabled", True):
        return

    appid = _get_running_appid()

    if appid != _current_game_id:
        prev = _current_game_id
        _current_game_id = appid

        if appid:
            profiles = _load_profiles()
            if appid in profiles:
                p = profiles[appid]
                result = _apply_ryzenadj(p["spl"], p["sppt"], p["fppt"])
                if result["success"]:
                    s["active_spl"]  = p["spl"]
                    s["active_sppt"] = p["sppt"]
                    s["active_fppt"] = p["fppt"]
                    _save_settings(s)
                    logger.info("Auto-applied game profile: app=%s", appid)
                return
        elif prev:
            spl  = s.get("spl",  DEFAULT_SETTINGS["spl"])
            sppt = s.get("sppt", DEFAULT_SETTINGS["sppt"])
            fppt = s.get("fppt", DEFAULT_SETTINGS["fppt"])
            result = _apply_ryzenadj(spl, sppt, fppt)
            if result["success"]:
                s["active_spl"]  = spl
                s["active_sppt"] = sppt
                s["active_fppt"] = fppt
                _save_settings(s)
                logger.info("Game exited, restored global TDP")
            return

    want_spl  = s.get("active_spl",  s.get("spl",  DEFAULT_SETTINGS["spl"]))
    want_sppt = s.get("active_sppt", s.get("sppt", DEFAULT_SETTINGS["sppt"]))
    want_fppt = s.get("active_fppt", s.get("fppt", DEFAULT_SETTINGS["fppt"]))

    if not _ryzenadj_lock.acquire(timeout=4.0):
        return
    try:
        rc, out, err = _run_ryzenadj(["--info"], timeout=3.0)
    finally:
        _ryzenadj_lock.release()

    if rc != 0:
        return

    parsed = _parse_ryzenadj_output(out + err)
    with _info_cache_lock:
        _info_cache.clear()
        _info_cache.update(parsed)

    cur_sppt = parsed.get("sppt_limit")
    cur_fppt = parsed.get("fppt_limit")
    want_sppt_w = want_sppt / 1000
    want_fppt_w = want_fppt / 1000

    if (cur_sppt is None or abs(cur_sppt - want_sppt_w) > 1.0 or
            cur_fppt is None or abs(cur_fppt - want_fppt_w) > 1.0):
        logger.info("TDP drift sppt=%s->%.0fW fppt=%s->%.0fW, re-applying",
                    cur_sppt, want_sppt_w, cur_fppt, want_fppt_w)
        _apply_ryzenadj(want_spl, want_sppt, want_fppt)


# ── Plugin class ───────────────────────────────────────────────────────────────

class Plugin:
    _ready: bool = False
    _setup_error: Optional[str] = None

    async def is_ready(self) -> dict:
        return {"ready": self._ready, "error": self._setup_error}

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
        def _do() -> dict:
            if not _ryzenadj_lock.acquire(timeout=4.0):
                return {"success": False, "stdout": "", "stderr": "ryzenadj busy", "returncode": -1}
            try:
                rc, out, err = _run_ryzenadj(["--max-performance"], timeout=5.0)
                logger.info("restore_defaults rc=%d", rc)
                return {"success": rc == 0, "stdout": out, "stderr": err, "returncode": rc}
            finally:
                _ryzenadj_lock.release()

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do)

    async def get_tdp_info(self) -> dict:
        if not self._ready:
            return {"success": False, "values": {}, "error": "not ready"}
        loop = asyncio.get_running_loop()
        values = await loop.run_in_executor(None, _read_tdp_live)
        return {"success": True, "values": values}

    async def apply_tdp(self, spl: int, sppt: int, fppt: int, app_id: str = "") -> dict:
        if not self._ready:
            return {"success": False, "stderr": "not ready", "stdout": "", "returncode": -1}

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _apply_ryzenadj, spl, sppt, fppt)

        if result["success"]:
            s = _load_settings()
            s["active_spl"]  = spl
            s["active_sppt"] = sppt
            s["active_fppt"] = fppt
            if app_id:
                profiles = _load_profiles()
                profiles[app_id] = {"spl": spl, "sppt": sppt, "fppt": fppt}
                _save_profiles(profiles)
                logger.info("Saved game profile: app=%s", app_id)
            else:
                s["spl"]  = spl
                s["sppt"] = sppt
                s["fppt"] = fppt
            _save_settings(s)

        return result

    async def _enforce_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(5)
            try:
                await loop.run_in_executor(None, _check_and_enforce)
            except Exception as e:
                logger.warning("enforce iteration failed: %s", e)

    async def _main(self):
        logger.info("LeGoTDP: initialising")
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _ensure_ryzenadj)
            self._ready = True
            asyncio.ensure_future(self._enforce_loop())
            logger.info("LeGoTDP: ready")
            s = _load_settings()
            if s.get("enabled", True):
                spl  = s.get("active_spl",  s.get("spl",  DEFAULT_SETTINGS["spl"]))
                sppt = s.get("active_sppt", s.get("sppt", DEFAULT_SETTINGS["sppt"]))
                fppt = s.get("active_fppt", s.get("fppt", DEFAULT_SETTINGS["fppt"]))
                await loop.run_in_executor(None, _apply_ryzenadj, spl, sppt, fppt)
        except Exception as e:
            self._setup_error = str(e)
            logger.error("LeGoTDP: setup failed: %s", e)

    async def _unload(self):
        logger.info("LeGoTDP: unloaded")
