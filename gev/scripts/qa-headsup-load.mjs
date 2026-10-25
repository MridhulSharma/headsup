#!/usr/bin/env node
/**
 * qa-headsup-load — the Heads Up viz budget, measured.
 *
 * The claim under test is the one D2.7 sets: the `headsup` layer holds 60 fps
 * with 1,000 vehicles, and Gate 1 will not accept less than 30. This script
 * measures it the way a person would — it reads the fork's OWN fps readout,
 * the one behind the backquote key (`src/ui/frameRateMonitor.js`), rather than
 * counting frames through a hook of its own. A number from the same readout a
 * developer looks at cannot disagree with what they see.
 *
 * What it does:
 *
 *  1. Starts `viz/load_stream.py` — 1,000 vehicles, 200 of them on a detour
 *     carrying deflection trails, one advisory, one accepted answer and one
 *     refusal — unless `--no-feed` says one is already running.
 *  2. Opens the viewer in Heads Up mode, parks the camera over the whole
 *     lattice, and waits for the fleet to actually arrive.
 *  3. Turns the fps readout on with a backquote and samples it once a second
 *     for the length of the run, next to the layer's own counters.
 *  4. Prints every sample, the distribution, and whether the budget held.
 *
 * The feed reports its own worst tick on exit. That matters: a browser holding
 * 60 fps against a generator that fell behind has not been tested, so the two
 * halves of the measurement are printed together.
 *
 * Usage, with a dev server already up (`cd gev && npm run dev`):
 *
 *   node scripts/qa-headsup-load.mjs
 *   node scripts/qa-headsup-load.mjs --url http://localhost:4173 --seconds 60
 *   node scripts/qa-headsup-load.mjs --width 1600 --height 960
 *   node scripts/qa-headsup-load.mjs --no-feed --headful
 *
 * Headless by default, with occlusion throttling disabled so the frame clock
 * is honest. On a machine without a GPU the browser falls back to SwiftShader
 * and the absolute numbers mean nothing — the script says so rather than
 * reporting a failure it cannot attribute.
 */
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import puppeteer from 'puppeteer';
import { projectRoot } from './project-root.mjs';

/** The fork's own directory, and the Heads Up repository that vendors it. */
const gevRoot = projectRoot(import.meta.url);

const argv = process.argv.slice(2);
const flag = (name, fallback = null) => {
  const index = argv.indexOf(`--${name}`);
  return index === -1 ? fallback : argv[index + 1];
};
const has = (name) => argv.includes(`--${name}`);

const url = flag('url', 'http://localhost:4173');
const seconds = Number(flag('seconds', 60));
/**
 * The page viewport the run is measured at.
 *
 * It is a parameter because the rasterized area IS what is being paid for:
 * Heads Up mode draws the globe into the left 55% of the window, so a reading
 * is only comparable to another one taken at the same size. The default is the
 * 1920x1080 the layout contract is stated at (src/headsup/layout.test.mjs).
 */
const width = Number(flag('width', 1920));
const height = Number(flag('height', 1080));
const port = Number(flag('port', 8766));
const startFeed = !has('no-feed');
const headless = !has('headful');
/** Gate 1's floor, and the budget the build step actually names. */
const FLOOR_FPS = 30;
const TARGET_FPS = 60;
/** Vehicles and trails the feed is built to deliver. */
const EXPECT_VEHICLES = 1000;
const EXPECT_TRAILS = 200;

const repoRoot = path.resolve(gevRoot, '..');
/**
 * The interpreter is taken from the repository's own virtualenv rather than
 * from `uv run`: `uv run` can reinstall the `headsup` package underneath a
 * process that is already running, which stalls a paced feed and makes its
 * timing worthless (see the repository's CLAUDE.md).
 */
const pythonCandidates = [
  path.join(repoRoot, '.venv/Scripts/python.exe'),
  path.join(repoRoot, '.venv/bin/python'),
];

function startLoadFeed() {
  const python = pythonCandidates.find((candidate) => existsSync(candidate));
  if (!python) {
    console.error(
      `No interpreter at ${pythonCandidates.join(' or ')}.\n` +
        'Run `uv sync` first, or start the feed yourself and pass --no-feed:\n' +
        '  uv run python viz/load_stream.py',
    );
    process.exit(2);
  }
  const feed = spawn(
    python,
    [
      path.join(repoRoot, 'viz/load_stream.py'),
      '--port',
      String(port),
      '--seconds',
      String(seconds + 10),
    ],
    { cwd: path.join(repoRoot, 'viz'), stdio: ['ignore', 'pipe', 'pipe'] },
  );
  const say = (prefix) => (chunk) =>
    String(chunk)
      .split('\n')
      .filter(Boolean)
      .forEach((line) => console.log(`  [${prefix}] ${line}`));
  feed.stdout.on('data', say('feed'));
  feed.stderr.on('data', say('feed'));
  return feed;
}

/** Wait for the feed's socket to accept a connection. */
async function waitForFeed(deadlineMs = 15_000) {
  const until = Date.now() + deadlineMs;
  while (Date.now() < until) {
    try {
      const response = await fetch(`http://localhost:${port}/scenarios`);
      if (response.ok) return true;
    } catch {
      // Not up yet.
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

const samples = [];
let feed = null;
let browser = null;

try {
  if (startFeed) {
    feed = startLoadFeed();
    if (!(await waitForFeed())) {
      console.error(`The load feed did not come up on port ${port}.`);
      process.exit(2);
    }
  }

  browser = await puppeteer.launch({
    headless: headless ? 'new' : false,
    protocolTimeout: 300_000,
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      `--window-size=${width},${height + 40}`,
      // Never let background or occlusion throttling freeze rAF: the whole
      // measurement depends on an honest frame clock.
      '--disable-backgrounding-occluded-windows',
      '--disable-renderer-backgrounding',
      '--disable-background-timer-throttling',
    ],
  });
  const page = await browser.newPage();
  await page.setViewport({ width, height });
  page.on('pageerror', (error) => console.log(`  [page] ${error.message}`));
  await page.goto(`${url}/?mode=headsup`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => !!window.__godsEyeView?.viewer, {
    timeout: 120_000,
  });
  // The startup flight, the first tiles, and every deferred subsystem.
  await new Promise((resolve) => setTimeout(resolve, 15_000));

  const renderer = await page.evaluate(() => {
    const gl = window.__godsEyeView.viewer.scene.context._gl;
    const info = gl.getExtension('WEBGL_debug_renderer_info');
    return info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : 'unknown';
  });
  const software = /swiftshader|software|llvmpipe/i.test(renderer);
  console.log(`\nRenderer: ${renderer}`);
  if (software)
    console.log(
      'This is a software rasterizer. The relative numbers below are still\n' +
        'worth reading; the absolute fps is not a claim about real hardware.',
    );

  // Park the camera over the whole lattice, so all 1,000 vehicles are in
  // front of the camera rather than most of them behind it.
  await page.evaluate(() => {
    const camera = window.__godsEyeView.viewer.camera;
    // The page is a module build: there is no `Cesium` global to reach for.
    // The one type setView needs is the class of the camera's own position.
    const Cartesian3 = camera.position.constructor;
    camera.cancelFlight();
    camera.setView({
      destination: Cartesian3.fromDegrees(-97.743, 30.2655, 2400),
      orientation: { heading: 0, pitch: (-55 * Math.PI) / 180, roll: 0 },
    });
  });

  // The fleet has to be on screen before anything is timed.
  const arrived = await page
    .waitForFunction(
      (expected) => {
        const layer =
          window.__godsEyeView.dataManager?.layers?.get('headsup')?.module;
        return (layer?.getStats?.().vehicles ?? 0) >= expected ? true : false;
      },
      { timeout: 60_000, polling: 500 },
      EXPECT_VEHICLES,
    )
    .then(() => true)
    .catch(() => false);
  const reached = await page.evaluate(
    () =>
      window.__godsEyeView.dataManager?.layers
        ?.get('headsup')
        ?.module?.getStats?.() ?? {},
  );
  if (!arrived)
    console.log(
      `\nOnly ${reached.vehicles ?? 0} of ${EXPECT_VEHICLES} vehicles arrived; ` +
        'measuring what did.',
    );
  console.log(`Viewport: ${width}x${height}`);
  console.log(
    `Feed: ${reached.endpoint ?? 'unknown'}` +
      `${reached.fallback ? ' (fallback)' : ''}`,
  );

  // The fork's own readout, turned on the way a person turns it on.
  await page.evaluate(() => {
    document.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: '`',
        code: 'Backquote',
        bubbles: true,
      }),
    );
  });
  const readoutVisible = await page.evaluate(() => {
    const readout = document.querySelector('.frame-rate-readout');
    return !!readout && !readout.hidden;
  });
  if (!readoutVisible) {
    console.error(
      'The fork`s fps readout did not open; there is nothing to record.',
    );
    process.exit(2);
  }

  console.log(
    `\nRecording ${seconds}s from the HUD readout ` +
      `(budget: ${TARGET_FPS} fps, Gate 1 floor: ${FLOOR_FPS})\n`,
  );
  console.log(
    '   t      fps   vehicles  trails  advisories  declined  oriented',
  );
  for (let second = 1; second <= seconds; second++) {
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    const row = await page.evaluate(() => {
      const text =
        document.querySelector('.frame-rate-readout')?.textContent ?? '';
      const layer =
        window.__godsEyeView.dataManager?.layers?.get('headsup')?.module;
      const stats = layer?.getStats?.() ?? {};
      return {
        // "FPS 58" from the readout itself, or null while it says "FPS —".
        fps: Number.parseInt(text.replace(/[^0-9]/g, ''), 10),
        vehicles: stats.vehicles ?? 0,
        trails: stats.trails ?? 0,
        advisories: stats.advisories ?? 0,
        declined: stats.declined ?? 0,
        oriented: stats.oriented === true,
      };
    });
    if (Number.isFinite(row.fps)) samples.push(row);
    console.log(
      `  ${String(second).padStart(3)}s  ${String(
        Number.isFinite(row.fps) ? row.fps : '—',
      ).padStart(5)}  ${String(row.vehicles).padStart(8)}  ${String(
        row.trails,
      ).padStart(6)}  ${String(row.advisories).padStart(10)}  ${String(
        row.declined,
      ).padStart(8)}  ${row.oriented ? 'yes' : 'no'}`,
    );
  }
} finally {
  await browser?.close();
  // The feed stops itself at the end of its run; this is for the early exits.
  feed?.kill();
}

if (!samples.length) {
  console.error('\nNo fps samples were recorded.');
  process.exit(1);
}

const fps = samples.map((row) => row.fps).sort((a, b) => a - b);
const at = (quantile) =>
  fps[Math.min(fps.length - 1, Math.floor(quantile * fps.length))];
const mean = fps.reduce((total, value) => total + value, 0) / fps.length;
const peakVehicles = Math.max(...samples.map((row) => row.vehicles));
const peakTrails = Math.max(...samples.map((row) => row.trails));

console.log('\n── Result ──────────────────────────────────────────────');
console.log(`  samples      ${fps.length}`);
console.log(`  min / p5     ${fps[0]} / ${at(0.05)} fps`);
console.log(`  median       ${at(0.5)} fps`);
console.log(`  mean         ${mean.toFixed(1)} fps`);
console.log(`  max          ${fps.at(-1)} fps`);
console.log(`  vehicles     ${peakVehicles} (expected ${EXPECT_VEHICLES})`);
console.log(`  trails       ${peakTrails} (expected ${EXPECT_TRAILS})`);
console.log(`  viewport     ${width}x${height}`);

// The p5 rather than the minimum: one stalled second during a tile load is
// not the steady state the budget is about, and the minimum would make the
// whole measurement hostage to it.
const floorHeld = at(0.05) >= FLOOR_FPS;
const targetHeld = at(0.05) >= TARGET_FPS;
console.log(
  `\n  ${targetHeld ? 'PASS' : floorHeld ? 'PASS (floor)' : 'FAIL'} — ` +
    `p5 ${at(0.05)} fps against a ${TARGET_FPS} fps budget and a ` +
    `${FLOOR_FPS} fps floor`,
);
if (peakVehicles < EXPECT_VEHICLES)
  console.log(
    `  NOTE the fleet never reached ${EXPECT_VEHICLES}; this is not the load case.`,
  );
process.exit(floorHeld && peakVehicles >= EXPECT_VEHICLES ? 0 : 1);
