import { catalogControlServices } from './catalog.js';
import { StyleManager } from '../ui/composition.js';
import { flyToAustin } from '../camera.js';
import { initCockpitCloudEffects } from '../cockpitCloudEffects.js';
import { t } from '../i18n/index.js';

/**
 * Detection's own services, as nothing at all.
 *
 * A mode that does not START detection must also not be able to start it by
 * accident, and `initDetection` is not the only door: applying a style preset
 * or restoring a share link sets a detection MODE, which is what used to log
 * `[Detection] Mode: DENSE` on a page with nothing to detect. Rather than
 * gate a dozen upstream call sites, the subsystem's services are replaced
 * with no-ops, so every one of those paths runs and reaches nothing.
 *
 * The two readers answer honestly rather than emptily: detection is OFF, and
 * its tuning is the shape upstream's own readouts destructure.
 */
const SILENT_DETECTION_SERVICES = Object.freeze({
  initDetection: () => {},
  destroyDetection: () => {},
  setDetectionStyle: () => {},
  setDetectionTuning: () => {},
  setDetectionModeByLabel: () => {},
  cycleDetectionMode: () => {},
  suspendDetection: () => {},
  resumeDetection: () => {},
  getDetectionMode: () => 'OFF',
  getDetectionTuning: () => ({
    densityPct: 0,
    allocationStrategy: 'ELASTIC',
  }),
  readDetectionDiagnostics: () => null,
});

/** The tracked-entity readout, which detection feeds. */
const SILENT_TRACKED_READOUT_SERVICES = Object.freeze({
  initTrackedReadout: () => {},
  destroyTrackedReadout: () => {},
});

/** Construct the existing controls and camera presentation. */
export function createApplicationControls({
  scene: { viewer, mapStackController, operations },
  loaderStatus,
  Controls = StyleManager,
  services,
  catalog,
  placeSearch,
  headsupMode = null,
  defer,
}) {
  // Initialize the style manager (post-processing, HUD, locations, share links)
  const styleManager = new Controls(viewer, {
    services: {
      ...services,
      // Optional subsystems this mode does not start (src/headsup/mode.js).
      // Spread after the real ones, so the no-ops win where they apply.
      ...(headsupMode?.detection === false ? SILENT_DETECTION_SERVICES : {}),
      ...(headsupMode?.trackedReadout === false
        ? SILENT_TRACKED_READOUT_SERVICES
        : {}),
      ...operations.surface.controlServices,
      searchAndFlyTo: operations.searchAndFlyTo,
      fetchRegionalBrief: (...args) =>
        operations.requests.regional.getBrief(...args),
      ...catalogControlServices(catalog),
    },
    requestServices: operations.requests,
    mapStackController,
    placeSearch,
    // Which optional subsystems this mode starts; see src/headsup/mode.js.
    headsupMode,
  });
  defer(() => styleManager.orbitController.stop());
  defer(() => styleManager.hud.destroy());
  defer(() => styleManager.dispose());
  // The previous multi-canvas weather compositor remains disabled. Cockpit
  // clouds use a separate, capped low-resolution GPU pass that never attaches
  // Cesium fog or post-process stages and is fully stopped in map mode.
  const weatherEffects = null;
  const cockpitCloudEffects = initCockpitCloudEffects(viewer, {
    weatherService: operations.requests.weather,
  });
  defer(() => cockpitCloudEffects?.destroy());

  // If no share link state, do default fly-to Austin
  if (!styleManager.hasShareState) {
    loaderStatus.textContent = t('Flying to Austin, TX...');
    defer(flyToAustin(viewer));
  } else {
    loaderStatus.textContent = t('Restoring shared view...');
  }

  return { styleManager, weatherEffects, cockpitCloudEffects };
}
