import { definePlugin, callable } from "@decky/api";
import {
  ButtonItem,
  Field,
  PanelSection,
  PanelSectionRow,
  Router,
  SliderField,
  Spinner,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { FC, useEffect, useRef, useState } from "react";

// ── Helpers ────────────────────────────────────────────────────────────────────
const toMw  = (w: number)  => w * 1000;
const toW   = (mw: number) => Math.round(mw / 1000);
const fmt   = (v?: number) => v != null ? `${v.toFixed(1)} W` : "-";

// ── Presets ────────────────────────────────────────────────────────────────────
type PresetKey = "minimum" | "silent" | "balanced" | "performance" | "max" | "custom";

const PRESETS: Record<Exclude<PresetKey, "custom">, { spl: number; sppt: number; fppt: number }> = {
  minimum:     { spl: 5,  sppt: 5,  fppt: 10 },
  silent:      { spl: 8,  sppt: 10, fppt: 15 },
  balanced:    { spl: 15, sppt: 18, fppt: 25 },
  performance: { spl: 25, sppt: 28, fppt: 35 },
  max:         { spl: 35, sppt: 37, fppt: 45 },
};

const PRESET_LABELS: Record<PresetKey, string> = {
  minimum:     "Minimum",
  silent:      "Silent",
  balanced:    "Balanced",
  performance: "Performance",
  max:         "Max",
  custom:      "Custom",
};

const PRESET_ORDER: PresetKey[] = ["minimum", "silent", "balanced", "performance", "max", "custom"];

function detectPreset(spl: number, sppt: number, fppt: number): PresetKey {
  for (const key of Object.keys(PRESETS) as Exclude<PresetKey, "custom">[]) {
    const v = PRESETS[key];
    if (v.spl === spl && v.sppt === sppt && v.fppt === fppt) return key;
  }
  return "custom";
}

// ── Types ──────────────────────────────────────────────────────────────────────
interface Settings   { spl: number; sppt: number; fppt: number; enabled: boolean }
interface TdpResult  { success: boolean; stderr: string }
interface TdpValues  {
  spl_limit?:  number; spl_value?:  number;
  sppt_limit?: number; sppt_value?: number;
  fppt_limit?: number; fppt_value?: number;
}
interface TdpInfo     { success: boolean; values: TdpValues; error?: string }
interface GameProfile { exists: boolean; profile: Settings }
interface RunningGame { appId: string; name: string }
interface ReadyState  { ready: boolean; error: string | null }

// ── Backend callables ──────────────────────────────────────────────────────────
const isReady           = callable<[], ReadyState>("is_ready");
const getSettings       = callable<[], Settings>("get_settings");
const applyTdp          = callable<[number, number, number, string], TdpResult>("apply_tdp");
const getTdpInfo        = callable<[], TdpInfo>("get_tdp_info");
const getGameProfile    = callable<[string], GameProfile>("get_game_profile");
const deleteGameProfile = callable<[string], void>("delete_game_profile");
const setPluginEnabled  = callable<[boolean], void>("set_plugin_enabled");
const restoreDefaults   = callable<[], TdpResult>("restore_defaults");
const setPanelActive    = callable<[boolean], void>("set_panel_active");

// ── Slider limits ──────────────────────────────────────────────────────────────
const LIMITS = {
  spl:  { min: 5, max: 35 },
  sppt: { min: 5, max: 37 },
  fppt: { min: 5, max: 45 },
};

// ── Steam game detection ───────────────────────────────────────────────────────
const detectRunningGame = (): RunningGame | null => {
  const app = (Router as any)?.MainRunningApp;
  if (app?.appid) {
    return { appId: String(app.appid), name: app.display_name ?? String(app.appid) };
  }
  return null;
};

// ── Icon ───────────────────────────────────────────────────────────────────────
const ChipIcon: FC = () => (
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"
    style={{ width: "1em", height: "1em" }}>
    <path d="M9 2v2H7a2 2 0 0 0-2 2v2H3v2h2v2H3v2h2v2H3v2h2v2a2 2 0 0 0 2 2h2v2h2v-2h2v2h2v-2h2a2 2 0 0 0 2-2v-2h2v-2h-2v-2h2v-2h-2V9h2V7h-2V6a2 2 0 0 0-2-2h-2V2h-2v2h-2V2H9zm-1 4h12v12H8V6zm3 3v6h6V9h-6z" />
  </svg>
);

// ── Live TDP panel ─────────────────────────────────────────────────────────────
const LivePanel: FC = () => {
  const [info, setInfo] = useState<TdpInfo | null>(null);

  useEffect(() => {
    let active = true;
    setPanelActive(true);
    const refresh = async () => {
      try { if (active) setInfo(await getTdpInfo()); } catch (_) {}
    };
    refresh();
    const id = setInterval(refresh, 2000);
    return () => {
      active = false;
      clearInterval(id);
      setPanelActive(false);
    };
  }, []);

  const v = info?.values ?? {};
  return (
    <PanelSection title="Current TDP">
      {!info ? (
        <PanelSectionRow><Spinner /></PanelSectionRow>
      ) : !info.success ? (
        <PanelSectionRow>
          <Field label="Error" description={info.error ?? "Failed to read TDP"} />
        </PanelSectionRow>
      ) : (
        <>
          <PanelSectionRow>
            <Field label="SPL  (Sustained)"
              description={`Limit: ${fmt(v.spl_limit)}   -   Now: ${fmt(v.spl_value)}`} />
          </PanelSectionRow>
          <PanelSectionRow>
            <Field label="SPPT (Slow)"
              description={`Limit: ${fmt(v.sppt_limit)}   -   Now: ${fmt(v.sppt_value)}`} />
          </PanelSectionRow>
          <PanelSectionRow>
            <Field label="FPPT (Fast)"
              description={`Limit: ${fmt(v.fppt_limit)}   -   Now: ${fmt(v.fppt_value)}`} />
          </PanelSectionRow>
        </>
      )}
    </PanelSection>
  );
};

// ── Main content ───────────────────────────────────────────────────────────────
const Content: FC = () => {
  const [ready,    setReady]    = useState(false);
  const [setupErr, setSetupErr] = useState<string | null>(null);

  const [spl,      setSpl]      = useState(15);
  const [sppt,     setSppt]     = useState(15);
  const [fppt,     setFppt]     = useState(15);
  const [preset,   setPreset]   = useState<PresetKey>("balanced");

  const [enabled,  setEnabled]  = useState(true);
  const [game,     setGame]     = useState<RunningGame | null>(null);
  const [perGame,  setPerGame]  = useState(false);

  const [status,   setStatus]   = useState<string | null>(null);
  const [loading,  setLoading]  = useState(false);

  const autoAppliedRef = useRef<string | null>(null);
  const statusTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showStatus = (msg: string | null) => {
    if (statusTimerRef.current) clearTimeout(statusTimerRef.current);
    setStatus(msg);
    if (msg) statusTimerRef.current = setTimeout(() => setStatus(null), 3000);
  };

  // ── Init ─────────────────────────────────────────────────────────────────────
  useEffect(() => {
    let active = true;
    const check = async () => {
      try {
        const r = await isReady();
        if (!active) return;
        if (r.error) { setSetupErr(r.error); return; }
        if (r.ready) {
          const s = await getSettings();
          const w = toW(s.spl), sw = toW(s.sppt), fw = toW(s.fppt);
          setSpl(w); setSppt(sw); setFppt(fw);
          setPreset(detectPreset(w, sw, fw));
          setEnabled(s.enabled !== false);
          setReady(true);
        } else {
          setTimeout(check, 1000);
        }
      } catch (_) { if (active) setTimeout(check, 1000); }
    };
    check();
    return () => { active = false; };
  }, []);

  // ── Game detection polling ────────────────────────────────────────────────────
  useEffect(() => {
    if (!ready) return;
    const poll = () => setGame(detectRunningGame());
    poll();
    const id = setInterval(poll, 3000);
    return () => clearInterval(id);
  }, [ready]);

  // ── Auto-apply game profile when game / ready / enabled changes ──────────────
  useEffect(() => {
    if (!ready) return;

    if (!enabled) {
      if (perGame) setPerGame(false);
      autoAppliedRef.current = null;
      return;
    }

    if (!game) {
      const wasInGame = autoAppliedRef.current !== null;
      if (perGame) setPerGame(false);
      autoAppliedRef.current = null;
      (async () => {
        const s = await getSettings();
        const w = toW(s.spl), sw = toW(s.sppt), fw = toW(s.fppt);
        setSpl(w); setSppt(sw); setFppt(fw);
        setPreset(detectPreset(w, sw, fw));
        await applyTdp(s.spl, s.sppt, s.fppt, "");
        if (wasInGame) showStatus("Global settings restored.");
      })();
      return;
    }

    if (autoAppliedRef.current === game.appId) return;
    autoAppliedRef.current = game.appId;

    (async () => {
      const { exists, profile } = await getGameProfile(game.appId);
      if (exists && profile) {
        const w = toW(profile.spl), sw = toW(profile.sppt), fw = toW(profile.fppt);
        setPerGame(true);
        setSpl(w); setSppt(sw); setFppt(fw);
        setPreset(detectPreset(w, sw, fw));
        await applyTdp(profile.spl, profile.sppt, profile.fppt, game.appId);
        showStatus(`Auto-applied profile for ${game.name}.`);
      }
    })();
  }, [game?.appId, ready, enabled]);

  // ── Preset handler ────────────────────────────────────────────────────────────
  const handlePresetChange = async (key: PresetKey) => {
    setPreset(key);
    if (key === "custom") return;

    const vals = PRESETS[key];
    setSpl(vals.spl); setSppt(vals.sppt); setFppt(vals.fppt);
    setLoading(true);
    showStatus(null);
    const appId = (perGame && game) ? game.appId : "";
    try {
      const r = await applyTdp(toMw(vals.spl), toMw(vals.sppt), toMw(vals.fppt), appId);
      showStatus(r.success
        ? (appId ? `${PRESET_LABELS[key]} saved for ${game!.name}.` : `${PRESET_LABELS[key]} applied.`)
        : `Error: ${r.stderr || "unknown"}`
      );
    } catch (e: unknown) {
      showStatus(`Error: ${String(e)}`);
    }
    setLoading(false);
  };

  // ── Slider handlers (cascade clamp SPL <= SPPT <= FPPT) ──────────────────────
  const handleSplChange = (v: number) => {
    setSpl(v);
    if (sppt < v) { setSppt(v); if (fppt < v) setFppt(v); }
  };
  const handleSpptChange = (v: number) => {
    setSppt(v);
    if (spl > v) setSpl(v);
    if (fppt < v) setFppt(v);
  };
  const handleFpptChange = (v: number) => {
    setFppt(v);
    if (sppt > v) { setSppt(v); if (spl > v) setSpl(v); }
  };

  // ── Per-game toggle ───────────────────────────────────────────────────────────
  const handlePerGameToggle = async (checked: boolean) => {
    setPerGame(checked);
    if (!checked && game) {
      await deleteGameProfile(game.appId);
      const s = await getSettings();
      const w = toW(s.spl), sw = toW(s.sppt), fw = toW(s.fppt);
      setSpl(w); setSppt(sw); setFppt(fw);
      setPreset(detectPreset(w, sw, fw));
      await applyTdp(s.spl, s.sppt, s.fppt, "");
      showStatus("Switched to global settings.");
    } else if (checked && game) {
      const { exists, profile } = await getGameProfile(game.appId);
      if (exists && profile) {
        const w = toW(profile.spl), sw = toW(profile.sppt), fw = toW(profile.fppt);
        setSpl(w); setSppt(sw); setFppt(fw);
        setPreset(detectPreset(w, sw, fw));
        await applyTdp(profile.spl, profile.sppt, profile.fppt, game.appId);
        showStatus(`Profile applied for ${game.name}.`);
      }
    }
  };

  // ── Enable / disable plugin ───────────────────────────────────────────────────
  const handleEnabledToggle = async (checked: boolean) => {
    setEnabled(checked);
    showStatus(null);
    await setPluginEnabled(checked);
    if (!checked) {
      const r = await restoreDefaults();
      showStatus(r.success ? "Plugin disabled. Default TDP restored." : `Error: ${r.stderr || "unknown"}`);
    }
  };

  // ── Apply (Custom mode only) ──────────────────────────────────────────────────
  const apply = async () => {
    setLoading(true);
    showStatus(null);
    const appId = (perGame && game) ? game.appId : "";
    try {
      const r = await applyTdp(toMw(spl), toMw(sppt), toMw(fppt), appId);
      showStatus(r.success
        ? (appId ? `Profile saved for ${game!.name}.` : "Custom settings applied.")
        : `Error: ${r.stderr || "unknown"}`
      );
    } catch (e: unknown) {
      showStatus(`Error: ${String(e)}`);
    }
    setLoading(false);
  };

  // ── Render ────────────────────────────────────────────────────────────────────
  if (setupErr) return (
    <PanelSection title="Setup Error">
      <PanelSectionRow><Field label="Error" description={setupErr} /></PanelSectionRow>
    </PanelSection>
  );

  if (!ready) return (
    <PanelSection title="Downloading ryzenadj...">
      <PanelSectionRow><Spinner /></PanelSectionRow>
    </PanelSection>
  );

  return (
    <>
      <PanelSection title="LeGoTDP">
        <PanelSectionRow>
          <ToggleField
            label="Enable"
            description={enabled ? "TDP management active" : "Using system defaults"}
            checked={enabled}
            onChange={handleEnabledToggle}
          />
        </PanelSectionRow>
        {status && !enabled && (
          <PanelSectionRow>
            <Field label="Status" description={status} />
          </PanelSectionRow>
        )}
      </PanelSection>

      {enabled && <>
        <PanelSection title="Game Profile">
          <PanelSectionRow>
            <ToggleField
              label="Per Game Profile"
              description={game ? game.name : "No game running"}
              checked={perGame}
              disabled={!game}
              onChange={handlePerGameToggle}
            />
          </PanelSectionRow>
        </PanelSection>

        <LivePanel />

        <PanelSection title="Preset">
          {PRESET_ORDER.map(key => (
            <PanelSectionRow key={key}>
              <ButtonItem
                layout="below"
                disabled={preset === key || loading}
                onClick={() => handlePresetChange(key)}
              >
                {preset === key ? `> ${PRESET_LABELS[key]}` : PRESET_LABELS[key]}
              </ButtonItem>
            </PanelSectionRow>
          ))}
          {status && (
            <PanelSectionRow>
              <Field label="Status" description={status} />
            </PanelSectionRow>
          )}
        </PanelSection>

        {preset === "custom" && (
          <>
            <PanelSection title="TDP Limits">
              <PanelSectionRow>
                <SliderField
                  label={`SPL (Sustained) - ${spl} W`}
                  value={spl} min={LIMITS.spl.min} max={LIMITS.spl.max} step={1}
                  onChange={handleSplChange}
                  description="ppt_pl1_spl"
                />
              </PanelSectionRow>
              <PanelSectionRow>
                <SliderField
                  label={`SPPT (Slow) - ${sppt} W`}
                  value={sppt} min={LIMITS.sppt.min} max={LIMITS.sppt.max} step={1}
                  onChange={handleSpptChange}
                  description="ppt_pl2_sppt"
                />
              </PanelSectionRow>
              <PanelSectionRow>
                <SliderField
                  label={`FPPT (Fast) - ${fppt} W`}
                  value={fppt} min={LIMITS.fppt.min} max={LIMITS.fppt.max} step={1}
                  onChange={handleFpptChange}
                  description="ppt_pl3_fppt"
                />
              </PanelSectionRow>
            </PanelSection>

            <PanelSection title="Action">
              <PanelSectionRow>
                <ButtonItem layout="below" onClick={apply} disabled={loading}>
                  {loading ? "Applying..."
                    : perGame && game ? `Apply & Save for ${game.name}`
                    : "Apply TDP"}
                </ButtonItem>
              </PanelSectionRow>
              {status && (
                <PanelSectionRow>
                  <Field label="Status" description={status} />
                </PanelSectionRow>
              )}
            </PanelSection>
          </>
        )}
      </>}
    </>
  );
};

export default definePlugin(() => ({
  name:    "LeGoTDP",
  title:   <div className={staticClasses.Title}>LeGoTDP</div>,
  content: <Content />,
  icon:    <ChipIcon />,
  onDismount() {},
}));
