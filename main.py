import decky
import asyncio
import glob
import json
import os
import re
import shutil
import ssl
import stat
import subprocess
import tarfile
import tempfile
import pwd
import threading
import urllib.request
from typing import Optional

PLUGIN_DIR    = os.path.dirname(os.path.abspath(__file__))
BIN_DIR       = os.path.join(PLUGIN_DIR, "bin")
BIN_PATH      = os.path.join(BIN_DIR, "ryzenadj")
SETTINGS_FILE = os.path.join(PLUGIN_DIR, "settings.json")
PROFILES_FILE = os.path.join(PLUGIN_DIR, "profiles.json")
RYZENADJ_URL  = (
    "https://github.com/FlyGoat/RyzenAdj/releases/download/v0.19.0/"
    "ryzenadj-manylinux_2_28-x86_64.tar.gz"
)
GITHUB_API_URL = "https://api.github.com/repos/Rayekkk/LeGoTDP/releases/latest"

DEFAULT_SETTINGS = {"spl": 15000, "sppt": 15000, "fppt": 15000, "enabled": True}

_ryzenadj_lock = threading.Lock()

# Cache of last successful --info parse - keeps UI responsive when lock is held
_info_cache: dict = {}
_info_cache_lock = threading.Lock()

_ROW_RE = re.compile(r"\|\s*(.+?)\s*\|\s*([\d.]+)\s*\|")

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

_current_game_id: str = ""
_current_ac_online: bool = False
_panel_active: bool = False


# ── AC power detection ─────────────────────────────────────────────────────────

def _get_ac_online() -> bool:
    for path in glob.glob("/sys/class/power_supply/*/online"):
        try:
            with open(path) as f:
                if f.read().strip() == "1":
                    return True
        except OSError:
            continue
    return False


def _pick_profile_values(p: dict, ac_online: bool) -> tuple:
    if ac_online and p.get("ac_separate") and p.get("ac_spl") is not None:
        return (
            p["ac_spl"],
            p.get("ac_sppt", p.get("sppt", DEFAULT_SETTINGS["sppt"])),
            p.get("ac_fppt", p.get("fppt", DEFAULT_SETTINGS["fppt"])),
        )
    return (
        p.get("spl",  DEFAULT_SETTINGS["spl"]),
        p.get("sppt", DEFAULT_SETTINGS["sppt"]),
        p.get("fppt", DEFAULT_SETTINGS["fppt"]),
    )


# ── JSON persistence ───────────────────────────────────────────────────────────

def _load_json(path: str, default: dict) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return dict(default)


def _save_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _load_settings() -> dict:
    return _load_json(SETTINGS_FILE, DEFAULT_SETTINGS)


def _save_settings(s: dict) -> None:
    _save_json(SETTINGS_FILE, s)


# ── Per-game profiles ──────────────────────────────────────────────────────────

def _load_profiles() -> dict:
    return _load_json(PROFILES_FILE, {})


def _save_profiles(profiles: dict) -> None:
    _save_json(PROFILES_FILE, profiles)


def _save_active(s: dict, spl: int, sppt: int, fppt: int) -> None:
    s["active_spl"]  = spl
    s["active_sppt"] = sppt
    s["active_fppt"] = fppt
    _save_settings(s)


# ── ryzenadj binary ────────────────────────────────────────────────────────────

def _download_ryzenadj() -> None:
    decky.logger.info(f"[legotdp] Downloading ryzenadj from {RYZENADJ_URL}")
    os.makedirs(BIN_DIR, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz")
    os.close(tmp_fd)
    try:
        with urllib.request.urlopen(RYZENADJ_URL, context=_ssl_ctx, timeout=30) as resp, \
             open(tmp_path, "wb") as out:
            shutil.copyfileobj(resp, out)
        with tarfile.open(tmp_path, "r:gz") as tar:
            member = next(
                (m for m in tar.getmembers()
                 if os.path.basename(m.name) == "ryzenadj" and m.isfile()),
                None,
            )
            if member is None:
                raise RuntimeError("ryzenadj binary not found inside tarball")
            member.name = "ryzenadj"
            try:
                tar.extract(member, BIN_DIR, filter='data')
            except TypeError:  # Python < 3.12
                tar.extract(member, BIN_DIR)
        os.chmod(BIN_PATH, 0o755)
        decky.logger.info(f"[legotdp] ryzenadj installed at {BIN_PATH}")
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
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            decky.logger.warning("[legotdp] ryzenadj process could not be killed")
        decky.logger.warning(f"[legotdp] ryzenadj timed out: {args}")
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
        decky.logger.info(f"[legotdp] ryzenadj apply {spl_mw//1000}W/{sppt_mw//1000}W/{fppt_mw//1000}W -> rc={rc}")
        return {"success": rc == 0, "stdout": out, "stderr": err, "returncode": rc}
    finally:
        _ryzenadj_lock.release()


# ── Info cache refresh ─────────────────────────────────────────────────────────

def _refresh_info_cache() -> None:
    if not _ryzenadj_lock.acquire(blocking=False):
        return
    try:
        rc, out, err = _run_ryzenadj(["--info"], timeout=3.0)
        if rc != 0:
            return
        parsed = _parse_ryzenadj_output(out)
        with _info_cache_lock:
            _info_cache.clear()
            _info_cache.update(parsed)
    except Exception:
        pass
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
                        appid = entry[len(b"SteamAppId="):].decode(errors="replace")
                        if appid and appid != "0":
                            return appid
        except OSError:
            continue
    return ""


# ── TDP enforce ────────────────────────────────────────────────────────────────

def _check_and_enforce() -> None:
    global _current_game_id, _current_ac_online

    s = _load_settings()
    if not s.get("enabled", True):
        return

    appid    = _get_running_appid()
    ac_now   = _get_ac_online()
    ac_changed = ac_now != _current_ac_online
    _current_ac_online = ac_now

    game_changed = appid != _current_game_id

    if game_changed or ac_changed:
        prev = _current_game_id if game_changed else appid
        _current_game_id = appid

        if appid:
            profiles = _load_profiles()
            if appid in profiles:
                p = profiles[appid]
                spl, sppt, fppt = _pick_profile_values(p, ac_now)
                result = _apply_ryzenadj(spl, sppt, fppt)
                if result["success"]:
                    s = _load_settings()
                    _save_active(s, spl, sppt, fppt)
                    reason = "AC state change" if ac_changed else "game launch"
                    decky.logger.info(f"[legotdp] Auto-applied game profile ({reason}): app={appid} ac={ac_now}")
                else:
                    decky.logger.warning(f"[legotdp] Failed to apply game profile: app={appid} rc={result['returncode']} err={result['stderr']}")
                return
            # Game running but no profile — apply global TDP to avoid enforcing stale active_*
            spl  = s.get("spl",  DEFAULT_SETTINGS["spl"])
            sppt = s.get("sppt", DEFAULT_SETTINGS["sppt"])
            fppt = s.get("fppt", DEFAULT_SETTINGS["fppt"])
            result = _apply_ryzenadj(spl, sppt, fppt)
            if result["success"]:
                s = _load_settings()
                _save_active(s, spl, sppt, fppt)
                decky.logger.info(f"[legotdp] Game launched with no profile, applied global TDP: app={appid}")
            return
        elif game_changed and prev:
            spl  = s.get("spl",  DEFAULT_SETTINGS["spl"])
            sppt = s.get("sppt", DEFAULT_SETTINGS["sppt"])
            fppt = s.get("fppt", DEFAULT_SETTINGS["fppt"])
            result = _apply_ryzenadj(spl, sppt, fppt)
            if result["success"]:
                s = _load_settings()
                _save_active(s, spl, sppt, fppt)
                decky.logger.info("[legotdp] Game exited, restored global TDP")
            return
        elif ac_changed:
            spl  = s.get("spl",  DEFAULT_SETTINGS["spl"])
            sppt = s.get("sppt", DEFAULT_SETTINGS["sppt"])
            fppt = s.get("fppt", DEFAULT_SETTINGS["fppt"])
            result = _apply_ryzenadj(spl, sppt, fppt)
            if result["success"]:
                s = _load_settings()
                _save_active(s, spl, sppt, fppt)
                decky.logger.info(f"[legotdp] Re-applied global TDP on AC change: ac={ac_now}")
            return

    s = _load_settings()
    want_spl  = s.get("active_spl",  s.get("spl",  DEFAULT_SETTINGS["spl"]))
    want_sppt = s.get("active_sppt", s.get("sppt", DEFAULT_SETTINGS["sppt"]))
    want_fppt = s.get("active_fppt", s.get("fppt", DEFAULT_SETTINGS["fppt"]))

    with _info_cache_lock:
        parsed = dict(_info_cache) if _panel_active else {}

    if not parsed:
        if not _ryzenadj_lock.acquire(timeout=4.0):
            return
        try:
            rc, out, err = _run_ryzenadj(["--info"], timeout=3.0)
        finally:
            _ryzenadj_lock.release()
        if rc != 0:
            return
        parsed = _parse_ryzenadj_output(out)
        with _info_cache_lock:
            _info_cache.update(parsed)

    cur_spl  = parsed.get("spl_limit")
    cur_sppt = parsed.get("sppt_limit")
    cur_fppt = parsed.get("fppt_limit")
    want_spl_w  = want_spl  / 1000
    want_sppt_w = want_sppt / 1000
    want_fppt_w = want_fppt / 1000

    if (cur_spl  is None or abs(cur_spl  - want_spl_w)  > 1.0 or
            cur_sppt is None or abs(cur_sppt - want_sppt_w) > 1.0 or
            cur_fppt is None or abs(cur_fppt - want_fppt_w) > 1.0):
        decky.logger.info(
            f"[legotdp] TDP drift spl={cur_spl}->{want_spl_w:.0f}W "
            f"sppt={cur_sppt}->{want_sppt_w:.0f}W fppt={cur_fppt}->{want_fppt_w:.0f}W, re-applying"
        )
        result = _apply_ryzenadj(want_spl, want_sppt, want_fppt)
        if not result["success"]:
            decky.logger.warning(f"[legotdp] drift re-apply failed rc={result['returncode']} err={result['stderr']}")


def _xdg_download_dir(home_dir: str) -> str:
    try:
        with open(os.path.join(home_dir, ".config", "user-dirs.dirs")) as f:
            for line in f:
                line = line.strip()
                if line.startswith("XDG_DOWNLOAD_DIR="):
                    value = line.split("=", 1)[1].strip('"')
                    return value.replace("$HOME", home_dir)
    except OSError:
        pass
    return os.path.join(home_dir, "Downloads")


# ── Plugin class ───────────────────────────────────────────────────────────────

class Plugin:
    _ready: bool = False
    _setup_error: Optional[str] = None

    async def is_ready(self) -> dict:
        return {"ready": self._ready, "error": self._setup_error}

    async def get_settings(self) -> dict:
        return _load_settings()

    async def get_power_source(self) -> dict:
        loop = asyncio.get_running_loop()
        return {"ac": await loop.run_in_executor(None, _get_ac_online)}

    async def get_extras_unlocked(self) -> bool:
        loop = asyncio.get_running_loop()
        s = await loop.run_in_executor(None, _load_settings)
        return s.get("extras_unlocked", False)

    async def set_extras_unlocked(self, enabled: bool) -> None:
        def _do():
            s = _load_settings()
            s["extras_unlocked"] = enabled
            _save_settings(s)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _do)
        decky.logger.info(f"[legotdp] extras_unlocked={enabled}")

    async def get_game_profile(self, app_id: str) -> dict:
        profiles = _load_profiles()
        p = profiles.get(app_id)
        if p is None:
            return {"exists": False, "profile": {}, "ac_separate": False, "ac_profile": {}}
        spl  = p.get("spl",  DEFAULT_SETTINGS["spl"])
        sppt = p.get("sppt", DEFAULT_SETTINGS["sppt"])
        fppt = p.get("fppt", DEFAULT_SETTINGS["fppt"])
        return {
            "exists":      True,
            "profile":     {"spl": spl, "sppt": sppt, "fppt": fppt, "preset": p.get("preset", "")},
            "ac_separate": p.get("ac_separate", False),
            "ac_profile":  {"spl": p.get("ac_spl", spl), "sppt": p.get("ac_sppt", sppt), "fppt": p.get("ac_fppt", fppt), "ac_preset": p.get("ac_preset", "")},
        }

    async def set_game_ac_profile(self, app_id: str, spl: int, sppt: int, fppt: int, ac_separate: bool, preset_name: str = "") -> dict:
        profiles = _load_profiles()
        p = profiles.get(app_id, {})
        update = {"ac_separate": ac_separate, "ac_spl": spl, "ac_sppt": sppt, "ac_fppt": fppt}
        if preset_name:
            update["ac_preset"] = preset_name
        p.update(update)
        profiles[app_id] = p
        _save_profiles(profiles)
        decky.logger.info(f"[legotdp] Saved AC profile: app={app_id} separate={ac_separate}")
        ac_now = _get_ac_online()
        loop = asyncio.get_running_loop()
        if ac_now and ac_separate:
            result = await loop.run_in_executor(None, _apply_ryzenadj, spl, sppt, fppt)
            if result["success"]:
                s = _load_settings()
                _save_active(s, spl, sppt, fppt)
            return result
        if ac_now and not ac_separate and p.get("spl") is not None and p.get("sppt") is not None and p.get("fppt") is not None:
            result = await loop.run_in_executor(None, _apply_ryzenadj, p["spl"], p["sppt"], p["fppt"])
            if result["success"]:
                s = _load_settings()
                _save_active(s, p["spl"], p["sppt"], p["fppt"])
            return result
        return {"success": True, "stderr": "", "stdout": "", "returncode": 0}

    async def delete_game_profile(self, app_id: str) -> None:
        profiles = _load_profiles()
        profiles.pop(app_id, None)
        _save_profiles(profiles)
        decky.logger.info(f"[legotdp] Deleted game profile: app={app_id}")

    async def set_plugin_enabled(self, enabled: bool) -> None:
        s = _load_settings()
        s["enabled"] = enabled
        _save_settings(s)
        decky.logger.info(f"[legotdp] Plugin enabled={enabled}")

    async def restore_defaults(self) -> dict:
        def _do() -> dict:
            if not _ryzenadj_lock.acquire(timeout=4.0):
                return {"success": False, "stdout": "", "stderr": "ryzenadj busy", "returncode": -1}
            try:
                rc, out, err = _run_ryzenadj(["--max-performance"], timeout=5.0)
                decky.logger.info(f"[legotdp] restore_defaults rc={rc}")
                return {"success": rc == 0, "stdout": out, "stderr": err, "returncode": rc}
            finally:
                _ryzenadj_lock.release()

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do)

    async def set_panel_active(self, active: bool) -> None:
        global _panel_active
        _panel_active = active

    async def get_tdp_info(self) -> dict:
        if not self._ready:
            return {"success": False, "values": {}, "error": "not ready"}
        with _info_cache_lock:
            return {"success": True, "values": dict(_info_cache)}

    async def apply_tdp(self, spl: int, sppt: int, fppt: int, app_id: str = "", preset_name: str = "") -> dict:
        if not self._ready:
            return {"success": False, "stderr": "not ready", "stdout": "", "returncode": -1}

        loop = asyncio.get_running_loop()
        profiles: Optional[dict] = None
        existing: dict = {}
        apply_spl, apply_sppt, apply_fppt = spl, sppt, fppt

        if app_id:
            profiles = _load_profiles()
            existing = profiles.get(app_id, {})
            if _get_ac_online() and existing.get("ac_separate") and existing.get("ac_spl") is not None:
                apply_spl  = existing["ac_spl"]
                apply_sppt = existing.get("ac_sppt", existing.get("sppt", DEFAULT_SETTINGS["sppt"]))
                apply_fppt = existing.get("ac_fppt", existing.get("fppt", DEFAULT_SETTINGS["fppt"]))

        result = await loop.run_in_executor(None, _apply_ryzenadj, apply_spl, apply_sppt, apply_fppt)

        if result["success"]:
            s = _load_settings()
            if app_id:
                existing.update({"spl": spl, "sppt": sppt, "fppt": fppt})
                if preset_name:
                    existing["preset"] = preset_name
                profiles[app_id] = existing
                _save_profiles(profiles)
                decky.logger.info(f"[legotdp] Saved game profile: app={app_id}")
            else:
                s["spl"]  = spl
                s["sppt"] = sppt
                s["fppt"] = fppt
                s["active_preset"] = preset_name
            _save_active(s, apply_spl, apply_sppt, apply_fppt)

        return result

    async def check_update(self) -> dict:
        def _do() -> dict:
            try:
                req = urllib.request.Request(
                    GITHUB_API_URL,
                    headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "LeGoTDP"},
                )
                with urllib.request.urlopen(req, context=_ssl_ctx, timeout=10) as resp:
                    data = json.loads(resp.read())
                tag = data.get("tag_name", "")
                if not tag:
                    raise ValueError("GitHub API response missing tag_name")
                latest_ver = tag.lstrip("v").split("-")[0]
                with open(os.path.join(PLUGIN_DIR, "plugin.json")) as f:
                    current_ver = json.load(f).get("version", "0.0.0").split("-")[0]
                def _v(s):
                    return tuple(int(x) for x in s.split("."))
                asset = next((a for a in data.get("assets", []) if a.get("name", "").endswith(".zip")), None)
                return {
                    "current_version":  current_ver,
                    "latest_version":   latest_ver,
                    "update_available": _v(latest_ver) > _v(current_ver),
                    "download_url":     asset.get("browser_download_url") if asset else None,
                    "asset_name":       asset.get("name") if asset else None,
                }
            except Exception as e:
                decky.logger.error(f"[legotdp] check_update: {e}")
                return {"error": str(e)}
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do)

    async def perform_update(self, download_url: str, asset_name: str) -> dict:
        def _do() -> dict:
            try:
                user = next(
                    (p for p in sorted(pwd.getpwall(), key=lambda p: p.pw_uid)
                     if p.pw_uid >= 1000 and os.path.isdir(p.pw_dir)),
                    None,
                )
                downloads_dir = _xdg_download_dir(user.pw_dir) if user else "/home/deck/Downloads"
                os.makedirs(downloads_dir, exist_ok=True)
                dest = os.path.join(downloads_dir, os.path.basename(asset_name))
                try:
                    os.unlink(dest)
                except FileNotFoundError:
                    pass
                with urllib.request.urlopen(download_url, context=_ssl_ctx, timeout=60) as resp, \
                     open(dest, "wb") as f:
                    shutil.copyfileobj(resp, f)
                decky.logger.info(f"[legotdp] update downloaded to {dest}")
                return {"success": True, "path": dest}
            except Exception as e:
                decky.logger.error(f"[legotdp] perform_update: {e}")
                return {"success": False, "error": str(e)}
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do)

    async def _info_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(2)
            if _panel_active:
                try:
                    await loop.run_in_executor(None, _refresh_info_cache)
                except Exception as e:
                    decky.logger.warning(f"[legotdp] info loop error: {e}")

    async def _enforce_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(5)
            try:
                await loop.run_in_executor(None, _check_and_enforce)
            except Exception as e:
                decky.logger.warning(f"[legotdp] enforce iteration failed: {e}")

    async def _main(self):
        decky.logger.info("[legotdp] initialising")
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _ensure_ryzenadj)
            self._ready = True
            asyncio.create_task(self._enforce_loop())
            asyncio.create_task(self._info_loop())
            decky.logger.info("[legotdp] ready")
            s = _load_settings()
            if s.get("enabled", True):
                spl  = s.get("active_spl",  s.get("spl",  DEFAULT_SETTINGS["spl"]))
                sppt = s.get("active_sppt", s.get("sppt", DEFAULT_SETTINGS["sppt"]))
                fppt = s.get("active_fppt", s.get("fppt", DEFAULT_SETTINGS["fppt"]))
                await loop.run_in_executor(None, _apply_ryzenadj, spl, sppt, fppt)
        except Exception as e:
            self._setup_error = str(e)
            decky.logger.error(f"[legotdp] setup failed: {e}")

    async def _unload(self):
        decky.logger.info("[legotdp] unloaded")
