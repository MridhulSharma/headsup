import assert from 'node:assert/strict';
import test from 'node:test';
import {
  advisoryIsLive,
  advisoryPhase,
  msUntilPhaseChange,
  partitionAdvisories,
} from './expiry.js';

const from = Date.parse('2026-10-21T18:00:00Z');
const until = Date.parse('2026-10-21T20:00:00Z');
const row = Object.freeze({ id: 'adv-1', validFrom: from, validUntil: until });

test('both ends of the window are inside it', () => {
  assert.equal(advisoryPhase(row, from - 1), 'pending');
  assert.equal(advisoryPhase(row, from), 'live');
  assert.equal(advisoryPhase(row, until), 'live');
  assert.equal(advisoryPhase(row, until + 1), 'expired');
});

test('a single-instant window is live at that instant', () => {
  const instant = { validFrom: from, validUntil: from };
  assert.equal(advisoryPhase(instant, from), 'live');
  assert.equal(advisoryPhase(instant, from + 1), 'expired');
});

test('an unreadable window is not thereby in force', () => {
  assert.equal(advisoryPhase({ validFrom: from }, from), null);
  assert.equal(
    advisoryPhase({ validFrom: 'noon', validUntil: until }, from),
    null,
  );
  assert.equal(advisoryPhase(null, from), null);
  assert.equal(advisoryPhase(row, Number.NaN), null);
  assert.equal(advisoryIsLive(null, from), false);
});

test('an advisory expiring mid-scene changes phase on the clock alone', () => {
  assert.equal(advisoryIsLive(row, from + 60_000), true);
  assert.equal(advisoryIsLive(row, until + 60_000), false);
});

test('partitioning keeps input order and quarantines unreadable rows', () => {
  const pending = { id: 'b', validFrom: until, validUntil: until + 1000 };
  const expired = { id: 'c', validFrom: from - 2000, validUntil: from - 1000 };
  const broken = { id: 'd' };
  const groups = partitionAdvisories(
    [row, pending, expired, broken, { ...row, id: 'e' }],
    from,
  );
  assert.deepEqual(
    groups.live.map((entry) => entry.id),
    ['adv-1', 'e'],
  );
  assert.deepEqual(
    groups.pending.map((entry) => entry.id),
    ['b'],
  );
  assert.deepEqual(
    groups.expired.map((entry) => entry.id),
    ['c'],
  );
  assert.deepEqual(
    groups.rejected.map((entry) => entry.id),
    ['d'],
  );
});

test('partitioning an absent set is four empty groups', () => {
  assert.deepEqual(partitionAdvisories(undefined, from), {
    live: [],
    pending: [],
    expired: [],
    rejected: [],
  });
});

test('the next phase change is a deadline a refresh can hold to', () => {
  assert.equal(msUntilPhaseChange(row, from - 5_000), 5_000);
  // One millisecond past the inclusive end is the first expired instant.
  assert.equal(msUntilPhaseChange(row, until), 1);
  assert.equal(msUntilPhaseChange(row, from), until - from + 1);
  assert.equal(msUntilPhaseChange(row, until + 1), null);
  assert.equal(msUntilPhaseChange({ id: 'x' }, from), null);
});
