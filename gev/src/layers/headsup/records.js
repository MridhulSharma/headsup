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
/**
 * Bounds on the reasoning frames the agent rail is drawn from.
 *
 * Every one of these is a DISPLAY limit, not a correctness one: the rail
 * shows a list a person reads, and a frame carrying a thousand evidence rows
 * or a ten-kilobyte rationale is a feed defect or a flood, not a longer
 * explanation. Over the bound the frame is rejected whole, the same
 * all-or-nothing rule a telemetry tick gets, because half a critic verdict
 * is a wrong verdict rather than a shorter one.
 */
const MAX_EVIDENCE = 32;
const MAX_CHECKS = 32;
const MAX_REASONS = 16;
const MAX_EXTRACTED_FIELDS = 32;
const MAX_ASSISTS = 512;
/** A rationale is a paragraph; a raw dispatch message is a short record. */
const MAX_RATIONALE_CHARS = 600;
const MAX_RAW_TEXT_CHARS = 2_000;
const MAX_FIELD_CHARS = 200;
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

  // The evidence rows themselves, not only how many there are. The advisory
  // card names the public feeds an advisory rests on, and reading them off a
  // neighbouring fusion frame instead would be the console inferring
  // provenance rather than reporting what the advisory carried. A damaged
  // row rejects the advisory, the same way it does for a fused hazard.
  const evidence = evidenceRows(properties.evidence);
  if (!evidence) return null;

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
    evidence,
    evidenceCount: evidence.length,
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
 * A multi-line block of feed text, for the raw dispatch message.
 *
 * Unlike `label`, line breaks and tabs are kept: a dispatch record arrives
 * with the shape it was written in, and reflowing it would be this module
 * editing the evidence. Carriage returns are normalized so one message does
 * not render with blank lines between every line, and every OTHER control
 * character still rejects the whole frame.
 * @param {unknown} value
 * @param {number} limit Characters.
 * @returns {string|null}
 */
function block(value, limit) {
  if (typeof value !== 'string') return null;
  const normalized = value.replace(/\r\n?/g, '\n').trim();
  if (!normalized || normalized.length > limit) return null;
  // The same class as `label`, less the two whitespace characters kept above.
  return /[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/.test(normalized)
    ? null
    : normalized;
}

/**
 * Validate one `evidence[]` array into display rows.
 *
 * `source` is the field the rail's FEEDS row lights a chip for, so it is
 * the one that has to be an identifier rather than free text: a chip is a
 * claim about WHICH public feed an advisory rests on.
 * @param {unknown} rows
 * @returns {Array<object>|null} Rows, or null when any one is unusable.
 */
function evidenceRows(rows) {
  if (rows == null) return [];
  if (!Array.isArray(rows) || rows.length > MAX_EVIDENCE) return null;
  const normalized = [];
  for (const row of rows) {
    if (!row || typeof row !== 'object' || Array.isArray(row)) return null;
    const source = identifier(row.source);
    if (!source) return null;
    normalized.push({
      eventId: identifier(row.event_id),
      source,
      kind: identifier(row.kind),
      observedAt: instantMs(row.observed_at),
      note: label(row.note, MAX_FIELD_CHARS),
      rawRef: label(row.raw_ref, MAX_FIELD_CHARS),
    });
  }
  return normalized;
}

/**
 * Validate one `checks[]` array: the named policy rules and their results.
 *
 * A check whose `passed` is not a boolean rejects the frame rather than
 * being read as a failure: "we do not know whether the hospital-access rule
 * held" is not the same claim as "it did not hold", and a critic card is
 * exactly where that difference matters.
 * @param {unknown} rows
 * @returns {Array<{rule: string, passed: boolean}>|null}
 */
function checkRows(rows) {
  if (rows == null) return [];
  if (!Array.isArray(rows) || rows.length > MAX_CHECKS) return null;
  const normalized = [];
  for (const row of rows) {
    if (!row || typeof row !== 'object' || Array.isArray(row)) return null;
    const rule = identifier(row.rule);
    if (!rule || typeof row.passed !== 'boolean') return null;
    normalized.push({ rule, passed: row.passed });
  }
  return normalized;
}

/** Validate one `reasons[]` array: short lines a person reads. */
function reasonRows(rows) {
  if (rows == null) return [];
  if (!Array.isArray(rows) || rows.length > MAX_REASONS) return null;
  const normalized = [];
  for (const row of rows) {
    const line = label(row, MAX_RATIONALE_CHARS);
    if (!line) return null;
    normalized.push(line);
  }
  return normalized;
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

/**
 * What an operator answered. An ack without a status is an acceptance.
 *
 * `DELAYED` is the third answer and not a shade of the other two: the fleet
 * HAS the advisory and has not applied it yet. An operator watching a
 * severe-weather advisory go out needs "not yet" to read differently from
 * "no", because the two call for different next moves.
 */
export const ACK_STATUSES = Object.freeze(['ACCEPTED', 'DELAYED', 'DECLINED']);

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
 * of "something other than yes" is not yes, and that reading must not quietly
 * widen to cover `DELAYED`, which is a contract value with a tag of its own.
 * @param {unknown} payload Parsed socket frame.
 * @returns {{advisoryId: string|null, operator: string, vehicles: number,
 *   status: string, actionTaken: string|null, reason: string|null,
 *   receivedAt: number|null}|null}
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
  const answer =
    ack.status == null ? 'ACCEPTED' : String(ack.status).trim().toUpperCase();
  return {
    advisoryId: identifier(ack.advisory_id),
    operator,
    vehicles:
      Number.isInteger(vehicles) && vehicles >= 0 && vehicles <= MAX_VEHICLES
        ? vehicles
        : 0,
    // i18n-ignore: contract values, carried on the wire and never shown
    status: ACK_STATUSES.includes(answer) ? answer : 'DECLINED',
    actionTaken: label(ack.action_taken, MAX_RATIONALE_CHARS),
    reason: label(ack.reason, MAX_RATIONALE_CHARS),
    receivedAt: instantMs(ack.received_at),
  };
}

/**
 * Validate one `{type: 'hazard', hazard: {...}}` frame.
 *
 * The fused hazard, which arrives BEFORE any advisory exists for it. The
 * geometry stays with the advisory feature — that is what this layer draws —
 * but everything the Fusion card shows is read here: the combined confidence,
 * the rationale fusion wrote, and the evidence it combined. A hazard that is
 * fused and never published is then still fully explainable, which is the
 * case an operator most wants an answer for.
 * @param {unknown} payload Parsed socket frame.
 * @returns {object|null}
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
  const confidence = Number(hazard.confidence);
  const evidence = evidenceRows(hazard.evidence);
  if (!evidence) return null;
  return {
    id,
    hazardClass: hazard.class,
    severity:
      Number.isInteger(severity) && severity >= 1 && severity <= 5
        ? severity
        : 1,
    confidence:
      Number.isFinite(confidence) && confidence >= 0 && confidence <= 1
        ? confidence
        : null,
    rationale: label(hazard.rationale, MAX_RATIONALE_CHARS),
    validUntil: instantMs(hazard.valid_until),
    evidence,
  };
}

/**
 * Validate one `{type: 'candidate', candidate: {...}}` frame.
 *
 * A sentinel's unfused reading: one source, no decision taken. This is the
 * frame the rail's SENTINELS stage lights on, and the only one that can
 * carry a raw message beside the fields an agent pulled out of it.
 *
 * `rawText` and `extracted` are what make a text agent checkable. They are
 * FEED TEXT in the strongest sense — a dispatch message written by somebody
 * outside this system — so they are bounded, carried as data, and only ever
 * displayed. Nothing here reaches an instruction slot.
 * @param {unknown} payload Parsed socket frame.
 * @returns {object|null}
 */
export function normalizeCandidateFrame(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload))
    return null;
  if (payload.type !== 'candidate') return null;
  const candidate = payload.candidate;
  if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate))
    return null;
  const id = identifier(candidate.id);
  const sentinel = identifier(candidate.sentinel);
  if (!id || !sentinel) return null;
  if (!HAZARD_CLASSES.has(candidate.hazard_class)) return null;
  const evidence = evidenceRows(candidate.evidence);
  if (!evidence) return null;
  const confidence = Number(candidate.confidence);
  // Present-but-unusable rejects the frame: a card claiming to show what an
  // agent extracted must not show a repaired version of it.
  let extracted = [];
  if (candidate.extracted != null) {
    if (
      !Array.isArray(candidate.extracted) ||
      candidate.extracted.length > MAX_EXTRACTED_FIELDS
    )
      return null;
    for (const row of candidate.extracted) {
      if (!row || typeof row !== 'object' || Array.isArray(row)) return null;
      const field = label(row.field, MAX_FIELD_CHARS);
      const value = label(row.value, MAX_FIELD_CHARS);
      if (!field || !value) return null;
      extracted.push({ field, value });
    }
  }
  let rawText = null;
  if (candidate.raw_text != null) {
    rawText = block(candidate.raw_text, MAX_RAW_TEXT_CHARS);
    if (!rawText) return null;
  }
  if (!rawText) extracted = [];
  return {
    id,
    sentinel,
    hazardClass: candidate.hazard_class,
    confidence:
      Number.isFinite(confidence) && confidence >= 0 && confidence <= 1
        ? confidence
        : null,
    observedAt: instantMs(candidate.observed_at),
    note: label(candidate.note, MAX_RATIONALE_CHARS),
    evidence,
    // The message the sentinel read, and the fields it says it read out of
    // it. Shown side by side, never one without the other.
    rawText,
    extracted,
  };
}

/**
 * Validate one `{type: 'proposal', proposal: {...}}` frame.
 * @param {unknown} payload Parsed socket frame.
 * @returns {object|null}
 */
export function normalizeProposalFrame(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload))
    return null;
  if (payload.type !== 'proposal') return null;
  const proposal = payload.proposal;
  if (!proposal || typeof proposal !== 'object' || Array.isArray(proposal))
    return null;
  const id = identifier(proposal.id);
  if (!id) return null;
  const action = proposal.action;
  if (!action || typeof action !== 'object' || Array.isArray(action))
    return null;
  if (!ACTION_TYPES.has(action.type)) return null;
  const revision = Number(proposal.revision);
  return {
    id,
    hazardId: identifier(proposal.hazard_id),
    revision:
      Number.isInteger(revision) && revision >= 1 && revision <= 99
        ? revision
        : 1,
    action: action.type,
    playbook: identifier(proposal.playbook),
    policyRule: identifier(proposal.policy_rule),
    validUntil: instantMs(proposal.valid_until),
    rationale: label(proposal.rationale, MAX_RATIONALE_CHARS),
  };
}

/** What the critic can answer. */
export const VERDICT_DECISIONS = Object.freeze([
  'APPROVE',
  'REVISE',
  'ESCALATE',
]);

/**
 * Validate one `{type: 'verdict', verdict: {...}}` frame.
 *
 * A decision this build does not know is read as `ESCALATE` rather than as
 * an approval, on the same rule the acknowledgement follows: the safe
 * reading of an answer we cannot parse is that a person still has to look.
 * @param {unknown} payload Parsed socket frame.
 * @returns {object|null}
 */
export function normalizeVerdictFrame(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload))
    return null;
  if (payload.type !== 'verdict') return null;
  const verdict = payload.verdict;
  if (!verdict || typeof verdict !== 'object' || Array.isArray(verdict))
    return null;
  const proposalId = identifier(verdict.proposal_id);
  if (!proposalId) return null;
  const checks = checkRows(verdict.checks);
  const reasons = reasonRows(verdict.reasons);
  if (!checks || !reasons) return null;
  // `approved` is the older boolean form the D2.x mock sent; a frame that
  // carries only that is still readable rather than being dropped.
  const decided =
    verdict.decision == null
      ? verdict.approved === true
        ? 'APPROVE'
        : 'ESCALATE'
      : String(verdict.decision).trim().toUpperCase();
  return {
    proposalId,
    // i18n-ignore: contract values, carried on the wire and never shown raw
    decision: VERDICT_DECISIONS.includes(decided) ? decided : 'ESCALATE',
    decidedAt: instantMs(verdict.decided_at),
    checks,
    reasons,
    rationale: label(verdict.rationale, MAX_RATIONALE_CHARS),
  };
}

/** What the compiler can report. */
export const COMPILE_STATUSES = Object.freeze([
  'SIGNED',
  'REJECTED',
  'PENDING_APPROVAL',
]);

/**
 * Validate one `{type: 'compile', compile: {...}}` frame.
 *
 * The publishing gate's own answer: signed, rejected with reasons, or held
 * for a second key. A status this build does not know is read as `REJECTED`,
 * because the safe reading of an answer we cannot parse is that nothing was
 * published.
 *
 * `signatureVerified` is deliberately three-valued. `true` and `false` are
 * claims about a check that RAN; `null` means no check has been reported,
 * and the console must say "not verified" rather than either of the other
 * two — only the issuer's key can turn that into a yes.
 * @param {unknown} payload Parsed socket frame.
 * @returns {object|null}
 */
export function normalizeCompileFrame(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload))
    return null;
  if (payload.type !== 'compile') return null;
  const result = payload.compile;
  if (!result || typeof result !== 'object' || Array.isArray(result))
    return null;
  const proposalId = identifier(result.proposal_id);
  if (!proposalId) return null;
  const reasons = reasonRows(result.reasons);
  if (!reasons) return null;
  const status = String(result.status ?? '')
    .trim()
    .toUpperCase();
  return {
    proposalId,
    advisoryId: identifier(result.advisory_id),
    // i18n-ignore: contract values, carried on the wire and never shown raw
    status: COMPILE_STATUSES.includes(status) ? status : 'REJECTED',
    decidedAt: instantMs(result.decided_at),
    issuer: label(result.issuer, MAX_FIELD_CHARS),
    policyRule: identifier(result.policy_rule),
    signed: typeof result.signature === 'string' && !!result.signature,
    signatureVerified:
      typeof result.signature_verified === 'boolean'
        ? result.signature_verified
        : null,
    reasons,
    // Whether this advisory is waiting on a human approval token, which is
    // what puts an Approve button on the Compiler card.
    twoKey: result.two_key === true,
  };
}

/** Remote-assist request states the viewer socket reports. */
const ASSIST_STATES = new Set(['QUEUED', 'ACTIVE']);

/**
 * Validate one `{type: 'assist', assists: [...]}` frame.
 *
 * Whole state per frame, the way telemetry is: an assist a human cleared has
 * to leave the screen, so the frame is the complete open set and a damaged
 * one is rejected rather than merged. `cluster` is null until surge triage
 * groups a request with others; once it is set, every member of that cluster
 * draws as ONE marker carrying the count.
 * @param {unknown} payload Parsed socket frame.
 * @returns {Array<object>|null}
 */
export function normalizeAssistFrame(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload))
    return null;
  if (payload.type !== 'assist') return null;
  const { assists } = payload;
  if (assists == null) return [];
  if (!Array.isArray(assists) || assists.length > MAX_ASSISTS) return null;
  const rows = [];
  const ids = new Set();
  for (const assist of assists) {
    if (!assist || typeof assist !== 'object' || Array.isArray(assist))
      return null;
    const id = identifier(assist.id);
    const op = identifier(assist.op);
    const position = lonLat([assist.lon, assist.lat]);
    if (!id || !op || !position || ids.has(id)) return null;
    ids.add(id);
    const cluster = assist.cluster == null ? null : identifier(assist.cluster);
    if (assist.cluster != null && !cluster) return null;
    const state = String(assist.state ?? 'QUEUED')
      .trim()
      .toUpperCase();
    rows.push({
      id,
      op,
      lon: position[0],
      lat: position[1],
      ghost: assist.ghost === true,
      // i18n-ignore: contract values, carried on the wire and never shown raw
      state: ASSIST_STATES.has(state) ? state : 'QUEUED',
      cluster,
      openedAt: instantMs(assist.opened_at),
    });
  }
  return rows;
}

/** A tally this large is not one scenario's fleet. */
const MAX_TALLY = 1_000_000;

/** One `{live, ghost}` cohort pair, or null when either side is unusable. */
function cohortTally(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const live = Number(value.live);
  const ghost = Number(value.ghost);
  if (!Number.isInteger(live) || live < 0 || live > MAX_TALLY) return null;
  if (!Number.isInteger(ghost) || ghost < 0 || ghost > MAX_TALLY) return null;
  return { live, ghost };
}

/**
 * Validate one `{type: 'counters', counters: {...}}` frame.
 *
 * The two tallies the viewer cannot honestly derive for itself. "A vehicle
 * entered the hazard" and "a vehicle is waiting on a human" are facts about
 * the FLEET, and re-deriving them from dots on a globe would be this layer
 * inventing the demo's own headline number.
 * @param {unknown} payload Parsed socket frame.
 * @returns {{hazardEntries: {live: number, ghost: number},
 *   assistQueue: {live: number, ghost: number}}|null}
 */
export function normalizeCountersFrame(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload))
    return null;
  if (payload.type !== 'counters') return null;
  const counters = payload.counters;
  if (!counters || typeof counters !== 'object' || Array.isArray(counters))
    return null;
  const hazardEntries = cohortTally(counters.hazard_entries);
  const assistQueue = cohortTally(counters.assist_queue);
  if (!hazardEntries || !assistQueue) return null;
  return { hazardEntries, assistQueue };
}
