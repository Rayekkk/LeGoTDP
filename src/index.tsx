import { callable, definePlugin, toaster, useQuickAccessVisible } from "@decky/api";
import {
  ButtonItem,
  Field,
  PanelSection,
  PanelSectionRow,
  Router,
  SliderField,
  Spinner,
  staticClasses,
  ToggleField,
} from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";

// ── Helpers ────────────────────────────────────────────────────────────────────
const toMw  = (w: number)  => w * 1000;
const toW   = (mw: number) => Math.round(mw / 1000);
const fmt   = (v?: number) => v != null ? `${v.toFixed(1)} W` : "-";
const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);

// ── Tuning model ───────────────────────────────────────────────────────────────
// SPL is the actual TDP dial. SPPT and FPPT are expressed as headroom *above* SPL
// rather than absolute watts, so raising the TDP carries the burst limits with it.
interface Tuning { spl: number; spptOff: number; fpptOff: number }
interface Caps   { spl: number; sppt: number; fppt: number }

const OFFSET_MAX = { sppt: 10, fppt: 15 };

// Used until get_caps() answers; the backend reports the firmware's real ceilings
// and falls back to these same numbers (FALLBACK_STD_W in main.py) if it cannot.
const FALLBACK_STD: Caps = { spl: 35, sppt: 37, fppt: 45 };
const FALLBACK_MAX: Caps = { spl: 50, sppt: 50, fppt: 50 };
const FALLBACK_MIN = 5;

/** Headroom still available above the current SPL, per parameter. */
const offsetMax = (spl: number, caps: Caps) => ({
  sppt: Math.max(0, Math.min(OFFSET_MAX.sppt, caps.sppt - spl)),
  fppt: Math.max(0, Math.min(OFFSET_MAX.fppt, caps.fppt - spl)),
});

/** Force a tuning back inside the ceilings, keeping SPPT <= FPPT. */
function normalise(t: Tuning, caps: Caps, minW: number): Tuning {
  const spl = clamp(t.spl, minW, caps.spl);
  const max = offsetMax(spl, caps);
  const spptOff = clamp(t.spptOff, 0, max.sppt);
  const fpptOff = Math.max(clamp(t.fpptOff, 0, max.fppt), spptOff);
  return { spl, spptOff, fpptOff };
}

const absolute = (t: Tuning) => ({
  spl: t.spl, sppt: t.spl + t.spptOff, fppt: t.spl + t.fpptOff,
});

const fromAbsolute = (spl: number, sppt: number, fppt: number): Tuning => ({
  spl, spptOff: Math.max(0, sppt - spl), fpptOff: Math.max(0, fppt - spl),
});

const sameTuning = (a: Tuning, b: Tuning) =>
  a.spl === b.spl && a.spptOff === b.spptOff && a.fpptOff === b.fpptOff;

/** Slider handlers implementing the coupling rules between the three limits. */
function makeTuningHandlers(t: Tuning, set: (next: Tuning) => void, caps: Caps, minW: number) {
  return {
    // Moving SPL re-clamps both offsets: at the ceiling there is no headroom left.
    onSpl: (v: number) => set(normalise({ ...t, spl: v }, caps, minW)),
    onSppt: (v: number) => {
      const spptOff = clamp(v, 0, offsetMax(t.spl, caps).sppt);
      // Pushing SPPT up drags FPPT along so SPPT never overtakes it.
      set({ ...t, spptOff, fpptOff: Math.max(t.fpptOff, spptOff) });
    },
    onFppt: (v: number) => {
      const fpptOff = clamp(v, 0, offsetMax(t.spl, caps).fppt);
      // Pulling FPPT below SPPT drags SPPT down to meet it.
      set({ ...t, fpptOff, spptOff: Math.min(t.spptOff, fpptOff) });
    },
  };
}

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
  const customLabel = `Custom (${spl} +${sppt - spl}/+${fppt - spl})`;
  if (stored !== undefined) {
    if (stored === "custom" || stored === "") return customLabel;
    return PRESET_LABELS[stored as PresetKey] ?? stored;
  }
  const key = detectPreset(spl, sppt, fppt);
  return key === "custom" ? customLabel : PRESET_LABELS[key];
}

const exceedsCaps = (spl: number, sppt: number, fppt: number, caps: Caps) =>
  spl > caps.spl || sppt > caps.sppt || fppt > caps.fppt;

function statusStyle(msg: string) {
  return msg.startsWith("Error") ? styles.errorBox : { fontSize: "12px", color: OK_COLOR };
}

// ── Types ──────────────────────────────────────────────────────────────────────
interface Settings   { spl: number; sppt: number; fppt: number; enabled: boolean; active_preset?: string }
interface TdpResult  { success: boolean; stderr: string }
interface TdpValues  {
  spl_limit?:  number;
  sppt_limit?: number;
  fppt_limit?: number;
  package_draw?: number;
  source?: string;
}
interface TdpInfo     { success: boolean; values: TdpValues; error?: string }
interface GameProfile {
  exists: boolean;
  profile: { spl: number; sppt: number; fppt: number; preset?: string };
  ac_separate: boolean;
  ac_profile: { spl: number; sppt: number; fppt: number; ac_preset?: string };
}
interface CapsInfo   { min: number; std: Caps; max: Caps; wmi: boolean }
interface RunningGame { appId: string; name: string }
interface ReadyState  { ready: boolean; error: string }
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
const getVersion        = callable<[], { version: string }>("get_version");
const getSettings       = callable<[], Settings>("get_settings");
const getCaps           = callable<[], CapsInfo>("get_caps");
const applyTdp          = callable<[number, number, number, string, string], TdpResult>("apply_tdp");
const getTdpInfo        = callable<[], TdpInfo>("get_tdp_info");
const getGameProfile    = callable<[string], GameProfile>("get_game_profile");
const deleteGameProfile = callable<[string], void>("delete_game_profile");
const setPluginEnabled  = callable<[boolean], void>("set_plugin_enabled");
const restoreDefaults   = callable<[], TdpResult>("restore_defaults");
const setPanelActive    = callable<[boolean], void>("set_panel_active");
const setActiveApp      = callable<[string], void>("set_active_app");
const getPowerSource    = callable<[], { ac: boolean }>("get_power_source");
const setGameAcProfile  = callable<[string, number, number, number, boolean, string], { success: boolean; stderr?: string }>("set_game_ac_profile");
const getExtrasUnlocked = callable<[], boolean>("get_extras_unlocked");
const setExtrasUnlockedCall = callable<[boolean], void>("set_extras_unlocked");
const checkForUpdates   = callable<[], UpdateInfo>("check_for_updates");
const performUpdate     = callable<[string, string], { success: boolean; path?: string; error?: string }>("perform_update");

// ── Toasts ─────────────────────────────────────────────────────────────────────

const notify = (title: string, body: string) => {
  try {
    toaster.toast({ title, body, duration: 4000 });
  } catch {
    console.error(`[legotdp] ${title}: ${body}`);
  }
};

const notifyFailure = (title: string, err: unknown) => {
  const body = err instanceof Error ? err.message : String(err ?? "Unknown error");
  console.error(`[legotdp] ${title}`, err);
  notify(title, body);
};

// ── Styles - Steam theme variables with hardcoded fallbacks ────────────────────

const OK_COLOR = "var(--gpColor-Green, #4ade80)";
const BAD_COLOR = "var(--gpColor-Red, #f87171)";
const WARN_COLOR = "var(--gpColor-Yellow, #fbbf24)";
const DIM_COLOR = "var(--gpColor-TextMuted, rgba(255,255,255,0.5))";

const styles = {
  valueTag: {
    fontSize: "13px",
    fontWeight: "bold",
    color: "var(--gpColor-White, #fff)",
    background: "rgba(255,255,255,0.1)",
    borderRadius: "4px",
    padding: "1px 6px",
    fontFamily: "monospace",
  },
  profileTag: {
    fontSize: "11px",
    fontWeight: "bold",
    color: "var(--gpColor-White, #fff)",
    background: "rgba(74,222,128,0.25)",
    border: "1px solid rgba(74,222,128,0.5)",
    borderRadius: "3px",
    padding: "0px 5px",
    fontFamily: "monospace",
  },
  infoBox: {
    background: "rgba(251,191,36,0.15)",
    border: "1px solid rgba(251,191,36,0.4)",
    borderRadius: "6px",
    padding: "8px 10px",
    fontSize: "11px",
    color: WARN_COLOR,
    lineHeight: "1.5",
    marginTop: "4px",
  },
  errorBox: {
    background: "rgba(248,113,113,0.1)",
    border: "1px solid rgba(248,113,113,0.4)",
    borderRadius: "6px",
    padding: "8px 10px",
    fontSize: "11px",
    color: BAD_COLOR,
    lineHeight: "1.5",
    marginTop: "4px",
  },
};

// ── Running app watcher ────────────────────────────────────────────────────────

type GameListener = (game: RunningGame | null) => void;

// The backend trusts a frontend-reported appid for 12 seconds. Refresh at half
// that, so one dropped call is not enough to make it fall back to the /proc scan.
const PUSH_INTERVAL_MS = 6000;

/**
 * Tracks the foreground game and tells the backend about it, so the backend's
 * enforce loop applies the right per-game profile even for titles its
 * /proc scan cannot see through pressure-vessel/gamescope.
 *
 * Started at plugin load rather than from the panel: the enforce loop runs
 * whether or not the Quick Access Menu is open, and it is exactly the
 * closed-panel case where the /proc fallback used to guess wrong.
 *
 * Unlike LeGo-Vibe-Control's copy, this pushes on every tick instead of only
 * on a change. The backend trusts a frontend-reported appid for 12 seconds and
 * falls back to the /proc scan once it goes stale, so the value has to be kept
 * fresh, not merely correct at the moment it last changed.
 */
class AppWatcher {
  private static listeners: GameListener[] = [];
  private static current: RunningGame | null = null;
  private static timer: ReturnType<typeof setInterval> | undefined;
  private static unsubs: Array<() => void> = [];
  private static started = false;
  private static busy = false;
  private static lastPush = 0;

  static activeGame(): RunningGame | null {
    try {
      const app = (Router as any)?.MainRunningApp;
      if (!app?.appid) return null;
      return { appId: String(app.appid), name: app.display_name ?? String(app.appid) };
    } catch {
      return null;
    }
  }

  static currentGame(): RunningGame | null {
    return this.current;
  }

  static listen(fn: GameListener): () => void {
    this.listeners.push(fn);
    return () => {
      this.listeners = this.listeners.filter((f) => f !== fn);
    };
  }

  static start() {
    if (this.started) return;
    this.started = true;
    this.current = this.activeGame();

    const steam = (window as any).SteamClient;

    try {
      const reg = steam?.GameSessions?.RegisterForAppLifetimeNotifications?.(() => {
        // Router.MainRunningApp lags the notification slightly.
        setTimeout(() => void this.check(), 300);
      });
      if (reg?.unregister) this.unsubs.push(() => reg.unregister());
    } catch (e) {
      console.warn("[legotdp] app lifetime notifications unavailable", e);
    }

    this.timer = setInterval(() => void this.check(), 2000);
    void this.check();
  }

  static stop() {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
    for (const off of this.unsubs) {
      try {
        off();
      } catch {
        /* the subscription may already be gone */
      }
    }
    this.unsubs = [];
    this.listeners = [];
    this.current = null;
    this.started = false;
    this.lastPush = 0;
  }

  private static async check() {
    if (this.busy) return;
    const game = this.activeGame();
    const changed = game?.appId !== this.current?.appId;
    this.current = game;

    // Tick often so a change is noticed quickly, but only send when there is
    // something to say or the backend's 12 s freshness window is running out.
    // This runs for the whole session, including mid-game with the panel shut.
    const now = Date.now();
    if (changed || now - this.lastPush >= PUSH_INTERVAL_MS) {
      this.busy = true;
      try {
        await setActiveApp(game?.appId ?? "");
        this.lastPush = now;
      } catch (e) {
        console.error("[legotdp] setActiveApp failed", e);
      } finally {
        this.busy = false;
      }
    }

    if (changed) this.listeners.forEach((fn) => fn(game));
  }
}

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
  const visible = useQuickAccessVisible();

  // Gated on visibility, not just on mount: the panel stays mounted while the
  // Quick Access Menu is on another tab, and refreshing it there costs a RAPL
  // read and an RPC round trip every two seconds for nobody to look at.
  useEffect(() => {
    if (!visible) return;
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
  }, [visible]);

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
            <Field label="SPL  (Sustained)" description={`Limit: ${fmt(v.spl_limit)}`} />
          </PanelSectionRow>
          <PanelSectionRow>
            <Field label="SPPT (Slow)" description={`Limit: ${fmt(v.sppt_limit)}`} />
          </PanelSectionRow>
          <PanelSectionRow>
            <Field label="FPPT (Fast)" description={`Limit: ${fmt(v.fppt_limit)}`} />
          </PanelSectionRow>
          <PanelSectionRow>
            <Field
              label="Package draw"
              description={`${fmt(v.package_draw)}${v.source ? `   -   set via ${v.source}` : ""}`}
            />
          </PanelSectionRow>
        </>
      )}
    </PanelSection>
  );
};

// ── Update section ─────────────────────────────────────────────────────────────
const UpdateSection: FC = () => {
  const [updateInfo,   setUpdateInfo]   = useState<UpdateInfo | null>(null);
  const [checking,     setChecking]     = useState(false);
  const [downloading,  setDownloading]  = useState(false);
  const [downloadPath, setDownloadPath] = useState<string | null>(null);
  const [version,      setVersion]      = useState("");

  // Read straight from the manifest so the installed version is on screen
  // before anyone presses the button, rather than only after a network call.
  useEffect(() => {
    let active = true;
    getVersion()
      .then((v) => { if (active) setVersion(v.version ?? ""); })
      .catch(() => undefined);
    return () => { active = false; };
  }, []);

  const handleCheckUpdate = useCallback(async () => {
    setChecking(true);
    setUpdateInfo(null);
    setDownloadPath(null);
    try {
      setUpdateInfo(await checkForUpdates());
    } catch (e) {
      notifyFailure("Update check failed", e);
      setUpdateInfo({ error: e instanceof Error ? e.message : String(e) });
    } finally {
      setChecking(false);
    }
  }, []);

  const handleDownloadUpdate = useCallback(async () => {
    if (!updateInfo?.download_url || !updateInfo?.asset_name) return;
    setDownloading(true);
    try {
      const res = await performUpdate(updateInfo.download_url, updateInfo.asset_name);
      if (res.success && res.path) setDownloadPath(res.path);
      else {
        setUpdateInfo({ ...updateInfo, error: res.error });
        notify("Download failed", res.error ?? "Unknown error");
      }
    } catch (e) {
      notifyFailure("Download failed", e);
    } finally {
      setDownloading(false);
    }
  }, [updateInfo]);

  return (
    <PanelSection title="Updates">
      <PanelSectionRow>
        <div style={{ fontSize: "12px", color: DIM_COLOR }}>
          Installed:{" "}
          <span style={styles.valueTag}>v{updateInfo?.current_version ?? version ?? "?"}</span>
          {updateInfo?.latest_version && !updateInfo.error && (
            <span>
              {" "}
              Latest: <span style={styles.valueTag}>v{updateInfo.latest_version}</span>
            </span>
          )}
        </div>
      </PanelSectionRow>
      {updateInfo?.error && (
        <PanelSectionRow>
          <div style={styles.errorBox}>{updateInfo.error}</div>
        </PanelSectionRow>
      )}
      {updateInfo && !updateInfo.error && !updateInfo.update_available && !downloadPath && (
        <PanelSectionRow>
          <div style={{ fontSize: "12px", color: OK_COLOR }}>Up to date</div>
        </PanelSectionRow>
      )}
      {updateInfo?.update_available && !downloadPath && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handleDownloadUpdate} disabled={downloading}>
            {downloading ? "Downloading..." : `Download v${updateInfo.latest_version}`}
          </ButtonItem>
        </PanelSectionRow>
      )}
      {downloadPath && (
        <PanelSectionRow>
          <div style={styles.infoBox}>
            Downloaded to{" "}
            <span style={{ fontFamily: "monospace", wordBreak: "break-all" }}>
              {downloadPath}
            </span>
            <br />
            <br />
            To install: Decky - Developer - Uninstall LeGoTDP - Install Plugin from ZIP -
            select the file. Your settings and per-game profiles are kept.
          </div>
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={handleCheckUpdate} disabled={checking || downloading}>
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

  const [tuning,   setTuning]   = useState<Tuning>(fromAbsolute(15, 18, 25));
  const [acTuning, setAcTuning] = useState<Tuning>(fromAbsolute(15, 18, 25));
  const [preset,   setPreset]   = useState<PresetKey>("balanced");

  const [stdCaps, setStdCaps] = useState<Caps>(FALLBACK_STD);
  const [maxCaps, setMaxCaps] = useState<Caps>(FALLBACK_MAX);
  const [minW,    setMinW]    = useState(FALLBACK_MIN);

  const [enabled,       setEnabled]       = useState(true);
  const [game,          setGame]          = useState<RunningGame | null>(null);
  const [perGame,       setPerGame]       = useState(false);

  const [acOnline,      setAcOnline]      = useState(false);
  const [acSeparate,    setAcSeparate]    = useState(false);
  const [editingAc,     setEditingAc]     = useState(false);

  const [globalProfile, setGlobalProfile] = useState<{ spl: number; sppt: number; fppt: number; preset: string | undefined }>({ spl: 15, sppt: 18, fppt: 25, preset: undefined });
  const [extrasUnlocked, setExtrasUnlocked] = useState(false);

  const [savedPreset,   setSavedPreset]   = useState<string | undefined>(undefined);
  const [savedAcPreset, setSavedAcPreset] = useState<string | undefined>(undefined);

  const [status,   setStatus]   = useState<string | null>(null);
  const [loading,  setLoading]  = useState(false);

  const visible = useQuickAccessVisible();

  const autoAppliedRef = useRef<string | null>(null);
  const noGameSyncedRef = useRef(false);
  const statusTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => { if (statusTimerRef.current) clearTimeout(statusTimerRef.current); }, []);

  const caps    = extrasUnlocked ? maxCaps : stdCaps;
  const active  = editingAc ? acTuning : tuning;
  const setActive = editingAc ? setAcTuning : setTuning;
  const handlers = makeTuningHandlers(active, setActive, caps, minW);
  const om      = offsetMax(active.spl, caps);

  const showStatus = (msg: string | null) => {
    if (statusTimerRef.current) clearTimeout(statusTimerRef.current);
    setStatus(msg);
    if (msg) statusTimerRef.current = setTimeout(() => setStatus(null), 3000);
  };

  /** Inline status plus a toast: the inline line clears after three seconds and
   *  lives in a section the user may not be looking at. */
  const showError = (title: string, e: unknown) => {
    notifyFailure(title, e);
    showStatus(`Error: ${e instanceof Error ? e.message : String(e)}`);
  };

  const applyGameProfile = async (gp: GameProfile, appId: string, statusMsg: string) => {
    if (!gp.exists || !gp.profile) {
      if (gp.exists) showStatus("Error: Game profile data is missing or corrupt.");
      return;
    }
    const p  = gp.profile;
    const t  = fromAbsolute(toW(p.spl), toW(p.sppt), toW(p.fppt));
    const ac = gp.ac_profile ?? { spl: p.spl, sppt: p.sppt, fppt: p.fppt, ac_preset: "" };
    const at = fromAbsolute(toW(ac.spl), toW(ac.sppt), toW(ac.fppt));
    setPerGame(true);
    setTuning(t);
    setAcTuning(at);
    setAcSeparate(gp.ac_separate);
    setEditingAc(false);
    const storedPreset = (p.preset as PresetKey | undefined) || undefined;
    setSavedPreset(storedPreset);
    setSavedAcPreset(gp.ac_separate ? (ac.ac_preset ?? "") : undefined);
    setPreset(storedPreset || detectPreset(toW(p.spl), toW(p.sppt), toW(p.fppt)));
    try {
      await applyTdp(p.spl, p.sppt, p.fppt, appId, "");
    } catch (e: unknown) {
      showError("Could not apply TDP", e);
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
          const [s, ps, eu, c] = await Promise.all([
            getSettings(), getPowerSource(), getExtrasUnlocked(), getCaps(),
          ]);
          if (!active) return;
          if (c?.std && c?.max) { setStdCaps(c.std); setMaxCaps(c.max); setMinW(c.min); }
          const w = toW(s.spl), sw = toW(s.sppt), fw = toW(s.fppt);
          setTuning(fromAbsolute(w, sw, fw));
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

  // ── Game detection ────────────────────────────────────────────────────────────
  // AppWatcher owns this and runs for the whole session, so the backend keeps
  // getting the authoritative appid while the panel is closed. Here we only
  // adopt what it reports.
  useEffect(() => {
    setGame(AppWatcher.currentGame());
    return AppWatcher.listen(setGame);
  }, []);

  // ── AC polling ────────────────────────────────────────────────────────────────
  // Only while the panel is on screen. The backend's own enforce loop reacts to
  // an AC change on its own; this poll exists purely to keep the label honest.
  useEffect(() => {
    if (!ready || !visible) return;
    let active = true;
    const poll = async () => {
      try {
        const ps = await getPowerSource();
        if (active) setAcOnline(ps.ac);
      } catch (_) {}
    };
    poll();
    const id = setInterval(poll, 3000);
    return () => { active = false; clearInterval(id); };
  }, [ready, visible]);

  // ── Auto-apply game profile when game / ready / enabled changes ──────────────
  useEffect(() => {
    if (!ready) return;

    if (!enabled) {
      if (perGame) setPerGame(false);
      autoAppliedRef.current = null;
      noGameSyncedRef.current = false;
      return;
    }

    if (!game) {
      const wasInGame = autoAppliedRef.current !== null;
      if (perGame) setPerGame(false);
      autoAppliedRef.current = null;
      // setPerGame above re-runs this effect (perGame is a dependency), and
      // without this the whole no-game branch ran twice per game exit - a
      // second getSettings for a state we had already adopted.
      if (noGameSyncedRef.current) return;
      noGameSyncedRef.current = true;
      setSavedPreset(undefined);
      setSavedAcPreset(undefined);
      setAcSeparate(false);
      setEditingAc(false);
      (async () => {
        try {
          const s = await getSettings();
          const w = toW(s.spl), sw = toW(s.sppt), fw = toW(s.fppt);
          setTuning(fromAbsolute(w, sw, fw));
          setPreset((s.active_preset as PresetKey | undefined) || detectPreset(w, sw, fw));
          setGlobalProfile({ spl: w, sppt: sw, fppt: fw, preset: s.active_preset || undefined });
          if (wasInGame) {
            await applyTdp(s.spl, s.sppt, s.fppt, "", s.active_preset || "");
            showStatus("Global settings restored.");
          }
        } catch (e: unknown) {
          showError("LeGoTDP", e);
        }
      })();
      return;
    }

    noGameSyncedRef.current = false;
    if (autoAppliedRef.current === game.appId) return;
    autoAppliedRef.current = game.appId;

    (async () => {
      try {
        const gp = await getGameProfile(game.appId);
        await applyGameProfile(gp, game.appId, `Auto-applied profile for ${game.name}.`);
      } catch (e: unknown) {
        autoAppliedRef.current = null;
        showError("LeGoTDP", e);
      }
    })();
  }, [game?.appId, ready, enabled, perGame]);

  // ── Preset handler ────────────────────────────────────────────────────────────
  const handlePresetChange = async (key: PresetKey) => {
    const prevPreset = preset;
    const prevTuning = tuning, prevAcTuning = acTuning;
    setPreset(key);
    if (key === "custom") return;

    const vals = PRESETS[key];
    const next = normalise(fromAbsolute(vals.spl, vals.sppt, vals.fppt), caps, minW);
    if (editingAc) setAcTuning(next); else setTuning(next);

    setLoading(true);
    showStatus(null);
    const appId = (perGame && game) ? game.appId : "";
    const a = absolute(next);
    try {
      if (editingAc && appId) {
        const r = await setGameAcProfile(appId, toMw(a.spl), toMw(a.sppt), toMw(a.fppt), acSeparate, key);
        if (r.success) {
          setSavedAcPreset(key);
        } else {
          setPreset(prevPreset);
          setAcTuning(prevAcTuning);
        }
        showStatus(r.success ? `AC: ${PRESET_LABELS[key]} saved for ${game!.name}.` : `Error: ${r.stderr || "unknown"}`);
      } else {
        const r = await applyTdp(toMw(a.spl), toMw(a.sppt), toMw(a.fppt), appId, key);
        if (r.success) {
          if (!appId) setGlobalProfile({ ...a, preset: key });
          else setSavedPreset(key);
        } else {
          setPreset(prevPreset);
          setTuning(prevTuning);
        }
        showStatus(r.success
          ? (appId ? `${PRESET_LABELS[key]} saved for ${game!.name}.` : `${PRESET_LABELS[key]} applied.`)
          : `Error: ${r.stderr || "unknown"}`
        );
      }
    } catch (e: unknown) {
      setPreset(prevPreset);
      if (editingAc) setAcTuning(prevAcTuning); else setTuning(prevTuning);
      showError("LeGoTDP", e);
    }
    setLoading(false);
  };

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
      let profileDeleted = false;
      try {
        await deleteGameProfile(game.appId);
        profileDeleted = true;
        const s = await getSettings();
        const w = toW(s.spl), sw = toW(s.sppt), fw = toW(s.fppt);
        setTuning(fromAbsolute(w, sw, fw));
        setPreset((s.active_preset as PresetKey | undefined) || detectPreset(w, sw, fw));
        setGlobalProfile({ spl: w, sppt: sw, fppt: fw, preset: s.active_preset || undefined });
        await applyTdp(s.spl, s.sppt, s.fppt, "", s.active_preset || "");
        showStatus("Switched to global settings.");
      } catch (e: unknown) {
        if (!profileDeleted) {
          setPerGame(true);
          setAcSeparate(prevAcSeparate); setEditingAc(prevEditingAc);
          setSavedPreset(prevSavedPreset); setSavedAcPreset(prevSavedAcPreset);
        }
        showError("LeGoTDP", e);
      }
      // Cleared last so the auto-apply effect cannot race the delete above.
      autoAppliedRef.current = profileDeleted ? game.appId : null;
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
        showError("LeGoTDP", e);
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
        showStatus(r.success ? "Plugin disabled. Firmware defaults restored." : `Error: ${r.stderr || "unknown"}`);
      } else {
        const a = absolute(tuning);
        await applyTdp(toMw(a.spl), toMw(a.sppt), toMw(a.fppt), "", preset === "custom" ? "custom" : preset);
        showStatus("Plugin enabled.");
      }
    } catch (e: unknown) {
      setEnabled(!checked);
      showError("LeGoTDP", e);
    }
  };

  // ── AC separate toggle ────────────────────────────────────────────────────────
  const handleAcSeparateToggle = async (checked: boolean) => {
    if (!game) return;
    const prevSavedAcPreset = savedAcPreset;
    const prevEditingAc = editingAc;
    const prevAcTuning = acTuning;
    setAcSeparate(checked);
    let use = acTuning;
    if (checked && savedAcPreset === undefined) {
      use = tuning;
      setAcTuning(tuning);
    }
    if (!checked) {
      setEditingAc(false);
      setSavedAcPreset(undefined);
    }
    const a = absolute(use);
    try {
      await setGameAcProfile(game.appId, toMw(a.spl), toMw(a.sppt), toMw(a.fppt), checked, "");
    } catch (e: unknown) {
      setAcSeparate(!checked);
      setSavedAcPreset(prevSavedAcPreset);
      setEditingAc(prevEditingAc);
      setAcTuning(prevAcTuning);
      showError("LeGoTDP", e);
    }
  };

  // ── Extras: unlock extended TDP range ────────────────────────────────────────
  const handleExtrasUnlockedToggle = async (checked: boolean) => {
    setExtrasUnlocked(checked);
    try {
      await setExtrasUnlockedCall(checked);
    } catch (e: unknown) {
      setExtrasUnlocked(!checked);
      showError("LeGoTDP", e);
      return;
    }
    if (checked) return;

    // Locking Extras again pulls anything above the firmware ceiling back down.
    const t  = normalise(tuning,   stdCaps, minW);
    const at = normalise(acTuning, stdCaps, minW);
    const tChanged  = !sameTuning(t,  tuning);
    const atChanged = !sameTuning(at, acTuning);
    setTuning(t);
    setAcTuning(at);
    const appId = (perGame && game) ? game.appId : "";
    try {
      if (tChanged) {
        const a = absolute(t);
        const r = await applyTdp(toMw(a.spl), toMw(a.sppt), toMw(a.fppt), appId, "custom");
        setPreset("custom");
        if (r.success) {
          if (!appId) setGlobalProfile({ ...a, preset: "custom" });
          else setSavedPreset("custom");
        }
      }
      if (acSeparate && appId && atChanged) {
        const a = absolute(at);
        const r = await setGameAcProfile(appId, toMw(a.spl), toMw(a.sppt), toMw(a.fppt), acSeparate, "custom");
        if (r.success) setSavedAcPreset("custom");
      }
    } catch (e: unknown) {
      showError("LeGoTDP", e);
    }
  };

  // ── Apply (Custom mode only) ──────────────────────────────────────────────────
  const apply = async () => {
    setLoading(true);
    showStatus(null);
    const appId = (perGame && game) ? game.appId : "";
    const a = absolute(active);
    try {
      if (editingAc && appId) {
        const r = await setGameAcProfile(appId, toMw(a.spl), toMw(a.sppt), toMw(a.fppt), acSeparate, "custom");
        if (r.success) setSavedAcPreset("custom");
        showStatus(r.success ? `AC profile saved for ${game!.name}.` : `Error: ${r.stderr || "unknown"}`);
      } else {
        const r = await applyTdp(toMw(a.spl), toMw(a.sppt), toMw(a.fppt), appId, "custom");
        if (r.success) {
          if (!appId) setGlobalProfile({ ...a, preset: "custom" });
          else setSavedPreset("custom");
        }
        showStatus(r.success
          ? (appId ? `Profile saved for ${game!.name}.` : "Custom settings applied.")
          : `Error: ${r.stderr || "unknown"}`
        );
      }
    } catch (e: unknown) {
      showError("LeGoTDP", e);
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
                  <span style={{ fontSize: "11px", color: DIM_COLOR }}>Global Profile: </span>
                  <span style={styles.profileTag}>{profileLabel(globalProfile.spl, globalProfile.sppt, globalProfile.fppt, globalProfile.preset)}</span>
                  {!extrasUnlocked && exceedsCaps(globalProfile.spl, globalProfile.sppt, globalProfile.fppt, stdCaps) && (
                    <span style={{ fontSize: "11px", color: WARN_COLOR }}> ⚠ exceeds firmware limits</span>
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
                          <span style={{ fontSize: "11px", color: DIM_COLOR }}>Battery: </span>
                          <span style={styles.profileTag}>
                            {profileLabel(absolute(tuning).spl, absolute(tuning).sppt, absolute(tuning).fppt, savedPreset)}
                          </span>
                        </span>
                        {acSeparate && (
                          <span>
                            <span style={{ fontSize: "11px", color: DIM_COLOR }}>AC: </span>
                            <span style={styles.profileTag}>
                              {profileLabel(absolute(acTuning).spl, absolute(acTuning).sppt, absolute(acTuning).fppt, savedAcPreset)}
                            </span>
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
                <div style={{ fontSize: "11px", fontWeight: "bold", color: acOnline ? OK_COLOR : WARN_COLOR }}>
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
                  label={`SPL (TDP) - ${active.spl} W`}
                  value={active.spl} min={minW} max={caps.spl} step={1}
                  onChange={handlers.onSpl}
                  description="Sustained power limit - the main TDP dial"
                />
              </PanelSectionRow>
              <PanelSectionRow>
                <SliderField
                  label={`SPPT +${active.spptOff} W  =  ${active.spl + active.spptOff} W`}
                  value={active.spptOff} min={0} max={om.sppt || 1} step={1}
                  disabled={om.sppt === 0}
                  onChange={handlers.onSppt}
                  description={om.sppt === 0
                    ? "No headroom left at this SPL"
                    : `Slow limit headroom above SPL (max +${om.sppt} W here)`}
                />
              </PanelSectionRow>
              <PanelSectionRow>
                <SliderField
                  label={`FPPT +${active.fpptOff} W  =  ${active.spl + active.fpptOff} W`}
                  value={active.fpptOff} min={0} max={om.fppt || 1} step={1}
                  disabled={om.fppt === 0}
                  onChange={handlers.onFppt}
                  description={om.fppt === 0
                    ? "No headroom left at this SPL"
                    : `Fast limit headroom above SPL (max +${om.fppt} W here)`}
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
          <div style={styles.infoBox}>
            These settings are for advanced users only and are NOT recommended.
            Changes are made at your own risk — they override the manufacturer's TDP safety limits.
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label={`Unlock Custom TDP to ${maxCaps.spl} W`}
            description={extrasUnlocked
              ? `Custom sliders extended to ${maxCaps.spl} W - applied via ryzenadj instead of firmware`
              : `Enable to allow Custom sliders up to ${maxCaps.spl} W`}
            checked={extrasUnlocked}
            onChange={handleExtrasUnlockedToggle}
          />
        </PanelSectionRow>
      </PanelSection>
    </>
  );
};

// ── Plugin entry point ─────────────────────────────────────────────────────────

export default definePlugin(() => {
  // Started here rather than from the panel: the backend enforce loop needs the
  // running appid whether or not anyone has the Quick Access Menu open.
  AppWatcher.start();

  return {
    name: "LeGoTDP",
    titleView: <div className={staticClasses.Title}>LeGoTDP</div>,
    content: <Content />,
    icon: <ChipIcon />,
    onDismount() {
      AppWatcher.stop();
    },
  };
});
