import { startApplicationChrome } from '../app/startupChrome.js';
import { initKeySetup } from '../keySetup.js';
export function startStandaloneChrome(options) {
  return startApplicationChrome({
    initializeSettings: initKeySetup,
    ...options,
  });
}

/** The same chrome without the POWER UP key surface (Heads Up mode). */
export function startStandaloneChromeWithoutSettings(options) {
  return startApplicationChrome({ ...options, initializeSettings: null });
}
