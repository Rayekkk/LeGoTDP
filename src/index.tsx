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

function profileLabel(spl: number, sppt: number, fppt: number, stored?: string): string {
  const customLabel = `Custom (${spl}/${sppt}/${fppt})`;
  if (stored !== undefined) {
    if (stored === "custom" || stored === "") return customLabel;
    return PRESET_LABELS[stored as PresetKey] ?? stored;
  }
  const key = detectPreset(spl, sppt, fppt);
  return key === "custom" ? customLabel : PRESET_LABELS[key];
}

const exceedsStd = (spl: number, sppt: number, fppt: number) =>
  spl > STD_LIMITS.spl.max || sppt > STD_LIMITS.sppt.max || fppt > STD_LIMITS.fppt.max;

function statusStyle(msg: string) {
  return msg.startsWith("Error:")
    ? { ...styles.warningBox, color: "#f87171", borderColor: "rgba(248,113,113,0.4)", background: "rgba(248,113,113,0.1)" }
    : { fontSize: "12px", color: "#4ade80" };
}

function makeCascadeHandlers(
  [a, setA]: [number, (v: number) => void],
  [b, setB]: [number, (v: number) => void],
  [c, setC]: [number, (v: number) => void],
) {
  return {
    onA: (v: number) => { setA(v); if (b < v) { setB(v); if (c < v) setC(v); } },
    onB: (v: number) => { setB(v); if (a > v) setA(v); if (c < v) setC(v); },
    onC: (v: number) => { setC(v); if (b > v) { setB(v); if (a > v) setA(v); } },
  };
}

// ── Types ──────────────────────────────────────────────────────────────────────
interface Settings   { spl: number; sppt: number; fppt: number; enabled: boolean; active_preset?: string }
interface TdpResult  { success: boolean; stderr: string }
interface TdpValues  {
  spl_limit?:  number; spl_value?:  number;
  sppt_limit?: number; sppt_value?: number;
  fppt_limit?: number; fppt_value?: number;
}
interface TdpInfo     { success: boolean; values: TdpValues; error?: string }
interface GameProfile {
  exists: boolean;
  profile: { spl: number; sppt: number; fppt: number; enabled: boolean; preset?: string };
  ac_separate: boolean;
  ac_profile: { spl: number; sppt: number; fppt: number; enabled: boolean; ac_preset?: string };
}
interface RunningGame { appId: string; name: string }
interface ReadyState  { ready: boolean; error: string | null }
interface UpdateInfo {
  current_version?: string;
  latest_version?: string;
  update_available?: boolean;
  download_url?: string;
  asset_name?: string;
  error?: string;
}

// ── Backend callables ──────────────────────────────────────────────────────────
const isReady           = callable<[], ReadyState>("is_ready");
const getSettings       = callable<[], Settings>("get_settings");
const applyTdp          = callable<[number, number, number, string, string], TdpResult>("apply_tdp");
const getTdpInfo        = callable<[], TdpInfo>("get_tdp_info");
const getGameProfile    = callable<[string], GameProfile>("get_game_profile");
const deleteGameProfile = callable<[string], void>("delete_game_profile");
const setPluginEnabled  = callable<[boolean], void>("set_plugin_enabled");
const restoreDefaults   = callable<[], TdpResult>("restore_defaults");
const setPanelActive    = callable<[boolean], void>("set_panel_active");
const checkUpdate      = callable<[], UpdateInfo>("check_update");
const performUpdate    = callable<[string, string], { success: boolean; path?: string; error?: string }>("perform_update");
const getPowerSource       = callable<[], { ac: boolean }>("get_power_source");
const setGameAcProfile     = callable<[string, number, number, number, boolean, string], { success: boolean; stderr?: string }>("set_game_ac_profile");
const getExtrasUnlocked    = callable<[], boolean>("get_extras_unlocked");
const setExtrasUnlockedCall = callable<[boolean], void>("set_extras_unlocked");

// ── Shared styles ──────────────────────────────────────────────────────────────
const styles = {
  valueTag: {
    fontSize: "13px", fontWeight: "bold", color: "#fff",
    background: "rgba(255,255,255,0.1)", borderRadius: "4px",
    padding: "1px 6px", fontFamily: "monospace",
  },
  profileTag: {
    fontSize: "11px", fontWeight: "bold", color: "#fff",
    background: "rgba(74,222,128,0.25)", border: "1px solid rgba(74,222,128,0.5)",
    borderRadius: "3px", padding: "0px 5px", fontFamily: "monospace",
  },
  warningBox: {
    background: "rgba(251,191,36,0.15)", border: "1px solid rgba(251,191,36,0.4)",
    borderRadius: "6px", padding: "8px 10px", fontSize: "11px",
    color: "rgba(251,191,36,0.9)", lineHeight: "1.5", marginTop: "4px",
  },
};

// ── Slider limits ──────────────────────────────────────────────────────────────
const STD_LIMITS = { spl: { min: 5, max: 35 }, sppt: { min: 5, max: 37 }, fppt: { min: 5, max: 45 } };
const MAX_LIMITS = { spl: { min: 5, max: 60 }, sppt: { min: 5, max: 60 }, fppt: { min: 5, max: 60 } };

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

// ── Update section ─────────────────────────────────────────────────────────────
const UpdateSection: FC = () => {
  const [checking,    setChecking]    = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [zipPath,     setZipPath]     = useState<string | null>(null);
  const [info,        setInfo]        = useState<UpdateInfo | null>(null);

  const handleCheck = async () => {
    setChecking(true);
    setInfo(null);
    setZipPath(null);
    try { setInfo(await checkUpdate()); }
    catch (e: unknown) { setInfo({ error: String(e) }); }
    setChecking(false);
  };

  const handleDownload = async () => {
    if (!info?.download_url || !info?.asset_name) return;
    setDownloading(true);
    try {
      const r = await performUpdate(info.download_url, info.asset_name);
      if (r.success && r.path) {
        setZipPath(r.path);
      } else {
        setInfo(prev => ({ ...(prev ?? {}), error: r.error ?? "Download failed" }));
      }
    } catch (e: unknown) {
      setInfo({ error: String(e) });
    }
    setDownloading(false);
  };

  return (
    <PanelSection title="Updates">
      <PanelSectionRow>
        <div style={{ fontSize: "12px", color: "rgba(255,255,255,0.6)" }}>
          Installed: <span style={styles.valueTag}>{info?.current_version ? `v${info.current_version}` : "—"}</span>
          {!info?.error && info?.latest_version && (
            <span> &nbsp; Latest: <span style={styles.valueTag}>v{info.latest_version}</span></span>
          )}
        </div>
      </PanelSectionRow>
      {info?.error && (
        <PanelSectionRow>
          <div style={{ ...styles.warningBox, color: "#f87171", borderColor: "rgba(248,113,113,0.4)", background: "rgba(248,113,113,0.1)" }}>
            {info.error}
          </div>
        </PanelSectionRow>
      )}
      {info && !info.error && !info.update_available && !zipPath && (
        <PanelSectionRow>
          <div style={{ fontSize: "12px", color: "#4ade80" }}>Up to date</div>
        </PanelSectionRow>
      )}
      {info?.update_available && !zipPath && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handleDownload} disabled={downloading}>
            {downloading ? "Downloading..." : `Download v${info.latest_version ?? "?"}`}
          </ButtonItem>
        </PanelSectionRow>
      )}
      {zipPath && (
        <PanelSectionRow>
          <div style={styles.warningBox}>
            Downloaded to <span style={{ fontFamily: "monospace", wordBreak: "break-all" }}>{zipPath}</span>
            <br /><br />
            To install: Decky → Developer → Uninstall LeGoTDP → Install Plugin from ZIP → select the file.
          </div>
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={handleCheck} disabled={checking || downloading}>
          {checking ? "Checking..." : "Check for updates"}
        </ButtonItem>
      </PanelSectionRow>
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

  const [enabled,       setEnabled]       = useState(true);
  const [game,          setGame]          = useState<RunningGame | null>(null);
  const [perGame,       setPerGame]       = useState(false);

  const [acOnline,      setAcOnline]      = useState(false);
  const [acSeparate,    setAcSeparate]    = useState(false);
  const [editingAc,     setEditingAc]     = useState(false);
  const [acSpl,         setAcSpl]         = useState(15);
  const [acSppt,        setAcSppt]        = useState(18);
  const [acFppt,        setAcFppt]        = useState(25);

  const [globalProfile, setGlobalProfile] = useState<{ spl: number; sppt: number; fppt: number; preset: string | undefined }>({ spl: 15, sppt: 15, fppt: 15, preset: undefined });
  const [extrasUnlocked, setExtrasUnlocked] = useState(false);

  const [savedPreset,   setSavedPreset]   = useState<string | undefined>(undefined);
  const [savedAcPreset, setSavedAcPreset] = useState<string | undefined>(undefined);

  const [status,   setStatus]   = useState<string | null>(null);
  const [loading,  setLoading]  = useState(false);

  const autoAppliedRef = useRef<string | null>(null);
  const statusTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => { if (statusTimerRef.current) clearTimeout(statusTimerRef.current); }, []);

  const limits = extrasUnlocked ? MAX_LIMITS : STD_LIMITS;

  const showStatus = (msg: string | null) => {
    if (statusTimerRef.current) clearTimeout(statusTimerRef.current);
    setStatus(msg);
    if (msg) statusTimerRef.current = setTimeout(() => setStatus(null), 3000);
  };

  const applyGameProfile = async (gp: GameProfile, appId: string, statusMsg: string) => {
    if (!gp.exists || !gp.profile) {
      if (gp.exists) showStatus("Error: Game profile data is missing or corrupt.");
      return;
    }
    const w = toW(gp.profile.spl), sw = toW(gp.profile.sppt), fw = toW(gp.profile.fppt);
    const acData = gp.ac_profile ?? { spl: gp.profile.spl, sppt: gp.profile.sppt, fppt: gp.profile.fppt, ac_preset: "" };
    const aw = toW(acData.spl), asw = toW(acData.sppt), afw = toW(acData.fppt);
    setPerGame(true);
    setSpl(w); setSppt(sw); setFppt(fw);
    setAcSpl(aw); setAcSppt(asw); setAcFppt(afw);
    setAcSeparate(gp.ac_separate);
    setEditingAc(false);
    const storedPreset = (gp.profile.preset as PresetKey | undefined) || undefined;
    setSavedPreset(storedPreset);
    setSavedAcPreset(gp.ac_separate ? (acData.ac_preset ?? "") : undefined);
    setPreset(storedPreset || detectPreset(w, sw, fw));
    try {
      await applyTdp(gp.profile.spl, gp.profile.sppt, gp.profile.fppt, appId, "");
    } catch (e: unknown) {
      showStatus(`Error applying TDP: ${String(e)}`);
      return;
    }
    showStatus(statusMsg);
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
          const [s, ps, eu] = await Promise.all([getSettings(), getPowerSource(), getExtrasUnlocked()]);
          if (!active) return;
          const w = toW(s.spl), sw = toW(s.sppt), fw = toW(s.fppt);
          setSpl(w); setSppt(sw); setFppt(fw);
          setGlobalProfile({ spl: w, sppt: sw, fppt: fw, preset: s.active_preset || undefined });
          setPreset((s.active_preset as PresetKey | undefined) || detectPreset(w, sw, fw));
          setEnabled(s.enabled !== false);
          setAcOnline(ps.ac);
          setExtrasUnlocked(eu);
          setReady(true);
        } else {
          if (active) setTimeout(check, 1000);
        }
      } catch (_) { if (active) setTimeout(check, 1000); }
    };
    check();
    return () => { active = false; };
  }, []);

  // ── Game detection + AC polling ───────────────────────────────────────────────
  useEffect(() => {
    if (!ready) return;
    const poll = async () => {
      setGame(detectRunningGame());
      try { const ps = await getPowerSource(); setAcOnline(ps.ac); } catch (_) {}
    };
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
      setSavedPreset(undefined);
      setSavedAcPreset(undefined);
      setAcSeparate(false);
      setEditingAc(false);
      (async () => {
        try {
          const s = await getSettings();
          const w = toW(s.spl), sw = toW(s.sppt), fw = toW(s.fppt);
          setSpl(w); setSppt(sw); setFppt(fw);
          setPreset((s.active_preset as PresetKey | undefined) || detectPreset(w, sw, fw));
          setGlobalProfile({ spl: w, sppt: sw, fppt: fw, preset: s.active_preset || undefined });
          if (wasInGame) {
            await applyTdp(s.spl, s.sppt, s.fppt, "", s.active_preset || "");
            showStatus("Global settings restored.");
          }
        } catch (e: unknown) {
          showStatus(`Error: ${String(e)}`);
        }
      })();
      return;
    }

    if (autoAppliedRef.current === game.appId) return;
    autoAppliedRef.current = game.appId;

    (async () => {
      try {
        const gp = await getGameProfile(game.appId);
        await applyGameProfile(gp, game.appId, `Auto-applied profile for ${game.name}.`);
      } catch (e: unknown) {
        autoAppliedRef.current = null;
        showStatus(`Error: ${String(e)}`);
      }
    })();
  }, [game?.appId, ready, enabled, perGame]);

  // ── Preset handler ────────────────────────────────────────────────────────────
  const handlePresetChange = async (key: PresetKey) => {
    const prevPreset = preset;
    const prevSpl = spl, prevSppt = sppt, prevFppt = fppt;
    const prevAcSpl = acSpl, prevAcSppt = acSppt, prevAcFppt = acFppt;
    setPreset(key);
    if (key === "custom") return;

    const vals = PRESETS[key];
    if (editingAc) {
      setAcSpl(vals.spl); setAcSppt(vals.sppt); setAcFppt(vals.fppt);
    } else {
      setSpl(vals.spl); setSppt(vals.sppt); setFppt(vals.fppt);
    }
    setLoading(true);
    showStatus(null);
    const appId = (perGame && game) ? game.appId : "";
    try {
      if (editingAc && appId) {
        const r = await setGameAcProfile(appId, toMw(vals.spl), toMw(vals.sppt), toMw(vals.fppt), acSeparate, key);
        if (r.success) {
          setSavedAcPreset(key);
        } else {
          setPreset(prevPreset);
          setAcSpl(prevAcSpl); setAcSppt(prevAcSppt); setAcFppt(prevAcFppt);
        }
        showStatus(r.success ? `AC: ${PRESET_LABELS[key]} saved for ${game!.name}.` : `Error: ${r.stderr || "unknown"}`);
      } else {
        const r = await applyTdp(toMw(vals.spl), toMw(vals.sppt), toMw(vals.fppt), appId, key);
        if (r.success) {
          if (!appId) { setGlobalProfile({ spl: vals.spl, sppt: vals.sppt, fppt: vals.fppt, preset: key }); }
          else setSavedPreset(key);
        } else {
          setPreset(prevPreset);
          setSpl(prevSpl); setSppt(prevSppt); setFppt(prevFppt);
        }
        showStatus(r.success
          ? (appId ? `${PRESET_LABELS[key]} saved for ${game!.name}.` : `${PRESET_LABELS[key]} applied.`)
          : `Error: ${r.stderr || "unknown"}`
        );
      }
    } catch (e: unknown) {
      setPreset(prevPreset);
      if (editingAc) { setAcSpl(prevAcSpl); setAcSppt(prevAcSppt); setAcFppt(prevAcFppt); }
      else { setSpl(prevSpl); setSppt(prevSppt); setFppt(prevFppt); }
      showStatus(`Error: ${String(e)}`);
    }
    setLoading(false);
  };

  // ── Slider handlers (cascade clamp SPL <= SPPT <= FPPT) ──────────────────────
  const { onA: handleSplChange, onB: handleSpptChange, onC: handleFpptChange } =
    makeCascadeHandlers([spl, setSpl], [sppt, setSppt], [fppt, setFppt]);
  const { onA: handleAcSplChange, onB: handleAcSpptChange, onC: handleAcFpptChange } =
    makeCascadeHandlers([acSpl, setAcSpl], [acSppt, setAcSppt], [acFppt, setAcFppt]);

  // ── Per-game toggle ───────────────────────────────────────────────────────────
  const handlePerGameToggle = async (checked: boolean) => {
    setPerGame(checked);
    if (!checked && game) {
      const prevAcSeparate = acSeparate, prevEditingAc = editingAc;
      const prevSavedPreset = savedPreset, prevSavedAcPreset = savedAcPreset;
      setAcSeparate(false);
      setEditingAc(false);
      setSavedPreset(undefined);
      setSavedAcPreset(undefined);
      autoAppliedRef.current = null;
      let profileDeleted = false;
      try {
        await deleteGameProfile(game.appId);
        profileDeleted = true;
        const s = await getSettings();
        const w = toW(s.spl), sw = toW(s.sppt), fw = toW(s.fppt);
        setSpl(w); setSppt(sw); setFppt(fw);
        setPreset((s.active_preset as PresetKey | undefined) || detectPreset(w, sw, fw));
        setGlobalProfile({ spl: w, sppt: sw, fppt: fw, preset: s.active_preset || undefined });
        await applyTdp(s.spl, s.sppt, s.fppt, "", s.active_preset || "");
        showStatus("Switched to global settings.");
      } catch (e: unknown) {
        if (!profileDeleted) {
          setPerGame(true);
          autoAppliedRef.current = game.appId;
          setAcSeparate(prevAcSeparate); setEditingAc(prevEditingAc);
          setSavedPreset(prevSavedPreset); setSavedAcPreset(prevSavedAcPreset);
        }
        showStatus(`Error: ${String(e)}`);
      }
    } else if (checked && game) {
      try {
        const gp = await getGameProfile(game.appId);
        if (!gp.exists) {
          setSavedPreset(undefined);
          setSavedAcPreset(undefined);
          showStatus(`No saved profile for ${game.name}. Use sliders to create one.`);
          autoAppliedRef.current = game.appId;
        } else {
          await applyGameProfile(gp, game.appId, `Profile applied for ${game.name}.`);
        }
      } catch (e: unknown) {
        setPerGame(false);
        showStatus(`Error: ${String(e)}`);
      }
    }
  };

  // ── Enable / disable plugin ───────────────────────────────────────────────────
  const handleEnabledToggle = async (checked: boolean) => {
    setEnabled(checked);
    showStatus(null);
    try {
      await setPluginEnabled(checked);
      if (!checked) {
        const r = await restoreDefaults();
        showStatus(r.success ? "Plugin disabled. Default TDP restored." : `Error: ${r.stderr || "unknown"}`);
      }
    } catch (e: unknown) {
      setEnabled(!checked);
      showStatus(`Error: ${String(e)}`);
    }
  };

  // ── AC separate toggle ────────────────────────────────────────────────────────
  const handleAcSeparateToggle = async (checked: boolean) => {
    if (!game) return;
    const prevSavedAcPreset = savedAcPreset;
    const prevEditingAc = editingAc;
    const prevAcSpl = acSpl, prevAcSppt = acSppt, prevAcFppt = acFppt;
    setAcSeparate(checked);
    let useSpl = acSpl, useSppt = acSppt, useFppt = acFppt;
    if (checked && savedAcPreset === undefined) {
      useSpl = spl; useSppt = sppt; useFppt = fppt;
      setAcSpl(spl); setAcSppt(sppt); setAcFppt(fppt);
    }
    if (!checked) {
      setEditingAc(false);
      setSavedAcPreset(undefined);
    }
    try {
      await setGameAcProfile(game.appId, toMw(useSpl), toMw(useSppt), toMw(useFppt), checked, "");
    } catch (e: unknown) {
      setAcSeparate(!checked);
      setSavedAcPreset(prevSavedAcPreset);
      setEditingAc(prevEditingAc);
      setAcSpl(prevAcSpl); setAcSppt(prevAcSppt); setAcFppt(prevAcFppt);
      showStatus(`Error: ${String(e)}`);
    }
  };

  // ── Extras: unlock extended TDP range ────────────────────────────────────────
  const handleExtrasUnlockedToggle = async (checked: boolean) => {
    setExtrasUnlocked(checked);
    try {
      await setExtrasUnlockedCall(checked);
    } catch (e: unknown) {
      setExtrasUnlocked(!checked);
      showStatus(`Error: ${String(e)}`);
      return;
    }
    if (!checked) {
      const newSpl  = Math.min(spl,   35), newSppt  = Math.min(sppt,   37), newFppt  = Math.min(fppt,   45);
      const newAcSpl = Math.min(acSpl, 35), newAcSppt = Math.min(acSppt, 37), newAcFppt = Math.min(acFppt, 45);
      setSpl(newSpl); setSppt(newSppt); setFppt(newFppt);
      setAcSpl(newAcSpl); setAcSppt(newAcSppt); setAcFppt(newAcFppt);
      const appId = (perGame && game) ? game.appId : "";
      try {
        if (spl !== newSpl || sppt !== newSppt || fppt !== newFppt) {
          const r = await applyTdp(toMw(newSpl), toMw(newSppt), toMw(newFppt), appId, "custom");
          setPreset("custom");
          if (r.success) {
            if (!appId) { setGlobalProfile({ spl: newSpl, sppt: newSppt, fppt: newFppt, preset: "custom" }); }
            else setSavedPreset("custom");
          }
        }
        if (acSeparate && appId && (acSpl !== newAcSpl || acSppt !== newAcSppt || acFppt !== newAcFppt)) {
          const r = await setGameAcProfile(appId, toMw(newAcSpl), toMw(newAcSppt), toMw(newAcFppt), acSeparate, "custom");
          if (r.success) setSavedAcPreset("custom");
        }
      } catch (e: unknown) {
        showStatus(`Error: ${String(e)}`);
      }
    }
  };

  // ── Apply (Custom mode only) ──────────────────────────────────────────────────
  const apply = async () => {
    setLoading(true);
    showStatus(null);
    const appId = (perGame && game) ? game.appId : "";
    try {
      if (editingAc && appId) {
        const r = await setGameAcProfile(appId, toMw(acSpl), toMw(acSppt), toMw(acFppt), acSeparate, "custom");
        if (r.success) setSavedAcPreset("custom");
        showStatus(r.success ? `AC profile saved for ${game!.name}.` : `Error: ${r.stderr || "unknown"}`);
      } else {
        const r = await applyTdp(toMw(spl), toMw(sppt), toMw(fppt), appId, "custom");
        if (r.success) {
          if (!appId) { setGlobalProfile({ spl, sppt, fppt, preset: "custom" }); }
          else setSavedPreset("custom");
        }
        showStatus(r.success
          ? (appId ? `Profile saved for ${game!.name}.` : "Custom settings applied.")
          : `Error: ${r.stderr || "unknown"}`
        );
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
    <PanelSection title="Initializing...">
      <PanelSectionRow><Spinner /></PanelSectionRow>
    </PanelSection>
  );

  return (
    <>
      <PanelSection title="LeGoTDP">
        <PanelSectionRow>
          <ToggleField
            label="Enable"
            description={
              enabled ? (
                <span>
                  <span style={{ fontSize: "11px", color: "rgba(255,255,255,0.5)" }}>Global Profile: </span>
                  <span style={styles.profileTag}>{profileLabel(globalProfile.spl, globalProfile.sppt, globalProfile.fppt, globalProfile.preset)}</span>
                  {!extrasUnlocked && exceedsStd(globalProfile.spl, globalProfile.sppt, globalProfile.fppt) && (
                    <span style={{ fontSize: "11px", color: "rgba(251,191,36,0.9)" }}> ⚠ exceeds std limits</span>
                  )}
                </span>
              ) : "Using system defaults"
            }
            checked={enabled}
            onChange={handleEnabledToggle}
          />
        </PanelSectionRow>
        {status && !enabled && (
          <PanelSectionRow>
            <div style={statusStyle(status)}>
              {status}
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>

      {enabled && <>
        <PanelSection title="Game Profile">
          <PanelSectionRow>
            <ToggleField
              label="Per Game Profile"
              description={
                game ? (
                  perGame ? (
                    <span style={{ display: "flex", flexDirection: "column", gap: "3px" }}>
                      <span>{game.name}</span>
                      <span style={{ display: "flex", flexDirection: "column", gap: "1px" }}>
                        <span>
                          <span style={{ fontSize: "11px", color: "rgba(255,255,255,0.5)" }}>Battery: </span>
                          <span style={styles.profileTag}>{profileLabel(spl, sppt, fppt, savedPreset)}</span>
                          {!extrasUnlocked && exceedsStd(spl, sppt, fppt) && (
                            <span style={{ fontSize: "11px", color: "rgba(251,191,36,0.9)" }}> ⚠</span>
                          )}
                        </span>
                        {acSeparate && (
                          <span>
                            <span style={{ fontSize: "11px", color: "rgba(255,255,255,0.5)" }}>AC: </span>
                            <span style={styles.profileTag}>{profileLabel(acSpl, acSppt, acFppt, savedAcPreset)}</span>
                            {!extrasUnlocked && exceedsStd(acSpl, acSppt, acFppt) && (
                              <span style={{ fontSize: "11px", color: "rgba(251,191,36,0.9)" }}> ⚠</span>
                            )}
                          </span>
                        )}
                      </span>
                    </span>
                  ) : game.name
                ) : "No game running"
              }
              checked={perGame}
              disabled={!game}
              onChange={handlePerGameToggle}
            />
          </PanelSectionRow>
          {perGame && (
            <PanelSectionRow>
              <ToggleField
                label="Separate AC Profile"
                description={acSeparate ? "AC and battery have independent TDP settings" : "Enable to set a separate TDP when charging"}
                checked={acSeparate}
                onChange={handleAcSeparateToggle}
              />
            </PanelSectionRow>
          )}
          {perGame && acSeparate && (
            <>
              <PanelSectionRow>
                <ButtonItem layout="below" onClick={() => setEditingAc(false)} disabled={!editingAc}>
                  {!editingAc ? "> Battery profile" : "Battery profile"}
                </ButtonItem>
              </PanelSectionRow>
              <PanelSectionRow>
                <ButtonItem layout="below" onClick={() => setEditingAc(true)} disabled={editingAc}>
                  {editingAc ? "> AC profile" : "AC profile"}
                </ButtonItem>
              </PanelSectionRow>
              <PanelSectionRow>
                <div style={{ fontSize: "11px", fontWeight: "bold", color: acOnline ? "#4ade80" : "#fbbf24" }}>
                  {acOnline ? "Charging (AC)" : "On battery"}
                </div>
              </PanelSectionRow>
            </>
          )}
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
          {status && preset !== "custom" && (
            <PanelSectionRow>
              <div style={statusStyle(status)}>
                {status}
              </div>
            </PanelSectionRow>
          )}
        </PanelSection>

        {preset === "custom" && (
          <>
            <PanelSection title={editingAc ? "TDP Limits (AC)" : "TDP Limits"}>
              <PanelSectionRow>
                <SliderField
                  label={`SPL (Sustained) - ${editingAc ? acSpl : spl} W`}
                  value={editingAc ? acSpl : spl} min={limits.spl.min} max={limits.spl.max} step={1}
                  onChange={editingAc ? handleAcSplChange : handleSplChange}
                  description="ppt_pl1_spl"
                />
              </PanelSectionRow>
              <PanelSectionRow>
                <SliderField
                  label={`SPPT (Slow) - ${editingAc ? acSppt : sppt} W`}
                  value={editingAc ? acSppt : sppt} min={limits.sppt.min} max={limits.sppt.max} step={1}
                  onChange={editingAc ? handleAcSpptChange : handleSpptChange}
                  description="ppt_pl2_sppt"
                />
              </PanelSectionRow>
              <PanelSectionRow>
                <SliderField
                  label={`FPPT (Fast) - ${editingAc ? acFppt : fppt} W`}
                  value={editingAc ? acFppt : fppt} min={limits.fppt.min} max={limits.fppt.max} step={1}
                  onChange={editingAc ? handleAcFpptChange : handleFpptChange}
                  description="ppt_pl3_fppt"
                />
              </PanelSectionRow>
            </PanelSection>

            <PanelSection title="Action">
              <PanelSectionRow>
                <ButtonItem layout="below" onClick={apply} disabled={loading}>
                  {loading ? "Applying..."
                    : editingAc && game ? `Save AC for ${game.name}`
                    : perGame && game ? `Apply & Save for ${game.name}`
                    : "Apply TDP"}
                </ButtonItem>
              </PanelSectionRow>
              {status && (
                <PanelSectionRow>
                  <div style={statusStyle(status)}>
                    {status}
                  </div>
                </PanelSectionRow>
              )}
            </PanelSection>
          </>
        )}
      </>}

      <UpdateSection />

      <PanelSection title="Extras">
        <PanelSectionRow>
          <div style={styles.warningBox}>
            These settings are for advanced users only and are NOT recommended.
            Changes are made at your own risk — they override the manufacturer's TDP safety limits.
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Unlock Custom TDP to 60 W"
            description={extrasUnlocked ? "Custom slider range extended to 60 W" : "Enable to allow Custom sliders up to 60 W"}
            checked={extrasUnlocked}
            onChange={handleExtrasUnlockedToggle}
          />
        </PanelSectionRow>
      </PanelSection>
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
