/**
 * Advisory validity-window maths against a supplied clock.
 *
 * Kept out of `records.js` so scene seeking can ask the same question at an
 * arbitrary instant: normalization decides whether an advisory is WELL FORMED,
 * this module decides whether it is IN FORCE at time t. Pure, no clock of its
 * own — a layer passes `Date.now()`, a seek passes the shot's instant.
 * @module layers/headsup/expiry
 */

/** The three phases of one advisory's window, in order. */
export const ADVISORY_PHASES = Object.freeze(['pending', 'live', 'expired']);

/**
 * Which phase an advisory is in at `nowMs`.
 *
 * Both bounds are inclusive of the live phase: an advisory whose window is a
 * single instant is live at that instant rather than never.
 * @param {{validFrom: number, validUntil: number}} row Normalized advisory.
 * @param {number} nowMs Epoch milliseconds.
 * @returns {'pending'|'live'|'expired'|null} null when the row has no window.
 */
export function advisoryPhase(row, nowMs) {
  const from = Number(row?.validFrom);
  const until = Number(row?.validUntil);
  if (!Number.isFinite(from) || !Number.isFinite(until)) return null;
  if (!Number.isFinite(nowMs)) return null;
  if (nowMs < from) return 'pending';
  if (nowMs > until) return 'expired';
  return 'live';
}

/**
 * Whether an advisory is in force at `nowMs`.
 * @param {{validFrom: number, validUntil: number}} row Normalized advisory.
 * @param {number} nowMs Epoch milliseconds.
 * @returns {boolean}
 */
export function advisoryIsLive(row, nowMs) {
  return advisoryPhase(row, nowMs) === 'live';
}

/**
 * Partition advisories by phase, preserving input order within each phase.
 *
 * A row with no usable window lands in `rejected` rather than in any phase:
 * an advisory whose validity cannot be read is not thereby in force.
 * @param {Iterable<object>} rows Normalized advisories.
 * @param {number} nowMs Epoch milliseconds.
 * @returns {{live: object[], pending: object[], expired: object[],
 *   rejected: object[]}}
 */
export function partitionAdvisories(rows, nowMs) {
  const groups = { live: [], pending: [], expired: [], rejected: [] };
  for (const row of rows ?? []) {
    const phase = advisoryPhase(row, nowMs);
    groups[phase ?? 'rejected'].push(row);
  }
  return groups;
}

/**
 * Milliseconds until this advisory's phase changes, for a refresh deadline.
 * @param {{validFrom: number, validUntil: number}} row Normalized advisory.
 * @param {number} nowMs Epoch milliseconds.
 * @returns {number|null} Remaining ms, or null once nothing further changes.
 */
export function msUntilPhaseChange(row, nowMs) {
  const phase = advisoryPhase(row, nowMs);
  if (phase === 'pending') return Number(row.validFrom) - nowMs;
  // One millisecond past the inclusive end is the first expired instant.
  if (phase === 'live') return Number(row.validUntil) - nowMs + 1;
  return null;
}
