<p align="center">
  <img src="docs/signum.webp" width="100%"
       alt="Signum — the wordmark over a dark lattice of city blocks lit in colour">
</p>

# Signum

> **The code says `headsup`.** That was the working name and it stays as the
> identifier throughout: the repository, the `headsup` Python package, the `hu`
> CLI, `src/headsup/`, and the viewer's `?mode=headsup`. Renaming those during a
> five-day build buys nothing a judge can see. The tool is Signum.

A neutral civic layer that turns public city signals into **signed, expiring
advisories** any robotaxi fleet can consume. The city publishes once; every fleet
reads the same feed. Operators decide whether to comply.

Research prototype, built in five days. Not affiliated with any city, agency or
fleet operator, and not for operational use.

---

## The Problem

A low-water crossing floods. A cluster of signals goes dark after a substation
fault. An engine company takes a lane for forty minutes.

The city knows about all three within seconds. Each one is already sitting in a
feed it publishes — a National Weather Service warning, a stream-gauge reading, a
signal-status endpoint, a dispatch record.

**None of it reaches the vehicles.**

That is the part people assume is solved. It isn't. The channel between a city and
an autonomous fleet today is a phone number and a PDF. Waymo publishes a
law-enforcement interaction protocol per vehicle model, runs a first-responder
hotline, and has trained more than 35,000 responders at over 150 agencies. That is
a serious programme, run properly — and it is also the exact shape of the problem.
It is manual, it is per-operator, and it scales by training humans.

Do the arithmetic a city actually faces. Three robotaxi operators means three
separate integrations, three hotlines, three escalation paths. A fourth operator
arrives and the city starts again from nothing. Every city repeats this for every
operator, forever, while the hazard is in the road and the clock is running.

**The gap is not sensing.** Every signal already exists, already public, already
timestamped. The gap is that there is no authoritative, machine-readable channel on
which a city can say one thing once and have every fleet hear it — and verify it
came from the city.

### What Already Exists, and What It Leaves Out

Nothing below is a direct substitute. That is the finding, not an excuse: the
adjacent work standardises one narrow hazard class, targets human drivers, or sells
the city a dashboard.

| | What it does | What it leaves open |
| --- | --- | --- |
| **WZDx** (US DOT) — the closest precedent | A GTFS-style spec for publishing harmonised work-zone data to OEMs and navigation apps | A format, not a pipeline. It assumes the city already holds structured data, covers work zones only, and carries no signature, expiry or evidence trail |
| **HAAS Alert** | V2X digital alerting; transponders on emergency apparatus broadcast into Waze and some OEM vehicles | The emitter is a vehicle and the receiver is a human driver. Covers the responder's own presence, not civic hazards generally |
| **Waze for Cities** | A two-way data partnership; cities share closures and incidents, and get flow data back | One proprietary consumer, rendering to a person. A fleet cannot subscribe to a verifiable machine contract |
| **Rekor / Waycare** | AI incident detection and traffic-management dashboards for DOTs | Sells the city situational awareness. The output is a screen for an operator, not an instrument a third-party fleet consumes |

Read down that last column and the missing piece is the same every time: **a signed,
expiring, evidence-bound instrument that any fleet can verify and act on, published
once by the city, owned by no operator.**

That is what Signum is. A hazard becomes a signed GeoJSON advisory carrying an
action, an area, an expiry, and the evidence it rests on. Fleets subscribe — one
integration per city instead of one per fleet. Compliance stays the operator's call,
and every decision is recorded on both sides.

## Who It's For

Four buyers, one artifact. The advisory is the product, and each of these reads a
different part of it.

| | What they get |
| --- | --- |
| **City transport and emergency-management agencies** | Publish once. A hazard their existing feeds already describe becomes a structured advisory with no new integration per operator — and their integration cost stops scaling with the number of operators in town |
| **Robotaxi and AV fleet operators** | One signed feed per city instead of a bilateral arrangement per city. Full autonomy over whether to comply, and a record proving what they were told and when |
| **Emergency responders** (fire, EMS, police) | A scene already reported to dispatch produces a keep-clear corridor, without anyone phoning a fleet operator from the kerb |
| **Regulators and city auditors** | A hash-chained record of what was advised, on what evidence, and which operator did what about it — the thing that is impossible to reconstruct from a phone call |

The reason this generalises is that **nothing in the contract is specific to
robotaxis.** An advisory is a signed statement that an area has a hazard class, a
recommended action and an expiry. Any consumer that can check a signature and read
GeoJSON can act on it: delivery robots and sidewalk fleets, drone corridors, transit
and school-bus routing, commercial telematics, mapping providers, insurers pricing
exposure in real time. The prototype proves the channel on robotaxis because that is
where the regulatory pressure is sharpest today — not because the channel is
narrower than that.

## How It Works

Public feeds → agents → a deterministic gate → signed advisory → fleets.

| Stage | What it does |
| --- | --- |
| **Ingest** | NWS alerts, USGS gauges, Austin signal status and dispatch incidents, recorded and replayable at 1–60× through the same parse path as live |
| **Sentinels** | Per-class detectors. Flood pairs NWS polygons with gauges above action stage; signal outage clusters flashing signals with DBSCAN; incident reads free-text dispatch records |
| **Fusion** | Groups candidates by class and geometry, combines confidence with noisy-OR, applies an evidence threshold, and **narrows the hazard to the reach the evidence actually corroborates** |
| **Proposer** | Chooses an action, constrained to the hazard class's playbook vocabulary |
| **Critic** | Sees the proposal and the raw evidence, never the proposer's rationale. Returns APPROVE, REVISE or ESCALATE |
| **Compiler** | The deterministic publishing gate. Not a model |
| **Operators** | Three simulated fleets with fixed personas: accept immediately, accept after 30–90 s, decline below 0.7 confidence |

### Guardrails USP

Every advisory by an agent passes a gate written in plain Python, and that
gate is what makes a model in the loop defensible:

- **Single writer.** A Redis ACL makes `compiler` the only user that can write to
  the `advisories` stream. Agents connect as `agent` and get `NOPERM`. This is
  enforced by the broker, not by convention — there is a test that asserts it.
- **Playbook vocabulary.** An action not in the hazard class's allowed list is
  rejected before the gate, so a model cannot invent an action.
- **Caps.** Per class: maximum area in km², maximum lifetime. Hypothesis property
  tests assert that for *any* generated proposal the output is never over the area
  cap, never past the lifetime cap, never an unknown action, and always verifies.
- **Two keys.** Severity 4+ or area over the two-key limit waits for a human
  approval token before publishing.
- **Never isolates a hospital.** The gate refuses any advisory that would close
  every access edge to a hospital or fire station.
- **Expiry and fail-open.** Advisories carry `valid_until` and lapse on their own.
  A stale feed stops producing hazards rather than producing confident ones.
- **Ed25519 signatures** over canonical JSON, verifiable by any consumer.
- **Hash-chained audit log.** Every candidate, hazard, proposal, verdict, compile
  result and ack. `uv run hu audit verify <run_id>` re-checks a log line by line.
  Every number in this README was computed by reading those logs and nothing else.

Feed text is treated as data, never as instructions. A model never sees free text
in an instruction slot.

## Scenarios

| | What it replays | What a rules engine does with it |
| --- | --- | --- |
| **S1** Flash flood, San Antonio | Real NWS flash-flood polygons and real USGS gauge history, April 2026 pattern | Fine — one polygon, one action |
| **S2** Signal blackout, Austin | A cluster of flashing signals on the real signal map (synthetic, flagged) | Fine |
| **S3** Emergency scene, Austin | Free-text dispatch records, some without coordinates | Struggles — this is where the agents earn their place |

Scripted events are flagged in the scenario YAML and badged on screen. S1 is real
public data.

### What It Looks Like

![S3 emergency scene, complete run: every agent-pipeline stage lit from FEEDS through OPERATORS, three fleets accepted, and a verified KEEP_CLEAR advisory in force over downtown Austin](docs/s3-complete.png)

**This is the whole system in one frame.** S3, the downtown Austin emergency scene —
the scenario built on free-text dispatch records, some without coordinates, which is
the case a rules engine cannot read.

Follow the rail down the right-hand side. One public feed is cited. A sentinel reads
the dispatch text and classes it `EMERGENCY_SCENE`. Fusion combines the readings at
**55% confidence** and narrows the hazard to the reach the evidence corroborates. The
proposer chooses `KEEP_CLEAR` from the hazard class's playbook vocabulary — it could
not have invented an action outside it. The critic, which sees the proposal and the
raw evidence but never the proposer's rationale, answers `APPROVE`. Only then does the
compiler — plain Python, not a model — sign and publish. All three fleets accepted.

The advisory card is the published artefact: `adv_894fdc88c560`, `KEEP_CLEAR`,
severity 2 of 5, 65,843 m², expiring in 43 minutes, issued by `headsup/austin`,
evidence `AUSTIN INCIDENTS`, and an Ed25519 signature this server **verified** rather
than assumed. Three advisories are in force.

**Time from first public signal to published advisory: 2.8 seconds.** That number is
measured by the page itself — from the first feed frame it saw to the first advisory
it received — not copied off a publisher's clock.

The status strip carries the comparison the whole project exists to make. Live fleet
against ghost fleet, the same scenario with no advisory: **235 hazard entries versus
251**, and an assist queue of **235 against 250**. The red figure is the counterfactual
— what the same city, the same minute, costs a fleet that never got the message.

On the map, the red volumes are what `KEEP_CLEAR` asks for: the approach to the fire
station held open for the second engine, before dispatch had called it a crash.

Nothing here is staged. `gev/scripts/railcheck.mjs` replays the server's own frames
through the viewer and prints the rail, so this picture is reproducible without a
live stack.

## Results

20 runs, real model (`claude-sonnet-5-5` / `claude-haiku-5-5`), **$0.249 total
spend** against a $25 guard — prompt and response caching did most of the work.
Variants: **V0** no Signum (baseline), **V1** rules only, no model, **V3** agents
without the critic, **V4** full pipeline. Full tables in
[`results/ablation.md`](results/ablation.md); per-run rows in
[`results/ablation.csv`](results/ablation.csv).

### What the 20 runs actually establish

Two things, on every row, with no seed-picking:

| | V0 no Signum | V1 rules only | V3 agents, no critic | V4 full pipeline |
| --- | --- | --- | --- | --- |
| **Missed hazards** (all rows) | **1** | 0 | 0 | 0 |
| Advisories published | 0 | 3–36 | 4–18 | 4–18 |
| Proposals the gate refused | — | 0 | 0 | **3–18** |

**Detection is the clean result.** V0 misses the hazard in all six of its runs;
every variant that reads the feeds misses none, in all fourteen of theirs. That is
the channel working, and it does not depend on a single metric's accounting.

**The refusals are the interesting column.** V4 is the only variant that ever has
the gate turn a proposal down — on S2 seed 43 it published 18 advisories and
refused 18 more. V3 runs the same agents without the critic and is refused nothing,
because nothing escalates. That column is the critic and the two-key rule becoming
visible in the data rather than only in the tests.

On the within-run counterfactual — each live fleet against its own ghost, same run,
same seed — the advised fleet accrues fewer blocked corridor minutes in **all seven
S3 runs that produce a corridor**, by 0.9–3.7%. Consistent in direction, small in
size, and reported as such.

### What we do not claim

**The `assist_requests` column is not trustworthy, so no reduction is claimed from
it.** An earlier version of this README headlined S3 assist load falling from 550 to
282.5 (48.6%). That figure is withdrawn.

In 7 of the 20 runs, `assist_requests` equals `assist_peak` *exactly* — including
V0 and V1 rows, so it is not a quirk of the agent path. A cumulative count landing
precisely on the concurrent peak in seven separate runs is an accounting fault, not
a coincidence, and until it is reconciled the column cannot carry a comparison. The
project document reaches the same conclusion and declines the same number; this file
now agrees with it.

### Where V1 keeps up, and we say so

On S3's blocked-corridor metric **V1 matches V4** (11,463 vs 11,642 vehicle-minutes)
and V4 is marginally *worse*. Both reach zero missed hazards. V0 shows 0 corridor
minutes only because it never creates a corridor at all.

So the case for the agent pipeline rests on what it can read, not on a speed-up: a
rules engine cannot turn free-text dispatch records with no coordinates into typed,
geocoded hazards, and V0's missed hazard is what that costs. Beyond that the case is
the gate, attack A3, and the refusal column above — **not** corridor minutes, and no
longer assist load. An honest margin beats a rigged one; that was the rule from day
one, and applying it to our own headline number is the test of it.

## Attacks

| | What it asks | Result |
| --- | --- | --- |
| **A1** Prompt injection | Hostile text in an NWS description, aimed at the model | No advisory the system will act on |
| **A3** Conflicting feeds | A flash-flood warning in force, and the only gauge under it reading normal | Refused on the area cap — see below |
| **A4** Assist surge | 300 requests in 2 minutes | Collapse to fewer than 10 decisions |
| **A5** Unverified responder | A plausible move order with no token, a wrong token, a prefix of a real one, a body naming its own identity | Every one refused; no path from that route to the compiler |

**A3 is the most interesting one, and it does not do what the plan asked.** The
plan said V4 would issue the advisory at reduced severity citing both sources. It
does neither. Severity is arithmetic over the feeds' own fields and nothing reduces
it because a gauge disagrees; and the sentinel cites gauges *above* stage, so a
normal reading is never attached as evidence. What actually happens is better: with
no gauge above stage there is no corroborated reach, so fusion keeps the polygon the
forecast office drew — 558 km² — and the gate refuses it on the 12 km² class cap.
That is a refusal **no second key can rescue**: not "nobody has approved this", but
"this advisory may not exist."

| gauge | geometry | area | the gate |
| --- | --- | --- | --- |
| 5.10 ft, over stage | the gauge's reach | 0.04 km² | held for a second key |
| 3.90 ft, normal | the office's polygon | 558 km² | refused on the area cap |

Four orders of magnitude off one reading, and it fails in the safe direction. The
tests record this rather than asserting the behaviour the plan described.

A5 asks no model by design — a token check is a token check — so it runs in the
free sweep rather than the paid one.

## Honest limitations

**Measurement**

- **Hazard entries were flat across variants because a KEEP_CLEAR advisory could
  not move them, which was a bug and is now fixed.** The movement gate decided
  whether an operator had been told to stay off an impassable edge by looking only
  at `no_entry`. A corridor populates `keep_clear_edges` and never `no_entry`, so
  it offered no protection: vehicles drove onto the scene, counted an entry and
  stalled in it. On the fixture scene the advisory changed entries by exactly 0%.
  The gate now honours both, and the same fixture falls from 24 entries to 8 — a
  67% reduction, with assist requests falling 24 to 8 alongside it and completed
  rides unchanged. **The committed ablation in `results/` predates this fix and
  still shows the flat figures.**
- **A separate flatness remains real for signal outages.** There an "entry" counts
  a vehicle passing through a dark signalized node, and under ALL_WAY_STOP the
  vehicle still passes — it just stops first. That metric genuinely does not
  capture the behaviour change, and no gate fix touches it.
- **`p95_agent_latency_ms` is unreportable for V1.** Four of six V1 rows read
  14–15 hours; the rules-only path has a timestamp bug. V3/V4 figures (5–83 s) are
  sound. Do not read the V1 latency column.
- **`rerouted: 0` on every S2 row is correct, not broken.** Only NO_ENTRY removes
  edges from routing. S2's actions are ALL_WAY_STOP, DENSITY_CAP and SPEED_CAP, none
  of which reroute — and KEEP_CLEAR does not either: it forbids *stopping* in the
  corridor, not travelling through it, which is what keeping a fire-station approach
  clear actually means. 18 advisories published, 14 acks, all three operators
  ACCEPTED.
- **The viewer's `rerouted` readout is always 0, in every scenario.** It counts
  vehicles carrying a `rerouted_by` field, and `VehicleState` has no such field — it
  never reaches the wire. The deflection trail in the viewer's legend cannot draw for
  the same reason. The simulator does reroute under NO_ENTRY; the viewer has no way
  to say so.
- **The D3.1 target was a 3× ghost/live assist-queue peak ratio. Actual is ~1.2×.**

**Coverage**

- **S1 is excluded from the ablation.** The harness caps a run at 600 simulated
  minutes and S1's feed window is 630, so every S1 run clamps and produces
  byte-identical results. S1's pipeline is fine; the harness budget is the limit.
  S1 is exercised end to end by the e2e suite and the live demo instead.
- **n = 2 seeds per agent variant, n = 3 per rules variant.** Shared-seed means are
  used above wherever variants are compared. Variant V2 (single orchestrator) was
  cut for budget.
- **One short run.** `eval_s3_incident_V3_s42` ran 410 simulated minutes instead of
  599.5 after a Redis read timeout, so its blocked-corridor figure is not comparable.
- **Two V3 rows show `llm_usd` 0.0 with non-zero `llm_calls`.** Those are full cache
  hits against the seed-42 runs, not silent fallback to rules.
- **Two-key approvals in the evaluation were granted by a stand-in operator**
  (`eval_operator`), not a person. The gate's requirement is real; the approver was
  automated so the sweep could run unattended.

**Performance**

- **The two-key approval control never appears in the viewer, and cannot.** The rail's
  Approve button is drawn for a proposal whose latest compiler answer is
  `PENDING_APPROVAL`, and there is no compile stream on the bus: `CompileResult` lives
  inside the compiler service and never reaches the viewer socket. A proposal held for a
  second key never becomes an advisory, so nothing about it is published at all. The
  COMPILER row is therefore derived from published advisories and can only ever read
  `SIGNED` — the gate's refusals and holds are real, recorded in the audit log, and
  exercised by the tests, but they are invisible on screen. Closing this needs one
  additive frame on the server, not a viewer change.
- **The viewer validated pipeline frames against a contract the server never spoke**,
  for the whole build: six validators read candidate, hazard, proposal, verdict, ack and
  counter frames out of a nested envelope under field names the API does not use, so
  every one was silently rejected and the rail, the stage cards and the status strip sat
  at their defaults on a pipeline that was running correctly. Nothing caught it because
  `viz/mock_stream.py` only ever emitted `telemetry` and `advisory` — no test on either
  side had ever put a real pipeline frame through a real validator. It is fixed, and the
  frames both sides agree on are now generated from the server's own builders rather
  than written twice: `scripts/dump_viewer_frames.py` dumps them,
  `tests/unit/test_viewer_frame_fixture.py` fails when they drift, and
  `records.contract.test.mjs` feeds them to the real validators. Ten of those seventeen
  tests fail against the pre-fix code. The lesson is in the shape of the bug: a contract
  with two hand-written ends and no shared example will drift, and stay quiet about it.
- **The 30 fps Gate 1 floor is not reachable on this hardware.** Intel Iris Xe with
  Google photoreal 3D tiles holds a median ~12–13 fps at 1920×1080 with 1,000
  vehicles and 200 trails. That is a measured hardware fact, not an open task.
  Levers exist (fewer vehicles, higher screen-space error, shorter trails) and are
  documented in `docs/gev-notes.md`.
- **One pre-existing test failure in the vendored viewer**
  (`gev/src/terrainHeightsBounds.test.mjs`) in upstream code this project does not
  touch; it fails identically in isolation. `scripts/package-boundaries.json`
  likewise drifts from prettier. Both are vendored and unmodified here.
- **One Python unit test is flaky under full-suite load.**
  `test_api_app.py::test_a_known_operator_gets_a_socket_with_no_scenario_running`
  intermittently raises `CancelledError` out of the websocket teardown when the
  viewer tailer is cancelled mid-retry against the closed Redis port that test
  deliberately uses. It passes in isolation (14/14 in 7 s) and cannot occur with
  Redis running, because the tailer only enters the retry loop when the bus is
  unreachable. Full suite: 1,644 passed, 1 flaky.

**Scope**

- The responder liaison is a boundary, not a pipeline: a verified move order is
  recorded and listed, and nothing carries it toward the compiler. Counter-offers
  and corridor negotiation are not built.
- Three operator personas, one city pair, mesoscopic simulation. No real fleet has
  consumed this.

## Running it

Requires Python 3.12 with [uv](https://docs.astral.sh/uv/), Node 24.14+, and
Docker Desktop.

```bash
git config core.hooksPath .githooks   # activate the co-author guard
cp .env.example .env                  # then fill in your keys

uv run hu infra up                    # Redis + PostGIS
uv run pytest -q -m "not llm"         # unit, property and contract tests, no spend
uv run hu world build --city san_antonio

uv run hu demo s1                     # replay S1 end to end (Ctrl+C to stop)
cd gev && npm run dev                 # viewer on localhost:4173?mode=headsup
```

The viewer opens on a calm landing view; **Test Scene** launches a replay.
`?mode=full` restores the original upstream application.

Paid runs are opt in and marked. `uv run pytest -q -m "not llm"` cannot spend money
even with `HU_LLM_MODE=claude` left in the shell:

```bash
HU_LLM_MODE=claude uv run pytest -q -m llm tests/attacks     # the live attack run
uv run hu eval --variants V0,V1,V3,V4 --scenarios s2,s3 \
  --seeds rules=3,ai=2 --minutes 10 --out results/
```

## Credits

The 3D viewer in [`gev/`](gev/) is a vendored copy of
[MridhulSharma/gods-eye-view](https://github.com/MridhulSharma/gods-eye-view), a
fork of [bilawalsidhu/gods-eye-view](https://github.com/bilawalsidhu/gods-eye-view)
by Bilawal Sidhu, used under the MIT License. See [`gev/NOTICE`](gev/NOTICE) and
[`gev/LICENSE`](gev/LICENSE).

Public data: NWS/NOAA alerts, USGS Water Data, City of Austin open data. Base
imagery via Cesium ion and Google Photorealistic 3D Tiles.

## License

The vendored `gev/` directory remains under its own MIT license. A license for the
rest of the repository is not yet chosen.
