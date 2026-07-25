import decky
import asyncio
import copy
import glob
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import threading
from settings import SettingsManager

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from updater import Updater  # noqa: E402 - needs the sys.path line above

BIN_DIR       = os.path.join(PLUGIN_DIR, "bin")
BIN_PATH      = os.path.join(BIN_DIR, "ryzenadj")
RYZENADJ_URL  = (
    "https://github.com/FlyGoat/RyzenAdj/releases/download/v0.19.0/"
    "ryzenadj-manylinux_2_28-x86_64.tar.gz"
)
GITHUB_RELEASES_URL = "https://api.github.com/repos/Rayekkk/LeGoTDP/releases/latest"

# Update checks, TLS trust store and downloads live in updater.py, which is kept
# identical in LeGo-Vibe-Control so a fix lands in both plugins. The host
# allowlist there matters more here than there: this plugin executes what it
# downloads, so an unrestricted URL would be a fetch-and-run primitive.
updater = Updater(
    releases_url=GITHUB_RELEASES_URL,
    user_agent="LeGoTDP",
    log_prefix="[legotdp]",
    plugin_dir=PLUGIN_DIR,
    logger=decky.logger,
)

# Matches the Balanced preset in src/index.tsx, so a fresh install and the
# preset the panel highlights agree with each other.
DEFAULT_SETTINGS = {"spl": 15000, "sppt": 18000, "fppt": 25000, "enabled": True}

# Reported by get_caps() when the firmware does not answer. The frontend carries
# the same numbers as FALLBACK_STD for the moments before get_caps() returns.
FALLBACK_STD_W = {"spl": 35, "sppt": 37, "fppt": 45}

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

_current_game_id: str = ""
_current_ac_online: bool = False
_panel_active: bool = False

# The frontend detects the running game via Steam's Router, which is authoritative;
# the /proc/*/environ scan misses games sandboxed by pressure-vessel/gamescope. When
# the panel is open the frontend pushes the appid here; we trust it while it is fresh
# and fall back to the proc scan once it goes stale (panel closed).
_frontend_appid: str = ""
_frontend_appid_ts: float = 0.0
_FRONTEND_APPID_TTL = 12.0


# ── AC power detection ─────────────────────────────────────────────────────────

def _read_sysfs(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _get_ac_online() -> bool:
    """True when an external charger is present.

    Only Mains-type supplies count. The Legion Go 2 also exposes USB-C PD source
    PSYs (ucsi-source-psy-*, type=USB, scope=Device) whose `online` flag tracks the
    port's PD role, not whether the device is being powered - ORing those in made an
    unplug flicker straight back to "charging". ACAD (Mains) is the real signal, and
    BAT0 status is unreliable here because battery conservation mode reports
    "Not charging" even while on AC.
    """
    mains_seen = False
    for path in glob.glob("/sys/class/power_supply/*"):
        if _read_sysfs(os.path.join(path, "type")) != "Mains":
            continue
        mains_seen = True
        if _read_sysfs(os.path.join(path, "online")) == "1":
            return True
    if mains_seen:
        return False
    # No Mains supply exposed at all - fall back to battery status.
    status = _read_sysfs("/sys/class/power_supply/BAT0/status")
    return status not in ("", "Discharging", "Unknown")


def _pick_profile_values(p: dict, ac_online: bool) -> tuple[int, int, int]:
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


# ── Persistence ────────────────────────────────────────────────────────────────

# Settings live in Decky's settings directory, not in the plugin directory. The
# plugin directory is wiped by every reinstall, and this plugin's own updater
# tells the user to uninstall before installing the new zip - which used to take
# the global settings and every per-game profile with it.
settings = SettingsManager(
    name="settings",
    settings_directory=decky.DECKY_PLUGIN_SETTINGS_DIR,
)

SETTINGS_KEY_SETTINGS      = "settings"
SETTINGS_KEY_GAME_PROFILES = "game_profiles"
SETTINGS_KEY_SCHEMA        = "schema_version"
CURRENT_SCHEMA             = 2

# Pre-schema-2 locations, inside the plugin directory. Read once by _migrate()
# and never written again.
LEGACY_SETTINGS_FILE = os.path.join(PLUGIN_DIR, "settings.json")
LEGACY_PROFILES_FILE = os.path.join(PLUGIN_DIR, "profiles.json")

# The enforce loop reads settings from an executor thread while RPC handlers
# write them from the event loop. Re-entrant because the write paths load first.
_settings_lock = threading.RLock()


async def _offload(fn, *args):
    """Run blocking work off the event loop.

    Settings I/O, sysfs reads and waiting on _settings_lock or _apply_lock all
    block. Decky gives each plugin its own process and loop, so blocking here
    does not stall other plugins - it stalls this one: every RPC the panel sends
    queues behind it, and neither the enforce loop nor the info loop ticks until
    it returns. _apply_lock alone can be held for a profile bounce plus three
    firmware writes.
    """
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _read_key(key: str, default: dict) -> dict:
    """A private copy of one key. Callers clamp and mutate what they get back,
    and getSetting hands out a live reference into the manager's own dict - so
    without the copy those edits would land in the store uncommitted, and a
    later read() would silently drop them again."""
    with _settings_lock:
        settings.read()
        value = settings.getSetting(key, None)
        return copy.deepcopy(value) if isinstance(value, dict) else dict(default)


def _write_key(key: str, value: dict) -> None:
    with _settings_lock:
        settings.setSetting(key, value)
        settings.commit()


def _clamp_triplet(spl, sppt, fppt) -> tuple[int, int, int]:
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
    s = _read_key(SETTINGS_KEY_SETTINGS, DEFAULT_SETTINGS)
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
    _write_key(SETTINGS_KEY_SETTINGS, s)


# ── Per-game profiles ──────────────────────────────────────────────────────────

def _load_profiles() -> dict:
    profiles = _read_key(SETTINGS_KEY_GAME_PROFILES, {})
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
    _write_key(SETTINGS_KEY_GAME_PROFILES, profiles)


def _save_active(s: dict, spl: int, sppt: int, fppt: int) -> None:
    s["active_spl"]  = spl
    s["active_sppt"] = sppt
    s["active_fppt"] = fppt
    _save_settings(s)


def _read_legacy(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _migrate() -> None:
    """Fold the pre-1.5.0 files in the plugin directory into Decky's store.

    Runs exactly once. The old files are left on disk untouched: they disappear
    with the next reinstall anyway, and leaving them means a downgrade still
    finds its settings.
    """
    with _settings_lock:
        settings.read()
        try:
            schema = int(settings.getSetting(SETTINGS_KEY_SCHEMA, 1))
        except (TypeError, ValueError):
            schema = 1
        if schema >= CURRENT_SCHEMA:
            return

        legacy_settings = _read_legacy(LEGACY_SETTINGS_FILE)
        legacy_profiles = _read_legacy(LEGACY_PROFILES_FILE)

        if legacy_settings and settings.getSetting(SETTINGS_KEY_SETTINGS, None) is None:
            settings.setSetting(SETTINGS_KEY_SETTINGS, legacy_settings)
            decky.logger.info(
                f"[legotdp] migrated {LEGACY_SETTINGS_FILE} into the Decky settings store")
        if legacy_profiles and settings.getSetting(SETTINGS_KEY_GAME_PROFILES, None) is None:
            settings.setSetting(SETTINGS_KEY_GAME_PROFILES, legacy_profiles)
            decky.logger.info(
                f"[legotdp] migrated {len(legacy_profiles)} per-game profile(s) "
                f"from {LEGACY_PROFILES_FILE}")

        settings.setSetting(SETTINGS_KEY_SCHEMA, CURRENT_SCHEMA)
        settings.commit()


# ── ryzenadj binary ────────────────────────────────────────────────────────────

def _download_ryzenadj() -> None:
    decky.logger.info(f"[legotdp] Downloading ryzenadj from {RYZENADJ_URL}")
    os.makedirs(BIN_DIR, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tar.gz")
    os.close(tmp_fd)
    try:
        with open(tmp_path, "wb") as out:
            updater.download_to(RYZENADJ_URL, out, timeout=30)
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

def _run_ryzenadj(args: list, timeout: float = 5.0) -> tuple[int, str, str]:
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


def _wmi_read(key: str, leaf: str) -> int | None:
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


def _profile_path() -> str | None:
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

# None = never probed, "" = probed and not found. A miss is retried: powercap can
# register after the plugin starts, and remembering the failure forever left the
# package draw reading blank until the plugin was reloaded.
_rapl_dir: str | None = None
_rapl_probed_at: float = 0.0
_RAPL_RESCAN_S = 60.0
_rapl_last: tuple = ()


def _find_rapl_package() -> str | None:
    global _rapl_dir, _rapl_probed_at
    if _rapl_dir:
        return _rapl_dir
    now = time.monotonic()
    if _rapl_dir is not None and now - _rapl_probed_at < _RAPL_RESCAN_S:
        return None

    _rapl_probed_at = now
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


def _rapl_watts() -> float | None:
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
                _invalidate_limits_cache()
                return result
            decky.logger.warning(
                f"[legotdp] WMI apply failed ({result['stderr']}), falling back to ryzenadj")
        result = _apply_ryzenadj(spl_mw, sppt_mw, fppt_mw)
        if result["success"]:
            _last_source = "ryzenadj"
            _invalidate_limits_cache()
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


# Reading limits over WMI is three sysfs reads. On the ryzenadj path it spawns a
# process, and the enforce loop asks every five seconds whether or not anyone has
# the panel open - so with Extras enabled that was a `ryzenadj --info` every five
# seconds forever, including mid-game. Serve a recent answer instead. The window
# is well inside the ryzenadj drift tolerance (6 W), which only exists to catch a
# post-resume reset, and _apply_limits drops the cache so a change we made is
# never hidden behind it.
_LIMITS_CACHE_TTL_S = 15.0
_limits_cache: dict = {}
_limits_cache_ts: float = 0.0
_limits_cache_lock = threading.Lock()


def _invalidate_limits_cache() -> None:
    global _limits_cache, _limits_cache_ts
    with _limits_cache_lock:
        _limits_cache, _limits_cache_ts = {}, 0.0


def _read_limits() -> dict:
    """Current limits in watts, read from whichever layer last applied them.

    The two layers do not observe each other: after a ryzenadj write the WMI
    attributes still report the firmware's own stale bookkeeping, so reading the
    wrong one would misreport the active limits.
    """
    global _limits_cache, _limits_cache_ts
    if _last_source == "wmi":
        vals = {f"{k}_limit": _wmi_read(k, "current_value") for k in WMI_ATTRS}
        if all(v is not None for v in vals.values()):
            return {k: float(v) for k, v in vals.items()}

    with _limits_cache_lock:
        if _limits_cache and time.monotonic() - _limits_cache_ts < _LIMITS_CACHE_TTL_S:
            return dict(_limits_cache)

    if not _ryzenadj_lock.acquire(timeout=4.0):
        return {}
    try:
        rc, out, _ = _run_ryzenadj(["--info"], timeout=3.0)
    finally:
        _ryzenadj_lock.release()

    parsed = _parse_ryzenadj_output(out) if rc == 0 else {}
    if parsed:
        with _limits_cache_lock:
            _limits_cache, _limits_cache_ts = dict(parsed), time.monotonic()
    return parsed


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
    """Current Steam game appid, or ''.

    Prefer the frontend's Router-based value while it is fresh - the /proc scan below
    misses games running inside pressure-vessel/gamescope. Falls back to the scan when
    the frontend has gone quiet (panel closed)."""
    if time.monotonic() - _frontend_appid_ts < _FRONTEND_APPID_TTL:
        return _frontend_appid
    return _scan_proc_for_appid()


def _scan_proc_for_appid() -> str:
    # The Steam "reaper" wrapper (reaper SteamLaunch AppId=NNNN -- ...) runs outside
    # the game's pressure-vessel/gamescope sandbox, so its cmdline is the most reliable
    # background signal. Fall back to SteamAppId in the environ.
    for path in glob.glob("/proc/*/cmdline"):
        try:
            with open(path, "rb") as f:
                for arg in f.read().split(b"\x00"):
                    if arg.startswith(b"AppId="):
                        appid = arg[len(b"AppId="):].decode(errors="replace")
                        if appid and appid != "0":
                            return appid
        except OSError:
            continue
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

def _global_triplet(s: dict) -> tuple[int, int, int]:
    return (s.get("spl",  DEFAULT_SETTINGS["spl"]),
            s.get("sppt", DEFAULT_SETTINGS["sppt"]),
            s.get("fppt", DEFAULT_SETTINGS["fppt"]))


def _apply_and_record(spl: int, sppt: int, fppt: int, why: str) -> None:
    """Apply a triplet and remember it as the target the enforce pass defends."""
    result = _apply_limits(spl, sppt, fppt)
    if result["success"]:
        # Re-read rather than reuse the caller's copy: an RPC handler may have
        # written the store since, and _save_active rewrites the whole object.
        _save_active(_load_settings(), spl, sppt, fppt)
        decky.logger.info(
            f"[legotdp] Applied {why}: {spl // 1000}/{sppt // 1000}/{fppt // 1000} W")
    else:
        decky.logger.warning(
            f"[legotdp] Failed to apply {why}: "
            f"rc={result['returncode']} err={result['stderr']}")


def _check_and_enforce() -> dict:
    """One enforce pass.

    Returns the events the caller should emit. This runs in an executor thread,
    which cannot await decky.emit itself, so the async loop above does the
    emitting - that is what lets the panel stop polling for the charger state.
    """
    global _current_game_id, _current_ac_online

    s = _load_settings()
    if not s.get("enabled", True):
        return {}

    appid    = _get_running_appid()
    ac_now   = _get_ac_online()
    ac_changed = ac_now != _current_ac_online
    _current_ac_online = ac_now
    events = {"power_source": {"ac": ac_now}} if ac_changed else {}

    game_changed = appid != _current_game_id

    if game_changed or ac_changed:
        prev = _current_game_id if game_changed else appid
        _current_game_id = appid

        profile = _load_profiles().get(appid) if appid else None
        if profile is not None:
            trigger = "AC state change" if ac_changed else "game launch"
            _apply_and_record(*_pick_profile_values(profile, ac_now),
                              f"game profile for app={appid} on {trigger} (ac={ac_now})")
            return events

        # Nothing per-game applies, so the global settings are what should be
        # running. Skipping this would leave the enforce pass below defending a
        # stale active_* triplet left over from whatever ran last.
        if appid:
            why = f"global TDP, app={appid} has no profile"
        elif prev:
            why = "global TDP, game exited"
        else:
            why = f"global TDP on AC change (ac={ac_now})"
        _apply_and_record(*_global_triplet(s), why)
        return events

    _enforce_target(_clamp_triplet(
        s.get("active_spl",  s.get("spl",  DEFAULT_SETTINGS["spl"])),
        s.get("active_sppt", s.get("sppt", DEFAULT_SETTINGS["sppt"])),
        s.get("active_fppt", s.get("fppt", DEFAULT_SETTINGS["fppt"])),
    ))
    return events


# WMI reads back the exact value we wrote, so a tight tolerance is right there. The
# ryzenadj path reports STAPM LIMIT for SPL, which the firmware manages dynamically
# (it drifts several watts below the set point under load), so comparing it tightly
# made the loop re-apply forever. A wide band there still catches a real reset - after
# resume the SMU drops to firmware defaults, which is a double-digit gap.
DRIFT_TOLERANCE_WMI_W      = 1.0
DRIFT_TOLERANCE_RYZENADJ_W = 6.0
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
    tolerance = DRIFT_TOLERANCE_WMI_W if _last_source == "wmi" else DRIFT_TOLERANCE_RYZENADJ_W
    # The WMI attributes keep reporting the last value even after the profile leaves
    # 'custom', so a matching read is not proof the limit is actually enforced - force
    # a re-apply (which re-selects custom) when we detect that.
    profile_lost = _wmi_profile_lost()
    if not profile_lost and all(abs(c - r) <= tolerance for c, r in zip(cur, reference)):
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


# ── Plugin class ───────────────────────────────────────────────────────────────

class Plugin:
    _ready: bool = False
    # Surfaced through is_ready() so a failed start shows up in the panel
    # instead of leaving the user with sliders that silently do nothing.
    _setup_error: str | None = None
    _tasks: list = []

    async def is_ready(self) -> dict:
        return {"ready": self._ready, "error": self._setup_error or ""}

    async def get_version(self) -> dict:
        return {"version": updater.plugin_version()}

    async def get_settings(self) -> dict:
        return await _offload(_load_settings)

    async def get_power_source(self) -> dict:
        return {"ac": await _offload(_get_ac_online)}

    async def get_extras_unlocked(self) -> bool:
        s = await _offload(_load_settings)
        return s.get("extras_unlocked", False)

    async def set_extras_unlocked(self, enabled: bool) -> None:
        def _do():
            s = _load_settings()
            s["extras_unlocked"] = enabled
            _save_settings(s)
        await _offload(_do)
        decky.logger.info(f"[legotdp] extras_unlocked={enabled}")

    async def get_game_profile(self, app_id: str) -> dict:
        def _do() -> dict:
            p = _load_profiles().get(app_id)
            if p is None:
                return {"exists": False, "profile": {}, "ac_separate": False, "ac_profile": {}}
            spl  = p.get("spl",  DEFAULT_SETTINGS["spl"])
            sppt = p.get("sppt", DEFAULT_SETTINGS["sppt"])
            fppt = p.get("fppt", DEFAULT_SETTINGS["fppt"])
            return {
                "exists":      True,
                "profile":     {"spl": spl, "sppt": sppt, "fppt": fppt,
                                "preset": p.get("preset", "")},
                "ac_separate": p.get("ac_separate", False),
                "ac_profile":  {"spl": p.get("ac_spl", spl), "sppt": p.get("ac_sppt", sppt),
                                "fppt": p.get("ac_fppt", fppt),
                                "ac_preset": p.get("ac_preset", "")},
            }
        return await _offload(_do)

    async def set_game_ac_profile(self, app_id: str, spl: int, sppt: int, fppt: int, ac_separate: bool, preset_name: str = "") -> dict:
        def _do() -> dict:
            # Clamp before storing, not just on read, so the file on disk never
            # holds a triplet the hardware would refuse.
            ac = _clamp_triplet(spl, sppt, fppt)
            profiles = _load_profiles()
            p = profiles.get(app_id, {})
            p.update({"ac_separate": ac_separate,
                      "ac_spl": ac[0], "ac_sppt": ac[1], "ac_fppt": ac[2]})
            if preset_name:
                p["ac_preset"] = preset_name
            profiles[app_id] = p
            _save_profiles(profiles)
            decky.logger.info(
                f"[legotdp] Saved AC profile: app={app_id} separate={ac_separate}")

            if not _get_ac_online():
                return {"success": True, "stderr": "", "stdout": "", "returncode": 0}
            if ac_separate:
                want = ac
            elif all(p.get(k) is not None for k in ("spl", "sppt", "fppt")):
                want = (p["spl"], p["sppt"], p["fppt"])
            else:
                return {"success": True, "stderr": "", "stdout": "", "returncode": 0}

            result = _apply_limits(*want)
            if result["success"]:
                _save_active(_load_settings(), *want)
            return result
        return await _offload(_do)

    async def delete_game_profile(self, app_id: str) -> None:
        def _do() -> None:
            profiles = _load_profiles()
            profiles.pop(app_id, None)
            _save_profiles(profiles)
        await _offload(_do)
        decky.logger.info(f"[legotdp] Deleted game profile: app={app_id}")

    async def set_plugin_enabled(self, enabled: bool) -> None:
        def _do() -> None:
            s = _load_settings()
            s["enabled"] = enabled
            _save_settings(s)
        await _offload(_do)
        decky.logger.info(f"[legotdp] Plugin enabled={enabled}")

    async def get_caps(self) -> dict:
        """Slider ceilings in watts. `std` is what the firmware accepts over WMI;
        `max` is the Extras range, which falls through to ryzenadj."""
        def _do() -> dict:
            caps = _wmi_caps()
            return {
                "min": min(caps[k]["min"] for k in WMI_ATTRS) if caps else HARD_MIN_MW // 1000,
                "std": {k: caps[k]["max"] for k in WMI_ATTRS} if caps else dict(FALLBACK_STD_W),
                "max": {k: HARD_MAX_MW // 1000 for k in WMI_ATTRS},
                "wmi": bool(caps),
            }
        return await _offload(_do)

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

    async def reapply(self) -> dict:
        """Force the saved limits back onto the hardware.

        Called by the frontend on resume from suspend, where the SMU comes back
        at firmware defaults. Decky has no backend resume hook - the loader only
        ever invokes _migration, _main, _unload and _uninstall - so Steam's own
        notification is the only signal there is, and without it the enforce
        loop takes up to five seconds to notice.
        """
        def _do() -> dict:
            s = _load_settings()
            if not s.get("enabled", True):
                return {"success": True, "skipped": True}
            # Whatever was cached describes the pre-suspend hardware.
            _invalidate_limits_cache()
            result = _apply_limits(*_clamp_triplet(
                s.get("active_spl",  s.get("spl",  DEFAULT_SETTINGS["spl"])),
                s.get("active_sppt", s.get("sppt", DEFAULT_SETTINGS["sppt"])),
                s.get("active_fppt", s.get("fppt", DEFAULT_SETTINGS["fppt"])),
            ))
            decky.logger.info(
                f"[legotdp] reapply after resume: success={result['success']}")
            return {"success": result["success"], "skipped": False}
        return await _offload(_do)

    async def set_active_app(self, app_id: str) -> None:
        """Frontend reports the authoritative running-game appid (or '' for none)."""
        global _frontend_appid, _frontend_appid_ts
        _frontend_appid = app_id or ""
        _frontend_appid_ts = time.monotonic()

    async def get_tdp_info(self) -> dict:
        if not self._ready:
            return {"success": False, "values": {}, "error": "not ready"}
        with _info_cache_lock:
            return {"success": True, "values": dict(_info_cache)}

    async def apply_tdp(self, spl: int, sppt: int, fppt: int, app_id: str = "", preset_name: str = "") -> dict:
        if not self._ready:
            return {"success": False, "stderr": "not ready", "stdout": "", "returncode": -1}

        def _do() -> dict:
            profiles: dict = {}
            existing: dict = {}
            want = (spl, sppt, fppt)

            if app_id:
                profiles = _load_profiles()
                existing = profiles.get(app_id, {})
                # On AC with a separate AC profile, the sliders describe the
                # battery values but the hardware should run the AC ones.
                if (_get_ac_online() and existing.get("ac_separate")
                        and existing.get("ac_spl") is not None):
                    want = (
                        existing["ac_spl"],
                        existing.get("ac_sppt", existing.get("sppt", DEFAULT_SETTINGS["sppt"])),
                        existing.get("ac_fppt", existing.get("fppt", DEFAULT_SETTINGS["fppt"])),
                    )

            result = _apply_limits(*want)
            if not result["success"]:
                return result

            s = _load_settings()
            if app_id:
                existing.update({"spl": spl, "sppt": sppt, "fppt": fppt})
                if preset_name:
                    existing["preset"] = preset_name
                profiles[app_id] = existing
                _save_profiles(profiles)
                decky.logger.info(f"[legotdp] Saved game profile: app={app_id}")
            else:
                s["spl"], s["sppt"], s["fppt"] = spl, sppt, fppt
                s["active_preset"] = preset_name
            _save_active(s, *want)
            return result
        return await _offload(_do)

    # ---- Updates ----------------------------------------------------- #

    async def check_for_updates(self) -> dict:
        return await _offload(updater.check)

    async def perform_update(self, download_url: str, asset_name: str) -> dict:
        return await _offload(updater.download, download_url, asset_name)

    async def _info_loop(self):
        while True:
            await asyncio.sleep(2)
            if not _panel_active:
                continue
            try:
                await _offload(_refresh_info_cache)
                with _info_cache_lock:
                    values = dict(_info_cache)
                # Pushed, not polled. The panel used to ask for this over RPC every
                # two seconds - a round trip per tick to fetch numbers the backend
                # had just refreshed on this very schedule.
                await decky.emit("tdp_info", {"success": True, "values": values})
            except Exception as e:
                decky.logger.warning(f"[legotdp] info loop error: {e}")

    async def _enforce_loop(self):
        while True:
            await asyncio.sleep(5)
            try:
                for event, payload in (await _offload(_check_and_enforce)).items():
                    await decky.emit(event, payload)
            except Exception as e:
                decky.logger.warning(f"[legotdp] enforce iteration failed: {e}")

    async def _migration(self):
        """Fold the pre-1.5.0 files into Decky's store, before anything reads it.

        This is the loader's own hook for the job: it runs to completion before
        _main() is even scheduled, so no settings read can race the migration.

        decky.migrate_settings() deliberately goes unused. It relocates a file
        under its own basename and rm -rf's the source, so the legacy
        PLUGIN_DIR/settings.json would land straight on top of the
        SettingsManager store - identical filename - and replace the whole
        keyed object with a flat pre-1.5.0 dict. That helper moves files; this
        migration has to reshape them.
        """
        await _offload(_migrate)

    async def _main(self):
        decky.logger.info(f"[legotdp] startup  v{updater.plugin_version()}")
        try:
            global _current_ac_online
            # Resolve the trust store now so the log states up front whether downloads
            # and update checks will be able to verify certificates.
            await _offload(updater.ssl_context)
            # Seed this so the first enforce pass does not report a phantom AC change.
            _current_ac_online = await _offload(_get_ac_online)
            wmi = await _offload(_wmi_caps)
            try:
                await _offload(_ensure_ryzenadj)
            except Exception as e:
                # Only fatal when there is no firmware path to fall back on.
                if not wmi:
                    raise
                decky.logger.warning(
                    f"[legotdp] ryzenadj unavailable ({e}); Extras range disabled")
            Plugin._ready = True
            # Keep references - a bare create_task() may be garbage-collected mid-run.
            self._tasks = [
                asyncio.create_task(self._enforce_loop()),
                asyncio.create_task(self._info_loop()),
            ]
            decky.logger.info(
                f"[legotdp] ready (wmi={'yes' if wmi else 'no'}, "
                f"ryzenadj={'yes' if os.path.isfile(BIN_PATH) else 'no'})")

            def _apply_saved() -> None:
                s = _load_settings()
                if s.get("enabled", True):
                    _apply_limits(
                        s.get("active_spl",  s.get("spl",  DEFAULT_SETTINGS["spl"])),
                        s.get("active_sppt", s.get("sppt", DEFAULT_SETTINGS["sppt"])),
                        s.get("active_fppt", s.get("fppt", DEFAULT_SETTINGS["fppt"])),
                    )
            await _offload(_apply_saved)
        except Exception as e:
            Plugin._setup_error = str(e)
            decky.logger.error(f"[legotdp] setup failed: {e}")

    async def _unload(self):
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks = []
        decky.logger.info("[legotdp] unloaded")

    async def _uninstall(self):
        """Hand the hardware back before the plugin directory disappears.

        The firmware keeps whatever ppt_* triplet was last latched into the
        'custom' profile, so without this an uninstall leaves the machine pinned
        to the plugin's final TDP with nothing left installed to change it.

        Nothing is cleaned off disk here: the loader removes the whole plugin
        directory straight afterwards, and DECKY_PLUGIN_SETTINGS_DIR - where the
        settings and per-game profiles live - is deliberately left alone, so a
        reinstall still finds them.
        """
        def _do() -> None:
            # _unload() cancelled the enforce loop, but cancelling a task parked
            # in run_in_executor does not stop the worker thread, so a pass may
            # still be in flight holding this lock. Take it, or that pass could
            # re-assert the limits after we have handed the profile back.
            if not _apply_lock.acquire(timeout=8.0):
                decky.logger.warning(
                    "[legotdp] uninstall: apply busy, leaving the profile as it is")
                return
            try:
                path = _profile_path()
                if path and _write_profile(path, "balanced"):
                    decky.logger.info("[legotdp] uninstall: platform profile -> balanced")
            finally:
                _apply_lock.release()
        await _offload(_do)
        decky.logger.info("[legotdp] uninstalled")
