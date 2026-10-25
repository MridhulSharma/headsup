/**
 * The active advisory, as one card's worth of fields.
 *
 * Pure: a feed snapshot, the pipeline view and a clock in, a frozen record
 * out. No DOM, no Cesium, no clock of its own — `./console.js` renders what
 * this returns, and `./pipeline.js` is the sibling that does the same job for
 * the rail.
 *
 * Three rules, and they are the three the rail already keeps:
 *
 * - **Every field comes off the advisory itself.** The hazard class, the
 *   action, the severity, the window and the evidence rows are the ones the
 *   advisory arrived carrying (`layers/headsup/records.js`), not ones read off
 *   a neighbouring fusion or proposal frame. An advisory is the signed
 *   artefact; describing it from its neighbours is the inference an audit
 *   trail exists to replace.
 * - **The signature is three-valued, and silence is never a yes.** Only the
 *   compiler reports whether a check RAN. "Present, unverified" is the honest
 *   reading when nothing has said, and this console does not hold the issuer's
 *   key.
 * - **Live is a question about a clock.** Which advisory is active is decided
 *   by `layers/headsup/expiry.js` against the clock passed in, so the card and
 *   the globe answer it the same way.
 *
 * Extent is reported as plain SI numbers — square metres for an area, metres
 * for a corridor — because choosing a unit is presentation, and the console
 * owns that together with `t()`.
 * @module headsup/advisory
 */

import { advisoryIsLive } from '../layers/headsup/expiry.js';
import { latestCompiles } from './pipeline.js';

/** How a signature reads. `unverified` is "present, nobody checked". */
export const SIGNATURE_STATES = Object.freeze({
  verified: 'verified',
  failed: 'failed',
  unverified: 'unverified',
  absent: 'absent',
});

/** Metres per degree of latitude, and of longitude at the equator. */
const METRES_PER_DEGREE_LAT = 110_574;
const METRES_PER_DEGREE_LON = 111_320;

const rows = (value) => (Array.isArray(value) ? value : []);

/**
 * Local metres per degree of longitude at one latitude.
 *
 * An advisory covers a few city blocks, so a ring is projected onto a plane
 * tangent at its own mean latitude rather than integrated on the ellipsoid:
 * over a kilometre that is a fraction of a percent, and the figure on the card
 * is rounded far coarser than that. It is written down here so nobody reads
 * the number as a geodesic area.
 * @param {number} latDeg
 * @returns {number}
 */
function metresPerDegreeLon(latDeg) {
  return METRES_PER_DEGREE_LON * Math.cos((latDeg * Math.PI) / 180);
}

/**
 * The planar area of one ring, in square metres.
 * @param {Array<Array<number>>} ring `[lon, lat]` pairs.
 * @returns {number}
 */
export function ringAreaSqM(ring) {
  const positions = rows(ring);
  if (positions.length < 3) return 0;
  let latSum = 0;
  for (const pair of positions) latSum += pair[1];
  const scaleLon = metresPerDegreeLon(latSum / positions.length);
  let shoelace = 0;
  for (let index = 0; index < positions.length; index++) {
    const from = positions[index];
    const to = positions[(index + 1) % positions.length];
    shoelace +=
      from[0] * scaleLon * (to[1] * METRES_PER_DEGREE_LAT) -
      to[0] * scaleLon * (from[1] * METRES_PER_DEGREE_LAT);
  }
  return Math.abs(shoelace) / 2;
}

/**
 * The area one advisory covers, in square metres, with holes taken out.
 *
 * Null — not zero — for an advisory with no polygon: a corridor covers no
 * area, and a reported zero would be a measurement of something that was
 * never measured.
 * @param {object} advisory One advisory row.
 * @returns {number|null}
 */
export function advisoryAreaSqM(advisory) {
  const polygons = rows(advisory?.polygons);
  if (!polygons.length) return null;
  let total = 0;
  for (const rings of polygons) {
    const parts = rows(rings);
    total += ringAreaSqM(parts[0]);
    for (const hole of parts.slice(1)) total -= ringAreaSqM(hole);
  }
  return Math.max(0, total);
}

/**
 * The length of one advisory's corridors in metres, or null when it has none.
 * @param {object} advisory One advisory row.
 * @returns {number|null}
 */
export function advisoryLengthM(advisory) {
  const lines = rows(advisory?.lines);
  if (!lines.length) return null;
  let total = 0;
  for (const line of lines) {
    const positions = rows(line);
    for (let index = 1; index < positions.length; index++) {
      const from = positions[index - 1];
      const to = positions[index];
      const scaleLon = metresPerDegreeLon((from[1] + to[1]) / 2);
      total += Math.hypot(
        (to[0] - from[0]) * scaleLon,
        (to[1] - from[1]) * METRES_PER_DEGREE_LAT,
      );
    }
  }
  return total;
}

/**
 * How the signature of one advisory reads.
 *
 * `signatureVerified` is the compiler's word about a check that RAN, so it
 * wins whenever there is one for this advisory. Without it, the most this
 * page can say is whether the advisory arrived carrying a signature at all.
 * @param {object} advisory One advisory row.
 * @param {object|null} compile The compiler record naming it, if any.
 * @returns {'verified'|'failed'|'unverified'|'absent'}
 */
export function signatureState(advisory, compile) {
  if (compile?.signatureVerified === true) return SIGNATURE_STATES.verified;
  if (compile?.signatureVerified === false) return SIGNATURE_STATES.failed;
  if (advisory?.signed || compile?.signed) return SIGNATURE_STATES.unverified;
  return SIGNATURE_STATES.absent;
}

/**
 * Which advisory the card is about.
 *
 * The one the pipeline says was published, while it is still live — the rail
 * and the card must not disagree about which advisory this run produced.
 * Otherwise the newest live one, by the start of its own validity window and
 * then by version, so a reissued advisory stands in front of the one it
 * supersedes.
 * @param {Array<object>} advisories
 * @param {string|null} publishedId The pipeline view's `advisoryId`.
 * @param {number} nowMs
 * @returns {{active: object|null, live: Array<object>}}
 */
export function activeAdvisory(advisories, publishedId, nowMs) {
  const live = rows(advisories)
    .filter((row) => advisoryIsLive(row, nowMs))
    .sort(
      (a, b) =>
        (b.validFrom ?? 0) - (a.validFrom ?? 0) ||
        (b.version ?? 0) - (a.version ?? 0),
    );
  const published = publishedId
    ? live.find((row) => row.id === publishedId)
    : null;
  return { active: published ?? live[0] ?? null, live };
}

/**
 * The advisory card's fields.
 * @param {object} [options]
 * @param {object|null} [options.snapshot] The layer's feed snapshot.
 * @param {object|null} [options.view] The pipeline view (`./pipeline.js`).
 * @param {number} [options.nowMs] The clock the window is read against.
 * @returns {Readonly<object>} Frozen; `present: false` is the empty state.
 */
export function buildAdvisoryCard({
  snapshot = null,
  view = null,
  nowMs = 0,
} = {}) {
  const { active, live } = activeAdvisory(
    snapshot?.advisories,
    view?.advisoryId ?? null,
    nowMs,
  );
  const known = rows(snapshot?.advisories).length;
  if (!active)
    return Object.freeze({
      present: false,
      liveCount: 0,
      // What the empty state is ABOUT: an advisory may have arrived and
      // expired, which is a different claim from none ever arriving.
      known,
    });
  const compile =
    [...latestCompiles(snapshot?.pipeline?.compiles).values()]
      .filter((record) => record.advisoryId === active.id)
      .at(-1) ?? null;
  // Distinct feeds, in the order the advisory lists them: which feeds it
  // rests on is the question, and a feed cited twice is still one feed.
  //
  // A citation whose source `records.js` could not read is counted rather
  // than dropped: "two feeds" and "two feeds plus one we cannot name" are
  // different claims about the same advisory, and the card says which it is.
  const sources = [];
  let unnamedSources = 0;
  for (const row of rows(active.evidence)) {
    if (row.source == null) unnamedSources += 1;
    else if (!sources.includes(row.source)) sources.push(row.source);
  }
  return Object.freeze({
    present: true,
    id: active.id,
    version: active.version,
    action: active.action,
    hazardClass: active.hazardClass,
    severity: active.severity,
    confidence: active.confidence,
    geometryType: active.geometryType,
    areaSqM: advisoryAreaSqM(active),
    lengthM: advisoryLengthM(active),
    validFrom: active.validFrom,
    validUntil: active.validUntil,
    /** Negative would mean expired, and an expired one is never active. */
    expiresInMs: Math.max(0, active.validUntil - nowMs),
    policyRule: active.policyRule,
    issuer: active.issuer,
    sources: Object.freeze(sources),
    /** Citations this layer could not attribute to any feed. */
    unnamedSources,
    evidenceCount: active.evidenceCount ?? rows(active.evidence).length,
    signature: signatureState(active, compile),
    /** How many are in force, so a card showing one does not imply one. */
    liveCount: live.length,
    known,
  });
}
