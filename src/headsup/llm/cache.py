"""The disk response cache: the same prompt is paid for once.

Keyed by sha256 of the model, the system blocks, the turns and the JSON Schema
of the model the reply has to satisfy - everything that could change the
answer, and nothing else. A replay of the same scenario therefore costs
nothing after the first run, and changing a contract or a prompt by one
character is a miss rather than a stale hit.

    cache = ResponseCache.from_env()
    key = cache.key("claude-haiku-5-5", request)
    hit = cache.get(key)

Entries hold the validated payload, not the raw reply, so a hit is a
`model_validate` away from the object the caller wanted. A file that no longer
validates against its schema - or is half-written, or hand-edited - is treated
as a miss, never as an error: a cache is an optimisation and must not be able
to break a run.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import orjson

from headsup.llm.base import ENV_CACHE_DIR, Request

PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: Default location. `data/cache/` is gitignored, so nothing here is committed.
DEFAULT_CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "llm"

#: Entry format. Bumped when the stored shape changes, which retires every
#: entry written by an older build without anyone having to clear the directory.
CACHE_VERSION = 1


def fingerprint(model: str, request: Request[Any]) -> str:
    """sha256 over (model, system, messages, schema), as hex.

    The payload is serialised with sorted keys, so two processes agree on the
    key for the same request whatever order their dicts happened to be built
    in. `purpose` is in the key as well: two call sites that happen to send
    identical text are still different questions.
    """
    payload = {
        "version": CACHE_VERSION,
        "model": model,
        "purpose": request.purpose,
        "system": list(request.system),
        "messages": [[message.role, message.content] for message in request.messages],
        "schema": request.schema.model_json_schema(),
    }
    digest = hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS))
    return digest.hexdigest()


class ResponseCache:
    """Validated replies on disk, one JSON file per prompt fingerprint.

    `enabled=False` makes every lookup a miss and every store a no-op, which
    is how mock mode runs: its replies already come from fixtures on disk, and
    writing them into the real cache directory would let a mocked answer be
    served to a later `claude` run.
    """

    def __init__(self, directory: Path | str | None = None, *, enabled: bool = True) -> None:
        self.directory = Path(directory) if directory is not None else _default_dir()
        self.enabled = enabled
        self.hits = 0
        self.misses = 0

    @classmethod
    def from_env(cls, *, enabled: bool = True) -> ResponseCache:
        """A cache at `HU_LLM_CACHE`, or `data/cache/llm`."""
        return cls(_default_dir(), enabled=enabled)

    def key(self, model: str, request: Request[Any]) -> str:
        """The fingerprint this request is stored under for this model."""
        return fingerprint(model, request)

    def path_for(self, key: str) -> Path:
        """Where one entry lives: sharded by the first two hex digits."""
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        """The stored payload for a key, or None on a miss or an unreadable file."""
        if not self.enabled:
            self.misses += 1
            return None
        path = self.path_for(key)
        try:
            entry = orjson.loads(path.read_bytes())
        except (OSError, orjson.JSONDecodeError):
            self.misses += 1
            return None
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if entry.get("version") != CACHE_VERSION or not isinstance(payload, dict):
            self.misses += 1
            return None
        self.hits += 1
        return payload

    def put(self, key: str, payload: dict[str, Any], *, model: str, purpose: str) -> None:
        """Store one validated payload, replacing whatever was there.

        Written to a temporary file and renamed, because a replay interrupted
        with Ctrl+C must not leave a half-written entry that the next run then
        has to recognise as damaged.
        """
        if not self.enabled:
            return
        path = self.path_for(key)
        entry = {
            "version": CACHE_VERSION,
            "model": model,
            "purpose": purpose,
            "payload": payload,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            scratch = path.with_suffix(".tmp")
            scratch.write_bytes(orjson.dumps(entry, option=orjson.OPT_INDENT_2))
            scratch.replace(path)
        except OSError:
            # An unwritable cache directory is not a reason to fail a run.
            return

    def clear(self) -> int:
        """Delete every entry. Returns how many files went."""
        if not self.directory.is_dir():
            return 0
        gone = 0
        for path in self.directory.rglob("*.json"):
            path.unlink(missing_ok=True)
            gone += 1
        return gone


def _default_dir() -> Path:
    override = os.environ.get(ENV_CACHE_DIR, "").strip()
    return Path(override) if override else DEFAULT_CACHE_DIR
