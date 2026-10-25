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

## The live layer (D2.7): ports, gestures and the frame budget

### Two ports, one ladder

The viewer asks the **real API on 8765 first** and falls back to the **mock on
8766**, for both of the contracts it speaks:

| Contract | First | Fallback | Override |
| --- | --- | --- | --- |
| Viewer socket | `ws://localhost:8765/viewer` | `ws://localhost:8766/viewer` | `VITE_HEADSUP_VIEWER_URL`, `VITE_HEADSUP_VIEWER_FALLBACK_URL` |
| Scenario API | `http://localhost:8765` | `http://localhost:8766` | `VITE_HEADSUP_SCENARIO_URL`, `VITE_HEADSUP_SCENARIO_FALLBACK_URL` |

`viz/mock_stream.py` therefore defaults to **8766**, not 8765 as it did before.
One port each is what makes "the API is not running" something the page
discovers instead of something a developer has to be told, and the feed chip
bottom right reads `LIVE`, `MOCK` or `DOWN` accordingly.

Two rules keep the ladder honest, and both are pinned by tests:

- A socket that **never opened** means nothing is listening, so the next
  candidate is tried after 250 ms. A socket that opened and later **dropped**
  keeps its endpoint for the first retry — it was serving a moment ago, and
  moving a working page onto the mock because the API restarted is a worse
  answer than waiting. Only when that retry also fails to open does the walk
  continue, and a full lap hands pacing back to the reconnect ladder.
- On the HTTP side, an origin that **refuses** (any status) or answers
  unreadably is *not* overridden by the fallback. Something is listening
  there; asking a second service to answer instead would make the page lie
  about which API it is driving.

### What the layer draws

Five surfaces, three of them new in this step:

| Surface | Primitive | Why |
| --- | --- | --- |
| Vehicles | `PointPrimitiveCollection` | The one thing that moves every frame. |
| Heading chevrons | `BillboardCollection` | One shared white arrow, tinted per operator, with `alignedAxis` set to the world-space direction of travel — so the bearing reads from an oblique camera and from straight down alike, which a screen-space `rotation` cannot do. |
| Hazard areas and corridors | entity polygons and clamped polylines on the layer's own `CustomDataSource` | Draped on the photoreal tiles (`classificationType: BOTH`, no height of their own). |
| Deflection trails | `PolylineCollection` | The last 30 s of every vehicle an advisory moved. |
| Dispatch beams | `PolylineCollection` | The 1.5 s in which an advisory reaches one fleet. |

### The four gestures, and the restraint rule

One **pulse** per published advisory version, one **beam** per operator per
publication, one **flash** per answer, and a **refusal tag** that stands as
long as the refusal does. Every one of them is gated on a scenario actually
running, because the landing view shows the idle feed, where an advisory in
force is context rather than news. They are also *seeded silently* at enable:
a page that opens mid-run shows where the run got to, not a replay of the
publication that got it there.

Three implementation notes worth keeping:

- **The pulse animates a colour attribute, not geometry.** Draped geometry
  re-tessellates whenever its positions change, so the advisory's material is
  a `ColorMaterialProperty` wrapping a `CallbackProperty`. Cesium's
  `StaticGeometryColorBatch` and `StaticGroundPolylinePerMaterialBatch` update
  the per-instance colour attribute for a non-constant colour, and
  `PolygonGeometryUpdater._isDynamic` explicitly exempts a
  `ColorMaterialProperty` from the dynamic path — so the swell costs one
  attribute write, and the drawn set is still rebuilt only when the live
  advisories change.
- **A trail's fade is in the shader.** `PolylineGlow`'s `taperPower` fades the
  start of a line, so trails are written oldest-first and tapered rather than
  having 200 alphas written every frame. Positions are only written when a
  tick lands.
- **One material per polyline, never one shared per operator.**
  `Polyline._destroy` destroys its material, so a shared instance would be
  freed out from under every other polyline using it. Sharing is not needed
  for batching: a `PolylineCollection` merges consecutive polylines whose
  material uniforms have equal *values* into one draw command.

`Cesium.Material` compiles its uniforms against the DOM and cannot be
constructed without a canvas, which is why the layer takes
`services.materials.polylineGlow` — a scene resource, arriving the way the
render governor does, so beams and trails can be exercised off-screen.

### Holding the frame budget with 1,000 vehicles

Four costs had to be bounded, and the first of them was measured rather than
reasoned about — it was an order of magnitude worse than it looked.

- **Ground heights, by cell and not by vehicle.** `scene.sampleHeight` reads
  the depth buffer, which costs a render pass per call: **12 ms per call** on
  the laptop GPU this was first measured on. A rotating window of 24 vehicles
  a tick bounds the *count* and not the cost — it is 600 ms of every second,
  and it held the load case to 3 fps. Height is a property of the ground, not
  of the vehicle standing on it, so it is now cached per `GROUND_CELL_DEGREES`
  cell (~130 m) and shared by every vehicle over that cell, with at most
  `GROUND_CELLS_PER_TICK` (4) cells newly sampled per tick. The cost is a
  bounded transient while a view fills in and nothing at all in the steady
  state, at any fleet size. That one change took the load case from 3 fps to
  10 on the same machine.
- **Degrees only where a bearing needs them.** A frame interpolates in
  Cartesian space (`Cartesian3.lerp` between the leg's two endpoints) rather
  than calling `Cartesian3.fromDegrees` per vehicle per frame, which was the
  largest single entry in the frame profile. Over one 2 Hz leg the chord and
  the arc differ by a fraction of a millimetre. The endpoints are still the
  reported positions, a retarget reuses the dot's current Cartesian as the new
  leg's start, and the oriented cohort — bounded by `CHEVRON_FLEET_LIMIT` —
  is the only one that still samples degrees per frame.
- **Colours parsed once.** `Color.fromCssColorString` parses and allocates;
  `operatorColor` and `vehicleAppearance` were being re-asked for a constant
  answer a thousand times a tick. Both are memoised now, and hand back a
  shared (frozen) object — safe because every Cesium setter clones the colour
  it is given. The appearance cache is capped, because operator identifiers
  arrive on the feed and feed text is data.
- **Trails at 1 Hz, not 2.** Every append rewrites a whole polyline — Cesium
  re-encodes its positions and recomputes its bounding sphere — so a
  thirty-second line is sampled at `TRAIL_SAMPLE_MS` and a tick that adds
  nothing rewrites nothing.

Chevrons are the fifth bound and a design decision as much as a budget one:
past `CHEVRON_FLEET_LIMIT` (400) they are not drawn *and not created*, because
a chevron per vehicle stops being a bearing and becomes a texture of arrows
long before a thousand of them. `getStats().oriented` reports which side of the
line the fleet is on, and the legend says `not drawn`.

Per-frame writes go through reusable `Cartesian3`s per marker, tracks are
retargeted in place, and the oriented cohort's sample object is reused, so a
frame of a thousand vehicles allocates nothing.

### What the gate actually reads on a laptop

`npm run qa:headsup-load` **fails its 30 fps floor on integrated graphics, and
the floor is not reachable there by any amount of work in this layer.** The
numbers from the machine this step was built on — Intel Iris Xe, headless
Chrome on ANGLE/D3D11, Google photoreal tiles, the dev server:

| What is drawn | fps (median) |
| --- | --- |
| The fork alone, same camera, no Heads Up content | 13 |
| 1,000 vehicles, 200 trails, hazard, beams, tags | 10 |
| 1,000 vehicles, before the ground-height cache | 3 |

The first row is the ceiling, and the 1,000-vehicle cohort costs about 3 fps
of it. So the honest reading of this gate has two parts: the layer's own
marginal cost is now small, and **the 60 fps budget cannot be demonstrated on
this hardware** — it needs a discrete GPU to be a claim rather than a hope.
The harness prints the renderer string for exactly that reason, and the feed's
own worst tick beside the fps, so a run that was limited by the generator
cannot be read as a run that was limited by the layer.

### Measuring it

```bash
uv run python viz/load_stream.py          # 1,000 vehicles, 200 trails, 60 s
cd gev && npm run dev                     # then ` toggles the fps readout
cd gev && npm run qa:headsup-load         # both ends, recorded
```

`viz/load_stream.py` is the load case as a *feed* rather than a fixture,
because the budget is about frames arriving at the simulator's cadence: 1,000
vehicles on a lattice of downtown Austin arterials, 800 advised and 200 ghosts,
with the five advised lanes that cross the hazard swinging clear of it and
carrying 200 deflection trails. It also publishes one advisory, one acceptance
and one refusal, so the pulse, the beams, the flash and the tag are all on
screen while the frame time is being measured. The ghost lanes cross the hazard
and keep going, which is what a control group is for.

`gev/scripts/qa-headsup-load.mjs` drives both ends and records **the fork's own
fps readout** (`src/ui/frameRateMonitor.js`, the backquote key) rather than
counting frames through a hook of its own — a number from the readout a
developer looks at cannot disagree with what they see. It reports the p5 rather
than the minimum, because one stalled second during a tile load is not the
steady state the budget is about, and prints the feed's own worst tick beside
it: a browser holding 60 fps against a generator that fell behind has not been
tested.

## The ops console and the agent rail (D3.6)

The rail opened in D2.0 is now filled, and three things on the map joined it.
Built against `viz/mock_stream.py`: **D3.7 has not landed, so there are no
real S3 events yet** — the dispatch-text card and the `KEEP_CLEAR` corridor
were built against scripted mock frames, the way D2.0 built the panel.

### The wire contract grew by four frame kinds

The rail is a record of what the agents did, so the reasoning has to survive
the frame that carried it. `candidate`, `hazard`, `proposal` and `verdict`
were previously *tallied* and are now normalized whole, and four kinds are
new or widened:

| Frame | Carries |
| --- | --- |
| `compile` | The publishing gate's own answer: `SIGNED`, `REJECTED` or `PENDING_APPROVAL`, with `reasons[]`, the signature state and a `two_key` flag. |
| `assist` | Every remote-assist request open right now, whole-state like telemetry, each with a `cluster` id once surge triage has grouped it. |
| `counters` | `hazard_entries` and `assist_queue`, each `{live, ghost}`. |
| `ack` (widened) | `DELAYED` beside `ACCEPTED` and `DECLINED`, plus `action_taken` and `reason`. |

Three readings are deliberately pessimistic, and each is pinned by a test: a
critic decision this build cannot parse reads as `ESCALATE`, a compiler status
it cannot parse reads as `REJECTED`, and an ack status it cannot parse reads
as `DECLINED`. The safe reading of an answer we cannot read is never "yes".

`signature_verified` is **three-valued**. `true` and `false` are claims about
a check that ran; `null` means none was reported, and the Compiler card then
says `present, unverified` rather than either of the other two. Only the
issuer's key can turn that into a yes, and this console does not hold it.

### Where the rail's rows come from

`gev/src/headsup/pipeline.js` is new and pure: a feed snapshot and the layer's
stats in, a frozen view out. `gev/src/headsup/console.js` renders it. Three
rules shaped it:

- **A stage lights from its own event, never from a timer.** `lit` is true
  because a record exists and `firstAt` is when the first one arrived, so a
  stage lights once and stays lit. Nothing animates.
- **Nothing is inferred from a neighbour.** A compiler that never answered
  leaves its row unlit even with an advisory on the map, because "an advisory
  exists, so the compiler must have signed it" is the reasoning an audit trail
  replaces.
- **Four tones, not a severity scale.** `wait` is a pipeline stopped for a
  person; `alert` is one that produced a refusal. Conflating them would hide
  the one state a human can act on — which is the whole of S3.

`FEED_LABELS` names the public feeds, and a source id this build has no name
for is shown **as it arrived**: a chip reading `travis_county_oem` is a better
answer than one reading `Unknown`, because which feed an advisory rests on is
the question that row exists to answer.

**Time to advisory** is measured between two instants *this page observed* —
the first candidate or hazard frame, and the first advisory — recorded in
`source.js` as each frame lands. Not from the timestamps inside the frames: a
number copied off a publisher's clock is a claim, not a measurement.

### The second key

`POST /approvals/{proposal_id}`, on `createScenarioClient`. The Compiler card
grows an Approve button only while that proposal's **latest** compiler answer
is `PENDING_APPROVAL` *and* says `two_key` — once it is signed or rejected the
decision is not anybody's to make any more, and the button goes.

Two rules, both tested: only `{"state": "approved"}` counts as a second key
(a 200 carrying anything else has not told the page a key was recorded, and a
button that *looked* as though it worked is the worst failure a two-key
control can have), and a refusal is **never** retried against the fallback
origin — something is listening there, and asking a second service to override
the first one's "no" would make the page lie about which API it drove.

The console does not redraw itself as published when the call returns. What
re-enables the pipeline is the compiler's next frame.

### What joined the map

- **Ghost fleet in faint red.** It was grey, which read as "inactive" — the
  opposite of what is happening: the ghost cohort is the one driving into a
  hazard nobody told it about. Faint because the ghosts are the background of
  the comparison. `#ef476f` came OUT of the operator palette at the same time,
  so no fleet can hash to a colour that now means "unadvised" or "refused".
- **Remote-assist markers**, a fifth `BillboardCollection` (so `init` now adds
  five). One ring per request, collapsing to one ring with a count badge
  wherever triage set a shared `cluster` id. The collapse follows **triage's
  own id and never a distance this layer picked** — a viewer that clustered by
  proximity would be asserting a triage decision nobody made, and the badge's
  number would stop meaning "this many requests were grouped". The badge is
  world text, so it goes through the overlay host.
- **`KEEP_CLEAR` corridors glow** via a wider, fainter clamped polyline
  *under* the line rather than a `PolylineGlow` material. Both carry a colour
  the publication pulse animates, and the pulse's whole design rests on
  Cesium updating the per-instance colour attribute of draped geometry for a
  `ColorMaterialProperty` without re-tessellating it
  (`PolygonGeometryUpdater._isDynamic` exempts it). A glow material would move
  the corridor onto another batch and put that at risk for an effect two lines
  already give. Keyed on the ACTION, not the geometry: a `SPEED_CAP` along a
  road is a limit on a route, not a lane somebody asked to be kept clear.

### The counter strip, and keeping the centre clear

Four readings, three of them a comparison: hazard entries live/ghost,
vehicles rerouted, time to advisory, assist queue live/ghost with a
twenty-four second sparkline. A single total would hide the only claim the
demo makes. A tally the feed has not reported shows `—`, not `0`: "nobody has
told us" and "nothing happened" are different claims. `rerouted` is the one
counter the viewer derives for itself, because it is a property of the
telemetry already on screen.

The sparkline draws two bars per column — the ghost queue as a wide faint
envelope, the live one as its core — rather than a stack, which would draw
their sum: a number nobody is claiming.

**The 1920x1080 requirement is a test, not an eyeball.**
`gev/src/headsup/layout.test.mjs` reads the geometry out of the stylesheets —
five tokens in `headsup-mode.css`, upstream's `--right-rail-x`,
`.hud-bottom-right` and `#command-dock` — computes each reserved rectangle at
1920x1080, and asserts none of them touches the protected centre: the middle
**half** of the viewport in both axes (x 480-1440, y 270-810). A value it
cannot find in the CSS is a failure, not a skip.

That is why the rail scrolls rather than grows (`--headsup-rail-width: 380px`,
plus a `max-height`), and why the strip's height is capped: it sits in
upstream's bottom-right HUD corner 180px up, so a strip allowed to grow upward
would reach the centre whatever its width. The rail's 300px bottom reserve is
what clears `#command-dock`, which upstream centres along the bottom and which
runs underneath the rail's lane horizontally — the two can only be kept apart
vertically.

The rail stays hidden on the landing, and ending a replay closes its open card
in the same act rather than waiting for a render: nothing is guaranteed to
render between one replay ending and the next, and a hidden rail holding an
open card is two states disagreeing.

### What the mock had to grow

`viz/scenario_script.py` now scripts all three scenarios, chosen so that
between them the rail shows every state it can draw rather than only the
happy one:

| ID | Hazard | Critic | Compiler | Fleets |
| --- | --- | --- | --- | --- |
| S1 | `FLOOD` sev 4 | `APPROVE` | `SIGNED` | ACCEPTED, DECLINED |
| S2 | `SIGNAL_OUTAGE` sev 3 | `REVISE`, then `APPROVE` | `SIGNED` on the second proposal | ACCEPTED, ACCEPTED |
| S3 | `EMERGENCY_SCENE` sev 4 | `ESCALATE` | `PENDING_APPROVAL`, then `SIGNED` | ACCEPTED, DELAYED |

S3 is the two-key case and **stops** at its last timed step: nothing more goes
out until `POST /approvals/{id}` supplies the second key, and then the
remaining frames go out in one burst, because what paces them is a person
pressing a button. Its candidate carries a raw CAD dispatch line and the seven
fields the text sentinel pulled out of it, which is what the Sentinels card
shows side by side. Its advisory is a `LineString` — `KEEP_CLEAR` over the two
closed lanes of the 600 block of East 6th Street.

S3's ghost cohort **queues** instead of driving on (`_queued_ghost`): a
vehicle stopped behind an engine company cannot clear itself, so each ghost
drives in, reaches a slot inside the closed block and holds. That is what
makes the ghost assist queue grow from zero to six while the advised fleets'
stays empty, and what triage then collapses into one marker.

Approvals are per run and cleared on every start and stop: an approval is
consent for one advisory in one replay, never a standing permission for the
next one. An approval for a proposal the running scenario is not held on is
refused with a 409.

### The fps gate, unchanged

`npm run qa:headsup-load` still fails its 30 fps floor on this laptop, and
still cannot reach it: the fork alone draws this camera at 13 fps on Intel
Iris Xe, and the thousand-vehicle cohort costs about 3 of those.

The one confirming run on this step read **8 fps median (p5 4, max 12)**
against the 10 fps D2.7 recorded. That is 2 fps below the earlier figure and
it is **not** established that the difference is this step's: the run was
taken minutes after the full unit suite on the same machine, the feed reported
its worst tick 0 ms late (so the generator was not the limit), and the gate
was deliberately measured once rather than re-measured. The two surfaces this
step added are bounded by the feed's own cadence — assist markers are upserted
once a second, and a corridor halo is one more *static* clamped polyline, not
a per-frame write — so there is no mechanism here that should cost a steady
2 fps; but "should not" is a prediction, and the honest record is the number
with its uncertainty attached rather than a claim of no change. Anyone who
needs that attributed should take the ceiling and the loaded reading back to
back on an idle machine. See the D2.7 section for why the absolute number is
a property of the hardware either way.

## Known issues

- `npm run layer-token:check` cannot run in this layout: the script builds its
  pathspec as `src/data/layerState.js`, but `gev/` is vendored one level down,
  so `git rev-parse <ref>:src/data/layerState.js` fails with *path exists, but
  not*. Pre-existing, and not this fork's to fix from inside `gev/`.
  `gev/src/data/layerState.js` is unchanged from HEAD, so the ledger carries
  0 new tokens — which is what the check would have reported.
