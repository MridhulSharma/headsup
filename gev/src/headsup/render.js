/**
 * The two render knobs Heads Up mode turns, and the only two numbers in it.
 *
 * Both are HERE, at the top of one file, because they are the dials a person
 * reaches for when the frame rate is short: raise the tileset's screen-space
 * error to draw a coarser city, or drop the resolution scale to rasterize
 * fewer pixels. Neither is derived from anything, neither is read off a feed,
 * and nothing else in the console may set them.
 *
 * They are applied in Heads Up mode only. `?mode=full` is upstream's
 * application with upstream's own defaults, which is what keeps a
 * `git subtree pull` a merge of upstream's files.
 *
 * No Cesium import: these are two property writes on objects the application
 * phase already holds, which is also what makes them testable without a
 * canvas.
 * @module headsup/render
 */

/**
 * Screen-space error for the photoreal 3D tileset, in pixels.
 *
 * Cesium's default is 16: a tile is refined until its error on screen is
 * under that. 24 asks for about half as many tiles over the same view —
 * coarser geometry on the far side of downtown, which is the half of the
 * frame a fleet readout is not about. Raise it further for more frames.
 */
export const HEADSUP_TILESET_SCREEN_SPACE_ERROR = 24;

/**
 * The scene's resolution scale: 1.0 is one drawing-buffer pixel per CSS
 * pixel, and the viewport is already only 60% of the window wide.
 *
 * Left at 1.0 deliberately. It is stated rather than left implicit so that
 * lowering it is a one-character edit in one place when a laptop GPU needs
 * the frames — 0.75 rasterizes 56% of the pixels — rather than a hunt through
 * the renderer for where the scale is decided.
 */
export const HEADSUP_RESOLUTION_SCALE = 1.0;

/**
 * Apply both knobs, as far as the objects handed in allow.
 *
 * A missing tileset is the OSM fallback route (`src/maps/google3d.js`), where
 * there is no 3D tileset to coarsen; that is not an error and the resolution
 * scale is still applied. The return value says what was actually set, so a
 * caller — or a test — never has to infer it.
 * @param {object} [options]
 * @param {object} [options.viewer] The Cesium viewer.
 * @param {object} [options.tileset] The photoreal 3D tileset, or null.
 * @returns {Readonly<{screenSpaceError: number|null,
 *   resolutionScale: number|null}>} What was set, with null for what was not.
 */
export function applyHeadsupRenderBudget({ viewer, tileset } = {}) {
  let screenSpaceError = null;
  let resolutionScale = null;
  if (tileset && tileset.isDestroyed?.() !== true) {
    tileset.maximumScreenSpaceError = HEADSUP_TILESET_SCREEN_SPACE_ERROR;
    screenSpaceError = HEADSUP_TILESET_SCREEN_SPACE_ERROR;
  }
  if (viewer && viewer.isDestroyed?.() !== true) {
    viewer.resolutionScale = HEADSUP_RESOLUTION_SCALE;
    resolutionScale = HEADSUP_RESOLUTION_SCALE;
  }
  return Object.freeze({ screenSpaceError, resolutionScale });
}
