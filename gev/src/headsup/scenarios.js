/**
 * The Heads Up scenario API: what the Test Scene picker reads and calls.
 *
 * Four calls, matching `viz/mock_stream.py` and the API that replaces it:
 *
 * - `GET /scenarios` — the picker's rows, one what-if line each.
 * - `POST /scenarios/{id}/start` — begin a what-if replay.
 * - `POST /scenarios/stop` — return the feed to idle.
 * - `POST /approvals/{proposal_id}` — the second key a two-key advisory is
 *   held on, which the agent rail's Compiler card offers as a button.
 *
 * Portable: no Cesium, no DOM. `fetch` is injected so the lifecycle can be
 * exercised offline.
 *
 * Every string here is FEED TEXT — a scenario name, a city, a what-if line —
 * so it is bounded by this module's own grammar, carried as data, and only
 * ever displayed. None of it reaches an instruction slot, a URL this module
 * did not build from a validated id, or `innerHTML`.
 *
 * A listing is all-or-nothing, the same rule
 * `src/layers/headsup/records.js` applies to a telemetry tick: a picker
 * showing two of three scenarios is a WRONG menu, not a shorter one.
 * @module headsup/scenarios
 */

// A portable package (no i18n): these messages stay English data and the
// surface that shows them translates at its own presentation boundary.
const msg = (text) => text;

/** Scenario ids share the wire contract's identifier grammar. */
const ID_GRAMMAR = /^[A-Za-z0-9._:-]{1,64}$/;
/** A short badge: `S1`, not a sentence. */
const CODE_GRAMMAR = /^[A-Za-z0-9 ._:-]{1,8}$/;

/** More rows than this is a dataset, not a demo menu. */
const MAX_SCENARIOS = 24;
const MAX_NAME_CHARS = 64;
const MAX_WHAT_IF_CHARS = 280;
/** A listing or a control call that has not answered by now is not going to. */
export const SCENARIO_REQUEST_TIMEOUT_MS = 6_000;

/** Reject the C0/C1 control characters outright rather than stripping them. */
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/;

function identifier(value) {
  return typeof value === 'string' && ID_GRAMMAR.test(value) ? value : null;
}

/** One line of display text, bounded and free of control characters. */
function text(value, limit) {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!trimmed || trimmed.length > limit) return null;
  return CONTROL_CHARACTERS.test(trimmed) ? null : trimmed;
}

function degrees(value, limit) {
  const numeric = Number(value);
  return Number.isFinite(numeric) && Math.abs(numeric) <= limit
    ? numeric
    : null;
}

/**
 * The camera pose a scenario opens on.
 *
 * Height is metres above the ellipsoid and is capped well below orbit: a
 * what-if is a street-level question, and an out-of-range value is a feed
 * defect rather than an instruction to leave the planet.
 */
function startView(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const lon = degrees(value.lon, 180);
  const lat = degrees(value.lat, 90);
  if (lon === null || lat === null) return null;
  const height = Number(value.height);
  const heading = Number(value.heading);
  const pitch = Number(value.pitch);
  return Object.freeze({
    lon,
    lat,
    height:
      Number.isFinite(height) && height >= 50 && height <= 200_000
        ? height
        : 900,
    heading: Number.isFinite(heading) ? ((heading % 360) + 360) % 360 : 0,
    pitch: Number.isFinite(pitch) && pitch >= -90 && pitch <= 90 ? pitch : -35,
  });
}

/**
 * Validate one scenario row.
 * @param {unknown} row
 * @returns {object|null} A frozen row, or null when it is unusable.
 */
export function normalizeScenario(row) {
  if (!row || typeof row !== 'object' || Array.isArray(row)) return null;
  const id = identifier(row.id);
  const name = text(row.name, MAX_NAME_CHARS);
  if (!id || !name) return null;
  const code =
    typeof row.code === 'string' && CODE_GRAMMAR.test(row.code.trim())
      ? row.code.trim()
      : null;
  const view = startView(row.start_view);
  if (!view) return null;
  const speed = Number(row.speed);
  return Object.freeze({
    id,
    code,
    name,
    city: text(row.city, MAX_NAME_CHARS),
    hazardClass: identifier(row.hazard_class),
    whatIf: text(row.what_if, MAX_WHAT_IF_CHARS),
    speed: Number.isFinite(speed) && speed > 0 && speed <= 10_000 ? speed : 1,
    scripted: row.scripted === true,
    startView: view,
  });
}

/**
 * Validate a whole `GET /scenarios` payload.
 *
 * Both `{"scenarios": [...]}` and a bare array are accepted: the envelope is
 * what the mock and the API send, and the bare array is what a hand-written
 * fixture tends to be. Anything else, or any unusable row, rejects the lot.
 * @param {unknown} payload
 * @returns {object[]|null}
 */
export function normalizeScenarioListing(payload) {
  const rows = Array.isArray(payload)
    ? payload
    : Array.isArray(payload?.scenarios)
      ? payload.scenarios
      : null;
  if (!rows || rows.length > MAX_SCENARIOS) return null;
  const normalized = [];
  const ids = new Set();
  for (const row of rows) {
    const scenario = normalizeScenario(row);
    if (!scenario || ids.has(scenario.id)) return null;
    ids.add(scenario.id);
    normalized.push(scenario);
  }
  return normalized;
}

/**
 * Validate the answer to an approval call.
 *
 * Only `approved` counts. An origin that answers 200 with something else has
 * not told this page that a second key was recorded, and a console that read
 * silence as consent would be the worst possible failure in a two-key
 * control: the button would look as though it worked.
 * @param {unknown} payload
 * @returns {{state: string}|null}
 */
export function normalizeApproval(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload))
    return null;
  return payload.state === 'approved'
    ? Object.freeze({ state: 'approved' })
    : null;
}

/**
 * Validate the answer to a start or stop call.
 * @param {unknown} payload
 * @returns {{state: string, scenario: object|null}|null}
 */
export function normalizeScenarioControl(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload))
    return null;
  const state = payload.state === 'running' ? 'running' : 'idle';
  return Object.freeze({
    state,
    scenario: payload.scenario ? normalizeScenario(payload.scenario) : null,
  });
}

/** Raised when the API answers with something this module will not show. */
export class ScenarioApiError extends Error {
  /**
   * @param {string} message English message data, for a `t()` at the surface.
   * @param {object} [options]
   * @param {number|null} [options.status] HTTP status, when there was one.
   */
  constructor(message, { status = null } = {}) {
    super(message);
    this.name = 'ScenarioApiError';
    this.status = status;
  }
}

/**
 * Talk to one scenario API, with the mock feed as its fallback.
 *
 * The origins are a LADDER, matching the viewer socket's: the real API is
 * asked first, and an origin that cannot be REACHED hands the call to the
 * next one. A refusal or an unreadable answer does not — something is
 * listening there, and asking a second service to override the first one's
 * "no" would make the page lie about which API it is driving.
 *
 * Once an origin answers, it is the one the client keeps using, so a session
 * does not alternate between a real run and a mock one.
 * @param {object} [options]
 * @param {string} [options.baseUrl] API origin, tried first.
 * @param {string|null} [options.fallbackUrl] Origin tried when the first one
 *   is unreachable.
 * @param {typeof fetch} [options.fetchImpl] Fetch seam, for tests.
 * @param {number} [options.timeoutMs] Per-call ceiling.
 * @returns {{baseUrl: string, fallback: boolean, list: Function,
 *   start: Function, stop: Function}}
 */
export function createScenarioClient({
  baseUrl,
  fallbackUrl = null,
  fetchImpl = (...args) => globalThis.fetch(...args),
  timeoutMs = SCENARIO_REQUEST_TIMEOUT_MS,
} = {}) {
  const normalize = (value) =>
    String(value || '')
      .trim()
      .replace(/\/+$/, '');
  const origin = normalize(baseUrl);
  if (!origin) throw new TypeError('A scenario API base URL is required');
  // A fallback equal to the primary is not a second origin.
  const origins = [
    ...new Set([origin, normalize(fallbackUrl)].filter(Boolean)),
  ];
  let chosen = 0;

  /** One request to one origin. Throws `ScenarioApiError` either way. */
  const request = async (target, path, { method, signal }) => {
    const deadline = AbortSignal.timeout(timeoutMs);
    const combined = signal ? AbortSignal.any([signal, deadline]) : deadline;
    let response;
    try {
      response = await fetchImpl(`${target}${path}`, {
        method,
        signal: combined,
        // A scenario call carries no credentials and must not pick any up.
        credentials: 'omit',
        redirect: 'error',
        headers: { Accept: 'application/json' },
      });
    } catch (error) {
      signal?.throwIfAborted();
      const unreachable = new ScenarioApiError(
        error?.name === 'TimeoutError'
          ? msg('The Heads Up scenario API did not answer.')
          : msg('The Heads Up scenario API is not reachable.'),
      );
      // The one failure another origin could answer instead.
      unreachable.unreachable = true;
      throw unreachable;
    }
    if (!response.ok)
      throw new ScenarioApiError(msg('The Heads Up scenario API refused.'), {
        status: response.status,
      });
    try {
      return await response.json();
    } catch {
      throw new ScenarioApiError(
        msg('The Heads Up scenario API answered with something unreadable.'),
      );
    }
  };

  /** One call, walking the ladder from whichever origin last answered. */
  const call = async (path, { method = 'GET', signal } = {}) => {
    signal?.throwIfAborted();
    let unreachable = null;
    for (let step = 0; step < origins.length; step++) {
      const index = (chosen + step) % origins.length;
      try {
        const payload = await request(origins[index], path, { method, signal });
        chosen = index;
        return payload;
      } catch (error) {
        if (!error?.unreachable) throw error;
        unreachable = error;
        signal?.throwIfAborted();
      }
    }
    throw unreachable;
  };

  return {
    /** The origin that last answered, which may be the fallback. */
    get baseUrl() {
      return origins[chosen];
    },
    /** Whether that origin is the fallback rather than the real API. */
    get fallback() {
      return chosen > 0;
    },
    /** Every origin this client will try, in order. */
    origins: Object.freeze([...origins]),

    /**
     * Every scenario the API offers.
     * @param {{signal?: AbortSignal}} [options]
     * @returns {Promise<object[]>}
     */
    async list({ signal } = {}) {
      const rows = normalizeScenarioListing(
        await call('/scenarios', { signal }),
      );
      if (!rows)
        throw new ScenarioApiError(
          msg('The Heads Up scenario list is not usable.'),
        );
      return rows;
    },

    /**
     * Begin one scenario.
     * @param {string} id A scenario id from `list()`.
     * @param {{signal?: AbortSignal}} [options]
     * @returns {Promise<{state: string, scenario: object|null}>}
     */
    async start(id, { signal } = {}) {
      // The id came off the wire once already. Re-check it here rather than
      // trusting a caller to have kept it intact, because this is where it
      // becomes part of a URL.
      const scenarioId = identifier(id);
      if (!scenarioId) throw new TypeError('A scenario id is required');
      const result = normalizeScenarioControl(
        await call(`/scenarios/${encodeURIComponent(scenarioId)}/start`, {
          method: 'POST',
          signal,
        }),
      );
      if (!result)
        throw new ScenarioApiError(
          msg('The Heads Up scenario API answered with something unreadable.'),
        );
      return result;
    },

    /**
     * Supply the second key one pending advisory is waiting for.
     *
     * `POST /approvals/{proposal_id}`. The compiler holds a severity-4 or
     * over-area advisory until a person approves it, so this is the one call
     * in this client that changes what gets PUBLISHED rather than what gets
     * replayed — which is why its answer is validated strictly and a refusal
     * is surfaced rather than retried against another origin.
     * @param {string} proposalId The proposal id the compiler named.
     * @param {{signal?: AbortSignal}} [options]
     * @returns {Promise<{state: string}>}
     */
    async approve(proposalId, { signal } = {}) {
      // The id came off the wire once already. Re-check it here, because
      // this is where it becomes part of a URL.
      const id = identifier(proposalId);
      if (!id) throw new TypeError('A proposal id is required');
      const result = normalizeApproval(
        await call(`/approvals/${encodeURIComponent(id)}`, {
          method: 'POST',
          signal,
        }),
      );
      if (!result)
        throw new ScenarioApiError(
          msg('The Heads Up API did not confirm the approval.'),
        );
      return result;
    },

    /**
     * End whatever is running.
     * @param {{signal?: AbortSignal}} [options]
     * @returns {Promise<{state: string, scenario: object|null}>}
     */
    async stop({ signal } = {}) {
      return (
        normalizeScenarioControl(
          await call('/scenarios/stop', { method: 'POST', signal }),
        ) ?? Object.freeze({ state: 'idle', scenario: null })
      );
    },
  };
}
