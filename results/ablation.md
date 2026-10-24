# Ablation: what Heads Up changes

*21 runs, 21 measurable, written 2026-10-24T14:11:42.186005+00:00.*

Every figure below was computed by reading the runs' hash-chained audit logs and nothing else. Each row of `ablation.csv` names the run id its numbers came from; `uv run hu audit verify <run_id>` re-checks that log line by line.

## What was run

- Wall-clock minutes per run: 1
- Model mode: mock
- Variants:
  - **V0** - baseline, no Heads Up: fleets ghost, rules only, no agents, seeds from `rules`
  - **V1** - rules only, no model: fleets live+ghost, rules only, mesh, critic, seeds from `rules`
  - **V2** - single orchestrator: fleets live+ghost, model, orchestrator, seeds from `ai`
  - **V3** - mesh without critic: fleets live+ghost, model, mesh, seeds from `ai`
  - **V4** - full Heads Up: fleets live+ghost, model, mesh, critic, seeds from `ai`

## s1_flood

| variant | runs | hazard entries | time to advisory (s) | assist peak | assist p95 wait (s) | blocked corridor (veh-min) | rides/veh/h | extra min per reroute | false-positive area-hours | LLM $/sim hour | p95 agent latency (ms) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| V0 | 2 | 299.0 | - | 299.0 | 3465.0 | 0.0 | 1.010 | - | 0.000 | 0.0000 | - |
| V1 | 2 | 299.0 | - | 299.0 | 3465.0 | 0.0 | 1.010 | - | 0.000 | 0.0000 | 3456 |
| V2 | 1 | 299.0 | - | 299.0 | 3480.0 | 0.0 | 0.988 | - | 0.000 | 0.0000 | 3176 |
| V3 | 1 | 299.0 | - | 299.0 | 3480.0 | 0.0 | 0.988 | - | 0.000 | 0.0000 | 3636 |
| V4 | 1 | 299.0 | - | 299.0 | 3480.0 | 0.0 | 0.988 | - | 0.000 | 0.0000 | 4208 |

## s2_blackout

| variant | runs | hazard entries | time to advisory (s) | assist peak | assist p95 wait (s) | blocked corridor (veh-min) | rides/veh/h | extra min per reroute | false-positive area-hours | LLM $/sim hour | p95 agent latency (ms) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| V0 | 2 | 128.0 | - | 128.0 | 2925.0 | 0.0 | 12.276 | - | 0.000 | 0.0000 | - |
| V1 | 2 | 93.5 | 5.1 | 76.0 | 1620.0 | 0.0 | 14.417 | - | 0.000 | 0.0000 | 5028 |
| V2 | 1 | 25.0 | 0.1 | 25.0 | 2910.0 | 0.0 | 15.109 | - | 0.413 | 0.0000 | 50063 |
| V3 | 1 | 75.0 | 5.1 | 57.0 | 1530.0 | 0.0 | 14.790 | - | 0.000 | 0.0000 | 5008 |
| V4 | 1 | 73.0 | 5.1 | 59.0 | 1620.0 | 0.0 | 14.770 | - | 0.000 | 0.0000 | 5033 |

## s3_incident

| variant | runs | hazard entries | time to advisory (s) | assist peak | assist p95 wait (s) | blocked corridor (veh-min) | rides/veh/h | extra min per reroute | false-positive area-hours | LLM $/sim hour | p95 agent latency (ms) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| V0 | 2 | 266.5 | - | 265.5 | 2310.0 | 0.0 | 10.454 | - | 0.000 | 0.0000 | - |
| V1 | 2 | 259.0 | 0.1 | 258.5 | 2310.0 | 6628.0 | 10.726 | -175.07 | 0.000 | 0.0000 | 40 |
| V2 | 1 | 240.0 | 0.1 | 238.0 | 2310.0 | 6112.1 | 11.250 | -4.96 | 0.000 | 0.0000 | 20188 |
| V3 | 1 | 244.0 | 0.1 | 243.0 | 2310.0 | 6164.3 | 11.220 | -218.04 | 0.000 | 0.0000 | 4972 |
| V4 | 1 | 243.0 | 0.1 | 242.0 | 2310.0 | 6165.3 | 11.203 | -313.21 | 0.000 | 0.0000 | 5009 |

## The headline

On `s3_incident`, the scenario a rules-only reading of the feed is expected to struggle with:

| variant | runs | blocked corridor (veh-min) | missed hazards | advisories |
| --- | --- | --- | --- | --- |
| V0 | 2 | 0.0 | 1.0 | 0.0 |
| V1 | 2 | 6628.0 | 0.0 | 1.0 |
| V4 | 1 | 6165.3 | 0.0 | 2.0 |

## Charts

- `hazard_entries_by_variant.png`
- `s2_queue_depth.png`
- `headline_s3.png`
