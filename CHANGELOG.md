# Changelog

All notable changes to LeGoTDP, newest first.

## [1.6.0] - 2026-07-28

### Added

- Support for the Lenovo Legion Go S with the Ryzen Z1 Extreme, which is the variant this was measured and tested on. It drives the same Lenovo firmware interface as the Go 2, so everything except the extended range works there unchanged.
- Other Legion Go S variants, including the Ryzen Z2 Go, take the same firmware-only path but are untested. Nothing there is assumed: the limits come from what that machine's own firmware reports, so a variant with different ceilings gets its own rather than the Z1 Extreme's.

### Fixed

- TDP is restored after the charger is plugged in. The firmware applies a profile of its own on that transition and it lands after the plugin's, so a single write at the moment the state changed was overwritten a fraction of a second later - measured on a Legion Go S as 40/43/53 W asked for and 10/15/20 W in place. The limits are now re-asserted over the following seconds until they stop being overwritten, and each pass is skipped once the hardware already agrees.

### Changed

- The Current TDP panel is always shown, including while the plugin is switched off. With it off that reading is the only way to see what the firmware settled on, which is exactly when it is worth having.
- Presets are spaced against the ceilings of the machine they run on and served by the backend, so there is one place that knows them. A Legion Go S gets 5/8/10, 8/10/15, 18/20/25, 33/33/35 and 40/43/53 W - its Max asks for everything the firmware reports. The Legion Go 2 ladder is unchanged.
- Slider ceilings are taken per parameter from what the firmware reports it accepts, instead of one shared limit. A Legion Go S answers 40 / 43 / 53 W for SPL / SPPT / FPPT, and the sliders now stop at each of those rather than at the highest.
- Profiles carried over from another machine are clamped to what the hardware in front of you actually takes, so a 50 W profile no longer arrives as a request the firmware will refuse.
- The Extras section is hidden on hardware driven through the firmware alone, and `ryzenadj` is not downloaded there. The plugin fetches that binary itself when the extended range needs it, and on those machines it is not wanted - the firmware range is the whole range.
- A firmware apply that falls outside the accepted range now says so, instead of failing over to a tool that was never installed.

### Internal

- Hardware is recognised by DMI product family rather than model number, so other SKUs in the same family are covered. Anything unrecognised keeps the behaviour it had, which is what leaves the Legion Go 2 path untouched.
- Backend tests up to 104 from 93.

## [1.5.0] - 2026-07-25

### Added

- Settings and per-game profiles now live in Decky's own settings directory, so reinstalling the plugin keeps them.
- The limits are cross-checked against a second, independent reading twice a minute. The firmware only reports back what the plugin last wrote to it, so anything that moved the limits behind its back went unnoticed and uncorrected.
- The installed version is shown in the panel before you check for updates.
- Uninstalling hands the platform profile back to the firmware, instead of leaving it pinned to the last TDP the plugin set. See Known issues.

### Changed

- TDP is re-applied the moment the console wakes, rather than within the following five seconds.
- Game detection reacts to Steam's own launch and exit events instead of polling, and keeps working while the plugin menu is closed.
- Current TDP readings are pushed from the backend as they are taken, instead of being fetched twice a second by the panel.
- Failures raise a notification, rather than only a line in a panel you may not be looking at.
- Colours follow the Steam theme instead of being hardcoded.

### Fixed

- The SPL row in Current TDP showed the FPPT value whenever the Extras range was in use. The chip reports its sustained limit as a copy of the fast limit, and the plugin was taking that at face value.
- Every change in the Extras range cost three redundant re-applies before the plugin stopped chasing a number the hardware was never going to return.
- Moving a slider was briefly reported as unexpected drift and corrected a second time, because the panel's cached reading predated the change.
- Monitoring could keep running after the Steam interface went away, reading power counters every two seconds for the rest of the session.

### Known issues

- **Upgrading from 1.4.0 or earlier loses your saved values.** Decky deletes the old plugin folder before the new version ever starts, and that folder is where they used to live. Write them down first, or see the README for how to hand the old file back. Every update after this one keeps them automatically.
- Decky does not reliably give a plugin the chance to run its uninstall step, so the platform profile is not guaranteed to be handed back. **Turn the plugin off before uninstalling** and it always is.

### Internal

- Update and download code is shared with LeGo Vibe Control, so a fix to certificate handling, the download allowlist or the release check lands in both plugins at once.
- Settings migration moved to Decky's `_migration()` lifecycle hook, so it finishes before anything can read the store.
- Backend test suite, run in CI on every push.

## [1.4.0] - 2026-07-24

### Added

- Firmware-first TDP control. Limits are applied through the Lenovo firmware (WMI) interface, which is more stable and survives sleep; `ryzenadj` is used only as a fallback for the extended range.
- SPPT and FPPT are set as offsets above SPL. SPL is the main TDP dial, and the other two are headroom above it (up to +10 W and +15 W); the sliders clamp against each other live, so no combination can exceed the limit.
- Live package power draw, read from RAPL and shown in the Current TDP panel next to each limit.

### Changed

- Maximum TDP lowered from 60 W to 50 W. 60 W was never actually reachable on this hardware; profiles saved above 50 W are migrated down automatically.

### Fixed

- Charging state no longer flickers. Power detection now counts only the mains adapter and ignores the USB-C port's PD role, which had made the state jump back to "charging" right after unplugging.
- Per-game battery and AC profiles switch reliably. The running game is detected even inside Proton and gamescope, so unplugging the charger applies the game's battery profile instead of falling back to the global one.
- TDP survives suspend and resume; the limits are re-applied after the console wakes.
- No more constant re-applying and log spam on extended-range (Extras) targets.
- Failed TDP changes no longer appear as a green "success" message.
- The downloaded update file is owned by you instead of by root.

## [1.3.2] - 2026-05-21

### Fixed

- Update downloads respect the system language. The ZIP is saved to your actual XDG download directory - `Scaricati`, `Téléchargements` and so on - instead of a hardcoded `Downloads` folder.

## [1.3.1] - 2026-05-21

### Fixed

- The plugin failed to load after a fresh install. `package.json` is now included in the release ZIP; without it Decky Loader fell back to legacy script loading, which is incompatible with the ES module bundle, and showed a syntax error instead of the UI.

## [1.3.0] - 2026-05-20

### Added

- Separate AC profile. Set independent TDP limits for battery and AC; the plugin switches automatically when the charger is plugged or unplugged. Works for both global settings and per-game profiles.
- Extended TDP range. A new Extras section with an unlock toggle raises the Custom slider limits to 60 W for SPL, SPPT and FPPT, for advanced users.
- The preset name is shown as a label below the preset buttons, so you always know which preset is active.

### Fixed

- The settings file is written atomically - to a temporary file, then replaced - to prevent corruption on an unexpected shutdown.
- The ryzenadj lock now correctly serialises all hardware calls across the enforce and info loops.

## [1.2.0] - 2026-05-18

### Added

- In-plugin update system. Check for updates and download the new version directly from the plugin menu.
- The downloaded ZIP is saved to `~/Downloads`, with install instructions shown in the UI.

## [1.1.0] - 2026-05-18

### Added

- Minimum preset (5/5/10 W).

### Changed

- The Live TDP panel only polls ryzenadj while the panel is visible.

### Fixed

- The device froze when opening or closing the Live TDP panel. ryzenadj calls are now handled entirely in the backend, decoupled from frontend IPC.

## [1.0.0] - 2026-05-17

Initial release. Requires a Lenovo Legion Go 2 (Ryzen Z2 Extreme) with DeckyLoader installed.

### Added

- SPL, SPPT and FPPT power limits, set via preset buttons or custom sliders.
- Presets: Silent (8/10/15 W), Balanced (15/18/25 W), Performance (25/28/35 W), Max (35/37/45 W).
- Per-game profiles, saved per Steam App ID and applied automatically in the background when a game launches, with no need to open the plugin menu.
- Global settings restored automatically when a game exits.
- Live TDP panel showing the current limits and real-time power draw via ryzenadj.
- Drift enforcement, re-applying your settings every 5 seconds if the system overrides them.
- Enable/disable toggle, restoring firmware defaults when turned off.
- The ryzenadj binary is downloaded automatically on first run.
