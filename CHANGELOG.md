# Changelog

All notable changes to LeGoTDP, newest first.

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
