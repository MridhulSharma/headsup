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
  const calls = { cohorts: [], cleared: 0, enabled: [], declinedLabels: [] };
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
      getGhostCohort: () => ({ color: '#8a94a6', vehicles: 0, visible: true }),
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
function fakeScenarios({ listing = [SCENARIO], failStart = null } = {}) {
  const calls = [];
  return {
    calls,
    client: {
      baseUrl: 'http://localhost:8765',
      list: async () => {
        calls.push('list');
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
    console: console_,
    layer,
    scenarios,
    flights,
    hud,
    hudCalls,
  };
}

test('the console retitles two panels and adds its own surfaces', (t) => {
  const page = mount();
  t.after(() => page.console.destroy());
  assert.equal(page.dataTitle.textContent, 'HEADS UP LAYERS');
  assert.equal(page.sceneTitle.textContent, 'TEST SCENE');
  // The four Heads Up cohorts, in the order the build step names them.
  assert.deepEqual(
    page.dataToggles
      .querySelectorAll('.headsup-cohort')
      .map((row) => row.dataset.cohort),
    ['hazards', 'corridors', 'ghost'],
  );
  assert.ok(page.sceneInner.querySelector('.headsup-scene-launch'));
  assert.ok(page.styleIndicator.querySelector('.headsup-readout'));
  assert.ok(page.hudContent.querySelector('.headsup-counters'));
  assert.ok(page.document.getElementById('headsup-pipeline-rail'));
  assert.ok(page.document.getElementById('headsup-banner'));
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
  const rows = page.dataToggles.querySelectorAll('.headsup-cohort');
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
  const fleets = page.dataToggles
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
      pipeline: { tallies: {}, hazards: 1, acks: [{}, {}, {}] },
    },
  });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  const readout = page.styleIndicator
    .querySelectorAll('.headsup-readout-value')
    .map((node) => node.textContent);
  assert.deepEqual(readout, ['S1 Flash flood', '18:50:00Z', '60×']);
  const counters = page.hudContent
    .querySelectorAll('.headsup-counter')
    .map((cell) => [
      cell.querySelector('.headsup-counter-label').textContent,
      cell.querySelector('.headsup-counter-value').textContent,
    ]);
  assert.deepEqual(counters, [
    ['HAZARDS', '1'],
    ['ADVISORIES', '1'],
    ['FLEET', '10'],
    ['REROUTED', '10'],
    ['GHOST', '6'],
    ['ACKS', '3'],
    ['FEED', 'LIVE'],
  ]);
});

test('an idle feed reads as idle, with no replay speed claimed', (t) => {
  const layer = fakeLayer({
    stats: { connected: false },
    feed: { scenario: { state: 'idle', speed: 1 }, pipeline: { acks: [] } },
  });
  const page = mount({ layer });
  t.after(() => page.console.destroy());
  layer.publish();
  const readout = page.styleIndicator
    .querySelectorAll('.headsup-readout-value')
    .map((node) => node.textContent);
  assert.deepEqual(readout, ['IDLE', '—', '—']);
  assert.equal(
    page.hudContent.querySelector('.headsup-feed .headsup-counter-value')
      .textContent,
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
    const chip = page.hudContent.querySelector('.headsup-feed');
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
  const rows = page.dataToggles
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
    // A refusal row with nothing to report is not shown.
    ['Declined', '', true],
  ]);
  const swatches = page.dataToggles
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
    page.dataToggles
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
  const badge = () => page.body.querySelector('.headsup-banner-badge');
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

test('the picker lists every scenario with its what-if line', async (t) => {
  const page = mount();
  t.after(() => page.console.destroy());
  page.sceneInner.querySelector('.headsup-scene-btn').click();
  await Promise.resolve();
  await Promise.resolve();
  const picker = page.document.getElementById('headsup-scene-picker');
  assert.equal(picker.hidden, false);
  const cards = picker.querySelectorAll('.headsup-picker-card');
  assert.equal(cards.length, 1);
  assert.equal(
    cards[0].querySelector('.headsup-picker-card-code').textContent,
    'S1',
  );
  assert.equal(
    cards[0].querySelector('.headsup-picker-card-whatif').textContent,
    SCENARIO.whatIf,
  );
  assert.deepEqual(page.scenarios.calls, ['list']);
});

test('choosing a scenario starts it, flies there, banners it and opens the rail', async (t) => {
  const page = mount();
  t.after(() => page.console.destroy());
  page.sceneInner.querySelector('.headsup-scene-btn').click();
  await Promise.resolve();
  await Promise.resolve();
  const picker = page.document.getElementById('headsup-scene-picker');
  picker.querySelector('.headsup-picker-card').click();
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
  assert.equal(picker.hidden, true);
});

test('the rail opens empty, naming the six stages it will fill', async (t) => {
  const page = mount();
  t.after(() => page.console.destroy());
  const rail = page.document.getElementById('headsup-pipeline-rail');
  assert.deepEqual(
    rail
      .querySelectorAll('.headsup-rail-stage')
      .map((item) => item.dataset.stage),
    ['sentinel', 'fusion', 'proposer', 'critic', 'compiler', 'ack'],
  );
  assert.ok(rail.querySelector('.headsup-rail-empty').textContent);
});

test('a start that fails leaves the picker open and says why', async (t) => {
  const scenarios = fakeScenarios({ failStart: new Error('boom') });
  const page = mount({ scenarios });
  t.after(() => page.console.destroy());
  page.sceneInner.querySelector('.headsup-scene-btn').click();
  await Promise.resolve();
  await Promise.resolve();
  const picker = page.document.getElementById('headsup-scene-picker');
  picker.querySelector('.headsup-picker-card').click();
  for (let turn = 0; turn < 6; turn++) await Promise.resolve();
  assert.equal(picker.hidden, false);
  assert.match(
    picker.querySelector('.headsup-picker-status').textContent,
    /failed/,
  );
  assert.deepEqual(page.flights, []);
  assert.equal(page.document.getElementById('headsup-banner').hidden, true);
});

test('Exit stops the replay, clears what it drew and flies home', async (t) => {
  const page = mount();
  t.after(() => page.console.destroy());
  page.sceneInner.querySelector('.headsup-scene-btn').click();
  await Promise.resolve();
  await Promise.resolve();
  page.document
    .getElementById('headsup-scene-picker')
    .querySelector('.headsup-picker-card')
    .click();
  for (let turn = 0; turn < 6; turn++) await Promise.resolve();
  page.sceneInner.querySelector('.headsup-scene-exit').click();
  for (let turn = 0; turn < 6; turn++) await Promise.resolve();
  assert.deepEqual(page.scenarios.calls, ['list', 'start:s1_flood', 'stop']);
  assert.equal(page.layer.calls.cleared, 1);
  assert.deepEqual(page.flights.at(-1), createHeadsupModeConfig().landingView);
  assert.equal(page.document.getElementById('headsup-banner').hidden, true);
  assert.equal(
    page.document.getElementById('headsup-pipeline-rail').hidden,
    true,
  );
});

test('Escape closes the picker first, then ends the replay', async (t) => {
  const page = mount();
  t.after(() => page.console.destroy());
  page.sceneInner.querySelector('.headsup-scene-btn').click();
  await Promise.resolve();
  await Promise.resolve();
  const picker = page.document.getElementById('headsup-scene-picker');
  picker.querySelector('.headsup-picker-card').click();
  for (let turn = 0; turn < 6; turn++) await Promise.resolve();
  // Reopen it: the first Escape must take the dialog, not the replay.
  page.sceneInner.querySelector('.headsup-scene-btn').click();
  await Promise.resolve();
  await Promise.resolve();
  page.document.dispatch('keydown', { key: 'Escape' });
  assert.equal(picker.hidden, true);
  assert.deepEqual(page.scenarios.calls, ['list', 'start:s1_flood', 'list']);
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

test('teardown takes back every node it added and both panel titles', (t) => {
  const page = mount();
  page.console.destroy();
  assert.equal(page.dataTitle.textContent, 'DATA LAYERS');
  assert.equal(page.sceneTitle.textContent, 'SCENES');
  assert.equal(page.document.getElementById('headsup-banner'), null);
  assert.equal(page.document.getElementById('headsup-pipeline-rail'), null);
  assert.equal(page.document.getElementById('headsup-scene-picker'), null);
  assert.equal(page.styleIndicator.querySelector('.headsup-readout'), null);
  assert.equal(page.hudContent.querySelector('.headsup-counters'), null);
  assert.equal(page.sceneInner.querySelector('.headsup-scene-launch'), null);
  // Idempotent: the application defers it, and an abort can also reach it.
  page.console.destroy();
  t.diagnostic('teardown is idempotent');
});
