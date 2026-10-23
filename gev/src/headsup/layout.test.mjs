import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * The Heads Up chrome leaves the centre of the 3D view clear at 1920x1080.
 *
 * The build step states this as a requirement, so it is checked rather than
 * eyeballed — and checked against the STYLESHEET, because that is where the
 * numbers live. The geometry tokens in `headsup-mode.css` and the two
 * upstream values the rail and the strip hang off are read out of the CSS,
 * the reserved rectangles are computed from them at 1920x1080, and each one
 * is asserted not to touch the protected centre.
 *
 * The protected centre is the middle HALF of the viewport in both axes:
 * 960x540 centred, which at 1920x1080 is x 480-1440 and y 270-810. That is
 * the region an operator is actually looking at — a hazard polygon, the
 * fleet moving through it, the camera's own subject — and it is a definition
 * rather than a measurement, so it is stated here once and everything else
 * is derived.
 *
 * A value this test cannot find in the CSS is a FAILURE, not a skip: a rule
 * renamed out from under it must show up as a broken check rather than as a
 * check that quietly stopped checking.
 */

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STYLES = path.join(HERE, '..', 'ui', 'styles');

const VIEWPORT_WIDTH = 1920;
const VIEWPORT_HEIGHT = 1080;

/** The region the chrome must not enter: the middle half, both axes. */
const CENTRE = Object.freeze({
  left: VIEWPORT_WIDTH * 0.25,
  right: VIEWPORT_WIDTH * 0.75,
  top: VIEWPORT_HEIGHT * 0.25,
  bottom: VIEWPORT_HEIGHT * 0.75,
});

const css = (name) => readFileSync(path.join(STYLES, name), 'utf8');

/**
 * One pixel value out of a stylesheet, by declaration name.
 * @param {string} source Stylesheet text.
 * @param {string} declaration Property or custom property name.
 * @param {string} [within] Selector whose block to look inside.
 * @returns {number} Pixels.
 */
function pixels(source, declaration, within) {
  let text = source;
  if (within) {
    const at = source.indexOf(within);
    assert.notEqual(
      at,
      -1,
      `layout: ${within} is gone from the stylesheet; this check needs it`,
    );
    const open = source.indexOf('{', at);
    const close = source.indexOf('}', open);
    text = source.slice(open, close);
  }
  const match = new RegExp(
    `${declaration.replace(/[-]/g, '\\-')}\\s*:\\s*(-?[\\d.]+)(px|vh)`,
  ).exec(text);
  assert.ok(
    match,
    `layout: ${declaration}${within ? ` in ${within}` : ''} is no longer a px or vh value; this check needs it`,
  );
  // `vh` resolved against the viewport this whole file is about, so a value
  // upstream expresses relative to the window still lands in one geometry.
  return match[2] === 'vh'
    ? (Number(match[1]) / 100) * VIEWPORT_HEIGHT
    : Number(match[1]);
}

const mode = css('headsup-mode.css');
const foundation = css('foundation.css');
const overlays = css('overlays.css');

/** The numbers the layout is built from, read where they are declared. */
const LAYOUT = Object.freeze({
  railX: pixels(foundation, '--right-rail-x'),
  railWidth: pixels(mode, '--headsup-rail-width'),
  railTop: pixels(mode, '--headsup-rail-top'),
  railBottom: pixels(mode, '--headsup-rail-bottom'),
  stripMaxWidth: pixels(mode, '--headsup-strip-max-width'),
  stripMaxHeight: pixels(mode, '--headsup-strip-max-height'),
  // Upstream's own bottom-right HUD corner, which hosts the strip.
  cornerBottom: pixels(overlays, 'bottom', '.hud-bottom-right'),
  cornerRight: pixels(overlays, 'right', '.hud-bottom-right'),
  cornerPadding: pixels(overlays, 'padding', '.hud-corner'),
  bannerTop: pixels(mode, 'top', '.headsup-banner'),
});

/**
 * The rail, at its tallest.
 *
 * It is fixed to the right rail lane and scrolls rather than growing, so its
 * height is the max-height the stylesheet gives it — which is what makes
 * this a bound and not a guess about content.
 */
const railRect = () => {
  const right = VIEWPORT_WIDTH - LAYOUT.railX;
  return {
    name: 'the agent pipeline rail',
    left: right - LAYOUT.railWidth,
    right,
    top: LAYOUT.railTop,
    bottom: VIEWPORT_HEIGHT - LAYOUT.railBottom,
  };
};

/**
 * The counter strip, at its widest and tallest.
 *
 * It lives inside upstream's bottom-right HUD corner, which is positioned
 * from the bottom and the right, so the strip grows up and to the left from
 * there. Both of its caps are in the stylesheet for exactly this reason.
 */
const stripRect = () => {
  const right = VIEWPORT_WIDTH - LAYOUT.cornerRight - LAYOUT.cornerPadding;
  const bottom = VIEWPORT_HEIGHT - LAYOUT.cornerBottom - LAYOUT.cornerPadding;
  return {
    name: 'the counter strip',
    left: right - LAYOUT.stripMaxWidth,
    right,
    top: bottom - LAYOUT.stripMaxHeight,
    bottom,
  };
};

/** The what-if banner: centred, so only its height matters here. */
const bannerRect = () => ({
  name: 'the what-if banner',
  // Its own `max-width: calc(100vw - 48px)`, taken at the worst case.
  left: 24,
  right: VIEWPORT_WIDTH - 24,
  top: LAYOUT.bannerTop,
  // One line of 10px uppercase mono in a 7px/18px pill, rounded up well
  // past what it needs: the claim being checked is that it stays above the
  // centre, so the generous number is the honest one to check.
  bottom: LAYOUT.bannerTop + 48,
});

const overlapsCentre = (rect) =>
  rect.left < CENTRE.right &&
  rect.right > CENTRE.left &&
  rect.top < CENTRE.bottom &&
  rect.bottom > CENTRE.top;

test('every value this check depends on is still declared in the CSS', () => {
  // `pixels` throws on a missing declaration, so reaching here is the
  // assertion. The numbers are printed so a failure downstream can be read
  // without re-deriving them.
  for (const [name, value] of Object.entries(LAYOUT))
    assert.ok(Number.isFinite(value), `${name} is not a number`);
});

test('the rail and the counter strip leave the centre of the view clear', () => {
  for (const rect of [railRect(), stripRect(), bannerRect()])
    assert.equal(
      overlapsCentre(rect),
      false,
      `${rect.name} reaches into the protected centre: ` +
        `x ${rect.left}-${rect.right}, y ${rect.top}-${rect.bottom} against ` +
        `x ${CENTRE.left}-${CENTRE.right}, y ${CENTRE.top}-${CENTRE.bottom}`,
    );
});

test('the rail sits in the right rail lane and stays on screen', () => {
  const rail = railRect();
  assert.ok(rail.left > CENTRE.right, 'the rail is right of the centre');
  assert.ok(rail.right <= VIEWPORT_WIDTH, 'the rail is on screen');
  assert.ok(rail.bottom > rail.top, 'the rail has room to draw in');
});

/**
 * How tall upstream's command dock is taken to be, generously.
 *
 * The dock is content-sized — a location bar, a control panel and the voice
 * control, side by side — so there is no px height in the CSS to read. 160px
 * is well over what it draws at, and over-estimating is the safe direction:
 * it makes this check stricter, not looser.
 */
const COMMAND_DOCK_HEIGHT = 160;

test('the rail stops above upstream`s command dock, which it spans', () => {
  // The dock is `left: 50%` with `width: min(94vw, 1240px)`, so it runs
  // under the rail's lane horizontally and the two can only be kept apart
  // vertically. That is what `--headsup-rail-bottom` reserves.
  const rail = railRect();
  const dockBottom = pixels(
    css('command-dock.css'),
    'bottom',
    '#command-dock',
  );
  const dockTop = VIEWPORT_HEIGHT - dockBottom - COMMAND_DOCK_HEIGHT;
  assert.ok(
    rail.bottom <= dockTop,
    `the rail (ends at y ${rail.bottom}) reaches the command dock (starts at y ${dockTop})`,
  );
});

test('the counter strip sits below the centre, however wide it gets', () => {
  const strip = stripRect();
  assert.ok(
    strip.top > CENTRE.bottom,
    `the strip's top edge (${strip.top}) must be below the centre (${CENTRE.bottom})`,
  );
  // Its height cap is what buys that, so the two are asserted together: a
  // strip allowed to grow upward would reach the centre whatever its width.
  assert.ok(LAYOUT.stripMaxHeight > 0, 'the strip height is capped');
  assert.ok(strip.bottom <= VIEWPORT_HEIGHT, 'the strip is on screen');
});

test('the rail is hidden rather than overlaid on a narrow viewport', () => {
  // Below 900px there is no right rail lane to put it in, so the stylesheet
  // takes it off the screen instead of letting it cover the globe.
  const narrow = mode.slice(mode.indexOf('@media (max-width: 900px)'));
  assert.notEqual(narrow.length, 0, 'the narrow-viewport block is gone');
  assert.match(narrow, /\.headsup-rail\s*\{\s*display:\s*none/);
});
