"""Mock mode's replies: one JSON file per call site, under tests/fixtures/llm.

A call with `purpose="fusion_rationale"` is answered by
`tests/fixtures/llm/fusion_rationale.json`, and a test that needs a second
shape of answer passes `variant="conflict"` for
`fusion_rationale.conflict.json`. The whole default test run goes through
here, which is why a missing fixture is an error that names the file it wanted
rather than an empty object that fails validation three layers later.

Fixtures are checked in, so the mock answers are reviewable: changing what a
sentinel is allowed to conclude from an alert means editing a file in a diff.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import orjson

from headsup.llm.base import ENV_FIXTURE_DIR, LlmError

PROJECT_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "llm"


class FixtureStore:
    """The checked-in replies mock mode serves, read once and kept."""

    def __init__(self, directory: Path | str | None = None) -> None:
        self.directory = Path(directory) if directory is not None else _default_dir()
        self._cache: dict[tuple[str, str | None], dict[str, Any]] = {}

    @classmethod
    def from_env(cls) -> FixtureStore:
        """A store at `HU_LLM_FIXTURES`, or `tests/fixtures/llm`."""
        return cls(_default_dir())

    def path_for(self, purpose: str, variant: str | None = None) -> Path:
        name = f"{purpose}.{variant}.json" if variant else f"{purpose}.json"
        return self.directory / name

    def load(self, purpose: str, variant: str | None = None) -> dict[str, Any]:
        """The payload for one call site, or `LlmError` naming what is missing.

        A variant falls back to the plain fixture, so a caller can ask for a
        specific shape without every call site needing one.
        """
        key = (purpose, variant)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        candidates = [self.path_for(purpose, variant)] if variant else []
        candidates.append(self.path_for(purpose))
        for path in candidates:
            if path.is_file():
                payload = _read(path)
                self._cache[key] = payload
                return payload
        wanted = " or ".join(str(path) for path in candidates)
        raise LlmError(
            f"mock mode has no reply for purpose {purpose!r}: expected {wanted}. "
            "Add the fixture, or script a reply on the mock backend."
        )

    def purposes(self) -> list[str]:
        """Every call site that has a fixture, for `hu` and for the tests."""
        if not self.directory.is_dir():
            return []
        return sorted({path.name.split(".")[0] for path in self.directory.glob("*.json")})


def _read(path: Path) -> dict[str, Any]:
    try:
        payload = orjson.loads(path.read_bytes())
    except (OSError, orjson.JSONDecodeError) as exc:
        raise LlmError(f"{path} is not readable JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise LlmError(f"{path} must hold one JSON object, the reply's fields")
    return payload


def _default_dir() -> Path:
    override = os.environ.get(ENV_FIXTURE_DIR, "").strip()
    return Path(override) if override else DEFAULT_FIXTURE_DIR
