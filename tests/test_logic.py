"""Backend tests that need no Legion Go attached. These run in CI."""
import asyncio
import glob
import json
import os
import ssl
import tempfile
import unittest

from _harness import (
    FIXTURE,
    GAME_WITHOUT_PROFILE,
    GAME_WITH_PROFILE,
    emitted,
    main,
    seed,
    updater,
)


def _explode() -> None:
    """Stand-in for a store that cannot be read or committed."""
    raise OSError("disk on fire")


class ClampTriplet(unittest.TestCase):
    """A hand-edited or truncated settings file must never reach the hardware."""

    def test_ordering_is_enforced(self):
        # SPPT and FPPT are offsets above SPL in the UI, so neither can sit below it.
        self.assertEqual(main._clamp_triplet(20000, 10000, 10000), (20000, 20000, 20000))

    def test_sppt_is_pulled_down_to_fppt(self):
        self.assertEqual(main._clamp_triplet(10000, 30000, 20000), (10000, 20000, 20000))

    def test_hard_limits(self):
        lo, hi = main.HARD_MIN_MW, main.HARD_MAX_MW
        self.assertEqual(main._clamp_triplet(1, 1, 1), (lo, lo, lo))
        self.assertEqual(main._clamp_triplet(99000, 99000, 99000), (hi, hi, hi))

    def test_junk_falls_back_to_defaults(self):
        defaults = (main.DEFAULT_SETTINGS["spl"],
                    main.DEFAULT_SETTINGS["sppt"],
                    main.DEFAULT_SETTINGS["fppt"])
        self.assertEqual(main._clamp_triplet("nonsense", None, {}), defaults)

    def test_numeric_strings_are_accepted(self):
        self.assertEqual(main._clamp_triplet("15000", "18000", "25000"),
                         (15000, 18000, 25000))


class ProfileSelection(unittest.TestCase):
    def setUp(self):
        seed(FIXTURE)
        self.profile = main._load_profiles()[GAME_WITH_PROFILE]

    def test_battery_values_on_battery(self):
        self.assertEqual(main._pick_profile_values(self.profile, ac_online=False),
                         (25000, 28000, 35000))

    def test_ac_values_when_charging(self):
        self.assertEqual(main._pick_profile_values(self.profile, ac_online=True),
                         (35000, 37000, 45000))

    def test_ac_falls_back_when_not_separate(self):
        p = dict(self.profile, ac_separate=False)
        self.assertEqual(main._pick_profile_values(p, ac_online=True),
                         (25000, 28000, 35000))

    def test_missing_fields_fall_back_to_defaults(self):
        self.assertEqual(
            main._pick_profile_values({}, ac_online=False),
            (main.DEFAULT_SETTINGS["spl"],
             main.DEFAULT_SETTINGS["sppt"],
             main.DEFAULT_SETTINGS["fppt"]),
        )


class Persistence(unittest.TestCase):
    """Settings live in Decky's settings directory, not the plugin directory."""

    def setUp(self):
        seed(FIXTURE)

    def test_settings_round_trip(self):
        s = main._load_settings()
        s["spl"] = 20000
        main._save_settings(s)
        self.assertEqual(main._load_settings()["spl"], 20000)

    def test_values_are_clamped_on_load(self):
        seed({"schema_version": main.CURRENT_SCHEMA,
              "settings": {"spl": 99000, "sppt": 1, "fppt": 1}})
        s = main._load_settings()
        self.assertEqual((s["spl"], s["sppt"], s["fppt"]),
                         (main.HARD_MAX_MW, main.HARD_MAX_MW, main.HARD_MAX_MW))

    def test_profiles_are_clamped_on_load(self):
        seed({"schema_version": main.CURRENT_SCHEMA,
              "game_profiles": {"1": {"spl": 99000, "sppt": 99000, "fppt": 99000}}})
        p = main._load_profiles()["1"]
        self.assertEqual(p["spl"], main.HARD_MAX_MW)

    def test_a_missing_store_yields_defaults(self):
        seed({})
        s = main._load_settings()
        self.assertEqual(s["spl"], main.DEFAULT_SETTINGS["spl"])
        self.assertEqual(main._load_profiles(), {})

    def test_an_unsaved_edit_never_reaches_the_disk(self):
        # getSetting returns a live reference into the manager's own dict, and
        # every caller here clamps and mutates what it gets back. Without a
        # private copy an unrelated commit - saving a game profile, say -
        # flushes those uncommitted edits to disk along with it.
        s = main._load_settings()
        s["spl"] = 31000
        main._save_profiles({"1": {"spl": 8000, "sppt": 8000, "fppt": 8000}})
        with open(main.settings.path) as handle:
            self.assertEqual(json.load(handle)["settings"]["spl"], 15000)

    def test_an_unsaved_profile_edit_never_reaches_the_disk(self):
        profiles = main._load_profiles()
        profiles[GAME_WITH_PROFILE]["spl"] = 9000
        # Any commit will do - it writes the manager's whole dict, not just the
        # key being saved.
        main.settings.setSetting("unrelated_key", True)
        main.settings.commit()
        with open(main.settings.path) as handle:
            stored = json.load(handle)["game_profiles"][GAME_WITH_PROFILE]
        self.assertEqual(stored["spl"], 25000)

    def test_save_active_does_not_disturb_the_saved_target(self):
        s = main._load_settings()
        main._save_active(s, 30000, 31000, 32000)
        reloaded = main._load_settings()
        self.assertEqual(reloaded["active_spl"], 30000)
        # The user's chosen global TDP is a separate field from what is currently
        # applied - a per-game profile must not overwrite it.
        self.assertEqual(reloaded["spl"], 15000)


class Migration(unittest.TestCase):
    """The pre-1.5.0 files lived in the plugin directory, which a reinstall wipes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (main.LEGACY_SETTINGS_FILE, main.LEGACY_PROFILES_FILE)
        main.LEGACY_SETTINGS_FILE = os.path.join(self._tmp.name, "settings.json")
        main.LEGACY_PROFILES_FILE = os.path.join(self._tmp.name, "profiles.json")

    def tearDown(self):
        main.LEGACY_SETTINGS_FILE, main.LEGACY_PROFILES_FILE = self._orig
        self._tmp.cleanup()

    def _write_legacy(self, settings=None, profiles=None):
        if settings is not None:
            with open(main.LEGACY_SETTINGS_FILE, "w") as handle:
                json.dump(settings, handle)
        if profiles is not None:
            with open(main.LEGACY_PROFILES_FILE, "w") as handle:
                json.dump(profiles, handle)

    def test_legacy_files_are_adopted(self):
        seed({})
        self._write_legacy(
            settings={"spl": 8000, "sppt": 10000, "fppt": 15000, "enabled": True},
            profiles={GAME_WITH_PROFILE: {"spl": 25000, "sppt": 28000, "fppt": 35000}},
        )
        main._migrate()
        self.assertEqual(main._load_settings()["spl"], 8000)
        self.assertIn(GAME_WITH_PROFILE, main._load_profiles())

    def test_the_originals_are_left_on_disk(self):
        # A downgrade has to still find its settings, and the files disappear with
        # the next reinstall anyway.
        seed({})
        self._write_legacy(settings={"spl": 8000})
        main._migrate()
        self.assertTrue(os.path.exists(main.LEGACY_SETTINGS_FILE))

    def test_migration_is_idempotent(self):
        seed({})
        self._write_legacy(settings={"spl": 8000, "sppt": 8000, "fppt": 8000})
        main._migrate()
        s = main._load_settings()
        s["spl"] = 30000
        main._save_settings(s)
        main._migrate()
        self.assertEqual(main._load_settings()["spl"], 30000)

    def test_an_existing_store_is_never_overwritten(self):
        seed(FIXTURE)
        # Schema is already current, so this should not even look at the files.
        self._write_legacy(settings={"spl": 5000, "sppt": 5000, "fppt": 5000})
        main._migrate()
        self.assertEqual(main._load_settings()["spl"], 15000)

    def test_missing_legacy_files_are_not_an_error(self):
        seed({})
        main._migrate()
        self.assertEqual(main._load_settings()["spl"], main.DEFAULT_SETTINGS["spl"])
        self.assertGreaterEqual(
            int(main.settings.getSetting(main.SETTINGS_KEY_SCHEMA, 1)),
            main.CURRENT_SCHEMA,
        )

    def test_the_lifecycle_hook_runs_the_migration(self):
        # Decky runs _migration() to completion before it even schedules _main(),
        # which is the guarantee we want: no settings read can outrun it.
        seed({})
        self._write_legacy(settings={"spl": 8000, "sppt": 10000, "fppt": 15000})
        asyncio.run(main.Plugin()._migration())
        self.assertEqual(main._load_settings()["spl"], 8000)

    def test_a_failed_migration_is_reported_not_raised(self):
        # The loader wraps start-up in a bare except that logs and exits, and it
        # never reaches setup_server() - so a raise here would strand the panel
        # retrying an is_ready() with nobody left to answer it.
        original, main._migrate = main._migrate, _explode
        try:
            asyncio.run(main.Plugin()._migration())
        finally:
            main._migrate = original
            self.addCleanup(setattr, main.Plugin, "_setup_error", None)
        self.assertIn("disk on fire", main.Plugin._setup_error or "")

    def test_the_legacy_file_is_not_handed_to_decky_migrate_settings(self):
        # decky.migrate_settings() would tar the legacy file into
        # DECKY_PLUGIN_SETTINGS_DIR under its own basename and rm -rf the source.
        # Both files are called settings.json, so that would drop a flat
        # pre-1.5.0 dict straight on top of the SettingsManager store.
        self.assertEqual(
            os.path.basename(main.LEGACY_SETTINGS_FILE),
            os.path.basename(main.settings.path),
        )


class EnforceEvents(unittest.TestCase):
    """_check_and_enforce runs in an executor thread and so cannot await
    decky.emit itself; it hands the events back for the async loop to push.
    The panel's charger label depends on those arriving."""

    def setUp(self):
        seed(FIXTURE)
        self._restore = {name: getattr(main, name) for name in (
            "_get_ac_online", "_get_running_appid", "_apply_limits", "_enforce_target")}
        self.applied = []
        main._get_running_appid = lambda: ""
        main._apply_limits = lambda *a: self.applied.append(a) or {
            "success": True, "stdout": "", "stderr": "", "returncode": 0}
        main._enforce_target = lambda want: None
        main._current_game_id = ""
        main._current_ac_online = False

    def tearDown(self):
        for name, original in self._restore.items():
            setattr(main, name, original)

    def _pass(self, ac: bool) -> dict:
        main._get_ac_online = lambda: ac
        return main._check_and_enforce()

    def test_a_charger_change_is_announced(self):
        self.assertEqual(self._pass(True), {"power_source": {"ac": True}})

    def test_a_steady_charger_says_nothing(self):
        self._pass(True)
        # Emitting every five seconds regardless would wake the panel for nothing.
        self.assertEqual(self._pass(True), {})

    def test_unplugging_is_announced_too(self):
        self._pass(True)
        self.assertEqual(self._pass(False), {"power_source": {"ac": False}})

    def test_a_disabled_plugin_emits_nothing(self):
        settings = main._load_settings()
        settings["enabled"] = False
        main._save_settings(settings)
        self.assertEqual(self._pass(True), {})
        self.assertEqual(self.applied, [])


class PanelLease(unittest.IsolatedAsyncioTestCase):
    """set_panel_active is a lease, not a latch. The panel drops it in its
    effect cleanup, but that never runs if the frontend is torn down outright -
    a Steam UI restart - and the info loop would then refresh forever."""

    def setUp(self):
        seed(FIXTURE)
        emitted.clear()
        self.addCleanup(setattr, main, "_panel_active", False)
        self.addCleanup(setattr, main, "_panel_active_ts", 0.0)

    @staticmethod
    def _age(seconds: float) -> None:
        """Backdate the lease. Cheaper and less invasive than moving the clock:
        patching time.monotonic patches it for asyncio too, which then reports
        every await as a stalled callback."""
        main._panel_active_ts -= seconds

    async def test_a_fresh_lease_is_active(self):
        await main.Plugin().set_panel_active(True)
        self.assertTrue(main._panel_is_active())

    async def test_the_lease_expires_when_nobody_renews_it(self):
        await main.Plugin().set_panel_active(True)
        self._age(main._PANEL_ACTIVE_TTL_S + 1)
        self.assertFalse(main._panel_is_active())

    async def test_renewing_it_keeps_the_panel_alive(self):
        plugin = main.Plugin()
        await plugin.set_panel_active(True)
        # The frontend renews every 30 s against a 90 s window, so two renewals
        # can be lost in a row without the panel going dark.
        for _ in range(4):
            self._age(main._PANEL_ACTIVE_TTL_S / 3)
            await plugin.set_panel_active(True)
            self.assertTrue(main._panel_is_active())

    async def test_closing_the_panel_drops_it_immediately(self):
        plugin = main.Plugin()
        await plugin.set_panel_active(True)
        await plugin.set_panel_active(False)
        self.assertFalse(main._panel_is_active())

    async def test_nothing_is_pushed_once_the_lease_lapses(self):
        plugin = main.Plugin()
        await plugin.set_panel_active(True)
        self.assertTrue(await plugin._push_info())
        self.assertEqual([name for name, _ in emitted], ["tdp_info"])

        emitted.clear()
        self._age(main._PANEL_ACTIVE_TTL_S + 1)
        self.assertFalse(await plugin._push_info())
        self.assertEqual(emitted, [])


class RyzenadjOutput(unittest.TestCase):
    SAMPLE = """
| Name                    |   Value   |          Parameter Description          |
| STAPM LIMIT             |   15.000  | stapm limit                             |
| STAPM VALUE             |   12.500  | stapm value                             |
| PPT LIMIT FAST          |   25.000  | fast limit                              |
| PPT VALUE FAST          |   20.125  | fast value                              |
| PPT LIMIT SLOW          |   18.000  | slow limit                              |
| PPT VALUE SLOW          |   16.000  | slow value                              |
"""

    def test_limits_and_values_are_extracted(self):
        parsed = main._parse_ryzenadj_output(self.SAMPLE)
        self.assertEqual(parsed["spl_limit"], 15.0)
        self.assertEqual(parsed["spl_value"], 12.5)
        self.assertEqual(parsed["fppt_limit"], 25.0)
        self.assertEqual(parsed["sppt_limit"], 18.0)

    def test_junk_yields_nothing_rather_than_raising(self):
        self.assertEqual(main._parse_ryzenadj_output("no table here"), {})
        self.assertEqual(main._parse_ryzenadj_output(""), {})


class LimitsCache(unittest.TestCase):
    """The ryzenadj read spawns a process and the enforce loop asks every 5 s."""

    INFO = ("| STAPM LIMIT    | 15.000 | stapm limit |\n"
            "| PPT LIMIT SLOW | 18.000 | slow limit  |\n"
            "| PPT LIMIT FAST | 25.000 | fast limit  |\n")

    def setUp(self):
        seed(FIXTURE)
        main._last_source = "ryzenadj"      # force the expensive path
        main._invalidate_limits_cache()
        self._real_run = main._run_ryzenadj
        self._real_caps = main._wmi_caps
        self.info_calls = 0

        def fake(args, timeout=5.0):
            if "--info" in args:
                self.info_calls += 1
                return 0, self.INFO, ""
            return 0, "", ""                # an apply
        main._run_ryzenadj = fake
        # Pretend the firmware is absent, so _apply_limits stays on the faked
        # ryzenadj path. On a real Legion it would otherwise take the WMI path,
        # reset _last_source to "wmi" and - the actual problem - write live
        # limits to the firmware from a suite that promises to touch nothing.
        main._wmi_caps = lambda: {}

    def tearDown(self):
        main._run_ryzenadj = self._real_run
        main._wmi_caps = self._real_caps
        main._last_source = ""
        main._invalidate_limits_cache()

    def test_the_first_read_spawns_and_parses(self):
        self.assertEqual(main._read_limits()["spl_limit"], 15.0)
        self.assertEqual(self.info_calls, 1)

    def test_a_second_read_is_served_from_the_cache(self):
        main._read_limits()
        self.assertEqual(main._read_limits()["spl_limit"], 15.0)
        self.assertEqual(self.info_calls, 1)

    def test_an_apply_drops_the_cache(self):
        # Otherwise the panel would keep showing the old limits for 15 seconds
        # after the user changed them.
        main._read_limits()
        self.assertTrue(main._apply_limits(15000, 18000, 25000)["success"])
        main._read_limits()
        self.assertEqual(self.info_calls, 2)

    def test_a_failed_read_is_not_cached(self):
        main._run_ryzenadj = lambda args, timeout=5.0: (-1, "", "boom")
        self.assertEqual(main._read_limits(), {})
        self.assertEqual(main._read_limits(), {})


class UnreadableSpl(unittest.TestCase):
    """STAPM LIMIT follows the fast limit rather than the value passed to
    --stapm-limit, so the panel's SPL row was really showing FPPT and the
    enforce loop chased a number the hardware would never return."""

    def setUp(self):
        self._applied = main._applied_mw
        self.addCleanup(setattr, main, "_applied_mw", self._applied)

    def test_a_settled_stapm_is_replaced_by_what_we_applied(self):
        main._applied_mw = (25000, 33000, 47000)
        parsed = main._adopt_unreadable_spl(
            {"spl_limit": 47.0, "sppt_limit": 33.0, "fppt_limit": 47.0})
        self.assertEqual(parsed["spl_limit"], 25.0)
        # The two the hardware does honour must pass through untouched.
        self.assertEqual((parsed["sppt_limit"], parsed["fppt_limit"]), (33.0, 47.0))

    def test_a_wobbling_stapm_is_replaced_too(self):
        # The SMU moves it by a few hundred milliwatts, so an exact match
        # against fppt is not a usable trigger - 46.643 against a 47 fast limit
        # is a real reading from the device.
        main._applied_mw = (40000, 45000, 47000)
        parsed = main._adopt_unreadable_spl(
            {"spl_limit": 46.643, "sppt_limit": 45.0, "fppt_limit": 47.0})
        self.assertEqual(parsed["spl_limit"], 40.0)

    def test_a_reading_taken_mid_transit_is_replaced_too(self):
        # Sampled a second after a change it sits between the old value and the
        # new one; 49.746 against a 47 fast limit is also a real reading.
        main._applied_mw = (40000, 45000, 47000)
        parsed = main._adopt_unreadable_spl(
            {"spl_limit": 49.746, "sppt_limit": 45.0, "fppt_limit": 47.0})
        self.assertEqual(parsed["spl_limit"], 40.0)

    def test_nothing_is_invented_before_the_first_apply(self):
        main._applied_mw = ()
        parsed = main._adopt_unreadable_spl(
            {"spl_limit": 47.0, "sppt_limit": 33.0, "fppt_limit": 47.0})
        self.assertEqual(parsed["spl_limit"], 47.0)

    def test_a_partial_read_is_not_touched(self):
        main._applied_mw = (25000, 33000, 47000)
        self.assertEqual(main._adopt_unreadable_spl({"sppt_limit": 33.0}),
                         {"sppt_limit": 33.0})

    def test_the_drift_check_stops_chasing_the_unreadable_row(self):
        # The whole point: the SPL comparison could never succeed, so every
        # target change burned DRIFT_MAX_ATTEMPTS re-applies before standing
        # down - visible in the journal as three applies per slider move.
        main._applied_mw = (25000, 33000, 47000)
        parsed = main._adopt_unreadable_spl(
            {"spl_limit": 47.0, "sppt_limit": 33.0, "fppt_limit": 47.0})
        cur = tuple(parsed[f"{k}_limit"] for k in ("spl", "sppt", "fppt"))
        want_w = tuple(v / 1000 for v in main._applied_mw)
        self.assertTrue(all(abs(c - w) <= main.DRIFT_TOLERANCE_RYZENADJ_W
                            for c, w in zip(cur, want_w)))

    def test_a_real_drift_is_still_caught_through_the_other_two(self):
        # A post-resume reset moves fast and slow, which are exact, so
        # substituting SPL does not blind the enforce loop.
        main._applied_mw = (40000, 45000, 47000)
        parsed = main._adopt_unreadable_spl(
            {"spl_limit": 35.0, "sppt_limit": 15.0, "fppt_limit": 25.0})
        cur = tuple(parsed[f"{k}_limit"] for k in ("spl", "sppt", "fppt"))
        want_w = tuple(v / 1000 for v in main._applied_mw)
        self.assertFalse(all(abs(c - w) <= main.DRIFT_TOLERANCE_RYZENADJ_W
                             for c, w in zip(cur, want_w)))


class WmiCrossCheck(unittest.TestCase):
    """The firmware attributes only record what was written through them, so an
    override that bypasses that interface is invisible there. Measured on the
    device: an external drop to 15 W left them reporting 25/30/35 and the
    enforce pass idle. A live read is the only way to notice."""

    LIVE = """
| STAPM LIMIT             |   35.000  | stapm limit |
| PPT LIMIT FAST          |   {fppt}  | fast limit  |
| PPT LIMIT SLOW          |   {sppt}  | slow limit  |
"""

    def setUp(self):
        self._run, self._isfile = main._run_ryzenadj, os.path.isfile
        main._wmi_verified_at = 0.0
        self.addCleanup(setattr, main, "_run_ryzenadj", self._run)
        self.addCleanup(setattr, os.path, "isfile", self._isfile)
        self.addCleanup(setattr, main, "_wmi_verified_at", 0.0)
        os.path.isfile = lambda path: True

    def _live(self, sppt, fppt, rc=0):
        text = self.LIVE.format(sppt=f"{sppt:.3f}", fppt=f"{fppt:.3f}")
        main._run_ryzenadj = lambda args, timeout=5.0: (rc, text, "")

    def test_an_override_is_noticed(self):
        self._live(15.0, 15.0)
        self.assertTrue(main._wmi_limits_overridden((25.0, 30.0, 35.0)))

    def test_matching_limits_are_left_alone(self):
        self._live(30.0, 35.0)
        self.assertFalse(main._wmi_limits_overridden((25.0, 30.0, 35.0)))

    def test_the_check_is_rate_limited(self):
        self._live(15.0, 15.0)
        self.assertTrue(main._wmi_limits_overridden((25.0, 30.0, 35.0)))
        # Spawning a process on every five-second pass is the cost the limits
        # cache was added to avoid; once per _WMI_VERIFY_EVERY_S is the budget.
        self.assertFalse(main._wmi_limits_overridden((25.0, 30.0, 35.0)))
        main._wmi_verified_at -= main._WMI_VERIFY_EVERY_S + 1
        self.assertTrue(main._wmi_limits_overridden((25.0, 30.0, 35.0)))

    def test_a_missing_binary_is_not_an_override(self):
        os.path.isfile = lambda path: False
        self._live(15.0, 15.0)
        self.assertFalse(main._wmi_limits_overridden((25.0, 30.0, 35.0)))

    def test_a_failed_read_is_not_an_override(self):
        # Better to leave the limits alone than to bounce the platform profile
        # on the strength of a reading we never got.
        self._live(15.0, 15.0, rc=-1)
        self.assertFalse(main._wmi_limits_overridden((25.0, 30.0, 35.0)))

    def test_an_unparsable_read_is_not_an_override(self):
        main._run_ryzenadj = lambda args, timeout=5.0: (0, "nothing useful", "")
        self.assertFalse(main._wmi_limits_overridden((25.0, 30.0, 35.0)))

    def test_spl_disagreement_alone_never_triggers(self):
        # STAPM is unreadable on this hardware, so it must not be able to drive
        # a re-apply on its own - that is the loop we just stopped chasing.
        self._live(30.0, 35.0)
        self.assertFalse(main._wmi_limits_overridden((5.0, 30.0, 35.0)))


class RaplDiscovery(unittest.TestCase):
    def setUp(self):
        self._dir, self._ts = main._rapl_dir, main._rapl_probed_at
        self._glob = main.RAPL_GLOB

    def tearDown(self):
        main._rapl_dir, main._rapl_probed_at = self._dir, self._ts
        main.RAPL_GLOB = self._glob

    def test_a_miss_is_retried_rather_than_remembered_forever(self):
        # powercap can register after the plugin starts; caching the miss left
        # the package draw blank until the plugin was reloaded.
        #
        # The miss has to be manufactured: a real Legion has powercap, so
        # without this the probe below finds it and the test only passed on a
        # dev box that has no /sys at all.
        main.RAPL_GLOB = "/nonexistent/powercap/intel-rapl:*"
        main._rapl_dir, main._rapl_probed_at = None, 0.0
        self.assertIsNone(main._find_rapl_package())
        self.assertEqual(main._rapl_dir, "")
        main._rapl_probed_at -= main._RAPL_RESCAN_S + 1
        probed_before = main._rapl_probed_at
        main._find_rapl_package()
        self.assertGreater(main._rapl_probed_at, probed_before)

    def test_a_hit_is_cached(self):
        main._rapl_dir, main._rapl_probed_at = "/sys/class/powercap/fake:0", 0.0
        self.assertEqual(main._find_rapl_package(), "/sys/class/powercap/fake:0")
        self.assertEqual(main._rapl_probed_at, 0.0)   # no rescan


class UpdateUrlValidation(unittest.TestCase):
    """The plugin runs as root and executes what it downloads."""

    def test_rejects_plain_http(self):
        with self.assertRaises(ValueError):
            updater.checked_url("http://github.com/x.zip")

    def test_rejects_non_http_schemes(self):
        for url in ("file:///etc/passwd", "ftp://github.com/x.zip"):
            with self.assertRaises(ValueError):
                updater.checked_url(url)

    def test_rejects_foreign_hosts(self):
        for url in ("https://evil.example.com/x.zip",
                    "https://github.com.evil.example.com/x.zip"):
            with self.assertRaises(ValueError):
                updater.checked_url(url)

    def test_accepts_known_github_hosts(self):
        for host in updater.ALLOWED_HOSTS:
            self.assertTrue(updater.checked_url(f"https://{host}/a.zip"))

    def test_the_ryzenadj_download_passes_its_own_check(self):
        self.assertTrue(updater.checked_url(main.RYZENADJ_URL))


class ShippedModuleNames(unittest.TestCase):
    """Before a plugin is imported, the loader aliases every one of its own
    submodules to a bare name:

        for key in [k for k in sys.modules if k.startswith("decky_loader.")]:
            sys.modules[key.replace("decky_loader.", "")] = sys.modules[key]

    `import x` consults sys.modules before sys.path, so a plugin file named
    after one of them never loads at all - the import silently hands back the
    loader's module instead. That is exactly how a shared `updater.py` shipped
    and killed both plugins on startup with a TypeError from the wrong Updater.
    """

    RESERVED = frozenset({
        "browser", "enums", "helpers", "injector", "loader",
        "main", "settings", "updater", "utilities", "wsrouter",
    })

    def test_no_shipped_module_is_shadowed_by_the_loader(self):
        shipped = {
            os.path.splitext(os.path.basename(path))[0]
            for path in glob.glob(os.path.join(main.PLUGIN_DIR, "*.py"))
        }
        # main.py is the one exemption: the loader loads it from an explicit
        # file location rather than by module name.
        self.assertEqual(sorted((shipped & self.RESERVED) - {"main"}), [])

    def test_the_packaged_payload_matches_what_we_import(self):
        # The zip is what reaches the device, so a rename that misses
        # scripts/package.mjs ships a plugin with no updater module at all.
        script = os.path.join(main.PLUGIN_DIR, "scripts", "package.mjs")
        if not os.path.isfile(script):
            self.skipTest("repo-only check; the deployed plugin ships no scripts/")
        with open(script) as handle:
            packaged = handle.read()
        self.assertIn('"lego_updater.py"', packaged)
        self.assertNotIn('"updater.py"', packaged)


class Versions(unittest.TestCase):
    def test_ordering(self):
        self.assertGreater(updater.version_tuple("1.5.0"), updater.version_tuple("1.4.9"))
        self.assertGreater(updater.version_tuple("1.10.0"), updater.version_tuple("1.9.0"))
        self.assertEqual(updater.version_tuple("1.5.0"), updater.version_tuple("1.5.0"))

    def test_non_numeric_tags_do_not_raise(self):
        self.assertEqual(updater.version_tuple("v1.5.0-beta"), (1, 5, 0))
        self.assertEqual(updater.version_tuple("nonsense"), ())

    def test_plugin_version_matches_the_manifest(self):
        with open(os.path.join(main.PLUGIN_DIR, "plugin.json")) as handle:
            self.assertEqual(main.updater.plugin_version(), json.load(handle)["version"])

    def test_the_loaders_version_wins_over_the_manifest(self):
        # PluginWrapper takes the version from package.json, so that is what
        # Decky's own plugin list shows. Preferring it here keeps the panel from
        # contradicting the loader if the two manifests ever drift.
        os.environ["DECKY_PLUGIN_VERSION"] = "9.9.9"
        try:
            self.assertEqual(main.updater.plugin_version(), "9.9.9")
        finally:
            del os.environ["DECKY_PLUGIN_VERSION"]

    def test_the_two_manifests_agree(self):
        # Nothing enforces this at runtime: the loader reads one file and the
        # packaging script reads the other.
        with open(os.path.join(main.PLUGIN_DIR, "plugin.json")) as handle:
            plugin_json = json.load(handle)["version"]
        with open(os.path.join(main.PLUGIN_DIR, "package.json")) as handle:
            package_json = json.load(handle)["version"]
        self.assertEqual(plugin_json, package_json)


class DownloadDirectory(unittest.TestCase):
    def test_reads_the_xdg_configuration(self):
        with tempfile.TemporaryDirectory() as home:
            config = os.path.join(home, ".config")
            os.makedirs(config)
            with open(os.path.join(config, "user-dirs.dirs"), "w") as handle:
                handle.write('XDG_DOWNLOAD_DIR="$HOME/Pobrane"\n')
            # The value is substituted verbatim, so the separator is the one from
            # the config file rather than the host's.
            self.assertEqual(updater.xdg_download_dir(home), f"{home}/Pobrane")

    def test_falls_back_to_downloads(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(updater.xdg_download_dir(home),
                             os.path.join(home, "Downloads"))


class TlsContext(unittest.TestCase):
    def test_verification_stays_enabled_with_a_populated_store(self):
        context = main.updater.ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        # An empty store is the frozen-loader failure mode the fallback exists to
        # cover; if it is still empty here, nothing would ever verify.
        self.assertGreater(context.cert_store_stats()["x509_ca"], 0)


class DownloadCeiling(unittest.TestCase):
    """A truncated or endless download must not fill the device's disk."""

    class _Response:
        def __init__(self, total):
            self.remaining = total

        def read(self, size):
            chunk = b"x" * min(size, self.remaining)
            self.remaining -= len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _updater_returning(self, total):
        u = updater.Updater(releases_url="https://api.github.com/x",
                            user_agent="test", log_prefix="[test]",
                            plugin_dir=main.PLUGIN_DIR, logger=main.decky.logger)
        u.open_url = lambda url, timeout: self._Response(total)
        return u

    def test_a_small_download_reports_its_size(self):
        u = self._updater_returning(1024)
        with tempfile.TemporaryFile() as out:
            self.assertEqual(u.download_to("https://github.com/a.zip", out, 10), 1024)

    def test_an_oversized_download_is_aborted(self):
        u = self._updater_returning(updater.MAX_DOWNLOAD_BYTES + 1)
        with tempfile.TemporaryFile() as out:
            with self.assertRaises(ValueError):
                u.download_to("https://github.com/a.zip", out, 10)


class ProfileLookup(unittest.TestCase):
    def setUp(self):
        seed(FIXTURE)

    def test_a_game_without_a_profile_is_absent(self):
        self.assertNotIn(GAME_WITHOUT_PROFILE, main._load_profiles())

    def test_a_saved_profile_survives_a_reload(self):
        profiles = main._load_profiles()
        profiles[GAME_WITHOUT_PROFILE] = {"spl": 8000, "sppt": 8000, "fppt": 8000}
        main._save_profiles(profiles)
        self.assertEqual(main._load_profiles()[GAME_WITHOUT_PROFILE]["spl"], 8000)


if __name__ == "__main__":
    unittest.main()
