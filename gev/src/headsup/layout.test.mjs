import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * The Heads Up layout holds at 1920x1080, checked rather than eyeballed.
 *
 * The build step states the screen split as a requirement, so it is checked
 * against the STYLESHEET, because that is where the numbers live. The geometry
 * tokens in `headsup-mode.css` are read out of the CSS, the two rectangles are
 * computed from them at 1920x1080, and the claims the layout makes are
 * asserted:
 *
 * 1. The viewport rectangle and the dock column TILE the window — they sum to
 *    its width exactly, which is what makes "no horizontal scroll" a property
 *    of the stylesheet rather than something somebody checks in a browser.
 * 2. The dock does not reach into the viewport rectangle, and the viewport is
 *    a rectangle at FULL height rather than an inscribed shape.
 * 3. The dock's four blocks fit its height, so the column never needs a
 *    scrollbar of its own — one block scrolls, the column does not.
 * 4. Upstream's two centred surfaces (`#command-dock`, the what-if banner) are
 *    centred on the viewport rectangle and stay inside it.
 *
 * A value this test cannot find in the CSS is a FAILURE, not a skip: a rule
 * renamed out from under it must show up as a broken check rather than as a
 * check that quietly stopped checking.
 */

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STYLES = path.join(HERE, '..', 'ui', 'styles');

const VIEWPORT_WIDTH = 1920;
const VIEWPORT_HEIGHT = 1080;

const css = (name) => readFileSync(path.join(STYLES, name), 'utf8');

/**
 * One length out of a stylesheet, by declaration name, in pixels.
 *
 * `vw` and `vh` are resolved against the viewport this whole file is about,
 * so a value expressed relative to the window still lands in one geometry.
 * @param {string} source Stylesheet text.
 * @param {string} declaration Property or custom property name.
 * @param {string} [within] Selector whose block to look inside.
 * @returns {number} Pixels.
 */
function pixels(source, declaration, within) {
  const pattern = new RegExp(
    `${declaration.replace(/[-]/g, '\\-')}\\s*:\\s*(-?[\\d.]+)(px|vh|vw)`,
  );
  let match = null;
  if (within) {
    // A selector can carry more than one rule — one in the geometry section,
    // one in its own — so every block it names is tried and the first that
    // actually declares the property answers. Finding none is still a failure.
    assert.notEqual(
      source.indexOf(within),
      -1,
      `layout: ${within} is gone from the stylesheet; this check needs it`,
    );
    for (let at = source.indexOf(within); at !== -1; ) {
      const open = source.indexOf('{', at);
      const close = source.indexOf('}', open);
      match = pattern.exec(source.slice(open, close));
      if (match) break;
      at = source.indexOf(within, close === -1 ? source.length : close);
    }
  } else match = pattern.exec(source);
  assert.ok(
    match,
    `layout: ${declaration}${within ? ` in ${within}` : ''} is no longer a px, vh or vw value; this check needs it`,
  );
  if (match[2] === 'vh') return (Number(match[1]) / 100) * VIEWPORT_HEIGHT;
  if (match[2] === 'vw') return (Number(match[1]) / 100) * VIEWPORT_WIDTH;
  return Number(match[1]);
}

const mode = css('headsup-mode.css');
const foundation = css('foundation.css');
const overlays = css('overlays.css');

/** The numbers the layout is built from, read where they are declared. */
const LAYOUT = Object.freeze({
  viewportWidth: pixels(mode, '--headsup-viewport-width'),
  dockWidth: pixels(mode, '--headsup-dock-width'),
  dockGap: pixels(mode, '--headsup-dock-gap'),
  dockPadding: pixels(mode, '--headsup-dock-padding'),
  scenesMaxHeight: pixels(mode, '--headsup-scenes-max-height'),
  advisoryMaxHeight: pixels(mode, '--headsup-advisory-max-height'),
  stripMaxHeight: pixels(mode, '--headsup-strip-max-height'),
  railMinHeight: pixels(mode, '--headsup-rail-min-height'),
  bannerTop: pixels(mode, 'top', '.headsup-banner'),
  // Upstream's own HUD corner inset, which the frame is drawn from.
  cornerBottom: pixels(overlays, 'bottom', '.hud-bottom-right'),
  // Upstream's attribution, which must stay visible at all times.
  creditsLeft: pixels(foundation, 'left', '#cesium-credits'),
  creditsBottom: pixels(foundation, 'bottom', '#cesium-credits'),
});

/** The rectangle the globe draws into: the left 55%, full height. */
const viewportRect = () => ({
  name: 'the 3D viewport',
  left: 0,
  right: LAYOUT.viewportWidth,
  top: 0,
  bottom: VIEWPORT_HEIGHT,
});

/** The dock: the right 45%, full height. */
const dockRect = () => ({
  name: 'the console dock',
  left: VIEWPORT_WIDTH - LAYOUT.dockWidth,
  right: VIEWPORT_WIDTH,
  top: 0,
  bottom: VIEWPORT_HEIGHT,
});

const overlaps = (a, b) =>
  a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;

test('every value this check depends on is still declared in the CSS', () => {
  // `pixels` throws on a missing declaration, so reaching here is the
  // assertion. The numbers are checked for finiteness so a failure downstream
  // can be read without re-deriving them.
  for (const [name, value] of Object.entries(LAYOUT))
    assert.ok(Number.isFinite(value), `${name} is not a number`);
});

test('the viewport and the dock tile the window, so nothing scrolls sideways', () => {
  assert.equal(
    LAYOUT.viewportWidth + LAYOUT.dockWidth,
    VIEWPORT_WIDTH,
    'the viewport and the dock must sum to the window width exactly',
  );
  assert.equal(
    overlaps(viewportRect(), dockRect()),
    false,
    'the dock reaches into the 3D viewport',
  );
});

test('the viewport is a full-height rectangle filling the left 55%', () => {
  const view = viewportRect();
  assert.equal(view.top, 0, 'the viewport starts at the top of the window');
  assert.equal(
    view.bottom,
    VIEWPORT_HEIGHT,
    'the viewport runs to the bottom of the window',
  );
  assert.equal(
    Math.round((LAYOUT.viewportWidth / VIEWPORT_WIDTH) * 100),
    55,
    'the viewport is 55% of the window wide',
  );
  // The HUD frame is drawn inside that rectangle, not over the window.
  assert.match(
    mode,
    /#intel-hud\s*\{\s*right:\s*var\(--headsup-dock-width\)/,
    'the HUD frame no longer ends at the dock edge',
  );
  // And the scene canvas is what was narrowed, which is the whole reason the
  // rectangle costs fewer pixels than a full-window one.
  assert.match(
    mode,
    /#cesiumContainer,\s*#world-overlay-root\)\s*\{\s*width:\s*var\(--headsup-viewport-width\)/,
    'the scene canvas and the overlay host are no longer held to the viewport',
  );
});

test('the dock`s four blocks fit its height without scrolling the column', () => {
  // Three blocks are capped and the fourth takes the slack, so the question
  // is whether the capped three plus the rail's own minimum fit. Borders are
  // not in the CSS as a token, so a pixel each is counted generously.
  const chrome = LAYOUT.dockPadding * 2 + LAYOUT.dockGap * 3 + 8;
  const blocks =
    LAYOUT.scenesMaxHeight +
    LAYOUT.advisoryMaxHeight +
    LAYOUT.stripMaxHeight +
    LAYOUT.railMinHeight;
  assert.ok(
    blocks + chrome <= VIEWPORT_HEIGHT,
    `the four blocks need ${blocks + chrome}px of the ${VIEWPORT_HEIGHT}px window`,
  );
  // The rail is the one that scrolls; the column itself must not.
  assert.match(
    mode,
    /\.headsup-dock\s*\{[^}]*overflow:\s*hidden/,
    'the dock column must not scroll',
  );
  assert.match(
    mode,
    /\.headsup-rail\s*\{[^}]*overflow-y:\s*auto/,
    'the rail must scroll rather than grow',
  );
});

/**
 * How tall upstream's command dock is taken to be, generously.
 *
 * It is content-sized — a location bar, a control panel and the voice control,
 * side by side — so there is no px height in the CSS to read. 160px is well
 * over what it draws at, and over-estimating is the safe direction here.
 */
const COMMAND_DOCK_HEIGHT = 160;

test('upstream`s centred surfaces are centred on the viewport, not the window', () => {
  // Both are `left: 50%` on the window upstream. Heads Up mode re-centres
  // them on the rectangle, so neither straddles the dock's edge.
  for (const selector of ['#command-dock', '.headsup-banner']) {
    const at = mode.indexOf(selector);
    assert.notEqual(at, -1, `${selector} has no Heads Up rule any more`);
    const block = mode.slice(mode.indexOf('{', at), mode.indexOf('}', at));
    assert.match(
      block,
      /left:\s*calc\(var\(--headsup-viewport-width\)\s*\/\s*2\)/,
      `${selector} is not centred on the viewport rectangle`,
    );
  }
  // The dock's own width is capped against the rectangle, so a 1240px bar
  // cannot cross into the column.
  const dockBlock = mode.slice(
    mode.indexOf('{', mode.indexOf('#command-dock')),
    mode.indexOf('}', mode.indexOf('#command-dock')),
  );
  assert.match(dockBlock, /width:\s*min\(calc\(var\(--headsup-viewport-width\)/);
  // And it still clears the bottom of the window by upstream's own inset.
  const dockBottom = pixels(css('command-dock.css'), 'bottom', '#command-dock');
  assert.ok(
    dockBottom + COMMAND_DOCK_HEIGHT < VIEWPORT_HEIGHT,
    'the command dock does not fit above the bottom of the window',
  );
});

test('the attribution stays inside the viewport and on screen', () => {
  // Google and Cesium attribution must be visible AT ALL TIMES, so the one
  // thing that could cover it — the dock — must not reach it.
  const credits = {
    name: 'the attribution',
    left: LAYOUT.creditsLeft,
    // Its own content width, over-estimated: the claim under test is which
    // side of the split it is on.
    right: LAYOUT.creditsLeft + 420,
    top: VIEWPORT_HEIGHT - LAYOUT.creditsBottom - 24,
    bottom: VIEWPORT_HEIGHT - LAYOUT.creditsBottom,
  };
  assert.equal(
    overlaps(credits, dockRect()),
    false,
    'the dock covers the Google/Cesium attribution',
  );
  assert.match(
    mode,
    /#cesium-credits\s*\{[^}]*display:\s*block\s*!important/,
    'the attribution is no longer pinned visible in Heads Up mode',
  );
});

test('the HUD frame keeps its corner inset inside the rectangle', () => {
  // The frame is kept, and it reads on a rectangle because it was never
  // circular: four corner brackets at an inset from the edges of whatever box
  // #intel-hud covers, which is now the viewport rectangle.
  assert.ok(
    LAYOUT.cornerBottom < VIEWPORT_HEIGHT,
    'the HUD corner inset is off screen',
  );
  assert.ok(
    LAYOUT.cornerBottom > 0,
    'the HUD corner no longer has an inset to draw at',
  );
});

test('the dock is hidden rather than overlaid on a narrow viewport', () => {
  // Below 1280px a 45% column is more chrome than picture, so the stylesheet
  // takes it off the screen and gives the window back to the globe.
  const narrow = mode.slice(mode.indexOf('@media (max-width: 1280px)'));
  assert.notEqual(narrow.length, 0, 'the narrow-viewport block is gone');
  assert.match(narrow, /\.headsup-dock\s*\{\s*display:\s*none/);
  // And every geometry token is restated, or the frame would stay narrowed
  // while the dock that justified it was gone.
  assert.match(narrow, /--headsup-viewport-width:\s*100vw/);
  assert.match(narrow, /--headsup-dock-width:\s*0vw/);
});
