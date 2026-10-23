/**
 * The Heads Up console: the chrome `?mode=headsup` puts over the fork.
 *
 * This module only ADDS. It retitles two panel headers, appends its own
 * children into four surfaces upstream already owns, and builds three of its
 * own. Nothing upstream is removed, and nothing upstream's own code holds a
 * reference to is replaced — `#active-style-name`, `#hud-gsd`, `#scene-select`
 * and the rest all stay in the document, hidden by
 * `src/ui/styles/headsup-mode.css` from the single `body.headsup-mode` hook.
 * That is the whole reason a `git subtree pull` stays a clean merge.
 *
 * Surfaces, in the order the build step names them:
 *
 * | Upstream surface | Heads Up use |
 * | --- | --- |
 * | `#data-panel` / `#data-toggles` | Hazards, one row per fleet, the ghost fleet, corridors |
 * | `#scene-panel` | a Test Scene button and its picker |
 * | `#style-indicator` (top right) | scenario name, sim clock, replay speed |
 * | `.hud-bottom-right` | the counter strip |
 * | (new) `#headsup-banner` | the what-if provenance line |
 * | (new) `#headsup-pipeline-rail` | the agent rail, empty until D3.6 |
 * | (new) `#headsup-scene-picker` | the scenario dialog |
 *
 * Every string a person reads goes through `t()`. Every string that came off
 * the feed — a scenario name, a city, an operator id — is written with
 * `textContent`, never markup, and is bounded by the grammar in
 * `./scenarios.js` and `../layers/headsup/records.js` before it arrives here.
 * @module headsup/console
 */

import { msg, t } from '../i18n/index.js';
import { ScenarioApiError } from './scenarios.js';

/**
 * The agent pipeline, as stages rather than as frames.
 *
 * The rail OPENS with a scenario and stays empty in this step: the reasoning
 * each stage produced — the candidate's evidence, the critic's checks — is
 * what D3.6 renders here. The stage list is still worth showing now, because
 * an empty rail that names the six steps says what is coming; an empty box
 * says nothing.
 */
export const PIPELINE_STAGES = Object.freeze([
  Object.freeze({ key: 'sentinel', label: msg('Sentinel') }),
  Object.freeze({ key: 'fusion', label: msg('Fusion') }),
  Object.freeze({ key: 'proposer', label: msg('Proposer') }),
  Object.freeze({ key: 'critic', label: msg('Critic') }),
  Object.freeze({ key: 'compiler', label: msg('Compiler') }),
  Object.freeze({ key: 'ack', label: msg('Fleet ack') }),
]);

/**
 * The counter strip, bottom right.
 *
 * `read` takes the layer's stats and the feed snapshot and returns one
 * number. Nothing here is derived from anything but those two, so a counter
 * can never disagree with what is drawn.
 */
export const HEADSUP_COUNTERS = Object.freeze([
  Object.freeze({
    key: 'hazards',
    label: msg('HAZARDS'),
    read: (stats, feed) => feed?.pipeline?.hazards ?? 0,
  }),
  Object.freeze({
    key: 'advisories',
    label: msg('ADVISORIES'),
    read: (stats) => stats.advisories ?? 0,
  }),
  Object.freeze({
    key: 'fleet',
    label: msg('FLEET'),
    read: (stats) => Math.max(0, (stats.vehicles ?? 0) - (stats.ghosts ?? 0)),
  }),
  Object.freeze({
    key: 'rerouted',
    label: msg('REROUTED'),
    read: (stats) => stats.rerouted ?? 0,
  }),
  Object.freeze({
    key: 'ghost',
    label: msg('GHOST'),
    read: (stats) => stats.ghosts ?? 0,
  }),
  Object.freeze({
    key: 'acks',
    label: msg('ACKS'),
    read: (stats, feed) => feed?.pipeline?.acks?.length ?? 0,
  }),
]);

/** The provenance line every what-if replay carries. */
export const WHAT_IF_BANNER = msg(
  'WHAT-IF REPLAY · recorded public data + scripted events',
);

/**
 * The badge a replay carries when its events were written rather than
 * observed, from the feed's own `scripted` flag.
 */
export const SCRIPTED_BADGE = msg('SCRIPTED DATA');

/** What the world tag on a refusing fleet says. */
export const DECLINED_TAG = msg('DECLINED');

/** How often the readouts are refreshed when no frame has arrived. */
const IDLE_REFRESH_MS = 1_000;

/**
 * Start the Heads Up console.
 * @param {object} options
 * @param {Readonly<object>} options.mode Mode config (`./mode.js`).
 * @param {object} options.layer The `headsup` data-layer module.
 * @param {(enabled: boolean) => Promise<unknown>} options.setLayerEnabled
 *   Switch the Heads Up layer on or off through the data manager.
 * @param {object} [options.hud] The IntelHUD, so the frame can be held on.
 * @param {(view: object) => void} options.flyTo Camera flight.
 * @param {object} options.scenarios Scenario API client (`./scenarios.js`).
 * @param {Document} [options.doc]
 * @param {AbortSignal} [options.signal] Application lifetime.
 * @returns {{destroy: () => void}}
 */
export function startHeadsupConsole({
  mode,
  layer,
  setLayerEnabled,
  hud,
  flyTo,
  scenarios,
  doc = document,
  signal,
}) {
  const removers = [];
  let destroyed = false;
  /** The scenario currently replaying, as the API described it. */
  let running = null;
  /** The operator ids the fleet rows were last built for. */
  let renderedOperators = '';
  let pickerBusy = false;
  const controller = new AbortController();
  if (signal) signal.addEventListener('abort', () => destroy(), { once: true });

  const on = (element, type, listener, options) => {
    if (!element) return;
    element.addEventListener(type, listener, options);
    removers.push(() => element.removeEventListener(type, listener, options));
  };
  const element = (tag, className, parent) => {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    parent?.append(node);
    return node;
  };
  /** A node this module created, so teardown can take it away again. */
  const owned = [];
  const mount = (node, parent) => {
    parent?.append(node);
    owned.push(node);
    return node;
  };

  // ── The HUD frame ───────────────────────────────────────────────────────
  //
  // Heads Up mode runs in the normal sensor style, where upstream's HUD is
  // hidden; the frame is held on explicitly so the counter strip has a
  // surface. The summary line is hidden by the stylesheet, and its policy is
  // closed here as well so the hidden line never spends a request on an
  // answer nobody will read.
  if (mode.forceHudVisible && hud) {
    if (!mode.hudSummary) hud.summaryPolicy = { canRequest: () => false };
    hud.setMode('on');
  }

  // ── Left panel: the Heads Up layers ─────────────────────────────────────

  const dataPanel = doc.getElementById('data-panel');
  const dataTitle = dataPanel?.querySelector('.panel-title');
  const previousDataTitle = dataTitle?.textContent ?? null;
  if (dataTitle) dataTitle.textContent = t('HEADS UP LAYERS');
  const toggles = doc.getElementById('data-toggles');
  const cohortRows = new Map();
  let fleetGroup = null;

  /**
   * One cohort row: a swatch, a name, a count, and a pressed state.
   *
   * The row is a button rather than a checkbox because it carries the
   * cohort's colour, and the colour is what connects the row to the dots on
   * the globe. `aria-pressed` is what makes it a toggle to a screen reader.
   */
  const cohortRow = (parent, { key, name, color, onToggle }) => {
    const row = element('button', 'headsup-cohort', parent);
    row.type = 'button';
    row.dataset.cohort = key;
    row.setAttribute('aria-pressed', 'true');
    const swatch = element('span', 'headsup-cohort-swatch', row);
    swatch.setAttribute('aria-hidden', 'true');
    if (color) swatch.style.setProperty('--headsup-cohort-color', color);
    const labelNode = element('span', 'headsup-cohort-label', row);
    labelNode.textContent = name;
    const countNode = element('span', 'headsup-cohort-count', row);
    countNode.textContent = '0';
    on(row, 'click', () => {
      const next = row.getAttribute('aria-pressed') !== 'true';
      row.setAttribute('aria-pressed', next ? 'true' : 'false');
      onToggle(next);
    });
    const entry = { row, swatch, labelNode, countNode };
    cohortRows.set(key, entry);
    return entry;
  };

  const setRowState = (key, { count, visible, color }) => {
    const entry = cohortRows.get(key);
    if (!entry) return;
    entry.countNode.textContent = String(count);
    if (visible !== undefined)
      entry.row.setAttribute('aria-pressed', visible ? 'true' : 'false');
    if (color) entry.swatch.style.setProperty('--headsup-cohort-color', color);
  };

  if (toggles) {
    cohortRow(toggles, {
      key: 'hazards',
      name: t('Hazards'),
      color: '#2bb3ff',
      onToggle: (shown) => layer.setCohortVisibility?.({ hazards: shown }),
    });
    cohortRow(toggles, {
      key: 'corridors',
      name: t('Corridors'),
      color: '#ffd400',
      onToggle: (shown) => layer.setCohortVisibility?.({ corridors: shown }),
    });
    const fleetHeading = element('h3', 'headsup-group', toggles);
    fleetHeading.textContent = t('Fleets');
    fleetGroup = element('div', 'headsup-fleet-group', toggles);
    const empty = element('p', 'headsup-empty', fleetGroup);
    empty.textContent = t('No fleet reporting');
    cohortRow(toggles, {
      key: 'ghost',
      name: t('Ghost fleet'),
      color: '#8a94a6',
      onToggle: (shown) => layer.setCohortVisibility?.({ ghost: shown }),
    });
  }

  // ── Left panel: the legend ──────────────────────────────────────────────
  //
  // Only what the panel does not already say. The cohort rows above carry a
  // swatch per FLEET, so the legend's job is the colours nobody can work out
  // from the globe alone — one per hazard class actually drawn — plus the two
  // marks whose meaning is not self-evident: the heading chevron and the
  // deflection trail. Everything here is read from what is DRAWN, so the
  // legend cannot name a colour that is not on screen.
  const legendHeading = toggles
    ? mount(element('h3', 'headsup-group'), toggles)
    : null;
  if (legendHeading) legendHeading.textContent = t('LEGEND');
  const legend = toggles
    ? mount(element('div', 'headsup-legend'), toggles)
    : null;
  const hazardLegend = legend ? element('div', null, legend) : null;
  /** The hazard classes the legend rows were last built for. */
  let renderedHazards = '';
  /** @type {Map<string, HTMLElement>} */
  const hazardCounts = new Map();

  /** One legend row: a mark, a name, and an optional count. */
  const legendRow = (parent, { mark, color, name, glyph }) => {
    const row = element('div', 'headsup-legend-row', parent);
    const swatch = element(
      'span',
      glyph ? 'headsup-legend-glyph' : 'headsup-legend-swatch',
      row,
    );
    swatch.setAttribute('aria-hidden', 'true');
    if (glyph) swatch.textContent = mark;
    if (color) swatch.style.setProperty('--headsup-legend-color', color);
    const label = element('span', 'headsup-legend-label', row);
    label.textContent = name;
    const count = element('span', 'headsup-legend-count', row);
    count.textContent = '';
    return { row, count };
  };

  // The static marks, built once: what a chevron and a trail mean does not
  // depend on the feed.
  const headingMark = legend
    ? legendRow(legend, { mark: '▲', glyph: true, name: t('Heading') })
    : null;
  const trailMark = legend
    ? legendRow(legend, {
        mark: '━',
        glyph: true,
        name: t('Deflection trail'),
      })
    : null;
  const declinedMark = legend
    ? legendRow(legend, {
        mark: '●',
        glyph: true,
        color: '#ef476f',
        name: t('Declined'),
      })
    : null;
  // A refusal row with nothing to report would be a control-panel light for
  // a condition that is not happening.
  if (declinedMark) declinedMark.row.hidden = true;

  /**
   * Rebuild the hazard-class rows, but only when the CLASS SET changed.
   *
   * The same discipline the fleet rows keep: counts move with every frame,
   * rows must not.
   */
  const renderHazardLegend = (rows) => {
    if (!hazardLegend) return;
    const signature = rows.map((row) => row.hazardClass).join(',');
    if (signature !== renderedHazards) {
      renderedHazards = signature;
      hazardCounts.clear();
      hazardLegend.replaceChildren();
      for (const row of rows)
        hazardCounts.set(
          row.hazardClass,
          legendRow(hazardLegend, {
            color: row.color,
            // A hazard class is contract text, not a sentence: shown as the
            // contract spells it, so it matches the advisory's own label.
            name: row.hazardClass,
          }).count,
        );
    }
    for (const row of rows) {
      const cell = hazardCounts.get(row.hazardClass);
      if (cell) cell.textContent = String(row.advisories);
    }
  };

  /**
   * Rebuild the per-operator rows, but only when the operator SET changed.
   *
   * Counts change twice a second; the rows must not, or a toggle would be
   * rebuilt under the pointer that is pressing it.
   */
  const renderFleetRows = (operators) => {
    const signature = operators.map(({ op }) => op).join(',');
    if (!fleetGroup || signature === renderedOperators) {
      for (const fleet of operators)
        setRowState(`op:${fleet.op}`, {
          count: fleet.vehicles,
          visible: fleet.visible,
        });
      return;
    }
    renderedOperators = signature;
    for (const key of [...cohortRows.keys()])
      if (key.startsWith('op:')) cohortRows.delete(key);
    fleetGroup.replaceChildren();
    if (!operators.length) {
      const empty = element('p', 'headsup-empty', fleetGroup);
      empty.textContent = t('No fleet reporting');
      return;
    }
    for (const fleet of operators) {
      cohortRow(fleetGroup, {
        key: `op:${fleet.op}`,
        // An operator id is feed text: shown, never interpolated into markup.
        name: fleet.op,
        color: fleet.color,
        onToggle: (shown) =>
          layer.setCohortVisibility?.({ operators: { [fleet.op]: shown } }),
      });
      setRowState(`op:${fleet.op}`, {
        count: fleet.vehicles,
        visible: fleet.visible,
      });
    }
  };

  // ── Left panel: the Test Scene button ───────────────────────────────────

  const scenePanel = doc.getElementById('scene-panel');
  const sceneTitle = scenePanel?.querySelector('.panel-title');
  const previousSceneTitle = sceneTitle?.textContent ?? null;
  if (sceneTitle) sceneTitle.textContent = t('TEST SCENE');
  const sceneInner = scenePanel?.querySelector('.scene-panel-inner');
  const sceneLaunch = sceneInner
    ? mount(element('div', 'headsup-scene-launch'), sceneInner)
    : null;
  const openButton = sceneLaunch
    ? element('button', 'headsup-scene-btn', sceneLaunch)
    : null;
  if (openButton) {
    openButton.type = 'button';
    openButton.textContent = t('OPEN TEST SCENE');
    openButton.title = t('Pick a what-if replay to run');
  }
  const exitButton = sceneLaunch
    ? element('button', 'headsup-scene-btn headsup-scene-exit', sceneLaunch)
    : null;
  if (exitButton) {
    exitButton.type = 'button';
    exitButton.textContent = t('EXIT SCENE');
    exitButton.title = t('Stop the replay and fly back to the landing view');
    exitButton.hidden = true;
  }
  const sceneStatus = sceneLaunch
    ? element('p', 'headsup-scene-status', sceneLaunch)
    : null;

  const setSceneStatus = (text) => {
    if (sceneStatus) sceneStatus.textContent = text ?? '';
  };

  // ── Top right: scenario, sim clock, replay speed ────────────────────────

  const styleIndicator = doc.getElementById('style-indicator');
  const readout = styleIndicator
    ? mount(element('div', 'headsup-readout'), styleIndicator)
    : null;
  const readoutRow = (labelText) => {
    if (!readout) return null;
    const row = element('div', 'headsup-readout-row', readout);
    const key = element('span', 'headsup-readout-label', row);
    key.textContent = labelText;
    const value = element('span', 'headsup-readout-value', row);
    value.textContent = '—';
    return value;
  };
  const scenarioValue = readoutRow(t('SCENARIO'));
  const clockValue = readoutRow(t('SIM CLOCK'));
  const speedValue = readoutRow(t('REPLAY'));

  // ── Bottom right: the counter strip ─────────────────────────────────────

  const hudBottomRight = doc.querySelector('#intel-hud .hud-bottom-right');
  const hudContent = hudBottomRight?.querySelector('.hud-content');
  const strip = hudContent
    ? mount(element('div', 'headsup-counters'), hudContent)
    : null;
  const counterValues = new Map();
  if (strip)
    for (const counter of HEADSUP_COUNTERS) {
      const cell = element('div', 'headsup-counter', strip);
      const value = element('strong', 'headsup-counter-value', cell);
      value.textContent = '0';
      const name = element('span', 'headsup-counter-label', cell);
      name.textContent = t(counter.label);
      counterValues.set(counter.key, value);
    }
  const feedChip = strip
    ? element('div', 'headsup-counter headsup-feed', strip)
    : null;
  const feedValue = feedChip
    ? element('strong', 'headsup-counter-value', feedChip)
    : null;
  if (feedChip) {
    const name = element('span', 'headsup-counter-label', feedChip);
    name.textContent = t('FEED');
  }

  // ── The what-if banner and the agent pipeline rail ──────────────────────

  const banner = mount(element('div', 'headsup-banner'), doc.body);
  banner.id = 'headsup-banner';
  banner.setAttribute('role', 'status');
  banner.hidden = true;
  const bannerText = element('span', 'headsup-banner-text', banner);
  bannerText.textContent = t(WHAT_IF_BANNER);
  // The scripted badge, driven by the scenario's own `scripted` flag rather
  // than by which scenario it is: a replay whose events were written for the
  // demo has to say so, and only the feed knows which replays those are.
  const scriptedBadge = element('span', 'headsup-banner-badge', banner);
  scriptedBadge.textContent = t(SCRIPTED_BADGE);
  scriptedBadge.title = t(
    'The events in this replay were written, not observed',
  );
  scriptedBadge.hidden = true;

  const rail = mount(element('aside', 'headsup-rail'), doc.body);
  rail.id = 'headsup-pipeline-rail';
  rail.hidden = true;
  rail.setAttribute('aria-label', t('Agent pipeline'));
  const railHeader = element('header', 'headsup-rail-header', rail);
  const railTitle = element('span', 'headsup-rail-title', railHeader);
  railTitle.textContent = t('AGENT PIPELINE');
  const railStages = element('ol', 'headsup-rail-stages', rail);
  for (const stage of PIPELINE_STAGES) {
    const item = element('li', 'headsup-rail-stage', railStages);
    item.dataset.stage = stage.key;
    const marker = element('span', 'headsup-rail-marker', item);
    marker.setAttribute('aria-hidden', 'true');
    const name = element('span', 'headsup-rail-stage-name', item);
    name.textContent = t(stage.label);
  }
  const railEmpty = element('p', 'headsup-rail-empty', rail);
  railEmpty.textContent = t('Stage detail arrives with the agent rail.');

  // ── The scenario picker ─────────────────────────────────────────────────

  const picker = mount(element('div', 'headsup-picker'), doc.body);
  picker.id = 'headsup-scene-picker';
  picker.hidden = true;
  const dialog = element('div', 'headsup-picker-dialog', picker);
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  dialog.setAttribute('aria-label', t('CHOOSE A TEST SCENE'));
  const dialogHeader = element('header', 'headsup-picker-header', dialog);
  const dialogTitle = element('h2', 'headsup-picker-title', dialogHeader);
  dialogTitle.textContent = t('CHOOSE A TEST SCENE');
  const closeButton = element('button', 'headsup-picker-close', dialogHeader);
  closeButton.type = 'button';
  closeButton.title = t('Close the scene picker');
  closeButton.setAttribute('aria-label', t('Close the scene picker'));
  closeButton.textContent = '×';
  const pickerList = element('div', 'headsup-picker-list', dialog);
  const pickerStatus = element('p', 'headsup-picker-status', dialog);
  pickerStatus.setAttribute('role', 'status');

  let restoreFocus = null;

  const setPickerStatus = (text) => {
    pickerStatus.textContent = text ?? '';
  };

  const closePicker = () => {
    if (picker.hidden) return;
    picker.hidden = true;
    const target = restoreFocus;
    restoreFocus = null;
    // Focus goes back where it came from, so a keyboard user is not dropped
    // at the top of the document by a dialog they dismissed.
    if (target?.isConnected) target.focus();
  };

  const renderScenarios = (rows) => {
    pickerList.replaceChildren();
    if (!rows.length) {
      setPickerStatus(t('No scenarios available'));
      return;
    }
    setPickerStatus('');
    for (const scenario of rows) {
      const card = element('button', 'headsup-picker-card', pickerList);
      card.type = 'button';
      const heading = element('span', 'headsup-picker-card-name', card);
      // Feed text, one node each: a code badge, the name, the city.
      if (scenario.code) {
        const code = element('span', 'headsup-picker-card-code', heading);
        code.textContent = scenario.code;
      }
      const name = element('span', null, heading);
      name.textContent = scenario.city
        ? t('{name} ({city})', { name: scenario.name, city: scenario.city })
        : scenario.name;
      if (scenario.whatIf) {
        const whatIf = element('span', 'headsup-picker-card-whatif', card);
        whatIf.textContent = scenario.whatIf;
      }
      on(card, 'click', () => void beginScenario(scenario, card));
    }
    pickerList.querySelector('button')?.focus();
  };

  const openPicker = async (opener) => {
    if (destroyed) return;
    restoreFocus = opener ?? doc.activeElement ?? null;
    picker.hidden = false;
    pickerList.replaceChildren();
    setPickerStatus(t('Loading scenarios…'));
    try {
      renderScenarios(await scenarios.list({ signal: controller.signal }));
    } catch (error) {
      if (controller.signal.aborted) return;
      setPickerStatus(describeScenarioError(error));
    }
  };

  /**
   * Turn a failure into one line a person can act on.
   *
   * A `ScenarioApiError` carries English message DATA from the portable
   * client, which is translated here at the presentation boundary. Anything
   * else is a defect in this console and says so without quoting a stack at
   * somebody watching a demo.
   */
  const describeScenarioError = (error) =>
    error instanceof ScenarioApiError
      ? t('{reason} Is the Heads Up API on {url}?', {
          reason: t(error.message),
          url: scenarios.baseUrl,
        })
      : t('The Heads Up scenario call failed.');

  // ── Starting and stopping a replay ──────────────────────────────────────

  const applyRunning = (scenario) => {
    running = scenario;
    banner.hidden = !scenario;
    rail.hidden = !scenario;
    if (exitButton) exitButton.hidden = !scenario;
    if (openButton)
      openButton.textContent = scenario
        ? t('CHANGE TEST SCENE')
        : t('OPEN TEST SCENE');
  };

  const beginScenario = async (scenario, card) => {
    if (pickerBusy) return;
    pickerBusy = true;
    card?.setAttribute('aria-busy', 'true');
    setPickerStatus(t('Starting {name}…', { name: scenario.name }));
    try {
      const result = await scenarios.start(scenario.id, {
        signal: controller.signal,
      });
      if (destroyed || controller.signal.aborted) return;
      // The API's own view wins when it sends one: it knows where the
      // scenario's recording actually is, and the listing may be older.
      const started = result.scenario ?? scenario;
      applyRunning(started);
      flyTo(started.startView ?? scenario.startView);
      setSceneStatus(
        started.city
          ? t('{name} ({city})', { name: started.name, city: started.city })
          : started.name,
      );
      closePicker();
    } catch (error) {
      if (destroyed || controller.signal.aborted) return;
      setPickerStatus(describeScenarioError(error));
    } finally {
      pickerBusy = false;
      card?.removeAttribute('aria-busy');
    }
  };

  const endScenario = async () => {
    if (!running) return;
    const stopping = running;
    applyRunning(null);
    setSceneStatus('');
    // Clear what the replay drew before the camera leaves it: an advisory
    // from a scenario that is over must not be left hanging over the landing
    // view as though it were in force there.
    layer.clearDrawn?.();
    flyTo(mode.landingView);
    try {
      await scenarios.stop({ signal: controller.signal });
    } catch (error) {
      if (destroyed || controller.signal.aborted) return;
      // The replay is already off the screen; say the server was not told.
      setSceneStatus(
        t('{name} stopped here, but the API was not reached.', {
          name: stopping.name,
        }),
      );
    }
  };

  if (openButton) on(openButton, 'click', () => void openPicker(openButton));
  if (exitButton) on(exitButton, 'click', () => void endScenario());
  on(closeButton, 'click', () => closePicker());
  // A click on the backdrop, not on the dialog, dismisses it.
  on(picker, 'mousedown', (event) => {
    if (event.target === picker) closePicker();
  });

  /**
   * Escape closes the picker, then ends the replay.
   *
   * Only ever acted on when one of those two is actually open, and the event
   * is left alone otherwise: upstream's own Escape handlers (the cockpit, the
   * draw tool, the street-level viewer) must keep working in Heads Up mode.
   */
  const onKeyDown = (event) => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    if (!picker.hidden) {
      event.preventDefault();
      event.stopPropagation();
      closePicker();
      return;
    }
    if (running) {
      event.preventDefault();
      event.stopPropagation();
      void endScenario();
    }
  };
  on(doc, 'keydown', onKeyDown, { capture: true });

  // ── Readouts ────────────────────────────────────────────────────────────

  const formatClock = (feed) => {
    // The simulator's clock, shown as the instant it reported. An unparseable
    // one is shown as sent rather than silently replaced with ours.
    if (!feed?.scenario?.simTime) return feed?.scenario?.simTimeText ?? '—';
    return new Date(feed.scenario.simTime)
      .toISOString()
      .slice(11, 19)
      .concat('Z');
  };

  const render = () => {
    if (destroyed) return;
    const stats = layer.getStats?.() ?? {};
    const feed = layer.getFeedSnapshot?.() ?? null;
    const scenario = feed?.scenario ?? null;
    const label = scenario?.name
      ? scenario.code
        ? t('{code} {name}', { code: scenario.code, name: scenario.name })
        : scenario.name
      : t('IDLE');
    if (scenarioValue) scenarioValue.textContent = label;
    if (clockValue) clockValue.textContent = formatClock(feed);
    if (speedValue)
      speedValue.textContent =
        scenario?.state === 'running'
          ? t('{speed}×', { speed: scenario.speed })
          : '—';
    scriptedBadge.hidden = !(
      scenario?.state === 'running' && scenario.scripted === true
    );
    for (const counter of HEADSUP_COUNTERS) {
      const cell = counterValues.get(counter.key);
      if (cell) cell.textContent = String(counter.read(stats, feed));
    }
    // Which feed answered, not merely whether one did: a page driving the
    // mock must say so, because every number on this strip then came from a
    // synthetic feed rather than from a city.
    if (feedValue)
      feedValue.textContent = !stats.connected
        ? t('DOWN')
        : stats.fallback
          ? t('MOCK')
          : t('LIVE');
    feedChip?.classList.toggle('is-down', !stats.connected);
    feedChip?.classList.toggle('is-mock', stats.connected && !!stats.fallback);
    if (feedChip)
      feedChip.title = stats.endpoint
        ? t('Feed: {url}', { url: stats.endpoint })
        : '';
    renderHazardLegend(layer.getHazardLegend?.() ?? []);
    // A fleet too large to orient says so, rather than leaving a legend
    // entry for a mark that is not being drawn.
    if (headingMark)
      headingMark.count.textContent = stats.oriented ? '' : t('not drawn');
    if (trailMark)
      trailMark.count.textContent = stats.trails ? String(stats.trails) : '';
    if (declinedMark) {
      declinedMark.count.textContent = stats.declined
        ? String(stats.declined)
        : '';
      declinedMark.row.hidden = !stats.declined;
    }
    renderFleetRows(layer.getOperatorLegend?.() ?? []);
    const ghost = layer.getGhostCohort?.() ?? { vehicles: 0, visible: true };
    setRowState('ghost', { count: ghost.vehicles, visible: ghost.visible });
    const cohorts = layer.getCohortVisibility?.() ?? {};
    setRowState('hazards', {
      count: Math.max(0, (stats.advisories ?? 0) - (stats.corridors ?? 0)),
      visible: cohorts.hazards !== false,
    });
    setRowState('corridors', {
      count: stats.corridors ?? 0,
      visible: cohorts.corridors !== false,
    });
    // A run the server started or stopped without this page asking (a second
    // tab, `hu demo`) still owns the banner and the rail.
    if (scenario?.state === 'running' && !running)
      applyRunning({
        id: scenario.id,
        code: scenario.code,
        name: scenario.name,
        city: scenario.city,
        startView: null,
      });
    else if (scenario?.state === 'idle' && running) applyRunning(null);
  };

  // The layer draws the refusal tag but does not translate; this is the one
  // string it is handed rather than deriving.
  layer.setDeclinedLabel?.(t(DECLINED_TAG));

  const unsubscribe = layer.subscribeFeed?.(() => render()) ?? (() => {});
  removers.push(unsubscribe);
  const refresh = setInterval(render, IDLE_REFRESH_MS);
  removers.push(() => clearInterval(refresh));

  // The layer is what owns the socket, so nothing above reports until it is
  // on. Failure is logged and left: the console still works, with a feed
  // chip that says the feed is down.
  void Promise.resolve(setLayerEnabled(true)).catch((error) =>
    console.warn('[HeadsUp] Could not enable the Heads Up layer:', error),
  );
  render();

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    controller.abort();
    for (const remove of removers.splice(0)) {
      try {
        remove();
      } catch (error) {
        console.warn('[HeadsUp] Console teardown step failed:', error);
      }
    }
    for (const node of owned.splice(0)) node.remove();
    // Hand the two retitled headers back exactly as they were found.
    if (dataTitle && previousDataTitle !== null)
      dataTitle.textContent = previousDataTitle;
    if (sceneTitle && previousSceneTitle !== null)
      sceneTitle.textContent = previousSceneTitle;
    cohortRows.clear();
    counterValues.clear();
  }

  return { destroy };
}
