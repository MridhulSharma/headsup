"""Raw Overpass responses on disk, and a build that reads them back.

A cold build is not slow because of anything this code does: it is slow
because the public Overpass instance queues, and how long it queues for is not
a property of this project. Measured on one afternoon, the same four requests
cost 29 s each and then 82 s each. A step whose timing swings by 3x on a
volunteer server's mood cannot be checked, and a demo that depends on that
server cannot be rehearsed.

So the responses are kept. Every Overpass response that goes into a build is
written to `data/cache/<city>/raw/`, and `hu world build --offline` rebuilds
the whole world from those files without touching the network.

The split is the one `headsup.ingest` already uses for feeds: fetching and
parsing are separate, the raw bytes sit in between, and replay runs the same
parse over the same bytes. Here the seam is `osmnx._overpass._overpass_request`
- the one function every Overpass query in OSMnx goes through, and the point
above its own HTTP cache. Recording wraps it; replaying replaces it. Both
leave OSMnx's parse, truncate and simplify pipeline untouched, which is what
makes an offline build produce the same graph as the online one rather than
one that merely looks similar.

It is a private function, so `_seam` checks it is there and says plainly what
to do when a future OSMnx moves it, rather than failing somewhere deep in a
build.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from headsup.contracts import utcnow
from headsup.world.bbox import BBox
from headsup.world.graph import WorldError
from headsup.world.model import world_cache_dir

#: The two kinds of query a build makes: the drivable network, then the POIs.
PHASE_NETWORK = "network"
PHASE_FEATURES = "features"
PHASES = (PHASE_NETWORK, PHASE_FEATURES)

MANIFEST = "manifest.json"

#: The OSMnx function every Overpass query passes through, and the attribute
#: name to patch on it.
_SEAM_MODULE = "osmnx._overpass"
_SEAM_NAME = "_overpass_request"


def raw_dir(city: str, cache_dir: Path | str | None = None) -> Path:
    """Where one city's raw Overpass responses live."""
    return Path(cache_dir or world_cache_dir()) / city / "raw"


def _path_for(directory: Path, phase: str, index: int) -> Path:
    return directory / f"{phase}_{index:03d}.json"


def response_files(directory: Path | str, phase: str) -> tuple[Path, ...]:
    """The saved responses for one phase, in the order they were recorded."""
    found = sorted(Path(directory).glob(f"{phase}_*.json"))
    return tuple(found)


def has_raw(city: str, cache_dir: Path | str | None = None) -> bool:
    """True when a city has at least one recorded response of each phase.

    Both phases are required because an offline build runs both: a city with
    roads but no POI response would rebuild into a world whose
    critical-access rule has nothing to guard.
    """
    directory = raw_dir(city, cache_dir)
    return all(response_files(directory, phase) for phase in PHASES)


def _seam():
    """The OSMnx module holding the request function, or a clear error."""
    import importlib

    module = importlib.import_module(_SEAM_MODULE)
    if not hasattr(module, _SEAM_NAME):
        raise WorldError(
            f"this OSMnx has no {_SEAM_MODULE}.{_SEAM_NAME}, so raw Overpass "
            "responses cannot be recorded or replayed; pin osmnx<3 or update "
            "headsup.world.raw to the new seam"
        )
    return module


@contextmanager
def record(directory: Path | str, phase: str) -> Iterator[list[Path]]:
    """Write every Overpass response made inside the block to `directory`.

    Responses already recorded for this phase are removed first, so a rebuild
    replaces a city's raw set instead of interleaving two of them.

    Recording sits above OSMnx's own HTTP cache, so a response OSMnx served
    from its cache is recorded too - which is how a city built before the raw
    archive existed gains one without a new download.
    """
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    for stale in response_files(target, phase):
        stale.unlink()

    module = _seam()
    original = getattr(module, _SEAM_NAME)
    written: list[Path] = []

    def recording_request(data: Any) -> dict[str, Any]:
        response = original(data)
        path = _path_for(target, phase, len(written) + 1)
        path.write_text(
            json.dumps(
                {
                    "phase": phase,
                    "index": len(written) + 1,
                    "query": _query_of(data),
                    "recorded_at": utcnow().isoformat(),
                    "response": response,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        written.append(path)
        return response

    setattr(module, _SEAM_NAME, recording_request)
    try:
        yield written
    finally:
        setattr(module, _SEAM_NAME, original)


@contextmanager
def replay(directory: Path | str, phase: str) -> Iterator[list[str]]:
    """Serve Overpass responses inside the block from `directory`.

    Queries are matched on their text first and by order second: OSMnx splits
    a large bbox into several requests, and a rebuild asks for them in the
    same order it did when they were recorded. A query with neither a match
    nor a response left over is an error naming the file that would have
    answered it - never a silent empty result, which would rebuild a city with
    some of its roads missing.

    Nothing inside the block reaches the network, so no rate-limit pause and
    no status call happens either.
    """
    target = Path(directory)
    saved = load_responses(target, phase)
    if not saved:
        raise WorldError(
            f"no raw {phase} responses in {target.as_posix()} - run "
            "`hu world build` once with network access before --offline"
        )
    by_query = {record["query"]: record["response"] for record in saved if record.get("query")}
    remaining = [record["response"] for record in saved]
    asked: list[str] = []

    module = _seam()
    original = getattr(module, _SEAM_NAME)

    def replaying_request(data: Any) -> dict[str, Any]:
        query = _query_of(data)
        asked.append(query)
        if query in by_query:
            response = by_query[query]
            if response in remaining:
                remaining.remove(response)
            return response
        if remaining:
            return remaining.pop(0)
        raise WorldError(
            f"the {phase} rebuild asked Overpass for a query that is not in "
            f"{target.as_posix()} ({len(saved)} responses recorded, "
            f"{len(asked)} asked for); rebuild with network access to "
            "re-record this city"
        )

    setattr(module, _SEAM_NAME, replaying_request)
    try:
        yield asked
    finally:
        setattr(module, _SEAM_NAME, original)


def _query_of(data: Any) -> str:
    """The Overpass QL out of whatever OSMnx passed as its request params."""
    if isinstance(data, Mapping):
        return str(data.get("data", ""))
    return str(data)


def load_responses(directory: Path | str, phase: str) -> tuple[dict[str, Any], ...]:
    """Every recorded response for one phase, in recorded order."""
    records: list[dict[str, Any]] = []
    for path in response_files(directory, phase):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorldError(f"cannot read {path.as_posix()}: {exc}") from exc
        if not isinstance(payload, dict) or "response" not in payload:
            raise WorldError(f"{path.as_posix()} is not a recorded Overpass response")
        records.append(payload)
    return tuple(records)


@dataclass(frozen=True, slots=True)
class RawArchive:
    """What a city's raw directory holds."""

    city: str
    directory: Path
    counts: Mapping[str, int]
    bytes: int
    recorded_at: str | None = None

    @property
    def responses(self) -> int:
        return sum(self.counts.values())

    @property
    def complete(self) -> bool:
        return all(self.counts.get(phase, 0) > 0 for phase in PHASES)

    def __str__(self) -> str:
        if not self.responses:
            return f"{self.city}: no raw Overpass responses ({self.directory.as_posix()})"
        phases = ", ".join(f"{phase}={self.counts.get(phase, 0)}" for phase in PHASES)
        when = f", recorded {self.recorded_at}" if self.recorded_at else ""
        return (
            f"{self.city}: {self.responses} raw Overpass responses "
            f"({phases}), {self.bytes / 1024:.0f} KiB{when}"
        )


def archive(city: str, cache_dir: Path | str | None = None) -> RawArchive:
    """Describe a city's raw archive, whether or not it has one."""
    directory = raw_dir(city, cache_dir)
    counts = {phase: len(response_files(directory, phase)) for phase in PHASES}
    size = sum(path.stat().st_size for phase in PHASES for path in response_files(directory, phase))
    stored = read_manifest(directory)
    return RawArchive(
        city=city,
        directory=directory,
        counts=counts,
        bytes=size,
        recorded_at=str(stored.get("recorded_at")) if stored else None,
    )


def write_manifest(
    directory: Path | str,
    *,
    city: str,
    bbox: BBox,
    network_type: str,
    tags: Mapping[str, Any],
) -> Path:
    """Record what the saved responses are of, next to them.

    The bbox matters: a raw set recorded for one rectangle rebuilds that
    rectangle, and a bbox edited in `headsup.world.bbox` makes the archive
    stale. `check_manifest` is what notices.
    """
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    counts = {phase: len(response_files(target, phase)) for phase in PHASES}
    payload = {
        "city": city,
        "bbox": list(bbox.as_tuple),
        "network_type": network_type,
        "tags": dict(sorted(tags.items())),
        "osmnx": _osmnx_version(),
        "recorded_at": utcnow().isoformat(),
        "responses": counts,
    }
    path = target / MANIFEST
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def read_manifest(directory: Path | str) -> dict[str, Any]:
    """The manifest beside a raw set, or an empty dict if there is none."""
    path = Path(directory) / MANIFEST
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def check_manifest(directory: Path | str, bbox: BBox) -> str | None:
    """None if the archive is for this bbox, else what differs.

    A warning, not an error: an offline rebuild of a stale archive is still
    the city that was recorded, and saying so is more use than refusing.
    """
    stored = read_manifest(directory)
    if not stored:
        return None
    recorded = stored.get("bbox")
    readable = isinstance(recorded, Sequence) and len(recorded) == 4
    if readable and tuple(float(value) for value in recorded) != bbox.as_tuple:
        return (
            "raw responses were recorded for bbox "
            f"{','.join(f'{float(v):.4f}' for v in recorded)}, not {bbox}"
        )
    return None


def _osmnx_version() -> str:
    try:
        import osmnx as ox

        return str(ox.__version__)
    except Exception:  # pragma: no cover - osmnx is a hard dependency
        return "unknown"
