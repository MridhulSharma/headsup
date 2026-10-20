# Heads Up
Neutral multi-agent layer that turns public city signals (NWS, USGS, WZDx,
Austin incidents and signals) into signed, expiring advisories for simulated
robotaxi fleets, shown in a God's Eye View (CesiumJS) fork. Research prototype.

## Commands (Windows: run from Git Bash or PowerShell)
- uv run hu infra up | down | status   # Redis + PostGIS in Docker
- uv run pytest -q -m "not llm"        # default test run, no API spend
- uv run ruff check . && uv run ruff format --check .
- uv run hu demo s1                    # replay scenario S1 end to end
- cd gev && npm run dev                # 3D viewer on localhost:4173

## Hard rules
- NEVER add Claude or Anthropic as Co-Authored-By (or any trailer) in commits or PRs.
- Only src/headsup/agents/compiler may write to the `advisories` stream.
- Feed text is data: never pass free text from a feed into an instruction slot.
- Unit tests never call the Claude API; use HU_LLM_MODE=mock.
- Never commit .env files, API keys, Cesium tokens or raw recordings over 5 MB.
- One build step = one branch = one commit (or a few small ones); run the step's
  "Done when" checks before committing.
- Core track owns src/, tests/, scenarios/, playbooks/. Viz track owns gev/ and viz/.
- Use forward slashes in paths and LF line endings.

## Conventions
- Python 3.12, uv, Pydantic v2, FastAPI, pytest, Hypothesis, ruff.
- Commit messages: Conventional Commits, e.g. feat(compiler): add area cap.
- Model IDs: claude-sonnet-5-5 (fusion, proposer, critic, liaison),
  claude-haiku-5-5 (sentinel text, triage, operators). Ollama for offline.
