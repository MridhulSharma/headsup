import { LayerLifecycle } from '../data/lifecycle.js';
import { LayerPresentation } from './layerPresentation.js';
import { createCyberSonarScene } from '../cyberSonarScene.js';
/**
 * Register the application layer catalog before allowing state restoration.
 *
 * `omitLayers` and `mountLayerPanel` are how a mode narrows this phase
 * without the catalog, the control surface or the layer panel knowing about
 * it. Registration is what later lets something ENABLE a layer, and enabling
 * is what makes it fetch, so an omitted layer never initializes, never polls
 * and never appears in any panel — while the catalog still holds every
 * instance the control surface declares. See src/headsup/mode.js.
 */
export function createApplicationData({
  scene: { viewer, mapStackController },
  controls: { styleManager },
  catalog,
  allowQaRegistration,
  onData,
  omitLayers = [],
  mountLayerPanel = true,
  defer,
}) {
  // Initialize data layer manager
  const dataManager = new LayerLifecycle(viewer, {
    allowQaRegistration,
    getSourceAvailability: catalog?.getSourceAvailability,
  });
  defer(async () => {
    await dataManager.destroyAll();
    if (dataManager.layers.size)
      throw new Error(
        `Data layers could not be destroyed: ${[...dataManager.layers.keys()].join(', ')}`,
      );
  });
  const presentation = new LayerPresentation(dataManager, {
    weatherClock: catalog?.weatherClock,
  });
  defer(() => presentation.destroy());
  onData?.(dataManager);
  if (!catalog?.layers || !catalog?.metadata)
    throw new TypeError('An application layer catalog is required');
  const omitted = new Set(omitLayers);
  const registered = catalog.layers.filter((layer) => !omitted.has(layer.id));
  for (const layer of registered) dataManager.register(layer);
  for (const layer of registered) layer.attachDataManager?.(dataManager);
  for (const layer of registered)
    layer.attachMapStackController?.(mapStackController);
  // Restoration starts only after the caller's complete registry is sealed.
  // The registry and its serialization metadata must stay a bijection, so an
  // omitted layer drops out of both.
  dataManager.finalizeRegistrations(
    omitted.size
      ? catalog.metadata.filter((entry) => !omitted.has(entry.id))
      : catalog.metadata,
  );
  if (allowQaRegistration) {
    window.__gevQaRegisterLayer = (targetManager, layerModule) => {
      if (targetManager !== dataManager)
        throw new Error('QA layer manager mismatch');
      return dataManager.registerForQa(layerModule);
    };
    window.__gevQaUnregisterLayer = (targetManager, layerId) => {
      if (targetManager !== dataManager)
        throw new Error('QA layer manager mismatch');
      return dataManager.unregisterForQa(layerId);
    };
    const register = window.__gevQaRegisterLayer;
    const unregister = window.__gevQaUnregisterLayer;
    defer(() => {
      if (window.__gevQaRegisterLayer === register)
        delete window.__gevQaRegisterLayer;
      if (window.__gevQaUnregisterLayer === unregister)
        delete window.__gevQaUnregisterLayer;
    });
  }
  // A mode that owns `#data-toggles` itself mounts the panel nowhere; the
  // presentation keeps every other job it does for the application.
  presentation.mount(
    mountLayerPanel ? document.getElementById('data-toggles') : null,
  );
  styleManager.attachDataManager(dataManager);
  defer(createCyberSonarScene(viewer, dataManager));

  return { dataManager, catalog, presentation };
}
