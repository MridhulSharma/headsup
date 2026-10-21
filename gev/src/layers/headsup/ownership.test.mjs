import assert from 'node:assert/strict';
import test from 'node:test';
import * as Cesium from 'cesium';
import { createHeadsupLayer } from './index.js';
import { createHeadsupViewerSource } from './source.js';

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
  assert.equal(a.primitives.length, 1);
  assert.equal(a.dataSources.length, 1);

  a.layer.destroy(a.viewer);
  assert.equal(a.primitives.length, 0);
  assert.equal(a.dataSources.length, 0);
  assert.equal(b.primitives.length, 1);
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
