# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/LeGoTDP

"""Import the plugin backend with stubbed DeckyLoader modules.

`main.py` imports `decky` and `settings`, which only exist inside the loader.
Stubbing both lets the backend run in an ordinary Python process, so the same
suite works on a build machine and on the Legion itself.

Set `LEGOTDP_PLUGIN_DIR` to test a deployed copy instead of the repo, e.g.
`/home/deck/homebrew/plugins/LeGoTDP`.
"""
import json
import os
import sys
import tempfile
import types

PLUGIN_DIR = os.environ.get("LEGOTDP_PLUGIN_DIR") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

_settings_dir = tempfile.mkdtemp(prefix="legotdp-tests-")


class StubLogger:
    """Records instead of printing, so tests can assert on what was logged."""

    def __init__(self):
        self.records = []

    def _log(self, level, message):
        self.records.append((level, str(message)))

    def info(self, message):
        self._log("info", message)

    def warning(self, message):
        self._log("warning", message)

    def error(self, message):
        self._log("error", message)

    def debug(self, message):
        self._log("debug", message)


#: Every (event, args) the backend pushed to the frontend, in order. Reset with
#: `emitted.clear()`; the loader replaces decky.emit with a socket write, so this
#: is the only place the payloads can be inspected.
emitted: list = []


async def _emit(event, *args):
    emitted.append((event, args))


_decky = types.ModuleType("decky")
_decky.logger = StubLogger()
_decky.DECKY_PLUGIN_SETTINGS_DIR = _settings_dir
_decky.emit = _emit
sys.modules["decky"] = _decky


class SettingsManager:
    """Same surface as Decky's, backed by a throwaway JSON file."""

    def __init__(self, name, settings_directory):
        self.path = os.path.join(settings_directory, f"{name}.json")
        self.data = {}

    def read(self):
        try:
            with open(self.path) as handle:
                self.data = json.load(handle)
        except (OSError, ValueError):
            self.data = {}

    def getSetting(self, key, default=None):
        return self.data.get(key, default)

    def setSetting(self, key, value):
        self.data[key] = value

    def commit(self):
        with open(self.path, "w") as handle:
            json.dump(self.data, handle, indent=2)


_settings_mod = types.ModuleType("settings")
_settings_mod.SettingsManager = SettingsManager
sys.modules["settings"] = _settings_mod

# main.py and lego_updater.py target Linux and import pwd at module scope. Stubbing
# it when absent lets the hardware-independent tests run on a Windows dev box
# too; nothing in test_logic.py touches it, and test_device.py skips itself.
try:
    import pwd  # noqa: F401
except ImportError:
    _stub = types.ModuleType("pwd")
    # Referenced in an annotation that Python evaluates at def time.
    _stub.struct_passwd = object
    sys.modules["pwd"] = _stub

sys.path.insert(0, PLUGIN_DIR)
import main  # noqa: E402
import lego_updater as updater  # noqa: E402

# DMI is an input like any other, so it is stubbed like any other: a suite that
# read the real one would test the machine it happens to be running on rather
# than the logic, and the same file would pass in CI and fail on a Legion Go S.
# Tests that care about a particular device replace _dmi and reset the cache
# themselves - see FirmwareOnlyHardware in test_logic.py.
main._dmi = lambda field: ""
main._wmi_only_cache = None


def seed(blob: dict) -> None:
    """Replace the settings file and drop every piece of cached state."""
    with open(main.settings.path, "w") as handle:
        json.dump(blob, handle, indent=2)
    main.settings.read()
    main._last_source = ""
    main._drift_target = ()
    main._drift_settled = ()
    main._drift_attempts = 0


def has_wmi() -> bool:
    """True when the Lenovo firmware attributes are present."""
    try:
        return bool(main._wmi_caps())
    except Exception:
        return False


# Milliwatt triplets chosen so a wrong pick is unambiguous rather than a near
# miss: the game profile is far above the global one on both AC and battery.
GAME_WITH_PROFILE = "292030"
GAME_WITHOUT_PROFILE = "2483190"

FIXTURE = {
    "schema_version": main.CURRENT_SCHEMA,
    "settings": {
        "spl": 15000, "sppt": 18000, "fppt": 25000,
        "enabled": True, "active_preset": "balanced",
    },
    "game_profiles": {
        GAME_WITH_PROFILE: {
            "spl": 25000, "sppt": 28000, "fppt": 35000, "preset": "performance",
            "ac_separate": True,
            "ac_spl": 35000, "ac_sppt": 37000, "ac_fppt": 45000, "ac_preset": "max",
        },
    },
}
