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
import time
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

# Absolute floor/ceiling for any single limit, in milliwatts. Applied on load, which
# also migrates profiles saved back when the Extras ceiling was 60 W.
HARD_MIN_MW = 5000
HARD_MAX_MW = 50000

# Lenovo firmware attributes. Writing these goes through the EC instead of poking the
# SMU directly, so the firmware stops fighting us and the values survive suspend.
WMI_ROOT  = "/sys/class/firmware-attributes/lenovo-wmi-other-0/attributes"
WMI_ATTRS = {"spl": "ppt_pl1_spl", "sppt": "ppt_pl2_sppt", "fppt": "ppt_pl3_fppt"}
PLATFORM_PROFILE_GLOB = "/sys/class/platform-profile/*/profile"

# Package energy counter, used instead of spawning `ryzenadj --info` every 2 s.
RAPL_GLOB = "/sys/class/powercap/intel-rapl:*"

_ryzenadj_lock = threading.Lock()
# Serialises every hardware apply. The ryzenadj path has its own lock, but the WMI
# path (profile bounce + three ppt writes) is not atomic, so concurrent applies from
# the enforce loop and a user action could interleave and corrupt each other.
_apply_lock = threading.Lock()

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


def _clamp_triplet(spl, sppt, fppt) -> tuple:
    """Enforce 5 W <= spl <= sppt <= fppt <= 50 W (milliwatts).

    SPPT/FPPT are offsets above SPL in the UI, so they can never sit below it.
    """
    try:
        spl, sppt, fppt = int(spl), int(sppt), int(fppt)
    except (TypeError, ValueError):
        return DEFAULT_SETTINGS["spl"], DEFAULT_SETTINGS["sppt"], DEFAULT_SETTINGS["fppt"]
    spl  = max(HARD_MIN_MW, min(spl,  HARD_MAX_MW))
    fppt = max(spl,         min(fppt, HARD_MAX_MW))
    sppt = max(spl,         min(sppt, fppt))
    return spl, sppt, fppt


def _load_settings() -> dict:
    s = _load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    s["spl"], s["sppt"], s["fppt"] = _clamp_triplet(
        s.get("spl",  DEFAULT_SETTINGS["spl"]),
        s.get("sppt", DEFAULT_SETTINGS["sppt"]),
        s.get("fppt", DEFAULT_SETTINGS["fppt"]),
    )
    if any(k in s for k in ("active_spl", "active_sppt", "active_fppt")):
        s["active_spl"], s["active_sppt"], s["active_fppt"] = _clamp_triplet(
            s.get("active_spl",  s["spl"]),
            s.get("active_sppt", s["sppt"]),
            s.get("active_fppt", s["fppt"]),
        )
    return s


def _save_settings(s: dict) -> None:
    _save_json(SETTINGS_FILE, s)


# ── Per-game profiles ──────────────────────────────────────────────────────────

def _load_profiles() -> dict:
    profiles = _load_json(PROFILES_FILE, {})
    for p in profiles.values():
        if not isinstance(p, dict):
            continue
        if p.get("spl") is not None:
            p["spl"], p["sppt"], p["fppt"] = _clamp_triplet(
                p["spl"], p.get("sppt", p["spl"]), p.get("fppt", p["spl"]))
        if p.get("ac_spl") is not None:
            p["ac_spl"], p["ac_sppt"], p["ac_fppt"] = _clamp_triplet(
                p["ac_spl"], p.get("ac_sppt", p["ac_spl"]), p.get("ac_fppt", p["ac_spl"]))
    return profiles


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


# ── Lenovo WMI firmware attributes ─────────────────────────────────────────────

def _wmi_path(key: str, leaf: str) -> str:
    return os.path.join(WMI_ROOT, WMI_ATTRS[key], leaf)


def _wmi_read(key: str, leaf: str) -> Optional[int]:
    try:
        with open(_wmi_path(key, leaf)) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _wmi_caps() -> dict:
    """Firmware-reported {min,max} in watts per parameter, or {} when unavailable."""
    caps = {}
    for key in WMI_ATTRS:
        lo, hi = _wmi_read(key, "min_value"), _wmi_read(key, "max_value")
        if lo is None or hi is None:
            return {}
        caps[key] = {"min": lo, "max": hi}
    return caps


def _profile_path() -> Optional[str]:
    """The platform-profile node whose choices include 'custom' (the tunable one)."""
    for path in glob.glob(PLATFORM_PROFILE_GLOB):
        try:
            with open(os.path.join(os.path.dirname(path), "choices")) as f:
                if "custom" in f.read().split():
                    return path
        except OSError:
            continue
    return None


def _read_profile(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _write_profile(path: str, value: str) -> bool:
    try:
        with open(path, "w") as f:
            f.write(value)
        return True
    except OSError:
        return False


def _write_ppt(spl_w: int, sppt_w: int, fppt_w: int) -> None:
    for key, val in (("spl", spl_w), ("sppt", sppt_w), ("fppt", fppt_w)):
        try:
            with open(_wmi_path(key, "current_value"), "w") as f:
                f.write(str(val))
        except OSError:
            pass


def _ppt_matches(spl_w: int, sppt_w: int, fppt_w: int) -> bool:
    return all(_wmi_read(k, "current_value") == v
               for k, v in (("spl", spl_w), ("sppt", sppt_w), ("fppt", fppt_w)))


# Verified on the Legion Go 2: the firmware only latches ppt_* writes when the
# platform profile transitions *into* 'custom'. Writing while already in custom is
# silently dropped, and entering custom resets the values to firmware defaults - so
# the reliable recipe is bounce-through-another-profile, then write.
def _apply_wmi(spl_w: int, sppt_w: int, fppt_w: int) -> dict:
    path = _profile_path()
    if path is None:
        return {"success": False, "stdout": "", "stderr": "no custom platform profile",
                "returncode": -1}

    # Fast path: if we can latch a write in place, skip the visible profile bounce.
    if _read_profile(path) == "custom":
        _write_ppt(spl_w, sppt_w, fppt_w)
        if _ppt_matches(spl_w, sppt_w, fppt_w):
            decky.logger.info(f"[legotdp] wmi apply {spl_w}W/{sppt_w}W/{fppt_w}W")
            return {"success": True, "stdout": "", "stderr": "", "returncode": 0}

    # Force a real transition into custom, then write. Bounce via a low profile so the
    # momentary blip is downward, never a spike.
    bounce = "low-power"
    try:
        with open(os.path.join(os.path.dirname(path), "choices")) as f:
            choices = f.read().split()
        bounce = next((c for c in ("low-power", "balanced", "performance") if c in choices),
                      next((c for c in choices if c != "custom"), "custom"))
    except OSError:
        pass
    _write_profile(path, bounce)
    if not _write_profile(path, "custom"):
        return {"success": False, "stdout": "", "stderr": "cannot select custom profile",
                "returncode": -1}
    _write_ppt(spl_w, sppt_w, fppt_w)

    if not _ppt_matches(spl_w, sppt_w, fppt_w):
        mismatch = "; ".join(
            f"{WMI_ATTRS[k]}={_wmi_read(k, 'current_value')} want {v}"
            for k, v in (("spl", spl_w), ("sppt", sppt_w), ("fppt", fppt_w))
            if _wmi_read(k, "current_value") != v)
        return {"success": False, "stdout": "", "stderr": mismatch, "returncode": -1}
    decky.logger.info(f"[legotdp] wmi apply {spl_w}W/{sppt_w}W/{fppt_w}W (via bounce)")
    return {"success": True, "stdout": "", "stderr": "", "returncode": 0}


# ── RAPL package power ─────────────────────────────────────────────────────────

_rapl_dir: Optional[str] = None
_rapl_last: tuple = ()


def _find_rapl_package() -> Optional[str]:
    global _rapl_dir
    if _rapl_dir is None:
        _rapl_dir = ""
        for d in sorted(glob.glob(RAPL_GLOB)):
            try:
                with open(os.path.join(d, "name")) as f:
                    if f.read().strip().startswith("package"):
                        _rapl_dir = d
                        break
            except OSError:
                continue
    return _rapl_dir or None


def _rapl_watts() -> Optional[float]:
    """Average package draw since the previous call, in watts."""
    global _rapl_last
    d = _find_rapl_package()
    if not d:
        return None
    try:
        with open(os.path.join(d, "energy_uj")) as f:
            energy = int(f.read().strip())
    except (OSError, ValueError):
        return None
    now = time.monotonic()
    prev, _rapl_last = _rapl_last, (energy, now)
    if not prev:
        return None
    delta_e, delta_t = energy - prev[0], now - prev[1]
    if delta_t <= 0:
        return None
    if delta_e < 0:  # counter wrapped
        try:
            with open(os.path.join(d, "max_energy_range_uj")) as f:
                delta_e += int(f.read().strip())
        except (OSError, ValueError):
            return None
    return delta_e / delta_t / 1_000_000


# ── Apply dispatcher ───────────────────────────────────────────────────────────

_last_source: str = ""


def _apply_limits(spl_mw: int, sppt_mw: int, fppt_mw: int) -> dict:
    """Prefer the firmware path; fall back to ryzenadj only when the request exceeds
    what the firmware accepts (the Extras range)."""
    global _last_source
    spl_mw, sppt_mw, fppt_mw = _clamp_triplet(spl_mw, sppt_mw, fppt_mw)
    triple_w = (("spl", spl_mw // 1000), ("sppt", sppt_mw // 1000), ("fppt", fppt_mw // 1000))
    if not _apply_lock.acquire(timeout=8.0):
        return {"success": False, "stdout": "", "stderr": "apply busy", "returncode": -1}
    try:
        caps = _wmi_caps()
        if caps and all(caps[k]["min"] <= v <= caps[k]["max"] for k, v in triple_w):
            result = _apply_wmi(*(v for _, v in triple_w))
            if result["success"]:
                _last_source = "wmi"
                return result
            decky.logger.warning(
                f"[legotdp] WMI apply failed ({result['stderr']}), falling back to ryzenadj")
        result = _apply_ryzenadj(spl_mw, sppt_mw, fppt_mw)
        if result["success"]:
            _last_source = "ryzenadj"
        return result
    finally:
        _apply_lock.release()


def _wmi_profile_lost() -> bool:
    """True when the last apply was via WMI but the platform profile is no longer
    'custom', so the ppt_* attributes still read the old values yet no longer bind.
    Something external (Steam, amd_pmf, gamezone) knocked us off custom."""
    if _last_source != "wmi":
        return False
    path = _profile_path()
    return path is not None and _read_profile(path) != "custom"


def _read_limits() -> dict:
    """Current limits in watts, read from whichever layer last applied them.

    The two layers do not observe each other: after a ryzenadj write the WMI
    attributes still report the firmware's own stale bookkeeping, so reading the
    wrong one would misreport the active limits.
    """
    if _last_source == "wmi":
        vals = {f"{k}_limit": _wmi_read(k, "current_value") for k in WMI_ATTRS}
        if all(v is not None for v in vals.values()):
            return {k: float(v) for k, v in vals.items()}
    if not _ryzenadj_lock.acquire(timeout=4.0):
        return {}
    try:
        rc, out, _ = _run_ryzenadj(["--info"], timeout=3.0)
    finally:
        _ryzenadj_lock.release()
    return _parse_ryzenadj_output(out) if rc == 0 else {}


# ── Info cache refresh ─────────────────────────────────────────────────────────

def _refresh_info_cache() -> None:
    values = _read_limits()
    watts  = _rapl_watts()
    with _info_cache_lock:
        if values:
            _info_cache.clear()
            _info_cache.update(values)
        if watts is not None:
            _info_cache["package_draw"] = round(watts, 1)
        _info_cache["source"] = _last_source or "wmi"


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
                result = _apply_limits(spl, sppt, fppt)
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
            result = _apply_limits(spl, sppt, fppt)
            if result["success"]:
                s = _load_settings()
                _save_active(s, spl, sppt, fppt)
                decky.logger.info(f"[legotdp] Game launched with no profile, applied global TDP: app={appid}")
            else:
                decky.logger.warning(f"[legotdp] Failed to apply global TDP on game launch: app={appid} rc={result['returncode']} err={result['stderr']}")
            return
        elif game_changed and prev:
            spl  = s.get("spl",  DEFAULT_SETTINGS["spl"])
            sppt = s.get("sppt", DEFAULT_SETTINGS["sppt"])
            fppt = s.get("fppt", DEFAULT_SETTINGS["fppt"])
            result = _apply_limits(spl, sppt, fppt)
            if result["success"]:
                s = _load_settings()
                _save_active(s, spl, sppt, fppt)
                decky.logger.info("[legotdp] Game exited, restored global TDP")
            else:
                decky.logger.warning(f"[legotdp] Failed to restore global TDP on game exit: rc={result['returncode']} err={result['stderr']}")
            return
        elif ac_changed:
            spl  = s.get("spl",  DEFAULT_SETTINGS["spl"])
            sppt = s.get("sppt", DEFAULT_SETTINGS["sppt"])
            fppt = s.get("fppt", DEFAULT_SETTINGS["fppt"])
            result = _apply_limits(spl, sppt, fppt)
            if result["success"]:
                s = _load_settings()
                _save_active(s, spl, sppt, fppt)
                decky.logger.info(f"[legotdp] Re-applied global TDP on AC change: ac={ac_now}")
            else:
                decky.logger.warning(f"[legotdp] Failed to re-apply global TDP on AC change: ac={ac_now} rc={result['returncode']} err={result['stderr']}")
            return

    s = _load_settings()
    _enforce_target(_clamp_triplet(
        s.get("active_spl",  s.get("spl",  DEFAULT_SETTINGS["spl"])),
        s.get("active_sppt", s.get("sppt", DEFAULT_SETTINGS["sppt"])),
        s.get("active_fppt", s.get("fppt", DEFAULT_SETTINGS["fppt"])),
    ))


DRIFT_TOLERANCE_W  = 1.0
DRIFT_MAX_ATTEMPTS = 3

_drift_target:   tuple = ()
_drift_settled:  tuple = ()
_drift_attempts: int   = 0


def _enforce_target(want: tuple) -> None:
    """Re-apply `want` when the hardware has drifted off it.

    Some targets are simply unreachable - the SMU silently caps slow-limit around
    50 W, for instance - and chasing those forever re-ran ryzenadj every 5 s and
    flooded the log. After a few failed attempts we accept whatever the hardware
    settled on, and only act again if it moves away from that.
    """
    global _drift_target, _drift_settled, _drift_attempts

    if want != _drift_target:
        _drift_target, _drift_settled, _drift_attempts = want, (), 0

    with _info_cache_lock:
        parsed = dict(_info_cache) if _panel_active else {}
    if not parsed:
        parsed = _read_limits()
    cur = tuple(parsed.get(f"{k}_limit") for k in ("spl", "sppt", "fppt"))
    if any(v is None for v in cur):
        return

    want_w    = tuple(v / 1000 for v in want)
    reference = _drift_settled or want_w
    # The WMI attributes keep reporting the last value even after the profile leaves
    # 'custom', so a matching read is not proof the limit is actually enforced - force
    # a re-apply (which re-selects custom) when we detect that.
    profile_lost = _wmi_profile_lost()
    if not profile_lost and all(abs(c - r) <= DRIFT_TOLERANCE_W for c, r in zip(cur, reference)):
        return

    if profile_lost:
        decky.logger.info("[legotdp] platform profile left 'custom', re-asserting limits")
        _drift_settled, _drift_attempts = (), 0

    if _drift_settled:
        # Moved off the value we had accepted, so something external changed it.
        # Give the real target another go.
        _drift_settled, _drift_attempts = (), 0

    if _drift_attempts >= DRIFT_MAX_ATTEMPTS:
        _drift_settled = cur
        decky.logger.warning(
            f"[legotdp] target {want_w} unreachable after {_drift_attempts} attempts, "
            f"accepting {cur} and standing down")
        return

    _drift_attempts += 1
    decky.logger.info(
        f"[legotdp] TDP drift {cur} -> {want_w}, re-applying (attempt {_drift_attempts})")
    result = _apply_limits(*want)
    if not result["success"]:
        decky.logger.warning(
            f"[legotdp] drift re-apply failed rc={result['returncode']} err={result['stderr']}")


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
    _tasks: list = []

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
            result = await loop.run_in_executor(None, _apply_limits, spl, sppt, fppt)
            if result["success"]:
                s = _load_settings()
                _save_active(s, spl, sppt, fppt)
            return result
        if ac_now and not ac_separate and p.get("spl") is not None and p.get("sppt") is not None and p.get("fppt") is not None:
            result = await loop.run_in_executor(None, _apply_limits, p["spl"], p["sppt"], p["fppt"])
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

    async def get_caps(self) -> dict:
        """Slider ceilings in watts. `std` is what the firmware accepts over WMI;
        `max` is the Extras range, which falls through to ryzenadj."""
        def _do() -> dict:
            caps = _wmi_caps()
            return {
                "min": min(caps[k]["min"] for k in WMI_ATTRS) if caps else HARD_MIN_MW // 1000,
                "std": {k: caps[k]["max"] for k in WMI_ATTRS} if caps
                       else {"spl": 35, "sppt": 37, "fppt": 45},
                "max": {k: HARD_MAX_MW // 1000 for k in WMI_ATTRS},
                "wmi": bool(caps),
            }
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _do)

    async def restore_defaults(self) -> dict:
        def _do() -> dict:
            global _drift_target, _drift_settled, _drift_attempts
            if not _apply_lock.acquire(timeout=8.0):
                return {"success": False, "stdout": "", "stderr": "apply busy", "returncode": -1}
            try:
                _drift_target, _drift_settled, _drift_attempts = (), (), 0
                # With WMI present the honest "restore defaults" is handing the profile
                # back to the firmware, rather than pinning ryzenadj to max performance.
                path = _profile_path()
                if _wmi_caps() and path and _write_profile(path, "balanced"):
                    decky.logger.info("[legotdp] restore_defaults: platform profile -> balanced")
                    return {"success": True, "stdout": "", "stderr": "", "returncode": 0}
                if not _ryzenadj_lock.acquire(timeout=4.0):
                    return {"success": False, "stdout": "", "stderr": "ryzenadj busy", "returncode": -1}
                try:
                    rc, out, err = _run_ryzenadj(["--max-performance"], timeout=5.0)
                    decky.logger.info(f"[legotdp] restore_defaults rc={rc}")
                    return {"success": rc == 0, "stdout": out, "stderr": err, "returncode": rc}
                finally:
                    _ryzenadj_lock.release()
            finally:
                _apply_lock.release()

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

        result = await loop.run_in_executor(None, _apply_limits, apply_spl, apply_sppt, apply_fppt)

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
                created_dir = not os.path.isdir(downloads_dir)
                os.makedirs(downloads_dir, exist_ok=True)
                # Plugin runs as root - hand ownership back so the user can manage the file
                if user and created_dir:
                    os.chown(downloads_dir, user.pw_uid, user.pw_gid)
                dest = os.path.join(downloads_dir, os.path.basename(asset_name))
                try:
                    os.unlink(dest)
                except FileNotFoundError:
                    pass
                with urllib.request.urlopen(download_url, context=_ssl_ctx, timeout=60) as resp, \
                     open(dest, "wb") as f:
                    shutil.copyfileobj(resp, f)
                if user:
                    os.chown(dest, user.pw_uid, user.pw_gid)
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
            global _current_ac_online
            loop = asyncio.get_running_loop()
            # Seed this so the first enforce pass does not report a phantom AC change.
            _current_ac_online = await loop.run_in_executor(None, _get_ac_online)
            wmi = await loop.run_in_executor(None, _wmi_caps)
            try:
                await loop.run_in_executor(None, _ensure_ryzenadj)
            except Exception as e:
                # Only fatal when there is no firmware path to fall back on.
                if not wmi:
                    raise
                decky.logger.warning(
                    f"[legotdp] ryzenadj unavailable ({e}); Extras range disabled")
            self._ready = True
            # Keep references - a bare create_task() may be garbage-collected mid-run.
            self._tasks = [
                asyncio.create_task(self._enforce_loop()),
                asyncio.create_task(self._info_loop()),
            ]
            decky.logger.info(
                f"[legotdp] ready (wmi={'yes' if wmi else 'no'}, "
                f"ryzenadj={'yes' if os.path.isfile(BIN_PATH) else 'no'})")
            s = _load_settings()
            if s.get("enabled", True):
                spl  = s.get("active_spl",  s.get("spl",  DEFAULT_SETTINGS["spl"]))
                sppt = s.get("active_sppt", s.get("sppt", DEFAULT_SETTINGS["sppt"]))
                fppt = s.get("active_fppt", s.get("fppt", DEFAULT_SETTINGS["fppt"]))
                await loop.run_in_executor(None, _apply_limits, spl, sppt, fppt)
        except Exception as e:
            self._setup_error = str(e)
            decky.logger.error(f"[legotdp] setup failed: {e}")

    async def _unload(self):
        for t in self._tasks:
            t.cancel()
        decky.logger.info("[legotdp] unloaded")
