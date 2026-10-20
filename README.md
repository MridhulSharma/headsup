# Heads Up

> **"Heads Up" is a placeholder name.** It stands in for the Civic Layer plan
> until the project is named properly, and it will be renamed before release.

A neutral multi-agent layer that turns public city signals (NWS, USGS, WZDx,
Austin incidents and signals) into signed, expiring advisories for simulated
robotaxi fleets, shown in a God's Eye View (CesiumJS) fork.

Research prototype. Not affiliated with any city, agency or fleet operator, and
not for operational use.

## Status

Day 0: guarded but empty repo. The build plan lives in
[`docs/build-packet.md`](docs/build-packet.md); standing rules for Claude Code
are in [`CLAUDE.md`](CLAUDE.md).

## Getting started

```bash
git config core.hooksPath .githooks   # activate the co-author guard
cp .env.example .env                  # then fill in your keys
uv run hu infra up                    # Redis + PostGIS in Docker
uv run pytest -q -m "not llm"
```

## License

TBD before release.
