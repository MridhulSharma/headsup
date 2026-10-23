import assert from 'node:assert/strict';
import test from 'node:test';
import {
  SPARKLINE_SAMPLES,
  SPARKLINE_SAMPLE_MS,
  buildPipelineView,
  feedChips,
  latestCompiles,
  pendingApproval,
  sampleAssistQueue,
  timeToAdvisoryMs,
} from './pipeline.js';

/**
 * The rail's rows, as a transcript turns into them.
 *
 * These tests are about the three rules in `pipeline.js`: a stage lights
 * from its own event, nothing is inferred from a stage's neighbours, and an
 * answer this build cannot read is never an approval.
 */

const evidence = (source) => ({
  eventId: `evt-${source}`,
  source,
  kind: `synthetic.${source}`,
  observedAt: null,
  note: null,
  rawRef: null,
});

const candidate = (overrides = {}) => ({
  id: 'cand-1',
  sentinel: 'flood',
  hazardClass: 'FLOOD',
  confidence: 0.62,
  observedAt: null,
  note: null,
  evidence: [evidence('nws')],
  rawText: null,
  extracted: [],
  at: 1_000,
  ...overrides,
});

const hazard = (overrides = {}) => ({
  id: 'haz-1',
  hazardClass: 'FLOOD',
  severity: 4,
  confidence: 0.91,
  rationale: 'One warning polygon and two gauges above action stage.',
  validUntil: null,
  evidence: [evidence('nws'), evidence('usgs')],
  at: 3_000,
  ...overrides,
});

const proposal = (overrides = {}) => ({
  id: 'prop-1',
  hazardId: 'haz-1',
  revision: 1,
  action: 'NO_ENTRY',
  playbook: 'flood',
  policyRule: 'flood.no_entry',
  validUntil: null,
  rationale: null,
  at: 4_500,
  ...overrides,
});

const verdict = (overrides = {}) => ({
  proposalId: 'prop-1',
  decision: 'APPROVE',
  decidedAt: null,
  checks: [{ rule: 'area_under_cap', passed: true }],
  reasons: [],
  rationale: null,
  at: 6_000,
  ...overrides,
});

const compiled = (overrides = {}) => ({
  proposalId: 'prop-1',
  advisoryId: 'adv-1',
  status: 'SIGNED',
  decidedAt: null,
  issuer: 'headsup/compiler',
  policyRule: 'flood.no_entry',
  signed: true,
  signatureVerified: true,
  reasons: [],
  twoKey: false,
  at: 7_000,
  ...overrides,
});

const snapshot = (pipeline = {}) => ({
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
    ...pipeline,
  },
});

const toneOf = (view, key) =>
  view.stages.find((stage) => stage.key === key)?.tone;
const stageOf = (view, key) => view.stages.find((stage) => stage.key === key);

test('an empty transcript lights nothing and claims no numbers', () => {
  const view = buildPipelineView({ snapshot: snapshot(), stats: {} });
  assert.deepEqual(
    view.stages.map((stage) => [stage.key, stage.lit, stage.tone]),
    [
      ['feeds', false, 'idle'],
      ['sentinel', false, 'idle'],
      ['fusion', false, 'idle'],
      ['proposer', false, 'idle'],
      ['critic', false, 'idle'],
      ['compiler', false, 'idle'],
    ],
  );
  assert.deepEqual(view.operators, []);
  assert.equal(view.pending, null);
  // Null, not zero: "nobody has told us" is not "nothing happened".
  assert.equal(view.counters.hazardEntries, null);
  assert.equal(view.counters.assistQueue, null);
  assert.equal(view.counters.timeToAdvisoryMs, null);
});

test('a stage lights from its own event, and only its own', () => {
  // A fused hazard, and nothing else. An advisory on the map would not make
  // the compiler row light: "something was published, so the compiler must
  // have signed it" is the reasoning an audit trail replaces.
  const view = buildPipelineView({
    snapshot: snapshot({ hazards: [hazard()] }),
    stats: {},
  });
  assert.equal(stageOf(view, 'fusion').lit, true);
  assert.equal(stageOf(view, 'sentinel').lit, false);
  assert.equal(stageOf(view, 'critic').lit, false);
  assert.equal(stageOf(view, 'compiler').lit, false);
  // Fusion's row carries the one number fusion exists to produce.
  assert.equal(stageOf(view, 'fusion').value, '91%');
  // And the feeds row lights off the hazard's own evidence.
  assert.equal(stageOf(view, 'feeds').lit, true);
  assert.equal(stageOf(view, 'feeds').value, '2');
});

test('a stage lights at its FIRST event and keeps that instant', () => {
  const view = buildPipelineView({
    snapshot: snapshot({
      proposals: [proposal({ at: 4_500 }), proposal({ id: 'prop-2', at: 9_000 })],
    }),
    stats: {},
  });
  const stage = stageOf(view, 'proposer');
  assert.equal(stage.firstAt, 4_500);
  // The row shows the latest round, the card has both.
  assert.equal(stage.latest.id, 'prop-2');
  assert.equal(stage.records.length, 2);
});

test('the feeds row names the sources the evidence actually cites', () => {
  const chips = feedChips({
    candidates: [candidate({ evidence: [evidence('nws')], at: 1_000 })],
    hazards: [
      hazard({ evidence: [evidence('nws'), evidence('usgs')], at: 3_000 }),
    ],
  });
  assert.deepEqual(
    chips.map((chip) => [chip.source, chip.events, chip.label]),
    [
      // Order of first arrival, which is information an alphabetical list
      // would throw away.
      ['nws', 2, 'NWS'],
      ['usgs', 1, 'USGS'],
    ],
  );
  // A source this build has no name for is shown as it arrived, not as
  // "Unknown": which feed an advisory rests on is the whole point.
  const [chip] = feedChips({
    candidates: [candidate({ evidence: [evidence('travis_county_oem')] })],
  });
  assert.equal(chip.source, 'travis_county_oem');
  assert.equal(chip.label, null);
});

test('the critic`s three answers read as three different states', () => {
  const tone = (decision) =>
    toneOf(
      buildPipelineView({
        snapshot: snapshot({ verdicts: [verdict({ decision })] }),
        stats: {},
      }),
      'critic',
    );
  assert.equal(tone('APPROVE'), 'ok');
  // Waiting for a person is not the same as having been refused.
  assert.equal(tone('REVISE'), 'wait');
  assert.equal(tone('ESCALATE'), 'wait');
});

test('the compiler`s three answers read as three different states', () => {
  const tone = (status) =>
    toneOf(
      buildPipelineView({
        snapshot: snapshot({ compiles: [compiled({ status })] }),
        stats: {},
      }),
      'compiler',
    );
  assert.equal(tone('SIGNED'), 'ok');
  assert.equal(tone('PENDING_APPROVAL'), 'wait');
  assert.equal(tone('REJECTED'), 'alert');
});

test('the compiler row reads the last word on a proposal, not the first', () => {
  const held = compiled({
    status: 'PENDING_APPROVAL',
    advisoryId: null,
    signed: false,
    signatureVerified: null,
    twoKey: true,
    reasons: ['two_key_approval_required'],
    at: 9_000,
  });
  const signed = compiled({ at: 12_000 });
  const map = latestCompiles([held, signed]);
  assert.equal(map.size, 1);
  assert.equal(map.get('prop-1').status, 'SIGNED');
  const view = buildPipelineView({
    snapshot: snapshot({ compiles: [held, signed] }),
    stats: {},
  });
  assert.equal(stageOf(view, 'compiler').value, 'SIGNED');
  // The advisory id comes from the compiler that signed, not from anywhere
  // else on the screen.
  assert.equal(view.advisoryId, 'adv-1');
  // And the button is gone: that decision is no longer anybody's to make.
  assert.equal(view.pending, null);
});

test('only a held two-key advisory offers an approval', () => {
  const held = (overrides) =>
    pendingApproval({
      compiles: [
        compiled({
          status: 'PENDING_APPROVAL',
          advisoryId: null,
          signed: false,
          twoKey: true,
          reasons: ['two_key_approval_required'],
          ...overrides,
        }),
      ],
    });
  assert.deepEqual(held(), {
    proposalId: 'prop-1',
    reasons: ['two_key_approval_required'],
    at: 7_000,
  });
  // A status that is not a hold is not an approval to offer...
  assert.equal(held({ status: 'REJECTED' }), null);
  assert.equal(held({ status: 'SIGNED' }), null);
  // ...and neither is a hold that never claimed to need a second key.
  assert.equal(held({ twoKey: false }), null);
});

test('one row per operator, in the order the fleets answered', () => {
  const view = buildPipelineView({
    snapshot: snapshot({
      compiles: [compiled()],
      acks: [
        {
          operator: 'op_b',
          status: 'DELAYED',
          vehicles: 4,
          actionTaken: 'advisory received; applying at next route refresh',
          reason: 'route solver runs on a 90 second cycle',
          advisoryId: 'adv-1',
          seq: 2,
          at: 9_000,
        },
        {
          operator: 'op_a',
          status: 'ACCEPTED',
          vehicles: 6,
          actionTaken: 'NO_ENTRY applied',
          reason: null,
          advisoryId: 'adv-1',
          seq: 1,
          at: 8_500,
        },
      ],
    }),
    stats: {},
  });
  assert.deepEqual(
    view.operators.map((row) => [row.op, row.status, row.tone]),
    [
      ['op_a', 'ACCEPTED', 'ok'],
      // Not yet is not no, and the rail says so in its own colour.
      ['op_b', 'DELAYED', 'wait'],
    ],
  );
  assert.equal(view.operators[0].linkedId, 'adv-1');
});

test('a refusal reads as a refusal, not as a delay', () => {
  const view = buildPipelineView({
    snapshot: snapshot({
      acks: [{ operator: 'op_b', status: 'DECLINED', vehicles: 4, seq: 1 }],
    }),
    stats: {},
  });
  assert.equal(view.operators[0].tone, 'alert');
});

test('every row carries the id that ties it to the globe', () => {
  const view = buildPipelineView({
    snapshot: snapshot({
      candidates: [candidate()],
      hazards: [hazard()],
      proposals: [proposal()],
      verdicts: [verdict()],
      compiles: [compiled()],
    }),
    stats: {},
  });
  assert.equal(view.hazardId, 'haz-1');
  assert.equal(view.hazardClass, 'FLOOD');
  assert.deepEqual(
    view.stages.map((stage) => stage.linkedId),
    ['haz-1', 'cand-1', 'haz-1', 'haz-1', 'prop-1', 'adv-1'],
  );
});

test('time to advisory is measured between two observed instants', () => {
  assert.equal(
    timeToAdvisoryMs({ firstSignalAt: 1_000, advisoryAt: 9_500 }),
    8_500,
  );
  // Still running: no answer yet, and a partial elapsed time would read as
  // one.
  assert.equal(timeToAdvisoryMs({ firstSignalAt: 1_000, advisoryAt: null }), null);
  assert.equal(timeToAdvisoryMs({ firstSignalAt: null, advisoryAt: 9_500 }), null);
  // A publication that appears to precede the signal is not a measurement.
  assert.equal(
    timeToAdvisoryMs({ firstSignalAt: 9_500, advisoryAt: 1_000 }),
    null,
  );
});

test('rerouted is the one counter the viewer derives for itself', () => {
  const view = buildPipelineView({
    snapshot: snapshot({
      counters: {
        hazardEntries: { live: 0, ghost: 7 },
        assistQueue: { live: 0, ghost: 5 },
      },
    }),
    stats: { rerouted: 6 },
  });
  // The two cohort tallies come off the feed, because only the simulator
  // knows them; rerouted is a property of the telemetry already on screen.
  assert.deepEqual(view.counters.hazardEntries, { live: 0, ghost: 7 });
  assert.deepEqual(view.counters.assistQueue, { live: 0, ghost: 5 });
  assert.equal(view.counters.rerouted, 6);
});

test('the sparkline samples the queue once a second, and bounds the window', () => {
  const samples = [];
  const queue = { live: 0, ghost: 3 };
  sampleAssistQueue(samples, queue, 1_000);
  // Inside the interval: a redraw is not a new reading.
  sampleAssistQueue(samples, queue, 1_000 + SPARKLINE_SAMPLE_MS - 1);
  assert.equal(samples.length, 1);
  sampleAssistQueue(samples, queue, 1_000 + SPARKLINE_SAMPLE_MS);
  assert.equal(samples.length, 2);
  // A tick with nothing reported changes nothing.
  sampleAssistQueue(samples, null, 99_000);
  assert.equal(samples.length, 2);
  for (let index = 0; index < SPARKLINE_SAMPLES * 2; index++)
    sampleAssistQueue(samples, queue, 10_000 + index * SPARKLINE_SAMPLE_MS);
  assert.equal(samples.length, SPARKLINE_SAMPLES);
  // The window keeps the newest, so the rightmost bar is the latest reading.
  assert.equal(samples.at(-1).at, 10_000 + (SPARKLINE_SAMPLES * 2 - 1) * SPARKLINE_SAMPLE_MS);
});

test('a snapshot with no pipeline at all is a quiet rail, not a throw', () => {
  for (const input of [undefined, null, {}, { pipeline: null }]) {
    const view = buildPipelineView({ snapshot: input, stats: {} });
    assert.equal(view.stages.length, 6);
    assert.equal(view.operators.length, 0);
    assert.equal(view.pending, null);
  }
});
