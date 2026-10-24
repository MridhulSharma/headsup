"""The hash-chained JSONL log: one line per bus entry, each line sealing the last.

Every entry that crossed any stream during a run is appended to
`data/audit/<run_id>.jsonl` as one JSON object, and each line carries

    hash = sha256(prev_hash + canonical_json(record))

where `prev_hash` is the previous line's `hash` as its 64 hex characters
(`GENESIS`, sixty-four zeros, for the first line) and `canonical_json(record)`
is the line's own fields with `hash` removed, dumped with sorted keys. The
`prev_hash` sits inside the record as well as in front of the digest, so the
link is covered by the hash that follows it: nobody can re-point a line at a
different predecessor without breaking that line too.

**What the chain detects, and where.** Editing a line and leaving its `hash`
alone breaks *that* line: the recomputed digest no longer matches the one the
line carries. Editing a line and recomputing its `hash` to match makes that
line self-consistent and breaks the *next* one, whose `prev_hash` no longer
names its predecessor. Deleting or reordering lines breaks the first line whose
`prev_hash` is wrong. So the first break is always at, or immediately after, the
altered line, and `verify` reports it with the line number and the reason - the
point of a chain is not to prevent an edit but to make it impossible to make one
quietly.

**What it does not do.** This is tamper *evidence*, not a signature: anyone who
can rewrite the file can rewrite every line after the one they changed and
produce a log that verifies. Pinning the head hash somewhere the writer cannot
reach is what would close that, and is out of scope for a research prototype.
Advisories carry their own Ed25519 signature (`compiler.signing`), so the one
thing the city actually publishes is covered by more than this.

**Canonical JSON is computed on the parsed dict, never re-validated through
Pydantic.** `verify` parses a line with orjson, drops `hash` and dumps the rest
with sorted keys, which is byte for byte what the writer hashed. Going back
through a model would put the chain at the mercy of a serialiser change - a
datetime that gains a `+00:00` in some later Pydantic would break every log ever
written, which is exactly the false alarm this record cannot afford.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import IO, Annotated, Any

import orjson
from pydantic import Field, StringConstraints

from headsup.contracts import HeadsUpModel, Identifier, UtcDatetime, utcnow

#: The `prev_hash` of the first line: there is no line before it.
GENESIS = "0" * 64

#: The field a line's own digest lives in, and the one left out of the digest.
HASH_FIELD = "hash"

#: Where the logs go, relative to the working directory.
DEFAULT_AUDIT_DIR = Path("data") / "audit"

#: Override for the log directory, so a test or a parallel run writes elsewhere.
ENV_AUDIT_DIR = "HU_AUDIT_DIR"

#: The run every agent in one process records under.
ENV_RUN_ID = "HU_RUN_ID"

#: A run id becomes a filename, so it is checked rather than trusted: no
#: separators, no dots to climb with, nothing but what an id needs.
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")

#: How much of a digest a citation carries. Twelve hex characters is 48 bits -
#: ample against collision in a log of a few thousand lines, and short enough to
#: read inside a sentence and to find by eye in the file.
CITE_LENGTH = 12

_HEX = frozenset("0123456789abcdef")

#: 64 lowercase hex characters: a sha256 digest as this log writes it.
Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

#: A stream name (`events.nws`, `advisories`) or a message type name. Capped
#: rather than patterned: a line records what was on the bus, and a log that
#: refuses an entry because its type name looked odd is worse than one that
#: records the odd name.
LogText = Annotated[str, StringConstraints(max_length=256)]


class ChainError(RuntimeError):
    """The log could not be read or written, or a run id is not usable."""


class Break(StrEnum):
    """Why a line failed to verify."""

    #: Not one JSON object.
    MALFORMED = "malformed"
    #: No `hash`, no `prev_hash`, or one of them is not a sha256 digest.
    FIELDS = "fields"
    #: The line's contents do not hash to the digest the line carries.
    HASH_MISMATCH = "hash_mismatch"
    #: The line's `prev_hash` does not name the line before it.
    PREV_MISMATCH = "prev_mismatch"
    #: The line is not where its `seq` says it is.
    SEQ_MISMATCH = "seq_mismatch"
    #: The line belongs to another run.
    RUN_MISMATCH = "run_mismatch"


class AuditRecord(HeadsUpModel):
    """One bus entry, as one line of the chain.

    `data` is the entry's payload verbatim. It is carried rather than summarised
    because a chain is only worth having if the thing it covers is the thing
    that happened: a sealed record of a decision with the decision left out
    answers nothing a year later.
    """

    seq: int = Field(ge=0)
    run_id: Identifier
    #: Where it was read from, and the Redis entry id it had there.
    stream: LogText
    entry_id: LogText
    #: The contract class name the bus stamped on the entry, or "" if it had none.
    type: LogText = ""
    #: When the publisher wrote it, as the bus recorded it.
    published_at: LogText | None = None
    #: When the auditor appended this line.
    recorded_at: UtcDatetime = Field(default_factory=utcnow)
    data: dict[str, Any] = Field(default_factory=dict)
    prev_hash: Hex64 = GENESIS
    hash: Hex64

    @property
    def short(self) -> str:
        """The first twelve hex characters: how the report cites a line."""
        return self.hash[:CITE_LENGTH]


def canonical_json(payload: Mapping[str, Any]) -> bytes:
    """Byte-stable JSON for hashing: sorted keys, no spaces, UTF-8."""
    return orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)


def chain_hash(prev_hash: str, body: Mapping[str, Any]) -> str:
    """`sha256(prev_hash + canonical_json(body))`, as 64 hex characters.

    `body` is a record without its `hash` field; one that still had it would
    hash the digest into itself and could never verify.
    """
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("ascii"))
    digest.update(canonical_json(body))
    return digest.hexdigest()


def body_of(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A line's fields with `hash` removed: exactly what the digest covers."""
    return {key: value for key, value in payload.items() if key != HASH_FIELD}


def check_run_id(run_id: str) -> str:
    """The run id, or `ChainError` if it could not safely be a filename."""
    if not RUN_ID_PATTERN.fullmatch(run_id or ""):
        raise ChainError(
            f"{run_id!r} is not a usable run id: letters, digits, underscore and "
            "hyphen, starting with a letter or digit, up to 64 characters"
        )
    return run_id


def new_run_id(now: datetime | None = None) -> str:
    """A run id from the clock: `run_20261024T154500Z`."""
    return f"run_{(now or utcnow()):%Y%m%dT%H%M%SZ}"


def run_id_from_env() -> str:
    """`HU_RUN_ID`, or a new id from the clock."""
    raw = os.environ.get(ENV_RUN_ID, "").strip()
    return check_run_id(raw) if raw else new_run_id()


def audit_dir(directory: Path | str | None = None) -> Path:
    """The log directory: the argument, `HU_AUDIT_DIR`, or `data/audit`."""
    if directory is not None:
        return Path(directory)
    override = os.environ.get(ENV_AUDIT_DIR, "").strip()
    return Path(override) if override else DEFAULT_AUDIT_DIR


def log_path(run_id: str, directory: Path | str | None = None) -> Path:
    """Where one run's chain lives."""
    return audit_dir(directory) / f"{check_run_id(run_id)}.jsonl"


def runs(directory: Path | str | None = None) -> list[str]:
    """Every run id with a log, newest file last."""
    target = audit_dir(directory)
    if not target.is_dir():
        return []
    found = sorted(target.glob("*.jsonl"), key=lambda path: path.stat().st_mtime)
    return [path.stem for path in found]


class AuditLog:
    """Append-only writer for one run's chain.

    Opening a log that already has lines resumes it: the head hash and the next
    sequence number come from the last line, so an auditor that was restarted
    extends the chain rather than starting a second one in the same file.

        with AuditLog("run_demo") as log:
            record = log.append(stream="hazards", entry_id="1-0", data=payload)

    Each `append` writes its line and flushes before returning, because the
    auditor acks the bus entry on the strength of that return: an entry
    acknowledged for a line still sitting in a buffer is an entry nothing will
    ever read again.
    """

    def __init__(
        self,
        run_id: str,
        *,
        directory: Path | str | None = None,
        fsync: bool = False,
    ) -> None:
        self.run_id = check_run_id(run_id)
        self.directory = audit_dir(directory)
        self.path = self.directory / f"{self.run_id}.jsonl"
        #: Call `fsync` after every line. Off by default: a prototype's log is
        #: worth a flush, not a disk round trip per bus entry.
        self.fsync = fsync
        self._handle: IO[bytes] | None = None
        self._head = GENESIS
        self._seq = 0
        self._resumed = False

    def __enter__(self) -> AuditLog:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def head(self) -> str:
        """The last line's hash, or `GENESIS` if the log is empty."""
        return self._head

    @property
    def entries(self) -> int:
        """Lines in the file, including any this writer resumed from."""
        return self._seq

    def open(self) -> AuditLog:
        """Create the directory, read any existing head, and open for append."""
        if self._handle is not None:
            return self
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ChainError(f"cannot create {self.directory.as_posix()}: {exc}") from exc
        self._resume()
        try:
            self._handle = self.path.open("ab")
        except OSError as exc:
            raise ChainError(f"cannot append to {self.path.as_posix()}: {exc}") from exc
        return self

    def _resume(self) -> None:
        """Pick the head hash and the sequence number off an existing file."""
        if self._resumed:
            return
        self._resumed = True
        if not self.path.is_file():
            return
        last: dict[str, Any] | None = None
        count = 0
        for payload in read_lines(self.path):
            last = payload
            count += 1
        if last is None:
            return
        head = last.get(HASH_FIELD)
        if not _is_digest(head):
            raise ChainError(
                f"{self.path.as_posix()} line {count} carries no usable hash; "
                "refusing to extend a chain whose head cannot be read"
            )
        self._head = str(head)
        self._seq = count

    def append(
        self,
        *,
        stream: str,
        entry_id: str,
        data: Mapping[str, Any] | None = None,
        type: str = "",
        published_at: str | None = None,
        recorded_at: datetime | None = None,
    ) -> AuditRecord:
        """Seal one bus entry onto the end of the chain and return its record."""
        handle = self.open()._handle
        if handle is None:  # pragma: no cover - open() raises instead
            raise ChainError(f"{self.path.as_posix()} is not open")
        body: dict[str, Any] = {
            "seq": self._seq,
            "run_id": self.run_id,
            "stream": str(stream)[:256],
            "entry_id": str(entry_id)[:256],
            "type": str(type)[:256],
            "published_at": published_at[:256] if published_at else None,
            "recorded_at": (recorded_at or utcnow()).isoformat(),
            "data": dict(data or {}),
            "prev_hash": self._head,
        }
        digest = chain_hash(self._head, body)
        try:
            handle.write(canonical_json({**body, HASH_FIELD: digest}) + b"\n")
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ChainError(f"cannot append to {self.path.as_posix()}: {exc}") from exc
        self._head = digest
        self._seq += 1
        return AuditRecord.model_validate({**body, HASH_FIELD: digest})

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def read_lines(path: Path | str) -> Iterator[dict[str, Any]]:
    """Each line of a log as a parsed object, oldest first.

    Blank lines are skipped: a file that was appended to after a crash can carry
    a stray newline, and that is not a tampered line. Anything else that is not
    one JSON object raises `ChainError` naming the line number.
    """
    target = Path(path)
    try:
        with target.open("rb") as handle:
            for number, raw in enumerate(handle, start=1):
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    payload = orjson.loads(stripped)
                except orjson.JSONDecodeError as exc:
                    raise ChainError(f"line {number} is not JSON: {exc}") from exc
                if not isinstance(payload, dict):
                    raise ChainError(f"line {number} is not a JSON object")
                yield payload
    except OSError as exc:
        raise ChainError(f"cannot read {target.as_posix()}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class BrokenLine:
    """The first line that did not verify, and why."""

    line: int
    reason: Break
    detail: str
    stream: str | None = None
    entry_id: str | None = None

    def summary(self) -> str:
        where = f"line {self.line}"
        if self.stream:
            where += f" ({self.stream} {self.entry_id})"
        return f"{where}: {self.reason.value}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ChainReport:
    """What `verify` found: how far the chain held, and where it stopped."""

    path: Path
    run_id: str
    #: Lines read, including the broken one.
    lines: int
    #: Lines that verified before the break.
    verified: int
    #: The hash of the last line that verified, or `GENESIS`.
    head: str
    broken: BrokenLine | None = None

    @property
    def ok(self) -> bool:
        return self.broken is None

    def summary(self) -> str:
        if self.broken is None:
            return f"{self.run_id}: {self.verified} lines verified, head {self.head[:CITE_LENGTH]}"
        return f"{self.run_id}: broken at {self.broken.summary()}"


def verify(run_id: str, *, directory: Path | str | None = None) -> ChainReport:
    """Recompute one run's chain and report the first line that does not hold."""
    return verify_file(log_path(run_id, directory), run_id=check_run_id(run_id))


def verify_file(path: Path | str, *, run_id: str | None = None) -> ChainReport:
    """Recompute the chain in one file, stopping at the first break.

    Stopping is deliberate. Once a line does not verify, every line after it is
    measured against a head that is not the one that was written, so reporting
    the rest would be reporting noise. The first break is the answer: it is the
    line the tampering is at, or the line right after it.
    """
    target = Path(path)
    expected = run_id or target.stem
    head = GENESIS
    verified = 0
    lines = 0

    def report(broken: BrokenLine | None) -> ChainReport:
        return ChainReport(
            path=target,
            run_id=expected,
            lines=lines,
            verified=verified,
            head=head,
            broken=broken,
        )

    if not target.is_file():
        raise ChainError(f"no audit log at {target.as_posix()}")

    try:
        for payload in read_lines(target):
            lines += 1
            failure = _check(payload, head=head, seq=lines - 1, run_id=run_id)
            if failure is not None:
                reason, detail = failure
                return report(
                    BrokenLine(
                        line=lines,
                        reason=reason,
                        detail=detail,
                        stream=_text(payload.get("stream")),
                        entry_id=_text(payload.get("entry_id")),
                    )
                )
            head = str(payload[HASH_FIELD])
            verified += 1
    except ChainError as exc:
        # A line that is not JSON at all. The chain cannot be followed past it,
        # and `read_lines` put the line number in the message.
        return report(BrokenLine(line=lines + 1, reason=Break.MALFORMED, detail=str(exc)))
    return report(None)


def records(run_id: str, *, directory: Path | str | None = None) -> list[AuditRecord]:
    """One run's lines as validated records, oldest first."""
    path = log_path(run_id, directory)
    if not path.is_file():
        raise ChainError(f"no audit log at {path.as_posix()}")
    return [AuditRecord.model_validate(payload) for payload in read_lines(path)]


def _check(
    payload: Mapping[str, Any], *, head: str, seq: int, run_id: str | None
) -> tuple[Break, str] | None:
    """Everything that can be wrong with one line, in the order worth reporting.

    The line's own digest is checked before its link, so a line that was edited
    and left otherwise alone is reported at itself rather than at its successor.
    """
    claimed = payload.get(HASH_FIELD)
    prev = payload.get("prev_hash")
    if not _is_digest(claimed) or not _is_digest(prev):
        return (Break.FIELDS, "hash and prev_hash must each be 64 hex characters")
    recomputed = chain_hash(str(prev), body_of(payload))
    if recomputed != claimed:
        return (
            Break.HASH_MISMATCH,
            f"contents hash to {recomputed[:CITE_LENGTH]}, "
            f"line carries {str(claimed)[:CITE_LENGTH]}",
        )
    if prev != head:
        return (
            Break.PREV_MISMATCH,
            f"follows {str(prev)[:CITE_LENGTH]}, but the line before it "
            f"hashes to {head[:CITE_LENGTH]}",
        )
    if payload.get("seq") != seq:
        return (Break.SEQ_MISMATCH, f"line {seq + 1} carries seq {payload.get('seq')!r}")
    if run_id is not None and payload.get("run_id") != run_id:
        return (
            Break.RUN_MISMATCH,
            f"belongs to run {payload.get('run_id')!r}, not {run_id!r}",
        )
    return None


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and _HEX.issuperset(value)


def _text(value: object) -> str | None:
    return str(value)[:256] if isinstance(value, str) else None
