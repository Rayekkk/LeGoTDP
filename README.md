# LeGoTDP

A [DeckyLoader](https://github.com/SteamDeckHomebrew/decky-loader) plugin for setting AMD CPU TDP limits directly from the Steam overlay.

Designed for the **Lenovo Legion Go 2** - uses Lenovo WMI firmware attributes natively, with ryzenadj as a fallback for other AMD devices.

---

## Features

- Set **SPL** (Sustained), **SPPT** (Slow) and **FPPT** (Fast) power limits via sliders
- **Per-game profiles** - automatically applied when a game launches
- **Live TDP panel** - shows current limits and real-time power draw
- **Enable/disable toggle** - restores firmware defaults when turned off
- Auto-downloads a pre-built `ryzenadj` binary on first run (no manual setup needed)

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

**Prerequisites:** Node.js ≥ 18, npm

```bash
npm install
npm run build
```

The built frontend lands in `dist/`. Copy the entire plugin directory to `~/homebrew/plugins/LeGoTDP/` and reload DeckyLoader.

---

## TDP parameters

| Parameter | ryzenadj flag | WMI sysfs attribute | Description |
|---|---|---|---|
| SPL | `--stapm-limit` | `ppt_pl1_spl` | Sustained Power Limit (long-term) |
| SPPT | `--slow-limit` | `ppt_pl2_sppt` | Slow Package Power Tracking |
| FPPT | `--fast-limit` | `ppt_pl3_fppt` | Fast Package Power Tracking (burst) |

Values are set in watts (range reported by the device firmware, typically 1–54 W).

---

## How it works

On devices with Lenovo WMI firmware attributes (`/sys/class/firmware-attributes/lenovo-wmi-other-0/`), the plugin writes directly to the sysfs interface - no external binary needed. On other AMD devices it falls back to calling `ryzenadj`.

`ryzenadj` is fetched automatically from [FlyGoat/RyzenAdj](https://github.com/FlyGoat/RyzenAdj) GitHub releases on the first run.

---

## License

MIT
