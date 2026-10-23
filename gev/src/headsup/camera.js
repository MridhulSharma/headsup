import * as Cesium from 'cesium';

/**
 * The two camera moves Heads Up mode owns: into a scenario, and home again.
 *
 * Both are ordinary Cesium flights with `CUBIC_IN_OUT`, the same easing
 * `src/camera.js` uses for its presets, so a scenario arrives the way the
 * startup flight to Austin does. The director's authored-move sampler is not
 * involved: these are destinations, not shots.
 * @module headsup/camera
 */

/** Seconds for a scenario flight: long enough to read, short enough to wait. */
export const HEADSUP_FLIGHT_SECONDS = 2.6;

/**
 * Fly to one scenario's start view, or back to the landing view.
 * @param {Cesium.Viewer} viewer
 * @param {{lon: number, lat: number, height: number, heading: number,
 *   pitch: number}} view
 * @param {object} [options]
 * @param {number} [options.duration] Seconds.
 * @returns {void}
 */
export function flyToHeadsupView(viewer, view, { duration } = {}) {
  if (!viewer || viewer.isDestroyed?.() || !view) return;
  viewer.camera.flyTo({
    destination: Cesium.Cartesian3.fromDegrees(view.lon, view.lat, view.height),
    orientation: {
      heading: Cesium.Math.toRadians(view.heading ?? 0),
      pitch: Cesium.Math.toRadians(view.pitch ?? -35),
      roll: 0,
    },
    duration: Number.isFinite(duration) ? duration : HEADSUP_FLIGHT_SECONDS,
    easingFunction: Cesium.EasingFunction.CUBIC_IN_OUT,
  });
}

/**
 * Cancel a flight this module started, so teardown does not leave the camera
 * drifting toward a scenario the page is no longer showing.
 * @param {Cesium.Viewer} viewer
 */
export function cancelHeadsupFlight(viewer) {
  if (!viewer || viewer.isDestroyed?.()) return;
  viewer.camera.cancelFlight();
}
