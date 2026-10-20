"""The recorder writes raw payloads; the Recording reads them back in order."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fixtures.ingest import NWS_BODY, NWS_URL, USGS_BODY
from headsup.contracts import Source
from headsup.ingest import FIXTURE_DIR, RAW_DIR, IngestError, RawPayload, Recorder, Recording

T0 = datetime(2026, 4, 20, 18, 55, tzinfo=UTC)


def payload(source: Source, offset_s: int, seq: int = 0) -> RawPayload:
    at = T0 + timedelta(seconds=offset_s)
    body = NWS_BODY if source is Source.NWS else USGS_BODY
    return RawPayload(
        source=source,
        ref=f"{source.value}/{at:%Y%m%dT%H%M%SZ}/{seq}",
        url=NWS_URL,
        fetched_at=at,
        body=body,
    )


def test_recorder_writes_one_file_per_feed(tmp_path: Path) -> None:
    with Recorder("s_test", root=tmp_path) as recorder:
        recorder.write(payload(Source.NWS, 0))
        recorder.write(payload(Source.USGS, 30))
        recorder.write(payload(Source.NWS, 60, seq=1))

    directory = tmp_path / "s_test" / RAW_DIR
    assert sorted(path.name for path in directory.glob("*.jsonl")) == ["nws.jsonl", "usgs.jsonl"]
    assert recorder.counts == {Source.NWS: 2, Source.USGS: 1}
    assert recorder.total == 3
    assert len(directory.joinpath("nws.jsonl").read_bytes().splitlines()) == 2


def test_recorder_leaves_no_file_for_a_feed_that_never_reported(tmp_path: Path) -> None:
    with Recorder("s_test", root=tmp_path) as recorder:
        recorder.write(payload(Source.NWS, 0))
    assert not (tmp_path / "s_test" / RAW_DIR / "usgs.jsonl").exists()


def test_round_trip_through_jsonl_keeps_the_payload_intact(tmp_path: Path) -> None:
    original = payload(Source.NWS, 0)
    with Recorder("s_test", root=tmp_path) as recorder:
        recorder.write(original)

    read_back = Recording.locate("s_test", root=tmp_path).payloads()
    assert len(read_back) == 1
    assert read_back[0] == original
    assert read_back[0].body == NWS_BODY


def test_payloads_come_back_interleaved_in_fetch_order(tmp_path: Path) -> None:
    with Recorder("s_test", root=tmp_path) as recorder:
        for offset, source in [
            (0, Source.NWS),
            (90, Source.NWS),
            (30, Source.USGS),
            (60, Source.USGS),
        ]:
            recorder.write(payload(source, offset))

    ordered = Recording.locate("s_test", root=tmp_path).payloads()
    assert [(item.source.value, (item.fetched_at - T0).total_seconds()) for item in ordered] == [
        ("nws", 0),
        ("usgs", 30),
        ("usgs", 60),
        ("nws", 90),
    ]
    assert ordered == sorted(ordered, key=lambda item: item.fetched_at)


def test_a_fresh_capture_wins_over_the_committed_fixture(tmp_path: Path) -> None:
    with Recorder("s_test", root=tmp_path, subdir=FIXTURE_DIR) as fixture:
        fixture.write(payload(Source.USGS, 0))
    assert Recording.locate("s_test", root=tmp_path).directory.name == FIXTURE_DIR

    with Recorder("s_test", root=tmp_path) as raw:
        raw.write(payload(Source.NWS, 0))
    located = Recording.locate("s_test", root=tmp_path)
    assert located.directory.name == RAW_DIR
    assert located.sources() == [Source.NWS]


def test_locate_says_how_to_make_a_missing_recording(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="hu ingest record --scenario nowhere"):
        Recording.locate("nowhere", root=tmp_path)


def test_a_corrupt_line_names_its_file_and_line_number(tmp_path: Path) -> None:
    directory = tmp_path / "s_test" / RAW_DIR
    directory.mkdir(parents=True)
    directory.joinpath("nws.jsonl").write_bytes(b'{"source": "nws"}\nnot json\n')
    with pytest.raises(IngestError, match=r"nws\.jsonl:1"):
        Recording.locate("s_test", root=tmp_path).payloads()


def test_blank_lines_are_skipped(tmp_path: Path) -> None:
    with Recorder("s_test", root=tmp_path) as recorder:
        recorder.write(payload(Source.NWS, 0))
    path = tmp_path / "s_test" / RAW_DIR / "nws.jsonl"
    path.write_bytes(path.read_bytes() + b"\n\n")
    assert len(Recording.locate("s_test", root=tmp_path).payloads()) == 1
