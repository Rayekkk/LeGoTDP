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

// ── Types ──────────────────────────────────────────────────────────────────────
interface Settings    { spl: number; sppt: number; fppt: number; enabled: boolean }
interface TdpResult   { success: boolean; stderr: string }
interface ParamLimits { min: number; max: number }
interface Limits      { spl: ParamLimits; sppt: ParamLimits; fppt: ParamLimits }
interface TdpValues   {
  spl_limit?:  number; spl_value?:  number;
  sppt_limit?: number; sppt_value?: number;
  fppt_limit?: number; fppt_value?: number;
}
interface TdpInfo      { success: boolean; values: TdpValues; error?: string }
interface GameProfile  { exists: boolean; profile: Settings }
interface RunningGame  { appId: string; name: string }
interface ReadyState   { ready: boolean; error: string | null; wmi: boolean }

// ── Backend callables ──────────────────────────────────────────────────────────
const isReady           = callable<[], ReadyState>("is_ready");
const getSettings       = callable<[], Settings>("get_settings");
const getLimits         = callable<[], Limits>("get_limits");
const applyTdp          = callable<[number, number, number, string], TdpResult>("apply_tdp");
const getTdpInfo        = callable<[], TdpInfo>("get_tdp_info");
const getGameProfile    = callable<[string], GameProfile>("get_game_profile");
const deleteGameProfile = callable<[string], void>("delete_game_profile");
const setPluginEnabled  = callable<[boolean], void>("set_plugin_enabled");
const restoreDefaults   = callable<[], TdpResult>("restore_defaults");

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
const LivePanel: FC<{ wmi: boolean }> = ({ wmi }) => {
  const [info, setInfo] = useState<TdpInfo | null>(null);

  useEffect(() => {
    let active = true;
    const refresh = async () => {
      try { if (active) setInfo(await getTdpInfo()); } catch (_) {}
    };
    refresh();
    const id = setInterval(refresh, 2000);
    return () => { active = false; clearInterval(id); };
  }, []);

  const v = info?.values ?? {};
  return (
    <PanelSection title={`Current TDP · ${wmi ? "WMI" : "ryzenadj"}`}>
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
              description={`Limit: ${fmt(v.spl_limit)}   •   Now: ${fmt(v.spl_value)}`} />
          </PanelSectionRow>
          <PanelSectionRow>
            <Field label="SPPT (Slow)"
              description={`Limit: ${fmt(v.sppt_limit)}   •   Now: ${fmt(v.sppt_value)}`} />
          </PanelSectionRow>
          <PanelSectionRow>
            <Field label="FPPT (Fast)"
              description={`Limit: ${fmt(v.fppt_limit)}   •   Now: ${fmt(v.fppt_value)}`} />
          </PanelSectionRow>
        </>
      )}
    </PanelSection>
  );
};

// ── Main content ───────────────────────────────────────────────────────────────
const DEFAULT_LIMITS: Limits = {
  spl: { min: 1, max: 54 }, sppt: { min: 1, max: 54 }, fppt: { min: 1, max: 54 },
};

const Content: FC = () => {
  const [ready,      setReady]      = useState(false);
  const [setupErr,   setSetupErr]   = useState<string | null>(null);
  const [limits,     setLimits]     = useState<Limits>(DEFAULT_LIMITS);
  const [wmiSource,  setWmiSource]  = useState(false);

  const [spl,        setSpl]        = useState(15);
  const [sppt,       setSppt]       = useState(15);
  const [fppt,       setFppt]       = useState(15);

  const [enabled,    setEnabled]    = useState(true);
  const [game,       setGame]       = useState<RunningGame | null>(null);
  const [perGame,    setPerGame]    = useState(false);

  const [status,     setStatus]     = useState<string | null>(null);
  const [loading,    setLoading]    = useState(false);

  const autoAppliedRef  = useRef<string | null>(null);
  const statusTimerRef  = useRef<ReturnType<typeof setTimeout> | null>(null);

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
          const [lim, s] = await Promise.all([getLimits(), getSettings()]);
          setLimits(lim);
          setSpl(toW(s.spl));
          setSppt(toW(s.sppt));
          setFppt(toW(s.fppt));
          setEnabled(s.enabled !== false);
          setWmiSource(r.wmi);
          setReady(true); // last - so game-change effect fires with correct enabled state
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
      // Plugin disabled - clean up state, toggle handler already called restoreDefaults()
      if (perGame) setPerGame(false);
      autoAppliedRef.current = null;
      return;
    }

    if (!game) {
      // No game running - restore global TDP
      const wasInGame = autoAppliedRef.current !== null;
      if (perGame) setPerGame(false);
      autoAppliedRef.current = null;
      (async () => {
        const s = await getSettings();
        setSpl(toW(s.spl)); setSppt(toW(s.sppt)); setFppt(toW(s.fppt));
        await applyTdp(s.spl, s.sppt, s.fppt, "");
        if (wasInGame) showStatus("Global settings restored.");
      })();
      return;
    }

    // Plugin enabled + game running
    if (autoAppliedRef.current === game.appId) return;
    autoAppliedRef.current = game.appId;

    (async () => {
      const { exists, profile } = await getGameProfile(game.appId);
      if (exists && profile) {
        setPerGame(true);
        setSpl(toW(profile.spl));
        setSppt(toW(profile.sppt));
        setFppt(toW(profile.fppt));
        await applyTdp(profile.spl, profile.sppt, profile.fppt, game.appId);
        showStatus(`Auto-applied profile for ${game.name}.`);
      }
    })();
  }, [game?.appId, ready, enabled]);

  // ── Slider handlers - cascade clamp to keep SPL ≤ SPPT ≤ FPPT ─────────────────
  const handleSplChange = (v: number) => {
    setSpl(v);
    if (sppt < v) {
      setSppt(v);
      if (fppt < v) setFppt(v);
    }
  };
  const handleSpptChange = (v: number) => {
    setSppt(v);
    if (spl > v) setSpl(v);
    if (fppt < v) setFppt(v);
  };
  const handleFpptChange = (v: number) => {
    setFppt(v);
    if (sppt > v) {
      setSppt(v);
      if (spl > v) setSpl(v);
    }
  };

  // ── Per-game switch toggled ───────────────────────────────────────────────────
  const handlePerGameToggle = async (checked: boolean) => {
    setPerGame(checked);
    if (!checked && game) {
      await deleteGameProfile(game.appId);
      const s = await getSettings();
      setSpl(toW(s.spl)); setSppt(toW(s.sppt)); setFppt(toW(s.fppt));
      await applyTdp(s.spl, s.sppt, s.fppt, "");
      showStatus("Switched to global settings.");
    } else if (checked && game) {
      const { exists, profile } = await getGameProfile(game.appId);
      if (exists && profile) {
        setSpl(toW(profile.spl)); setSppt(toW(profile.sppt)); setFppt(toW(profile.fppt));
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
    // When enabling: the effect (with enabled in deps) fires and applies the right TDP
  };

  // ── Apply ─────────────────────────────────────────────────────────────────────
  const apply = async () => {
    setLoading(true);
    showStatus(null);
    const appId = (perGame && game) ? game.appId : "";
    try {
      const r = await applyTdp(toMw(spl), toMw(sppt), toMw(fppt), appId);
      if (r.success) {
        showStatus(appId
          ? `Profile saved for ${game!.name}.`
          : "Global settings applied.");
      } else {
        showStatus(`Error: ${r.stderr || "unknown"}`);
      }
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
    <PanelSection title="Downloading ryzenadj…">
      <PanelSectionRow><Spinner /></PanelSectionRow>
    </PanelSection>
  );

  return (
    <>
      {/* Enable / disable plugin */}
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
      {/* Per-game toggle - always on top, disabled when no game is running */}
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

      <LivePanel wmi={wmiSource} />

      <PanelSection title="TDP Limits">
        <PanelSectionRow>
          <SliderField
            label={`SPL (Sustained) – ${spl} W`}
            value={spl} min={limits.spl.min} max={limits.spl.max} step={1}
            onChange={handleSplChange}
            description="--stapm-limit / ppt_pl1_spl"
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField
            label={`SPPT (Slow) – ${sppt} W`}
            value={sppt} min={limits.sppt.min} max={limits.sppt.max} step={1}
            onChange={handleSpptChange}
            description="--slow-limit / ppt_pl2_sppt"
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField
            label={`FPPT (Fast) – ${fppt} W`}
            value={fppt} min={limits.fppt.min} max={limits.fppt.max} step={1}
            onChange={handleFpptChange}
            description="--fast-limit / ppt_pl3_fppt"
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Action">
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={apply} disabled={loading}>
            {loading ? "Applying…"
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
