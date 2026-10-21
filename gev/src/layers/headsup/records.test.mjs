import assert from 'node:assert/strict';
import test from 'node:test';
import {
  normalizeAdvisoryFeature,
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
    { id: 'veh-1', op: 'op_a', lon: -97.7431, lat: 30.2672, heading: 91.5 },
  ]);
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
