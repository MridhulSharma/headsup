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
 * | (new) `#headsup-pipeline-rail` | the agent rail and its stage cards |
 * | (new) `#headsup-scene-picker` | the scenario dialog |
 *
 * Every string a person reads goes through `t()`. Every string that came off
 * the feed — a scenario name, a city, an operator id, a critic's reason, a
 * dispatch message — is written with `textContent`, never markup, and is
 * bounded by the grammar in `./scenarios.js` and
 * `../layers/headsup/records.js` before it arrives here.
 *
 * **The rail is event-driven, never timed.** Every row, chip and card is
 * rendered from `./pipeline.js`, which reads the socket's own transcript: a
 * stage is lit because a frame for it arrived. There is no animation, no
 * progress bar and no stage that advances on a clock, because the rail's
 * whole job is to be a record of what the agents actually did.
 * @module headsup/console
 */

import { msg, t } from '../i18n/index.js';
import {
  FEED_LABELS,
  PIPELINE_STAGES,
  SPARKLINE_SAMPLES,
  buildPipelineView,
  sampleAssistQueue,
} from './pipeline.js';
import { ScenarioApiError } from './scenarios.js';

export { PIPELINE_STAGES } from './pipeline.js';

/**
 * The counter strip, along the bottom right.
 *
 * Four readings, and three of them are a COMPARISON rather than a number:
 * the whole claim Heads Up makes is that the advised fleet does better than
 * the same fleet without advisories, and a single total would hide exactly
 * that. A `pair` cell therefore draws the live cohort against the ghost one.
 *
 * `read` and `pair` take the pipeline view (`./pipeline.js`), so nothing on
 * this strip is derived from anything but frames that arrived — and a tally
 * the feed has not reported shows as `—` rather than as zero, because "no
 * vehicle entered the hazard" and "nobody has told us" are different claims.
 */
export const HEADSUP_COUNTERS = Object.freeze([
  Object.freeze({
    key: 'entries',
    label: msg('HAZARD ENTRIES'),
    pair: (view) => view.counters.hazardEntries,
  }),
  Object.freeze({
    key: 'rerouted',
    label: msg('REROUTED'),
    read: (view) => String(view.counters.rerouted ?? 0),
  }),
  Object.freeze({
    key: 'advisory',
    label: msg('TIME TO ADVISORY'),
    read: (view, { duration }) => duration(view.counters.timeToAdvisoryMs),
  }),
  Object.freeze({
    key: 'assists',
    label: msg('ASSIST QUEUE'),
    pair: (view) => view.counters.assistQueue,
    /** The one cell that also carries the last few seconds of its own value. */
    spark: true,
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
 * @param {object} options.scenarios Scenario API client (`./scenarios.js`),
 *   which also carries the approval call the Compiler card makes.
 * @param {Document} [options.doc]
 * @param {() => number} [options.now] Clock seam, for tests.
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
  now = () => Date.now(),
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
      // The colour arrives from the layer on the first render: the ghost
      // cohort's red is the layer's to choose, and a swatch that disagreed
      // with the dots would be worse than no swatch.
      name: t('Ghost fleet'),
      onToggle: (shown) => layer.setCohortVisibility?.({ ghost: shown }),
    });
  }

  // ── Left panel: the legend ──────────────────────────────────────────────
  //
  // Only what the panel does not already say. The cohort rows above carry a
  // swatch per FLEET, so the legend's job is the colours nobody can work out
  // from the globe alone — one per hazard class actually drawn — plus the
  // marks whose meaning is not self-evident: the heading chevron, the
  // deflection trail, the assist ring and the refusal tag. Everything here
  // is read from what is DRAWN, so the legend cannot name a colour that is
  // not on screen.
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
    return { row, count, swatch };
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
  const assistMark = legend
    ? legendRow(legend, {
        mark: '◎',
        glyph: true,
        color: '#ffd166',
        name: t('Remote assist'),
      })
    : null;
  const declinedMark = legend
    ? legendRow(legend, {
        mark: '◆',
        glyph: true,
        color: '#ef476f',
        name: t('Declined'),
      })
    : null;
  // A row with nothing to report would be a control-panel light for a
  // condition that is not happening.
  if (assistMark) assistMark.row.hidden = true;
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
  /** @type {Map<string, object>} One entry per counter cell. */
  const counterCells = new Map();
  /** @type {Array<{at: number, live: number, ghost: number}>} */
  const sparkSamples = [];
  /** @type {Array<{live: HTMLElement, ghost: HTMLElement}>} */
  const sparkColumns = [];

  /**
   * One sparkline column: the ghost queue behind the live one.
   *
   * Two bars per column rather than a stack, because the two cohorts are a
   * COMPARISON and not a total — stacking them would draw a number nobody
   * is claiming. Heights are CSS custom properties, so a sample costs two
   * property writes and no layout of ours.
   */
  const sparkColumn = (parent) => {
    const column = element('div', 'headsup-spark-col', parent);
    column.setAttribute('aria-hidden', 'true');
    return {
      ghost: element('span', 'headsup-spark-ghost', column),
      live: element('span', 'headsup-spark-live', column),
    };
  };

  if (strip)
    for (const counter of HEADSUP_COUNTERS) {
      const cell = element('div', 'headsup-counter', strip);
      cell.dataset.counter = counter.key;
      const value = element('strong', 'headsup-counter-value', cell);
      const entry = { value, live: null, ghost: null };
      if (counter.pair) {
        // Three nodes, so the stylesheet can colour the ghost side on its
        // own: the comparison is the reading, not the pair of digits.
        entry.live = element('span', 'headsup-counter-live', value);
        entry.live.textContent = '—';
        const separator = element('span', 'headsup-counter-split', value);
        separator.textContent = '/';
        separator.setAttribute('aria-hidden', 'true');
        entry.ghost = element('span', 'headsup-counter-ghost', value);
        entry.ghost.textContent = '—';
      } else value.textContent = '0';
      const name = element('span', 'headsup-counter-label', cell);
      name.textContent = t(counter.label);
      if (counter.spark) {
        const spark = element('div', 'headsup-spark', cell);
        spark.title = t('Assist queue, last {count} seconds', {
          count: SPARKLINE_SAMPLES,
        });
        for (let index = 0; index < SPARKLINE_SAMPLES; index++)
          sparkColumns.push(sparkColumn(spark));
      }
      counterCells.set(counter.key, entry);
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

  /** Seconds, or minutes and seconds; `—` while there is no answer yet. */
  const formatDuration = (ms) => {
    if (!Number.isFinite(ms) || ms < 0) return '—';
    const seconds = ms / 1000;
    if (seconds < 60) return t('{seconds}s', { seconds: seconds.toFixed(1) });
    return t('{minutes}m {seconds}s', {
      minutes: Math.floor(seconds / 60),
      seconds: String(Math.round(seconds % 60)).padStart(2, '0'),
    });
  };

  const renderCounters = (view) => {
    for (const counter of HEADSUP_COUNTERS) {
      const cell = counterCells.get(counter.key);
      if (!cell) continue;
      if (counter.pair) {
        const pair = counter.pair(view);
        cell.live.textContent = pair ? String(pair.live) : '—';
        cell.ghost.textContent = pair ? String(pair.ghost) : '—';
      } else
        cell.value.textContent = counter.read(view, {
          duration: formatDuration,
        });
    }
    if (!sparkColumns.length) return;
    sampleAssistQueue(sparkSamples, view.counters.assistQueue, now());
    // Scaled to the window's own peak, with a floor of one: a queue of two
    // against a peak of two is a full bar, which is honest for a sparkline
    // whose job is the SHAPE, and the cell beside it carries the number.
    let peak = 1;
    for (const sample of sparkSamples)
      peak = Math.max(peak, sample.live, sample.ghost);
    // Oldest column first, and right-aligned: the newest sample is always
    // the rightmost bar, so a half-filled window grows from the right.
    const offset = sparkColumns.length - sparkSamples.length;
    sparkColumns.forEach((column, index) => {
      const sample = index >= offset ? sparkSamples[index - offset] : null;
      const height = (value) =>
        sample ? `${Math.round((value / peak) * 100)}%` : '0%';
      column.live.style.setProperty(
        '--headsup-spark-height',
        height(sample?.live ?? 0),
      );
      column.ghost.style.setProperty(
        '--headsup-spark-height',
        height(sample?.ghost ?? 0),
      );
    });
  };

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
  // What this run of the pipeline is ABOUT: the hazard its stages name, which
  // is the id that ties every row here to the geometry on the globe.
  const railLink = element('span', 'headsup-rail-link', railHeader);
  railLink.textContent = '';
  const railStages = element('ol', 'headsup-rail-stages', rail);
  /** @type {Map<string, object>} One entry per stage row. */
  const stageRows = new Map();

  /**
   * One rail row: a marker, a name, and the one short value it carries.
   *
   * A button, because clicking it opens that stage's card. `aria-expanded`
   * is what tells a screen reader the row discloses something, and
   * `aria-controls` points at the card the click fills.
   */
  const railRow = (parent, { key, name }) => {
    const item = element('li', 'headsup-rail-item', parent);
    const row = element('button', 'headsup-rail-stage', item);
    row.type = 'button';
    row.dataset.stage = key;
    row.setAttribute('aria-expanded', 'false');
    row.setAttribute('aria-controls', 'headsup-rail-card');
    const marker = element('span', 'headsup-rail-marker', row);
    marker.setAttribute('aria-hidden', 'true');
    const nameNode = element('span', 'headsup-rail-stage-name', row);
    nameNode.textContent = name;
    const value = element('span', 'headsup-rail-stage-value', row);
    value.textContent = '';
    on(row, 'click', () => selectCard(key));
    const entry = { item, row, value, nameNode };
    stageRows.set(key, entry);
    return entry;
  };

  for (const stage of PIPELINE_STAGES)
    railRow(railStages, { key: stage.key, name: t(stage.label) });

  const ackHeading = element('h3', 'headsup-rail-group', rail);
  ackHeading.textContent = t('FLEET ANSWERS');
  const railAcks = element('ol', 'headsup-rail-stages', rail);
  /** The operator rows the rail was last built for. */
  let renderedAcks = '';
  const railEmpty = element('p', 'headsup-rail-empty', rail);
  railEmpty.textContent = t('No fleet has answered yet.');
  const card = element('section', 'headsup-rail-card', rail);
  card.id = 'headsup-rail-card';
  card.hidden = true;
  const cardHeader = element('header', 'headsup-rail-card-header', card);
  const cardTitle = element('h3', 'headsup-rail-card-title', cardHeader);
  const cardClose = element('button', 'headsup-rail-card-close', cardHeader);
  cardClose.type = 'button';
  cardClose.textContent = '×';
  cardClose.title = t('Close this stage');
  cardClose.setAttribute('aria-label', t('Close this stage'));
  const cardBody = element('div', 'headsup-rail-card-body', card);

  /** Which stage's card is open, or null. */
  let selected = null;
  /** What the open card was last built from. */
  let cardSignature = '';
  /** The Approve button and its status line, while a card holds them. */
  let approveButton = null;
  let approveStatus = null;
  let approveBusy = false;
  /** The last thing the approval call said, as one line a person reads. */
  let approveMessage = '';

  const selectCard = (key) => {
    selected = selected === key ? null : key;
    // A new selection rebuilds, and so does closing one: the signature is
    // what stops a passing frame from rebuilding the card under a pointer.
    cardSignature = '';
    approveMessage = '';
    render();
    if (selected) card.querySelector('button')?.focus();
  };

  on(cardClose, 'click', () => selectCard(selected));

  // ── Card contents ───────────────────────────────────────────────────────

  /** One `label: value` line. Feed text goes in as text, never as markup. */
  const cardRow = (parent, label, value) => {
    if (value == null || value === '') return null;
    const row = element('div', 'headsup-card-row', parent);
    const name = element('span', 'headsup-card-key', row);
    name.textContent = label;
    const content = element('span', 'headsup-card-value', row);
    content.textContent = String(value);
    return row;
  };

  /** A heading over a block, only when the block has something in it. */
  const cardGroup = (parent, label) => {
    const heading = element('h4', 'headsup-card-group', parent);
    heading.textContent = label;
    return element('ul', 'headsup-card-list', parent);
  };

  const cardItem = (parent, text, { tone } = {}) => {
    const item = element('li', 'headsup-card-item', parent);
    if (tone) item.dataset.tone = tone;
    item.textContent = text;
    return item;
  };

  const cardParagraph = (parent, text) => {
    if (!text) return null;
    const paragraph = element('p', 'headsup-card-text', parent);
    paragraph.textContent = text;
    return paragraph;
  };

  /** A feed's display name, or the id it arrived as. */
  const feedName = (source) =>
    FEED_LABELS[source] ? t(FEED_LABELS[source]) : source;

  /** The evidence rows of one record, as a list a person can check. */
  const evidenceList = (parent, rows) => {
    if (!rows?.length) return;
    const list = cardGroup(parent, t('EVIDENCE'));
    for (const row of rows) {
      const item = element('li', 'headsup-card-item', list);
      const chip = element('span', 'headsup-card-chip', item);
      chip.textContent = feedName(row.source);
      const text = element('span', null, item);
      // The kind and the note are feed text, shown as they arrived.
      text.textContent = row.note
        ? t('{kind} — {note}', { kind: row.kind ?? row.source, note: row.note })
        : (row.kind ?? row.source);
    }
  };

  /**
   * The Sentinels card's two halves, for a sentinel that read a message.
   *
   * The raw dispatch text sits BESIDE the fields the agent says it pulled
   * out of it, because the one thing a person needs to be able to do with a
   * text agent is check it: a list of extracted fields with no message next
   * to it is an assertion, and the message alone is work nobody has done.
   * Both sides are feed text and are written as text.
   */
  const dispatchSplit = (parent, record) => {
    if (!record?.rawText) return;
    const split = element('div', 'headsup-card-split', parent);
    const rawSide = element('div', 'headsup-card-raw-side', split);
    const rawHeading = element('h4', 'headsup-card-group', rawSide);
    rawHeading.textContent = t('DISPATCH TEXT');
    const raw = element('pre', 'headsup-card-raw', rawSide);
    raw.textContent = record.rawText;
    const fieldSide = element('div', 'headsup-card-field-side', split);
    const fieldHeading = element('h4', 'headsup-card-group', fieldSide);
    fieldHeading.textContent = t('EXTRACTED');
    const fields = element('dl', 'headsup-card-fields', fieldSide);
    for (const field of record.extracted) {
      const key = element('dt', null, fields);
      key.textContent = field.field;
      const value = element('dd', null, fields);
      value.textContent = field.value;
    }
  };

  /** The Compiler card's Approve button, when one advisory is held. */
  const approvalBlock = (parent, pending) => {
    const block = element('div', 'headsup-card-approve', parent);
    const note = element('p', 'headsup-card-text', block);
    note.textContent = t(
      'This advisory needs a second key before it can be published.',
    );
    approveButton = element('button', 'headsup-card-approve-btn', block);
    approveButton.type = 'button';
    approveButton.textContent = t('APPROVE');
    approveButton.title = t('Supply the second key for this advisory');
    approveStatus = element('p', 'headsup-card-status', block);
    approveStatus.setAttribute('role', 'status');
    approveStatus.textContent = approveMessage;
    on(approveButton, 'click', () => void approve(pending.proposalId));
  };

  /**
   * Supply the second key.
   *
   * The button is disabled for the length of the call and then left alone:
   * what re-enables the pipeline is the COMPILER's next frame, not this
   * call returning. A console that redrew itself as "published" on a 200
   * would be asserting a publication it has not seen.
   */
  const approve = async (proposalId) => {
    if (approveBusy || !proposalId) return;
    approveBusy = true;
    if (approveButton) {
      approveButton.disabled = true;
      approveButton.setAttribute('aria-busy', 'true');
    }
    approveMessage = t('Sending the approval…');
    if (approveStatus) approveStatus.textContent = approveMessage;
    try {
      await scenarios.approve(proposalId, { signal: controller.signal });
      if (destroyed || controller.signal.aborted) return;
      approveMessage = t('Approved. Waiting for the compiler.');
    } catch (error) {
      if (destroyed || controller.signal.aborted) return;
      approveMessage = describeScenarioError(error);
      if (approveButton) {
        approveButton.disabled = false;
        approveButton.removeAttribute('aria-busy');
      }
    } finally {
      approveBusy = false;
      if (approveStatus) approveStatus.textContent = approveMessage;
    }
  };

  /** What the open card says, built from the stage's own records. */
  const buildCard = (view) => {
    approveButton = null;
    approveStatus = null;
    cardBody.replaceChildren();
    if (!selected) return;
    if (selected.startsWith('ack:')) {
      const op = selected.slice(4);
      const answer = view.operators.find((row) => row.op === op);
      cardTitle.textContent = op;
      if (!answer) {
        cardParagraph(cardBody, t('This fleet has not answered.'));
        return;
      }
      cardRow(cardBody, t('Answer'), answer.status);
      cardRow(cardBody, t('Vehicles'), answer.vehicles);
      cardRow(cardBody, t('Advisory'), answer.linkedId);
      cardParagraph(cardBody, answer.actionTaken);
      if (answer.reason) {
        const list = cardGroup(cardBody, t('REASON'));
        cardItem(list, answer.reason, { tone: answer.tone });
      }
      return;
    }
    const stage = view.stages.find((row) => row.key === selected);
    const stageLabel = PIPELINE_STAGES.find((row) => row.key === selected);
    cardTitle.textContent = stageLabel ? t(stageLabel.label) : selected;
    if (!stage?.lit) {
      cardParagraph(cardBody, t('This stage has not reported yet.'));
      return;
    }
    const latest = stage.latest;
    if (selected === 'feeds') {
      const list = cardGroup(cardBody, t('SOURCES'));
      for (const chip of stage.records) {
        const item = element('li', 'headsup-card-item', list);
        const name = element('span', 'headsup-card-chip', item);
        name.textContent = feedName(chip.source);
        // The count on its own, in a column, the way the legend rows read:
        // a number beside a name needs no sentence around it, and one less
        // sentence is one less plural rule to get wrong in five languages.
        const count = element('span', 'headsup-card-outcome', item);
        count.textContent = String(chip.events);
      }
      return;
    }
    if (selected === 'sentinel') {
      cardRow(cardBody, t('Sentinel'), latest.sentinel);
      cardRow(cardBody, t('Reading'), latest.hazardClass);
      cardRow(
        cardBody,
        t('Confidence'),
        latest.confidence == null
          ? null
          : t('{percent}%', { percent: Math.round(latest.confidence * 100) }),
      );
      cardParagraph(cardBody, latest.note);
      dispatchSplit(cardBody, latest);
      evidenceList(cardBody, latest.evidence);
      return;
    }
    if (selected === 'fusion') {
      cardRow(cardBody, t('Hazard'), latest.id);
      cardRow(cardBody, t('Class'), latest.hazardClass);
      cardRow(cardBody, t('Severity'), latest.severity);
      cardRow(
        cardBody,
        t('Confidence'),
        latest.confidence == null
          ? null
          : t('{percent}%', { percent: Math.round(latest.confidence * 100) }),
      );
      cardParagraph(cardBody, latest.rationale);
      evidenceList(cardBody, latest.evidence);
      return;
    }
    if (selected === 'proposer') {
      cardRow(cardBody, t('Action'), latest.action);
      cardRow(cardBody, t('Playbook'), latest.playbook);
      cardRow(cardBody, t('Policy rule'), latest.policyRule);
      cardRow(cardBody, t('Hazard'), latest.hazardId);
      cardParagraph(cardBody, latest.rationale);
      // Every round, when the critic sent one back: a revision is only
      // explainable beside the proposal it revised.
      if (stage.records.length > 1) {
        const list = cardGroup(cardBody, t('ROUNDS'));
        for (const record of stage.records)
          cardItem(
            list,
            t('{revision}. {action} — {id}', {
              revision: record.revision,
              action: record.action,
              id: record.id,
            }),
          );
      }
      return;
    }
    if (selected === 'critic') {
      cardRow(cardBody, t('Verdict'), latest.decision);
      cardRow(cardBody, t('Proposal'), latest.proposalId);
      cardParagraph(cardBody, latest.rationale);
      if (latest.checks.length) {
        const list = cardGroup(cardBody, t('CHECKS'));
        for (const check of latest.checks) {
          const item = element('li', 'headsup-card-item', list);
          item.dataset.tone = check.passed ? 'ok' : 'alert';
          const glyph = element('span', 'headsup-card-glyph', item);
          glyph.textContent = check.passed ? '✓' : '✗';
          glyph.setAttribute('aria-hidden', 'true');
          const name = element('span', null, item);
          // A rule id is contract text, shown as the playbook spells it.
          name.textContent = check.rule;
          // The word as well as the tick, because the tick is aria-hidden and
          // the colour is the only other thing carrying this. Built into a
          // variable rather than written inline: `materialSymbolsSubset`
          // reads lower-case literals assigned straight to `textContent` as
          // icon-font glyph names, and these two are words.
          const outcomeText = check.passed ? t('passed') : t('failed');
          const outcome = element('span', 'headsup-card-outcome', item);
          outcome.textContent = outcomeText;
        }
      }
      if (latest.reasons.length) {
        const list = cardGroup(cardBody, t('REASONS'));
        for (const reason of latest.reasons)
          cardItem(list, reason, { tone: 'alert' });
      }
      return;
    }
    if (selected === 'compiler') {
      cardRow(cardBody, t('Result'), latest.status);
      cardRow(cardBody, t('Advisory'), latest.advisoryId);
      cardRow(cardBody, t('Proposal'), latest.proposalId);
      cardRow(cardBody, t('Policy rule'), latest.policyRule);
      cardRow(cardBody, t('Issuer'), latest.issuer);
      // Three states, and the third one is the honest default: this console
      // cannot verify a signature, so it reports what the issuer claimed and
      // never upgrades silence into a yes.
      cardRow(
        cardBody,
        t('Signature'),
        latest.signatureVerified === true
          ? t('verified')
          : latest.signatureVerified === false
            ? t('NOT VERIFIED')
            : latest.signed
              ? t('present, unverified')
              : t('absent'),
      );
      if (latest.reasons.length) {
        const list = cardGroup(cardBody, t('REASONS'));
        for (const reason of latest.reasons)
          cardItem(list, reason, { tone: 'alert' });
      }
      if (view.pending) approvalBlock(cardBody, view.pending);
      return;
    }
  };

  /**
   * Draw the rail.
   *
   * Stage rows exist from the start and only change state; the fleet rows
   * are rebuilt when the operator SET changes, and the card when its own
   * contents do. None of the three is rebuilt by a passing frame, because a
   * row rebuilt under a pointer is a row that cannot be clicked.
   */
  const renderRail = (view) => {
    railLink.textContent = view.hazardId
      ? view.hazardClass
        ? t('{class} · {id}', { class: view.hazardClass, id: view.hazardId })
        : view.hazardId
      : '';
    railLink.title = view.advisoryId
      ? t('Advisory {id}', { id: view.advisoryId })
      : '';
    for (const stage of view.stages) {
      const entry = stageRows.get(stage.key);
      if (!entry) continue;
      entry.row.dataset.tone = stage.tone;
      entry.row.dataset.lit = stage.lit ? 'true' : 'false';
      entry.value.textContent = stage.value ?? '';
      entry.row.setAttribute(
        'aria-expanded',
        selected === stage.key ? 'true' : 'false',
      );
      // The id this stage's work belongs to, so a row on the rail and a
      // shape on the globe can be matched up without opening the card.
      entry.row.title = stage.linkedId ?? '';
    }
    const signature = view.operators
      .map((row) => `${row.op}:${row.status}`)
      .join(',');
    if (signature !== renderedAcks) {
      renderedAcks = signature;
      for (const key of [...stageRows.keys()])
        if (key.startsWith('ack:')) stageRows.delete(key);
      railAcks.replaceChildren();
      for (const answer of view.operators) {
        // An operator id is feed text: shown, never interpolated into markup.
        const entry = railRow(railAcks, {
          key: `ack:${answer.op}`,
          name: answer.op,
        });
        entry.row.dataset.tone = answer.tone;
        entry.row.dataset.lit = 'true';
        entry.value.textContent = answer.status;
      }
    } else
      for (const answer of view.operators) {
        const entry = stageRows.get(`ack:${answer.op}`);
        if (!entry) continue;
        entry.row.dataset.tone = answer.tone;
        entry.value.textContent = answer.status;
        entry.row.setAttribute(
          'aria-expanded',
          selected === `ack:${answer.op}` ? 'true' : 'false',
        );
      }
    railEmpty.hidden = view.operators.length > 0;

    const open = selected != null;
    card.hidden = !open;
    const stage = view.stages.find((row) => row.key === selected);
    const latest = stage?.latest;
    // What the open card is built from, so a passing frame does not rebuild
    // it under a pointer. The three answer fields are in here beside the
    // arrival time because a stage can change its ANSWER about the same
    // record — the compiler holds an advisory and then signs it — and a card
    // keyed on arrival alone would go on showing the first word.
    const nextSignature = open
      ? [
          selected,
          stage?.records.length ?? 0,
          latest?.at ?? '',
          latest?.status ?? latest?.decision ?? '',
          String(latest?.signatureVerified ?? ''),
          view.operators.find((row) => `ack:${row.op}` === selected)?.status ??
            '',
          view.pending?.proposalId ?? '',
        ].join('|')
      : '';
    if (nextSignature !== cardSignature) {
      cardSignature = nextSignature;
      buildCard(view);
    } else if (approveStatus) approveStatus.textContent = approveMessage;
  };

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
      const scenarioCard = element('button', 'headsup-picker-card', pickerList);
      scenarioCard.type = 'button';
      const heading = element('span', 'headsup-picker-card-name', scenarioCard);
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
        const whatIf = element(
          'span',
          'headsup-picker-card-whatif',
          scenarioCard,
        );
        whatIf.textContent = scenario.whatIf;
      }
      on(
        scenarioCard,
        'click',
        () => void beginScenario(scenario, scenarioCard),
      );
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
    if (!scenario) {
      // A rail that is going away takes its open card with it, so the next
      // replay does not start with the last one's reasoning on screen. The
      // card is closed here rather than on the next render: nothing is
      // guaranteed to render between a replay ending and the one after it,
      // and a hidden rail holding an open card is two states disagreeing.
      selected = null;
      cardSignature = '';
      approveMessage = '';
      approveButton = null;
      approveStatus = null;
      card.hidden = true;
      cardBody.replaceChildren();
      for (const row of stageRows.values())
        row.row.setAttribute('aria-expanded', 'false');
      sparkSamples.length = 0;
    }
  };

  const beginScenario = async (scenario, scenarioCard) => {
    if (pickerBusy) return;
    pickerBusy = true;
    scenarioCard?.setAttribute('aria-busy', 'true');
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
      scenarioCard?.removeAttribute('aria-busy');
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
   * Escape closes the picker, then the open card, then ends the replay.
   *
   * Only ever acted on when one of those three is actually open, and the
   * event is left alone otherwise: upstream's own Escape handlers (the
   * cockpit, the draw tool, the street-level viewer) must keep working in
   * Heads Up mode.
   */
  const onKeyDown = (event) => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    if (!picker.hidden) {
      event.preventDefault();
      event.stopPropagation();
      closePicker();
      return;
    }
    if (selected) {
      event.preventDefault();
      event.stopPropagation();
      selectCard(selected);
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
    const view = buildPipelineView({ snapshot: feed, stats });
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
    renderCounters(view);
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
    if (assistMark) {
      // Requests, not markers: the badge on the globe says how many a
      // cluster collapsed, and this row says how many there are in all.
      assistMark.count.textContent = stats.assists ? String(stats.assists) : '';
      assistMark.row.hidden = !stats.assists;
    }
    if (declinedMark) {
      declinedMark.count.textContent = stats.declined
        ? String(stats.declined)
        : '';
      declinedMark.row.hidden = !stats.declined;
    }
    renderFleetRows(layer.getOperatorLegend?.() ?? []);
    const ghost = layer.getGhostCohort?.() ?? { vehicles: 0, visible: true };
    setRowState('ghost', {
      count: ghost.vehicles,
      visible: ghost.visible,
      color: ghost.color,
    });
    const cohorts = layer.getCohortVisibility?.() ?? {};
    setRowState('hazards', {
      count: Math.max(0, (stats.advisories ?? 0) - (stats.corridors ?? 0)),
      visible: cohorts.hazards !== false,
    });
    setRowState('corridors', {
      count: stats.corridors ?? 0,
      visible: cohorts.corridors !== false,
    });
    renderRail(view);
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

  // The layer draws the refusal tag and the assist badge but does not
  // translate; these are the two strings it is handed rather than deriving.
  layer.setDeclinedLabel?.(t(DECLINED_TAG));
  layer.setAssistLabel?.((count) => t('ASSIST ×{count}', { count }));

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
  // The first render happens here rather than on the first frame, so the
  // console is never briefly blank. If it throws, the subscription and the
  // refresh timer above are already live and nobody holds the handle that
  // would stop them, so this tears itself down before rethrowing.
  try {
    render();
  } catch (error) {
    destroy();
    throw error;
  }

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
    counterCells.clear();
    stageRows.clear();
    sparkColumns.length = 0;
    sparkSamples.length = 0;
  }

  return { destroy };
}
