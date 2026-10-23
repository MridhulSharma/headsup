/**
 * Which application this page is: Heads Up, or the fork as upstream ships it.
 *
 * `?mode=headsup` (the default) is the Heads Up civic console — downtown
 * Austin, the advisory layer, a scenario picker, and nothing that reaches for
 * a satellite. `?mode=full` is upstream's God's Eye View, untouched.
 *
 * Everything Heads Up mode changes is expressed here as DATA: which layers are
 * registered, which optional subsystems start, which readouts the stylesheet
 * hides. No upstream module is deleted or rewritten; each one asks this config
 * whether to do its optional work, and `gev/src/ui/styles/headsup-mode.css`
 * hides the chrome from a single `body.headsup-mode` hook. That is what keeps
 * `git subtree pull` a merge of upstream's own files.
 *
 * Pure: a search string in, a frozen config out. No DOM, no Cesium, no
 * `import.meta.env` — the standalone entry reads the environment and passes
 * the override in.
 * @module headsup/mode
 */

/** `?mode=headsup`: the Heads Up console. The default. */
export const HEADSUP_MODE = 'headsup';
/** `?mode=full`: upstream's application, with nothing gated. */
export const FULL_MODE = 'full';

/** The class every Heads Up CSS rule hangs off. */
export const HEADSUP_BODY_CLASS = 'headsup-mode';

/** Where the real Heads Up scenario API answers (`hu api`). */
export const HEADSUP_SCENARIO_BASE_URL = 'http://localhost:8765';
/**
 * Where the mock feed answers (`viz/mock_stream.py`).
 *
 * The console asks the real API first and falls back here, the same ladder
 * the viewer socket walks (`layers/headsup/source.js`). One port each, so
 * both can be up at once and "the API is not running" is a thing the page can
 * discover rather than a thing someone has to be told.
 */
export const HEADSUP_MOCK_SCENARIO_BASE_URL = 'http://localhost:8766';

/**
 * Layers Heads Up mode never registers.
 *
 * Registration is what later lets something enable a layer, and enabling is
 * what makes it fetch. Leaving these out of the data manager is therefore the
 * whole of "do not initialize them": no `init()`, no poll, no 401 from a
 * provider this console has no key for. They stay in the CATALOG so the
 * control surface upstream builds from it still finds every instance it
 * declares — only the manager's registry is narrower.
 */
export const HEADSUP_OMITTED_LAYERS = Object.freeze([
  'flights',
  'military',
  'ais-live-vessels',
  'satellites',
  'rocket-launches',
  // Global Context, labelled CONTACTS in the UI. It is a view OVER the four
  // layers above, so it could not report anything once they are gone.
  'military-awareness',
]);

/** The layer Heads Up mode switches on for itself at startup. */
export const HEADSUP_LAYER_ID = 'headsup';

/**
 * Where the camera sits when no scenario is running: downtown Austin, the
 * same pose `flyToAustin()` settles at, so Exit returns to where the page
 * started rather than to a second, slightly different "home".
 */
export const HEADSUP_LANDING_VIEW = Object.freeze({
  lon: -97.7431,
  lat: 30.2672,
  height: 600,
  heading: 15,
  pitch: -30,
});

/**
 * Read the mode out of a query string.
 *
 * An unrecognized value resolves to Heads Up, because Heads Up is the
 * default: a typo in the parameter must not silently hand someone the full
 * application.
 * @param {string} [search] `location.search`, with or without its `?`.
 * @returns {'headsup'|'full'}
 */
export function resolveHeadsupMode(search = '') {
  let requested = null;
  try {
    requested = new URLSearchParams(String(search ?? '')).get('mode');
  } catch {
    // A search string this malformed says nothing about the mode.
    requested = null;
  }
  return String(requested ?? '')
    .trim()
    .toLowerCase() === FULL_MODE
    ? FULL_MODE
    : HEADSUP_MODE;
}

/**
 * The frozen config every gated module reads.
 * @param {object} [options]
 * @param {string} [options.search] `location.search`.
 * @param {string} [options.scenarioBaseUrl] Scenario API origin override.
 * @param {string|null} [options.scenarioFallbackUrl] Origin tried when the
 *   first one does not answer; null for no fallback.
 * @returns {Readonly<object>}
 */
export function createHeadsupModeConfig({
  search = '',
  scenarioBaseUrl = HEADSUP_SCENARIO_BASE_URL,
  scenarioFallbackUrl = HEADSUP_MOCK_SCENARIO_BASE_URL,
} = {}) {
  const id = resolveHeadsupMode(search);
  const headsUp = id === HEADSUP_MODE;
  return Object.freeze({
    id,
    headsUp,
    bodyClass: HEADSUP_BODY_CLASS,
    /** Layer ids the data manager must not register. */
    omitLayers: Object.freeze(headsUp ? [...HEADSUP_OMITTED_LAYERS] : []),
    /** Layer ids Heads Up mode enables once registration is sealed. */
    enableLayers: Object.freeze(headsUp ? [HEADSUP_LAYER_ID] : []),
    /** Upstream's own layer rows own `#data-toggles` only in full mode. */
    upstreamLayerPanel: !headsUp,
    /** The Realtime voice agent: a paid session nobody asked for here. */
    voiceAgent: !headsUp,
    /** The POWER UP key-setup surface. */
    providerSettings: !headsUp,
    /** The HUD's LLM-written summary line, hidden and so never requested. */
    hudSummary: !headsUp,
    /** Heads Up keeps the HUD frame on in the normal sensor style. */
    forceHudVisible: headsUp,
    scenarioBaseUrl: String(scenarioBaseUrl || HEADSUP_SCENARIO_BASE_URL),
    scenarioFallbackUrl: scenarioFallbackUrl
      ? String(scenarioFallbackUrl)
      : null,
    landingView: HEADSUP_LANDING_VIEW,
  });
}

/**
 * Apply the one CSS hook, as early as the entry can.
 * @param {Document} doc
 * @param {Readonly<object>} mode
 */
export function markHeadsupMode(doc, mode) {
  if (!doc?.documentElement) return;
  doc.documentElement.dataset.gevMode = mode.id;
  // The class, not the attribute, is what the stylesheet selects on: it is
  // also what an upstream rule like `body.ui-clean-view` reads, so Heads Up
  // rules compose with upstream's instead of out-specifying them.
  if (mode.headsUp) doc.body?.classList.add(mode.bodyClass);
  else doc.body?.classList.remove(mode.bodyClass);
}
