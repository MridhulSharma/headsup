import assert from 'node:assert/strict';
import test from 'node:test';
import {
  FULL_MODE,
  HEADSUP_MODE,
  HEADSUP_OMITTED_LAYERS,
  createHeadsupModeConfig,
  markHeadsupMode,
  resolveHeadsupMode,
} from './mode.js';

test('Heads Up is the default application, with no parameter at all', () => {
  assert.equal(resolveHeadsupMode(), HEADSUP_MODE);
  assert.equal(resolveHeadsupMode(''), HEADSUP_MODE);
  assert.equal(resolveHeadsupMode('?city=austin'), HEADSUP_MODE);
});

test('?mode=full asks for upstream, in any casing or spacing', () => {
  assert.equal(resolveHeadsupMode('?mode=full'), FULL_MODE);
  assert.equal(resolveHeadsupMode('mode=FULL'), FULL_MODE);
  assert.equal(resolveHeadsupMode('?a=1&mode=%20full%20'), FULL_MODE);
});

test('a mode nobody declared resolves to the default, not to upstream', () => {
  // A typo in the parameter must not quietly hand someone the full
  // application with every live feed it initializes.
  assert.equal(resolveHeadsupMode('?mode=fulll'), HEADSUP_MODE);
  assert.equal(resolveHeadsupMode('?mode='), HEADSUP_MODE);
});

test('Heads Up mode omits the feeds it has no use for and gates the extras', () => {
  const mode = createHeadsupModeConfig();
  assert.equal(mode.headsUp, true);
  assert.deepEqual([...mode.omitLayers], [...HEADSUP_OMITTED_LAYERS]);
  assert.deepEqual([...mode.enableLayers], ['headsup']);
  assert.deepEqual(
    {
      layerPanel: mode.upstreamLayerPanel,
      voice: mode.voiceAgent,
      keys: mode.providerSettings,
      summary: mode.hudSummary,
      hud: mode.forceHudVisible,
    },
    {
      layerPanel: false,
      voice: false,
      keys: false,
      summary: false,
      hud: true,
    },
  );
  assert.equal(mode.scenarioBaseUrl, 'http://localhost:8765');
});

test('full mode gates nothing and omits nothing', () => {
  const mode = createHeadsupModeConfig({ search: '?mode=full' });
  assert.deepEqual(
    {
      id: mode.id,
      headsUp: mode.headsUp,
      omitted: mode.omitLayers.length,
      enabled: mode.enableLayers.length,
      layerPanel: mode.upstreamLayerPanel,
      voice: mode.voiceAgent,
      keys: mode.providerSettings,
      summary: mode.hudSummary,
      hud: mode.forceHudVisible,
    },
    {
      id: 'full',
      headsUp: false,
      omitted: 0,
      enabled: 0,
      layerPanel: true,
      voice: true,
      keys: true,
      summary: true,
      hud: false,
    },
  );
});

test('the scenario API origin can be overridden by the entry', () => {
  assert.equal(
    createHeadsupModeConfig({ scenarioBaseUrl: 'http://127.0.0.1:9000' })
      .scenarioBaseUrl,
    'http://127.0.0.1:9000',
  );
  // An empty override is not an origin; the default stands.
  assert.equal(
    createHeadsupModeConfig({ scenarioBaseUrl: '' }).scenarioBaseUrl,
    'http://localhost:8765',
  );
});

test('the mock feed is the scenario API`s fallback, and can be refused', () => {
  const defaults = createHeadsupModeConfig();
  // The real API first, the mock second: one port each, so both can be up.
  assert.equal(defaults.scenarioBaseUrl, 'http://localhost:8765');
  assert.equal(defaults.scenarioFallbackUrl, 'http://localhost:8766');
  assert.equal(
    createHeadsupModeConfig({ scenarioFallbackUrl: null }).scenarioFallbackUrl,
    null,
    'a deployment with no mock to fall back to says so',
  );
});

test('the config is frozen, so no module can widen another module`s mode', () => {
  const mode = createHeadsupModeConfig();
  assert.throws(() => {
    'use strict';
    mode.voiceAgent = true;
  }, TypeError);
  assert.equal(Object.isFrozen(mode.omitLayers), true);
});

test('the CSS hook is the single thing applied to the document', () => {
  const classes = new Set();
  const doc = {
    documentElement: { dataset: {} },
    body: {
      classList: {
        add: (name) => classes.add(name),
        remove: (name) => classes.delete(name),
      },
    },
  };
  markHeadsupMode(doc, createHeadsupModeConfig());
  assert.equal(doc.documentElement.dataset.gevMode, 'headsup');
  assert.deepEqual([...classes], ['headsup-mode']);
  markHeadsupMode(doc, createHeadsupModeConfig({ search: '?mode=full' }));
  assert.equal(doc.documentElement.dataset.gevMode, 'full');
  assert.deepEqual([...classes], []);
});
