import { createStandaloneCatalog } from './catalog.js';
import { createStandalonePlaceSearch } from './placeSearch.js';
import { CITY_POIS } from '../cityPois.js';
import { createApplication } from '../app/application.js';
import { createStandaloneScene } from './scene.js';
import { createStandaloneControls } from './controls.js';
import { createStandaloneData } from './data.js';
import { createStandaloneTools } from './tools.js';
import { resolveLocale, applyDocumentLanguage } from '../i18n/locale.js';
import { applyDocumentTranslations, setLocale } from '../i18n/index.js';
import { createGoogleTokenSource } from '../maps/googleTokens.js';
import { applyHeadsupBrand } from '../headsup/brand.js';
import { createHeadsupModeConfig, markHeadsupMode } from '../headsup/mode.js';
import { createScenarioClient } from '../headsup/scenarios.js';
import { startStandaloneChromeWithoutSettings } from './startupChrome.js';

// The existing controls and layer catalog contain page-scoped state.
let constructed = false;

/** Compose the standalone application once per page. Reload to start again. */
export function createStandaloneApplication({
  googleApiKey,
  cesiumToken,
  geospatial = {},
  voice = {},
  sources = {},
  streetLevelProviders,
  allowQaRegistration = false,
  search = globalThis.location?.search ?? '',
  scenarioBaseUrl,
  scenarioFallbackUrl,
}) {
  if (constructed)
    throw new Error('The standalone application already owns this page');
  constructed = true;
  // Locale is resolved before any UI initialization so <html lang>/<html dir>
  // and every catalog lookup agree from the first painted frame. Module
  // scripts execute after parsing, so the DOM is ready here and every
  // data-i18n* element honors the resolved locale without a reload.
  const activeLocale = resolveLocale();
  setLocale(activeLocale);
  applyDocumentLanguage(document, activeLocale);
  applyDocumentTranslations(document);
  // Which application this page is, decided before any UI is built so the
  // stylesheet's one hook is in place for the first painted frame and no
  // readout Heads Up mode hides is ever shown and then withdrawn.
  const mode = createHeadsupModeConfig({
    search,
    scenarioBaseUrl,
    ...(scenarioFallbackUrl === undefined ? {} : { scenarioFallbackUrl }),
  });
  markHeadsupMode(document, mode);
  // The product name goes on before the scene is built, so the cover a
  // viewer looks at while the world loads already says Signum rather than
  // the fork's name. `?mode=full` is left as upstream built it.
  if (mode.headsUp) applyHeadsupBrand(document);
  const headsupScenarios = mode.headsUp
    ? createScenarioClient({
        baseUrl: mode.scenarioBaseUrl,
        fallbackUrl: mode.scenarioFallbackUrl,
      })
    : null;
  const loadingScreen = document.getElementById('loading-screen');
  const loaderStatus = loadingScreen.querySelector('.loader-status');
  let placeSearch;
  let catalog;
  return createApplication({
    createScene: async (context) => {
      placeSearch = createStandalonePlaceSearch({
        // The bundled city and landmark data the offline name provider reads.
        // The search package takes it as plain data rather than importing it,
        // so it stays free of application state.
        presets: CITY_POIS,
        ...geospatial,
        resolveApiKey: () => googleApiKey,
        signal: context.signal,
      });
      const scene = await createStandaloneScene({
        ...context,
        googleApiKey,
        // Without a key, Google 3D can still load with tokens from the
        // app's server when it offers them.
        googleTokens: googleApiKey ? null : createGoogleTokenSource(),
        cesiumToken,
        loaderStatus,
      });
      catalog = createStandaloneCatalog({
        sources,
        streetLevelProviders,
        nepalBoundaryResolver: (signal) =>
          scene.operations.annotationResolver.resolveRegionRingForQuery(
            'Nepal',
            signal,
            placeSearch,
            // The locator draws the border whenever it arrives.
            { budgetMs: Infinity },
          ),
        signal: context.signal,
        surface: scene.operations.surface,
      });
      return scene;
    },
    createControls: (context) =>
      createStandaloneControls({
        ...context,
        loaderStatus,
        placeSearch,
        catalog,
        // Detection and the tracked readout are not started in Heads Up mode.
        headsupMode: mode,
      }),
    createData: (context) =>
      createStandaloneData({
        ...context,
        allowQaRegistration,
        catalog,
        omitLayers: mode.omitLayers,
        mountLayerPanel: mode.upstreamLayerPanel,
      }),
    createTools: (context) =>
      createStandaloneTools({
        ...context,
        loadingScreen,
        placeSearch,
        voice,
        voiceAgent: mode.voiceAgent,
        headsupMode: mode,
        headsupScenarios,
        // The POWER UP surface offers provider keys. Heads Up mode has no
        // provider to key, so the surface is never built rather than built
        // and hidden.
        ...(mode.providerSettings
          ? {}
          : { startChrome: startStandaloneChromeWithoutSettings }),
      }),
  });
}
