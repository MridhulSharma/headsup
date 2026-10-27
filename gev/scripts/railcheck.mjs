/**
 * Replay the server's real frames through the viewer and print the rail.
 *
 * Not a test -- a demo rehearsal. It drives the same path the running page
 * drives: the socket source ingests each frame, the snapshot comes out, and
 * `buildPipelineView` turns it into the rows the rail renders. A row that
 * says `[   ]` here is a row that will be dark on screen.
 *
 * Run from gev/: node scripts/railcheck.mjs
 */

import { readFileSync } from 'node:fs';

import { buildPipelineView } from '../src/headsup/pipeline.js';
import { createHeadsupViewerSource } from '../src/layers/headsup/source.js';

const FRAMES = JSON.parse(
  readFileSync(
    new URL(
      '../src/layers/headsup/__fixtures__/api-frames.json',
      import.meta.url,
    ),
    'utf8',
  ),
);

// A fake socket driven by hand. The source hangs `onmessage` on whatever
// `socketFactory` returns, so this is the same path a live frame takes.
let live = null;
const source = createHeadsupViewerSource({
  socketFactory: () => {
    live = { readyState: 1, close() {}, send() {} };
    return live;
  },
  setTimer: () => null,
  clearTimer: () => {},
});

if (typeof source.start === 'function') source.start();
else if (typeof source.connect === 'function') source.connect();
else source.subscribe(() => {});

if (!live) {
  console.error('No socket was created, so the source cannot be driven.');
  process.exit(2);
}
live.onopen?.();

const snap = () => source.getStatus();
const send = (frame) => {
  const before = snap()?.rejectedFrames ?? 0;
  live.onmessage?.({ data: JSON.stringify(frame) });
  return (snap()?.rejectedFrames ?? 0) === before;
};

const fleet = (ids, op) =>
  ids.map((n, i) => ({
    id: `${op}-${n}`,
    op,
    lon: -97.74 + i * 0.001,
    lat: 30.27,
    heading: 90,
  }));

// The scenario, then the pipeline in the order a live run produces it.
const order = [
  { type: 'telemetry', tick: 1, vehicles: fleet([1, 2, 3], 'op_a') },
  { type: 'ghost_telemetry', tick: 1, vehicles: fleet([1, 2, 3], 'ghost') },
  { type: 'scenario_state', id: 's2_signal', state: 'running', label: 'S2' },
  FRAMES.candidate,
  FRAMES.hazard,
  FRAMES.proposal,
  FRAMES.verdict,
  FRAMES.advisory,
  FRAMES.ack,
  FRAMES.assist,
  FRAMES.counters,
];

let accepted = 0;
let rejected = 0;
for (const frame of order) {
  if (send(frame)) accepted++;
  else {
    rejected++;
    console.log(`  REJECTED  ${frame.type}`);
  }
}

const view = buildPipelineView({
  snapshot: snap(),
  stats: source.getStats?.() ?? {},
});

console.log(`\nframes accepted ${accepted}, rejected ${rejected}\n`);
console.log('AGENT PIPELINE');
for (const stage of view.stages)
  console.log(
    `  ${stage.lit ? '[LIT]' : '[   ]'} ` +
      `${stage.key.toUpperCase().padEnd(9)} ${stage.value ?? ''}`,
  );

const rows = snap()?.vehicles ?? [];
console.log('FLEETS');
console.log(`  live  ${rows.filter((v) => !v.ghost).length} vehicles`);
console.log(`  ghost ${rows.filter((v) => v.ghost).length} vehicles`);

console.log('\nOPERATORS');
if (!view.operators.length) console.log('  (no fleet has answered)');
for (const op of view.operators)
  console.log(`  ${op.op}  ${op.status}  ${op.vehicles} vehicles`);

console.log('\nSTATUS');
const c = view.counters;
const pair = (v) => (v ? `live ${v.live} / ghost ${v.ghost}` : 'not reported');
console.log(`  hazard entries    ${pair(c.hazardEntries)}`);
console.log(`  assist queue      ${pair(c.assistQueue)}`);
console.log(`  time to advisory  ${c.timeToAdvisoryMs ?? 'not reported'} ms`);
console.log(`  hazard            ${view.hazardId ?? '-'}`);
console.log(`  advisory          ${view.advisoryId ?? '-'}`);
console.log(`  pending approval  ${view.pending ? 'yes' : 'none'}`);

const ghostSeen = rows.filter((v) => v.ghost).length;
if (!ghostSeen) console.log('\nGHOST COHORT MISSING: the control fleet was dropped.');

const dark = view.stages.filter((s) => !s.lit).map((s) => s.key);
console.log(
  dark.length ? `\nDARK ROWS: ${dark.join(', ')}` : '\nEvery stage lit.',
);
process.exit(dark.length || rejected || !ghostSeen ? 1 : 0);
