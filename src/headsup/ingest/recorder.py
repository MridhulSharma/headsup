"""Writing raw feed payloads to disk, and reading them back for replay.

Layout under `data/recordings/<scenario>/`:

    raw/<source>.jsonl       everything a recording session fetched (gitignored)
    fixture/<source>.jsonl   the trimmed, committed version used by tests and demos

One JSON object per line, each a `RawPayload`: the body exactly as the feed
returned it, the URL it came from, and the moment it was fetched. Nothing is
parsed on the way in, so a recording keeps its value when an adapter changes.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

import orjson

from headsup.contracts import Source
from headsup.ingest.base import IngestError, RawPayload

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RECORDINGS_ROOT = PROJECT_ROOT / "data" / "recordings"

#: Raw capture first, the committed fixture as the fallback.
RAW_DIR = "raw"
FIXTURE_DIR = "fixture"
SEARCH_ORDER = (RAW_DIR, FIXTURE_DIR)


def scenario_dir(scenario: str, root: Path | None = None) -> Path:
    return (root or RECORDINGS_ROOT) / scenario


class Recorder:
    """Append raw payloads to one JSONL file per feed.

    Files are opened lazily, so a recording session that never hears from a
    feed does not leave an empty file behind to confuse the replayer.
    """

    def __init__(self, scenario: str, *, root: Path | None = None, subdir: str = RAW_DIR) -> None:
        self.scenario = scenario
        self.directory = scenario_dir(scenario, root) / subdir
        self._handles: dict[Source, BinaryIO] = {}
        self.counts: dict[Source, int] = {}

    def __enter__(self) -> Recorder:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def path_for(self, source: Source) -> Path:
        return self.directory / f"{source.value}.jsonl"

    def write(self, payload: RawPayload) -> None:
        """Append one payload to its feed's file."""
        handle = self._handles.get(payload.source)
        if handle is None:
            self.directory.mkdir(parents=True, exist_ok=True)
            handle = self.path_for(payload.source).open("ab")
            self._handles[payload.source] = handle
        handle.write(orjson.dumps(payload.to_wire()) + b"\n")
        self.counts[payload.source] = self.counts.get(payload.source, 0) + 1

    def write_many(self, payloads: Iterable[RawPayload]) -> int:
        written = 0
        for payload in payloads:
            self.write(payload)
            written += 1
        return written

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()


@dataclass(frozen=True, slots=True)
class Recording:
    """A directory of JSONL files, read back in feed-time order."""

    scenario: str
    directory: Path

    @classmethod
    def locate(cls, scenario: str, *, root: Path | None = None) -> Recording:
        """Find a scenario's recording: the raw capture, else the fixture.

        A fresh capture under `raw/` wins so that re-recording a scenario takes
        effect without touching the committed fixture.
        """
        base = scenario_dir(scenario, root)
        for subdir in SEARCH_ORDER:
            candidate = base / subdir
            if candidate.is_dir() and any(candidate.glob("*.jsonl")):
                return cls(scenario=scenario, directory=candidate)
        raise IngestError(
            f"no recording for scenario {scenario!r}: expected *.jsonl under "
            f"{(base / RAW_DIR).as_posix()} or {(base / FIXTURE_DIR).as_posix()} "
            f"(run `hu ingest record --scenario {scenario}`)"
        )

    @classmethod
    def at(cls, directory: Path, *, scenario: str = "") -> Recording:
        """A recording read straight out of one directory, for tests."""
        return cls(scenario=scenario or directory.parent.name, directory=directory)

    def files(self) -> list[Path]:
        return sorted(self.directory.glob("*.jsonl"))

    def sources(self) -> list[Source]:
        found = []
        for path in self.files():
            try:
                found.append(Source(path.stem))
            except ValueError:
                continue
        return found

    def payloads(self) -> list[RawPayload]:
        """Every payload in the recording, oldest fetch first.

        Sorted across feeds, because replay has to interleave an NWS polygon
        with the gauge readings that arrived between two of them. The tie-break
        on `ref` keeps the order stable when two feeds share a timestamp.
        """
        collected = [item for path in self.files() for item in self._read(path)]
        collected.sort(key=lambda item: (item.fetched_at, item.source.value, item.ref))
        return collected

    def _read(self, path: Path) -> Iterator[RawPayload]:
        with path.open("rb") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    yield RawPayload.model_validate(orjson.loads(line))
                except (orjson.JSONDecodeError, ValueError) as exc:
                    raise IngestError(f"{path.as_posix()}:{number} is not a RawPayload") from exc

    @property
    def size_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.files())
