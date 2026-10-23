import assert from 'node:assert/strict';
import test from 'node:test';
import {
  normalizeAckFrame,
  normalizeAdvisoryFeature,
  normalizeHazardFrame,
  normalizeScenarioStateFrame,
  normalizeTelemetryFrame,
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
    },
  );
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
    { id: 'haz-1', hazardClass: 'FLOOD', severity: 4 },
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
