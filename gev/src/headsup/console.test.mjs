import assert from 'node:assert/strict';
import test from 'node:test';
import { consoleTestDocument } from '../testSupport/headsupConsoleDocument.mjs';
import { startHeadsupConsole } from './console.js';
import { createHeadsupModeConfig } from './mode.js';

const SCENARIO = Object.freeze({
  id: 's1_flood',
  code: 'S1',
  name: 'Flash flood',
  city: 'San Antonio',
  whatIf: 'What if a creek crossing floods under a routed fleet?',
  speed: 60,
  scripted: true,
  startView: Object.freeze({
    lon: -98.396,
    lat: 29.5228,
    height: 900,
    heading: 20,
    pitch: -34,
  }),
});

/** A Heads Up layer that records what the console asked it to do. */
function fakeLayer({
  stats = {},
  feed = null,
  operators = [],
  hazards = [],
} = {}) {
  const calls = {
    cohorts: [],
    cleared: 0,
    enabled: [],
    declinedLabels: [],
    assistLabels: [],
  };
  let listener = null;
  return {
    calls,
    publish: () => listener?.(feed),
    layer: {
      getStats: () => ({
        vehicles: 0,
        advisories: 0,
        corridors: 0,
        ghosts: 0,
        rerouted: 0,
        connected: true,
        ...stats,
      }),
      getFeedSnapshot: () => feed,
      getOperatorLegend: () => operators,
      getHazardLegend: () => hazards,
      setDeclinedLabel: (title) => calls.declinedLabels.push(title),
      setAssistLabel: (format) => calls.assistLabels.push(format),
      getGhostCohort: () => ({ color: '#ff5a52', vehicles: 0, visible: true }),
      getCohortVisibility: () => ({ hazards: true, corridors: true }),
      setCohortVisibility: (patch) => calls.cohorts.push(patch),
      clearDrawn: () => calls.cleared++,
      subscribeFeed: (next) => {
        listener = next;
        return () => {
          listener = null;
        };
      },
    },
  };
}

/** A scenario API that answers from memory and records every call. */
function fakeScenarios({
  listing = [SCENARIO],
  failList = null,
  failStart = null,
  failApprove = null,
} = {}) {
  const calls = [];
  return {
    calls,
    client: {
      baseUrl: 'http://localhost:8765',
      list: async () => {
        calls.push('list');
        if (failList) throw failList;
        return listing;
      },
      start: async (id) => {
        calls.push(`start:${id}`);
        if (failStart) throw failStart;
        return { state: 'running', scenario: SCENARIO };
      },
      stop: async () => {
        calls.push('stop');
        return { state: 'idle', scenario: null };
      },
      approve: async (proposalId) => {
        calls.push(`approve:${proposalId}`);
        if (failApprove) throw failApprove;
        return { state: 'approved' };
      },
    },
  };
}

function mount(options = {}) {
  const fixture = consoleTestDocument();
  const flights = [];
  const layer = options.layer ?? fakeLayer();
  const scenarios = options.scenarios ?? fakeScenarios();
  const hudCalls = [];
  const hud = {
    setMode: (value) => hudCalls.push(value),
    summaryPolicy: {},
  };
  const console_ = startHeadsupConsole({
    mode: createHeadsupModeConfig(),
    layer: layer.layer,
    setLayerEnabled: async (enabled) => layer.calls.enabled.push(enabled),
    hud,
    flyTo: (view) => flights.push(view),
    scenarios: scenarios.client,
    doc: fixture.document,
  });
  return {
    ...fixture,
    // Everything this console says now lives in one dock beside the globe,
    // and everything a person can press lives in its control panel.
    dock: fixture.document.getElementById('headsup-dock'),
    controls: fixture.document.getElementById('headsup-controls'),
    console: console_,
    layer,
    scenarios,
    flights,
    hud,
    hudCalls,
  };
}

test('the console builds its own dock and touches no upstream node', (t) => {
  const page = mount();
  t.after(() => page.console.destroy());
  // The dock's four panes. Each is placed by NAME in the stylesheet, so this
  // is the order they are built in, not the order they are read on screen.
  assert.deepEqual(
    page.dock.children.map((pane) => pane.className),
    [
      'headsup-block headsup-controls',
      'headsup-rail headsup-block',
      'headsup-block headsup-strip',
      'headsup-block headsup-advisory',
    ],
  );
  // The control panel: the scenario picker over the cohort toggles, with the
  // four Heads Up cohorts in the order the build step names them.
  assert.deepEqual(
    page.controls.children.map((group) => group.className),
    [
      'headsup-control-group headsup-scenes',
      'headsup-control-group headsup-layers',
    ],
  );
  assert.deepEqual(
    page.controls
      .querySelectorAll('.headsup-cohort')
      .map((row) => row.dataset.cohort),
    ['hazards', 'corridors', 'ghost'],
  );
  assert.ok(page.controls.querySelector('.headsup-scene-list'));
  assert.ok(page.controls.querySelector('.headsup-readout'));
  assert.ok(page.dock.querySelector('.headsup-counters'));
  assert.ok(page.document.getElementById('headsup-pipeline-rail'));
  assert.ok(page.document.getElementById('headsup-status'));
  assert.ok(page.document.getElementById('headsup-advisory'));
  assert.ok(page.document.getElementById('headsup-banner'));
  // The four surfaces that hosted these before the dock existed are left
  // exactly as upstream built them, not emptied, retitled or rebuilt: the
  // stylesheet hides them, which is what keeps a subtree pull a merge.
  assert.equal(page.dataTitle.textContent, 'DATA LAYERS');
  assert.equal(page.dataToggles.children.length, 0);
  assert.equal(page.styleIndicator.querySelector('.headsup-readout'), null);
  assert.equal(page.hudContent.children.length, 0);
  assert.equal(page.sceneTitle.textContent, 'SCENES');
  assert.equal(page.sceneInner.querySelector('.headsup-scene-list'), null);
});

test('upstream nodes other modules still write to are left in place', (t) => {
  const page = mount();
  t.after(() => page.console.destroy());
  // The stylesheet hides this; removing it would break visualSettings, which
  // writes the active style name into it by id on every style change.
  assert.equal(page.activeStyle.parent, page.styleIndicator);
  assert.equal(page.activeStyle.textContent, 'NORMAL');
});

test('the HUD frame is held on and its summary is closed', (t) => {
  const page = mount();
  t.after(() => page.console.destroy());
  assert.deepEqual(page.hudCalls, ['on']);
  assert.equal(page.hud.summaryPolicy.canRequest(), false);
});

test('the Heads Up layer is switched on so the socket exists', (t) => {
  const page = mount();
  t.after(() => page.console.destroy());
  assert.deepEqual(page.layer.calls.enabled, [true]);
});

test('a cohort row toggles exactly its own cohort', (t) => {
  const page = mount();
  t.after(() => page.console.destroy());
  const rows = page.controls.querySelectorAll('.headsup-cohort');
  const hazards = rows.find((row) => row.dataset.cohort === 'hazards');
  assert.equal(hazards.getAttribute('aria-pressed'), 'true');
  hazards.click();
  assert.equal(hazards.getAttribute('aria-pressed'), 'false');
  assert.deepEqual(page.layer.calls.cohorts, [{ hazards: false }]);
  hazards.click();
  assert.deepEqual(page.layer.calls.cohorts.at(-1), { hazards: true });
});

test('one row per operator appears as fleets report, and toggles that fleet', (t) => {
  const layer = fakeLayer({
    operators: [
      { op: 'op_a', color: '#38f0c0', vehicles: 6, rerouted: 6, visible: true },
      { op: 'op_b', color: '#ffd166', vehicles: 4, rerouted: 4, visible: true },
    ],
  });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  const fleets = page.controls
    .querySelectorAll('.headsup-cohort')
    .filter((row) => row.dataset.cohort.startsWith('op:'));
  assert.deepEqual(
    fleets.map((row) => row.querySelector('.headsup-cohort-label').textContent),
    ['op_a', 'op_b'],
  );
  assert.deepEqual(
    fleets.map((row) => row.querySelector('.headsup-cohort-count').textContent),
    ['6', '4'],
  );
  fleets[0].click();
  assert.deepEqual(page.layer.calls.cohorts, [{ operators: { op_a: false } }]);
});

test('the readout and the counter strip report the feed, not their own guess', (t) => {
  const layer = fakeLayer({
    stats: {
      vehicles: 16,
      ghosts: 6,
      rerouted: 10,
      advisories: 1,
      corridors: 0,
      connected: true,
    },
    feed: {
      scenario: {
        state: 'running',
        id: 's1_flood',
        code: 'S1',
        name: 'Flash flood',
        city: 'San Antonio',
        simTime: Date.parse('2026-04-20T18:50:00Z'),
        simTimeText: '2026-04-20T18:50:00Z',
        elapsedS: 7.5,
        speed: 60,
        scripted: true,
      },
      pipeline: {
        tallies: {},
        hazardCount: 1,
        acks: [],
        candidates: [],
        hazards: [],
        proposals: [],
        verdicts: [],
        compiles: [],
        assists: [],
        counters: {
          hazardEntries: { live: 0, ghost: 7 },
          assistQueue: { live: 0, ghost: 3 },
        },
        firstSignalAt: 1_000,
        advisoryAt: 9_500,
      },
    },
  });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  const readout = page.dock
    .querySelectorAll('.headsup-readout-value')
    .map((node) => node.textContent);
  assert.deepEqual(readout, ['S1 Flash flood', '18:50:00Z', '60×']);
  const cell = (key) =>
    page.dock
      .querySelectorAll('.headsup-counter')
      .find((node) => node.dataset.counter === key);
  const pair = (key) => [
    cell(key).querySelector('.headsup-counter-live').textContent,
    cell(key).querySelector('.headsup-counter-ghost').textContent,
  ];
  // The two cohort tallies are the feed's, not a count of dots.
  assert.deepEqual(pair('entries'), ['0', '7']);
  assert.deepEqual(pair('assists'), ['0', '3']);
  // Rerouted is the one the viewer derives from telemetry it already drew.
  assert.equal(
    cell('rerouted').querySelector('.headsup-counter-value').textContent,
    '10',
  );
  // Measured between two observed instants: 9500 - 1000.
  assert.equal(
    cell('advisory').querySelector('.headsup-counter-value').textContent,
    '8.5s',
  );
  assert.equal(
    page.dock.querySelector('.headsup-feed .headsup-counter-value').textContent,
    'LIVE',
  );
});

test('a tally the feed has not reported shows as unknown, not as zero', (t) => {
  const layer = fakeLayer({
    stats: { connected: true, rerouted: 0 },
    feed: { scenario: null, pipeline: { acks: [], counters: null } },
  });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  const cell = (key) =>
    page.dock
      .querySelectorAll('.headsup-counter')
      .find((node) => node.dataset.counter === key);
  assert.deepEqual(
    [
      cell('entries').querySelector('.headsup-counter-live').textContent,
      cell('entries').querySelector('.headsup-counter-ghost').textContent,
      cell('advisory').querySelector('.headsup-counter-value').textContent,
    ],
    // "Nobody has told us" is not "nothing happened".
    ['—', '—', '—'],
  );
});

test('an idle feed reads as idle, with no replay speed claimed', (t) => {
  const layer = fakeLayer({
    stats: { connected: false },
    feed: { scenario: { state: 'idle', speed: 1 }, pipeline: { acks: [] } },
  });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  const readout = page.dock
    .querySelectorAll('.headsup-readout-value')
    .map((node) => node.textContent);
  assert.deepEqual(readout, ['IDLE', '—', '—']);
  assert.equal(
    page.dock.querySelector('.headsup-feed .headsup-counter-value').textContent,
    'DOWN',
  );
});

test('the feed chip says which feed answered, not merely that one did', (t) => {
  for (const [stats, expected] of [
    [{ connected: true, fallback: false }, 'LIVE'],
    [{ connected: true, fallback: true }, 'MOCK'],
    [{ connected: false, fallback: true }, 'DOWN'],
  ]) {
    const layer = fakeLayer({
      stats: { ...stats, endpoint: 'ws://localhost:8766/viewer' },
      feed: { scenario: null, pipeline: { acks: [] } },
    });
    const page = mount({ layer });
    layer.publish();
    const chip = page.dock.querySelector('.headsup-feed');
    assert.equal(
      chip.querySelector('.headsup-counter-value').textContent,
      expected,
    );
    assert.equal(chip.classList.contains('is-mock'), expected === 'MOCK');
    assert.equal(chip.title, 'Feed: ws://localhost:8766/viewer');
    page.console.destroy();
  }
  // Nothing left mounted by the loop above.
  const page = mount();
  t.after(() => page.console.destroy());
});

test('the legend names the hazard classes drawn, and the marks it draws', (t) => {
  const layer = fakeLayer({
    stats: { trails: 12, declined: 0, oriented: true, connected: true },
    hazards: [
      { hazardClass: 'FLOOD', color: '#2bb3ff', advisories: 2 },
      { hazardClass: 'WORK_ZONE', color: '#ff8c1a', advisories: 1 },
    ],
    feed: { scenario: null, pipeline: { acks: [] } },
  });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  const rows = page.controls
    .querySelectorAll('.headsup-legend-row')
    .map((row) => [
      row.querySelector('.headsup-legend-label').textContent,
      row.querySelector('.headsup-legend-count').textContent,
      row.hidden,
    ]);
  assert.deepEqual(rows, [
    // The classes actually on screen, then the marks that are always drawn.
    ['FLOOD', '2', false],
    ['WORK_ZONE', '1', false],
    ['Heading', '', false],
    ['Deflection trail', '12', false],
    // Two rows with nothing to report are not shown: a control-panel light
    // for a condition that is not happening.
    ['Remote assist', '', true],
    ['Declined', '', true],
  ]);
  const swatches = page.controls
    .querySelectorAll('.headsup-legend-swatch')
    .map((node) => node.style.properties.get('--headsup-legend-color'));
  assert.deepEqual(swatches, ['#2bb3ff', '#ff8c1a']);
});

test('a fleet too large to orient, and a fleet that refused, say so', (t) => {
  const layer = fakeLayer({
    stats: { trails: 0, declined: 2, oriented: false, connected: true },
    feed: { scenario: null, pipeline: { acks: [] } },
  });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  const row = (name) =>
    page.controls
      .querySelectorAll('.headsup-legend-row')
      .find(
        (node) =>
          node.querySelector('.headsup-legend-label').textContent === name,
      );
  assert.equal(
    row('Heading').querySelector('.headsup-legend-count').textContent,
    'not drawn',
  );
  assert.equal(
    row('Deflection trail').querySelector('.headsup-legend-count').textContent,
    '',
  );
  assert.equal(row('Declined').hidden, false);
  assert.equal(
    row('Declined').querySelector('.headsup-legend-count').textContent,
    '2',
  );
});

test('the refusal tag is handed to the layer already translated', (t) => {
  const layer = fakeLayer();
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  assert.deepEqual(layer.calls.declinedLabels, ['DECLINED']);
});

test('the scripted badge follows the feed`s own flag', (t) => {
  const feed = {
    scenario: { state: 'running', name: 'Flash flood', speed: 60 },
    pipeline: { acks: [] },
  };
  const layer = fakeLayer({ feed });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  const badge = () => page.dock.querySelector('.headsup-scripted-badge');
  layer.publish();
  assert.equal(
    badge().hidden,
    true,
    'a replay the feed does not call scripted is not badged',
  );
  feed.scenario.scripted = true;
  layer.publish();
  assert.equal(badge().hidden, false);
  assert.equal(badge().textContent, 'SCRIPTED DATA');
  // And an idle feed is not a scripted replay, whatever the last one was.
  feed.scenario = { state: 'idle', scripted: true, speed: 1 };
  layer.publish();
  assert.equal(badge().hidden, true);
});

// ── The advisory card ────────────────────────────────────────────────────

/** One live advisory, as the layer's snapshot carries it. */
const liveAdvisory = (overrides = {}) => ({
  id: 'adv-flood-1',
  version: 2,
  geometryType: 'Polygon',
  polygons: [
    [
      [
        [-97.75, 30.26],
        [-97.73958, 30.26],
        [-97.73958, 30.26904],
        [-97.75, 30.26904],
        [-97.75, 30.26],
      ],
    ],
  ],
  lines: [],
  hazardClass: 'FLOOD',
  severity: 4,
  confidence: 0.8,
  action: 'NO_ENTRY',
  validFrom: Date.now() - 10_000,
  validUntil: Date.now() + 90_000,
  evidence: [
    { source: 'nws', kind: 'nws.flood_warning' },
    { source: 'usgs', kind: 'usgs.gauge' },
  ],
  evidenceCount: 2,
  policyRule: 'flood.no_entry',
  issuer: 'headsup/compiler',
  signed: true,
  ...overrides,
});

test('the advisory card is on screen with its empty state before anything is in force', (t) => {
  const page = mount();
  t.after(() => page.console.destroy());
  const card = page.document.getElementById('headsup-advisory');
  assert.equal(card.dataset.state, 'empty');
  assert.equal(
    card.querySelector('.headsup-advisory-empty').textContent,
    'No advisory in force.',
  );
  // The fields are not drawn as dashes: "nothing in force" is a reading, and
  // a grid of em dashes would read as an advisory nobody filled in.
  assert.equal(card.querySelector('.headsup-advisory-grid').hidden, true);
  assert.equal(card.querySelector('.headsup-advisory-action-row').hidden, true);
  assert.equal(card.querySelector('.headsup-advisory-evidence').hidden, true);
  // The band says which state it is in, so the quiet card is not mistaken
  // for one that has stopped reporting.
  assert.equal(
    card.querySelector('.headsup-advisory-state').textContent,
    'STANDBY',
  );
});

test('the advisory card reports the action, the hazard, the extent and the window', (t) => {
  const layer = fakeLayer({
    stats: { connected: true },
    feed: {
      scenario: null,
      advisories: [liveAdvisory()],
      pipeline: {
        acks: [],
        compiles: [
          {
            proposalId: 'prop-1',
            advisoryId: 'adv-flood-1',
            status: 'SIGNED',
            signed: true,
            signatureVerified: true,
            reasons: [],
          },
        ],
      },
    },
  });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  const card = page.document.getElementById('headsup-advisory');
  assert.equal(card.dataset.state, 'live');
  assert.equal(
    card.querySelector('.headsup-advisory-state').textContent,
    'IN FORCE',
  );
  // The action is the thing the advisory asks for, so it is its own line.
  const action = card.querySelector('.headsup-advisory-action');
  assert.equal(action.textContent, 'NO_ENTRY');
  assert.equal(action.dataset.action, 'NO_ENTRY');
  assert.equal(
    card.querySelector('.headsup-advisory-id').textContent,
    'adv-flood-1',
  );
  // One labelled row per field, in the order they are read. The row is
  // addressed by the field it carries rather than by its index, so adding a
  // row cannot silently move an assertion onto another value.
  const values = card.querySelectorAll('.headsup-advisory-grid dd');
  const field = (name) =>
    values.find((node) => node.dataset.field === name) ?? null;
  assert.deepEqual(
    values.map((node) => node.dataset.field),
    ['hazard', 'severity', 'extent', 'expiry', 'issuer', 'signature'],
  );
  assert.equal(field('hazard').textContent, 'FLOOD');
  assert.equal(
    card.querySelector('.headsup-advisory-severity-text').textContent,
    '4 of 5',
  );
  // The pips are a view of the same number, so they carry its level.
  assert.equal(card.querySelector('.headsup-advisory-pips').dataset.level, '4');
  // About a square kilometre, in the unit it reads at.
  assert.match(field('extent').textContent, /^1\.0[01] km²$/);
  // Time left beside the instant the window closes: the countdown alone
  // cannot be checked against a clock on a wall.
  assert.match(
    card.querySelector('.headsup-advisory-countdown').textContent,
    /^1m 3\ds$/,
  );
  assert.match(
    card.querySelector('.headsup-advisory-until').textContent,
    /^\d\d:\d\d:\d\dZ$/,
  );
  assert.equal(field('expiry').dataset.urgency, 'open');
  assert.equal(field('issuer').textContent, 'headsup/compiler');
  // The feeds it rests on, named — not a count, which would answer "how
  // much evidence" rather than "whose".
  assert.deepEqual(
    card
      .querySelectorAll('.headsup-advisory-chip')
      .map((chip) => chip.textContent),
    ['NWS', 'USGS'],
  );
  assert.equal(field('signature').textContent, 'verified');
  assert.equal(
    card.querySelector('.headsup-advisory-signature').dataset.signature,
    'verified',
  );
});

test('a citation naming no feed is its own chip rather than a silent drop', (t) => {
  const layer = fakeLayer({
    stats: { connected: true },
    feed: {
      scenario: null,
      advisories: [
        liveAdvisory({
          evidence: [
            { source: 'nws', kind: 'nws.flood_warning' },
            { source: null, kind: 'usgs.gauge' },
          ],
          evidenceCount: 2,
        }),
      ],
      pipeline: { acks: [], compiles: [] },
    },
  });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  const card = page.document.getElementById('headsup-advisory');
  // The card still stands, and the chip row still accounts for both
  // citations: an advisory with one unreadable source is not a blank card.
  assert.equal(card.dataset.state, 'live');
  assert.deepEqual(
    card
      .querySelectorAll('.headsup-advisory-chip')
      .map((chip) => chip.textContent),
    ['NWS', '+1 unnamed'],
  );
});

test('an advisory citing nothing says so, rather than showing an empty row', (t) => {
  const layer = fakeLayer({
    stats: { connected: true },
    feed: {
      scenario: null,
      advisories: [liveAdvisory({ evidence: [], evidenceCount: 0 })],
      pipeline: { acks: [], compiles: [] },
    },
  });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  assert.deepEqual(
    page.document
      .getElementById('headsup-advisory')
      .querySelectorAll('.headsup-advisory-chip')
      .map((chip) => chip.textContent),
    ['none cited'],
  );
});

test('the card never upgrades an unreported signature into a yes', (t) => {
  const layer = fakeLayer({
    stats: { connected: true },
    feed: {
      scenario: null,
      advisories: [liveAdvisory()],
      pipeline: { acks: [], compiles: [] },
    },
  });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  const signature = page.document
    .getElementById('headsup-advisory')
    .querySelector('.headsup-advisory-signature');
  // The advisory arrived carrying a signature; nothing has checked it, and
  // this console does not hold the issuer's key.
  assert.equal(signature.textContent, 'present, unverified');
  assert.equal(signature.dataset.signature, 'unverified');
});

test('a corridor advisory reports its length, and a second live one is counted', (t) => {
  const layer = fakeLayer({
    stats: { connected: true },
    feed: {
      scenario: null,
      advisories: [
        liveAdvisory(),
        liveAdvisory({
          id: 'adv-keep-clear-6',
          geometryType: 'LineString',
          action: 'KEEP_CLEAR',
          hazardClass: 'EMERGENCY_SCENE',
          polygons: [],
          lines: [
            [
              [-97.74, 30.2665],
              [-97.7365, 30.2665],
            ],
          ],
          validFrom: Date.now() - 1_000,
        }),
      ],
      pipeline: { acks: [], compiles: [] },
    },
  });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  const card = page.document.getElementById('headsup-advisory');
  // The newest live window, and the card says how many are in force so one
  // card is not read as the claim that there is one.
  assert.equal(
    card.querySelector('.headsup-advisory-id').textContent,
    'adv-keep-clear-6 · 2 in force',
  );
  const extent = card
    .querySelectorAll('.headsup-advisory-grid dd')
    .find((node) => node.dataset.field === 'extent');
  // Metres, because that is the unit a third of a kilometre reads at.
  assert.match(extent.textContent, /^33\d m corridor$/);
});

test('the dock lists every test scene with its what-if line', async (t) => {
  const page = mount();
  t.after(() => page.console.destroy());
  // Asked for at startup: the buttons ARE the surface, so nothing is opened.
  await Promise.resolve();
  await Promise.resolve();
  const buttons = page.dock.querySelectorAll('.headsup-scene-btn');
  assert.equal(buttons.length, 1);
  assert.equal(buttons[0].dataset.scenario, 's1_flood');
  assert.equal(
    buttons[0].querySelector('.headsup-scene-code').textContent,
    'S1',
  );
  assert.equal(
    buttons[0].querySelector('.headsup-scene-whatif').textContent,
    SCENARIO.whatIf,
  );
  assert.deepEqual(page.scenarios.calls, ['list']);
});

test('a listing the API cannot answer says why and offers a retry', async (t) => {
  const scenarios = fakeScenarios({ failList: new Error('offline') });
  const page = mount({ scenarios });
  t.after(() => page.console.destroy());
  for (let turn = 0; turn < 4; turn++) await Promise.resolve();
  assert.match(
    page.dock.querySelector('.headsup-scene-status').textContent,
    /failed/,
  );
  // One retry affordance, and it asks the same question again.
  const retry = page.dock.querySelector('.headsup-scene-retry');
  assert.equal(retry.textContent, 'RETRY');
  retry.click();
  for (let turn = 0; turn < 4; turn++) await Promise.resolve();
  assert.deepEqual(page.scenarios.calls, ['list', 'list']);
});

test('one press starts a scene, flies there, banners it and opens the rail', async (t) => {
  const page = mount();
  t.after(() => page.console.destroy());
  await Promise.resolve();
  await Promise.resolve();
  page.dock.querySelector('.headsup-scene-btn').click();
  for (let turn = 0; turn < 6; turn++) await Promise.resolve();
  assert.deepEqual(page.scenarios.calls, ['list', 'start:s1_flood']);
  assert.deepEqual(page.flights, [SCENARIO.startView]);
  const banner = page.document.getElementById('headsup-banner');
  assert.equal(banner.hidden, false);
  assert.equal(
    banner.querySelector('.headsup-banner-text').textContent,
    'WHAT-IF REPLAY · recorded public data + scripted events',
  );
  assert.equal(
    page.document.getElementById('headsup-pipeline-rail').hidden,
    false,
  );
  // The button that started it says so, and Exit appears beside the clock.
  assert.equal(
    page.dock.querySelector('.headsup-scene-btn').getAttribute('aria-pressed'),
    'true',
  );
  assert.equal(page.dock.querySelector('.headsup-scene-exit').hidden, false);
  assert.equal(
    page.dock.querySelector('.headsup-scene-status').textContent,
    'Flash flood (San Antonio)',
  );
});

test('the rail opens naming its six stages, none of them lit', async (t) => {
  const page = mount();
  t.after(() => page.console.destroy());
  const rail = page.document.getElementById('headsup-pipeline-rail');
  assert.deepEqual(
    rail
      .querySelectorAll('.headsup-rail-stage')
      .map((item) => [item.dataset.stage, item.dataset.lit, item.dataset.tone]),
    [
      ['feeds', 'false', 'idle'],
      ['sentinel', 'false', 'idle'],
      ['fusion', 'false', 'idle'],
      ['proposer', 'false', 'idle'],
      ['critic', 'false', 'idle'],
      ['compiler', 'false', 'idle'],
    ],
  );
  // No fleet has answered, and the rail says so rather than leaving a gap.
  assert.equal(rail.querySelector('.headsup-rail-empty').hidden, false);
  // Nothing is open until somebody clicks a stage.
  assert.equal(page.document.getElementById('headsup-rail-card').hidden, true);
});

test('a start that fails says why and claims no replay', async (t) => {
  const scenarios = fakeScenarios({ failStart: new Error('boom') });
  const page = mount({ scenarios });
  t.after(() => page.console.destroy());
  await Promise.resolve();
  await Promise.resolve();
  page.dock.querySelector('.headsup-scene-btn').click();
  for (let turn = 0; turn < 6; turn++) await Promise.resolve();
  assert.match(
    page.dock.querySelector('.headsup-scene-status').textContent,
    /failed/,
  );
  assert.deepEqual(page.flights, []);
  assert.equal(page.document.getElementById('headsup-banner').hidden, true);
  assert.equal(page.dock.querySelector('.headsup-scene-exit').hidden, true);
});

test('Exit stops the replay, clears what it drew and flies home', async (t) => {
  const page = mount();
  t.after(() => page.console.destroy());
  await Promise.resolve();
  await Promise.resolve();
  page.dock.querySelector('.headsup-scene-btn').click();
  for (let turn = 0; turn < 6; turn++) await Promise.resolve();
  page.dock.querySelector('.headsup-scene-exit').click();
  for (let turn = 0; turn < 6; turn++) await Promise.resolve();
  assert.deepEqual(page.scenarios.calls, ['list', 'start:s1_flood', 'stop']);
  assert.equal(page.layer.calls.cleared, 1);
  assert.deepEqual(page.flights.at(-1), createHeadsupModeConfig().landingView);
  assert.equal(page.document.getElementById('headsup-banner').hidden, true);
  assert.equal(
    page.document.getElementById('headsup-pipeline-rail').hidden,
    true,
  );
  assert.equal(page.dock.querySelector('.headsup-scene-exit').hidden, true);
});

test('Escape ends the replay once there is no card open to close', async (t) => {
  const page = mount();
  t.after(() => page.console.destroy());
  await Promise.resolve();
  await Promise.resolve();
  page.dock.querySelector('.headsup-scene-btn').click();
  for (let turn = 0; turn < 6; turn++) await Promise.resolve();
  page.document.dispatch('keydown', { key: 'Escape' });
  for (let turn = 0; turn < 6; turn++) await Promise.resolve();
  assert.equal(page.scenarios.calls.at(-1), 'stop');
});

test('an Escape with nothing open is left for upstream`s own handlers', (t) => {
  const page = mount();
  t.after(() => page.console.destroy());
  const event = page.document.dispatch('keydown', { key: 'Escape' });
  assert.equal(event.defaultPrevented, false);
  const other = page.document.dispatch('keydown', { key: 'c' });
  assert.equal(other.defaultPrevented, false);
});

test('teardown takes back every node it added', (t) => {
  const page = mount();
  page.console.destroy();
  // Neither panel was ever retitled: the dock owns scenario control and the
  // cohort rows now, and the stylesheet hides upstream's panels whole.
  assert.equal(page.dataTitle.textContent, 'DATA LAYERS');
  assert.equal(page.sceneTitle.textContent, 'SCENES');
  assert.equal(page.document.getElementById('headsup-banner'), null);
  assert.equal(page.document.getElementById('headsup-dock'), null);
  assert.equal(page.document.getElementById('headsup-controls'), null);
  assert.equal(page.document.getElementById('headsup-pipeline-rail'), null);
  assert.equal(page.document.getElementById('headsup-status'), null);
  assert.equal(page.document.getElementById('headsup-advisory'), null);
  // Idempotent: the application defers it, and an abort can also reach it.
  page.console.destroy();
  t.diagnostic('teardown is idempotent');
});

// ── The agent rail, its cards and the second key ─────────────────────────

/**
 * A feed snapshot one stage at a time.
 *
 * Built as a transcript rather than as flags, because that is what the
 * socket actually hands over and the rail's whole claim is that it is a
 * record of frames that arrived.
 */
const transcript = (parts = {}) => ({
  scenario: {
    state: 'running',
    id: 's3_emergency',
    code: 'S3',
    name: 'Emergency scene',
    city: 'Austin',
    simTime: Date.parse('2026-04-23T14:02:00Z'),
    simTimeText: '2026-04-23T14:02:00Z',
    elapsedS: 9,
    speed: 60,
    scripted: true,
  },
  pipeline: {
    tallies: {},
    hazardCount: 0,
    acks: [],
    candidates: [],
    hazards: [],
    proposals: [],
    verdicts: [],
    compiles: [],
    assists: [],
    counters: null,
    firstSignalAt: null,
    advisoryAt: null,
    ...parts,
  },
});

const DISPATCH_TEXT =
  'AFD INC 2026-0423-00871 BOX ALARM / STRUCTURE E 6TH ST 600 BLK\n' +
  '2 LANES EB CLOSED // EST 45 MIN';

const s3Candidate = {
  id: 'cand-1',
  sentinel: 'dispatch-text',
  hazardClass: 'EMERGENCY_SCENE',
  confidence: 0.71,
  observedAt: null,
  note: 'structure fire response closing two eastbound lanes',
  evidence: [
    {
      eventId: 'evt-1',
      source: 'austin_incidents',
      kind: 'synthetic.austin.active_incident_dispatch',
      observedAt: null,
      note: 'box alarm, three units on scene',
      rawRef: null,
    },
  ],
  rawText: DISPATCH_TEXT,
  extracted: [
    { field: 'incident', value: 'AFD INC 2026-0423-00871' },
    { field: 'lanes_closed', value: '2 eastbound' },
  ],
  at: 2_000,
};

const s3Hazard = {
  id: 'haz-1',
  hazardClass: 'EMERGENCY_SCENE',
  severity: 4,
  confidence: 0.88,
  rationale: 'One authoritative dispatch record; units on scene confirm it.',
  validUntil: null,
  evidence: s3Candidate.evidence,
  at: 4_000,
};

const s3Verdict = {
  proposalId: 'prop-1',
  decision: 'ESCALATE',
  decidedAt: null,
  checks: [
    { rule: 'action_allowed_for_class', passed: true },
    { rule: 'two_key_required', passed: false },
  ],
  reasons: ['severity 4 requires a human approval token'],
  rationale: 'Every policy check passes; severity 4 is over the two-key line.',
  at: 8_000,
};

const s3Held = {
  proposalId: 'prop-1',
  advisoryId: null,
  status: 'PENDING_APPROVAL',
  decidedAt: null,
  issuer: 'headsup/mock-stream',
  policyRule: 'emergency_scene.keep_clear.mock',
  signed: false,
  signatureVerified: null,
  reasons: ['two_key_approval_required'],
  twoKey: true,
  at: 9_000,
};

const railStage = (page, key) =>
  page.document
    .getElementById('headsup-pipeline-rail')
    .querySelectorAll('.headsup-rail-stage')
    .find((row) => row.dataset.stage === key);

const cardText = (page) =>
  page.document
    .getElementById('headsup-rail-card')
    .descendants()
    .map((node) => node.textContent)
    .join(' | ');

test('a stage lights when its own event arrives, and not before', (t) => {
  const feed = transcript();
  const layer = fakeLayer({ stats: { connected: true }, feed });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  assert.equal(railStage(page, 'sentinel').dataset.lit, 'false');
  assert.equal(railStage(page, 'fusion').dataset.lit, 'false');

  feed.pipeline.candidates = [s3Candidate];
  layer.publish();
  assert.equal(railStage(page, 'sentinel').dataset.lit, 'true');
  assert.equal(
    railStage(page, 'sentinel').querySelector('.headsup-rail-stage-value')
      .textContent,
    'EMERGENCY_SCENE',
  );
  // The feeds row lights off the same evidence, named as a source.
  assert.equal(railStage(page, 'feeds').dataset.lit, 'true');
  // And fusion has still not reported, whatever the sentinel said.
  assert.equal(railStage(page, 'fusion').dataset.lit, 'false');

  feed.pipeline.hazards = [s3Hazard];
  layer.publish();
  assert.equal(railStage(page, 'fusion').dataset.lit, 'true');
  // Fusion's row carries the confidence, which is what fusion produces.
  assert.equal(
    railStage(page, 'fusion').querySelector('.headsup-rail-stage-value')
      .textContent,
    '88%',
  );
});

test('the critic and the compiler read as three states, not two', (t) => {
  const feed = transcript({ verdicts: [s3Verdict], compiles: [s3Held] });
  const layer = fakeLayer({ stats: { connected: true }, feed });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  // Stopped for a person, which is not the same as refused.
  assert.equal(railStage(page, 'critic').dataset.tone, 'wait');
  assert.equal(railStage(page, 'compiler').dataset.tone, 'wait');
  assert.equal(
    railStage(page, 'compiler').querySelector('.headsup-rail-stage-value')
      .textContent,
    'PENDING_APPROVAL',
  );
  feed.pipeline.compiles = [{ ...s3Held, status: 'REJECTED', twoKey: false }];
  layer.publish();
  assert.equal(railStage(page, 'compiler').dataset.tone, 'alert');
});

test('every rail row names the id that ties it to the globe', (t) => {
  const feed = transcript({
    candidates: [s3Candidate],
    hazards: [s3Hazard],
    verdicts: [s3Verdict],
  });
  const layer = fakeLayer({ stats: { connected: true }, feed });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  assert.equal(railStage(page, 'fusion').title, 'haz-1');
  assert.equal(railStage(page, 'critic').title, 'prop-1');
  assert.equal(
    page.document.querySelector('.headsup-rail-link').textContent,
    'EMERGENCY_SCENE · haz-1',
  );
});

test('clicking a stage opens its card, and clicking again closes it', (t) => {
  const feed = transcript({ hazards: [s3Hazard] });
  const layer = fakeLayer({ stats: { connected: true }, feed });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  const card = page.document.getElementById('headsup-rail-card');
  assert.equal(card.hidden, true);
  railStage(page, 'fusion').click();
  assert.equal(card.hidden, false);
  assert.equal(railStage(page, 'fusion').getAttribute('aria-expanded'), 'true');
  const text = cardText(page);
  // The evidence list, the confidence and the rationale fusion wrote.
  assert.match(text, /haz-1/);
  assert.match(text, /88%/);
  assert.match(text, /units on scene confirm it/);
  assert.match(text, /Austin incidents/);
  railStage(page, 'fusion').click();
  assert.equal(card.hidden, true);
});

test('a stage that has not reported says so rather than inventing a card', (t) => {
  const layer = fakeLayer({ stats: { connected: true }, feed: transcript() });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  railStage(page, 'critic').click();
  assert.match(cardText(page), /has not reported yet/);
});

test('the S3 Sentinels card shows the dispatch text beside the fields read from it', (t) => {
  const feed = transcript({ candidates: [s3Candidate] });
  const layer = fakeLayer({ stats: { connected: true }, feed });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  railStage(page, 'sentinel').click();
  const card = page.document.getElementById('headsup-rail-card');
  const split = card.querySelector('.headsup-card-split');
  assert.ok(split, 'the two halves are laid out side by side');
  // The raw message, written as TEXT and with its line breaks intact.
  const raw = card.querySelector('.headsup-card-raw');
  assert.equal(raw.textContent, DISPATCH_TEXT);
  // And the fields the agent says it pulled out of it, beside it.
  const fields = card.querySelector('.headsup-card-fields');
  assert.deepEqual(
    fields.children.map((node) => node.textContent),
    ['incident', 'AFD INC 2026-0423-00871', 'lanes_closed', '2 eastbound'],
  );
});

test('the Compiler card never upgrades an unreported signature into a yes', (t) => {
  const feed = transcript({ compiles: [s3Held] });
  const layer = fakeLayer({ stats: { connected: true }, feed });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  railStage(page, 'compiler').click();
  assert.match(cardText(page), /absent/);
  feed.pipeline.compiles = [
    { ...s3Held, status: 'SIGNED', advisoryId: 'adv-1', signed: true },
  ];
  layer.publish();
  // Present but unverified: only the issuer's key could say more.
  assert.match(cardText(page), /present, unverified/);
  feed.pipeline.compiles = [
    {
      ...s3Held,
      status: 'SIGNED',
      advisoryId: 'adv-1',
      signed: true,
      signatureVerified: false,
    },
  ];
  layer.publish();
  assert.match(cardText(page), /NOT VERIFIED/);
});

test('a held two-key advisory offers an Approve button that posts the proposal', async (t) => {
  const feed = transcript({ verdicts: [s3Verdict], compiles: [s3Held] });
  const layer = fakeLayer({ stats: { connected: true }, feed });
  const scenarios = fakeScenarios();
  const page = mount({ layer, scenarios });
  t.after(() => page.console.destroy());
  layer.publish();
  railStage(page, 'compiler').click();
  const button = page.document
    .getElementById('headsup-rail-card')
    .querySelector('.headsup-card-approve-btn');
  assert.ok(button, 'the Compiler card offers the second key');
  assert.match(cardText(page), /needs a second key/);
  // The critic's reason for escalating is on the Critic card, where it was
  // produced — the button only says what it is for.
  button.click();
  for (let turn = 0; turn < 6; turn++) await Promise.resolve();
  // The listing is the call the console makes at startup, before anybody
  // has pressed anything.
  assert.deepEqual(page.scenarios.calls, ['list', 'approve:prop-1']);
  // The console does not redraw itself as published: what re-enables the
  // pipeline is the compiler's next frame, not this call returning.
  assert.match(cardText(page), /Waiting for the compiler/);
  feed.pipeline.compiles = [
    ...feed.pipeline.compiles,
    {
      ...s3Held,
      status: 'SIGNED',
      advisoryId: 'adv-1',
      signed: true,
      signatureVerified: false,
      twoKey: false,
      at: 12_000,
    },
  ];
  layer.publish();
  // Signed: the decision is no longer anybody's to make, so the button goes.
  assert.equal(
    page.document
      .getElementById('headsup-rail-card')
      .querySelector('.headsup-card-approve-btn'),
    null,
  );
});

test('an approval the API refuses says so and leaves the button usable', async (t) => {
  const feed = transcript({ compiles: [s3Held] });
  const layer = fakeLayer({ stats: { connected: true }, feed });
  const scenarios = fakeScenarios({ failApprove: new Error('boom') });
  const page = mount({ layer, scenarios });
  t.after(() => page.console.destroy());
  layer.publish();
  railStage(page, 'compiler').click();
  const button = page.document
    .getElementById('headsup-rail-card')
    .querySelector('.headsup-card-approve-btn');
  button.click();
  for (let turn = 0; turn < 6; turn++) await Promise.resolve();
  assert.match(cardText(page), /failed/);
  assert.equal(button.disabled, false);
});

test('one rail row per operator, carrying what that fleet answered', (t) => {
  const feed = transcript({
    compiles: [{ ...s3Held, status: 'SIGNED', advisoryId: 'adv-1' }],
    acks: [
      {
        operator: 'op_a',
        status: 'ACCEPTED',
        vehicles: 6,
        actionTaken: 'KEEP_CLEAR applied; eastbound routes moved to E 7th',
        reason: null,
        advisoryId: 'adv-1',
        seq: 1,
        at: 10_000,
      },
      {
        operator: 'op_b',
        status: 'DELAYED',
        vehicles: 4,
        actionTaken: 'advisory received; applying at next route refresh',
        reason: 'fleet-side route solver runs on a 90 second cycle',
        advisoryId: 'adv-1',
        seq: 2,
        at: 10_500,
      },
    ],
  });
  const layer = fakeLayer({ stats: { connected: true }, feed });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  const rows = page.document
    .getElementById('headsup-pipeline-rail')
    .querySelectorAll('.headsup-rail-stage')
    .filter((row) => row.dataset.stage.startsWith('ack:'));
  assert.deepEqual(
    rows.map((row) => [
      row.querySelector('.headsup-rail-stage-name').textContent,
      row.querySelector('.headsup-rail-stage-value').textContent,
      row.dataset.tone,
    ]),
    [
      ['op_a', 'ACCEPTED', 'ok'],
      // Not yet is not no, and it has a colour of its own.
      ['op_b', 'DELAYED', 'wait'],
    ],
  );
  // And a fleet row opens its own card, with the reason it gave.
  rows[1].click();
  assert.match(cardText(page), /90 second cycle/);
});

test('the assist badge is handed to the layer already worded', (t) => {
  const layer = fakeLayer();
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  assert.equal(layer.calls.assistLabels.length, 1);
  assert.equal(layer.calls.assistLabels[0](3), 'ASSIST ×3');
});

test('ending a replay takes the rail`s open card with it', async (t) => {
  const feed = transcript({ hazards: [s3Hazard] });
  const layer = fakeLayer({ stats: { connected: true }, feed });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  railStage(page, 'fusion').click();
  assert.equal(page.document.getElementById('headsup-rail-card').hidden, false);
  page.dock.querySelector('.headsup-scene-exit').click();
  for (let turn = 0; turn < 6; turn++) await Promise.resolve();
  // The rail is hidden on the landing, and nothing of the last run is left
  // open inside it.
  assert.equal(
    page.document.getElementById('headsup-pipeline-rail').hidden,
    true,
  );
  assert.equal(page.document.getElementById('headsup-rail-card').hidden, true);
});

test('Escape closes an open card before it ends the replay', async (t) => {
  const feed = transcript({ hazards: [s3Hazard] });
  const layer = fakeLayer({ stats: { connected: true }, feed });
  const scenarios = fakeScenarios();
  const page = mount({ layer, scenarios });
  t.after(() => page.console.destroy());
  layer.publish();
  railStage(page, 'fusion').click();
  page.document.dispatch('keydown', { key: 'Escape' });
  assert.equal(page.document.getElementById('headsup-rail-card').hidden, true);
  // Nothing but the startup listing: the first Escape took the card.
  assert.deepEqual(page.scenarios.calls, ['list']);
  // The next one reaches the replay.
  page.document.dispatch('keydown', { key: 'Escape' });
  for (let turn = 0; turn < 6; turn++) await Promise.resolve();
  assert.deepEqual(page.scenarios.calls, ['list', 'stop']);
});

test('a snapshot whose pipeline is not the shape this build knows is a quiet rail', (t) => {
  // The console renders from data the layer hands over. A publisher sending
  // a field that is not a list must leave an empty rail, not a dead one: the
  // render loop also drives the clock, the counters and the legend.
  const layer = fakeLayer({
    stats: { connected: true },
    feed: { scenario: null, pipeline: { hazards: 3, acks: 'none' } },
  });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  assert.equal(railStage(page, 'fusion').dataset.lit, 'false');
  assert.equal(
    page.dock.querySelector('.headsup-feed .headsup-counter-value').textContent,
    'LIVE',
  );
});
