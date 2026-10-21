import { createHeadsupLayer } from '../../layers/headsup/index.js';
import * as render from '../../renderGovernor.js';
import { overlayHost } from './overlayHost.js';

/** Wire Heads Up advisories to the application overlay host and governor. */
export function createApplicationHeadsup(options) {
  return createHeadsupLayer({ overlayHost, services: { render }, ...options });
}
