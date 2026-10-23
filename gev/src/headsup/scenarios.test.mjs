import assert from 'node:assert/strict';
import test from 'node:test';
import {
  ScenarioApiError,
  createScenarioClient,
  normalizeScenario,
  normalizeScenarioControl,
  normalizeScenarioListing,
} from './scenarios.js';

const row = (overrides = {}) => ({
  id: 's1_flood',
  code: 'S1',
  name: 'Flash flood',
  city: 'San Antonio',
  hazard_class: 'FLOOD',
  what_if: 'What if a creek crossing floods under a routed fleet?',
  speed: 60,
  scripted: true,
  start_view: {
    lon: -98.396,
    lat: 29.5228,
    height: 900,
    heading: 20,
    pitch: -34,
  },
  ...overrides,
});

const json = (payload, { ok = true, status = 200 } = {}) => ({
  ok,
  status,
  json: async () => payload,
});

test('a well-formed row becomes a frozen scenario', () => {
  const scenario = normalizeScenario(row());
  assert.deepEqual(scenario, {
    id: 's1_flood',
    code: 'S1',
    name: 'Flash flood',
    city: 'San Antonio',
    hazardClass: 'FLOOD',
    whatIf: 'What if a creek crossing floods under a routed fleet?',
    speed: 60,
    scripted: true,
    startView: {
      lon: -98.396,
      lat: 29.5228,
      height: 900,
      heading: 20,
      pitch: -34,
    },
  });
  assert.equal(Object.isFrozen(scenario), true);
  assert.equal(Object.isFrozen(scenario.startView), true);
});

test('a row without an id, a name or a position is not a scenario', () => {
  assert.equal(normalizeScenario(row({ id: 'not an id!' })), null);
  assert.equal(normalizeScenario(row({ name: '   ' })), null);
  assert.equal(normalizeScenario(row({ start_view: null })), null);
  assert.equal(
    normalizeScenario(row({ start_view: { lon: 400, lat: 29 } })),
    null,
  );
});

test('a camera value outside the contract falls back rather than flying there', () => {
  const scenario = normalizeScenario(
    row({ start_view: { lon: -98.4, lat: 29.5, height: 9e9, pitch: 400 } }),
  );
  assert.deepEqual(
    { height: scenario.startView.height, pitch: scenario.startView.pitch },
    { height: 900, pitch: -35 },
  );
});

test('a heading is wrapped into one turn, in the direction it was sent', () => {
  assert.equal(
    normalizeScenario(
      row({ start_view: { ...row().start_view, heading: -20 } }),
    ).startView.heading,
    340,
  );
});

test('display text carrying control characters is refused, not stripped', () => {
  assert.equal(normalizeScenario(row({ name: 'Flash\u0000flood' })), null);
  assert.equal(normalizeScenario(row({ what_if: 'a\u001bb' })).whatIf, null);
});

test('a listing is accepted as an envelope or as a bare array', () => {
  assert.equal(normalizeScenarioListing({ scenarios: [row()] }).length, 1);
  assert.equal(normalizeScenarioListing([row()]).length, 1);
  assert.equal(normalizeScenarioListing({ scenarios: [] }).length, 0);
});

test('one unusable row rejects the whole menu', () => {
  // A picker showing two of three scenarios is a WRONG menu, not a shorter
  // one: the missing row looks like a scenario that does not exist.
  assert.equal(
    normalizeScenarioListing({ scenarios: [row(), row({ id: '' })] }),
    null,
  );
  assert.equal(normalizeScenarioListing({ scenarios: 'all of them' }), null);
  assert.equal(normalizeScenarioListing(null), null);
});

test('a duplicate id rejects the menu rather than picking one', () => {
  assert.equal(
    normalizeScenarioListing({ scenarios: [row(), row({ name: 'Other' })] }),
    null,
  );
});

test('a control answer is a state, with the scenario only when sent', () => {
  assert.deepEqual(normalizeScenarioControl({ state: 'idle' }), {
    state: 'idle',
    scenario: null,
  });
  assert.equal(
    normalizeScenarioControl({ state: 'running', scenario: row() }).scenario.id,
    's1_flood',
  );
  // Any state the contract does not define reads as idle, never as running:
  // a banner claiming a replay that is not running is the worse error.
  assert.equal(normalizeScenarioControl({ state: 'maybe' }).state, 'idle');
  assert.equal(normalizeScenarioControl(null), null);
});

test('the client asks the four paths it is documented to ask', async () => {
  const calls = [];
  const client = createScenarioClient({
    baseUrl: 'http://localhost:8765/',
    fetchImpl: async (url, init) => {
      calls.push(`${init.method} ${url}`);
      return json(
        url.endsWith('/scenarios')
          ? { scenarios: [row()] }
          : url.includes('/approvals/')
            ? { state: 'approved' }
            : { state: url.endsWith('/stop') ? 'idle' : 'running' },
      );
    },
  });
  assert.equal(client.baseUrl, 'http://localhost:8765');
  assert.equal((await client.list()).length, 1);
  assert.equal((await client.start('s1_flood')).state, 'running');
  assert.equal((await client.stop()).state, 'idle');
  assert.equal((await client.approve('prop-1')).state, 'approved');
  assert.deepEqual(calls, [
    'GET http://localhost:8765/scenarios',
    'POST http://localhost:8765/scenarios/s1_flood/start',
    'POST http://localhost:8765/scenarios/stop',
    'POST http://localhost:8765/approvals/prop-1',
  ]);
});

test('a call carries no credentials and follows no redirect', async () => {
  let init;
  const client = createScenarioClient({
    baseUrl: 'http://localhost:8765',
    fetchImpl: async (_url, options) => {
      init = options;
      return json({ scenarios: [] });
    },
  });
  await client.list();
  assert.deepEqual(
    { credentials: init.credentials, redirect: init.redirect },
    { credentials: 'omit', redirect: 'error' },
  );
});

test('an id that is not an identifier never becomes part of a URL', async () => {
  const client = createScenarioClient({
    baseUrl: 'http://localhost:8765',
    fetchImpl: async () => {
      throw new Error('must not reach the network');
    },
  });
  await assert.rejects(() => client.start('../../etc/passwd'), TypeError);
  await assert.rejects(() => client.start(''), TypeError);
  // The approval path takes an id off the wire too, and it is the one call
  // that changes what gets PUBLISHED.
  await assert.rejects(() => client.approve('../../admin'), TypeError);
  await assert.rejects(() => client.approve(''), TypeError);
});

test('only an approved answer counts as a second key', async () => {
  // A 200 with something else in it has not told this page that a key was
  // recorded, and a button that looked as though it worked would be the
  // worst possible failure in a two-key control.
  for (const payload of [{}, { state: 'pending' }, { ok: true }, null]) {
    const client = createScenarioClient({
      baseUrl: 'http://localhost:8765',
      fetchImpl: async () => json(payload),
    });
    await assert.rejects(() => client.approve('prop-1'), ScenarioApiError);
  }
  const client = createScenarioClient({
    baseUrl: 'http://localhost:8765',
    fetchImpl: async () => json({ state: 'approved' }),
  });
  assert.deepEqual(await client.approve('prop-1'), { state: 'approved' });
});

test('an approval the API refuses is never retried against the fallback', async () => {
  // Something is listening there, and asking a second service to override
  // the first one's "no" would make the page lie about which API it drove.
  const asked = [];
  const client = createScenarioClient({
    baseUrl: 'http://localhost:8765',
    fallbackUrl: 'http://localhost:8766',
    fetchImpl: async (url) => {
      asked.push(url);
      return json({ error: 'nothing is waiting' }, { ok: false, status: 409 });
    },
  });
  await assert.rejects(
    () => client.approve('prop-1'),
    (error) => {
      assert.ok(error instanceof ScenarioApiError);
      assert.equal(error.status, 409);
      return true;
    },
  );
  assert.deepEqual(asked, ['http://localhost:8765/approvals/prop-1']);
});

test('an unreachable, refusing or unreadable API is one explained failure', async () => {
  const unreachable = createScenarioClient({
    baseUrl: 'http://localhost:8765',
    fetchImpl: async () => {
      throw new TypeError('fetch failed');
    },
  });
  await assert.rejects(() => unreachable.list(), ScenarioApiError);

  const refusing = createScenarioClient({
    baseUrl: 'http://localhost:8765',
    fetchImpl: async () => json(null, { ok: false, status: 503 }),
  });
  await assert.rejects(
    () => refusing.list(),
    (error) => {
      assert.ok(error instanceof ScenarioApiError);
      assert.equal(error.status, 503);
      return true;
    },
  );

  const unreadable = createScenarioClient({
    baseUrl: 'http://localhost:8765',
    fetchImpl: async () => ({
      ok: true,
      status: 200,
      json: async () => {
        throw new SyntaxError('not JSON');
      },
    }),
  });
  await assert.rejects(() => unreadable.list(), ScenarioApiError);
});

test('an unusable listing is a failure, not an empty picker', async () => {
  const client = createScenarioClient({
    baseUrl: 'http://localhost:8765',
    fetchImpl: async () => json({ scenarios: [row({ name: '' })] }),
  });
  await assert.rejects(() => client.list(), ScenarioApiError);
});

test('a caller`s abort is the caller`s abort, not an API failure', async () => {
  const controller = new AbortController();
  const client = createScenarioClient({
    baseUrl: 'http://localhost:8765',
    fetchImpl: async (_url, { signal }) => {
      controller.abort();
      signal.throwIfAborted();
      return json({ scenarios: [] });
    },
  });
  await assert.rejects(
    () => client.list({ signal: controller.signal }),
    (error) => error.name === 'AbortError',
  );
});

test('an unreachable API hands the call to the mock, and then stays there', async () => {
  const calls = [];
  const client = createScenarioClient({
    baseUrl: 'http://localhost:8765',
    fallbackUrl: 'http://localhost:8766',
    fetchImpl: async (url) => {
      calls.push(url);
      if (url.startsWith('http://localhost:8765'))
        throw new TypeError('fetch failed');
      return json({ scenarios: [row()] });
    },
  });
  assert.deepEqual(client.origins, [
    'http://localhost:8765',
    'http://localhost:8766',
  ]);
  assert.equal(client.fallback, false);
  assert.equal((await client.list()).length, 1);
  assert.deepEqual(calls, [
    'http://localhost:8765/scenarios',
    'http://localhost:8766/scenarios',
  ]);
  assert.equal(client.baseUrl, 'http://localhost:8766');
  assert.equal(client.fallback, true);

  // The next call starts where the last one succeeded: a session does not
  // alternate between a real run and a mock one.
  await client.list();
  assert.equal(calls.length, 3);
  assert.equal(calls.at(-1), 'http://localhost:8766/scenarios');
});

test('an API that refuses is not overridden by the mock', async () => {
  const calls = [];
  const client = createScenarioClient({
    baseUrl: 'http://localhost:8765',
    fallbackUrl: 'http://localhost:8766',
    fetchImpl: async (url) => {
      calls.push(url);
      return json(null, { ok: false, status: 500 });
    },
  });
  // Something IS listening on the real API, and it said no. Asking a second
  // service to answer instead would make the page lie about which it drives.
  await assert.rejects(() => client.list(), ScenarioApiError);
  assert.deepEqual(calls, ['http://localhost:8765/scenarios']);
  assert.equal(client.fallback, false);
});

test('when neither origin answers, the failure is the one a person reads', async () => {
  const client = createScenarioClient({
    baseUrl: 'http://localhost:8765',
    fallbackUrl: 'http://localhost:8766',
    fetchImpl: async () => {
      throw new TypeError('fetch failed');
    },
  });
  await assert.rejects(
    () => client.list(),
    (error) =>
      error instanceof ScenarioApiError &&
      error.message === 'The Heads Up scenario API is not reachable.',
  );
});

test('a base URL is required: there is no implicit origin to call', () => {
  assert.throws(() => createScenarioClient({}), TypeError);
  assert.throws(() => createScenarioClient({ baseUrl: '  ' }), TypeError);
});
