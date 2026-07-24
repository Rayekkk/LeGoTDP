# LeGoTDP

A [DeckyLoader](https://github.com/SteamDeckHomebrew/decky-loader) plugin for setting AMD CPU TDP limits directly from the Steam overlay.

Designed exclusively for the **Lenovo Legion Go 2** (Ryzen Z2 Extreme / Strix Point).

---

## Features

- **Presets** - Minimum / Silent / Balanced / Performance / Max with one tap
- **Custom mode** - SPL is the main TDP dial; SPPT and FPPT are set as headroom *above* it
- **Firmware-first** - limits are written through the Lenovo WMI interface, falling back to `ryzenadj` only for the extended range
- **Per-game profiles** - automatically applied in the background when a game launches, no need to open the plugin menu
- **Separate AC profile** - set independent TDP limits for battery and charging; switches automatically when AC state changes
- **Live TDP panel** - shows current limits plus real-time package draw read from RAPL
- **Drift enforcement** - re-applies your settings every 5 seconds if the system overrides them, and stands down on targets the hardware refuses
- **Enable/disable toggle** - hands the platform profile back to the firmware when turned off
- **Extended TDP range** - Extras section unlocks Custom sliders up to 50 W (advanced users, use at your own risk)
- Auto-downloads a pre-built `ryzenadj` binary on first run (only needed for the extended range)

---

## Presets

| Preset | SPL | SPPT | FPPT |
|---|---|---|---|
| Minimum | 5 W | +0 (5 W) | +5 (10 W) |
| Silent | 8 W | +2 (10 W) | +7 (15 W) |
| Balanced | 15 W | +3 (18 W) | +10 (25 W) |
| Performance | 25 W | +3 (28 W) | +10 (35 W) |
| Max | 35 W | +2 (37 W) | +10 (45 W) |

---

## Requirements

| Requirement | Details |
|---|---|
| Device | Lenovo Legion Go 2 |
| Plugin loader | [DeckyLoader](https://github.com/SteamDeckHomebrew/decky-loader) |

---

## Installation

### Easy install (recommended)

1. Download the latest `LeGoTDP.zip` from the [Releases](../../releases) page.
2. In DeckyLoader, open the settings and enable **Developer Mode**.
3. In the Developer section, choose **Install Plugin from ZIP** and select the downloaded file.

### From source

**Prerequisites:** Node.js >= 18, npm

```bash
npm install
npm run build
```

The built frontend lands in `dist/`. Copy the entire plugin directory to `~/homebrew/plugins/LeGoTDP/` and reload DeckyLoader.

---

## TDP parameters

SPL is an absolute value. SPPT and FPPT are set as an offset above SPL, so raising the
TDP carries the burst limits with it and the ordering `SPL <= SPPT <= FPPT` always holds.

| Parameter | WMI attribute | ryzenadj flag | Description | Range |
|---|---|---|---|---|
| SPL | `ppt_pl1_spl` | `--stapm-limit` | Sustained Power Limit - thermal steady-state target | 5-35 W (50 W with Extras) |
| SPPT | `ppt_pl2_sppt` | `--slow-limit` | Slow Package Power Tracking - sustained hard ceiling | +0 to +10 W above SPL |
| FPPT | `ppt_pl3_fppt` | `--fast-limit` | Fast Package Power Tracking - burst ceiling | +0 to +15 W above SPL |

Both offsets shrink automatically as SPL approaches the ceiling, so no combination can
exceed it. At the top of the range both collapse to +0.

---

## How it works

Limits are applied through whichever layer can satisfy them:

- **Lenovo WMI** (preferred) - writes `ppt_pl*` under `/sys/class/firmware-attributes/`,
  which requires the platform profile to be `custom`. The firmware owns the value, so it
  is not fought back and survives suspend.
- **ryzenadj** (fallback) - used only when the request exceeds what the firmware accepts,
  i.e. the Extras range. Writes straight to the AMD SMU via PCIe MMIO.

The two layers do not observe each other: after a `ryzenadj` write the WMI attributes
still report the firmware's own bookkeeping, so the plugin reads back limits from
whichever layer last applied them.

Live package draw comes from the RAPL energy counter under `/sys/class/powercap/`, so
the panel does not need to spawn a process to refresh.

The Python backend runs an enforce loop every 5 seconds that:
1. Detects running Steam games by scanning `/proc/*/environ` for `SteamAppId`
2. Applies a saved per-game profile automatically when a game launches
3. Restores global settings when a game exits
4. Re-applies settings if the system has overridden them (drift correction), giving up
   after a few attempts on targets the hardware silently refuses

`ryzenadj` is fetched automatically from [FlyGoat/RyzenAdj](https://github.com/FlyGoat/RyzenAdj)
GitHub releases on the first run. If that fails and WMI is available, the plugin still works
with the standard range.

---

---

## Third-party

This plugin downloads and uses [ryzenadj](https://github.com/FlyGoat/RyzenAdj) as an external binary, which is licensed under [LGPL-3.0](https://github.com/FlyGoat/RyzenAdj/blob/master/LICENSE).

---

## License

MIT — see [LICENSE](LICENSE).
