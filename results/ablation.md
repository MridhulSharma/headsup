# Ablation: what Heads Up changes

*20 runs, 20 measurable, written 2026-10-25T14:33:27.642092+00:00.*

Every figure below was computed by reading the runs' hash-chained audit logs and nothing else. Each row of `ablation.csv` names the run id its numbers came from; `uv run hu audit verify <run_id>` re-checks that log line by line.

## What was run

- Wall-clock minutes per run: 10
- Model mode: claude
- Variants:
  - **V0** - baseline, no Heads Up: fleets ghost, rules only, no agents, seeds from `rules`
  - **V1** - rules only, no model: fleets live+ghost, rules only, mesh, critic, seeds from `rules`
  - **V3** - mesh without critic: fleets live+ghost, model, mesh, seeds from `ai`
  - **V4** - full Heads Up: fleets live+ghost, model, mesh, critic, seeds from `ai`

## s2_blackout

| variant | runs | hazard entries | time to advisory (s) | assist peak | assist p95 wait (s) | blocked corridor (veh-min) | rides/veh/h | extra min per reroute | false-positive area-hours | LLM $/sim hour | p95 agent latency (ms) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| V0 | 3 | 158.0 | - | 158.0 | 4350.0 | 0.0 | 15.011 | - | 0.000 | 0.0000 | - |
| V1 | 3 | 155.0 | 5.1 | 135.0 | 3030.0 | 0.0 | 15.254 | - | 1.219 | 0.0000 | 52853836 |
| V3 | 2 | 158.5 | 8.4 | 135.0 | 3105.0 | 0.0 | 15.313 | - | 0.000 | 0.0024 | 14360 |
| V4 | 2 | 153.0 | 9.1 | 131.5 | 3060.0 | 0.0 | 15.285 | - | 0.000 | 0.0073 | 37443 |

## s3_incident

| variant | runs | hazard entries | time to advisory (s) | assist peak | assist p95 wait (s) | blocked corridor (veh-min) | rides/veh/h | extra min per reroute | false-positive area-hours | LLM $/sim hour | p95 agent latency (ms) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| V0 | 3 | 278.0 | - | 278.0 | 3210.0 | 0.0 | 16.295 | - | 0.000 | 0.0000 | - |
| V1 | 3 | 275.0 | 0.1 | 274.0 | 3210.0 | 11463.5 | 16.340 | -252.23 | 0.000 | 0.0000 | 54611072 |
| V3 | 2 | 284.0 | 3.3 | 284.0 | 3195.0 | 11413.5 | 16.122 | -89.10 | 0.000 | 0.0024 | 81672 |
| V4 | 2 | 283.0 | 2.9 | 282.5 | 3195.0 | 11641.5 | 16.356 | -79.68 | 0.000 | 0.0011 | 80025 |

## The headline

On `s3_incident`, the scenario a rules-only reading of the feed is expected to struggle with:

| variant | runs | blocked corridor (veh-min) | missed hazards | advisories |
| --- | --- | --- | --- | --- |
| V0 | 3 | 0.0 | 1.0 | 0.0 |
| V1 | 3 | 11463.5 | 0.0 | 4.0 |
| V4 | 2 | 11641.5 | 0.0 | 4.0 |

## Charts

- `hazard_entries_by_variant.png`
- `s2_queue_depth.png`
- `headline_s3.png`
