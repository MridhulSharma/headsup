import assert from 'node:assert/strict';
import test from 'node:test';
import {
  normalizeAckFrame,
  normalizeAdvisoryFeature,
  normalizeAssistFrame,
  normalizeCandidateFrame,
  normalizeCompileFrame,
  normalizeCountersFrame,
  normalizeHazardFrame,
  normalizeProposalFrame,
  normalizeScenarioStateFrame,
  normalizeTelemetryFrame,
  normalizeVerdictFrame,
} from './records.js';

const vehicle = Object.freeze({
  id: 'veh-1',
  op: 'op_a',
  lon: -97.7431,
  lat: 30.2672,
  heading: 91.5,
});

const frame = (vehicles) => ({
  type: 'telemetry',
  tick: 7,
  sim_time: '2026-10-21T18:00:00Z',
  vehicles,
});

const ring = [
  [-97.75, 30.26],
  [-97.74, 30.26],
  [-97.74, 30.27],
  [-97.75, 30.27],
  [-97.75, 30.26],
];

const advisory = (overrides = {}, propertyOverrides = {}) => ({
  type: 'Feature',
  geometry: { type: 'Polygon', coordinates: [ring] },
  properties: {
    id: 'adv-flood-1',
    version: 2,
    hazard: { class: 'FLOOD', severity: 4, confidence: 0.8 },
    action: { type: 'NO_ENTRY' },
    valid_from: '2026-10-21T18:00:00Z',
    valid_until: '2026-10-21T20:00:00Z',
    evidence: [{ event_id: 'nws-1' }, { event_id: 'usgs-1' }],
    policy_rule: 'flood.no_entry',
    issuer: 'headsup/compiler',
    signature: 'c2lnbmF0dXJl',
    ...propertyOverrides,
  },
  ...overrides,
});

test('a well-formed telemetry frame becomes one row per vehicle', () => {
  assert.deepEqual(normalizeTelemetryFrame(frame([vehicle])), [
    {
      id: 'veh-1',
      op: 'op_a',
      lon: -97.7431,
      lat: 30.2672,
      heading: 91.5,
      ghost: false,
      reroutedBy: null,
    },
  ]);
});

test('a ghost vehicle and a rerouted one are separable cohorts', () => {
  const rows = normalizeTelemetryFrame(
    frame([
      { ...vehicle, rerouted_by: 'adv-1' },
      { ...vehicle, id: 'veh-2', op: 'op_ghost', ghost: true },
    ]),
  );
  assert.deepEqual(
    rows.map(({ id, ghost, reroutedBy }) => ({ id, ghost, reroutedBy })),
    [
      { id: 'veh-1', ghost: false, reroutedBy: 'adv-1' },
      { id: 'veh-2', ghost: true, reroutedBy: null },
    ],
  );
});

test('a damaged reroute claim rejects the tick rather than dropping it', () => {
  // "This vehicle moved because of that advisory" is the demo's evidence, so
  // half of it is worse than none: the last good fleet stays on screen.
  assert.equal(
    normalizeTelemetryFrame(frame([{ ...vehicle, rerouted_by: 'not a id!' }])),
    null,
  );
  assert.equal(
    normalizeTelemetryFrame(frame([{ ...vehicle, rerouted_by: 42 }])),
    null,
  );
});

test('a ghost flag is only the exact boolean', () => {
  const rows = normalizeTelemetryFrame(frame([{ ...vehicle, ghost: 'yes' }]));
  assert.equal(rows[0].ghost, false);
});

test('a frame without vehicles is an empty fleet, not a rejection', () => {
  assert.deepEqual(normalizeTelemetryFrame(frame(undefined)), []);
  assert.deepEqual(normalizeTelemetryFrame(frame([])), []);
});

test('a heading outside the contract range reads as zero', () => {
  const rows = normalizeTelemetryFrame(
    frame([
      { ...vehicle, heading: 360 },
      { ...vehicle, id: 'veh-2' },
    ]),
  );
  assert.equal(rows[0].heading, 0);
  assert.equal(rows[1].heading, 91.5);
});

test('one unusable vehicle rejects the whole tick', () => {
  for (const broken of [
    { ...vehicle, id: '' },
    { ...vehicle, id: 'veh 1' },
    { ...vehicle, op: null },
    { ...vehicle, lon: 181 },
    { ...vehicle, lat: -91 },
    { ...vehicle, lon: 'west' },
    { ...vehicle, lat: Number.NaN },
    null,
    [],
  ]) {
    assert.equal(
      normalizeTelemetryFrame(frame([vehicle, broken])),
      null,
      `accepted ${JSON.stringify(broken)}`,
    );
  }
});

test('a duplicate vehicle id rejects the tick rather than picking one', () => {
  assert.equal(normalizeTelemetryFrame(frame([vehicle, vehicle])), null);
});

test('only a telemetry frame normalizes as telemetry', () => {
  assert.equal(normalizeTelemetryFrame({ type: 'advisory' }), null);
  assert.equal(normalizeTelemetryFrame(null), null);
  assert.equal(normalizeTelemetryFrame([frame([vehicle])]), null);
  assert.equal(normalizeTelemetryFrame({ ...frame([]), vehicles: {} }), null);
});

test('a tick larger than a city is not a tick', () => {
  const crowd = Array.from({ length: 5001 }, (_, index) => ({
    ...vehicle,
    id: `veh-${index}`,
  }));
  assert.equal(normalizeTelemetryFrame(frame(crowd)), null);
});

test('a polygon advisory normalizes to drawable rings and a window', () => {
  const row = normalizeAdvisoryFeature(advisory());
  assert.equal(row.id, 'adv-flood-1');
  assert.equal(row.version, 2);
  assert.equal(row.geometryType, 'Polygon');
  assert.equal(row.polygons.length, 1);
  assert.deepEqual(row.polygons[0][0], ring);
  assert.equal(row.hazardClass, 'FLOOD');
  assert.equal(row.severity, 4);
  assert.equal(row.action, 'NO_ENTRY');
  assert.equal(row.validFrom, Date.parse('2026-10-21T18:00:00Z'));
  assert.equal(row.validUntil, Date.parse('2026-10-21T20:00:00Z'));
  assert.equal(row.evidenceCount, 2);
  assert.equal(row.policyRule, 'flood.no_entry');
  assert.equal(row.signed, true);
});

test('a multipolygon advisory keeps every area and its holes', () => {
  const hole = [
    [-97.748, 30.262],
    [-97.746, 30.262],
    [-97.746, 30.264],
    [-97.748, 30.264],
    [-97.748, 30.262],
  ];
  const row = normalizeAdvisoryFeature(
    advisory({
      geometry: {
        type: 'MultiPolygon',
        coordinates: [[ring, hole], [ring]],
      },
    }),
  );
  assert.equal(row.polygons.length, 2);
  assert.equal(row.polygons[0].length, 2);
  assert.deepEqual(row.polygons[0][1], hole);
  assert.equal(row.polygons[1].length, 1);
});

test('a geometry this layer cannot draw is still a counted advisory', () => {
  const row = normalizeAdvisoryFeature(
    advisory({ geometry: { type: 'Point', coordinates: [-97.74, 30.27] } }),
  );
  assert.equal(row.geometryType, 'Point');
  assert.deepEqual(row.polygons, []);
  assert.deepEqual(row.anchors, [[-97.74, 30.27]]);
});

test('an unsigned advisory is recorded as unsigned, not rejected', () => {
  assert.equal(
    normalizeAdvisoryFeature(advisory({}, { signature: null })).signed,
    false,
  );
  const { signature, ...rest } = advisory().properties;
  assert.equal(signature, 'c2lnbmF0dXJl');
  assert.equal(
    normalizeAdvisoryFeature({ ...advisory(), properties: rest }).signed,
    false,
  );
});

test('an advisory outside the contract is rejected whole', () => {
  assert.equal(normalizeAdvisoryFeature(null), null);
  assert.equal(normalizeAdvisoryFeature(advisory({ type: 'Point' })), null);
  assert.equal(
    normalizeAdvisoryFeature(advisory({ geometry: { type: 'Circle' } })),
    null,
  );
  for (const properties of [
    { id: 'not a valid id' },
    { hazard: { class: 'ASTEROID', severity: 3, confidence: 0.5 } },
    { hazard: { class: 'FLOOD', severity: 6, confidence: 0.5 } },
    { hazard: { class: 'FLOOD', severity: 2.5, confidence: 0.5 } },
    { hazard: { class: 'FLOOD', severity: 3, confidence: 1.5 } },
    { action: { type: 'TOW_AWAY' } },
    { valid_from: 'yesterday' },
    { valid_until: null },
    // A window that ends before it opens is never in force.
    { valid_from: '2026-10-21T20:00:00Z', valid_until: '2026-10-21T18:00:00Z' },
  ]) {
    assert.equal(
      normalizeAdvisoryFeature(advisory({}, properties)),
      null,
      `accepted ${JSON.stringify(properties)}`,
    );
  }
});

test('a ring that cannot close, or carries a bad vertex, is rejected', () => {
  for (const coordinates of [
    [
      [
        [-97.75, 30.26],
        [-97.74, 30.26],
        [-97.75, 30.26],
      ],
    ],
    [[...ring.slice(0, 4), [-97.75, 'north']]],
    [[...ring.slice(0, 4), [-181, 30.26]]],
    [[]],
    [ring, 'hole'],
  ]) {
    assert.equal(
      normalizeAdvisoryFeature(
        advisory({ geometry: { type: 'Polygon', coordinates } }),
      ),
      null,
      `accepted ${JSON.stringify(coordinates)}`,
    );
  }
});

test('a corridor advisory normalizes to drawable lines, not rings', () => {
  const row = normalizeAdvisoryFeature(
    advisory({
      geometry: {
        type: 'LineString',
        coordinates: [
          [-97.75, 30.26],
          [-97.74, 30.265],
        ],
      },
    }),
  );
  assert.equal(row.geometryType, 'LineString');
  assert.deepEqual(row.polygons, []);
  assert.deepEqual(row.lines, [
    [
      [-97.75, 30.26],
      [-97.74, 30.265],
    ],
  ]);
});

test('a multilinestring corridor keeps every leg', () => {
  const row = normalizeAdvisoryFeature(
    advisory({
      geometry: {
        type: 'MultiLineString',
        coordinates: [
          [
            [-97.75, 30.26],
            [-97.74, 30.26],
          ],
          [
            [-97.74, 30.27],
            [-97.73, 30.27],
          ],
        ],
      },
    }),
  );
  assert.equal(row.lines.length, 2);
});

test('a corridor leg of one position is not a corridor', () => {
  assert.equal(
    normalizeAdvisoryFeature(
      advisory({
        geometry: { type: 'LineString', coordinates: [[-97.75, 30.26]] },
      }),
    ),
    null,
  );
});

test('a scenario-state frame carries the clock, the speed and the name', () => {
  assert.deepEqual(
    normalizeScenarioStateFrame({
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
    }),
    {
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
  );
});

test('an idle scenario-state frame names no scenario', () => {
  const row = normalizeScenarioStateFrame({
    type: 'scenario_state',
    state: 'idle',
    speed: 1,
  });
  assert.equal(row.state, 'idle');
  assert.equal(row.id, null);
  assert.equal(row.name, null);
  assert.equal(row.speed, 1);
});

test('a state outside the contract is not a scenario state', () => {
  assert.equal(
    normalizeScenarioStateFrame({ type: 'scenario_state', state: 'paused' }),
    null,
  );
  assert.equal(normalizeScenarioStateFrame({ type: 'telemetry' }), null);
});

test('a scenario name carrying control characters is refused', () => {
  assert.equal(
    normalizeScenarioStateFrame({
      type: 'scenario_state',
      state: 'running',
      scenario: { id: 's1_flood', name: 'Flash\u0007flood' },
    }).name,
    null,
  );
});

test('an acknowledgement names one operator and a vehicle count', () => {
  assert.deepEqual(
    normalizeAckFrame({
      type: 'ack',
      ack: { advisory_id: 'adv-1', operator: 'op_a', vehicles: 6 },
    }),
    {
      advisoryId: 'adv-1',
      operator: 'op_a',
      vehicles: 6,
      status: 'ACCEPTED',
      actionTaken: null,
      reason: null,
      receivedAt: null,
    },
  );
});

test('the three answers an operator can give are kept apart', () => {
  const answer = (status) =>
    normalizeAckFrame({
      type: 'ack',
      ack: { operator: 'op_b', vehicles: 4, status },
    }).status;
  assert.equal(answer('ACCEPTED'), 'ACCEPTED');
  // Not a shade of either of the others: the fleet has the advisory and has
  // not applied it yet, which is the one state a dispatcher can chase.
  assert.equal(answer('DELAYED'), 'DELAYED');
  assert.equal(answer('DECLINED'), 'DECLINED');
  // An answer this build cannot read is still not a yes.
  assert.equal(answer('MAYBE_LATER'), 'DECLINED');
});

test('an acknowledgement carries what the fleet says it did, and why', () => {
  const ack = normalizeAckFrame({
    type: 'ack',
    ack: {
      operator: 'op_b',
      vehicles: 4,
      status: 'DELAYED',
      action_taken: 'advisory received; applying at next route refresh',
      reason: 'fleet-side route solver runs on a 90 second cycle',
      received_at: '2026-04-20T18:50:00Z',
    },
  });
  assert.equal(ack.actionTaken, 'advisory received; applying at next route refresh');
  assert.equal(ack.reason, 'fleet-side route solver runs on a 90 second cycle');
  assert.equal(ack.receivedAt, Date.parse('2026-04-20T18:50:00Z'));
});

test('an answer that is not an acceptance is read as a refusal', () => {
  const read = (status) =>
    normalizeAckFrame({
      type: 'ack',
      ack: { operator: 'op_c', vehicles: 3, status },
    }).status;
  assert.equal(read('DECLINED'), 'DECLINED');
  assert.equal(read('declined'), 'DECLINED');
  assert.equal(read('accepted'), 'ACCEPTED');
  // A status this build has never heard of is not a yes, so it is not read
  // as one: an advisory nobody applied must not look applied.
  assert.equal(read('DEFERRED_PENDING_REVIEW'), 'DECLINED');
  assert.equal(read(undefined), 'ACCEPTED');
});

test('an acknowledgement without an operator is not one', () => {
  assert.equal(normalizeAckFrame({ type: 'ack', ack: { vehicles: 6 } }), null);
  assert.equal(normalizeAckFrame({ type: 'ack' }), null);
});

test('a fused hazard frame carries identity, class and severity', () => {
  assert.deepEqual(
    normalizeHazardFrame({
      type: 'hazard',
      hazard: { id: 'haz-1', class: 'FLOOD', severity: 4 },
    }),
    {
      id: 'haz-1',
      hazardClass: 'FLOOD',
      severity: 4,
      // Absent is null, not a guess: a hazard that reported no confidence
      // has not told this page it is 0.
      confidence: null,
      rationale: null,
      validUntil: null,
      evidence: [],
    },
  );
});

test('a fused hazard carries the confidence, rationale and evidence', () => {
  const hazard = normalizeHazardFrame({
    type: 'hazard',
    hazard: {
      id: 'haz-1',
      class: 'FLOOD',
      severity: 4,
      confidence: 0.91,
      rationale: 'One warning polygon and two gauges above action stage.',
      valid_until: '2026-04-21T00:50:00Z',
      evidence: [
        {
          event_id: 'evt-1',
          source: 'nws',
          kind: 'synthetic.nws.flash_flood_warning',
          note: 'warning polygon covers both crossings',
          observed_at: '2026-04-20T18:50:00Z',
        },
        { event_id: 'evt-2', source: 'usgs', kind: 'synthetic.usgs.gauge' },
      ],
    },
  });
  assert.equal(hazard.confidence, 0.91);
  assert.match(hazard.rationale, /action stage/);
  assert.deepEqual(
    hazard.evidence.map((row) => [row.source, row.note]),
    [
      ['nws', 'warning polygon covers both crossings'],
      ['usgs', null],
    ],
  );
});

test('evidence without a source rejects the frame it came on', () => {
  // The source is what lights a feed chip, and a chip is a claim about which
  // public feed an advisory rests on: an unnamed one is not a shorter claim.
  assert.equal(
    normalizeHazardFrame({
      type: 'hazard',
      hazard: {
        id: 'haz-1',
        class: 'FLOOD',
        severity: 4,
        evidence: [{ event_id: 'evt-1', kind: 'synthetic.nws.warning' }],
      },
    }),
    null,
  );
});

test('a hazard class outside the contract is rejected', () => {
  assert.equal(
    normalizeHazardFrame({
      type: 'hazard',
      hazard: { id: 'haz-1', class: 'ALIEN_INVASION', severity: 4 },
    }),
    null,
  );
});

// ── The reasoning frames the agent rail is drawn from ────────────────────

const dispatchCandidate = (overrides = {}) => ({
  type: 'candidate',
  candidate: {
    id: 'cand-1',
    sentinel: 'dispatch-text',
    hazard_class: 'EMERGENCY_SCENE',
    confidence: 0.71,
    observed_at: '2026-04-23T14:02:00Z',
    note: 'structure fire response closing two eastbound lanes',
    evidence: [
      {
        event_id: 'evt-1',
        source: 'austin_incidents',
        kind: 'synthetic.austin.active_incident_dispatch',
      },
    ],
    ...overrides,
  },
});

test('a candidate carries the sentinel, its reading and its evidence', () => {
  const candidate = normalizeCandidateFrame(dispatchCandidate());
  assert.equal(candidate.sentinel, 'dispatch-text');
  assert.equal(candidate.hazardClass, 'EMERGENCY_SCENE');
  assert.equal(candidate.confidence, 0.71);
  assert.deepEqual(
    candidate.evidence.map((row) => row.source),
    ['austin_incidents'],
  );
  // No message read, so nothing to show beside it.
  assert.equal(candidate.rawText, null);
  assert.deepEqual(candidate.extracted, []);
});

test('a dispatch candidate keeps the message and the fields read from it', () => {
  const candidate = normalizeCandidateFrame(
    dispatchCandidate({
      raw_text: 'AFD INC 2026-0423-00871 BOX ALARM\r\n2 LANES EB CLOSED',
      extracted: [
        { field: 'incident', value: 'AFD INC 2026-0423-00871' },
        { field: 'lanes_closed', value: '2 eastbound' },
      ],
    }),
  );
  // Line breaks survive — a dispatch record arrives with the shape it was
  // written in, and reflowing it would be editing the evidence. Carriage
  // returns are normalized so it does not render double-spaced.
  assert.equal(
    candidate.rawText,
    'AFD INC 2026-0423-00871 BOX ALARM\n2 LANES EB CLOSED',
  );
  assert.deepEqual(
    candidate.extracted.map((row) => [row.field, row.value]),
    [
      ['incident', 'AFD INC 2026-0423-00871'],
      ['lanes_closed', '2 eastbound'],
    ],
  );
});

test('extracted fields with no message to check them against are dropped', () => {
  // A list of fields beside no message is an assertion nobody can check,
  // which is the one thing this card exists to prevent.
  const candidate = normalizeCandidateFrame(
    dispatchCandidate({
      extracted: [{ field: 'units', value: 'Engine 4' }],
    }),
  );
  assert.deepEqual(candidate.extracted, []);
});

test('a damaged extracted field rejects the candidate, not just the field', () => {
  assert.equal(
    normalizeCandidateFrame(
      dispatchCandidate({
        raw_text: 'AFD INC 2026-0423-00871',
        extracted: [{ field: 'units', value: '' }],
      }),
    ),
    null,
  );
  // A control character in the message is not a line break.
  assert.equal(
    normalizeCandidateFrame(
      dispatchCandidate({ raw_text: 'AFD INC\u0007 00871' }),
    ),
    null,
  );
});

test('a proposal names one action from the contract', () => {
  const proposal = normalizeProposalFrame({
    type: 'proposal',
    proposal: {
      id: 'prop-1',
      hazard_id: 'haz-1',
      revision: 2,
      action: { type: 'KEEP_CLEAR' },
      playbook: 'emergency_scene',
      policy_rule: 'emergency_scene.keep_clear',
      rationale: 'Keep the two closed lanes clear for 45 minutes.',
    },
  });
  assert.equal(proposal.action, 'KEEP_CLEAR');
  assert.equal(proposal.revision, 2);
  assert.equal(proposal.playbook, 'emergency_scene');
  // An action outside the contract is not a proposal this layer will show.
  assert.equal(
    normalizeProposalFrame({
      type: 'proposal',
      proposal: { id: 'prop-1', action: { type: 'TOW_EVERYTHING' } },
    }),
    null,
  );
});

test('a verdict carries the decision, its checks and its reasons', () => {
  const verdict = normalizeVerdictFrame({
    type: 'verdict',
    verdict: {
      proposal_id: 'prop-1',
      decision: 'REVISE',
      decided_at: '2026-04-23T14:06:00Z',
      checks: [
        { rule: 'action_allowed_for_class', passed: true },
        { rule: 'area_under_cap', passed: false },
      ],
      reasons: ['area 2.4 km2 is over the 1.5 km2 cap for SIGNAL_OUTAGE'],
      rationale: 'The action is right and the area is not.',
    },
  });
  assert.equal(verdict.decision, 'REVISE');
  assert.deepEqual(
    verdict.checks.map((row) => [row.rule, row.passed]),
    [
      ['action_allowed_for_class', true],
      ['area_under_cap', false],
    ],
  );
  assert.equal(verdict.reasons.length, 1);
});

test('a decision this build cannot read is never an approval', () => {
  const decision = (value) =>
    normalizeVerdictFrame({
      type: 'verdict',
      verdict: { proposal_id: 'prop-1', decision: value },
    }).decision;
  assert.equal(decision('APPROVE'), 'APPROVE');
  assert.equal(decision('ESCALATE'), 'ESCALATE');
  assert.equal(decision('LOOKS_FINE_TO_ME'), 'ESCALATE');
  // And the older boolean form still reads, rather than being dropped.
  assert.equal(
    normalizeVerdictFrame({
      type: 'verdict',
      verdict: { proposal_id: 'prop-1', approved: true },
    }).decision,
    'APPROVE',
  );
});

test('a check whose result is not a boolean rejects the verdict', () => {
  // "We do not know whether the hospital-access rule held" is not the same
  // claim as "it did not hold", and a critic card is where that matters.
  assert.equal(
    normalizeVerdictFrame({
      type: 'verdict',
      verdict: {
        proposal_id: 'prop-1',
        decision: 'APPROVE',
        checks: [{ rule: 'hospital_access_preserved', passed: 'yes' }],
      },
    }),
    null,
  );
});

test('a compile result reports what was published, or why nothing was', () => {
  const signed = normalizeCompileFrame({
    type: 'compile',
    compile: {
      proposal_id: 'prop-1',
      advisory_id: 'adv-1',
      status: 'SIGNED',
      signature: 'base64...',
      signature_verified: true,
      policy_rule: 'flood.no_entry',
      issuer: 'headsup/compiler',
    },
  });
  assert.equal(signed.status, 'SIGNED');
  assert.equal(signed.advisoryId, 'adv-1');
  assert.equal(signed.signed, true);
  assert.equal(signed.signatureVerified, true);
  const rejected = normalizeCompileFrame({
    type: 'compile',
    compile: {
      proposal_id: 'prop-1',
      status: 'REJECTED',
      reasons: ['area_over_cap'],
    },
  });
  assert.equal(rejected.advisoryId, null);
  assert.deepEqual(rejected.reasons, ['area_over_cap']);
});

test('an unreported signature check is three-valued, never a yes', () => {
  const held = normalizeCompileFrame({
    type: 'compile',
    compile: {
      proposal_id: 'prop-1',
      status: 'PENDING_APPROVAL',
      two_key: true,
      reasons: ['two_key_approval_required'],
    },
  });
  assert.equal(held.status, 'PENDING_APPROVAL');
  assert.equal(held.twoKey, true);
  assert.equal(held.signed, false);
  // Null, not false: no check has been reported, and only the issuer's key
  // could turn that into either of the other two.
  assert.equal(held.signatureVerified, null);
  // A status this build cannot read means nothing was published.
  assert.equal(
    normalizeCompileFrame({
      type: 'compile',
      compile: { proposal_id: 'prop-1', status: 'PROBABLY_FINE' },
    }).status,
    'REJECTED',
  );
});

test('an assist frame is the whole open set, clusters and all', () => {
  const rows = normalizeAssistFrame({
    type: 'assist',
    assists: [
      { id: 'asr-1', op: 'op_ghost', lon: -97.739, lat: 30.2671, ghost: true },
      {
        id: 'asr-2',
        op: 'op_ghost',
        lon: -97.7386,
        lat: 30.2671,
        ghost: true,
        state: 'ACTIVE',
        cluster: 'trg-1',
      },
    ],
  });
  assert.deepEqual(
    rows.map((row) => [row.id, row.ghost, row.state, row.cluster]),
    [
      ['asr-1', true, 'QUEUED', null],
      ['asr-2', true, 'ACTIVE', 'trg-1'],
    ],
  );
  // An empty list is an empty queue, not a rejection: that is how a request
  // a human cleared leaves the screen.
  assert.deepEqual(normalizeAssistFrame({ type: 'assist', assists: [] }), []);
  // One unusable row rejects the frame, the same rule a telemetry tick gets.
  assert.equal(
    normalizeAssistFrame({
      type: 'assist',
      assists: [{ id: 'asr-1', op: 'op_a', lon: 999, lat: 30 }],
    }),
    null,
  );
});

test('the counter frame carries both cohorts or neither', () => {
  assert.deepEqual(
    normalizeCountersFrame({
      type: 'counters',
      counters: {
        hazard_entries: { live: 0, ghost: 7 },
        assist_queue: { live: 0, ghost: 5 },
      },
    }),
    {
      hazardEntries: { live: 0, ghost: 7 },
      assistQueue: { live: 0, ghost: 5 },
    },
  );
  // Half a comparison is a wrong comparison, not a shorter one.
  assert.equal(
    normalizeCountersFrame({
      type: 'counters',
      counters: { hazard_entries: { live: 0, ghost: 7 } },
    }),
    null,
  );
  assert.equal(
    normalizeCountersFrame({
      type: 'counters',
      counters: {
        hazard_entries: { live: -1, ghost: 7 },
        assist_queue: { live: 0, ghost: 5 },
      },
    }),
    null,
  );
});
