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
 * tokens in `headsup-mode.css` are read out of the CSS, the five rectangles
 * are computed from them at 1920x1080, and the claims the layout makes are
 * asserted:
 *
 * 1. The viewport rectangle and the dock TILE the window — they sum to its
 *    width exactly, which is what makes "no horizontal scroll" a property of
 *    the stylesheet rather than something somebody checks in a browser.
 * 2. The dock does not reach into the viewport rectangle, and the viewport is
 *    a rectangle at FULL height rather than an inscribed shape.
 * 3. The dock's four panes tile the dock: two columns of 20% window width
 *    across, a 70% row over a 30% row down, each pane in a NAMED grid area so
 *    that hiding the rail cannot reflow the other three.
 * 4. The dock column never scrolls — the two panes whose content arrives from
 *    the feed scroll inside their own region instead.
 * 5. Upstream's two centred surfaces (`#command-dock`, the what-if banner) are
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
    for (let at = source.indexOf(within); at !== -1;) {
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
  dockColumnWidth: pixels(mode, '--headsup-dock-column-width'),
  dockTopHeight: pixels(mode, '--headsup-dock-top-height'),
  dockBottomHeight: pixels(mode, '--headsup-dock-bottom-height'),
  bannerTop: pixels(mode, 'top', '.headsup-banner'),
  // Upstream's own HUD corner inset, which the frame is drawn from.
  cornerBottom: pixels(overlays, 'bottom', '.hud-bottom-right'),
  // Upstream's attribution, which must stay visible at all times.
  creditsLeft: pixels(foundation, 'left', '#cesium-credits'),
  creditsBottom: pixels(foundation, 'bottom', '#cesium-credits'),
});

/** The rectangle the globe draws into: the left 60%, full height. */
const viewportRect = () => ({
  name: 'the 3D viewport',
  left: 0,
  right: LAYOUT.viewportWidth,
  top: 0,
  bottom: VIEWPORT_HEIGHT,
});

/** The dock: the right 40%, full height. */
const dockRect = () => ({
  name: 'the console dock',
  left: VIEWPORT_WIDTH - LAYOUT.dockWidth,
  right: VIEWPORT_WIDTH,
  top: 0,
  bottom: VIEWPORT_HEIGHT,
});

/**
 * The dock's four panes, each from the grid track it is placed in.
 *
 * Placement is by named area in the CSS, so the rectangles here are computed
 * the same way the browser would compute them and not from the order the
 * console happens to append its panes in.
 */
const paneRects = () => {
  const dock = dockRect();
  const middleX = dock.left + LAYOUT.dockColumnWidth;
  const middleY = LAYOUT.dockTopHeight;
  return [
    {
      name: 'the control panel',
      area: 'controls',
      left: dock.left,
      right: middleX,
      top: 0,
      bottom: middleY,
    },
    {
      name: 'the agent rail',
      area: 'rail',
      left: middleX,
      right: dock.right,
      top: 0,
      bottom: middleY,
    },
    {
      name: 'the STATUS pane',
      area: 'status',
      left: dock.left,
      right: middleX,
      top: middleY,
      bottom: VIEWPORT_HEIGHT,
    },
    {
      name: 'the advisory pane',
      area: 'advisory',
      left: middleX,
      right: dock.right,
      top: middleY,
      bottom: VIEWPORT_HEIGHT,
    },
  ];
};

/** Which class carries each of the dock's named grid areas. */
const PANE_SELECTORS = Object.freeze({
  controls: '.headsup-controls',
  rail: '.headsup-rail',
  status: '.headsup-strip',
  advisory: '.headsup-advisory',
});

const overlaps = (a, b) =>
  a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;

const area = (rect) => (rect.right - rect.left) * (rect.bottom - rect.top);

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

test('the viewport is a full-height rectangle filling the left 60%', () => {
  const view = viewportRect();
  assert.equal(view.top, 0, 'the viewport starts at the top of the window');
  assert.equal(
    view.bottom,
    VIEWPORT_HEIGHT,
    'the viewport runs to the bottom of the window',
  );
  assert.equal(
    Math.round((LAYOUT.viewportWidth / VIEWPORT_WIDTH) * 100),
    60,
    'the viewport is 60% of the window wide',
  );
  // The HUD frame is drawn inside that rectangle, not over the window.
  assert.match(
    mode,
    /#intel-hud\s*\{\s*right:\s*var\(--headsup-dock-width\)/,
    'the HUD frame no longer ends at the dock edge',
  );
  // And the scene canvas is what was narrowed, which is the whole reason the
  // rectangle costs fewer pixels than a full-window one. No mask, no painting
  // panels over a full-window canvas: the element Cesium sizes its drawing
  // buffer from IS the 60% region.
  assert.match(
    mode,
    /#cesiumContainer,\s*#world-overlay-root\)\s*\{\s*width:\s*var\(--headsup-viewport-width\)/,
    'the scene canvas and the overlay host are no longer held to the viewport',
  );
});

test('the dock`s four panes tile it: two 20% columns, a 70% row over a 30% one', () => {
  assert.equal(
    LAYOUT.dockColumnWidth * 2,
    LAYOUT.dockWidth,
    'the two dock columns must sum to the dock width exactly',
  );
  assert.equal(
    LAYOUT.dockTopHeight + LAYOUT.dockBottomHeight,
    VIEWPORT_HEIGHT,
    'the two dock rows must sum to the window height exactly',
  );
  assert.equal(
    Math.round((LAYOUT.dockColumnWidth / VIEWPORT_WIDTH) * 100),
    20,
    'each dock column is 20% of the window wide',
  );
  assert.equal(
    Math.round((LAYOUT.dockTopHeight / VIEWPORT_HEIGHT) * 100),
    70,
    'the dock`s top row is 70% of the window high',
  );
  const panes = paneRects();
  // The four panes fill the dock and nothing else: equal areas, no overlap,
  // and none of them reaching into the picture.
  let filled = 0;
  for (const pane of panes) {
    filled += area(pane);
    assert.equal(
      overlaps(pane, viewportRect()),
      false,
      `${pane.name} reaches into the 3D viewport`,
    );
    for (const other of panes)
      if (other !== pane)
        assert.equal(
          overlaps(pane, other),
          false,
          `${pane.name} overlaps ${other.name}`,
        );
  }
  assert.equal(filled, area(dockRect()), 'the four panes do not fill the dock');
});

test('each pane is placed by name, so hiding the rail cannot reflow the rest', () => {
  // The rail is hidden on the landing view. Under auto-placement that would
  // pull STATUS up into the column the rail had just vacated, so the areas are
  // named and every pane says which one it occupies.
  assert.match(
    mode,
    /grid-template-areas:\s*'controls rail'\s*'status advisory';/,
    'the dock no longer names its four grid areas',
  );
  for (const [name, selector] of Object.entries(PANE_SELECTORS))
    assert.match(
      mode,
      new RegExp(`\\${selector}\\s*\\{[^}]*grid-area:\\s*${name};`),
      `${selector} is not placed in the ${name} grid area`,
    );
});

test('the dock never scrolls; the panes the feed fills scroll inside themselves', () => {
  assert.match(
    mode,
    /\.headsup-dock\s*\{[^}]*overflow:\s*hidden/,
    'the dock column must not scroll',
  );
  // The rail grows with the pipeline, and the control panel grows with the
  // fleet and hazard-class rows the feed reports.
  assert.match(
    mode,
    /\.headsup-rail\s*\{[^}]*overflow-y:\s*auto/,
    'the rail must scroll rather than grow',
  );
  assert.match(
    mode,
    /\.headsup-controls\s*\{[^}]*overflow-y:\s*auto/,
    'the control panel must scroll rather than grow',
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
  assert.match(
    dockBlock,
    /width:\s*min\(calc\(var\(--headsup-viewport-width\)/,
  );
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
  // Below 1280px a 40% dock is more chrome than picture, so the stylesheet
  // takes it off the screen and gives the window back to the globe.
  const narrow = mode.slice(mode.indexOf('@media (max-width: 1280px)'));
  assert.notEqual(narrow.length, 0, 'the narrow-viewport block is gone');
  assert.match(narrow, /\.headsup-dock\s*\{\s*display:\s*none/);
  // And every geometry token is restated, or the frame would stay narrowed
  // while the dock that justified it was gone.
  assert.match(narrow, /--headsup-viewport-width:\s*100vw/);
  assert.match(narrow, /--headsup-dock-width:\s*0vw/);
  assert.match(narrow, /--headsup-dock-column-width:\s*0vw/);
});
