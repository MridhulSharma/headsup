import assert from 'node:assert/strict';
import test from 'node:test';
import * as Cesium from 'cesium';
import { createHeadsupLayer } from './index.js';
import { createHeadsupViewerSource } from './source.js';
import { GHOST_VEHICLE_COLOR } from './model.js';

const ring = [
  [-97.75, 30.26],
  [-97.74, 30.26],
  [-97.74, 30.27],
  [-97.75, 30.27],
  [-97.75, 30.26],
];

const advisoryRow = (overrides = {}) => ({
  id: 'adv-1',
  version: 1,
  geometryType: 'Polygon',
  polygons: [[ring]],
  lines: [],
  anchors: [[-97.745, 30.265]],
  hazardClass: 'FLOOD',
  severity: 4,
  confidence: 0.8,
  action: 'NO_ENTRY',
  validFrom: 0,
  validUntil: 10_000,
  evidenceCount: 2,
  policyRule: 'flood.no_entry',
  issuer: 'headsup/compiler',
  signed: true,
  ...overrides,
});

const vehicleRow = (overrides = {}) => ({
  id: 'veh-1',
  op: 'op_a',
  lon: -97.7431,
  lat: 30.2672,
  heading: 90,
  ...overrides,
});

/** One layer over fake scene owners the test can inspect and drive. */
function harness({ clock = { ms: 1_000 }, onReset = () => {} } = {}) {
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
  const overlay = { entries: [], visible: [], cleared: 0 };
  const renderRequests = [];
  const holds = [];
  let listener = null;
  let connects = 0;
  let closes = 0;
  const layer = createHeadsupLayer({
    source: {
      subscribe(fn) {
        listener = fn;
        return () => {
          if (listener === fn) listener = null;
        };
      },
      connect: () => connects++,
      close: () => closes++,
      getSnapshot: async () => ({ vehicles: [], advisories: [] }),
      getStatus: () => ({ connected: true }),
      reset: onReset,
    },
    overlayHost: {
      setEntries(...args) {
        overlay.entries.push(args);
      },
      setVisible(_id, value) {
        overlay.visible.push(value);
      },
      clearSource() {
        overlay.cleared++;
      },
    },
    services: {
      render: {
        governorRequestRender: (reason) => renderRequests.push(reason),
        holdContinuousRender: (id) => holds.push(`hold:${id}`),
        releaseContinuousRender: (id) => holds.push(`release:${id}`),
      },
    },
    now: () => clock.ms,
  });
  layer.init(viewer);
  return {
    layer,
    viewer,
    primitives,
    dataSources,
    overlay,
    renderRequests,
    holds,
    clock,
    counts: () => ({ connects, closes }),
    /** The handle the transport holds, captured so a stale one can be used. */
    handle: () => listener,
    subscribed: () => listener !== null,
    frame: () => {
      for (const run of frames) run();
    },
    framesInstalled: () => frames.size,
  };
}

const fleetFrame = {
  vehicles: [vehicleRow()],
  advisories: [advisoryRow()],
};

test('a frame handed to a released subscription cannot publish', () => {
  for (const action of ['disable', 'destroy']) {
    const h = harness();
    h.layer.enable(h.viewer);
    const stale = h.handle();
    h.layer[action](h.viewer);
    // The layer gives the handle back, so the transport cannot reach it even
    // if it kept a reference; the handle itself also refuses the work.
    assert.equal(h.subscribed(), false);
    assert.equal(h.framesInstalled(), 0);
    stale(fleetFrame);
    assert.equal(h.layer.getStats().vehicles, 0);
    assert.equal(h.layer.getStats().advisories, 0);
    h.layer.destroy(h.viewer);
  }
});

test('re-enabling takes a fresh subscription and draws again', () => {
  const h = harness();
  h.layer.enable(h.viewer);
  h.handle()(fleetFrame);
  assert.equal(h.layer.getStats().vehicles, 1);
  h.layer.disable(h.viewer);
  assert.equal(h.layer.getStats().vehicles, 0);
  h.layer.enable(h.viewer);
  assert.equal(h.subscribed(), true);
  h.handle()(fleetFrame);
  assert.equal(h.layer.getStats().vehicles, 1);
  assert.deepEqual(h.counts(), { connects: 2, closes: 1 });
  h.layer.destroy(h.viewer);
});

test('two displays own separate collections and destruction', () => {
  const a = harness();
  const b = harness();
  a.layer.enable(a.viewer);
  b.layer.enable(b.viewer);
  a.handle()(fleetFrame);
  b.handle()({ vehicles: [], advisories: [] });
  assert.equal(a.layer.getStats().vehicles, 1);
  assert.equal(a.layer.getStats().advisories, 1);
  assert.equal(b.layer.getStats().count, 0);
  // Five collections: the vehicle dots, the heading chevrons, the deflection
  // trails, the dispatch beams and the remote-assist markers.
  assert.equal(a.primitives.length, 5);
  assert.equal(a.dataSources.length, 1);

  a.layer.destroy(a.viewer);
  assert.equal(a.primitives.length, 0);
  assert.equal(a.dataSources.length, 0);
  assert.equal(b.primitives.length, 5);
  assert.equal(b.dataSources.length, 1);
  b.layer.destroy(b.viewer);
});

test('enable holds the render loop and disable releases it', () => {
  const h = harness();
  h.layer.enable(h.viewer);
  assert.equal(h.framesInstalled(), 1);
  h.layer.disable(h.viewer);
  assert.deepEqual(h.holds, ['hold:headsup', 'release:headsup']);
  assert.deepEqual(h.counts(), { connects: 1, closes: 1 });
  h.layer.destroy(h.viewer);
});

test('a vehicle moves between the two positions the feed reported', () => {
  const clock = { ms: 1_000 };
  const h = harness({ clock });
  h.layer.enable(h.viewer);
  h.handle()({ vehicles: [vehicleRow({ lon: -97.75, lat: 30.26 })] });
  clock.ms = 1_500;
  h.handle()({ vehicles: [vehicleRow({ lon: -97.74, lat: 30.27 })] });

  // Halfway through the 500 ms the feed itself took between samples.
  clock.ms = 1_750;
  h.frame();
  const half = h.layer.getAnalystRecords()[0];
  assert.ok(half.lon > -97.75 && half.lon < -97.74, `lon ${half.lon}`);
  assert.ok(half.lat > 30.26 && half.lat < 30.27, `lat ${half.lat}`);

  // At the end of the leg it rests on the reported position, never past it.
  clock.ms = 2_000;
  h.frame();
  assert.deepEqual(
    [h.layer.getAnalystRecords()[0].lon, h.layer.getAnalystRecords()[0].lat],
    [-97.74, 30.27],
  );
  clock.ms = 5_000;
  h.frame();
  assert.deepEqual(
    [h.layer.getAnalystRecords()[0].lon, h.layer.getAnalystRecords()[0].lat],
    [-97.74, 30.27],
  );
  h.layer.destroy(h.viewer);
});

test('an expired advisory stops being drawn without a new frame', () => {
  const clock = { ms: 1_000 };
  const h = harness({ clock });
  h.layer.enable(h.viewer);
  h.handle()({
    vehicles: [],
    advisories: [advisoryRow({ validUntil: 2_000 })],
  });
  assert.equal(h.layer.getStats().advisories, 1);
  clock.ms = 2_001;
  h.frame();
  assert.equal(h.layer.getStats().advisories, 0);
  h.layer.destroy(h.viewer);
});

test('a hazard area is draped, not a geometry with a height', () => {
  const h = harness();
  h.layer.enable(h.viewer);
  h.handle()({ vehicles: [], advisories: [advisoryRow()] });
  const entity = h.dataSources[0].entities.values.find((item) => item.polygon);
  const now = Cesium.JulianDate.now();
  const value = (property) => property?.getValue(now);

  // Cesium draws a polygon as a GroundPrimitive only while it has no height
  // of its own. A `heightReference` here earns the oneTimeWarning
  // "heightReference must also have a defined height … will be ignored",
  // which is a warning for geometry that then drapes by accident.
  assert.equal(value(entity.polygon.height), undefined);
  assert.equal(value(entity.polygon.extrudedHeight), undefined);
  assert.equal(value(entity.polygon.perPositionHeight), undefined);
  assert.equal(value(entity.polygon.heightReference), undefined);
  // BOTH, so an advisory drapes over the photoreal 3D tiles and over terrain.
  assert.equal(
    value(entity.polygon.classificationType),
    Cesium.ClassificationType.BOTH,
  );
  assert.equal(value(entity.polyline.clampToGround), true);
  assert.equal(
    value(entity.polyline.classificationType),
    Cesium.ClassificationType.BOTH,
  );
  assert.equal(value(entity.polygon.hierarchy).positions.length, ring.length);
  h.layer.destroy(h.viewer);
});

test('a vehicle the tick no longer reports is retired', () => {
  const h = harness();
  h.layer.enable(h.viewer);
  h.handle()({ vehicles: [vehicleRow(), vehicleRow({ id: 'veh-2' })] });
  assert.equal(h.layer.getStats().vehicles, 2);
  h.handle()({ vehicles: [vehicleRow({ id: 'veh-2' })] });
  assert.deepEqual(
    h.layer.getAnalystRecords().map((record) => record.id),
    ['veh-2'],
  );
  h.layer.destroy(h.viewer);
});

test('operators keep distinct, stable colors', () => {
  const h = harness();
  h.layer.enable(h.viewer);
  h.handle()({
    vehicles: [
      vehicleRow({ id: 'veh-1', op: 'op_a' }),
      vehicleRow({ id: 'veh-2', op: 'op_b' }),
      vehicleRow({ id: 'veh-3', op: 'op_a' }),
    ],
  });
  const legend = h.layer.getOperatorLegend();
  assert.deepEqual(
    legend.map((entry) => [entry.op, entry.vehicles]),
    [
      ['op_a', 2],
      ['op_b', 1],
    ],
  );
  assert.notEqual(legend[0].color, legend[1].color);
  assert.equal(legend[0].color, h.layer.getOperatorLegend()[0].color);
  h.layer.destroy(h.viewer);
});

test('a layer requires a subscription source, a snapshot and an overlay host', () => {
  const overlayHost = { setEntries() {}, setVisible() {}, clearSource() {} };
  const source = { subscribe() {}, getSnapshot: async () => ({}) };
  assert.throws(() => createHeadsupLayer({ overlayHost }), /viewer-socket/);
  assert.throws(
    () => createHeadsupLayer({ source: { subscribe() {} }, overlayHost }),
    /snapshot source/,
  );
  assert.throws(() => createHeadsupLayer({ source }), /overlay host/);
});

test('a malformed frame leaves the last good fleet on screen', () => {
  const sockets = [];
  const source = createHeadsupViewerSource({
    socketFactory: (url) => {
      const socket = {
        url,
        readyState: 1,
        closed: false,
        close() {
          this.closed = true;
        },
      };
      sockets.push(socket);
      return socket;
    },
    now: () => 1_000,
    setTimer: () => null,
    clearTimer: () => {},
  });
  source.connect();
  assert.equal(sockets.length, 1);
  const socket = sockets[0];
  socket.onmessage({
    data: JSON.stringify({ type: 'telemetry', vehicles: [vehicleRow()] }),
  });
  assert.equal(source.getStatus().vehicles.length, 1);

  socket.onmessage({ data: '{not json' });
  socket.onmessage({
    data: JSON.stringify({ type: 'telemetry', vehicles: 3 }),
  });
  const after = source.getStatus();
  assert.equal(after.vehicles.length, 1, 'kept the last good tick');
  assert.equal(after.rejectedFrames, 2);
  assert.match(after.lastError, /Malformed/);

  // A closed subscription keeps no fleet: those positions were only true for
  // their tick, and nothing is reporting them now.
  source.close();
  assert.equal(socket.closed, true);
  assert.equal(socket.onmessage, null);
  assert.deepEqual(source.getStatus().vehicles, []);
});

test('an advisory outlives a reconnect, a vehicle position does not', () => {
  const sockets = [];
  const timers = [];
  const source = createHeadsupViewerSource({
    socketFactory: (url) => {
      const socket = { url, readyState: 1, close() {} };
      sockets.push(socket);
      return socket;
    },
    now: () => 1_000,
    setTimer: (handler) => {
      timers.push(handler);
      return timers.length;
    },
    clearTimer: () => {},
  });
  source.connect();
  const first = sockets[0];
  first.onmessage({
    data: JSON.stringify({
      type: 'advisory',
      feature: {
        type: 'Feature',
        geometry: { type: 'Polygon', coordinates: [ring] },
        properties: {
          id: 'adv-1',
          hazard: { class: 'FLOOD', severity: 3, confidence: 0.7 },
          action: { type: 'NO_ENTRY' },
          valid_from: '2026-10-21T18:00:00Z',
          valid_until: '2026-10-21T20:00:00Z',
          evidence: [{ event_id: 'nws-1' }],
          policy_rule: 'flood.no_entry',
          issuer: 'headsup/compiler',
        },
      },
    }),
  });
  first.onmessage({
    data: JSON.stringify({ type: 'telemetry', vehicles: [vehicleRow()] }),
  });
  assert.equal(source.getStatus().advisories.length, 1);

  // The transport drops: the ladder is armed and a replacement socket opens.
  first.onclose();
  assert.equal(timers.length, 1);
  timers[0]();
  assert.equal(sockets.length, 2);
  const status = source.getStatus();
  assert.equal(status.advisories.length, 1, 'the window still stands');
  assert.equal(status.vehicles.length, 1, 'until a fresh tick replaces it');

  // A frame from the socket the source no longer owns is discarded.
  first.onmessage({
    data: JSON.stringify({ type: 'telemetry', vehicles: [] }),
  });
  assert.equal(source.getStatus().vehicles.length, 1);
  source.close();
});

test('a corridor advisory draws a clamped line, not an area', () => {
  const h = harness();
  h.layer.enable(h.viewer);
  h.handle()({
    vehicles: [],
    advisories: [
      advisoryRow({
        id: 'adv-corridor',
        geometryType: 'LineString',
        polygons: [],
        lines: [
          [
            [-97.75, 30.26],
            [-97.74, 30.265],
          ],
        ],
      }),
    ],
  });
  const entities = h.dataSources[0].entities.values;
  const value = (property) => property?.getValue(Cesium.JulianDate.now());
  assert.equal(entities.length, 1);
  assert.equal(entities[0].polygon, undefined);
  assert.equal(value(entities[0].polyline.clampToGround), true);
  assert.equal(
    value(entities[0].polyline.classificationType),
    Cesium.ClassificationType.BOTH,
  );
  assert.equal(h.layer.getStats().corridors, 1);
  h.layer.destroy(h.viewer);
});

test('hiding a cohort stops drawing it and leaves the count alone', () => {
  const h = harness();
  h.layer.enable(h.viewer);
  h.handle()({
    vehicles: [],
    advisories: [
      advisoryRow(),
      advisoryRow({
        id: 'adv-corridor',
        geometryType: 'LineString',
        polygons: [],
        lines: [
          [
            [-97.75, 30.26],
            [-97.74, 30.265],
          ],
        ],
      }),
    ],
  });
  const entities = h.dataSources[0].entities;
  assert.equal(entities.values.length, 2);
  h.layer.setCohortVisibility({ hazards: false });
  assert.equal(entities.values.length, 1);
  assert.equal(entities.values[0].id, 'headsup-corridor:adv-corridor:0');
  h.layer.setCohortVisibility({ corridors: false });
  assert.equal(entities.values.length, 0);
  // Hiding is a draw decision: both advisories are still in force, so both
  // are still counted and both come straight back.
  assert.equal(h.layer.getStats().advisories, 2);
  h.layer.setCohortVisibility({ hazards: true, corridors: true });
  assert.equal(entities.values.length, 2);
  h.layer.destroy(h.viewer);
});

test('the ghost fleet is one switch, apart from the operator fleets', () => {
  const h = harness();
  h.layer.enable(h.viewer);
  h.handle()({
    vehicles: [
      vehicleRow({ id: 'veh-a', op: 'op_a', reroutedBy: 'adv-1' }),
      vehicleRow({ id: 'veh-g', op: 'op_ghost', ghost: true }),
    ],
    advisories: [],
  });
  assert.deepEqual(
    h.layer.getOperatorLegend().map(({ op, vehicles, rerouted }) => ({
      op,
      vehicles,
      rerouted,
    })),
    [{ op: 'op_a', vehicles: 1, rerouted: 1 }],
  );
  assert.deepEqual(h.layer.getGhostCohort(), {
    color: GHOST_VEHICLE_COLOR,
    vehicles: 1,
    visible: true,
  });
  assert.deepEqual(
    h.layer.getStats().ghosts + h.layer.getStats().rerouted * 10,
    11,
  );
  const collection = h.primitives[0];
  const shown = () =>
    Array.from(
      { length: collection.length },
      (_, index) => collection.get(index).show,
    );
  h.layer.setCohortVisibility({ ghost: false });
  assert.deepEqual(shown(), [true, false]);
  h.layer.setCohortVisibility({ operators: { op_a: false }, ghost: true });
  assert.deepEqual(shown(), [false, true]);
  h.layer.destroy(h.viewer);
});

test('an operator back at the default is forgotten, not stored forever', () => {
  const h = harness();
  h.layer.enable(h.viewer);
  h.layer.setCohortVisibility({ operators: { op_a: false } });
  assert.deepEqual(h.layer.getCohortVisibility().operators, { op_a: false });
  h.layer.setCohortVisibility({ operators: { op_a: true } });
  assert.deepEqual(h.layer.getCohortVisibility().operators, {});
  h.layer.destroy(h.viewer);
});

test('clearing drops the picture and asks the feed to forget it too', () => {
  const resets = [];
  const h = harness({ onReset: () => resets.push(1) });
  h.layer.enable(h.viewer);
  h.handle()(fleetFrame);
  assert.equal(h.layer.getStats().vehicles, 1);
  assert.equal(h.layer.getStats().advisories, 1);
  h.layer.clearDrawn();
  assert.deepEqual(
    {
      vehicles: h.layer.getStats().vehicles,
      advisories: h.layer.getStats().advisories,
      resets: resets.length,
      stillSubscribed: h.subscribed(),
    },
    { vehicles: 0, advisories: 0, resets: 1, stillSubscribed: true },
  );
  h.layer.destroy(h.viewer);
});

// ── Remote-assist markers, and the clusters triage makes of them ─────────

const assistRow = (overrides = {}) => ({
  id: 'asr-1',
  op: 'op_ghost',
  lon: -98.396,
  lat: 29.5228,
  ghost: true,
  state: 'QUEUED',
  cluster: null,
  openedAt: null,
  ...overrides,
});

/** The assist marker collection, which `init` adds fifth. */
const assistCollection = (h) => h.primitives[4];

test('one assist marker per request, and one per cluster when triage groups them', () => {
  const h = harness();
  h.layer.enable(h.viewer);
  h.handle()({
    vehicles: [],
    advisories: [],
    pipeline: {
      acks: [],
      assists: [
        assistRow({ id: 'asr-1', lon: -98.396 }),
        assistRow({ id: 'asr-2', lon: -98.3955 }),
        assistRow({ id: 'asr-3', lon: -98.395 }),
      ],
    },
  });
  // Nothing clustered yet: three requests, three rings.
  assert.equal(assistCollection(h).length, 3);
  assert.deepEqual(
    [h.layer.getStats().assists, h.layer.getStats().assistClusters],
    [3, 0],
  );

  // Triage groups them, and the three rings collapse into one.
  h.handle()({
    vehicles: [],
    advisories: [],
    pipeline: {
      acks: [],
      assists: [
        assistRow({ id: 'asr-1', lon: -98.396, cluster: 'trg-1' }),
        assistRow({ id: 'asr-2', lon: -98.3955, cluster: 'trg-1' }),
        assistRow({ id: 'asr-3', lon: -98.395, cluster: 'trg-1' }),
      ],
    },
  });
  assert.equal(assistCollection(h).length, 1);
  assert.deepEqual(
    [
      h.layer.getStats().assists,
      h.layer.getStats().assistMarkers,
      h.layer.getStats().assistClusters,
    ],
    // Three requests still, drawn as one marker, which is one cluster.
    [3, 1, 1],
  );

  // A request a human cleared leaves the screen.
  h.handle()({
    vehicles: [],
    advisories: [],
    pipeline: { acks: [], assists: [] },
  });
  assert.equal(assistCollection(h).length, 0);
  assert.equal(h.layer.getStats().assists, 0);
  h.layer.destroy(h.viewer);
});

test('a collapsed cluster carries a count badge, a single request does not', () => {
  const h = harness();
  h.layer.enable(h.viewer);
  h.layer.setAssistLabel((count) => `WAITING ${count}`);
  const badges = () =>
    (h.overlay.entries.at(-1)?.[1] ?? [])
      .filter((entry) => entry.id.startsWith('assist:'))
      .map((entry) => entry.title);
  h.handle()({
    vehicles: [],
    advisories: [],
    pipeline: { acks: [], assists: [assistRow()] },
  });
  // One request needs no number: "x1" would be noise with a count in it.
  assert.deepEqual(badges(), []);
  h.handle()({
    vehicles: [],
    advisories: [],
    pipeline: {
      acks: [],
      assists: [
        assistRow({ id: 'asr-1', cluster: 'trg-1' }),
        assistRow({ id: 'asr-2', lon: -98.3955, cluster: 'trg-1' }),
      ],
    },
  });
  assert.deepEqual(badges(), ['WAITING 2']);
  h.layer.destroy(h.viewer);
});

test('hiding the ghost fleet hides the queue it is responsible for', () => {
  const h = harness();
  h.layer.enable(h.viewer);
  h.handle()({
    vehicles: [],
    advisories: [],
    pipeline: {
      acks: [],
      assists: [
        assistRow({ id: 'asr-ghost', ghost: true }),
        assistRow({ id: 'asr-live', op: 'op_a', ghost: false, lon: -98.3955 }),
      ],
    },
  });
  const collection = assistCollection(h);
  const shown = () =>
    Array.from(
      { length: collection.length },
      (_, index) => collection.get(index).show,
    );
  // Markers are held in key order, so the ghost request is the first.
  assert.deepEqual(shown(), [true, true]);
  h.layer.setCohortVisibility({ ghost: false });
  // The ghost request goes; the advised fleet's own stays.
  assert.deepEqual(shown(), [false, true]);
  h.layer.setCohortVisibility({ ghost: true, operators: { op_a: false } });
  assert.deepEqual(shown(), [true, false]);
  h.layer.destroy(h.viewer);
});

test('a KEEP_CLEAR corridor is drawn with a halo under it, other lines are not', () => {
  const h = harness();
  h.layer.enable(h.viewer);
  const corridor = (action) =>
    advisoryRow({
      id: `adv-${action}`,
      geometryType: 'LineString',
      action,
      polygons: [],
      lines: [
        [
          [-97.7399, 30.26712],
          [-97.7372, 30.26714],
        ],
      ],
    });
  h.handle()({
    vehicles: [],
    advisories: [corridor('KEEP_CLEAR'), corridor('SPEED_CAP')],
  });
  const ids = h.dataSources[0].entities.values.map((entity) => entity.id);
  // The glow is a second, wider clamped polyline rather than a glow
  // material: both stay on the colour-attribute path the pulse needs.
  assert.deepEqual(ids, [
    'headsup-corridor:adv-KEEP_CLEAR:0:halo',
    'headsup-corridor:adv-KEEP_CLEAR:0',
    'headsup-corridor:adv-SPEED_CAP:0',
  ]);
  const value = (property) => property?.getValue(Cesium.JulianDate.now());
  const halo = h.dataSources[0].entities.values[0];
  const core = h.dataSources[0].entities.values[1];
  assert.ok(value(halo.polyline.width) > value(core.polyline.width));
  assert.equal(value(halo.polyline.clampToGround), true);
  assert.equal(
    value(halo.polyline.classificationType),
    Cesium.ClassificationType.BOTH,
  );
  // The halo is fainter than the line it sits under, at rest.
  assert.ok(
    value(halo.polyline.material.color).alpha <
      value(core.polyline.material.color).alpha,
  );
  // Both legs of one corridor are still one corridor.
  assert.equal(h.layer.getStats().corridors, 2);
  h.layer.destroy(h.viewer);
});
