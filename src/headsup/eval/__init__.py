"""Ablation harness, variants V0-V4, metrics and charts.

    uv run hu eval --variants V0,V1,V2,V3,V4 --scenarios s1,s2,s3 \
                   --seeds rules=20,ai=3 --minutes 10 --out results/

Five modules, in the order a sweep uses them:

* `variants` -- the five configurations, as flags on `api.runner.ScenarioRunner`.
  There is no second pipeline here: V4 is what `hu demo` runs, and V0 to V3 are
  the same runner with the model, the mesh, the critic or the advised fleet
  taken away.
* `runs` -- the sweep itself: one run at a time, each sealing its own
  hash-chained audit log.
* `metrics` -- every figure in the table, computed by reading one of those logs
  and nothing else.
* `labels` -- the one thing a log cannot say: what was actually true, from the
  scenario descriptor's own `ground_truth` and `scoring` blocks.
* `tables` and `charts` -- `results/ablation.csv`, `results/ablation.md` and the
  three PNGs.

`cli` wires it to `hu eval`. Importing this package pulls in nothing heavy: the
simulator, the agents and matplotlib are imported by the modules that need them.
"""

from headsup.eval.variants import (
    AI_GROUP,
    DEFAULT_SPEC,
    ORDER,
    RULES_GROUP,
    SPECS,
    Variant,
    VariantSpec,
    parse_list,
    parse_seeds,
    seeds_for,
    spec_for,
)

__all__ = [
    "AI_GROUP",
    "DEFAULT_SPEC",
    "ORDER",
    "RULES_GROUP",
    "SPECS",
    "Variant",
    "VariantSpec",
    "parse_list",
    "parse_seeds",
    "seeds_for",
    "spec_for",
]
