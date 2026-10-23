import * as Cesium from 'cesium';
import {
  ACK_FLASH_MS,
  BEAM_GLOW_POWER,
  BEAM_WIDTH_PX,
  CHEVRON_FAR_M,
  CHEVRON_NEAR_M,
  CORRIDOR_WIDTH_PX,
  DISPATCH_BEAM_MS,
  FLASH_PEAK_SCALE,
  GHOST_VEHICLE_COLOR,
  HAZARD_PULSE_MS,
  HEADSUP_OVERLAY_COHORT_LIMIT,
  HEADSUP_OVERLAY_COLLISION_CAPACITY,
  HEADSUP_OVERLAY_SOURCE_ID,
  HEADSUP_RENDER_HOLD_ID,
  PULSE_PEAK_ALPHA,
  TRAIL_ALPHA,
  TRAIL_TAPER_POWER,
  TRAIL_WIDTH_PX,
  VEHICLE_CHEVRON_IMAGE,
  VEHICLE_CHEVRON_PX,
  VEHICLE_HEIGHT_OFFSET_M,
  beamProgress,
  createAdvisoryOverlayEntry,
  createDeclinedOverlayEntry,
  fleetCentroid,
  hazardCssColor,
  hazardFillAlpha,
  hazardLegendRows,
  headingDirection,
  mapAnalystRecord,
  operatorColor,
  operatorCssColor,
  pruneTrail,
  pulseLevel,
  sampleTrack,
  trackProgress,
  selectAdvisoryOverlayCohort,
  trackDurationMs,
  vehicleAppearance,
} from './model.js';
import { advisoryIsLive } from './expiry.js';
export * from './model.js';
export {
  HEADSUP_MOCK_VIEWER_ENDPOINT,
  HEADSUP_VIEWER_ENDPOINT,
  createHeadsupViewerSource,
} from './source.js';

/**
 * Heads Up advisories and the fleets they are addressed to.
 *
 * Five surfaces, for five different kinds of data:
 *
 * - **Vehicles** are a `PointPrimitiveCollection`. One dot per vehicle,
 *   colored by operator, its position written per frame from the
 *   interpolator in `model.js` so a 2 Hz feed reads as motion instead of a
 *   twice-a-second jump. Primitives rather than entities because the fleet is
 *   the one thing here that moves every frame.
 * - **Heading chevrons** are a `BillboardCollection` over the dots: one
 *   shared white arrow per vehicle, tinted by operator, with its
 *   `alignedAxis` set to the vehicle's direction of travel so the bearing
 *   reads from any camera angle rather than only from straight down. A
 *   chevron per vehicle stops being legible long before a thousand of them,
 *   so past `CHEVRON_FLEET_LIMIT` the cohort is the dots and the chevrons are
 *   not drawn at all — the same decision in perf terms as in design ones.
 * - **Hazard areas** are entity polygons on this layer's own
 *   `CustomDataSource` that Cesium draws as ground primitives — no height, no
 *   `perPositionHeight` — classifying `BOTH`, so an advisory drapes over the
 *   photoreal 3D tiles and over terrain alike. A clamped outline polyline
 *   carries the edge, because a ground polygon cannot outline itself. Static
 *   geometry, rebuilt only when the set of live advisories actually changes,
 *   because draped geometry re-tessellates whenever its positions do.
 * - **Corridors** are the same advisory contract addressed to a route rather
 *   than an area: LineString and MultiLineString geometry, drawn as
 *   ground-clamped polylines in the hazard class's colour.
 * - **Dispatch beams and deflection trails** are two `PolylineCollection`s.
 *   A beam is the one-and-a-half seconds in which an advisory reaches one
 *   operator's fleet; a trail is the last thirty seconds of a vehicle the
 *   advisory moved, which is what makes the bend around the zone visible
 *   rather than merely asserted.
 *
 * **Events, and the restraint rule.** Four things on this screen are gestures
 * rather than state: the hazard pulse, the dispatch beam, the acknowledgement
 * flash and the refusal tag. Each fires ONCE per event — one pulse per
 * published advisory version, one beam per operator per publication, one
 * flash per answer — and each is gated on a scenario actually running. The
 * landing view shows the idle feed, where an advisory in force is context and
 * not news, so nothing there animates. Everything animated is also seeded
 * silently at enable: a page that opens mid-run shows the current picture
 * without replaying the events that produced it.
 *
 * Four cohorts can be hidden independently — hazard areas, corridors, the
 * ghost control fleet, and any one operator's fleet — because the question
 * Heads Up mode's layer panel asks is "what changed for WHOM", and that is
 * only answerable if one cohort can be taken off the screen while the rest
 * stay. Hiding is a DRAW decision, never an ingest one: a hidden cohort is
 * still counted, still expiring on its own clock, and comes back instantly.
 *
 * World text goes through the overlay host, never Cesium labels
 * (`gev/docs/CODE-BOUNDARIES.md`).
 * @module layers/headsup
 */

// A portable package (no i18n): the error stays English data and the layer
// panel shows it translated.
const msg = (text) => text;

const ENTITY_PREFIX = 'headsup-advisory';
const CORRIDOR_PREFIX = 'headsup-corridor';
/** How often the drawn set is re-tested against the advisory windows. */
const PHASE_CHECK_MS = 500;
/** Past this many vehicles the heading chevrons stop being drawn. */
export const CHEVRON_FLEET_LIMIT = 400;
/**
 * Ground heights: a cell cache, and the cells one tick may newly sample.
 *
 * `scene.sampleHeight` reads the depth buffer, which costs a render pass per
 * call — measured at 12 ms on the laptop GPU this layer was first held to a
 * budget on. A window that bounds the COUNT per tick therefore does not bound
 * the cost: 24 samples a tick is 600 ms of every second, and at that price a
 * fleet of a thousand cannot be drawn at all.
 *
 * Height is a property of the ground, not of the vehicle standing on it, so
 * it is cached per cell and shared by every vehicle over that cell, and a
 * tick samples at most `GROUND_CELLS_PER_TICK` cells it has never seen. The
 * cost is then a bounded transient while a view fills in, and nothing at all
 * in the steady state, however large the fleet.
 *
 * A cell is sampled once and kept. Tiles arriving later can refine the
 * ground under a cell already answered, so the error that buys is a dot a
 * metre off its roadway where the first sample met a coarse tile — a sample
 * taken before any tile loaded reads as no answer and is not cached, so it
 * is retried rather than frozen.
 */
const GROUND_CELL_DEGREES = 0.0012;
const GROUND_CELLS_PER_TICK = 4;
/** Cells remembered before the cache is dropped and refilled. */
const GROUND_CELL_LIMIT = 4096;
/**
 * How often a trail takes a sample, and how many it keeps.
 *
 * Every append rewrites the whole polyline — Cesium re-encodes its positions
 * and recomputes its bounding sphere — so the feed's own 2 Hz is twice the
 * cadence a thirty-second line needs. At 1 Hz the bend around a zone is drawn
 * from the same reported positions for half the write cost.
 */
const TRAIL_SAMPLE_MS = 1000;
/** Trail samples kept per vehicle: thirty seconds at `TRAIL_SAMPLE_MS`. */
const MAX_TRAIL_SAMPLES = 32;

/**
 * Own one Heads Up display and its subscription.
 * @param {object} options
 * @param {object} options.source Viewer-socket source (`./source.js`).
 * @param {object} options.overlayHost Frozen world-overlay facade.
 * @param {object} [options.services] Application scene owners: `render`
 *   (`governorRequestRender`, `holdContinuousRender`,
 *   `releaseContinuousRender`) and `materials` (`polylineGlow`).
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
  /**
   * The glow material a beam or a trail draws with.
   *
   * A scene resource, so it arrives the same way the render governor does.
   * `Cesium.Material` compiles its uniforms against the DOM — it cannot be
   * constructed at all without a canvas — so a caller drawing to something
   * other than a browser scene supplies its own.
   */
  const {
    polylineGlow = (uniforms) =>
      Cesium.Material.fromType('PolylineGlow', uniforms),
  } = services.materials ?? {};
  let _viewer = null;
  let _points = null;
  let _chevrons = null;
  let _trails = null;
  let _beams = null;
  let _dataSource = null;
  /** @type {Map<string, object>} */
  const _markers = new Map();
  /** @type {object[]} */
  let _advisories = [];
  let _drawnSignature = '';
  let _drawnAdvisories = 0;
  let _liveCorridors = 0;
  let _lastPhaseCheck = 0;
  /**
   * Per-advisory colours, and when that advisory's pulse began.
   *
   * The entities read their fill and edge colour from here through a callback
   * rather than carrying a constant, which is what lets a pulse animate the
   * colour ATTRIBUTE of draped geometry instead of re-tessellating it. The
   * `Color` objects are reused per advisory, so a frame of pulsing allocates
   * nothing.
   * @type {Map<string, object>}
   */
  const _paint = new Map();
  /** @type {object[]} Advisory label entries, as the overlay host wants them. */
  let _overlayLabels = [];
  let _overlaySignature = '';
  /** @type {Array<{hazardClass: string, color: string, advisories: number}>} */
  let _legend = [];
  /** @type {Map<string, object>} One live dispatch beam per operator. */
  const _beamRecords = new Map();
  /** @type {Map<string, number>} When each operator's fleet began flashing. */
  const _flashes = new Map();
  /** @type {Map<string, object>} Operators whose latest answer was a refusal. */
  let _declined = new Map();
  /**
   * The refusal tag's text.
   *
   * This layer is a portable package and does not translate, so the console
   * hands the finished string down (`setDeclinedLabel`). The English default
   * is what a test, or a surface that never sets one, gets.
   */
  let _declinedTitle = 'DECLINED';
  /** @type {Set<string>} Advisory `id@version` keys already seen. */
  let _knownAdvisories = new Set();
  /** @type {Map<string, number>} The last answer counted per operator. */
  const _seenAcks = new Map();
  /**
   * Whether a picture has been applied since this layer was switched on.
   *
   * Until it has, every event animation is SEEDED rather than played: a page
   * that opens into a running scenario should show where that scenario got
   * to, not replay the publication that got it there.
   */
  let _seeded = false;
  let _scenarioRunning = false;
  /** Ground height per cell: see `GROUND_CELL_DEGREES`. */
  const _groundCells = new Map();
  /** Cells this tick may still sample for the first time. */
  let _cellBudget = 0;
  /** The last ground height sampled anywhere, as a new vehicle's floor. */
  let _groundFloor = VEHICLE_HEIGHT_OFFSET_M;
  /**
   * Which cohorts are drawn. An operator absent from the map is VISIBLE: a
   * fleet that joins the feed mid-run appears without needing a toggle to be
   * created for it first.
   */
  const _visible = {
    hazards: true,
    corridors: true,
    ghost: true,
    /** @type {Map<string, boolean>} */
    operators: new Map(),
  };
  const operatorVisible = (op) => _visible.operators.get(op) !== false;
  const vehicleVisible = (marker) =>
    marker.ghost ? _visible.ghost : operatorVisible(marker.op);
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
   *
   * Answered from the cell cache whenever it can be; see
   * `GROUND_CELL_DEGREES` for why the cache is what makes the fleet
   * affordable. Falls back to the height the vehicle already had, so a cell
   * still waiting its turn leaves a dot where it is rather than dropping it
   * to the last height sampled somewhere else.
   */
  const groundHeight = (lon, lat, fallback) => {
    const floor = Number.isFinite(fallback) ? fallback : _groundFloor;
    const key = `${Math.round(lon / GROUND_CELL_DEGREES)}:${Math.round(
      lat / GROUND_CELL_DEGREES,
    )}`;
    const known = _groundCells.get(key);
    if (known !== undefined) return known;
    if (_cellBudget <= 0) return floor;
    const scene = _viewer?.scene;
    // sampleHeight reads the depth buffer, which only exists in 3D.
    if (
      !scene?.sampleHeightSupported ||
      scene.mode !== Cesium.SceneMode.SCENE3D
    )
      return floor;
    _cellBudget--;
    try {
      const sampled = scene.sampleHeight(
        Cesium.Cartographic.fromDegrees(lon, lat),
        _points ? [_points] : undefined,
      );
      if (!Number.isFinite(sampled)) return floor;
      const height = sampled + VEHICLE_HEIGHT_OFFSET_M;
      // A bound on the map as well as on the rate: a camera flown across a
      // continent would otherwise accumulate cells it will never revisit.
      if (_groundCells.size >= GROUND_CELL_LIMIT) _groundCells.clear();
      _groundCells.set(key, height);
      _groundFloor = height;
      return height;
    } catch {
      // A scene that cannot answer is not a reason to stop drawing the fleet.
      return floor;
    }
  };

  const cartesianAt = (lon, lat, height, result) =>
    Cesium.Cartesian3.fromDegrees(lon, lat, height, undefined, result);

  const positionOf = (sample, result) =>
    cartesianAt(sample.lon, sample.lat, sample.height, result);

  /** Scratch sample, so retargeting a thousand tracks allocates nothing. */
  const _retarget = { lon: 0, lat: 0, height: 0, heading: 0 };

  /**
   * Brighten and swell one fleet's dots for the length of one answer.
   *
   * The flash keeps the operator's hue — it is that operator answering, and a
   * white flash would say only that something happened.
   */
  const applyFlash = (marker, nowMs) => {
    const startedAt = _flashes.get(marker.op);
    if (startedAt == null) {
      if (!marker.flashing) return;
      marker.flashing = false;
      marker.point.color = marker.baseColor;
      marker.point.pixelSize = marker.basePixelSize;
      return;
    }
    const level = pulseLevel(nowMs - startedAt, ACK_FLASH_MS);
    marker.flashing = true;
    marker.point.pixelSize =
      marker.basePixelSize * (1 + FLASH_PEAK_SCALE * level);
    Cesium.Color.lerp(
      marker.baseColor,
      Cesium.Color.WHITE,
      0.5 * level,
      marker.flashColor,
    );
    marker.flashColor.alpha = marker.baseColor.alpha;
    marker.point.color = marker.flashColor;
  };

  /** Advance every live dispatch beam, and retire the ones that are done. */
  const advanceBeams = (nowMs) => {
    if (!_beamRecords.size) return;
    for (const [op, beam] of [..._beamRecords]) {
      const { reach, alpha, done } = beamProgress(
        nowMs - beam.startedAt,
        DISPATCH_BEAM_MS,
      );
      if (done) {
        _beams?.remove(beam.polyline);
        _beamRecords.delete(op);
        continue;
      }
      // Never all the way back to the start: a polyline whose two positions
      // coincide loses one of them to duplicate removal, and a polyline that
      // changes LENGTH is rebuilt rather than rewritten.
      Cesium.Cartesian3.lerp(
        beam.from,
        beam.to,
        Math.max(0.02, reach),
        beam.head,
      );
      // A fresh array per frame per beam: there is at most one per operator,
      // and the setter is what marks the polyline's positions dirty.
      beam.polyline.positions = [beam.from, beam.head];
      beam.material.uniforms.color.alpha = alpha;
    }
  };

  /** Close any pulse whose window has run out. */
  const retirePulses = (nowMs) => {
    for (const paint of _paint.values())
      if (paint.pulseAt != null && nowMs - paint.pulseAt >= HAZARD_PULSE_MS)
        paint.pulseAt = null;
  };

  /** Write every marker's interpolated position for this frame. */
  const frame = () => {
    if (!_enabled || !_points) return;
    const nowMs = now();
    const flashing = _flashes.size > 0;
    const oriented = _chevrons?.show === true;
    for (const marker of _markers.values()) {
      const track = marker.track;
      // The leg is walked in Cartesian space rather than in degrees.
      // `Cartesian3.fromDegrees` costs a handful of trig calls per vehicle,
      // and a thousand of those per frame was the single largest cost in the
      // load case's frame profile. Over one 2 Hz leg — a hundred metres at
      // city speeds — the chord and the arc differ by a fraction of a
      // millimetre, so the dot moves where it moved before at a fraction of
      // the price, and both endpoints are still the reported ones.
      Cesium.Cartesian3.lerp(
        track.fromCart,
        track.toCart,
        trackProgress(track, nowMs),
        marker.position,
      );
      marker.point.position = marker.position;
      if (oriented && marker.billboard) {
        // Degrees are only needed by the cohort that draws a bearing, and
        // that cohort is bounded by `CHEVRON_FLEET_LIMIT`.
        const sample = sampleTrack(track, nowMs, marker.sample ?? undefined);
        marker.sample = sample;
        marker.billboard.position = marker.position;
        marker.billboard.alignedAxis = headingDirection(
          sample.lon,
          sample.lat,
          sample.heading,
          marker.axis,
        );
      } else marker.sample = null;
      if (flashing) applyFlash(marker, nowMs);
    }
    for (const [op, startedAt] of [..._flashes])
      if (nowMs - startedAt >= ACK_FLASH_MS) _flashes.delete(op);
    advanceBeams(nowMs);
    retirePulses(nowMs);
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

  /** Drop one vehicle's deflection trail, if it has one. */
  const dropTrail = (marker) => {
    if (!marker.trail) return;
    _trails?.remove(marker.trail.polyline);
    marker.trail = null;
  };

  /** Drop every drawn vehicle, keeping the collections themselves. */
  const clearVehicles = () => {
    for (const marker of _markers.values()) {
      _points?.remove(marker.point);
      removeChevron(marker);
      dropTrail(marker);
    }
    _markers.clear();
    _flashes.clear();
  };

  /** Drop every dispatch beam, drawn or pending. */
  const clearBeams = () => {
    for (const beam of _beamRecords.values()) _beams?.remove(beam.polyline);
    _beamRecords.clear();
  };

  const clearAdvisories = () => {
    _dataSource?.entities.removeAll();
    _drawnSignature = '';
    _drawnAdvisories = 0;
    _paint.clear();
    _overlayLabels = [];
    _overlaySignature = '';
    _legend = [];
    overlayHost.clearSource(HEADSUP_OVERLAY_SOURCE_ID);
  };

  /**
   * Give one vehicle the chevron that carries its heading.
   *
   * `alignedAxis` rather than `rotation`: the axis is the world-space
   * direction of travel, which Cesium points the billboard's top along in
   * screen space, so the arrow reads as the reported bearing from an oblique
   * camera and from straight down alike.
   */
  const addChevron = (marker, heading) => {
    if (!_chevrons || marker.billboard) return;
    marker.billboard = _chevrons.add({
      position: marker.position,
      image: VEHICLE_CHEVRON_IMAGE,
      width: VEHICLE_CHEVRON_PX,
      height: VEHICLE_CHEVRON_PX,
      color: marker.baseColor,
      alignedAxis: headingDirection(
        marker.track.toLon,
        marker.track.toLat,
        heading,
        marker.axis,
      ),
      scaleByDistance: new Cesium.NearFarScalar(
        CHEVRON_NEAR_M,
        1.15,
        CHEVRON_FAR_M,
        0.6,
      ),
      // The chevron is a close-range reading, so it fades out where it would
      // become a smear rather than a bearing.
      translucencyByDistance: new Cesium.NearFarScalar(
        CHEVRON_NEAR_M,
        1.0,
        CHEVRON_FAR_M,
        0.0,
      ),
      disableDepthTestDistance: 2500,
      show: marker.point.show,
      id: `headsup-vehicle:${marker.id}`,
    });
  };

  /** Take one vehicle's chevron away again, leaving its dot. */
  const removeChevron = (marker) => {
    if (!marker.billboard) return;
    _chevrons?.remove(marker.billboard);
    marker.billboard = null;
  };

  /** Push one trail's samples to its polyline, oldest first. */
  const writeTrail = (marker) => {
    marker.trail.polyline.positions = marker.trail.samples.map((sample) =>
      Cesium.Cartesian3.fromDegrees(sample.lon, sample.lat, sample.height),
    );
  };

  /**
   * One vehicle's deflection trail, appended to at the feed's own cadence.
   *
   * Positions are only written when a tick lands, never per frame: the fade
   * along the trail is the glow material's taper, computed in the shader, so
   * two hundred trails cost nothing between ticks. The samples are the
   * positions the simulator REPORTED, not the interpolated ones, so the bend
   * drawn here is the bend it drove.
   */
  const extendTrail = (marker, lon, lat, height, nowMs) => {
    if (marker.ghost || !marker.reroutedBy || !_trails) {
      // A reroute that is withdrawn stops growing its trail; the window then
      // drains it, which reads as the deflection fading out.
      if (marker.trail && !marker.reroutedBy) {
        pruneTrail(marker.trail.samples, nowMs);
        if (marker.trail.samples.length < 2) dropTrail(marker);
        else writeTrail(marker);
      }
      return;
    }
    if (!marker.trail) {
      // One material per trail, never one shared per operator: removing a
      // polyline DESTROYS its material (`Polyline._destroy`), so a shared
      // instance would be torn out from under every other trail using it.
      // Batching does not need sharing — a PolylineCollection merges
      // consecutive polylines whose material uniforms have equal VALUES into
      // one draw command, so a fleet in one colour is one command either way.
      const material = polylineGlow({
        color: operatorColor(marker.op).withAlpha(TRAIL_ALPHA),
        glowPower: 0.2,
        taperPower: TRAIL_TAPER_POWER,
      });
      marker.trail = {
        polyline: _trails.add({
          // Two positions from the start: a polyline whose position COUNT
          // changes is rebuilt rather than rewritten, and the first tick
          // would otherwise go from none to two.
          positions: [marker.position, marker.position],
          width: TRAIL_WIDTH_PX,
          show: marker.point.show,
          material,
          id: `headsup-trail:${marker.id}`,
        }),
        samples: [],
      };
    }
    const samples = marker.trail.samples;
    const newest = samples[samples.length - 1];
    // The cadence gate: see `TRAIL_SAMPLE_MS`. A tick that adds nothing also
    // rewrites nothing, which is the whole point of it.
    if (newest && nowMs - newest.at < TRAIL_SAMPLE_MS) return;
    samples.push({ lon, lat, height, at: nowMs });
    pruneTrail(marker.trail.samples, nowMs);
    if (marker.trail.samples.length > MAX_TRAIL_SAMPLES)
      marker.trail.samples.splice(
        0,
        marker.trail.samples.length - MAX_TRAIL_SAMPLES,
      );
    if (marker.trail.samples.length >= 2) writeTrail(marker);
  };

  /** Apply one telemetry frame: upsert, retire, and retarget each track. */
  const drawVehicles = (vehicles, sampledAt) => {
    if (!_points) return;
    const nowMs = now();
    const seen = new Set();
    // Past the limit a chevron per vehicle is a texture of arrows rather than
    // a set of bearings, so the cohort becomes the dots alone — and the
    // billboards are not merely hidden, they are never created.
    const oriented = vehicles.length <= CHEVRON_FLEET_LIMIT;
    if (_chevrons) _chevrons.show = oriented;
    // What one tick may spend on ground it has never read before.
    _cellBudget = GROUND_CELLS_PER_TICK;
    for (const vehicle of vehicles) {
      seen.add(vehicle.id);
      const existing = _markers.get(vehicle.id);
      const height = groundHeight(
        vehicle.lon,
        vehicle.lat,
        existing?.track?.toHeight,
      );
      const appearance = vehicleAppearance(vehicle);
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
        const position = cartesianAt(vehicle.lon, vehicle.lat, height);
        // The track carries its own two endpoints as Cartesians, because that
        // is what a frame interpolates between; see the frame loop.
        track.fromCart = Cesium.Cartesian3.clone(position);
        track.toCart = Cesium.Cartesian3.clone(position);
        const point = _points.add({
          position,
          pixelSize: appearance.pixelSize,
          color: appearance.color,
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
        const marker = {
          id: vehicle.id,
          point,
          billboard: null,
          op: vehicle.op,
          ghost: vehicle.ghost === true,
          reroutedBy: vehicle.reroutedBy ?? null,
          track,
          trail: null,
          position,
          axis: new Cesium.Cartesian3(),
          baseColor: appearance.color,
          basePixelSize: appearance.pixelSize,
          flashColor: Cesium.Color.clone(appearance.color),
          flashing: false,
          /** Degrees for this frame, kept only while chevrons are drawn. */
          sample: null,
        };
        point.show = vehicleVisible(marker);
        if (oriented) addChevron(marker, vehicle.heading);
        _markers.set(vehicle.id, marker);
        continue;
      }
      // Start the new leg from where the dot IS, not from the previous
      // sample, so a frame that lands mid-interpolation does not snap back.
      const current = sampleTrack(existing.track, nowMs, _retarget);
      const wasGhost = existing.ghost;
      if (existing.op !== vehicle.op || wasGhost !== (vehicle.ghost === true)) {
        existing.op = vehicle.op;
        existing.ghost = vehicle.ghost === true;
        existing.baseColor = appearance.color;
        existing.basePixelSize = appearance.pixelSize;
        existing.point.color = appearance.color;
        existing.point.pixelSize = appearance.pixelSize;
        existing.point.show = vehicleVisible(existing);
        if (existing.billboard) {
          existing.billboard.color = appearance.color;
          existing.billboard.show = existing.point.show;
        }
        // An operator change invalidates the trail's colour, and a vehicle
        // that became a ghost must not keep one at all.
        dropTrail(existing);
      }
      // A reroute is reported per tick, so it can also be withdrawn: an
      // advisory that expires leaves the vehicle on the feed without it.
      existing.reroutedBy = vehicle.reroutedBy ?? null;
      // A fleet that grew past the chevron limit, or shrank back under it,
      // gains or loses its arrows on the tick that crossed the line.
      if (oriented) addChevron(existing, vehicle.heading);
      else removeChevron(existing);
      // Retargeted in place: a thousand fresh track objects a tick is a
      // thousand objects for the collector, and the two Cartesian endpoints
      // are worth keeping rather than reallocating.
      const track = existing.track;
      track.durationMs = trackDurationMs(nowMs - track.startedAt);
      track.fromLon = current.lon;
      track.fromLat = current.lat;
      track.fromHeight = current.height;
      track.fromHeading = current.heading;
      track.toLon = vehicle.lon;
      track.toLat = vehicle.lat;
      track.toHeight = height;
      track.toHeading = vehicle.heading;
      track.startedAt = nowMs;
      // The new leg starts where the dot IS, and that is already a Cartesian:
      // reusing it keeps a retarget free of a degrees-to-Cartesian conversion.
      Cesium.Cartesian3.clone(existing.position, track.fromCart);
      cartesianAt(vehicle.lon, vehicle.lat, height, track.toCart);
      extendTrail(existing, vehicle.lon, vehicle.lat, height, nowMs);
    }
    for (const [id, marker] of [..._markers]) {
      if (seen.has(id)) continue;
      _points.remove(marker.point);
      removeChevron(marker);
      dropTrail(marker);
      _markers.delete(id);
    }
    _lastUpdate = Number.isFinite(sampledAt) ? sampledAt : nowMs;
  };

  /**
   * The colours one advisory draws in, and the pulse that may be swelling it.
   *
   * Reused per advisory and mutated in place: these are read once per frame
   * per advisory by Cesium's colour-attribute batch, which clones what it
   * gets, so returning a shared object is safe and allocates nothing.
   */
  const paintOf = (row) => {
    let paint = _paint.get(row.id);
    if (!paint) {
      const base = Cesium.Color.fromCssColorString(
        hazardCssColor(row.hazardClass),
      );
      paint = {
        base,
        fillAlpha: hazardFillAlpha(row.severity),
        fill: Cesium.Color.clone(base),
        edge: Cesium.Color.clone(base),
        pulseAt: null,
      };
      _paint.set(row.id, paint);
    } else {
      Cesium.Color.clone(
        Cesium.Color.fromCssColorString(hazardCssColor(row.hazardClass)),
        paint.base,
      );
      paint.fillAlpha = hazardFillAlpha(row.severity);
    }
    return paint;
  };

  const pulseLevelOf = (paint, nowMs) =>
    paint.pulseAt == null ? 0 : pulseLevel(nowMs - paint.pulseAt);

  const fillColorOf = (id) => {
    const paint = _paint.get(id);
    if (!paint) return Cesium.Color.TRANSPARENT;
    Cesium.Color.clone(paint.base, paint.fill);
    paint.fill.alpha = Math.min(
      1,
      paint.fillAlpha + PULSE_PEAK_ALPHA * pulseLevelOf(paint, now()),
    );
    return paint.fill;
  };

  const edgeColorOf = (id, steady) => {
    const paint = _paint.get(id);
    if (!paint) return Cesium.Color.TRANSPARENT;
    Cesium.Color.clone(paint.base, paint.edge);
    paint.edge.alpha = Math.min(
      1,
      steady + (1 - steady) * pulseLevelOf(paint, now()),
    );
    return paint.edge;
  };

  /** A colour property that follows one advisory's pulse. */
  const fillMaterial = (id) =>
    new Cesium.ColorMaterialProperty(
      new Cesium.CallbackProperty(() => fillColorOf(id), false),
    );
  const edgeMaterial = (id, steady) =>
    new Cesium.ColorMaterialProperty(
      new Cesium.CallbackProperty(() => edgeColorOf(id, steady), false),
    );

  /**
   * Redraw the hazard areas and corridors, but only when something changed.
   *
   * Draped geometry re-tessellates whenever its positions do, so this runs
   * only when the live set or the cohort visibility actually moved — both are
   * in the signature, which is why flipping a toggle redraws and a passing
   * frame does not. A PULSE is not in the signature and must not be: it
   * animates the colour attribute of geometry that already exists.
   * @param {number} nowMs Epoch milliseconds.
   */
  const drawAdvisories = (nowMs) => {
    if (!_dataSource) return;
    const live = _advisories.filter((row) => advisoryIsLive(row, nowMs));
    const signature = [
      _visible.hazards ? 'h' : '-',
      _visible.corridors ? 'c' : '-',
      ...live.map((row) => `${row.id}@${row.version}`).sort(),
    ].join(',');
    if (signature === _drawnSignature) {
      publishOverlay();
      return;
    }
    _drawnSignature = signature;

    const entities = [];
    const overlayEntries = [];
    _liveCorridors = 0;
    const liveIds = new Set();
    for (const row of live) {
      liveIds.add(row.id);
      const css = hazardCssColor(row.hazardClass);
      paintOf(row);
      if (row.lines.length) _liveCorridors++;
      // Which cohort this advisory belongs to is decided by its geometry: an
      // area is a hazard, a route is a corridor. Geometry this layer cannot
      // draw (a Point) is labelled with the hazards cohort, so it never
      // becomes invisible for lack of a toggle of its own.
      const isCorridor = row.lines.length > 0 && row.polygons.length === 0;
      const cohortVisible = isCorridor ? _visible.corridors : _visible.hazards;
      if (_visible.corridors)
        row.lines.forEach((positions, index) => {
          entities.push(
            new Cesium.Entity({
              id: `${CORRIDOR_PREFIX}:${row.id}:${index}`,
              polyline: {
                positions: Cesium.Cartesian3.fromDegreesArray(positions.flat()),
                clampToGround: true,
                width: CORRIDOR_WIDTH_PX,
                material: edgeMaterial(row.id, 0.85),
                classificationType: Cesium.ClassificationType.BOTH,
              },
              properties: {
                advisoryId: row.id,
                hazardClass: row.hazardClass,
                severity: row.severity,
                action: row.action,
                validUntil: row.validUntil,
                corridor: true,
              },
            }),
          );
        });
      if (_visible.hazards)
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
                // A callback colour, not a constant: the pulse writes the
                // per-instance colour attribute, which a ColorMaterialProperty
                // on draped geometry supports without becoming dynamic
                // (PolygonGeometryUpdater._isDynamic).
                material: fillMaterial(row.id),
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
                material: edgeMaterial(row.id, 0.9),
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
      // A label without the geometry it names would be a claim about a
      // hazard the operator cannot see, so it goes with its cohort.
      if (cohortVisible) {
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
    }
    // An advisory that expired keeps nothing: its colours and its pulse go
    // with the geometry they belonged to.
    for (const id of [..._paint.keys()])
      if (!liveIds.has(id)) _paint.delete(id);

    _dataSource.entities.removeAll();
    for (const entity of entities) _dataSource.entities.add(entity);
    _drawnAdvisories = live.length;
    _legend = hazardLegendRows(live);
    _overlayLabels = overlayEntries;
    publishOverlay();
    governorRequestRender('headsup-advisories');
  };

  /**
   * Publish the world text: the advisory labels, plus any refusal tag.
   *
   * One call for both, because the overlay host owns a SOURCE rather than
   * individual entries: publishing the labels alone would take a refusal tag
   * off the screen. The signature keeps a stationary picture from
   * re-publishing, and quantizes the fleet anchors to about a metre so a
   * tag that follows a fleet is republished at the feed's cadence rather
   * than at the frame rate.
   */
  const publishOverlay = () => {
    if (!_enabled) return;
    const tags = [];
    for (const [op, record] of _declined) {
      if (!operatorVisible(op)) continue;
      const centroid = fleetCentroid(fleetSamples(op));
      if (!centroid) continue;
      tags.push({
        op,
        centroid,
        entry: createDeclinedOverlayEntry({
          operator: op,
          position: Cesium.Cartesian3.fromDegrees(
            centroid.lon,
            centroid.lat,
            centroid.height,
          ),
          title: record.title,
        }),
      });
    }
    const signature = [
      _drawnSignature,
      ...tags.map(
        ({ op, centroid }) =>
          `${op}@${centroid.lon.toFixed(5)},${centroid.lat.toFixed(5)}`,
      ),
    ].join('|');
    if (signature === _overlaySignature) return;
    _overlaySignature = signature;
    overlayHost.setEntries(
      HEADSUP_OVERLAY_SOURCE_ID,
      selectAdvisoryOverlayCohort([
        ..._overlayLabels,
        ...tags.map(({ entry }) => entry),
      ]),
      {
        cohortLimit: HEADSUP_OVERLAY_COHORT_LIMIT,
        collisionCapacity: HEADSUP_OVERLAY_COLLISION_CAPACITY,
        // A refusal tag rides on a fleet, so while one is up the host is told
        // the source moves and re-solves placement on its own interval.
        moving: tags.length > 0,
      },
    );
  };

  /** The interpolated positions of one operator's drawn fleet. */
  const fleetSamples = function* (op) {
    for (const marker of _markers.values()) {
      if (marker.ghost || marker.op !== op) continue;
      yield marker.sample ?? sampleTrack(marker.track, now());
    }
  };

  /**
   * Draw one dispatch beam per operator, from the zone to that fleet.
   *
   * The ghost fleet gets none: nothing was dispatched to it, which is the
   * whole claim the control cohort makes.
   */
  const openBeams = (row, nowMs) => {
    if (!_beams) return;
    const [anchorLon, anchorLat] = row.anchors[0];
    const operators = new Set();
    for (const marker of _markers.values())
      if (!marker.ghost && operatorVisible(marker.op)) operators.add(marker.op);
    for (const op of operators) {
      const centroid = fleetCentroid(fleetSamples(op));
      if (!centroid) continue;
      // One beam per operator: a second publication replaces the first rather
      // than stacking a second line over the same fleet.
      const existing = _beamRecords.get(op);
      if (existing) _beams.remove(existing.polyline);
      const from = Cesium.Cartesian3.fromDegrees(
        anchorLon,
        anchorLat,
        centroid.height,
      );
      const to = Cesium.Cartesian3.fromDegrees(
        centroid.lon,
        centroid.lat,
        centroid.height,
      );
      const material = polylineGlow({
        // Invisible for its first frame: the next one writes the reach and
        // the alpha together, so the beam is never briefly full length.
        color: operatorColor(op).withAlpha(0),
        glowPower: BEAM_GLOW_POWER,
        taperPower: 1.0,
      });
      _beamRecords.set(op, {
        polyline: _beams.add({
          positions: [from, to],
          width: BEAM_WIDTH_PX,
          material,
          id: `headsup-beam:${row.id}:${op}`,
        }),
        material,
        from,
        to,
        head: Cesium.Cartesian3.clone(from),
        startedAt: nowMs,
      });
    }
  };

  /**
   * Note what just happened, and let each gesture fire exactly once.
   *
   * Three events are read off two diffs: a publication is an advisory key the
   * layer has not seen, and an answer is an operator whose acknowledgement
   * sequence moved. Both diffs are updated whether or not the gesture plays,
   * which is what makes the seeding rule and the idle rule suppress an event
   * rather than defer it.
   */
  const applyEvents = (snapshot, nowMs) => {
    const live = _advisories.filter((row) => advisoryIsLive(row, nowMs));
    const keys = new Set(live.map((row) => `${row.id}@${row.version}`));
    const play = _seeded && _scenarioRunning;
    for (const row of live) {
      if (_knownAdvisories.has(`${row.id}@${row.version}`)) continue;
      if (!play) continue;
      const paint = _paint.get(row.id);
      if (paint) paint.pulseAt = nowMs;
      openBeams(row, nowMs);
    }
    _knownAdvisories = keys;

    const acks = snapshot.pipeline?.acks ?? [];
    for (const ack of acks) {
      const seen = _seenAcks.get(ack.operator);
      if (seen === ack.seq) continue;
      _seenAcks.set(ack.operator, ack.seq);
      if (play) _flashes.set(ack.operator, nowMs);
    }
    // Derived, never accumulated: a run that resets its acknowledgements
    // takes its refusal tags with it.
    for (const op of [..._seenAcks.keys()])
      if (!acks.some((ack) => ack.operator === op)) _seenAcks.delete(op);
    const declined = new Map();
    for (const ack of acks)
      if (ack.status === 'DECLINED')
        declined.set(ack.operator, {
          advisoryId: ack.advisoryId ?? null,
          title: _declinedTitle,
        });
    _declined = declined;
  };

  /** Apply one accepted socket frame, or the snapshot taken at enable. */
  const apply = (snapshot) => {
    if (!_enabled) return;
    const nowMs = now();
    _advisories = snapshot.advisories ?? [];
    _lastError = snapshot.lastError ?? null;
    _scenarioRunning = snapshot.scenario?.state === 'running';
    drawVehicles(snapshot.vehicles ?? [], snapshot.vehiclesAt);
    // Paint before events, so a publication has colours to pulse, and draw
    // after them, so the first frame of the pulse is the one being requested.
    drawAdvisories(nowMs);
    applyEvents(snapshot, nowMs);
    publishOverlay();
    _seeded = true;
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
      // No `scene`: that option exists to clamp billboards to terrain by
      // height reference, and these take their height from the vehicle dot
      // they ride on. Handing one over would buy a per-billboard terrain
      // callback this layer has no use for.
      _chevrons = new Cesium.BillboardCollection({
        blendOption: Cesium.BlendOption.TRANSLUCENT,
      });
      viewer.scene.primitives.add(_chevrons);
      _chevrons.show = false;
      _trails = new Cesium.PolylineCollection();
      viewer.scene.primitives.add(_trails);
      _trails.show = false;
      _beams = new Cesium.PolylineCollection();
      viewer.scene.primitives.add(_beams);
      _beams.show = false;
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
      _seeded = false;
      if (_points) _points.show = true;
      if (_trails) _trails.show = true;
      if (_beams) _beams.show = true;
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
      _seeded = false;
      _unsubscribe?.();
      _unsubscribe = null;
      source.close?.();
      releaseFrame();
      releaseContinuousRender(HEADSUP_RENDER_HOLD_ID);
      clearVehicles();
      clearBeams();
      clearAdvisories();
      _advisories = [];
      _liveCorridors = 0;
      _declined = new Map();
      _seenAcks.clear();
      _knownAdvisories = new Set();
      if (_points) _points.show = false;
      if (_chevrons) _chevrons.show = false;
      if (_trails) _trails.show = false;
      if (_beams) _beams.show = false;
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
      for (const collection of [_points, _chevrons, _trails, _beams])
        if (collection) viewer?.scene?.primitives?.remove(collection);
      _points = null;
      _chevrons = null;
      _trails = null;
      _beams = null;
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

    /**
     * Operator colors currently on screen, for a legend or a toggle row.
     *
     * Ghost operators are left out: the ghost fleet is ONE cohort with one
     * switch, so listing its operators beside the advised ones would offer a
     * control that cannot be acted on and imply a comparison per company
     * rather than per cohort.
     * @returns {Array<{op: string, color: string, vehicles: number,
     *   rerouted: number, declined: boolean, visible: boolean}>}
     */
    getOperatorLegend() {
      const operators = new Map();
      for (const marker of _markers.values()) {
        if (marker.ghost) continue;
        const entry = operators.get(marker.op);
        if (entry) {
          entry.vehicles++;
          if (marker.reroutedBy) entry.rerouted++;
        } else
          operators.set(marker.op, {
            op: marker.op,
            color: operatorCssColor(marker.op),
            vehicles: 1,
            rerouted: marker.reroutedBy ? 1 : 0,
            declined: _declined.has(marker.op),
            visible: operatorVisible(marker.op),
          });
      }
      return [...operators.values()].sort((a, b) => a.op.localeCompare(b.op));
    },

    /** The ghost control fleet on screen, as one cohort. */
    getGhostCohort() {
      let vehicles = 0;
      for (const marker of _markers.values()) if (marker.ghost) vehicles++;
      return {
        color: GHOST_VEHICLE_COLOR,
        vehicles,
        visible: _visible.ghost,
      };
    },

    /**
     * The hazard classes drawn right now, for the legend.
     *
     * What is on screen, not what the palette knows: a legend that names a
     * colour nothing is drawn in teaches the wrong thing about the picture.
     * @returns {Array<{hazardClass: string, color: string,
     *   advisories: number}>}
     */
    getHazardLegend() {
      return _legend.map((row) => ({ ...row }));
    },

    /**
     * Set the translated text of the refusal tag.
     *
     * This layer is a portable package and does not translate; the console
     * owns `t()` and hands the finished string down, the same way it hands
     * down nothing else about how a tag is drawn.
     * @param {string} title
     */
    setDeclinedLabel(title) {
      const text = String(title ?? '').trim();
      if (!text) return;
      _declinedTitle = text;
      for (const record of _declined.values()) record.title = text;
      _overlaySignature = '';
      publishOverlay();
    },

    /** Which cohorts are drawn right now. */
    getCohortVisibility() {
      return {
        hazards: _visible.hazards,
        corridors: _visible.corridors,
        ghost: _visible.ghost,
        operators: Object.fromEntries(_visible.operators),
      };
    },

    /**
     * Show or hide cohorts. Only the keys present in `patch` change.
     *
     * Visibility is a draw decision: nothing is unsubscribed, no advisory
     * stops expiring, and no counter stops counting, so a cohort switched
     * back on is immediately current rather than waiting for a frame.
     * @param {{hazards?: boolean, corridors?: boolean, ghost?: boolean,
     *   operators?: Record<string, boolean>}} patch
     */
    setCohortVisibility(patch = {}) {
      if (!patch || typeof patch !== 'object') return;
      for (const key of ['hazards', 'corridors', 'ghost'])
        if (typeof patch[key] === 'boolean') _visible[key] = patch[key];
      if (patch.operators && typeof patch.operators === 'object')
        for (const [op, shown] of Object.entries(patch.operators)) {
          if (typeof shown !== 'boolean') continue;
          // An operator back at the default is forgotten rather than stored,
          // so the map cannot grow with every fleet the feed has ever named.
          if (shown) _visible.operators.delete(op);
          else _visible.operators.set(op, false);
        }
      for (const marker of _markers.values()) {
        marker.point.show = vehicleVisible(marker);
        if (marker.billboard) marker.billboard.show = marker.point.show;
        if (marker.trail) marker.trail.polyline.show = marker.point.show;
      }
      drawAdvisories(now());
      governorRequestRender('headsup-cohorts');
    },

    /**
     * Drop everything on screen and everything the feed is holding for it.
     *
     * What a replay drew is only true inside that replay, so ending one
     * clears the fleet, the advisories and the run tally rather than leaving
     * a flood polygon hanging over a city the scenario was not about. The
     * socket stays open: the next idle frame repaints whatever is actually
     * current.
     */
    clearDrawn() {
      source.reset?.();
      clearVehicles();
      clearBeams();
      clearAdvisories();
      _advisories = [];
      _liveCorridors = 0;
      _declined = new Map();
      _seenAcks.clear();
      _knownAdvisories = new Set();
      _scenarioRunning = false;
      // The next picture seeds instead of playing: whatever arrives after a
      // clear is the current state of the world, not a fresh publication.
      _seeded = false;
      _lastUpdate = null;
      governorRequestRender('headsup-clear');
    },

    /**
     * Watch every accepted socket frame, without owning the subscription.
     *
     * The Heads Up console needs the same frames this layer draws — the
     * scenario clock, the run tallies, which vehicles were rerouted — and
     * there must be exactly ONE socket: a second subscriber would be a second
     * scripted run of the mock feed. So it watches through the layer rather
     * than opening its own source.
     * @param {(snapshot: object) => void} listener
     * @returns {() => void} Unsubscribe.
     */
    subscribeFeed(listener) {
      return source.subscribe(listener);
    },

    /** The feed's current picture, including the scenario run state. */
    getFeedSnapshot() {
      return source.getStatus?.() ?? null;
    },

    getStats() {
      const status = source.getStatus?.() ?? {};
      let ghosts = 0;
      let rerouted = 0;
      let trails = 0;
      for (const marker of _markers.values()) {
        if (marker.ghost) ghosts++;
        if (marker.reroutedBy) rerouted++;
        if (marker.trail) trails++;
      }
      return {
        count: _markers.size + _drawnAdvisories,
        vehicles: _markers.size,
        advisories: _drawnAdvisories,
        corridors: _liveCorridors,
        ghosts,
        rerouted,
        trails,
        declined: _declined.size,
        oriented: _chevrons?.show === true,
        connected: status.connected === true,
        fallback: status.fallback === true,
        endpoint: status.endpoint ?? null,
        lastUpdate: _lastUpdate,
        error: _lastError,
      };
    },
  };
  return layer;
}
