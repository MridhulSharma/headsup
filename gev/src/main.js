import { createStandaloneApplication } from './standalone/application.js';
import { describeError } from './standalone/errors.js';
import { t } from './i18n/index.js';

const application = createStandaloneApplication({
  googleApiKey: import.meta.env.GOOGLE_MAPS_API_KEY,
  cesiumToken: import.meta.env.CESIUM_ION_TOKEN,
  allowQaRegistration: import.meta.env.DEV,
  // Heads Up mode's scenario API, overridable the same way its viewer socket
  // is (VITE_HEADSUP_VIEWER_URL in src/standalone/layerSources.js): only this
  // entry reads the environment.
  scenarioBaseUrl: import.meta.env.VITE_HEADSUP_SCENARIO_URL || undefined,
  scenarioFallbackUrl:
    import.meta.env.VITE_HEADSUP_SCENARIO_FALLBACK_URL || undefined,
});

application.start().catch((error) => {
  console.error("God's Eye View initialization failed:", error);
  const loaderStatus = document.querySelector('#loading-screen .loader-status');
  loaderStatus.textContent = t('Error: {message}', {
    message: describeError(error),
  });
  loaderStatus.style.color = '#ff4444';
});

export { application };
