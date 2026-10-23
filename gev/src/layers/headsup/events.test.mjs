import assert from 'node:assert/strict';
import test from 'node:test';
import * as Cesium from 'cesium';
import { CHEVRON_FLEET_LIMIT, createHeadsupLayer } from './index.js';
import {
  ACK_FLASH_MS,
  VEHICLE_PIXEL_SIZE,
  operatorColor,
  DEFLECTION_TRAIL_MS,
  DISPATCH_BEAM_MS,
  HAZARD_PULSE_MS,
  PULSE_PEAK_ALPHA,
  beamProgress,
  fleetCentroid,
  hazardFillAlpha,
  hazardLegendRows,
  headingDirection,
  pruneTrail,
  pulseLevel,
} from './model.js';

/**
 * The four gestures, the trails, and the restraint rule that governs them.
 *
 * `ownership.test.mjs` covers what the layer OWNS; this covers what it
 * ANIMATES, which is a different question with a different failure mode: an
 * animation that fires twice, or fires over the landing view, is wrong even
 * though every collection is accounted for.
 */

const ring = [
  [-98.399, 29.52],
  [-98.393, 29.52],
  [-98.393, 29.5255],
  [-98.399, 29.5255],
  [-98.399, 29.52],
];

const advisoryRow = (overrides = {}) => ({
  id: 'adv-1',
  version: 1,
  geometryType: 'Polygon',
  polygons: [[ring]],
  lines: [],
  anchors: [[-98.396, 29.5228]],
  hazardClass: 'FLOOD',
  severity: 4,
  confidence: 0.9,
  action: 'NO_ENTRY',
  validFrom: 0,
  validUntil: 10_000_000,
  evidenceCount: 1,
  policyRule: 'flood.no_entry',
  issuer: 'headsup/compiler',
  signed: true,
  ...overrides,
});

const vehicleRow = (overrides = {}) => ({
  id: 'veh-1',
  op: 'op_a',
  lon: -98.408,
  lat: 29.5228,
  heading: 90,
  ghost: false,
  reroutedBy: null,
  ...overrides,
});

const running = (overrides = {}) => ({
  state: 'running',
  id: 's1_flood',
  code: 'S1',
  name: 'Flash flood',
  speed: 60,
  scripted: true,
  ...overrides,
});

const ack = (operator, overrides = {}) => ({
  operator,
  vehicles: 6,
  status: 'ACCEPTED',
  seq: 1,
  advisoryId: 'adv-1',
  ...overrides,
});

/** One layer over fake scene owners, with the clock under the test's hand. */
function harness({ clock = { ms: 1_000 } } = {}) {
  const primitives = [];
  const dataSources = [];
  const frames = new Set();
  const viewer = {
    scene: {
      sampleHeightSupported: false,
      primitives: {
        add: (value) => primitives.push(value),
        remove: (value) => primitives.splice(primitives.indexOf(value), 1),
      },
      preRender: {
        addEventListener(listener) {
          frames.add(listener);
          return () => frames.delete(listener);
        },
      },
    },
    dataSources: {
      add: (value) => dataSources.push(value),
      remove: (value) => dataSources.splice(dataSources.indexOf(value), 1),
    },
  };
  const overlay = { calls: [] };
  let listener = null;
  const layer = createHeadsupLayer({
    source: {
      subscribe(fn) {
        listener = fn;
        return () => {
          if (listener === fn) listener = null;
        };
      },
      connect: () => {},
      close: () => {},
      reset: () => {},
      getSnapshot: async () => ({ vehicles: [], advisories: [] }),
      getStatus: () => ({ connected: true }),
    },
    overlayHost: {
      setEntries: (id, entries, options) =>
        overlay.calls.push({ entries, options }),
      setVisible: () => {},
      clearSource: () => {},
    },
    services: {
      render: {},
      // `Cesium.Material` compiles against the DOM and cannot be built here
      // at all, so the beams and trails get a stand-in that carries the one
      // thing this suite reads back: the uniforms, and their identity.
      materials: {
        polylineGlow: (uniforms) => ({ uniforms, destroy: () => {} }),
      },
    },
    now: () => clock.ms,
  });
  layer.init(viewer);
  layer.enable(viewer);
  const [points, chevrons, trails, beams] = primitives;
  return {
    layer,
    viewer,
    clock,
    overlay,
    points,
    chevrons,
    trails,
    beams,
    push: (snapshot) => listener(snapshot),
    /** The entities this layer drew, as the scene holds them. */
    entities: () => dataSources[0].entities,
    tick: () => {
      for (const run of frames) run();
    },
    /** The entries of the most recent overlay publication. */
    lastEntries: () => overlay.calls.at(-1)?.entries ?? [],
  };
}

/**
 * One advisory's drawn fill alpha, read off the entity Cesium holds.
 *
 * Through the entity rather than through a hook on the layer: the pulse's
 * whole claim is that it moves the colour of DRAWN geometry, so the drawn
 * geometry is what has to be asked.
 */
const fillAlpha = (h, id = 'adv-1') =>
  h
    .entities()
    .getById(`headsup-advisory:${id}:0`)
    .polygon.material.color.getValue(Cesium.JulianDate.now()).alpha;

/** How many of the drawn dots are mid-flash, by size. */
const flashing = (h, basePixelSize = VEHICLE_PIXEL_SIZE) => {
  let count = 0;
  for (let index = 0; index < h.points.length; index++)
    if (h.points.get(index).pixelSize > basePixelSize) count++;
  return count;
};

// ── The pure gestures ───────────────────────────────────────────────────

test('a gesture is one swell: zero at both ends, one in the middle', () => {
  assert.equal(pulseLevel(0), 0);
  assert.equal(pulseLevel(HAZARD_PULSE_MS), 0);
  assert.equal(pulseLevel(HAZARD_PULSE_MS + 1), 0);
  assert.equal(pulseLevel(-50), 0);
  assert.ok(Math.abs(pulseLevel(HAZARD_PULSE_MS / 2) - 1) < 1e-9);
  assert.ok(pulseLevel(HAZARD_PULSE_MS / 4) > 0.7);
  // The same shape at the flash's own length, so both read as one gesture.
  assert.ok(Math.abs(pulseLevel(ACK_FLASH_MS / 2, ACK_FLASH_MS) - 1) < 1e-9);
});

test('a beam grows, then fades where it arrived', () => {
  // At the instant it is created the beam has gone nowhere; it is advanced
  // from the next frame on, which is why zero reach is not zero alpha.
  assert.equal(beamProgress(0).reach, 0);
  assert.deepEqual(beamProgress(-1), { reach: 0, alpha: 0, done: false });
  const early = beamProgress(DISPATCH_BEAM_MS * 0.2);
  const landed = beamProgress(DISPATCH_BEAM_MS * 0.6);
  const going = beamProgress(DISPATCH_BEAM_MS * 0.8);
  assert.ok(early.reach > 0 && early.reach < 1);
  assert.equal(early.alpha, 1);
  assert.ok(landed.reach > 0.99, 'it has arrived before it starts to fade');
  assert.ok(going.alpha < 1 && going.alpha > 0);
  assert.ok(going.reach >= landed.reach, 'it never retreats');
  assert.deepEqual(beamProgress(DISPATCH_BEAM_MS), {
    reach: 1,
    alpha: 0,
    done: true,
  });
});

test('a heading becomes a unit direction in the tangent plane', () => {
  const at = (lon, lat, heading) =>
    headingDirection(lon, lat, heading, new Cesium.Cartesian3());
  const north = at(0, 0, 0);
  const east = at(0, 0, 90);
  assert.ok(Math.abs(Cesium.Cartesian3.magnitude(north) - 1) < 1e-12);
  assert.ok(Math.abs(Cesium.Cartesian3.magnitude(east) - 1) < 1e-12);
  // On the equator at the prime meridian, north is +Z and east is +Y.
  assert.ok(north.z > 0.999 && Math.abs(north.x) < 1e-12);
  assert.ok(east.y > 0.999 && Math.abs(east.z) < 1e-12);
  // Opposite bearings are opposite vectors, and both are perpendicular to
  // the local up — which is what makes the chevron lie flat on the ground.
  const south = at(0, 0, 180);
  assert.ok(Cesium.Cartesian3.dot(north, south) < -0.999);
  // The ellipsoid's surface normal, not the geocentric radius: the two differ
  // by up to a fifth of a degree, and the chevron lies on the ground.
  const up = Cesium.Ellipsoid.WGS84.geodeticSurfaceNormal(
    Cesium.Cartesian3.fromDegrees(12, 34),
    new Cesium.Cartesian3(),
  );
  assert.ok(Math.abs(Cesium.Cartesian3.dot(at(12, 34, 57), up)) < 1e-9);
});

test('a fleet centroid is the mean of its fleet, and nothing of an empty one', () => {
  assert.equal(fleetCentroid([]), null);
  assert.equal(fleetCentroid([{ lon: Number.NaN, lat: 1, height: 0 }]), null);
  assert.deepEqual(
    fleetCentroid([
      { lon: 0, lat: 0, height: 10 },
      { lon: 2, lat: 4, height: 30 },
    ]),
    { lon: 1, lat: 2, height: 20 },
  );
});

test('a trail keeps its window and drops what left it', () => {
  const samples = [{ at: 0 }, { at: 10_000 }, { at: 29_000 }];
  pruneTrail(samples, DEFLECTION_TRAIL_MS);
  assert.deepEqual(samples, [{ at: 0 }, { at: 10_000 }, { at: 29_000 }]);
  pruneTrail(samples, DEFLECTION_TRAIL_MS + 10_001);
  assert.deepEqual(samples, [{ at: 29_000 }]);
});

test('the legend names the classes drawn, counted, in a stable order', () => {
  assert.deepEqual(hazardLegendRows([]), []);
  assert.deepEqual(
    hazardLegendRows([
      { hazardClass: 'WORK_ZONE' },
      { hazardClass: 'FLOOD' },
      { hazardClass: 'FLOOD' },
    ]),
    [
      { hazardClass: 'FLOOD', color: '#2bb3ff', advisories: 2 },
      { hazardClass: 'WORK_ZONE', color: '#ff8c1a', advisories: 1 },
    ],
  );
});

// ── The drawn gestures ──────────────────────────────────────────────────

test('a published advisory pulses once, and only inside its window', () => {
  const clock = { ms: 1_000 };
  const h = harness({ clock });
  // The picture that was already there when the page opened: seeded, not
  // played, so nothing swells over a run already in progress.
  h.push({
    vehicles: [vehicleRow()],
    advisories: [advisoryRow()],
    scenario: running(),
  });
  const steady = hazardFillAlpha(4);
  assert.equal(fillAlpha(h), steady, 'the advisory that was already in force');

  clock.ms = 2_000;
  h.push({
    vehicles: [vehicleRow()],
    advisories: [advisoryRow(), advisoryRow({ id: 'adv-2' })],
    scenario: running(),
  });
  clock.ms = 2_000 + HAZARD_PULSE_MS / 4;
  assert.ok(fillAlpha(h, 'adv-2') > steady, 'the newly published one swells');
  assert.equal(fillAlpha(h), steady, 'and the older one does not');

  clock.ms = 2_000 + HAZARD_PULSE_MS / 2;
  assert.ok(
    Math.abs(fillAlpha(h, 'adv-2') - (steady + PULSE_PEAK_ALPHA)) < 1e-9,
    'at the peak it is the steady fill plus the pulse',
  );

  clock.ms = 2_000 + HAZARD_PULSE_MS + 1;
  h.tick();
  assert.equal(fillAlpha(h, 'adv-2'), steady, 'and then it is over');
  h.layer.destroy(h.viewer);
});

test('nothing animates over the landing view', () => {
  const clock = { ms: 1_000 };
  const h = harness({ clock });
  // The idle feed publishes the advisory that is in force where the camera
  // is parked. It is context, not news.
  h.push({ vehicles: [vehicleRow()], advisories: [], scenario: null });
  clock.ms = 2_000;
  h.push({
    vehicles: [vehicleRow()],
    advisories: [advisoryRow()],
    scenario: { state: 'idle' },
    pipeline: { acks: [ack('op_a')] },
  });
  assert.equal(fillAlpha(h), hazardFillAlpha(4), 'no pulse');
  assert.equal(h.beams.length, 0, 'no beam');
  assert.equal(flashing(h), 0, 'no flash');
  h.layer.destroy(h.viewer);
});

test('a publication beams once to each fleet, never to the ghosts', () => {
  const clock = { ms: 1_000 };
  const h = harness({ clock });
  const fleet = [
    vehicleRow({ id: 'a-1', op: 'op_a' }),
    vehicleRow({ id: 'a-2', op: 'op_a', lon: -98.404 }),
    vehicleRow({ id: 'b-1', op: 'op_b' }),
    vehicleRow({ id: 'g-1', op: 'op_ghost', ghost: true }),
  ];
  h.push({ vehicles: fleet, advisories: [], scenario: running() });
  assert.equal(h.beams.length, 0);

  clock.ms = 2_000;
  h.push({ vehicles: fleet, advisories: [advisoryRow()], scenario: running() });
  assert.equal(h.beams.length, 2, 'one per advised operator');
  assert.deepEqual(
    [h.beams.get(0).id, h.beams.get(1).id].sort(),
    ['headsup-beam:adv-1:op_a', 'headsup-beam:adv-1:op_b'],
    'and none for the control fleet',
  );

  // The same advisory arriving again is not a second publication.
  clock.ms = 2_500;
  h.push({ vehicles: fleet, advisories: [advisoryRow()], scenario: running() });
  assert.equal(h.beams.length, 2);

  clock.ms = 2_000 + DISPATCH_BEAM_MS + 1;
  h.tick();
  assert.equal(h.beams.length, 0, 'and it is gone when its window closes');
  h.layer.destroy(h.viewer);
});

test('an answer flashes that fleet once, in its own colour', () => {
  const clock = { ms: 1_000 };
  const h = harness({ clock });
  const fleet = [
    vehicleRow({ id: 'a-1', op: 'op_a' }),
    vehicleRow({ id: 'b-1', op: 'op_b' }),
  ];
  const frame = (acks) => ({
    vehicles: fleet,
    advisories: [advisoryRow()],
    scenario: running(),
    pipeline: { acks },
  });
  h.push(frame([]));
  clock.ms = 2_000;
  h.push(frame([ack('op_a')]));
  clock.ms = 2_000 + ACK_FLASH_MS / 2;
  h.tick();
  assert.equal(flashing(h), 1, 'one fleet, and only the one that answered');
  const flashed = h.points.get(0);
  const quiet = h.points.get(1);
  assert.ok(flashed.pixelSize > quiet.pixelSize, 'the answering fleet swells');
  // In the operator's colour: at the peak it is that colour half way to
  // white, which is brighter without being a different hue.
  const peak = Cesium.Color.lerp(
    operatorColor('op_a'),
    Cesium.Color.WHITE,
    0.5,
    new Cesium.Color(),
  );
  assert.ok(Cesium.Color.equals(flashed.color, peak), 'op_a stays op_a');

  // The same answer, re-sent in the next snapshot, is still one answer.
  h.push(frame([ack('op_a')]));
  h.tick();
  assert.equal(flashing(h), 1, 'still the one answer, not a second');

  clock.ms = 2_000 + ACK_FLASH_MS + 1;
  h.tick();
  h.tick();
  assert.equal(flashing(h), 0);
  assert.equal(h.points.get(0).pixelSize, quiet.pixelSize, 'and it settles');

  // A SECOND answer from the same fleet is a second event.
  clock.ms = 3_000;
  h.push(frame([ack('op_a', { seq: 7 })]));
  clock.ms = 3_000 + ACK_FLASH_MS / 2;
  h.tick();
  assert.equal(flashing(h), 1);
  h.layer.destroy(h.viewer);
});

test('a refusal is tagged on the fleet that refused, and withdrawn with it', () => {
  const clock = { ms: 1_000 };
  const h = harness({ clock });
  const fleet = [
    vehicleRow({ id: 'a-1', op: 'op_a' }),
    vehicleRow({ id: 'c-1', op: 'op_c', lon: -98.39 }),
  ];
  const frame = (acks) => ({
    vehicles: fleet,
    advisories: [advisoryRow()],
    scenario: running(),
    pipeline: { acks },
  });
  h.push(frame([ack('op_a')]));
  assert.deepEqual(
    h.lastEntries().map((entry) => entry.id),
    ['adv-1'],
    'an accepted advisory is labelled, and nothing else',
  );

  clock.ms = 2_000;
  h.push(frame([ack('op_a'), ack('op_c', { status: 'DECLINED', seq: 2 })]));
  const tag = h.lastEntries().find((entry) => entry.id === 'declined:op_c');
  assert.ok(tag, 'the refusing fleet carries a tag');
  assert.equal(tag.accent, '#ef476f', 'in the one red on this screen');
  assert.equal(h.layer.getStats().declined, 1);
  assert.equal(
    h.overlay.calls.at(-1).options.moving,
    true,
    'and the host is told the tag rides on a moving fleet',
  );

  // The same fleet accepting later takes its own tag away.
  clock.ms = 3_000;
  h.push(frame([ack('op_a'), ack('op_c', { seq: 3 })]));
  assert.equal(
    h.lastEntries().some((entry) => entry.id === 'declined:op_c'),
    false,
  );
  assert.equal(h.layer.getStats().declined, 0);
  h.layer.destroy(h.viewer);
});

test('the refusal tag is drawn in the words the console gives it', () => {
  const h = harness();
  h.layer.setDeclinedLabel('RECHAZADO');
  h.push({
    vehicles: [vehicleRow({ op: 'op_c' })],
    advisories: [advisoryRow()],
    scenario: running(),
    pipeline: { acks: [ack('op_c', { status: 'DECLINED' })] },
  });
  const tag = h.lastEntries().find((entry) => entry.id === 'declined:op_c');
  assert.equal(tag.title, 'RECHAZADO');
  h.layer.destroy(h.viewer);
});

test('a rerouted vehicle leaves a trail and a ghost leaves none', () => {
  const clock = { ms: 1_000 };
  const h = harness({ clock });
  const fleet = (reroutedBy) => [
    vehicleRow({ id: 'a-1', op: 'op_a', reroutedBy }),
    vehicleRow({ id: 'g-1', op: 'op_ghost', ghost: true, reroutedBy }),
  ];
  h.push({ vehicles: fleet('adv-1'), advisories: [], scenario: running() });
  // The first tick of a vehicle has nothing to draw a line between.
  assert.equal(h.trails.length, 0);

  for (let step = 1; step <= 3; step++) {
    clock.ms = 1_000 + step * 500;
    h.push({
      vehicles: fleet('adv-1').map((vehicle) => ({
        ...vehicle,
        lon: -98.408 + step * 0.002,
      })),
      advisories: [],
      scenario: running(),
    });
  }
  assert.equal(h.trails.length, 1, 'the advised vehicle only');
  assert.equal(h.trails.get(0).id, 'headsup-trail:a-1');
  assert.equal(h.layer.getStats().trails, 1);
  assert.ok(h.trails.get(0).positions.length >= 2);

  // A reroute that is withdrawn stops growing, and the window drains it.
  clock.ms = 3_000;
  h.push({ vehicles: fleet(null), advisories: [], scenario: running() });
  assert.equal(h.trails.length, 1, 'it fades rather than vanishing');
  clock.ms = 3_000 + DEFLECTION_TRAIL_MS + 1;
  h.push({ vehicles: fleet(null), advisories: [], scenario: running() });
  assert.equal(h.trails.length, 0);
  h.layer.destroy(h.viewer);
});

test('trails of one fleet carry one set of uniforms, so they batch', () => {
  const clock = { ms: 1_000 };
  const h = harness({ clock });
  const fleet = (step) =>
    Array.from({ length: 12 }, (_, index) => ({
      ...vehicleRow({
        id: `v-${index}`,
        op: index % 2 ? 'op_a' : 'op_b',
        reroutedBy: 'adv-1',
      }),
      lon: -98.408 + step * 0.002 + index * 1e-4,
    }));
  for (let step = 0; step < 3; step++) {
    clock.ms = 1_000 + step * 500;
    h.push({ vehicles: fleet(step), advisories: [], scenario: running() });
  }
  assert.equal(h.trails.length, 12);
  // A PolylineCollection merges consecutive polylines whose material uniforms
  // have EQUAL VALUES into one draw command, so two operators' trails are two
  // commands rather than twelve. The instances must still be distinct:
  // removing a polyline destroys its material.
  const instances = new Set();
  const colours = new Set();
  for (let index = 0; index < h.trails.length; index++) {
    const { material } = h.trails.get(index);
    instances.add(material);
    colours.add(material.uniforms.color.toCssColorString());
  }
  assert.equal(instances.size, 12, 'never a material two polylines can free');
  assert.equal(colours.size, 2, 'one colour per operator');
  h.layer.destroy(h.viewer);
});

test('a fleet too large to orient is drawn as dots alone', () => {
  const h = harness();
  const fleet = (count) =>
    Array.from({ length: count }, (_, index) =>
      vehicleRow({ id: `v-${index}`, lon: -98.4 + index * 1e-4 }),
    );
  h.push({ vehicles: fleet(3), advisories: [], scenario: running() });
  assert.equal(h.chevrons.length, 3);
  assert.equal(h.chevrons.show, true);
  assert.equal(h.layer.getStats().oriented, true);

  h.push({
    vehicles: fleet(CHEVRON_FLEET_LIMIT + 1),
    advisories: [],
    scenario: running(),
  });
  assert.equal(h.chevrons.show, false);
  assert.equal(
    h.chevrons.length,
    0,
    'the chevrons are not merely hidden, they are not built',
  );
  assert.equal(h.layer.getStats().oriented, false);
  assert.equal(h.layer.getStats().vehicles, CHEVRON_FLEET_LIMIT + 1);

  h.push({ vehicles: fleet(2), advisories: [], scenario: running() });
  assert.equal(h.chevrons.length, 2, 'and they come back under the limit');
  h.layer.destroy(h.viewer);
});

test('a chevron carries the bearing its vehicle reported', () => {
  const clock = { ms: 1_000 };
  const h = harness({ clock });
  h.push({
    vehicles: [vehicleRow({ heading: 90 })],
    advisories: [],
    scenario: running(),
  });
  h.tick();
  const chevron = h.chevrons.get(0);
  const expected = headingDirection(
    -98.408,
    29.5228,
    90,
    new Cesium.Cartesian3(),
  );
  assert.ok(
    Cesium.Cartesian3.equalsEpsilon(chevron.alignedAxis, expected, 1e-9),
    'east, as the feed said',
  );
  assert.ok(
    Cesium.Cartesian3.equals(chevron.position, h.points.get(0).position),
    'and it rides on the dot rather than beside it',
  );
  h.layer.destroy(h.viewer);
});

test('ending a run takes every gesture with it', () => {
  const clock = { ms: 1_000 };
  const h = harness({ clock });
  const fleet = [vehicleRow({ reroutedBy: 'adv-1' })];
  h.push({ vehicles: fleet, advisories: [], scenario: running() });
  clock.ms = 1_500;
  h.push({
    vehicles: fleet,
    advisories: [advisoryRow()],
    scenario: running(),
    pipeline: { acks: [ack('op_a', { status: 'DECLINED' })] },
  });
  assert.ok(h.beams.length > 0 && h.trails.length > 0);

  h.layer.clearDrawn();
  assert.equal(h.beams.length, 0);
  assert.equal(h.trails.length, 0);
  assert.equal(h.points.length, 0);
  assert.equal(h.layer.getStats().declined, 0);

  // And the picture that arrives next seeds rather than replaying.
  clock.ms = 2_000;
  h.push({
    vehicles: fleet,
    advisories: [advisoryRow()],
    scenario: running(),
  });
  assert.equal(fillAlpha(h), hazardFillAlpha(4));
  assert.equal(h.beams.length, 0);
  h.layer.destroy(h.viewer);
});
