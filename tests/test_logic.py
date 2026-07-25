"""Backend tests that need no Legion Go attached. These run in CI."""
import json
import os
import ssl
import tempfile
import unittest

from _harness import (
    FIXTURE,
    GAME_WITHOUT_PROFILE,
    GAME_WITH_PROFILE,
    main,
    seed,
    updater,
)


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
