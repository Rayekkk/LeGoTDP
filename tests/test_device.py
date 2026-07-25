"""Tests that need a real Legion Go 2 with the Lenovo WMI attributes present.

They skip themselves when the firmware interface is absent, so the same
`python -m unittest discover tests` works on a build machine and on the device.

These apply real TDP limits. Each class puts the previous limits back in
tearDown, but the fans will audibly react while they run - that is expected.
"""
import os
import unittest

from _harness import FIXTURE, has_wmi, main, seed

requires_wmi = unittest.skipUnless(
    has_wmi(), "no Lenovo WMI firmware attributes present"
)


class RestoresLimits(unittest.TestCase):
    """Base class: remember what was applied and put it back afterwards."""

    def setUp(self):
        seed(FIXTURE)
        self._before = main._read_limits()
        self._source = main._last_source

    def tearDown(self):
        want = tuple(self._before.get(f"{k}_limit") for k in ("spl", "sppt", "fppt"))
        if all(v is not None for v in want):
            main._apply_limits(*(int(v * 1000) for v in want))
        main._last_source = self._source


@requires_wmi
class FirmwareCapabilities(unittest.TestCase):
    def test_every_attribute_reports_a_range(self):
        caps = main._wmi_caps()
        self.assertEqual(set(caps), set(main.WMI_ATTRS))
        for key, bounds in caps.items():
            self.assertLess(bounds["min"], bounds["max"], key)

    def test_a_tunable_platform_profile_exists(self):
        # The firmware only latches ppt_* writes on a transition into 'custom',
        # so without this node the WMI path cannot work at all.
        path = main._profile_path()
        self.assertIsNotNone(path)
        with open(os.path.join(os.path.dirname(path), "choices")) as handle:
            self.assertIn("custom", handle.read().split())


@requires_wmi
class ApplyingLimits(RestoresLimits):
    def test_wmi_write_reaches_the_firmware(self):
        self.assertTrue(main._apply_wmi(12, 14, 18)["success"])
        self.assertTrue(main._ppt_matches(12, 14, 18))

    def test_a_second_write_in_custom_still_latches(self):
        # Regression guard: writing while already in 'custom' used to be dropped
        # silently, which is why the apply path bounces the profile.
        main._apply_wmi(12, 14, 18)
        self.assertTrue(main._apply_wmi(16, 18, 22)["success"])
        self.assertTrue(main._ppt_matches(16, 18, 22))

    def test_the_dispatcher_prefers_the_firmware(self):
        self.assertTrue(main._apply_limits(15000, 18000, 25000)["success"])
        self.assertEqual(main._last_source, "wmi")

    def test_read_back_follows_whichever_layer_applied(self):
        main._apply_limits(15000, 18000, 25000)
        limits = main._read_limits()
        self.assertEqual(limits["spl_limit"], 15)

    def test_out_of_range_requests_are_clamped_before_the_hardware(self):
        result = main._apply_limits(99000, 99000, 99000)
        self.assertTrue(result["success"])
        self.assertLessEqual(main._read_limits()["spl_limit"], main.HARD_MAX_MW / 1000)


@requires_wmi
class PowerReadings(unittest.TestCase):
    def test_rapl_reports_a_plausible_package_draw(self):
        main._rapl_watts()          # first call only primes the counter
        import time
        time.sleep(0.5)
        watts = main._rapl_watts()
        self.assertIsNotNone(watts)
        self.assertGreater(watts, 0)
        self.assertLess(watts, main.HARD_MAX_MW / 1000 * 2)

    def test_ac_detection_answers(self):
        self.assertIsInstance(main._get_ac_online(), bool)


@requires_wmi
class PluginApi(RestoresLimits, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.plugin = main.Plugin()
        main.Plugin._ready = True

    async def test_reports_ready(self):
        self.assertTrue((await self.plugin.is_ready())["ready"])

    async def test_version_matches_the_manifest(self):
        import json
        with open(os.path.join(main.PLUGIN_DIR, "plugin.json")) as handle:
            self.assertEqual((await self.plugin.get_version())["version"],
                             json.load(handle)["version"])

    async def test_caps_come_from_the_firmware(self):
        caps = await self.plugin.get_caps()
        self.assertTrue(caps["wmi"])
        self.assertLessEqual(caps["std"]["spl"], caps["max"]["spl"])

    async def test_applying_a_global_tdp_persists_it(self):
        result = await self.plugin.apply_tdp(15000, 18000, 25000, "", "balanced")
        self.assertTrue(result["success"])
        settings = await self.plugin.get_settings()
        self.assertEqual(settings["spl"], 15000)
        self.assertEqual(settings["active_preset"], "balanced")

    async def test_a_game_profile_does_not_overwrite_the_global_one(self):
        await self.plugin.apply_tdp(15000, 18000, 25000, "", "balanced")
        await self.plugin.apply_tdp(25000, 28000, 35000, "9999", "performance")
        settings = await self.plugin.get_settings()
        # This leaking is what used to promote a game's values to the global
        # default after a restart.
        self.assertEqual(settings["spl"], 15000)
        profile = await self.plugin.get_game_profile("9999")
        self.assertTrue(profile["exists"])
        self.assertEqual(profile["profile"]["spl"], 25000)
        await self.plugin.delete_game_profile("9999")

    async def test_reapply_restores_the_active_limits(self):
        # Stands in for resume from suspend, where the SMU comes back at firmware
        # defaults. Driven from the frontend off Steam's notification: Decky has
        # no backend resume hook to test through.
        await self.plugin.apply_tdp(15000, 18000, 25000, "", "balanced")
        main._apply_wmi(30, 30, 30)         # something external moves them
        result = await self.plugin.reapply()
        self.assertTrue(result["success"])
        self.assertFalse(result["skipped"])
        self.assertEqual(main._read_limits()["spl_limit"], 15)

    async def test_reapply_does_nothing_while_disabled(self):
        await self.plugin.apply_tdp(15000, 18000, 25000, "", "balanced")
        await self.plugin.set_plugin_enabled(False)
        try:
            main._apply_wmi(30, 30, 30)
            self.assertTrue((await self.plugin.reapply())["skipped"])
            # Left where it was: a disabled plugin has no business touching the
            # hardware just because the machine woke up.
            self.assertEqual(main._read_limits()["spl_limit"], 30)
        finally:
            await self.plugin.set_plugin_enabled(True)


if __name__ == "__main__":
    unittest.main()
