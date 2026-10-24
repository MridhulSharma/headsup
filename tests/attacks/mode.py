"""Which model the attack tests run against: mock by default, real on request.

The attacks are the one part of this suite that is worth running twice. In
mock mode they pin the structure - the closed schemas, the severity caps, the
playbook vocabulary, the token check, the audit flag - and that is what makes
them cheap enough to run on every commit. But three of the four are about what
happens when a *model* is handed hostile text, and a committed fixture cannot
be hostile to itself: the mock answer is whatever `tests/fixtures/llm/` says,
including the deliberately obedient one A1 scripts. The question "does a real
model, given a real injection, produce anything this system will act on" is
only answered by asking a real model.

So:

    uv run pytest -q tests/attacks                      # mock, free, default
    HU_LLM_MODE=claude uv run pytest -q -m llm tests/attacks   # once, in the
                                                               # ablation run

`attack_client()` is the one place either choice is made. With `HU_LLM_MODE`
unset or `mock` it answers with `LlmClient.mock()`, exactly as before. With
any other mode it answers with `LlmClient.from_env()`, which is the same
client the agents are built with - same budget, same meter, same audit
records, so a live attack run shows up in the spend the ablation reports.

**The live run is marked, so nothing spends money by accident.** A1 to A4
assign `LLM_MARK` to their `pytestmark`, which is the `llm` marker when and
only when the mode is live - so the default `-m "not llm"` sweep deselects
them outright if somebody has left `HU_LLM_MODE=claude` in their shell. A5
has no marker because it asks no model: a token check is a token check in
either mode. Belt and braces with `mode_from_env`'s own default, which is
mock.
"""

from __future__ import annotations

import pytest

from headsup.llm import LlmClient, Mode, mode_from_env

#: The mode this test session was started in. Read once, at import: a test
#: that monkeypatched `HU_LLM_MODE` half way through a run would otherwise be
#: deciding what the marker meant.
MODE: Mode = mode_from_env()

#: True when the attacks are being run against a real model and will spend.
LIVE: bool = MODE is not Mode.MOCK

#: What an attack module assigns to `pytestmark`: the `llm` marker in a live
#: run, nothing at all in a mock one.
LLM_MARK: list[pytest.MarkDecorator] = [pytest.mark.llm] if LIVE else []


def attack_client() -> LlmClient:
    """The client the attack tests hand to an agent.

    One client per call rather than a session-scoped fixture: several of the
    attacks build two agents and compare what each did, and sharing a client
    would share the disk cache between them.
    """
    return LlmClient.from_env() if LIVE else LlmClient.mock()
