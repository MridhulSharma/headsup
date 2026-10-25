import assert from 'node:assert/strict';
import test from 'node:test';
import {
  activeAdvisory,
  advisoryAreaSqM,
  advisoryLengthM,
  buildAdvisoryCard,
  ringAreaSqM,
  signatureState,
} from './advisory.js';

/** A ring of roughly 1 km by 1 km over downtown Austin. */
const KM_RING = Object.freeze([
  [-97.75, 30.26],
  [-97.73958, 30.26],
  [-97.73958, 30.26904],
  [-97.75, 30.26904],
  [-97.75, 30.26],
]);

const advisory = (overrides = {}) => ({
  id: 'adv-flood-1',
  version: 2,
  geometryType: 'Polygon',
  polygons: [[KM_RING]],
  lines: [],
  anchors: [[-97.745, 30.265]],
  hazardClass: 'FLOOD',
  severity: 4,
  confidence: 0.82,
  action: 'NO_ENTRY',
  validFrom: 1_000,
  validUntil: 61_000,
  evidence: [
    { source: 'nws', kind: 'nws.flood_warning' },
    { source: 'usgs', kind: 'usgs.gauge' },
    { source: 'nws', kind: 'nws.update' },
  ],
  evidenceCount: 3,
  policyRule: 'flood.no_entry',
  issuer: 'headsup/compiler',
  signed: true,
  ...overrides,
});

test('a ring`s area is its planar area in square metres', () => {
  const area = ringAreaSqM(KM_RING);
  // A kilometre square, to within the projection's own error.
  assert.ok(area > 0.97e6 && area < 1.03e6, `${area} is not about 1 km²`);
  // Winding order is not a claim about size.
  assert.equal(
    Math.round(ringAreaSqM([...KM_RING].reverse())),
    Math.round(area),
  );
  // Fewer than three positions bound nothing.
  assert.equal(ringAreaSqM([[-97.75, 30.26]]), 0);
  assert.equal(ringAreaSqM(null), 0);
});

test('an advisory`s area takes its holes out', () => {
  const hole = Object.freeze([
    [-97.747, 30.263],
    [-97.7452, 30.263],
    [-97.7452, 30.2645],
    [-97.747, 30.2645],
    [-97.747, 30.263],
  ]);
  const solid = advisoryAreaSqM(advisory());
  const holed = advisoryAreaSqM(advisory({ polygons: [[KM_RING, hole]] }));
  assert.ok(holed < solid, 'a hole makes the covered area smaller');
  assert.ok(holed > 0, 'a hole does not swallow the whole area');
});

test('a corridor has a length and no area, and an area has no length', () => {
  const corridor = advisory({
    geometryType: 'LineString',
    polygons: [],
    lines: [
      [
        [-97.75, 30.26],
        [-97.73958, 30.26],
      ],
    ],
  });
  // Null rather than zero: a corridor covers no area, and a reported 0 m²
  // would be a measurement of something nobody measured.
  assert.equal(advisoryAreaSqM(corridor), null);
  const length = advisoryLengthM(corridor);
  assert.ok(length > 970 && length < 1030, `${length} is not about 1 km`);
  assert.equal(advisoryLengthM(advisory()), null);
});

test('a signature nobody checked is never read as a yes', () => {
  // The compiler's word about a check that RAN wins.
  assert.equal(
    signatureState(advisory(), { signatureVerified: true }),
    'verified',
  );
  assert.equal(
    signatureState(advisory(), { signatureVerified: false }),
    'failed',
  );
  // Three-valued: null is "nobody said", which is not "verified".
  assert.equal(
    signatureState(advisory(), { signatureVerified: null, signed: true }),
    'unverified',
  );
  // No compiler frame at all, and an advisory that carried a signature.
  assert.equal(signatureState(advisory(), null), 'unverified');
  // Nothing carried one.
  assert.equal(signatureState(advisory({ signed: false }), null), 'absent');
});

test('the active advisory is the published one while it is live', () => {
  const other = advisory({ id: 'adv-signal-9', validFrom: 5_000 });
  const rows = [advisory(), other];
  // The pipeline's own answer wins, so the rail and the card agree.
  assert.equal(
    activeAdvisory(rows, 'adv-flood-1', 10_000).active.id,
    'adv-flood-1',
  );
  // Without one, the newest live window — a reissue stands in front of what
  // it supersedes.
  assert.equal(activeAdvisory(rows, null, 10_000).active.id, 'adv-signal-9');
  // An id that is not live is not resurrected by having been published.
  assert.equal(
    activeAdvisory([advisory({ validUntil: 2_000 })], 'adv-flood-1', 10_000)
      .active,
    null,
  );
});

test('the card reports the advisory`s own fields and its window', () => {
  const card = buildAdvisoryCard({
    snapshot: {
      advisories: [advisory()],
      pipeline: {
        compiles: [
          {
            proposalId: 'prop-1',
            advisoryId: 'adv-flood-1',
            status: 'SIGNED',
            signed: true,
            signatureVerified: true,
          },
        ],
      },
    },
    view: { advisoryId: 'adv-flood-1' },
    nowMs: 31_000,
  });
  assert.equal(card.present, true);
  assert.equal(card.action, 'NO_ENTRY');
  assert.equal(card.hazardClass, 'FLOOD');
  assert.equal(card.severity, 4);
  assert.equal(card.expiresInMs, 30_000);
  assert.equal(card.signature, 'verified');
  // Distinct feeds, in the order the advisory cites them: a feed cited twice
  // is still one feed.
  assert.deepEqual(card.sources, ['nws', 'usgs']);
  assert.equal(card.unnamedSources, 0);
  assert.equal(card.evidenceCount, 3);
  assert.equal(card.liveCount, 1);
  assert.ok(card.areaSqM > 0.97e6 && card.areaSqM < 1.03e6);
});

test('a citation this layer could not attribute is counted, not dropped', () => {
  // `records.js` keeps an evidence row whose source it could not read, so the
  // card has to say that one of the three citations names no feed. Reporting
  // two feeds and nothing else would be a smaller claim than the advisory
  // made.
  const card = buildAdvisoryCard({
    snapshot: {
      advisories: [
        advisory({
          evidence: [
            { source: 'nws', kind: 'nws.flood_warning' },
            { source: null, kind: 'usgs.gauge' },
            { source: 'nws', kind: 'nws.update' },
          ],
          evidenceCount: 3,
        }),
      ],
    },
    nowMs: 31_000,
  });
  assert.deepEqual(card.sources, ['nws']);
  assert.equal(card.unnamedSources, 1);
  assert.equal(card.evidenceCount, 3);
});

test('a compiler answer about another advisory is not read as this one`s', () => {
  const card = buildAdvisoryCard({
    snapshot: {
      advisories: [advisory()],
      pipeline: {
        compiles: [
          {
            proposalId: 'prop-7',
            advisoryId: 'adv-signal-9',
            status: 'SIGNED',
            signed: true,
            signatureVerified: true,
          },
        ],
      },
    },
    nowMs: 31_000,
  });
  // The advisory carried a signature; nothing has verified THIS one.
  assert.equal(card.signature, 'unverified');
});

test('an empty state says which nothing it is', () => {
  const none = buildAdvisoryCard({ snapshot: null, nowMs: 0 });
  assert.equal(none.present, false);
  assert.equal(none.known, 0);
  assert.equal(none.liveCount, 0);
  // An advisory that arrived and expired is a different claim from none
  // ever arriving, so the count survives the empty state.
  const expired = buildAdvisoryCard({
    snapshot: { advisories: [advisory()] },
    nowMs: 90_000,
  });
  assert.equal(expired.present, false);
  assert.equal(expired.known, 1);
});

test('a snapshot this build cannot read is an empty card, not a throw', () => {
  for (const snapshot of [
    { advisories: 'many' },
    { advisories: [null] },
    { advisories: [{ id: 'adv-1' }] },
    {},
  ])
    assert.equal(buildAdvisoryCard({ snapshot, nowMs: 1 }).present, false);
});
