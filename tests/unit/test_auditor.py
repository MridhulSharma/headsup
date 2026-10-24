"""The auditor: every stream in, one chained line each, nothing published.

No Redis here. `FakeBus` yields prepared entries and keeps what was acked,
which is the whole of the worker's contract: one entry read, one line sealed
before the ack, and never an exception out of `run`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from fixtures.audit import ENTRIES, RUN, T0
from headsup.agents.auditor import AUDITOR_NAME, Auditor
from headsup.audit import AuditLog, ChainError, records, verify
from headsup.bus import ALL_STREAMS, BusError, BusMessage


@dataclass(slots=True)
class FakeBus:
    """Entries in, acks recorded. Enough bus for one auditor."""

    entries: list[BusMessage] = field(default_factory=list)
    published: list[tuple[str, Any]] = field(default_factory=list)
    acked: list[str] = field(default_factory=list)
    asked: list[tuple[tuple[str, ...], str, str]] = field(default_factory=list)
    ack_fails: bool = False

    async def consume(
        self,
        streams: str | Sequence[str],
        group: str,
        consumer: str,
        **kwargs: Any,
    ) -> AsyncIterator[BusMessage]:
        names = (streams,) if isinstance(streams, str) else tuple(streams)
        self.asked.append((names, group, consumer))
        limit = kwargs.get("limit")
        for index, message in enumerate(self.entries):
            if limit is not None and index >= limit:
                return
            yield message

    async def publish(self, message: Any, *, stream: str | None = None) -> str:
        self.published.append((stream or "", message))
        return f"0-{len(self.published)}"

    async def ack(self, message: BusMessage) -> int:
        if self.ack_fails:
            raise BusError("XACK failed")
        self.acked.append(message.id)
        return 1


def messages() -> list[BusMessage]:
    """The sample run as entries off the bus, in arrival order."""
    return [
        BusMessage(
            stream=stream,
            id=f"{1700000000000 + index}-0",
            type=type_name,
            data=data,
            published_at=T0.isoformat(),
            group=AUDITOR_NAME,
        )
        for index, (stream, type_name, data) in enumerate(ENTRIES)
    ]


async def recorded(tmp_path: Path, bus: FakeBus, **kwargs: Any) -> Any:
    async with Auditor(AuditLog(RUN, directory=tmp_path), bus, **kwargs) as auditor:
        return await auditor.run(once=True)


# --- what it reads ----------------------------------------------------------


async def test_it_consumes_every_stream_through_one_group(tmp_path: Path) -> None:
    """One chain per run, not one per stage: the order between streams is the record."""
    bus = FakeBus(entries=messages())
    await recorded(tmp_path, bus)
    names, group, consumer = bus.asked[0]
    assert names == tuple(ALL_STREAMS)
    assert group == AUDITOR_NAME == "agent.auditor"
    assert consumer == "worker-1"


async def test_every_entry_becomes_one_line_in_arrival_order(tmp_path: Path) -> None:
    bus = FakeBus(entries=messages())
    report = await recorded(tmp_path, bus)
    assert report.entries == len(ENTRIES)
    written = records(RUN, directory=tmp_path)
    assert [(record.stream, record.type) for record in written] == [
        (stream, type_name) for stream, type_name, _ in ENTRIES
    ]
    assert [record.entry_id for record in written] == [message.id for message in bus.entries]
    assert [record.published_at for record in written] == [T0.isoformat()] * len(ENTRIES)


async def test_the_chain_it_writes_verifies(tmp_path: Path) -> None:
    bus = FakeBus(entries=messages())
    report = await recorded(tmp_path, bus)
    chain = verify(RUN, directory=tmp_path)
    assert chain.ok
    assert chain.verified == len(ENTRIES)
    assert chain.head == report.head


async def test_every_entry_is_acked(tmp_path: Path) -> None:
    bus = FakeBus(entries=messages())
    await recorded(tmp_path, bus)
    assert bus.acked == [message.id for message in bus.entries]


async def test_it_publishes_nothing(tmp_path: Path) -> None:
    """The one agent on the bus that only reads. A recorder that writes can be fed."""
    bus = FakeBus(entries=messages())
    await recorded(tmp_path, bus)
    assert bus.published == []


async def test_the_report_counts_by_stream(tmp_path: Path) -> None:
    bus = FakeBus(entries=messages())
    report = await recorded(tmp_path, bus)
    assert report.by_stream["audit"] == 2
    assert report.by_stream["advisories"] == 1
    assert sum(report.by_stream.values()) == len(ENTRIES)
    assert report.run_id == RUN
    assert RUN in report.summary()


async def test_it_prints_each_line_when_asked(tmp_path: Path) -> None:
    seen: list[str] = []
    bus = FakeBus(entries=messages())
    await recorded(tmp_path, bus, on_record=lambda record: seen.append(record.short))
    assert len(seen) == len(ENTRIES)
    assert all(len(short) == 12 for short in seen)


# --- what it survives -------------------------------------------------------


async def test_an_entry_that_cannot_be_recorded_is_counted_and_acked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poison entry must not become a loop that blocks the rest of the record."""
    bus = FakeBus(entries=messages())
    audit_log = AuditLog(RUN, directory=tmp_path)
    calls = {"n": 0}
    real_append = audit_log.append

    def flaky(**kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 3:
            raise ChainError("disk full")
        return real_append(**kwargs)

    monkeypatch.setattr(audit_log, "append", flaky)
    async with Auditor(audit_log, bus) as auditor:
        report = await auditor.run(once=True)

    assert report.skipped == 1
    assert report.entries == len(ENTRIES) - 1
    assert len(bus.acked) == len(ENTRIES)
    assert "disk full" in report.errors[0]
    assert verify(RUN, directory=tmp_path).ok


async def test_a_failed_ack_does_not_end_the_run(tmp_path: Path) -> None:
    """The line is already written; a redelivery shows as two lines, not a gap."""
    bus = FakeBus(entries=messages(), ack_fails=True)
    report = await recorded(tmp_path, bus)
    assert report.entries == len(ENTRIES)
    assert bus.acked == []
    assert verify(RUN, directory=tmp_path).ok


async def test_a_restarted_auditor_extends_the_same_chain(tmp_path: Path) -> None:
    half = len(ENTRIES) // 2
    first = FakeBus(entries=messages()[:half])
    await recorded(tmp_path, first)
    second = FakeBus(entries=messages()[half:])
    report = await recorded(tmp_path, second)
    assert report.entries == len(ENTRIES) - half
    chain = verify(RUN, directory=tmp_path)
    assert chain.ok
    assert chain.verified == len(ENTRIES)


async def test_entries_can_be_capped(tmp_path: Path) -> None:
    bus = FakeBus(entries=messages())
    async with Auditor(AuditLog(RUN, directory=tmp_path), bus) as auditor:
        report = await auditor.run(events=3)
    assert report.entries == 3
    assert verify(RUN, directory=tmp_path).verified == 3
