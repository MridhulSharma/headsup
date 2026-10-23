/**
 * The agent pipeline, as rows a rail can draw.
 *
 * Pure: a feed snapshot and the layer's stats in, a frozen view out. No DOM,
 * no Cesium, no clock of its own — `src/headsup/console.js` renders what this
 * returns, and every number here traces to a frame that actually arrived.
 *
 * Three rules shaped it, and all three are about not lying on a wall display:
 *
 * - **A stage lights from its own event, never from a timer.** `lit` is true
 *   because a record exists, and `at` is when the FIRST one arrived, so a
 *   stage lights once and stays lit. Nothing here animates, because an
 *   animated pipeline would show progress a replay had not made.
 * - **Nothing is inferred from a stage's neighbours.** A compiler that never
 *   answered leaves its row unlit even when an advisory is on the map,
 *   because "an advisory exists, so the compiler must have signed it" is
 *   exactly the kind of reasoning an audit trail is supposed to replace.
 * - **An answer this build cannot read is never an approval.** The records
 *   module already maps an unknown critic decision to `ESCALATE` and an
 *   unknown compiler status to `REJECTED`; this module adds no softer
 *   reading on top of that.
 *
 * `linkedId` on each stage is what ties a row to the hazard on the globe: the
 * hazard id a stage's record names, or the advisory id once one exists.
 * @module headsup/pipeline
 */

import { msg } from '../i18n/index.js';

/**
 * How a row reads at a glance, before anybody clicks it.
 *
 * Four tones, not a severity scale: `wait` is a pipeline that stopped and is
 * waiting for a person, which is a different thing from `alert`, a pipeline
 * that produced a refusal. Conflating them would hide the one state a human
 * can actually act on.
 */
export const TONES = Object.freeze({
  idle: 'idle',
  ok: 'ok',
  wait: 'wait',
  alert: 'alert',
});

/**
 * The stages, in the order a signal passes through them.
 *
 * `Feeds` is a stage rather than a caption because it answers the first
 * question anybody asks of a civic advisory — which public feed is this
 * resting on — and the answer is a set of chips, not one line of text.
 */
export const PIPELINE_STAGES = Object.freeze([
  Object.freeze({ key: 'feeds', label: msg('Feeds') }),
  Object.freeze({ key: 'sentinel', label: msg('Sentinels') }),
  Object.freeze({ key: 'fusion', label: msg('Fusion') }),
  Object.freeze({ key: 'proposer', label: msg('Proposer') }),
  Object.freeze({ key: 'critic', label: msg('Critic') }),
  Object.freeze({ key: 'compiler', label: msg('Compiler') }),
]);

/**
 * Display names for the public feeds Heads Up ingests.
 *
 * A source id this build does not know is shown AS IT ARRIVED. It is feed
 * text, so it is displayed and never interpolated into markup — and a chip
 * reading `travis_county_oem` is a better answer than a chip reading
 * `Unknown`, which would hide which feed an advisory actually rests on.
 */
export const FEED_LABELS = Object.freeze({
  nws: msg('NWS'),
  usgs: msg('USGS'),
  austin_signals: msg('Austin signals'),
  austin_incidents: msg('Austin incidents'),
  wzdx: msg('WZDx'),
});

/** The critic's three answers, as one short word each. */
export const VERDICT_TONES = Object.freeze({
  APPROVE: TONES.ok,
  REVISE: TONES.wait,
  ESCALATE: TONES.wait,
});

/** The compiler's three answers. */
export const COMPILE_TONES = Object.freeze({
  SIGNED: TONES.ok,
  PENDING_APPROVAL: TONES.wait,
  REJECTED: TONES.alert,
});

/** What an operator answered. */
export const ACK_TONES = Object.freeze({
  ACCEPTED: TONES.ok,
  DELAYED: TONES.wait,
  DECLINED: TONES.alert,
});

/** How many assist-queue samples the counter strip's sparkline keeps. */
export const SPARKLINE_SAMPLES = 24;
/** How often a sample is taken. A queue depth is not a per-frame quantity. */
export const SPARKLINE_SAMPLE_MS = 1_000;

/**
 * One record list out of a snapshot, or an empty one.
 *
 * Checked rather than defaulted with `??`, because the rail is rendered from
 * data the layer hands over: a field that is present but is not a list is a
 * snapshot from a publisher this build does not agree with, and the rail's
 * job then is to show nothing rather than to take the render loop down with
 * it. An empty rail is readable; a dead one is not.
 */
const rows = (value) => (Array.isArray(value) ? value : []);
const last = (list) => (list.length ? list[list.length - 1] : null);
const first = (list) => (list.length ? list[0] : null);

/**
 * The public feeds this run's evidence actually cites.
 *
 * Read off the evidence rows of the candidates and the fused hazards, so a
 * chip is lit because a frame named that source and never because this
 * module expected it to. Sorted by first arrival: the order the signals came
 * in is information, and an alphabetical list would throw it away.
 * @param {object} pipeline The snapshot's pipeline transcript.
 * @returns {Array<{source: string, label: string|null, events: number,
 *   firstAt: number|null, lastAt: number|null}>}
 */
export function feedChips(pipeline) {
  const chips = new Map();
  const walk = (records) => {
    for (const record of rows(records))
      for (const row of rows(record?.evidence)) {
        const known = chips.get(row.source);
        const at = record.at ?? null;
        if (known) {
          known.events++;
          if (at != null) known.lastAt = at;
        } else
          chips.set(row.source, {
            source: row.source,
            // Null when this build has no name for it; the surface then
            // shows the id as it arrived.
            label: FEED_LABELS[row.source] ?? null,
            events: 1,
            firstAt: at,
            lastAt: at,
          });
      }
  };
  walk(pipeline?.candidates);
  walk(pipeline?.hazards);
  return [...chips.values()].sort(
    (a, b) => (a.firstAt ?? 0) - (b.firstAt ?? 0) || a.source.localeCompare(b.source),
  );
}

/**
 * The latest compiler answer per proposal.
 *
 * The compiler answers twice about a two-key advisory — held, then signed —
 * so the rail has to read the last word rather than the first.
 * @param {Array<object>} compiles
 * @returns {Map<string, object>}
 */
export function latestCompiles(compiles) {
  const byProposal = new Map();
  for (const record of rows(compiles))
    byProposal.set(record.proposalId, record);
  return byProposal;
}

/**
 * The advisory waiting on a second key, if one is.
 *
 * Only ever a proposal whose LATEST compiler answer is `PENDING_APPROVAL`
 * and which says `two_key`: an approval button for an advisory that has
 * already been signed, or that was rejected on policy, would offer a person
 * a decision that is not theirs to make any more.
 * @param {object} pipeline
 * @returns {{proposalId: string, reasons: string[], at: number|null}|null}
 */
export function pendingApproval(pipeline) {
  let pending = null;
  for (const record of latestCompiles(pipeline?.compiles).values()) {
    if (record.status !== 'PENDING_APPROVAL' || !record.twoKey) continue;
    // The most recent one, when a run somehow holds two.
    if (!pending || (record.at ?? 0) >= (pending.at ?? 0))
      pending = {
        proposalId: record.proposalId,
        reasons: record.reasons,
        at: record.at ?? null,
      };
  }
  return pending;
}

/**
 * From the run's first public signal to its first advisory, in milliseconds.
 *
 * Both instants are ones THIS page observed, so the number is a measurement
 * rather than a claim copied off a publisher's clock. Null until an advisory
 * has actually arrived — a pipeline still running has no answer yet, and
 * showing a partial elapsed time would read as one.
 * @param {object} pipeline
 * @returns {number|null}
 */
export function timeToAdvisoryMs(pipeline) {
  const from = pipeline?.firstSignalAt;
  const to = pipeline?.advisoryAt;
  if (!Number.isFinite(from) || !Number.isFinite(to) || to < from) return null;
  return to - from;
}

/** One stage row, with its record set attached for the card. */
function stage(key, { lit, tone, value, firstAt, linkedId, records, latest }) {
  return Object.freeze({
    key,
    lit: lit === true,
    tone: lit ? tone : TONES.idle,
    /** The one short string the rail row shows beside the stage name. */
    value: lit ? (value ?? null) : null,
    /** When this stage FIRST produced something, which is when it lit. */
    firstAt: firstAt ?? null,
    /** The hazard or advisory this stage's work belongs to. */
    linkedId: linkedId ?? null,
    /** Everything the stage produced, newest last — the card's contents. */
    records: Object.freeze([...rows(records)]),
    latest: latest ?? null,
  });
}

/**
 * Turn one feed snapshot into the rail's rows.
 * @param {object} [options]
 * @param {object|null} [options.snapshot] The layer's feed snapshot.
 * @param {object} [options.stats] The layer's `getStats()`.
 * @returns {object} The frozen rail view.
 */
export function buildPipelineView({ snapshot = null, stats = {} } = {}) {
  const pipeline = snapshot?.pipeline ?? null;
  const candidates = rows(pipeline?.candidates);
  const hazards = rows(pipeline?.hazards);
  const proposals = rows(pipeline?.proposals);
  const verdicts = rows(pipeline?.verdicts);
  const compiles = rows(pipeline?.compiles);
  const acks = rows(pipeline?.acks);
  const chips = feedChips(pipeline);
  const latestHazard = last(hazards);
  const latestCandidate = last(candidates);
  const latestProposal = last(proposals);
  const latestVerdict = last(verdicts);
  const latestCompile = last(compiles);
  // Which hazard this run is about: the fused one when there is one, and the
  // sentinel's own reading before fusion has answered.
  const hazardId = latestHazard?.id ?? latestProposal?.hazardId ?? null;
  const advisoryId =
    [...latestCompiles(compiles).values()].find(
      (record) => record.status === 'SIGNED' && record.advisoryId,
    )?.advisoryId ?? null;

  const stages = [
    stage('feeds', {
      lit: chips.length > 0,
      tone: TONES.ok,
      value: chips.length ? String(chips.length) : null,
      firstAt: first(chips)?.firstAt ?? null,
      linkedId: hazardId,
      records: chips,
      latest: null,
    }),
    stage('sentinel', {
      lit: candidates.length > 0,
      tone: TONES.ok,
      value: latestCandidate?.hazardClass ?? null,
      firstAt: first(candidates)?.at ?? null,
      linkedId: latestCandidate?.id ?? hazardId,
      records: candidates,
      latest: latestCandidate,
    }),
    stage('fusion', {
      lit: hazards.length > 0,
      tone: TONES.ok,
      // Confidence is the one number fusion exists to produce, so it is what
      // the row carries rather than the hazard class the stage above said.
      value:
        latestHazard?.confidence == null
          ? null
          : `${Math.round(latestHazard.confidence * 100)}%`,
      firstAt: first(hazards)?.at ?? null,
      linkedId: latestHazard?.id ?? null,
      records: hazards,
      latest: latestHazard,
    }),
    stage('proposer', {
      lit: proposals.length > 0,
      tone: TONES.ok,
      value: latestProposal?.action ?? null,
      firstAt: first(proposals)?.at ?? null,
      linkedId: latestProposal?.hazardId ?? hazardId,
      records: proposals,
      latest: latestProposal,
    }),
    stage('critic', {
      lit: verdicts.length > 0,
      tone: VERDICT_TONES[latestVerdict?.decision] ?? TONES.wait,
      value: latestVerdict?.decision ?? null,
      firstAt: first(verdicts)?.at ?? null,
      linkedId: latestVerdict?.proposalId ?? null,
      records: verdicts,
      latest: latestVerdict,
    }),
    stage('compiler', {
      lit: compiles.length > 0,
      tone: COMPILE_TONES[latestCompile?.status] ?? TONES.alert,
      value: latestCompile?.status ?? null,
      firstAt: first(compiles)?.at ?? null,
      linkedId: latestCompile?.advisoryId ?? latestCompile?.proposalId ?? null,
      records: compiles,
      latest: latestCompile,
    }),
  ];

  // One row per operator that answered, in arrival order: the order fleets
  // came back in is what an operator watching a publication is reading.
  const operators = [...acks]
    .sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
    .map((ack) =>
      Object.freeze({
        op: ack.operator,
        status: ack.status,
        tone: ACK_TONES[ack.status] ?? TONES.alert,
        vehicles: ack.vehicles ?? 0,
        actionTaken: ack.actionTaken ?? null,
        reason: ack.reason ?? null,
        at: ack.at ?? null,
        linkedId: ack.advisoryId ?? advisoryId,
      }),
    );

  const counters = pipeline?.counters ?? null;
  return Object.freeze({
    hazardId,
    hazardClass: latestHazard?.hazardClass ?? latestCandidate?.hazardClass ?? null,
    advisoryId,
    stages: Object.freeze(stages),
    operators: Object.freeze(operators),
    pending: pendingApproval(pipeline),
    counters: Object.freeze({
      // Null rather than zero when the feed has not reported: "no vehicle
      // entered the hazard" and "nobody has told us" are different claims.
      hazardEntries: counters?.hazardEntries ?? null,
      assistQueue: counters?.assistQueue ?? null,
      // Rerouted is the one counter the viewer can honestly derive: it is a
      // property of the telemetry it is already drawing.
      rerouted: stats?.rerouted ?? 0,
      timeToAdvisoryMs: timeToAdvisoryMs(pipeline),
    }),
  });
}

/**
 * Append one assist-queue sample, at most one per `SPARKLINE_SAMPLE_MS`.
 *
 * In place and bounded, because the strip redraws at the feed's cadence and
 * a sparkline is a window rather than a history. A tick with nothing new to
 * say changes nothing, which is what keeps the line a record of the queue
 * rather than of how often this function was called.
 * @param {Array<{at: number, live: number, ghost: number}>} samples
 * @param {{live: number, ghost: number}|null} queue
 * @param {number} nowMs
 * @returns {Array<object>} `samples`.
 */
export function sampleAssistQueue(samples, queue, nowMs) {
  if (!queue) return samples;
  const newest = samples[samples.length - 1];
  if (newest && nowMs - newest.at < SPARKLINE_SAMPLE_MS) return samples;
  samples.push({ at: nowMs, live: queue.live, ghost: queue.ghost });
  if (samples.length > SPARKLINE_SAMPLES)
    samples.splice(0, samples.length - SPARKLINE_SAMPLES);
  return samples;
}
