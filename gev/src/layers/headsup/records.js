/**
 * Normalize the Heads Up viewer socket's two frame kinds into plain rows.
 *
 * Pure, and deliberately all-or-nothing per frame: a telemetry tick carrying
 * one unusable vehicle is rejected whole rather than drawn minus that
 * vehicle, because half a fleet is a WRONG fleet picture, not a smaller one.
 * The last good rows stay on screen instead.
 *
 * Feed text is data, never an instruction: every string that reaches a row is
 * bounded by the wire contract's own grammar and is only ever displayed.
 * @module layers/headsup/records
 */

/** Wire identifiers (vehicle, operator, advisory) share the contract grammar. */
const ID_GRAMMAR = /^[A-Za-z0-9._:-]{1,128}$/;

const GEOMETRY_TYPES = new Set([
  'Point',
  'MultiPoint',
  'LineString',
  'MultiLineString',
  'Polygon',
  'MultiPolygon',
]);

const HAZARD_CLASSES = new Set([
  'FLOOD',
  'WORK_ZONE',
  'SIGNAL_OUTAGE',
  'EMERGENCY_SCENE',
  'SCHOOL_ZONE',
  'SEVERE_WEATHER',
]);

const ACTION_TYPES = new Set([
  'NO_ENTRY',
  'SPEED_CAP',
  'ALL_WAY_STOP',
  'DENSITY_CAP',
  'KEEP_CLEAR',
  'PAUSE_SERVICE',
]);

/** One tick cannot carry a whole city; past this the frame is not a tick. */
const MAX_VEHICLES = 5_000;
/** A ring longer than this is a dataset, not one hazard area. */
const MAX_RING_POSITIONS = 10_000;

/** Ring depth per geometry kind, used to find a label anchor. */
const ANCHOR_DEPTH = Object.freeze({
  Point: 0,
  MultiPoint: 1,
  LineString: 1,
  MultiLineString: 2,
  Polygon: 2,
  MultiPolygon: 3,
});

function identifier(value) {
  return typeof value === 'string' && ID_GRAMMAR.test(value) ? value : null;
}

function lonLat(pair) {
  if (!Array.isArray(pair) || pair.length < 2) return null;
  const [lon, lat] = pair;
  if (!Number.isFinite(lon) || Math.abs(lon) > 180) return null;
  if (!Number.isFinite(lat) || Math.abs(lat) > 90) return null;
  return [lon, lat];
}

/** An RFC 3339 instant as epoch milliseconds, or null. */
function instantMs(value) {
  if (typeof value !== 'string' || !value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function ringPositions(ring) {
  if (!Array.isArray(ring) || ring.length < 4) return null;
  if (ring.length > MAX_RING_POSITIONS) return null;
  const positions = [];
  for (const pair of ring) {
    const point = lonLat(pair);
    if (!point) return null;
    positions.push(point);
  }
  return positions;
}

/** One corridor: an open path, so two positions are enough. */
function linePositions(line) {
  if (!Array.isArray(line) || line.length < 2) return null;
  if (line.length > MAX_RING_POSITIONS) return null;
  const positions = [];
  for (const pair of line) {
    const point = lonLat(pair);
    if (!point) return null;
    positions.push(point);
  }
  return positions;
}

/** One polygon: exterior ring first, holes after. */
function polygonRings(rings) {
  if (!Array.isArray(rings) || !rings.length) return null;
  const normalized = [];
  for (const ring of rings) {
    const positions = ringPositions(ring);
    if (!positions) return null;
    normalized.push(positions);
  }
  return normalized;
}

/** Every coordinate in any geometry kind, as the label's candidate anchors. */
function anchorPoints(geometry) {
  const points = [];
  const walk = (node, depth) => {
    if (depth === 0) {
      const point = lonLat(node);
      if (point) points.push(point);
      return;
    }
    if (!Array.isArray(node)) return;
    for (const child of node) walk(child, depth - 1);
  };
  walk(geometry.coordinates, ANCHOR_DEPTH[geometry.type]);
  return points;
}

/**
 * Validate one `{type: 'telemetry', vehicles: [...]}` frame.
 *
 * Two optional fields widen a row beyond position: `ghost` marks a vehicle in
 * the control fleet that is NOT acting on advisories, and `rerouted_by` names
 * the advisory that moved this one. Both are what makes the Heads Up and
 * ghost cohorts separable on screen, which is the whole claim the demo makes.
 * @param {unknown} payload Parsed socket frame.
 * @returns {Array<{id: string, op: string, lon: number, lat: number,
 *   heading: number, ghost: boolean, reroutedBy: string|null}>|null} Rows, or
 *   null when the frame is unusable.
 */
export function normalizeTelemetryFrame(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload))
    return null;
  if (payload.type !== 'telemetry') return null;
  const { vehicles } = payload;
  if (vehicles == null) return [];
  if (!Array.isArray(vehicles) || vehicles.length > MAX_VEHICLES) return null;
  const rows = [];
  const ids = new Set();
  for (const vehicle of vehicles) {
    if (!vehicle || typeof vehicle !== 'object' || Array.isArray(vehicle))
      return null;
    const id = identifier(vehicle.id);
    const op = identifier(vehicle.op);
    const position = lonLat([vehicle.lon, vehicle.lat]);
    if (!id || !op || !position || ids.has(id)) return null;
    ids.add(id);
    // A heading outside the contract's [0, 360) becomes 0 rather than being
    // wrapped. The dot's POSITION is what this layer draws; a repaired
    // bearing would be our invention rather than the simulator's reading.
    const heading = Number(vehicle.heading);
    const bearing =
      Number.isFinite(heading) && heading >= 0 && heading < 360 ? heading : 0;
    // A `rerouted_by` that is present but not an identifier rejects the whole
    // tick: the claim "this vehicle moved because of that advisory" is the
    // demo's evidence, so a damaged one is not quietly dropped.
    const reroutedBy =
      vehicle.rerouted_by == null ? null : identifier(vehicle.rerouted_by);
    if (vehicle.rerouted_by != null && !reroutedBy) return null;
    rows.push({
      id,
      op,
      lon: position[0],
      lat: position[1],
      heading: bearing,
      ghost: vehicle.ghost === true,
      reroutedBy,
    });
  }
  return rows;
}

/**
 * Validate one advisory GeoJSON Feature (`schemas/advisory.schema.json`).
 *
 * Polygon and MultiPolygon geometry becomes drawable rings, and LineString
 * and MultiLineString become drawable corridors — an advisory addressed to a
 * route rather than to an area. Point geometry normalizes to a row with
 * neither, so an advisory this layer cannot yet draw is still counted and
 * labelled instead of silently dropped.
 * @param {unknown} feature Parsed `advisory.feature`.
 * @returns {object|null} One advisory row, or null when it is unusable.
 */
export function normalizeAdvisoryFeature(feature) {
  if (!feature || typeof feature !== 'object' || Array.isArray(feature))
    return null;
  if (feature.type != null && feature.type !== 'Feature') return null;
  const geometry = feature.geometry;
  const properties = feature.properties;
  if (!geometry || typeof geometry !== 'object' || Array.isArray(geometry))
    return null;
  if (!GEOMETRY_TYPES.has(geometry.type)) return null;
  if (
    !properties ||
    typeof properties !== 'object' ||
    Array.isArray(properties)
  )
    return null;

  const id = identifier(properties.id);
  const hazard = properties.hazard;
  const action = properties.action;
  if (!id || !hazard || typeof hazard !== 'object') return null;
  if (!action || typeof action !== 'object') return null;
  if (!HAZARD_CLASSES.has(hazard.class)) return null;
  if (!ACTION_TYPES.has(action.type)) return null;

  const severity = Number(hazard.severity);
  const confidence = Number(hazard.confidence);
  if (!Number.isInteger(severity) || severity < 1 || severity > 5) return null;
  if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1)
    return null;

  const validFrom = instantMs(properties.valid_from);
  const validUntil = instantMs(properties.valid_until);
  if (validFrom == null || validUntil == null || validUntil < validFrom)
    return null;

  const polygons = [];
  if (geometry.type === 'Polygon') {
    const rings = polygonRings(geometry.coordinates);
    if (!rings) return null;
    polygons.push(rings);
  } else if (geometry.type === 'MultiPolygon') {
    if (!Array.isArray(geometry.coordinates) || !geometry.coordinates.length)
      return null;
    for (const polygon of geometry.coordinates) {
      const rings = polygonRings(polygon);
      if (!rings) return null;
      polygons.push(rings);
    }
  }
  const lines = [];
  if (geometry.type === 'LineString') {
    const positions = linePositions(geometry.coordinates);
    if (!positions) return null;
    lines.push(positions);
  } else if (geometry.type === 'MultiLineString') {
    if (!Array.isArray(geometry.coordinates) || !geometry.coordinates.length)
      return null;
    for (const line of geometry.coordinates) {
      const positions = linePositions(line);
      if (!positions) return null;
      lines.push(positions);
    }
  }
  const anchors = anchorPoints(geometry);
  if (!anchors.length) return null;

  return {
    id,
    version: Number.isInteger(properties.version) ? properties.version : 1,
    geometryType: geometry.type,
    polygons,
    lines,
    anchors,
    hazardClass: hazard.class,
    severity,
    confidence,
    action: action.type,
    validFrom,
    validUntil,
    evidenceCount: Array.isArray(properties.evidence)
      ? properties.evidence.length
      : 0,
    policyRule: identifier(properties.policy_rule),
    issuer: typeof properties.issuer === 'string' ? properties.issuer : null,
    // Whether the advisory ARRIVED carrying a signature. This layer never
    // claims the signature verifies — only the issuer's key can say that.
    signed: typeof properties.signature === 'string' && !!properties.signature,
  };
}

/** Scenario run states the viewer socket reports. */
const SCENARIO_STATES = new Set(['idle', 'running']);
/** A scenario label is a badge and a short name, never a paragraph. */
const MAX_SCENARIO_LABEL_CHARS = 64;
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/;

function label(value, limit = MAX_SCENARIO_LABEL_CHARS) {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!trimmed || trimmed.length > limit) return null;
  return CONTROL_CHARACTERS.test(trimmed) ? null : trimmed;
}

/**
 * Validate one `{type: 'scenario_state', ...}` frame.
 *
 * This is the frame the scenario readout — name, sim clock, replay speed — is
 * driven from, so a damaged one leaves the previous readout standing rather
 * than blanking a clock that was correct a moment ago.
 * @param {unknown} payload Parsed socket frame.
 * @returns {object|null} One scenario-state row, or null when unusable.
 */
export function normalizeScenarioStateFrame(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload))
    return null;
  if (payload.type !== 'scenario_state') return null;
  if (!SCENARIO_STATES.has(payload.state)) return null;
  const scenario =
    payload.scenario && typeof payload.scenario === 'object'
      ? payload.scenario
      : null;
  const speed = Number(payload.speed);
  const elapsed = Number(payload.elapsed_s);
  return {
    state: payload.state,
    id: scenario ? identifier(scenario.id) : null,
    code: scenario ? label(scenario.code, 8) : null,
    name: scenario ? label(scenario.name) : null,
    city: scenario ? label(scenario.city) : null,
    // The simulator's clock, not ours: shown as the text it sent, parsed only
    // to prove it is an instant.
    simTime: instantMs(payload.sim_time),
    simTimeText: label(payload.sim_time, 40),
    elapsedS: Number.isFinite(elapsed) && elapsed >= 0 ? elapsed : 0,
    speed: Number.isFinite(speed) && speed > 0 && speed <= 10_000 ? speed : 1,
    scripted: payload.scripted === true,
  };
}

/** What an operator answered. An ack without a status is an acceptance. */
export const ACK_STATUSES = Object.freeze(['ACCEPTED', 'DECLINED']);

/**
 * Validate one `{type: 'ack', ack: {...}}` frame.
 *
 * An acknowledgement is an operator's claim that it applied an advisory to a
 * named number of vehicles. The counter strip reports how many operators have
 * answered; the agent pipeline rail (D3.6) is what will show the rest.
 *
 * `status` is the one field here that changes what is DRAWN: a fleet that
 * declined is tagged on screen, because an advisory nobody acted on is the
 * most important thing an operator can be told. A status this build does not
 * know is read as a refusal rather than as an acceptance — the safe reading
 * of "something other than yes" is not yes.
 * @param {unknown} payload Parsed socket frame.
 * @returns {{advisoryId: string|null, operator: string, vehicles: number,
 *   status: string}|null}
 */
export function normalizeAckFrame(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload))
    return null;
  if (payload.type !== 'ack') return null;
  const ack = payload.ack;
  if (!ack || typeof ack !== 'object' || Array.isArray(ack)) return null;
  const operator = identifier(ack.operator);
  if (!operator) return null;
  const vehicles = Number(ack.vehicles);
  const declined =
    ack.status != null &&
    String(ack.status).trim().toUpperCase() !== 'ACCEPTED';
  return {
    advisoryId: identifier(ack.advisory_id),
    operator,
    vehicles:
      Number.isInteger(vehicles) && vehicles >= 0 && vehicles <= MAX_VEHICLES
        ? vehicles
        : 0,
    // i18n-ignore: contract values, carried on the wire and never shown
    status: declined ? 'DECLINED' : 'ACCEPTED',
  };
}

/**
 * Validate one `{type: 'hazard', hazard: {...}}` frame.
 *
 * The fused hazard, which arrives BEFORE any advisory exists for it. Only its
 * identity and class are read here; the advisory feature carries the geometry
 * this layer draws.
 * @param {unknown} payload Parsed socket frame.
 * @returns {{id: string, hazardClass: string, severity: number}|null}
 */
export function normalizeHazardFrame(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload))
    return null;
  if (payload.type !== 'hazard') return null;
  const hazard = payload.hazard;
  if (!hazard || typeof hazard !== 'object' || Array.isArray(hazard))
    return null;
  const id = identifier(hazard.id);
  if (!id || !HAZARD_CLASSES.has(hazard.class)) return null;
  const severity = Number(hazard.severity);
  return {
    id,
    hazardClass: hazard.class,
    severity:
      Number.isInteger(severity) && severity >= 1 && severity <= 5
        ? severity
        : 1,
  };
}
