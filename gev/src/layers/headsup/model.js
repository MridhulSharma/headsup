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
 *
 * No red in this palette, on purpose. Red is spoken for twice on this
 * screen — the unadvised ghost cohort and a refusal — and a fleet that
 * happened to hash to it would read as one of those at a glance.
 */
export const OPERATOR_COLORS = Object.freeze([
  '#38f0c0',
  '#ffd166',
  '#4cc9f0',
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

/**
 * The ghost fleet's look: one faint red, smaller and translucent, for every
 * ghost operator.
 *
 * Deliberately NOT the operator palette. A ghost vehicle is the control group
 * — the same fleet driving as if no advisory existed — and the claim the demo
 * makes is about the two COHORTS, not about which company runs which ghost.
 * Giving ghosts their own colour means a glance separates advised from
 * unadvised, which is the only comparison on screen that matters.
 *
 * Red rather than the grey this started as, and faint rather than solid. Red
 * because the ghost fleet is the cohort driving into a hazard nobody told it
 * about, and grey read as "inactive" — the opposite of what is happening.
 * Faint because the ghosts are the BACKGROUND of the comparison: they are
 * numerous, they are what the advised fleet is being judged against, and a
 * screen of saturated red dots would bury the fleets the advisory reached.
 */
export const GHOST_VEHICLE_COLOR = '#ff5a52';
export const GHOST_VEHICLE_ALPHA = 0.42;
export const GHOST_VEHICLE_PIXEL_SIZE = 8;

/**
 * Corridor advisories are lines, drawn heavier than an area's outline, with
 * a wider and fainter halo under them.
 *
 * The halo is what makes a corridor GLOW. Two clamped polylines rather than
 * a `PolylineGlow` material, because both of these carry a colour that the
 * publication pulse animates: Cesium updates the per-instance colour
 * attribute of draped geometry for a `ColorMaterialProperty` without
 * re-tessellating it (`PolygonGeometryUpdater._isDynamic` exempts it), and
 * that exemption is what the whole pulse design rests on. A glow material
 * would move the corridor onto a different batch and put that property at
 * risk for an effect two lines already give.
 */
export const CORRIDOR_WIDTH_PX = 5;
export const CORRIDOR_HALO_WIDTH_PX = 16;
/** How much of the corridor's colour the halo carries at rest. */
export const CORRIDOR_HALO_ALPHA = 0.26;
/**
 * The action whose advisory IS a corridor rather than merely line-shaped.
 *
 * `KEEP_CLEAR` is addressed to a route: keep this lane open. Every other
 * action can arrive with line geometry too, so the glow is keyed on the
 * action and not on the geometry — a `SPEED_CAP` along a road is a limit on
 * a route, not a lane somebody is asking to be kept clear.
 */
export const CORRIDOR_ACTION = 'KEEP_CLEAR';

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
  const css = operatorCssColor(op);
  let color = OPERATOR_CESIUM_COLORS.get(css);
  if (color === undefined) {
    color = Cesium.Color.fromCssColorString(css);
    OPERATOR_CESIUM_COLORS.set(css, color);
  }
  return color;
}

/**
 * Parsed colours and appearances, kept rather than rebuilt.
 *
 * `Color.fromCssColorString` parses a string and allocates, and a thousand
 * vehicles re-asked for it on every telemetry tick: in a frame profile of the
 * load case that parse was one of the layer's largest costs, for an answer
 * that never changes.
 *
 * Both caches hand back a SHARED instance. Every Cesium setter clones the
 * colour it is given, and `withAlpha` and `Color.clone` both return a new
 * one, so sharing is safe as long as nothing mutates what it is handed —
 * which is why the appearance object is frozen.
 */
const OPERATOR_CESIUM_COLORS = new Map();
const VEHICLE_APPEARANCES = new Map();
/**
 * Appearances remembered before the cache is dropped and refilled. Operator
 * identifiers arrive on the feed, and feed text is data: the cap means a feed
 * naming thousands of operators cannot keep the memory it costs.
 */
const APPEARANCE_LIMIT = 256;

/**
 * How one vehicle dot is drawn: its colour and its size.
 *
 * Pure, and the single place the ghost/advised distinction is made, so the
 * layer never has to repeat the rule at each of the three points where it
 * creates, recolours or re-reports a dot.
 * @param {{op: string, ghost?: boolean}} vehicle
 * @returns {{color: Cesium.Color, pixelSize: number, css: string}}
 */
export function vehicleAppearance({ op, ghost = false } = {}) {
  const key = ghost ? 'ghost' : `op:${op}`;
  const known = VEHICLE_APPEARANCES.get(key);
  if (known !== undefined) return known;
  const appearance = Object.freeze(
    ghost
      ? {
          color:
            Cesium.Color.fromCssColorString(GHOST_VEHICLE_COLOR).withAlpha(
              GHOST_VEHICLE_ALPHA,
            ),
          pixelSize: GHOST_VEHICLE_PIXEL_SIZE,
          css: GHOST_VEHICLE_COLOR,
        }
      : {
          color: operatorColor(op),
          pixelSize: VEHICLE_PIXEL_SIZE,
          css: operatorCssColor(op),
        },
  );
  if (VEHICLE_APPEARANCES.size >= APPEARANCE_LIMIT) VEHICLE_APPEARANCES.clear();
  VEHICLE_APPEARANCES.set(key, appearance);
  return appearance;
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
export function trackProgress(track, nowMs) {
  const duration = Math.max(1, Number(track.durationMs) || 1);
  const elapsed = Number(nowMs) - Number(track.startedAt);
  return Math.min(1, Math.max(0, elapsed / duration));
}

/**
 * Where one track is at a moment, in degrees.
 *
 * Pass `result` to be written into rather than allocated: the layer samples
 * every vehicle it orients on every frame, and a thousand throwaway objects a
 * frame is a visible slice of the collector's time.
 * @param {object} track Track from the layer's vehicle marker.
 * @param {number} nowMs Wall clock in milliseconds.
 * @param {{lon: number, lat: number, height: number, heading: number}} [result]
 * @returns {{lon: number, lat: number, height: number, heading: number}}
 */
export function sampleTrack(track, nowMs, result) {
  const out = result ?? { lon: 0, lat: 0, height: 0, heading: 0 };
  const t = trackProgress(track, nowMs);
  // The endpoints are returned exactly, as the director's camera sampler also
  // does: a dot at rest must sit ON the position the feed reported, not a
  // float-error nudge away from it.
  if (t === 0) {
    out.lon = track.fromLon;
    out.lat = track.fromLat;
    out.height = track.fromHeight;
    out.heading = track.fromHeading;
    return out;
  }
  if (t === 1) {
    out.lon = track.toLon;
    out.lat = track.toLat;
    out.height = track.toHeight;
    out.heading = track.toHeading;
    return out;
  }
  // Shortest signed arc, so a vehicle turning past north sweeps the short way
  // (the director's camera sampler reads the same way).
  const turn =
    ((((track.toHeading - track.fromHeading + 540) % 360) + 360) % 360) - 180;
  out.lon = track.fromLon + (track.toLon - track.fromLon) * t;
  out.lat = track.fromLat + (track.toLat - track.fromLat) * t;
  out.height = track.fromHeight + (track.toHeight - track.fromHeight) * t;
  out.heading = (((track.fromHeading + turn * t) % 360) + 360) % 360;
  return out;
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

/**
 * The chevron that carries a vehicle's heading.
 *
 * An SVG data URI rather than a canvas: it is a plain string, so this module
 * stays pure and portable, every vehicle shares ONE texture-atlas entry, and
 * the colour comes from the billboard's own `color` rather than from a
 * per-operator image. White on purpose — the tint is what makes it the
 * operator's colour.
 */
const CHEVRON_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" ' +
  'viewBox="0 0 24 24"><path d="M12 1.5 21.5 22 12 17.2 2.5 22Z" ' +
  'fill="#ffffff"/></svg>';
export const VEHICLE_CHEVRON_IMAGE = `data:image/svg+xml;utf8,${encodeURIComponent(
  CHEVRON_SVG,
)}`;
/** Chevron size in pixels, and the band over which it fades out. */
export const VEHICLE_CHEVRON_PX = 15;
export const CHEVRON_NEAR_M = 400;
export const CHEVRON_FAR_M = 2_600;

/**
 * Event animation windows, in milliseconds.
 *
 * Each one is a SINGLE gesture with a beginning and an end: one swell of the
 * hazard area when an advisory is published, one beam out to each fleet, one
 * flash when that fleet answers. Nothing here loops, because a loop is a
 * thing that keeps asking for attention it has already been given.
 */
export const HAZARD_PULSE_MS = 2_000;
export const DISPATCH_BEAM_MS = 1_500;
export const ACK_FLASH_MS = 700;
export const DEFLECTION_TRAIL_MS = 30_000;

/** How much alpha the pulse adds over the advisory's steady fill, at peak. */
export const PULSE_PEAK_ALPHA = 0.34;
/** How much wider a flashing vehicle dot gets, as a fraction of its size. */
export const FLASH_PEAK_SCALE = 0.7;

/** Dispatch beams and deflection trails. */
export const BEAM_WIDTH_PX = 4;
export const BEAM_GLOW_POWER = 0.25;
export const TRAIL_WIDTH_PX = 3;
export const TRAIL_ALPHA = 0.7;
/**
 * Taper of the deflection trail's glow, which is what makes it FADE.
 *
 * The fade is in the shader, not in a per-frame alpha write: a trail tapers
 * from its newest end to nothing at its oldest, so two hundred of them cost
 * two hundred vertex buffers that only change when a tick lands, and nothing
 * at all per frame.
 */
export const TRAIL_TAPER_POWER = 0.42;

/** The one red on this screen: a refusal, and nothing else. */
export const DECLINED_ACCENT = '#ef476f';

/**
 * One swell and back: 0 at both ends of the window, 1 in the middle.
 *
 * Used by the hazard pulse and the acknowledgement flash, so both read as the
 * same gesture at two sizes. Outside the window it is 0, which is what lets a
 * caller treat "finished" and "never started" identically.
 * @param {number} elapsedMs Milliseconds since the event.
 * @param {number} [durationMs] Window length.
 * @returns {number} Level in [0, 1].
 */
export function pulseLevel(elapsedMs, durationMs = HAZARD_PULSE_MS) {
  const duration = Math.max(1, Number(durationMs) || 1);
  const elapsed = Number(elapsedMs);
  if (!Number.isFinite(elapsed) || elapsed <= 0 || elapsed >= duration)
    return 0;
  return Math.sin(Math.PI * (elapsed / duration));
}

/**
 * How far one dispatch beam has reached, and how visible it still is.
 *
 * The beam GROWS over the first half of its window and then fades where it
 * arrived, rather than growing all the way and vanishing: the gesture being
 * drawn is "this went to that fleet", and the end of it is the part that
 * should still be on screen when it goes.
 * @param {number} elapsedMs Milliseconds since the advisory was published.
 * @param {number} [durationMs] Window length.
 * @returns {{reach: number, alpha: number, done: boolean}}
 */
export function beamProgress(elapsedMs, durationMs = DISPATCH_BEAM_MS) {
  const duration = Math.max(1, Number(durationMs) || 1);
  const elapsed = Number(elapsedMs);
  if (!Number.isFinite(elapsed) || elapsed < 0)
    return { reach: 0, alpha: 0, done: false };
  if (elapsed >= duration) return { reach: 1, alpha: 0, done: true };
  const t = elapsed / duration;
  // Ease-out cubic over the first 55%, so the beam leaves fast and lands soft.
  const travel = Math.min(1, t / 0.55);
  return {
    reach: 1 - (1 - travel) ** 3,
    alpha: t <= 0.6 ? 1 : 1 - (t - 0.6) / 0.4,
    done: false,
  };
}

/**
 * The unit direction of travel at one position, in world coordinates.
 *
 * This is what orients a vehicle: a billboard's `alignedAxis` points its top
 * along this vector in screen space, so the chevron reads as the bearing the
 * simulator reported from any camera angle — including straight down, where a
 * screen-space rotation would be meaningless.
 *
 * The east and north unit vectors are written out rather than taken from
 * `Transforms.eastNorthUpToFixedFrame`: this runs once per vehicle per frame,
 * and a thousand 4x4 matrices a frame is a cost with nothing to show for it.
 * @param {number} lon Degrees.
 * @param {number} lat Degrees.
 * @param {number} headingDeg Bearing clockwise from north.
 * @param {{x: number, y: number, z: number}} result Written in place.
 * @returns {{x: number, y: number, z: number}} `result`.
 */
export function headingDirection(lon, lat, headingDeg, result) {
  const lonRad = (Number(lon) || 0) * (Math.PI / 180);
  const latRad = (Number(lat) || 0) * (Math.PI / 180);
  const heading = (Number(headingDeg) || 0) * (Math.PI / 180);
  const sinLon = Math.sin(lonRad);
  const cosLon = Math.cos(lonRad);
  const sinLat = Math.sin(latRad);
  const cosLat = Math.cos(latRad);
  const east = Math.sin(heading);
  const north = Math.cos(heading);
  result.x = -sinLon * east - sinLat * cosLon * north;
  result.y = cosLon * east - sinLat * sinLon * north;
  result.z = cosLat * north;
  return result;
}

/**
 * The mean position of one fleet, as the dispatch beam's destination.
 *
 * A centroid rather than a nearest vehicle: the beam is addressed to the
 * OPERATOR, and pointing it at one vehicle would claim the advisory was sent
 * to that vehicle. Returns null for an empty fleet, because there is then no
 * fleet to draw a beam to.
 * @param {Iterable<{lon: number, lat: number, height: number}>} samples
 * @returns {{lon: number, lat: number, height: number}|null}
 */
export function fleetCentroid(samples) {
  let lon = 0;
  let lat = 0;
  let height = 0;
  let count = 0;
  for (const sample of samples) {
    if (!Number.isFinite(sample?.lon) || !Number.isFinite(sample?.lat))
      continue;
    lon += sample.lon;
    lat += sample.lat;
    height += Number.isFinite(sample.height) ? sample.height : 0;
    count++;
  }
  if (!count) return null;
  return { lon: lon / count, lat: lat / count, height: height / count };
}

/**
 * Drop trail samples that have left the window.
 *
 * In place and from the front, because the array is in arrival order and the
 * caller is about to write it to a polyline: a copy per tick per vehicle is
 * two hundred arrays a tick for nothing.
 * @param {Array<{at: number}>} samples Oldest first.
 * @param {number} nowMs Epoch milliseconds.
 * @param {number} [windowMs] How long a trail is kept.
 * @returns {Array<{at: number}>} `samples`.
 */
export function pruneTrail(samples, nowMs, windowMs = DEFLECTION_TRAIL_MS) {
  const cutoff = Number(nowMs) - Math.max(0, Number(windowMs) || 0);
  let drop = 0;
  while (drop < samples.length && samples[drop].at < cutoff) drop++;
  if (drop) samples.splice(0, drop);
  return samples;
}

/**
 * Build the source-owned presentation for one DECLINED acknowledgement.
 *
 * A refusal is the one thing on this screen that is not an advisory, so it is
 * labelled on the FLEET that refused rather than on the hazard: the question
 * it answers is "who did not take it", and that is an operator, not an area.
 * @param {object} input
 * @param {string} input.operator Operator identifier.
 * @param {object} input.position Label anchor (`Cesium.Cartesian3`).
 * @param {string} input.title Label text, already translated by the surface.
 * @returns {object} World-overlay entry.
 */
export function createDeclinedOverlayEntry({ operator, position, title }) {
  return {
    id: `declined:${operator}`,
    position,
    variant: 'label',
    title,
    accent: DECLINED_ACCENT,
    // Above every advisory label: a fleet that refused is the most urgent
    // thing the overlay can be asked to keep when it has to drop some.
    priority: 9_000,
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

/**
 * The hazard classes on screen right now, as a legend reads them.
 *
 * Derived from what is DRAWN rather than from the palette, so the legend
 * never names a colour the viewer cannot see, and never omits one it can.
 * @param {Array<{hazardClass: string}>} advisories Live advisory rows.
 * @returns {Array<{hazardClass: string, color: string, advisories: number}>}
 */
export function hazardLegendRows(advisories) {
  const rows = new Map();
  for (const row of advisories ?? []) {
    const entry = rows.get(row.hazardClass);
    if (entry) entry.advisories++;
    else
      rows.set(row.hazardClass, {
        hazardClass: row.hazardClass,
        color: hazardCssColor(row.hazardClass),
        advisories: 1,
      });
  }
  return [...rows.values()].sort((a, b) =>
    a.hazardClass.localeCompare(b.hazardClass),
  );
}

/**
 * The remote-assist mark: an exclamation inside a ring.
 *
 * One shared texture-atlas entry for every marker, tinted per cohort by the
 * billboard's own `color`, the same arrangement the heading chevron uses.
 * White in the image, because the tint is what carries the meaning.
 */
const ASSIST_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="28" height="28" ' +
  'viewBox="0 0 28 28"><circle cx="14" cy="14" r="11" fill="none" ' +
  'stroke="#ffffff" stroke-width="2.6"/><path d="M14 8v8" stroke="#ffffff" ' +
  'stroke-width="2.6" stroke-linecap="round"/>' +
  '<circle cx="14" cy="20.2" r="1.5" fill="#ffffff"/></svg>';
export const ASSIST_MARKER_IMAGE = `data:image/svg+xml;utf8,${encodeURIComponent(
  ASSIST_SVG,
)}`;
/** Marker size, and the size one collapsed cluster is drawn at instead. */
export const ASSIST_MARKER_PX = 20;
export const ASSIST_CLUSTER_PX = 26;
/**
 * A request waiting on a human is amber; one from the unadvised cohort keeps
 * the ghost fleet's red.
 *
 * Which cohort is queueing for a human IS the comparison S2 and S3 make, so
 * the marker says it without anybody having to click: a screen of red rings
 * over the ghost lane beside an empty advised lane is the whole claim.
 */
export const ASSIST_LIVE_COLOR = '#ffd166';
export const ASSIST_GHOST_COLOR = GHOST_VEHICLE_COLOR;
export const ASSIST_MARKER_ALPHA = 0.95;
/** How high above the ground an assist marker floats, in metres. */
export const ASSIST_HEIGHT_OFFSET_M = 18;

/**
 * Collapse assist requests into the markers that get drawn.
 *
 * Surge triage is what decides a group of requests is one event, and it says
 * so by giving them a shared `cluster` id. This function only ever follows
 * that: requests WITHOUT a cluster stay one marker each, however close
 * together they are. A viewer that clustered by distance on its own would be
 * asserting a triage decision nobody made, and the number on the badge would
 * stop meaning "this many requests were grouped".
 *
 * A cluster sits at the mean of its members, carries their count, and is a
 * ghost cluster only when every member is — a group containing one advised
 * vehicle is not a statement about the control cohort.
 * @param {Array<object>} rows Normalized assist rows.
 * @returns {Array<{key: string, lon: number, lat: number, count: number,
 *   ghost: boolean, cluster: string|null, ops: string[]}>}
 */
export function groupAssists(rows) {
  const groups = new Map();
  for (const row of rows ?? []) {
    const key = row.cluster ? `cluster:${row.cluster}` : `assist:${row.id}`;
    const group = groups.get(key);
    if (group) {
      group.lon += row.lon;
      group.lat += row.lat;
      group.count++;
      group.ghost = group.ghost && row.ghost === true;
      if (!group.ops.includes(row.op)) group.ops.push(row.op);
    } else
      groups.set(key, {
        key,
        lon: row.lon,
        lat: row.lat,
        count: 1,
        ghost: row.ghost === true,
        cluster: row.cluster ?? null,
        ops: [row.op],
      });
  }
  const markers = [];
  for (const group of groups.values())
    markers.push({
      ...group,
      lon: group.lon / group.count,
      lat: group.lat / group.count,
      ops: group.ops.sort(),
    });
  // Stable order, so a marker keeps its billboard across ticks.
  return markers.sort((a, b) => a.key.localeCompare(b.key));
}

/**
 * The count badge over a collapsed cluster of assist requests.
 *
 * Only ever built for a cluster — a single request needs no number, and
 * writing "x1" over one marker would be noise with the word count in it.
 * The text arrives already translated, because this module is a portable
 * package and the console owns `t()`.
 * @param {object} input
 * @param {string} input.key Marker key from `groupAssists`.
 * @param {object} input.position Label anchor (`Cesium.Cartesian3`).
 * @param {string} input.title Badge text, already translated.
 * @param {boolean} input.ghost Whether every member is a ghost.
 * @returns {object} World-overlay entry.
 */
export function createAssistOverlayEntry({ key, position, title, ghost }) {
  return {
    id: `assist:${key}`,
    position,
    variant: 'label',
    title,
    accent: ghost ? ASSIST_GHOST_COLOR : ASSIST_LIVE_COLOR,
    // Above an advisory label and below a refusal tag: a queue of vehicles
    // waiting on a human outranks a hazard's name, and a fleet that refused
    // outranks both.
    priority: 7_000,
    collisionGroup: 'ambient-label',
    paintLane: 'ambient-label',
    interactive: false,
    edgeFade: 'keyhole',
    horizonCull: true,
    terrainOcclusion: false,
    gapPx: 12,
    verticalOnly: true,
    placement: 'above',
  };
}

export {
  normalizeAdvisoryFeature,
  normalizeAssistFrame,
  normalizeCountersFrame,
  normalizeTelemetryFrame,
} from './records.js';
export {
  ADVISORY_PHASES,
  advisoryIsLive,
  advisoryPhase,
  msUntilPhaseChange,
  partitionAdvisories,
} from './expiry.js';
