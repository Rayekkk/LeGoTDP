# LeGoTDP

A [DeckyLoader](https://github.com/SteamDeckHomebrew/decky-loader) plugin for setting AMD CPU TDP limits directly from the Steam overlay.

Designed exclusively for the **Lenovo Legion Go 2** (Ryzen Z2 Extreme / Strix Point).

---

## Features

- **Presets** - Silent / Balanced / Performance / Max with one tap
- **Custom mode** - fine-tune SPL, SPPT and FPPT via sliders
- **Per-game profiles** - automatically applied in the background when a game launches, no need to open the plugin menu
- **Live TDP panel** - shows current limits and real-time power draw from ryzenadj
- **Drift enforcement** - re-applies your settings every 5 seconds if the system overrides them
- **Enable/disable toggle** - restores firmware defaults (`--max-performance`) when turned off
- Auto-downloads a pre-built `ryzenadj` binary on first run (no manual setup needed)

---

## Presets

| Preset | SPL | SPPT | FPPT |
|---|---|---|---|
| Silent | 8 W | 10 W | 15 W |
| Balanced | 15 W | 18 W | 25 W |
| Performance | 25 W | 28 W | 35 W |
| Max | 35 W | 37 W | 45 W |

---

## Requirements

- Lenovo Legion Go 2 (or another AMD APU device running SteamOS)
- [DeckyLoader](https://github.com/SteamDeckHomebrew/decky-loader) installed

---

## Installation

1. Download the latest `LeGoTDP.zip` from the [Releases](../../releases) page.
2. In DeckyLoader, open the settings and enable **Developer Mode**.
3. In the Developer section, choose **Install Plugin from ZIP** and select the downloaded file.

---

## Building from source

**Prerequisites:** Node.js >= 18, npm

```bash
npm install
npm run build
```

The built frontend lands in `dist/`. Copy the entire plugin directory to `~/homebrew/plugins/LeGoTDP/` and reload DeckyLoader.

---

## TDP parameters

| Parameter | ryzenadj flag | Description | Range |
|---|---|---|---|
| SPL | `--stapm-limit` | Sustained Power Limit - thermal steady-state target | 5-35 W |
| SPPT | `--slow-limit` | Slow Package Power Tracking - sustained hard ceiling | 5-37 W |
| FPPT | `--fast-limit` | Fast Package Power Tracking - burst ceiling | 5-45 W |

> Note: On Strix Point (Ryzen Z2 Extreme) STAPM always mirrors FPPT due to a known ryzenadj v0.19.0 limitation. SPPT and FPPT are the effective controls.

---

## How it works

All TDP control goes through `ryzenadj`, which writes limits directly to the AMD SMU via PCIe MMIO.

The Python backend runs an enforce loop every 5 seconds that:
1. Detects running Steam games by scanning `/proc/*/environ` for `SteamAppId`
2. Applies a saved per-game profile automatically when a game launches
3. Restores global settings when a game exits
4. Re-applies settings if the system has overridden them (drift correction)

`ryzenadj` is fetched automatically from [FlyGoat/RyzenAdj](https://github.com/FlyGoat/RyzenAdj) GitHub releases on the first run.

---

## License

MIT
