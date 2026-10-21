import * as Cesium from 'cesium';
import {
  HEADSUP_OVERLAY_COHORT_LIMIT,
  HEADSUP_OVERLAY_COLLISION_CAPACITY,
  HEADSUP_OVERLAY_SOURCE_ID,
  HEADSUP_RENDER_HOLD_ID,
  VEHICLE_HEIGHT_OFFSET_M,
  VEHICLE_PIXEL_SIZE,
  createAdvisoryOverlayEntry,
  hazardCssColor,
  hazardFillAlpha,
  mapAnalystRecord,
  operatorColor,
  operatorCssColor,
  sampleTrack,
  selectAdvisoryOverlayCohort,
  trackDurationMs,
} from './model.js';
import { advisoryIsLive } from './expiry.js';
export * from './model.js';
export {
  HEADSUP_VIEWER_ENDPOINT,
  createHeadsupViewerSource,
} from './source.js';

/**
 * Heads Up advisories and the fleets they are addressed to.
 *
 * Two surfaces, for two different kinds of data:
 *
 * - **Vehicles** are a `PointPrimitiveCollection`. One dot per vehicle,
 *   colored by operator, its position written per frame from the
 *   interpolator in `model.js` so a 2 Hz feed reads as motion instead of a
 *   twice-a-second jump. Primitives rather than entities because the fleet is
 *   the one thing here that moves every frame.
 * - **Hazard areas** are entity polygons on this layer's own
 *   `CustomDataSource` that Cesium draws as ground primitives — no height, no
 *   `perPositionHeight` — classifying `BOTH`, so an advisory drapes over the
 *   photoreal 3D tiles and over terrain alike. A clamped outline polyline
 *   carries the edge, because a ground polygon cannot outline itself. Static
 *   geometry, rebuilt only when the set of live advisories actually changes,
 *   because draped geometry re-tessellates whenever its positions do.
 *
 * World text goes through the overlay host, never Cesium labels
 * (`gev/docs/CODE-BOUNDARIES.md`).
 * @module layers/headsup
 */

// A portable package (no i18n): the error stays English data and the layer
// panel shows it translated.
const msg = (text) => text;

const ENTITY_PREFIX = 'headsup-advisory';
/** How often the drawn set is re-tested against the advisory windows. */
const PHASE_CHECK_MS = 500;

/**
 * Own one Heads Up display and its subscription.
 * @param {object} options
 * @param {object} options.source Viewer-socket source (`./source.js`).
 * @param {object} options.overlayHost Frozen world-overlay facade.
 * @param {object} [options.services] Application scene owners: `render`
 *   (`governorRequestRender`, `holdContinuousRender`,
 *   `releaseContinuousRender`).
 * @param {() => number} [options.now] Clock seam, for tests.
 * @returns {object} Data-layer module.
 */
export function createHeadsupLayer({
  source,
  overlayHost,
  services = {},
  now = () => Date.now(),
} = {}) {
  if (typeof source?.subscribe !== 'function')
    throw new TypeError('Heads Up requires a viewer-socket source');
  if (typeof source?.getSnapshot !== 'function')
    throw new TypeError('Heads Up requires a snapshot source');
  if (!overlayHost) throw new TypeError('Heads Up requires an overlay host');
  const {
    governorRequestRender = () => {},
    holdContinuousRender = () => {},
    releaseContinuousRender = () => {},
  } = services.render ?? {};
  let _viewer = null;
  let _points = null;
  let _dataSource = null;
  /** @type {Map<string, {point: object, op: string, track: object}>} */
  const _markers = new Map();
  /** @type {object[]} */
  let _advisories = [];
  let _drawnSignature = '';
  let _drawnAdvisories = 0;
  let _lastPhaseCheck = 0;
  let _lastUpdate = null;
  let _lastError = null;
  let _enabled = false;
  let _unsubscribe = null;
  let _removePreRender = null;

  /**
   * Ground height for a vehicle dot, from whatever surface is loaded.
   *
   * Our own collection is excluded from the sample: the dot sits two metres
   * above the ground it reports, so sampling a pixel it already occupies
   * would read its own height back and walk it two metres higher per tick.
   * Downtown Austin is ~150 m above the ellipsoid, so a dot at height 0
   * would be underground — the fallback keeps the last known ground until a
   * tile loads, and `disableDepthTestDistance` keeps it visible meanwhile.
   */
  const groundHeight = (lon, lat, fallback) => {
    const scene = _viewer?.scene;
    const floor = Number.isFinite(fallback)
      ? fallback
      : VEHICLE_HEIGHT_OFFSET_M;
    // sampleHeight reads the depth buffer, which only exists in 3D.
    if (
      !scene?.sampleHeightSupported ||
      scene.mode !== Cesium.SceneMode.SCENE3D
    )
      return floor;
    try {
      const sampled = scene.sampleHeight(
        Cesium.Cartographic.fromDegrees(lon, lat),
        _points ? [_points] : undefined,
      );
      return Number.isFinite(sampled)
        ? sampled + VEHICLE_HEIGHT_OFFSET_M
        : floor;
    } catch {
      // A scene that cannot answer is not a reason to stop drawing the fleet.
      return floor;
    }
  };

  const positionOf = (sample) =>
    Cesium.Cartesian3.fromDegrees(sample.lon, sample.lat, sample.height);

  /** Write every marker's interpolated position for this frame. */
  const frame = () => {
    if (!_enabled || !_points) return;
    const nowMs = now();
    for (const marker of _markers.values()) {
      const sample = sampleTrack(marker.track, nowMs);
      marker.point.position = positionOf(sample);
      marker.sample = sample;
    }
    if (nowMs - _lastPhaseCheck >= PHASE_CHECK_MS) {
      _lastPhaseCheck = nowMs;
      // An advisory expires on its own clock, with no frame to announce it.
      drawAdvisories(nowMs);
    }
  };

  const installFrame = () => {
    if (_removePreRender || !_viewer?.scene?.preRender?.addEventListener)
      return;
    _removePreRender = _viewer.scene.preRender.addEventListener(frame);
  };

  const releaseFrame = () => {
    _removePreRender?.();
    _removePreRender = null;
  };

  /** Drop every drawn vehicle, keeping the collection itself. */
  const clearVehicles = () => {
    if (_points)
      for (const marker of _markers.values()) _points.remove(marker.point);
    _markers.clear();
  };

  const clearAdvisories = () => {
    _dataSource?.entities.removeAll();
    _drawnSignature = '';
    _drawnAdvisories = 0;
    overlayHost.clearSource(HEADSUP_OVERLAY_SOURCE_ID);
  };

  /** Apply one telemetry frame: upsert, retire, and retarget each track. */
  const drawVehicles = (vehicles, sampledAt) => {
    if (!_points) return;
    const nowMs = now();
    const seen = new Set();
    for (const vehicle of vehicles) {
      seen.add(vehicle.id);
      const existing = _markers.get(vehicle.id);
      const height = groundHeight(
        vehicle.lon,
        vehicle.lat,
        existing?.track?.toHeight,
      );
      if (!existing) {
        // A vehicle's first sample has nothing to interpolate from, so it
        // appears where it was reported rather than sliding in from nowhere.
        const track = {
          fromLon: vehicle.lon,
          fromLat: vehicle.lat,
          fromHeight: height,
          fromHeading: vehicle.heading,
          toLon: vehicle.lon,
          toLat: vehicle.lat,
          toHeight: height,
          toHeading: vehicle.heading,
          startedAt: nowMs,
          durationMs: trackDurationMs(0),
        };
        const point = _points.add({
          position: positionOf({
            lon: vehicle.lon,
            lat: vehicle.lat,
            height,
          }),
          pixelSize: VEHICLE_PIXEL_SIZE,
          color: operatorColor(vehicle.op),
          outlineColor: Cesium.Color.BLACK.withAlpha(0.7),
          outlineWidth: 1,
          scaleByDistance: new Cesium.NearFarScalar(200, 1.35, 120000, 0.4),
          translucencyByDistance: new Cesium.NearFarScalar(
            200,
            1.0,
            180000,
            0.15,
          ),
          disableDepthTestDistance: 2500,
          id: `headsup-vehicle:${vehicle.id}`,
        });
        _markers.set(vehicle.id, { point, op: vehicle.op, track });
        continue;
      }
      // Start the new leg from where the dot IS, not from the previous
      // sample, so a frame that lands mid-interpolation does not snap back.
      const current = sampleTrack(existing.track, nowMs);
      if (existing.op !== vehicle.op) {
        existing.op = vehicle.op;
        existing.point.color = operatorColor(vehicle.op);
      }
      existing.track = {
        fromLon: current.lon,
        fromLat: current.lat,
        fromHeight: current.height,
        fromHeading: current.heading,
        toLon: vehicle.lon,
        toLat: vehicle.lat,
        toHeight: height,
        toHeading: vehicle.heading,
        startedAt: nowMs,
        durationMs: trackDurationMs(nowMs - existing.track.startedAt),
      };
    }
    for (const [id, marker] of [..._markers]) {
      if (seen.has(id)) continue;
      _points.remove(marker.point);
      _markers.delete(id);
    }
    _lastUpdate = Number.isFinite(sampledAt) ? sampledAt : nowMs;
  };

  /**
   * Redraw the hazard areas, but only when the live set actually changed.
   * @param {number} nowMs Epoch milliseconds.
   */
  const drawAdvisories = (nowMs) => {
    if (!_dataSource) return;
    const live = _advisories.filter((row) => advisoryIsLive(row, nowMs));
    const signature = live
      .map((row) => `${row.id}@${row.version}`)
      .sort()
      .join(',');
    if (signature === _drawnSignature) return;
    _drawnSignature = signature;

    const entities = [];
    const overlayEntries = [];
    for (const row of live) {
      const css = hazardCssColor(row.hazardClass);
      const color = Cesium.Color.fromCssColorString(css);
      row.polygons.forEach((rings, index) => {
        const [exterior, ...holes] = rings;
        entities.push(
          new Cesium.Entity({
            id: `${ENTITY_PREFIX}:${row.id}:${index}`,
            polygon: {
              hierarchy: new Cesium.PolygonHierarchy(
                Cesium.Cartesian3.fromDegreesArray(exterior.flat()),
                holes.map(
                  (hole) =>
                    new Cesium.PolygonHierarchy(
                      Cesium.Cartesian3.fromDegreesArray(hole.flat()),
                    ),
                ),
              ),
              material: new Cesium.ColorMaterialProperty(
                color.withAlpha(hazardFillAlpha(row.severity)),
              ),
              // No `height`, no `perPositionHeight` and no `heightReference`:
              // that is what makes this a GroundPrimitive, draped on the
              // surface. `heightReference` belongs to geometry that HAS a
              // height — Cesium warns and ignores it otherwise
              // (GroundGeometryUpdater.getGeometryHeight).
              classificationType: Cesium.ClassificationType.BOTH,
            },
            polyline: {
              positions: Cesium.Cartesian3.fromDegreesArray(exterior.flat()),
              clampToGround: true,
              width: 2,
              material: new Cesium.ColorMaterialProperty(color.withAlpha(0.9)),
              classificationType: Cesium.ClassificationType.BOTH,
            },
            properties: {
              advisoryId: row.id,
              hazardClass: row.hazardClass,
              severity: row.severity,
              action: row.action,
              validUntil: row.validUntil,
              evidenceCount: row.evidenceCount,
              signed: row.signed,
            },
          }),
        );
      });
      const [anchorLon, anchorLat] = row.anchors[0];
      overlayEntries.push(
        createAdvisoryOverlayEntry({
          id: row.id,
          position: Cesium.Cartesian3.fromDegrees(anchorLon, anchorLat),
          hazardClass: row.hazardClass,
          action: row.action,
          severity: row.severity,
          accent: css,
        }),
      );
    }

    _dataSource.entities.removeAll();
    for (const entity of entities) _dataSource.entities.add(entity);
    _drawnAdvisories = live.length;
    if (_enabled) {
      overlayHost.setEntries(
        HEADSUP_OVERLAY_SOURCE_ID,
        selectAdvisoryOverlayCohort(overlayEntries),
        {
          cohortLimit: HEADSUP_OVERLAY_COHORT_LIMIT,
          collisionCapacity: HEADSUP_OVERLAY_COLLISION_CAPACITY,
          moving: false,
        },
      );
    }
    governorRequestRender('headsup-advisories');
  };

  /** Apply one accepted socket frame, or the snapshot taken at enable. */
  const apply = (snapshot) => {
    if (!_enabled) return;
    _advisories = snapshot.advisories ?? [];
    _lastError = snapshot.lastError ?? null;
    drawVehicles(snapshot.vehicles ?? [], snapshot.vehiclesAt);
    drawAdvisories(now());
    governorRequestRender('headsup-frame');
  };

  const layer = {
    id: 'headsup',
    name: 'Heads Up Advisories',
    icon: '◈',
    source: 'Heads Up',
    // Push-driven: frames arrive on the socket, so the manager keeps the
    // panel's counters fresh instead of polling for data.
    updateInterval: 0,
    statsRefreshInterval: 1000,

    init(viewer) {
      if (_viewer) throw new Error('Heads Up layer is already initialized');
      _viewer = viewer;
      _points = new Cesium.PointPrimitiveCollection({
        blendOption: Cesium.BlendOption.TRANSLUCENT,
      });
      viewer.scene.primitives.add(_points);
      _points.show = false;
      _dataSource = new Cesium.CustomDataSource('headsup');
      _dataSource.show = false;
      viewer.dataSources.add(_dataSource);
      _advisories = [];
      _drawnSignature = '';
      _drawnAdvisories = 0;
      _lastUpdate = null;
      _lastError = null;
      _enabled = false;
      overlayHost.setVisible(HEADSUP_OVERLAY_SOURCE_ID, false);
      console.log('[Data:HeadsUp] Initialized');
    },

    enable() {
      _enabled = true;
      if (_points) _points.show = true;
      if (_dataSource) _dataSource.show = true;
      overlayHost.setVisible(HEADSUP_OVERLAY_SOURCE_ID, true);
      // Vehicle positions are written every frame while this layer is on, so
      // the governor stays in continuous mode for its lifetime (perf wave 2).
      holdContinuousRender(HEADSUP_RENDER_HOLD_ID);
      _unsubscribe?.();
      _unsubscribe = source.subscribe(apply);
      source.connect?.();
      installFrame();
    },

    disable() {
      _enabled = false;
      _unsubscribe?.();
      _unsubscribe = null;
      source.close?.();
      releaseFrame();
      releaseContinuousRender(HEADSUP_RENDER_HOLD_ID);
      clearVehicles();
      clearAdvisories();
      _advisories = [];
      if (_points) _points.show = false;
      if (_dataSource) _dataSource.show = false;
      overlayHost.setVisible(HEADSUP_OVERLAY_SOURCE_ID, false);
    },

    /**
     * Paint the current picture. The socket pushes every later frame; this
     * runs at enable so a layer switched on mid-stream is not blank until the
     * next tick.
     * @param {Cesium.Viewer} viewer
     * @returns {Promise<boolean>}
     */
    async update() {
      if (!_enabled || !_dataSource) return false;
      try {
        const snapshot = await source.getSnapshot();
        if (!_enabled) return false;
        apply(snapshot);
        return true;
      } catch (error) {
        if (!_enabled) return false;
        console.warn('[Data:HeadsUp] Snapshot error:', error);
        _lastError = error?.message || msg('Heads Up feed unavailable');
        return false;
      }
    },

    destroy(viewer = _viewer) {
      this.disable();
      if (_points) {
        viewer?.scene?.primitives?.remove(_points);
        _points = null;
      }
      if (_dataSource) {
        viewer?.dataSources?.remove(_dataSource, true);
        _dataSource = null;
      }
      overlayHost.clearSource(HEADSUP_OVERLAY_SOURCE_ID);
      _viewer = null;
      _lastUpdate = null;
      _lastError = null;
    },

    /**
     * Snapshot the drawn vehicles as plain JSON-safe records for the analyst
     * query engine. On-demand only; empty while the layer is off.
     * @param {number} [maxCount=2000] Maximum records to return.
     * @returns {Array<object>} See mapAnalystRecord for the record shape.
     */
    getAnalystRecords(maxCount = 2000) {
      if (!_enabled) return [];
      const limit = Number.isFinite(maxCount)
        ? Math.max(1, Math.floor(maxCount))
        : 2000;
      const records = [];
      for (const [id, marker] of _markers) {
        if (records.length >= limit) break;
        const sample = marker.sample ?? sampleTrack(marker.track, now());
        records.push(
          mapAnalystRecord(
            {
              id,
              op: marker.op,
              lat: sample.lat,
              lon: sample.lon,
              heading: sample.heading,
            },
            records.length,
          ),
        );
      }
      return records;
    },

    /** Operator colors currently on screen, for a legend. */
    getOperatorLegend() {
      const operators = new Map();
      for (const marker of _markers.values()) {
        const entry = operators.get(marker.op);
        if (entry) entry.vehicles++;
        else
          operators.set(marker.op, {
            op: marker.op,
            color: operatorCssColor(marker.op),
            vehicles: 1,
          });
      }
      return [...operators.values()].sort((a, b) => a.op.localeCompare(b.op));
    },

    getStats() {
      const status = source.getStatus?.() ?? {};
      return {
        count: _markers.size + _drawnAdvisories,
        vehicles: _markers.size,
        advisories: _drawnAdvisories,
        connected: status.connected === true,
        lastUpdate: _lastUpdate,
        error: _lastError,
      };
    },
  };
  return layer;
}
