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

/**
 * Acquisition for the Heads Up viewer socket.
 *
 * The feed PUSHES: telemetry arrives at the simulator's tick rate and
 * advisories arrive when the compiler publishes one, so this source owns a
 * subscription rather than a poll. It still exposes `getSnapshot()` because
 * the catalog's source contract is what makes the layer's row appear as
 * configured, and because a caller that only wants the current picture (a
 * scene seek, a test) should not have to hold a subscription to get it.
 *
 * Portable: no Cesium, no DOM. The socket constructor is injected so the
 * lifecycle can be exercised offline against a mock.
 *
 * The socket carries more than the two drawable frame kinds. A scenario run
 * announces itself with `scenario_state`, and the agent pipeline narrates
 * itself with `candidate`, `hazard`, `proposal`, `verdict`, `compile` and
 * `ack` frames, while `assist` and `counters` carry the remote-assist
 * requests and the fleet tallies.
 *
 * Those are held here as a RUN TRANSCRIPT: the agent rail shows what each
 * stage actually produced, so the reasoning has to survive the frame that
 * carried it. The transcript is bounded per kind and reset whenever a new run
 * starts, so a long session cannot grow it without limit and a second replay
 * never shows the first one's reasoning. Arrival times are recorded with each
 * record, because "how long from the first public signal to a signed
 * advisory" is a question about when frames ARRIVED and not about the
 * timestamps inside them.
 *
 * Two ownership rules keep the single-socket invariant under late events:
 * every handler re-checks that the module still owns THAT socket object
 * before acting, and a socket is only cleared from the slot when the slot
 * still holds it. A frame from a replaced socket is discarded, never merged.
 *
 * The endpoint is a LADDER, not a setting: the real API is asked first and
 * the mock feed is the fallback, so the page works the same whether or not
 * `hu api` is up. Exactly one socket is open at a time either way — the walk
 * along the list is sequential, never a race between two candidates — and the
 * snapshot says which one answered, because a synthetic feed has to say so.
 * @module layers/headsup/source
 */

// A portable package (no i18n): the message stays English data and the layer
// panel shows it translated.
const msg = (text) => text;

/** The viewer socket the real Heads Up API serves (`hu api`, port 8765). */
export const HEADSUP_VIEWER_ENDPOINT = 'ws://localhost:8765/viewer';
/**
 * The mock feed's viewer socket (`viz/mock_stream.py`, port 8766).
 *
 * A FALLBACK, not an alternative: the viewer asks the real API first and only
 * drops to the mock when nothing answers there, so a developer with the API
 * running never has to remember to point the page somewhere else, and one
 * without it still has a moving fleet to build against. Which one answered is
 * carried in the snapshot, because a synthetic feed must say so on screen.
 */
export const HEADSUP_MOCK_VIEWER_ENDPOINT = 'ws://localhost:8766/viewer';

/** Reconnect ladder, in milliseconds; the last value repeats. */
const RECONNECT_DELAYS_MS = Object.freeze([1_000, 2_000, 4_000, 8_000, 15_000]);
/**
 * How long to wait before trying the NEXT endpoint rather than this one again.
 *
 * Short on purpose: the ladder above paces retries against a service that is
 * down, while this paces a walk along a list of services that may not be, and
 * making someone wait a second per candidate to find the one that is up would
 * be pacing for its own sake.
 */
const NEXT_ENDPOINT_DELAY_MS = 250;
/** A frame larger than this is not one tick of one city. */
const MAX_FRAME_BYTES = 4 * 1024 * 1024;
/** Advisories held at once; past this the oldest-arriving are dropped. */
const MAX_ADVISORIES = 512;
/** Distinct hazards and acknowledging operators remembered per run. */
const MAX_RUN_HAZARDS = 512;
const MAX_RUN_ACKS = 64;
/**
 * Reasoning records kept per kind, per run.
 *
 * The rail shows the latest of each stage and offers the earlier ones in its
 * card, so a handful is what a person can read. Over the bound the OLDEST
 * goes: a replay that revised a proposal nine times is still explainable by
 * its last few rounds, and the first one stopped mattering.
 */
const MAX_RUN_RECORDS = 16;

/**
 * Subscribe to the Heads Up viewer socket.
 * @param {object} [options]
 * @param {string} [options.endpoint] Socket URL, tried first.
 * @param {string|null} [options.fallbackEndpoint] Socket URL tried when the
 *   first one does not answer; pass null for no fallback.
 * @param {(url: string) => object} [options.socketFactory] Socket
 *   constructor seam; defaults to the platform WebSocket.
 * @param {() => number} [options.now] Clock seam, for tests.
 * @param {(handler: () => void, delayMs: number) => unknown} [options.setTimer]
 *   Timer seam, for tests.
 * @param {(handle: unknown) => void} [options.clearTimer] Timer cancel seam.
 * @returns {object} Source with connect, close, subscribe, getSnapshot and
 *   getStatus.
 */
export function createHeadsupViewerSource({
  endpoint = HEADSUP_VIEWER_ENDPOINT,
  fallbackEndpoint = HEADSUP_MOCK_VIEWER_ENDPOINT,
  socketFactory = (url) => new globalThis.WebSocket(url),
  now = () => Date.now(),
  setTimer = (handler, delayMs) => setTimeout(handler, delayMs),
  clearTimer = (handle) => clearTimeout(handle),
} = {}) {
  /**
   * The endpoints to try, in order, with duplicates and blanks dropped: a
   * fallback that is the same URL as the primary is not a fallback.
   * @type {string[]}
   */
  const endpoints = [...new Set([endpoint, fallbackEndpoint].filter(Boolean))];
  /** Which endpoint the next socket opens against. */
  let candidate = 0;
  /** @type {object|null} */
  let socket = null;
  let wanted = false;
  let attempt = 0;
  /** @type {unknown} */
  let reconnectTimer = null;
  let vehicles = [];
  let vehiclesAt = null;
  /** @type {Map<string, object>} */
  const advisories = new Map();
  let lastFrameAt = null;
  let lastError = null;
  let rejectedFrames = 0;
  /** @type {object|null} The latest accepted `scenario_state`. */
  let scenario = null;
  /** @type {Set<string>} Distinct hazard ids fused during this run. */
  const runHazards = new Set();
  /**
   * @type {Map<string, {operator: string, vehicles: number, status: string,
   *   seq: number}>}
   */
  const runAcks = new Map();
  const emptyTallies = () => ({
    candidate: 0,
    hazard: 0,
    proposal: 0,
    verdict: 0,
    compile: 0,
    advisory: 0,
    ack: 0,
  });
  let runTallies = emptyTallies();
  /**
   * What each stage of the agent pipeline produced during this run.
   *
   * Arrays in arrival order, oldest first, so "the latest" is the last entry
   * and the rail can also show the rounds that came before it.
   */
  const emptyRecords = () => ({
    candidates: [],
    hazards: [],
    proposals: [],
    verdicts: [],
    compiles: [],
  });
  let runRecords = emptyRecords();
  /** @type {Array<object>} Every assist request open right now. */
  let runAssists = [];
  /** @type {object|null} The latest fleet tallies the simulator reported. */
  let runCounters = null;
  /**
   * When the run's first public signal arrived, and when its first advisory
   * did. The two together are the only honest basis for "time to advisory":
   * both are instants THIS page observed, so the number cannot be inflated
   * or flattered by a publisher's own clock.
   */
  let firstSignalAt = null;
  let advisoryAt = null;

  /** Append one record, dropping the oldest once the bound is reached. */
  const remember = (list, record) => {
    list.push(record);
    if (list.length > MAX_RUN_RECORDS)
      list.splice(0, list.length - MAX_RUN_RECORDS);
  };

  /**
   * Note the run's first public signal.
   *
   * A candidate is the usual first frame, but a run whose sentinel output
   * was missed — a page that connected late, a feed that fuses without
   * publishing a candidate — still has a first signal, so the first frame of
   * either kind sets it.
   */
  const noteSignal = (at) => {
    if (firstSignalAt == null) firstSignalAt = at;
  };

  /** Start a fresh run. A new replay never inherits the last one's. */
  const resetRun = () => {
    runHazards.clear();
    runAcks.clear();
    runTallies = emptyTallies();
    runRecords = emptyRecords();
    runAssists = [];
    runCounters = null;
    firstSignalAt = null;
    advisoryAt = null;
  };
  /** @type {Set<(snapshot: object) => void>} */
  const listeners = new Set();

  const snapshot = () => ({
    /** The endpoint this picture came from, which may be the fallback. */
    endpoint: endpoints[candidate] ?? endpoint,
    primaryEndpoint: endpoints[0] ?? endpoint,
    fallback: candidate > 0,
    connected: socket?.readyState === 1,
    vehicles,
    vehiclesAt,
    advisories: [...advisories.values()],
    scenario,
    pipeline: {
      tallies: { ...runTallies },
      /** Distinct hazards fused this run, however many frames said so. */
      hazardCount: runHazards.size,
      acks: [...runAcks.values()],
      candidates: runRecords.candidates,
      hazards: runRecords.hazards,
      proposals: runRecords.proposals,
      verdicts: runRecords.verdicts,
      compiles: runRecords.compiles,
      assists: runAssists,
      counters: runCounters,
      firstSignalAt,
      advisoryAt,
    },
    lastFrameAt,
    lastError,
    rejectedFrames,
  });

  const publish = () => {
    const current = snapshot();
    // One listener's throw must not cost the others their frame.
    for (const listener of [...listeners]) {
      try {
        listener(current);
      } catch (error) {
        console.warn('[Data:HeadsUp] Listener failed:', error);
      }
    }
  };

  const reject = (message) => {
    rejectedFrames++;
    lastError = message;
  };

  const ingest = (data) => {
    if (typeof data !== 'string' || data.length > MAX_FRAME_BYTES) {
      reject(msg('Malformed Heads Up frame'));
      return false;
    }
    let payload;
    try {
      payload = JSON.parse(data);
    } catch {
      reject(msg('Malformed Heads Up frame'));
      return false;
    }
    if (payload?.type === 'telemetry') {
      const rows = normalizeTelemetryFrame(payload);
      if (!rows) {
        reject(msg('Malformed Heads Up frame'));
        return false;
      }
      vehicles = rows;
      vehiclesAt = now();
    } else if (payload?.type === 'advisory') {
      const row = normalizeAdvisoryFeature(payload.feature);
      if (!row) {
        reject(msg('Malformed Heads Up frame'));
        return false;
      }
      // Re-insert on update so the eviction order stays arrival order.
      advisories.delete(row.id);
      advisories.set(row.id, row);
      while (advisories.size > MAX_ADVISORIES) {
        const oldest = advisories.keys().next();
        if (oldest.done) break;
        advisories.delete(oldest.value);
      }
      runTallies.advisory++;
      noteSignal(now());
      if (advisoryAt == null) advisoryAt = now();
    } else if (payload?.type === 'scenario_state') {
      const state = normalizeScenarioStateFrame(payload);
      if (!state) {
        reject(msg('Malformed Heads Up frame'));
        return false;
      }
      // A different scenario, or a transition in or out of a run, is a new
      // run: everything the previous one tallied stops counting here.
      if (state.id !== scenario?.id || state.state !== scenario?.state)
        resetRun();
      scenario = state;
    } else if (payload?.type === 'hazard') {
      const hazard = normalizeHazardFrame(payload);
      if (!hazard) {
        reject(msg('Malformed Heads Up frame'));
        return false;
      }
      if (runHazards.size < MAX_RUN_HAZARDS) runHazards.add(hazard.id);
      runTallies.hazard++;
      noteSignal(now());
      remember(runRecords.hazards, { ...hazard, at: now() });
    } else if (payload?.type === 'candidate') {
      const candidate = normalizeCandidateFrame(payload);
      if (!candidate) {
        reject(msg('Malformed Heads Up frame'));
        return false;
      }
      runTallies.candidate++;
      noteSignal(now());
      remember(runRecords.candidates, { ...candidate, at: now() });
    } else if (payload?.type === 'proposal') {
      const proposal = normalizeProposalFrame(payload);
      if (!proposal) {
        reject(msg('Malformed Heads Up frame'));
        return false;
      }
      runTallies.proposal++;
      remember(runRecords.proposals, { ...proposal, at: now() });
    } else if (payload?.type === 'verdict') {
      const verdict = normalizeVerdictFrame(payload);
      if (!verdict) {
        reject(msg('Malformed Heads Up frame'));
        return false;
      }
      runTallies.verdict++;
      remember(runRecords.verdicts, { ...verdict, at: now() });
    } else if (payload?.type === 'compile') {
      const result = normalizeCompileFrame(payload);
      if (!result) {
        reject(msg('Malformed Heads Up frame'));
        return false;
      }
      runTallies.compile++;
      // Keyed by nothing: a compiler that answers twice about one proposal
      // has changed its answer (held for approval, then signed), and both
      // rounds belong in the card. The rail reads the LATEST per proposal.
      remember(runRecords.compiles, { ...result, at: now() });
    } else if (payload?.type === 'assist') {
      const assists = normalizeAssistFrame(payload);
      if (!assists) {
        reject(msg('Malformed Heads Up frame'));
        return false;
      }
      // Whole-state, like telemetry: an assist a human cleared leaves.
      runAssists = assists;
    } else if (payload?.type === 'counters') {
      const counters = normalizeCountersFrame(payload);
      if (!counters) {
        reject(msg('Malformed Heads Up frame'));
        return false;
      }
      runCounters = counters;
    } else if (payload?.type === 'ack') {
      const ack = normalizeAckFrame(payload);
      if (!ack) {
        reject(msg('Malformed Heads Up frame'));
        return false;
      }
      runTallies.ack++;
      // Keyed by operator: a fleet that acknowledges twice has still
      // answered once, and the strip counts operators, not messages.
      //
      // `seq` is what makes a SECOND answer from the same fleet an event
      // rather than an identical row. The layer flashes that fleet once per
      // answer, and it can only tell two answers apart if the entry moves.
      if (runAcks.has(ack.operator) || runAcks.size < MAX_RUN_ACKS)
        runAcks.set(ack.operator, {
          ...ack,
          seq: runTallies.ack,
          at: now(),
        });
    } else {
      // An unrecognized frame kind is a newer publisher talking, not a
      // defect: ignore it without disturbing what is drawn or the ladder.
      return false;
    }
    lastFrameAt = now();
    lastError = null;
    attempt = 0;
    return true;
  };

  const ladderDelay = () => {
    const delay =
      RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)];
    attempt++;
    return delay;
  };

  /**
   * Try again, either against this endpoint or against the next one.
   *
   * `advance` is set when the socket never OPENED: nothing is listening
   * there, so the next candidate is worth a quick try. A socket that opened
   * and later dropped keeps its endpoint for the first retry — it was serving
   * a moment ago, and moving a working page onto the mock because the API
   * restarted would be a worse answer than waiting for it. Only when that
   * retry also fails to open does the walk continue, and a full lap of the
   * list hands the pacing back to the ladder.
   * @param {{advance?: boolean}} [options]
   */
  const scheduleReconnect = ({ advance = false } = {}) => {
    if (!wanted || reconnectTimer != null || socket) return;
    let delay;
    if (advance && endpoints.length > 1) {
      candidate = (candidate + 1) % endpoints.length;
      delay = candidate === 0 ? ladderDelay() : NEXT_ENDPOINT_DELAY_MS;
    } else delay = ladderDelay();
    reconnectTimer = setTimer(() => {
      reconnectTimer = null;
      open();
    }, delay);
  };

  function open() {
    if (socket || !wanted) return;
    const url = endpoints[candidate] ?? endpoint;
    let pending;
    try {
      pending = socketFactory(url);
    } catch (error) {
      lastError = error?.message || msg('Heads Up feed unavailable');
      scheduleReconnect({ advance: true });
      publish();
      return;
    }
    socket = pending;
    // Whether THIS socket ever reached the open state, which is what tells a
    // dead endpoint apart from a service that went away mid-stream.
    let opened = false;
    pending.onopen = () => {
      if (socket !== pending) return;
      opened = true;
      attempt = 0;
      lastError = null;
      publish();
    };
    pending.onmessage = (event) => {
      if (socket !== pending) return;
      if (ingest(event?.data)) publish();
    };
    const drop = () => {
      // Identity-checked: a late close from a replaced socket must not clear
      // its successor out of the slot.
      if (socket !== pending) return;
      socket = null;
      if (wanted) lastError = lastError ?? msg('Heads Up feed unavailable');
      scheduleReconnect({ advance: !opened });
      publish();
    };
    pending.onerror = () => {
      if (socket !== pending) return;
      lastError = msg('Heads Up feed unavailable');
    };
    pending.onclose = drop;
  }

  return {
    /** The endpoint a socket would open against now. */
    get endpoint() {
      return endpoints[candidate] ?? endpoint;
    },
    /** Every endpoint this source will try, in order. */
    endpoints: Object.freeze([...endpoints]),

    /** Open the subscription. Idempotent. */
    connect() {
      wanted = true;
      open();
    },

    /** Close the subscription and drop the fleet picture it was feeding. */
    close() {
      wanted = false;
      attempt = 0;
      // A later `connect()` is a fresh start, so it asks the real API first
      // again rather than inheriting a fallback this session once needed.
      candidate = 0;
      if (reconnectTimer != null) {
        clearTimer(reconnectTimer);
        reconnectTimer = null;
      }
      const closing = socket;
      socket = null;
      if (closing) {
        closing.onopen = null;
        closing.onmessage = null;
        closing.onerror = null;
        closing.onclose = null;
        try {
          closing.close();
        } catch (error) {
          console.warn('[Data:HeadsUp] Socket close failed:', error);
        }
      }
      // Vehicle positions are only true for their tick: keeping them across a
      // disconnect would draw a fleet that is no longer reporting. Advisories
      // carry their own validity window and are kept until it ends.
      vehicles = [];
      vehiclesAt = null;
    },

    /**
     * Forget the current picture without touching the subscription.
     *
     * What a scenario published stops being true the moment the scenario
     * ends: its advisories are not in force over the landing view, and its
     * run tally is not the next run's. Advisories survive a RECONNECT on
     * purpose — they carry their own validity window — so dropping them has
     * to be an explicit act, which is what this is.
     */
    reset() {
      advisories.clear();
      vehicles = [];
      vehiclesAt = null;
      scenario = null;
      resetRun();
      publish();
    },

    /**
     * Watch every accepted frame.
     * @param {(snapshot: object) => void} listener
     * @returns {() => void} Unsubscribe.
     */
    subscribe(listener) {
      if (typeof listener !== 'function')
        throw new TypeError('A Heads Up subscriber must be a function');
      listeners.add(listener);
      return () => listeners.delete(listener);
    },

    /**
     * The current picture, without holding a subscription.
     * @param {object} [options]
     * @param {AbortSignal} [options.signal]
     * @returns {Promise<object>}
     */
    async getSnapshot({ signal } = {}) {
      signal?.throwIfAborted();
      return snapshot();
    },

    getStatus() {
      return snapshot();
    },
  };
}
