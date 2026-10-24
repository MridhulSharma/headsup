import assert from 'node:assert/strict';
import test from 'node:test';
import { createHeadsupViewerSource } from './source.js';

/** One socket the test opens, feeds and closes by hand. */
function fakeSocket() {
  const socket = {
    readyState: 1,
    closed: 0,
    onopen: null,
    onmessage: null,
    onerror: null,
    onclose: null,
    close() {
      this.closed++;
    },
  };
  return socket;
}

function connected({ now = () => 1_000 } = {}) {
  const sockets = [];
  const source = createHeadsupViewerSource({
    endpoint: 'ws://localhost:8765/viewer',
    // One endpoint: these tests are about what a frame does, and the ladder
    // has its own.
    fallbackEndpoint: null,
    socketFactory: () => {
      const socket = fakeSocket();
      sockets.push(socket);
      return socket;
    },
    now,
    setTimer: () => null,
    clearTimer: () => {},
  });
  source.connect();
  const send = (frame) =>
    sockets.at(-1).onmessage({ data: JSON.stringify(frame) });
  return { source, sockets, send };
}

/** One publishable advisory, the smallest the contract accepts. */
const advisoryFrame = () => ({
  type: 'advisory',
  feature: {
    type: 'Feature',
    geometry: {
      type: 'Polygon',
      coordinates: [
        [
          [-98.399, 29.52],
          [-98.393, 29.52],
          [-98.393, 29.5255],
          [-98.399, 29.5255],
          [-98.399, 29.52],
        ],
      ],
    },
    properties: {
      id: 'adv-1',
      hazard: { class: 'FLOOD', severity: 4, confidence: 0.9 },
      action: { type: 'NO_ENTRY' },
      valid_from: '2026-04-20T18:50:00Z',
      valid_until: '2026-04-21T00:50:00Z',
    },
  },
});

const scenarioFrame = (overrides = {}) => ({
  type: 'scenario_state',
  state: 'running',
  sim_time: '2026-04-20T18:50:00Z',
  elapsed_s: 7.5,
  speed: 60,
  scripted: true,
  scenario: {
    id: 's1_flood',
    code: 'S1',
    name: 'Flash flood',
    city: 'San Antonio',
  },
  ...overrides,
});

test('a scenario-state frame becomes the run the readout reports', () => {
  const feed = connected();
  feed.send(scenarioFrame());
  const { scenario } = feed.source.getStatus();
  assert.deepEqual(
    {
      state: scenario.state,
      id: scenario.id,
      code: scenario.code,
      speed: scenario.speed,
    },
    { state: 'running', id: 's1_flood', code: 'S1', speed: 60 },
  );
});

/** One well-formed frame of each reasoning kind, in timeline order. */
const PIPELINE_FRAMES = Object.freeze([
  {
    type: 'candidate',
    candidate: {
      id: 'cand-1',
      sentinel: 'flood',
      hazard_class: 'FLOOD',
      confidence: 0.62,
      evidence: [{ event_id: 'evt-1', source: 'nws', kind: 'synthetic.nws' }],
    },
  },
  { type: 'hazard', hazard: { id: 'haz-1', class: 'FLOOD', severity: 4 } },
  // The same hazard fused again is one hazard, not two.
  { type: 'hazard', hazard: { id: 'haz-1', class: 'FLOOD', severity: 5 } },
  {
    type: 'proposal',
    proposal: {
      id: 'prop-1',
      hazard_id: 'haz-1',
      action: { type: 'NO_ENTRY' },
    },
  },
  {
    type: 'verdict',
    verdict: { proposal_id: 'prop-1', decision: 'APPROVE' },
  },
  {
    type: 'compile',
    compile: {
      proposal_id: 'prop-1',
      advisory_id: 'adv-1',
      status: 'SIGNED',
      signature: 'base64...',
    },
  },
]);

test('the pipeline frames are kept as a transcript, hazards by identity', () => {
  const feed = connected();
  feed.send(scenarioFrame());
  for (const frame of PIPELINE_FRAMES) feed.send(frame);
  feed.send({ type: 'ack', ack: { operator: 'op_a', vehicles: 6 } });
  feed.send({ type: 'ack', ack: { operator: 'op_b', vehicles: 4 } });
  // A fleet that acknowledges twice has still answered once.
  feed.send({ type: 'ack', ack: { operator: 'op_a', vehicles: 6 } });
  const { pipeline } = feed.source.getStatus();
  assert.deepEqual(pipeline.tallies, {
    candidate: 1,
    hazard: 2,
    proposal: 1,
    verdict: 1,
    compile: 1,
    advisory: 0,
    ack: 3,
  });
  assert.equal(pipeline.hazardCount, 1);
  // The reasoning survives the frame that carried it: the rail shows what
  // each stage produced, so a tally alone would not be enough.
  assert.deepEqual(
    [
      pipeline.candidates.length,
      pipeline.hazards.length,
      pipeline.proposals.length,
      pipeline.verdicts.length,
      pipeline.compiles.length,
    ],
    [1, 2, 1, 1, 1],
  );
  assert.equal(pipeline.candidates[0].evidence[0].source, 'nws');
  assert.equal(pipeline.verdicts[0].decision, 'APPROVE');
  assert.equal(pipeline.compiles[0].advisoryId, 'adv-1');
  // Arrival times, so "time to advisory" is a measurement and not a claim
  // copied off a publisher's clock.
  assert.ok(Number.isFinite(pipeline.firstSignalAt));
  assert.equal(pipeline.advisoryAt, null);
  assert.deepEqual(
    pipeline.acks.map(({ operator, vehicles, status, seq }) => ({
      operator,
      vehicles,
      status,
      seq,
    })),
    [
      // `seq` moves on the SECOND answer from op_a, which is what lets the
      // layer flash that fleet again for an answer it has already counted.
      { operator: 'op_a', vehicles: 6, status: 'ACCEPTED', seq: 3 },
      { operator: 'op_b', vehicles: 4, status: 'ACCEPTED', seq: 2 },
    ],
  );
});

test('a refusal is carried through as one, not counted as an answer', () => {
  const feed = connected();
  feed.send(scenarioFrame());
  feed.send({
    type: 'ack',
    ack: { operator: 'op_c', vehicles: 3, status: 'DECLINED' },
  });
  const [ack] = feed.source.getStatus().pipeline.acks;
  assert.deepEqual(
    { operator: ack.operator, status: ack.status, seq: ack.seq },
    { operator: 'op_c', status: 'DECLINED', seq: 1 },
  );
});

test('assist requests are whole state, and counters are kept as reported', () => {
  const feed = connected();
  feed.send(scenarioFrame());
  feed.send({
    type: 'assist',
    assists: [
      { id: 'asr-1', op: 'op_ghost', lon: -98.4, lat: 29.52, ghost: true },
      { id: 'asr-2', op: 'op_ghost', lon: -98.399, lat: 29.52, ghost: true },
    ],
  });
  feed.send({
    type: 'counters',
    counters: {
      hazard_entries: { live: 0, ghost: 4 },
      assist_queue: { live: 0, ghost: 2 },
    },
  });
  let { pipeline } = feed.source.getStatus();
  assert.equal(pipeline.assists.length, 2);
  assert.deepEqual(pipeline.counters.hazardEntries, { live: 0, ghost: 4 });
  // A request a human cleared leaves, because the frame is the whole set.
  feed.send({ type: 'assist', assists: [] });
  ({ pipeline } = feed.source.getStatus());
  assert.deepEqual(pipeline.assists, []);
  // And the counters it reported are not retracted by that.
  assert.deepEqual(pipeline.counters.assistQueue, { live: 0, ghost: 2 });
});

test('the instant an advisory arrives is recorded once, not per frame', () => {
  const clock = { ms: 1_000 };
  const feed = connected({ now: () => clock.ms });
  feed.send(scenarioFrame());
  clock.ms = 2_000;
  feed.send({
    type: 'candidate',
    candidate: { id: 'cand-1', sentinel: 'flood', hazard_class: 'FLOOD' },
  });
  clock.ms = 9_500;
  feed.send(advisoryFrame());
  clock.ms = 12_000;
  // A second version of the same advisory is not a second publication: the
  // number on the strip is "how long did the first one take".
  feed.send(advisoryFrame());
  const { pipeline } = feed.source.getStatus();
  assert.equal(pipeline.firstSignalAt, 2_000);
  assert.equal(pipeline.advisoryAt, 9_500);
});

test('the real API is asked first and the mock is the fallback', () => {
  const opened = [];
  const sockets = [];
  const timers = [];
  const source = createHeadsupViewerSource({
    endpoint: 'ws://localhost:8765/viewer',
    fallbackEndpoint: 'ws://localhost:8766/viewer',
    socketFactory: (url) => {
      opened.push(url);
      const socket = fakeSocket();
      sockets.push(socket);
      return socket;
    },
    now: () => 1_000,
    setTimer: (handler, delayMs) => {
      timers.push({ handler, delayMs });
      return timers.length;
    },
    clearTimer: () => {},
  });
  source.connect();
  assert.deepEqual(opened, ['ws://localhost:8765/viewer']);
  assert.equal(source.getStatus().fallback, false);

  // Nothing is listening on the real API: the socket closes without ever
  // having opened, so the next candidate is tried rather than this one again.
  sockets.at(-1).onclose();
  assert.equal(timers.length, 1);
  assert.ok(timers[0].delayMs <= 1_000, 'the next endpoint is tried promptly');
  timers.pop().handler();
  assert.deepEqual(opened, [
    'ws://localhost:8765/viewer',
    'ws://localhost:8766/viewer',
  ]);
  sockets.at(-1).onopen();
  const status = source.getStatus();
  assert.equal(status.endpoint, 'ws://localhost:8766/viewer');
  assert.equal(status.primaryEndpoint, 'ws://localhost:8765/viewer');
  assert.equal(status.fallback, true);

  // A feed that WAS serving and went away keeps its endpoint for the first
  // retry: a restarted API is not a reason to move a working page.
  sockets.at(-1).onclose();
  timers.pop().handler();
  assert.equal(opened.at(-1), 'ws://localhost:8766/viewer');
});

test('a fallback equal to the primary is not a second endpoint', () => {
  const opened = [];
  const sockets = [];
  const timers = [];
  const source = createHeadsupViewerSource({
    endpoint: 'ws://localhost:8765/viewer',
    fallbackEndpoint: 'ws://localhost:8765/viewer',
    socketFactory: (url) => {
      opened.push(url);
      const socket = fakeSocket();
      sockets.push(socket);
      return socket;
    },
    now: () => 1_000,
    setTimer: (handler) => {
      timers.push(handler);
      return timers.length;
    },
    clearTimer: () => {},
  });
  assert.deepEqual(source.endpoints, ['ws://localhost:8765/viewer']);
  source.connect();
  sockets.at(-1).onclose();
  timers.pop()();
  assert.deepEqual(opened, [
    'ws://localhost:8765/viewer',
    'ws://localhost:8765/viewer',
  ]);
  assert.equal(source.getStatus().fallback, false);
});

test('a new run never inherits the previous run`s tally', () => {
  const feed = connected();
  feed.send(scenarioFrame());
  feed.send({
    type: 'hazard',
    hazard: { id: 'haz-1', class: 'FLOOD', severity: 4 },
  });
  feed.send({ type: 'ack', ack: { operator: 'op_a', vehicles: 6 } });
  feed.send({
    type: 'counters',
    counters: {
      hazard_entries: { live: 2, ghost: 9 },
      assist_queue: { live: 0, ghost: 3 },
    },
  });
  assert.equal(feed.source.getStatus().pipeline.hazardCount, 1);
  feed.send(scenarioFrame({ state: 'idle', scenario: undefined }));
  const { pipeline } = feed.source.getStatus();
  assert.deepEqual(pipeline.tallies, {
    candidate: 0,
    hazard: 0,
    proposal: 0,
    verdict: 0,
    compile: 0,
    advisory: 0,
    ack: 0,
  });
  // The transcript goes with the tally: a second replay must not show the
  // first one's reasoning, its queue or its totals.
  assert.deepEqual(
    {
      hazardCount: pipeline.hazardCount,
      acks: pipeline.acks,
      candidates: pipeline.candidates,
      hazards: pipeline.hazards,
      proposals: pipeline.proposals,
      verdicts: pipeline.verdicts,
      compiles: pipeline.compiles,
      assists: pipeline.assists,
      counters: pipeline.counters,
      firstSignalAt: pipeline.firstSignalAt,
      advisoryAt: pipeline.advisoryAt,
    },
    {
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
    },
  );
});

test('a damaged scenario or pipeline frame is rejected, not half applied', () => {
  const feed = connected();
  feed.send(scenarioFrame());
  feed.send({ type: 'scenario_state', state: 'paused' });
  feed.send({ type: 'hazard', hazard: { id: 'haz-1', class: 'NOPE' } });
  feed.send({ type: 'ack', ack: {} });
  const status = feed.source.getStatus();
  // The readout keeps the run it had; the frames are counted as rejected.
  assert.equal(status.scenario.state, 'running');
  assert.equal(status.pipeline.tallies.hazard, 0);
  assert.equal(status.pipeline.tallies.ack, 0);
  assert.equal(status.rejectedFrames, 3);
});

test('a frame kind this build does not know disturbs nothing', () => {
  const feed = connected();
  feed.send(scenarioFrame());
  feed.send({ type: 'something_newer', detail: 'from a later publisher' });
  const status = feed.source.getStatus();
  assert.equal(status.rejectedFrames, 0);
  assert.equal(status.scenario.id, 's1_flood');
});

test('telemetry carries the ghost and reroute cohorts through to the layer', () => {
  const feed = connected();
  feed.send({
    type: 'telemetry',
    vehicles: [
      { id: 'veh-1', op: 'op_a', lon: -98.4, lat: 29.52, rerouted_by: 'adv-1' },
      { id: 'veh-2', op: 'op_ghost', lon: -98.4, lat: 29.52, ghost: true },
    ],
  });
  assert.deepEqual(
    feed.source.getStatus().vehicles.map(({ id, ghost, reroutedBy }) => ({
      id,
      ghost,
      reroutedBy,
    })),
    [
      { id: 'veh-1', ghost: false, reroutedBy: 'adv-1' },
      { id: 'veh-2', ghost: true, reroutedBy: null },
    ],
  );
});

test('reset forgets the picture and the run without closing the socket', () => {
  const feed = connected();
  feed.send(scenarioFrame());
  feed.send({
    type: 'telemetry',
    vehicles: [{ id: 'veh-1', op: 'op_a', lon: -98.4, lat: 29.52 }],
  });
  feed.send({
    type: 'hazard',
    hazard: { id: 'haz-1', class: 'FLOOD', severity: 4 },
  });
  const published = [];
  feed.source.subscribe((snapshot) => published.push(snapshot));
  feed.source.reset();
  const status = feed.source.getStatus();
  assert.deepEqual(
    {
      vehicles: status.vehicles.length,
      advisories: status.advisories.length,
      scenario: status.scenario,
      hazards: status.pipeline.hazardCount,
      connected: status.connected,
      closed: feed.sockets.at(-1).closed,
      published: published.length,
    },
    {
      vehicles: 0,
      advisories: 0,
      scenario: null,
      hazards: 0,
      connected: true,
      closed: 0,
      published: 1,
    },
  );
});
