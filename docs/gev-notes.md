# God's Eye View fork — orientation notes

Notes taken while getting `gev/` running, before any upstream file is touched.
They record how the viewer is put together and name the exact files the
`headsup` layer will add. Nothing here is a change to upstream code.

- Vendored from `MridhulSharma/gods-eye-view`, a fork of
  `bilawalsidhu/gods-eye-view` (MIT). See [`gev/NOTICE`](../gev/NOTICE).
- Upstream's own docs under `gev/docs/` are the authority; this file is the
  short version plus the Heads Up plan. The ones worth reading first are
  `CODE-BOUNDARIES.md`, `APPLICATION.md`, `DIRECTOR.md`, `DIRECTOR-CAMERA.md`
  and `SCENE-DOCUMENT.md`.

## Running it

```bash
cd gev && npm ci && npm run dev     # http://localhost:4173
```

`gev/vite.config.js` re-exports `gev/server/standalone/vite.config.js`, which
loads the dotenv ladder from the `gev/` root and hands selected values to
`createBrowserViteConfig` in `gev/build/vite.js`. That helper fixes the default
port at **4173** and injects `import.meta.env.CESIUM_ION_TOKEN`,
`GOOGLE_MAPS_API_KEY` and `MAPILLARY_CLIENT_TOKEN` at build time.

The photoreal basemap needs credentials. `gev/src/maps/google3d.js`
(`selectMapStartupRoute`, `loadPhotorealisticTileset`) tries, in order:

1. `google-direct` — Google Photorealistic 3D Tiles with a browser
   `GOOGLE_MAPS_API_KEY`.
2. `google-token` — the same tiles through short-lived tokens from the app's
   own server.
3. `google-ion` — Cesium ion asset `2275207`, using `CESIUM_ION_TOKEN`.
4. `osm` — flat OSM fallback, and `gev/src/maps/defaultSources.js` then reports
   the photoreal source unavailable.

So **one of `CESIUM_ION_TOKEN` or `GOOGLE_MAPS_API_KEY` in `gev/.env` is
required** for the photoreal city. Austin is already the startup target:
`gev/src/camera.js` `flyToAustin()` sets a 25 km top-down view, then flies to
600 m at heading 15°, pitch −30° after 500 ms.

### On `gev/.env.example`

The build step asked for one to be created. Upstream already ships a tracked
`gev/.env.example` (~18 KB) that documents `CESIUM_ION_TOKEN` and every other
provider key, so nothing was added — writing one would have meant editing an
upstream file for no gain. `gev/.gitignore` ignores the whole dotenv ladder
(`.env`, `.env.*`) with `!.env.example`, and the repo root `.gitignore` has the
same `.env*` / `!.env.example` pair, so `gev/.env` can never be committed from
either level.

## How layers are organized

A layer family is a directory under `gev/src/layers/<family>/`. The split is
enforced by `npm run check:boundaries`, which runs an import-direction parse and
a per-export build against `gev/scripts/package-boundaries.json`.

| File | Owns |
| --- | --- |
| `source.js` | Acquisition only: URL, fetch, abort, "is this payload whole". Portable — no Cesium, no DOM, no `window`. |
| `records.js` | Normalizing a raw payload into plain rows. Pure; rejects the whole snapshot rather than salvaging part of it. |
| `model.js` | Presentation constants and pure helpers: colors, overlay-entry builders, cohort selection, analyst-record mapping. May import Cesium for `Color`. |
| `index.js` | The layer object itself and its Cesium resources. |

`gev/src/layers/earthquakes/` is the cleanest small example and the closest
analogue to an advisory feed: a periodic snapshot of geolocated, expiring
events. Read it before writing ours.

The layer object returned by the factory is the whole contract:

```js
{ id, name, icon, source, updateInterval,
  init(viewer), enable(viewer), disable(viewer), update(viewer),
  destroy(viewer), getStats(), getAnalystRecords(max) }
```

`update()` is re-entrant-safe by convention: abort the previous
`AbortController`, and after every `await` re-check `signal.aborted`, that the
request is still the current one, and that the layer is still enabled.

### Wiring, in order

1. **`gev/src/layers/<family>/`** — the portable family above.
2. **`gev/src/app/layers/<family>.js`** — a three-line adapter that injects the
   application's scene services into the factory. Most of them just add
   `overlayHost` from `gev/src/app/layers/overlayHost.js`.
3. **`gev/src/sources/reference.js`** (or `gev/src/standalone/layerSources.js`
   for provider-backed feeds) — constructs the default source instance under a
   source key.
4. **`gev/src/app/sourceComposition.js`** — `CATALOG_SOURCE_CONTRACTS` declares
   which layer IDs a source key feeds and which methods it must expose.
5. **`gev/src/app/constructCatalog.js`** — calls the adapter inside
   `createLayerCatalog([...], metadata)`.
6. **`gev/src/data/layerState.js`** — `LAYER_STATE_REGISTRY` assigns the layer a
   one- or two-character share-link token. Get the next free one with
   `npm run layer-token:next -- <layer-id>`; it is a closed compatibility set,
   so the token is permanent once merged.
7. **`gev/src/ui/layerNames.js`** — the English display name, marked with
   `msg()` so i18n can translate it at the presentation boundary.
8. **`gev/src/ui/layerPanel.js`** — `PANEL_GROUPS` places the row under
   Movement / Cameras / Infrastructure / Events / Weather / Utilities.
9. **`gev/scripts/package-boundaries.json`** and
   **`gev/scripts/format-scope.json`** — register the new modules, or the
   boundary and format gates fail.

### Drawing

Two surfaces, and the choice matters:

- **Cesium entities** on a `Cesium.CustomDataSource` for geometry — polygons,
  polylines, ellipses. Earthquakes clamp ground ellipses with
  `heightReference: CLAMP_TO_GROUND` and deliberately use static axes, because a
  `CallbackProperty` there re-tessellates clamped geometry every frame.
- **The world-overlay host** for all text. `gev/src/overlays/worldOverlay.js`
  paints labels onto a canvas above the globe with collision resolution, horizon
  culling and paint lanes. `gev/docs/CODE-BOUNDARIES.md` states the rule plainly:
  *"New world text labels must use the world-overlay host; Cesium text labels are
  forbidden."*

A layer talks to the overlay through the frozen `overlayHost` facade —
`setEntries(sourceId, entries, { cohortLimit, collisionCapacity, moving })`,
`setVisible`, `clearSource` — and builds each entry in its own `model.js`
(`variant`, `title`, `accent`, `priority`, `collisionGroup`, `paintLane`,
`placement`, `edgeFade`, `horizonCull`, …). Entry variants include `label`,
`track`, `card`, `thumbnail`, `selected` and `tracked`.

## Where the HUD styles live

One ordered stylesheet entry, no CSS-in-JS:

- `gev/src/ui/styles.js` imports `gev/style.css`.
- `gev/style.css` is nothing but ordered `@import`s of
  `gev/src/ui/styles/*.css` — order is the cascade and is load-bearing.
- **`gev/src/ui/styles/overlays.css` is the HUD file** (~50 HUD rules):
  `#intel-hud`, the `--hud-color` / `--hud-glow` / `--hud-border` custom
  properties, and the `.hud-top-bar`, `.hud-corner`, `.hud-bracket`,
  `.hud-classification`, `.hud-summary`, `.hud-edge` blocks, plus the
  `#intel-hud[data-variant='tactical'|'operator'|'minimal']` overrides.
- `gev/src/ui/styles/cyber.css` adds the `cyber` variant overrides;
  `cockpit.css`, `responsive.css` and the `command-dock-*.css` files carry the
  rest of the HUD-adjacent rules.

The renderer is `gev/src/hud.js` (~1050 lines) — classification banners, MGRS
and lat/lon, GSD/NIIRS/ONA sensor metrics, timestamps. It sets the color custom
properties per shader mode rather than swapping classes.
`gev/src/hudLayouts.js` and `gev/src/hudLayoutPolicy.js` own the canonical
layout list shared by the UI, the voice schema and the renderer; the default is
`tactical`. Markup lives in `gev/src/ui/templates/` (`hud-loading.html`,
`cockpit.html`, `display-controls.html`), expanded into `gev/index.html` at
build time by `build/html` from an allowlist of `<!-- gev:template name -->`
markers.

## Scene files and the camera director

A **scene document** is UTF-8 JSON, specified in `gev/docs/SCENE-DOCUMENT.md`.
Root is `{ version, scenes[] }`; each scene has `shots[]` and optional
`anchors[]`; each shot has `durationSec`, `holdSec`, `camera`, optional `move`,
`visual` and `layers`. Validation is bounded and total — 5 MiB, 256 scenes,
10,000 shots, 24 nesting levels — and an invalid import leaves the current
project untouched. Browser storage key is `godsEyeView.sceneProject.v2` (the
suffix is not the document version).

Versions, in the order they arrived: v4 anchors and explicit moves, v5 data
packs, v6 declarative interactions. Legacy files migrate on import without
converting their shots into authored moves.

Shot `layers` entries are `{ "<layer-id>": true }` or
`{ "<layer-id>": { "enabled": true, "params": {} } }` — so a scene can switch
the `headsup` layer on and pass it parameters, using the same layer ID the
catalog registers.

Ownership splits three ways:

- **`gev/src/director/`** — portable. `playback.js` (`buildPlaybackQueue`,
  `playSceneQueue`), `timeline.js` (pure seek math), `clock.js` (run/load/shot
  tickers and hold deadlines), `camera.js` (poses and sampling), `document.js`
  (`parseSceneDocument`, `validateSceneDocument`), plus `packs/`,
  `interactions/` and `sharing/`. Imports no renderer, UI, storage or dataset.
- **`gev/src/scenes/`** — the editor and the renderer bridge.
  `director.js` (2572 lines) owns editor state, import/export and camera
  arbitration; `playbackAdapter.js` connects the portable runner to it;
  `cameraMotion.js` owns the animation frame; `recipes.js` holds the built-in
  scenes (`flights-radar`, `orbital-watch`, `thermal-threats`, `city-overload`,
  `omniscience-pullback`, and the Bhote Koshi event pack); `packs/registry.js`
  registers presentation adapters.
- **`gev/src/ui/sceneControls.js`, `scenes.js`, `scenePresentation.js`** — the
  panel. `ui/scenes` imports no director, camera engine or storage.

Playback runs an adapter through fixed phases per shot — `selectShot`,
`applyVisual`, `applyLayers`, `travel`, `settle`, `hold`, `completeShot` — with
a cancellation check between each. The runner only checks *between* phases, so
an adapter must also pass the abort signal into the work itself.

## How motion interpolation works

Two separate mechanisms:

**1. Ordinary shots** have a `camera` destination and no `move`. They use
Cesium's own `camera.flyTo` with `EasingFunction.CUBIC_IN_OUT`, which arcs the
path automatically. `gev/src/camera.js` is the simple case.

**2. Explicit authored moves** (`shot.move`) do not touch the live viewport at
all. `gev/src/director/camera.js` is 59 lines and worth reading whole:

- `resolveCameraPose(scene, camera)` — inline pose or scene-local `anchorId`;
  defaults are heading 0, pitch −35, roll 0.
- `resolveCameraMove(scene, shot)` — resolves both endpoints plus `easing` and
  `durationSec`.
- `sampleCameraMove(move, progress)` — the sampler, shared by live playback and
  by seeking, which is why a backward seek never depends on where the camera
  currently is.

Inside the sampler: `progress` is clamped to `[0, 1]`; `t === 0` and `t === 1`
return the authored endpoints exactly (so zero pitch survives). Easing is
`linear` or `cubic-in-out`, the latter computed inline as `4t³` below the
midpoint and `1 − (−2t + 2)³ / 2` above it. Then:

- `lat`, `alt`, `pitch` — plain linear interpolation of the eased `t`.
- `lon`, `heading`, `roll` — shortest signed arc,
  `a + ((((b − a + 540) % 360) + 360) % 360 − 180) * eased`, so a move crosses
  the dateline the short way; an exact 180° tie takes the negative arc.
- `lon` is finally wrapped by `normalizeCameraLongitude`.

This is authored coordinate interpolation, **not** a geodesic and **not** a
flight with an automatically raised arc. Heights are metres above the WGS84
ellipsoid and every anchor must say `"altitudeReference": "ellipsoid"`;
terrain-relative input is rejected and no terrain is sampled, so an author who
wants clearance has to supply it. Curved paths and look-at targets do not exist
yet.

`gev/src/scenes/cameraMotion.js` wraps the sampler in frame ownership:
`createCameraMotion({ applyPose })` holds exactly one `requestAnimationFrame`
callback, re-checks `token.cancelled` / `token.signal.aborted` every tick,
re-checks that it is still the active run after `applyPose` (because applying a
pose can synchronously transfer camera ownership), and settles immediately on
stop, replacement, teardown, navigation handoff or manual pointer/wheel input.

## Files we will add for the `headsup` layer

Layer ID `headsup`, source key `headsup`, panel group **Events**, share token
**`5`** (from `npm run layer-token:next -- headsup`; re-run before merge, since
it is only reserved once committed). Advisories are the signed, expiring
GeoJSON Features defined in
[`schemas/advisory.schema.json`](../schemas/advisory.schema.json) — Point /
LineString / Polygon and their Multi variants, carrying `properties.hazard`
(class, severity 1–5, confidence), `properties.action` (`NO_ENTRY`,
`SPEED_CAP`, `ALL_WAY_STOP`, `DENSITY_CAP`, `KEEP_CLEAR`, `PAUSE_SERVICE`),
`valid_from` / `valid_until`, `evidence[]`, `policy_rule`, `issuer` and
`signature`.

New files, all additive:

| File | Contents |
| --- | --- |
| `gev/src/layers/headsup/source.js` | `createHeadsupSource({ endpoint, fetchImpl })` — polls the Heads Up API or subscribes to the viewer WebSocket; `getSnapshot({ signal })`. Portable: no Cesium, no DOM. |
| `gev/src/layers/headsup/records.js` | `normalizeAdvisorySnapshot(payload)` — validates a whole advisory FeatureCollection into plain rows (id, hazard class, severity, action, validity window, geometry, evidence count, signature state) or returns `null`. Pure. |
| `gev/src/layers/headsup/model.js` | Severity and hazard-class palette, `createAdvisoryOverlayEntry()`, cohort selection and limits, `mapAnalystRecord()`. |
| `gev/src/layers/headsup/expiry.js` | Pure `valid_from` / `valid_until` window maths against a supplied clock — which advisories are live, pending or expired at time *t*. Kept out of `records.js` so scene seeking can reuse it. |
| `gev/src/layers/headsup/index.js` | `createHeadsupLayer({ source, overlayHost })` — the layer object, its `CustomDataSource`, polygon/polyline/point geometry and overlay labels. |
| `gev/src/layers/headsup/records.test.mjs` | Normalization and rejection fixtures. |
| `gev/src/layers/headsup/expiry.test.mjs` | Window boundaries, including an advisory expiring mid-scene. |
| `gev/src/layers/headsup/ownership.test.mjs` | The upstream per-layer ownership regression, matching `earthquakes/ownership.test.mjs`. |
| `gev/src/app/layers/headsup.js` | `createApplicationHeadsup(options)` — injects `overlayHost`. |
| `gev/src/ui/styles/headsup.css` | Advisory card, severity accents and the HUD advisory-count block. Appended as the last `@import` in `gev/style.css` so it cannot reorder the existing cascade. |
| `viz/` (repo root, Viz track) | Any Heads Up-side bridge that is not a `gev/` module — fixture advisories and the dev feed shim. |

Upstream files we will have to edit, each a one-line registration (deferred to
the next build step, nothing touched yet):

- `gev/src/sources/reference.js` — add the `headsup` source key.
- `gev/src/app/sourceComposition.js` —
  `CATALOG_SOURCE_CONTRACTS.headsup = contract(['headsup'], ['getSnapshot'])`.
- `gev/src/app/constructCatalog.js` — the import and the
  `createApplicationHeadsup({ source: sources.headsup })` entry.
- `gev/src/data/layerState.js` — the `LAYER_STATE_REGISTRY` row, token `5`.
- `gev/src/ui/layerNames.js` — `headsup: msg('Heads Up Advisories')`.
- `gev/src/ui/layerPanel.js` — add `'headsup'` to the Events group.
- `gev/style.css` — the `headsup.css` import.
- `gev/scripts/package-boundaries.json` — a `headsup` group owning the new
  `src/layers/headsup/*` modules, plus the new paths in the
  `application-components` and `application-layer-construction` groups.
- `gev/scripts/format-scope.json` — the new files.

Gates to run on that step: `npm run format:check`, `npm run check:boundaries`,
`npm run layer-token:check` and `npm test`, from `gev/`.

## What the test layer actually added (D1.5), and where it differs

The plan above held, with four corrections found while wiring it up. The layer
drawn in this step reads the viewer socket directly:
`createHeadsupViewerSource` subscribes to `ws://localhost:8765/viewer`
(override with `VITE_HEADSUP_VIEWER_URL`) and exposes `getSnapshot()` beside
`subscribe()`, so the catalog's source contract still sees a snapshot source.
Vehicles are a `PointPrimitiveCollection` colored by operator — hashed, not
registered, because operator ids are free identifiers — with positions written
each frame from `sampleTrack()`; hazard areas are entity polygons Cesium draws
as ground primitives, with a clamped outline polyline.

On the draping: the polygon carries no `height`, no `perPositionHeight` and no
`heightReference`, and sets `classificationType: BOTH`. `heightReference` is
for geometry that HAS a height — set on a polygon without one, Cesium logs
`"heightReference must also have a defined height. heightReference will be
ignored"` (`GroundGeometryUpdater.getGeometryHeight`) and the polygon then
drapes only by accident, over terrain but not over the photoreal tiles. The
first cut of this layer had exactly that, so the rule is worth writing down for
the scene work: a draped polygon is one with NO height of its own, and `BOTH`
is what puts it on the 3D tiles. `ownership.test.mjs` pins all four.

1. **No share-link token yet.** The notes reserved token `5`. The layer is
   registered as `LOCAL_ONLY_LAYER_METADATA` in `constructCatalog.js` instead,
   on the `local-adsb` precedent: a share link cannot carry another browser to
   this machine's own simulator socket. A ledger token is permanent once
   merged, and the upstream ledger fixtures hard-code the next free digit, so
   `5` is better spent when the feed is a real Heads Up server. That keeps
   `src/data/layerState.js` and the token ledger untouched —
   `npm run layer-token:check` reports 0 new.
2. **Voice-off for now.** `layerManifest.test.mjs` requires every catalog layer
   to be in the voice manifest or in `VOICE_OFF_LAYERS`; a manifest entry
   regenerates the pinned Realtime tool schema. The layer is listed voice-off
   (`local Heads Up viewer socket; scene-driven (control_scene)`) so the
   published tool contract stays as it is until the feed is real.
3. **The source is constructed in `src/standalone/layerSources.js`**, not
   `src/sources/reference.js`: it is a provider-backed live feed, and only the
   standalone entry may read `import.meta.env`.
4. **`headsup.css` is not needed yet.** Nothing in this step paints HUD chrome:
   polygon fill and outline colors come from `model.js`, and advisory text goes
   through the overlay host. `gev/style.css` is therefore untouched. The
   advisory card and HUD counter block are what will need that stylesheet.

Also required, and not in the list above: `gev/package.json` needs the
`./layers/headsup` and `./layers/headsup/source` exports (the boundary checker
fails unless every group's export is declared), both locale catalogs need the
three new `msg()` strings, and two upstream membership counts move from 32 to
33 (`src/app/constructCatalog.test.mjs`, `src/app/sourceComposition.test.mjs`).

`gev/src/layers/headsup/expiry.js` landed as planned and the layer re-tests the
advisory windows twice a second from its frame hook, so an advisory expires on
its own clock without waiting for a socket frame.
