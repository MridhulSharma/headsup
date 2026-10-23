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

function connected() {
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
    now: () => 1_000,
    setTimer: () => null,
    clearTimer: () => {},
  });
  source.connect();
  const send = (frame) =>
    sockets.at(-1).onmessage({ data: JSON.stringify(frame) });
  return { source, sockets, send };
}

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

test('the pipeline frames are tallied, hazards and acks by identity', () => {
  const feed = connected();
  feed.send(scenarioFrame());
  feed.send({ type: 'candidate', candidate: { id: 'cand-1' } });
  feed.send({
    type: 'hazard',
    hazard: { id: 'haz-1', class: 'FLOOD', severity: 4 },
  });
  // The same hazard fused again is one hazard, not two.
  feed.send({
    type: 'hazard',
    hazard: { id: 'haz-1', class: 'FLOOD', severity: 5 },
  });
  feed.send({ type: 'proposal', proposal: { id: 'prop-1' } });
  feed.send({ type: 'verdict', verdict: { approved: true } });
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
    advisory: 0,
    ack: 3,
  });
  assert.equal(pipeline.hazards, 1);
  assert.deepEqual(pipeline.acks, [
    // `seq` moves on the SECOND answer from op_a, which is what lets the
    // layer flash that fleet again for an answer it has already counted.
    { operator: 'op_a', vehicles: 6, status: 'ACCEPTED', seq: 3 },
    { operator: 'op_b', vehicles: 4, status: 'ACCEPTED', seq: 2 },
  ]);
});

test('a refusal is carried through as one, not counted as an answer', () => {
  const feed = connected();
  feed.send(scenarioFrame());
  feed.send({
    type: 'ack',
    ack: { operator: 'op_c', vehicles: 3, status: 'DECLINED' },
  });
  assert.deepEqual(feed.source.getStatus().pipeline.acks, [
    { operator: 'op_c', vehicles: 3, status: 'DECLINED', seq: 1 },
  ]);
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
  assert.equal(feed.source.getStatus().pipeline.hazards, 1);
  feed.send(scenarioFrame({ state: 'idle', scenario: undefined }));
  assert.deepEqual(feed.source.getStatus().pipeline, {
    tallies: {
      candidate: 0,
      hazard: 0,
      proposal: 0,
      verdict: 0,
      advisory: 0,
      ack: 0,
    },
    hazards: 0,
    acks: [],
  });
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
      hazards: status.pipeline.hazards,
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
