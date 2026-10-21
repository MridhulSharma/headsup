import * as Cesium from 'cesium';

/**
 * Presentation constants and pure helpers for the Heads Up layer: the
 * operator and hazard palettes, the sample interpolator behind smooth vehicle
 * motion, and the overlay-entry and analyst-record builders.
 * @module layers/headsup/model
 */

export const HEADSUP_OVERLAY_SOURCE_ID = 'headsup';
export const HEADSUP_OVERLAY_COHORT_LIMIT = 48;
export const HEADSUP_OVERLAY_COLLISION_CAPACITY = 24;
/** Owner id this layer holds the render governor under. */
export const HEADSUP_RENDER_HOLD_ID = 'headsup';

/**
 * Operator colors, assigned by hash rather than by a registry.
 *
 * Operator ids are free identifiers in the fleet contract — a demo can add a
 * fourth fleet without a code change — so a fixed `{op: color}` map would
 * either go stale or make the layer reject fleets it could draw. Hashing
 * keeps one operator's color STABLE across runs and processes, which is what
 * an observer actually relies on, at the price of a possible collision when
 * more than eight operators are on screen.
 */
export const OPERATOR_COLORS = Object.freeze([
  '#38f0c0',
  '#ffd166',
  '#ef476f',
  '#8ecae6',
  '#b5e48c',
  '#c792ea',
  '#ff9f1c',
  '#e2e8f0',
]);

/** Fill and outline color per hazard class. */
export const HAZARD_CLASS_COLORS = Object.freeze({
  FLOOD: '#2bb3ff',
  WORK_ZONE: '#ff8c1a',
  SIGNAL_OUTAGE: '#ffd400',
  EMERGENCY_SCENE: '#ff3b30',
  SCHOOL_ZONE: '#34c759',
  SEVERE_WEATHER: '#b36bff',
});
/** A class this build does not know is still drawn, in neutral grey. */
export const UNKNOWN_HAZARD_COLOR = '#9aa5b1';

/** Vehicle dot size and distance falloff, matching the other contact layers. */
export const VEHICLE_PIXEL_SIZE = 11;
export const VEHICLE_HEIGHT_OFFSET_M = 2.0;

/** Interpolation window bounds; the feed's own cadence picks the value between. */
export const MIN_TRACK_DURATION_MS = 100;
export const MAX_TRACK_DURATION_MS = 2_000;

/** FNV-1a over the operator id: stable across runs, processes and machines. */
export function operatorColorIndex(op, palette = OPERATOR_COLORS) {
  const text = String(op ?? '');
  let hash = 0x811c9dc5;
  for (let index = 0; index < text.length; index++) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash % palette.length;
}

/**
 * The CSS color one operator's vehicles draw in.
 * @param {string} op Operator identifier.
 * @returns {string} CSS color.
 */
export function operatorCssColor(op) {
  return OPERATOR_COLORS[operatorColorIndex(op)];
}

/**
 * The Cesium color one operator's vehicles draw in.
 * @param {string} op Operator identifier.
 * @returns {Cesium.Color}
 */
export function operatorColor(op) {
  return Cesium.Color.fromCssColorString(operatorCssColor(op));
}

/**
 * The CSS color one hazard class draws in.
 * @param {string} hazardClass Hazard class from the advisory contract.
 * @returns {string} CSS color.
 */
export function hazardCssColor(hazardClass) {
  return HAZARD_CLASS_COLORS[hazardClass] ?? UNKNOWN_HAZARD_COLOR;
}

/**
 * The fill opacity for one advisory's area.
 *
 * Severity 1-5 maps to 0.14-0.38: translucent enough that the street grid
 * underneath stays readable at every severity, because an operator judges the
 * advisory by what it covers.
 * @param {number} severity Advisory severity, 1-5.
 * @returns {number} Alpha in [0.14, 0.38].
 */
export function hazardFillAlpha(severity) {
  const level = Math.min(5, Math.max(1, Math.round(Number(severity) || 1)));
  return 0.14 + (level - 1) * 0.06;
}

/**
 * Sample a vehicle track between its previous and newest position.
 *
 * This INTERPOLATES, it does not extrapolate: the drawn dot trails the feed
 * by up to one sample interval and never runs ahead of a position the
 * simulator actually reported. A dead-reckoned dot would look smoother and be
 * wrong exactly when it matters — at a stop, where the real vehicle is still
 * and the guess keeps rolling it into an advisory polygon.
 *
 * @param {{fromLon: number, fromLat: number, fromHeight: number,
 *   fromHeading: number, toLon: number, toLat: number, toHeight: number,
 *   toHeading: number, startedAt: number, durationMs: number}} track
 * @param {number} nowMs Epoch milliseconds.
 * @returns {{lon: number, lat: number, height: number, heading: number}}
 */
export function sampleTrack(track, nowMs) {
  const duration = Math.max(1, Number(track.durationMs) || 1);
  const elapsed = Number(nowMs) - Number(track.startedAt);
  const t = Math.min(1, Math.max(0, elapsed / duration));
  // The endpoints are returned exactly, as the director's camera sampler also
  // does: a dot at rest must sit ON the position the feed reported, not a
  // float-error nudge away from it.
  if (t === 0)
    return {
      lon: track.fromLon,
      lat: track.fromLat,
      height: track.fromHeight,
      heading: track.fromHeading,
    };
  if (t === 1)
    return {
      lon: track.toLon,
      lat: track.toLat,
      height: track.toHeight,
      heading: track.toHeading,
    };
  // Shortest signed arc, so a vehicle turning past north sweeps the short way
  // (the director's camera sampler reads the same way).
  const turn =
    ((((track.toHeading - track.fromHeading + 540) % 360) + 360) % 360) - 180;
  return {
    lon: track.fromLon + (track.toLon - track.fromLon) * t,
    lat: track.fromLat + (track.toLat - track.fromLat) * t,
    height: track.fromHeight + (track.toHeight - track.fromHeight) * t,
    heading: (((track.fromHeading + turn * t) % 360) + 360) % 360,
  };
}

/**
 * Clamp a measured inter-sample interval to the interpolation window.
 *
 * A gap longer than the ceiling means the feed stalled; interpolating across
 * it would glide the dot through a minute of unknown ground, so motion
 * finishes inside the ceiling and the dot then waits where it was last seen.
 * @param {number} measuredMs Milliseconds since the previous sample.
 * @returns {number} Interpolation duration in milliseconds.
 */
export function trackDurationMs(measuredMs) {
  const measured = Number(measuredMs);
  if (!Number.isFinite(measured)) return MIN_TRACK_DURATION_MS;
  return Math.min(
    MAX_TRACK_DURATION_MS,
    Math.max(MIN_TRACK_DURATION_MS, measured),
  );
}

/**
 * Build the source-owned presentation for one advisory's world label.
 *
 * Hazard class and action are the two things an operator needs before
 * deciding anything, so they are the title; the severity ranks the label
 * against its neighbours when the overlay has to drop some.
 * @param {object} input
 * @param {string} input.id Advisory id.
 * @param {Cesium.Cartesian3} input.position Label anchor.
 * @param {string} input.hazardClass Hazard class.
 * @param {string} input.action Advisory action type.
 * @param {number} input.severity Advisory severity, 1-5.
 * @param {string} input.accent Source-owned hazard-class color.
 * @returns {object} World-overlay entry.
 */
export function createAdvisoryOverlayEntry({
  id,
  position,
  hazardClass,
  action,
  severity,
  accent,
}) {
  return {
    id: String(id),
    position,
    variant: 'label',
    title: `${hazardClass} · ${action}`,
    accent,
    priority: Math.round((Number(severity) || 0) * 1000),
    collisionGroup: 'ambient-label',
    paintLane: 'ambient-label',
    interactive: false,
    edgeFade: 'keyhole',
    horizonCull: true,
    terrainOcclusion: false,
    gapPx: 15,
    verticalOnly: true,
    placement: 'above',
  };
}

/** Keep the most severe advisories, with stable identity as the tie-break. */
export function selectAdvisoryOverlayCohort(
  entries,
  limit = HEADSUP_OVERLAY_COHORT_LIMIT,
) {
  const cap = Math.max(
    0,
    Math.min(HEADSUP_OVERLAY_COHORT_LIMIT, Math.floor(Number(limit) || 0)),
  );
  if (!Array.isArray(entries) || cap === 0) return [];
  return entries
    .slice()
    .sort(
      (a, b) =>
        b.priority - a.priority || String(a.id).localeCompare(String(b.id)),
    )
    .slice(0, cap);
}

/**
 * Map one drawn vehicle to a JSON-safe analyst record (query-engine seam).
 * Pure — no Cesium types, and missing fields are null rather than undefined.
 * @param {object|null|undefined} raw Plain values read off the marker.
 * @param {number} [index=0] Position in the snapshot (fallback id only).
 * @returns {{id: string, operator: string|null, lat: number|null,
 *   lon: number|null, headingDeg: number|null, kind: string}}
 */
export function mapAnalystRecord(raw, index = 0) {
  const num = (value) => (Number.isFinite(value) ? value : null);
  const text = (value) => {
    const trimmed = String(value ?? '').trim();
    return trimmed || null;
  };
  return {
    id: text(raw?.id) || `VEHICLE-${String(index).padStart(4, '0')}`,
    operator: text(raw?.op),
    lat: num(raw?.lat),
    lon: num(raw?.lon),
    headingDeg: num(raw?.heading),
    kind: 'vehicle',
  };
}

export {
  normalizeAdvisoryFeature,
  normalizeTelemetryFrame,
} from './records.js';
export {
  ADVISORY_PHASES,
  advisoryIsLive,
  advisoryPhase,
  msUntilPhaseChange,
  partitionAdvisories,
} from './expiry.js';
