import assert from 'node:assert/strict';
import test from 'node:test';
import {
  HEADSUP_RESOLUTION_SCALE,
  HEADSUP_TILESET_SCREEN_SPACE_ERROR,
  applyHeadsupRenderBudget,
} from './render.js';

/**
 * The two render knobs, pinned at the values the build step asked for.
 *
 * They are numbers in a stylesheet's position: something a person tunes by
 * hand. A test is what says which value is the one that was decided on, so a
 * change to either is a change somebody made on purpose.
 */

test('the knobs are the values Heads Up mode was tuned to', () => {
  assert.equal(HEADSUP_TILESET_SCREEN_SPACE_ERROR, 24);
  assert.equal(HEADSUP_RESOLUTION_SCALE, 1.0);
});

test('both are applied to the objects handed in', () => {
  const tileset = { maximumScreenSpaceError: 16 };
  const viewer = { resolutionScale: 1 };
  const applied = applyHeadsupRenderBudget({ viewer, tileset });
  assert.equal(tileset.maximumScreenSpaceError, 24);
  assert.equal(viewer.resolutionScale, HEADSUP_RESOLUTION_SCALE);
  assert.deepEqual(applied, { screenSpaceError: 24, resolutionScale: 1.0 });
});

test('the OSM fallback has no tileset, and that is not a failure', () => {
  // `src/maps/google3d.js` resolves to flat OSM imagery when no provider key
  // answers. There is no 3D tileset to coarsen then, and the resolution scale
  // is still the console's to set.
  const viewer = { resolutionScale: 1 };
  const applied = applyHeadsupRenderBudget({ viewer, tileset: null });
  assert.deepEqual(applied, { screenSpaceError: null, resolutionScale: 1.0 });
});

test('a destroyed viewer or tileset is left alone', () => {
  // Teardown can race a late application phase; writing to either one after
  // it is destroyed is how Cesium throws from a path nobody is watching.
  const tileset = {
    maximumScreenSpaceError: 16,
    isDestroyed: () => true,
  };
  const viewer = { resolutionScale: 1, isDestroyed: () => true };
  const applied = applyHeadsupRenderBudget({ viewer, tileset });
  assert.equal(tileset.maximumScreenSpaceError, 16);
  assert.equal(viewer.resolutionScale, 1);
  assert.deepEqual(applied, {
    screenSpaceError: null,
    resolutionScale: null,
  });
});

test('nothing at all is a no-op rather than a throw', () => {
  assert.deepEqual(applyHeadsupRenderBudget(), {
    screenSpaceError: null,
    resolutionScale: null,
  });
});
