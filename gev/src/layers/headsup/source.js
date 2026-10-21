import {
  normalizeAdvisoryFeature,
  normalizeTelemetryFrame,
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
 * Two ownership rules keep the single-socket invariant under late events:
 * every handler re-checks that the module still owns THAT socket object
 * before acting, and a socket is only cleared from the slot when the slot
 * still holds it. A frame from a replaced socket is discarded, never merged.
 * @module layers/headsup/source
 */

// A portable package (no i18n): the message stays English data and the layer
// panel shows it translated.
const msg = (text) => text;

/** The dev viewer socket the fleet simulator and compiler publish to. */
export const HEADSUP_VIEWER_ENDPOINT = 'ws://localhost:8765/viewer';

/** Reconnect ladder, in milliseconds; the last value repeats. */
const RECONNECT_DELAYS_MS = Object.freeze([1_000, 2_000, 4_000, 8_000, 15_000]);
/** A frame larger than this is not one tick of one city. */
const MAX_FRAME_BYTES = 4 * 1024 * 1024;
/** Advisories held at once; past this the oldest-arriving are dropped. */
const MAX_ADVISORIES = 512;

/**
 * Subscribe to the Heads Up viewer socket.
 * @param {object} [options]
 * @param {string} [options.endpoint] Socket URL.
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
  socketFactory = (url) => new globalThis.WebSocket(url),
  now = () => Date.now(),
  setTimer = (handler, delayMs) => setTimeout(handler, delayMs),
  clearTimer = (handle) => clearTimeout(handle),
} = {}) {
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
  /** @type {Set<(snapshot: object) => void>} */
  const listeners = new Set();

  const snapshot = () => ({
    endpoint,
    connected: socket?.readyState === 1,
    vehicles,
    vehiclesAt,
    advisories: [...advisories.values()],
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

  const scheduleReconnect = () => {
    if (!wanted || reconnectTimer != null || socket) return;
    const delay =
      RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)];
    attempt++;
    reconnectTimer = setTimer(() => {
      reconnectTimer = null;
      open();
    }, delay);
  };

  function open() {
    if (socket || !wanted) return;
    let candidate;
    try {
      candidate = socketFactory(endpoint);
    } catch (error) {
      lastError = error?.message || msg('Heads Up feed unavailable');
      scheduleReconnect();
      publish();
      return;
    }
    socket = candidate;
    candidate.onopen = () => {
      if (socket !== candidate) return;
      lastError = null;
      publish();
    };
    candidate.onmessage = (event) => {
      if (socket !== candidate) return;
      if (ingest(event?.data)) publish();
    };
    const drop = () => {
      // Identity-checked: a late close from a replaced socket must not clear
      // its successor out of the slot.
      if (socket !== candidate) return;
      socket = null;
      if (wanted) lastError = lastError ?? msg('Heads Up feed unavailable');
      scheduleReconnect();
      publish();
    };
    candidate.onerror = () => {
      if (socket !== candidate) return;
      lastError = msg('Heads Up feed unavailable');
    };
    candidate.onclose = drop;
  }

  return {
    endpoint,

    /** Open the subscription. Idempotent. */
    connect() {
      wanted = true;
      open();
    },

    /** Close the subscription and drop the fleet picture it was feeding. */
    close() {
      wanted = false;
      attempt = 0;
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
