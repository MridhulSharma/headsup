"""The chain: what each line hashes, and where an edit to the log shows up.

The one test this module exists for is that tampering with a line is detected
*at that line*. Everything else here is the surrounding contract: the digest is
sha256 over `prev_hash` and the record's canonical JSON and nothing else, the
first line follows `GENESIS`, and a careful attacker who rehashes the line they
edited is caught one line later instead.
"""

from __future__ import annotations

import hashlib
from itertools import pairwise
from pathlib import Path

import pytest

from fixtures.audit import ENTRIES, RUN, hashes, lines, rewrite, tamper, write_run
from headsup.audit import (
    GENESIS,
    AuditLog,
    Break,
    ChainError,
    body_of,
    canonical_json,
    chain_hash,
    check_run_id,
    new_run_id,
    records,
    verify,
    verify_file,
)

# --- what a line carries ----------------------------------------------------


def test_the_first_line_follows_the_genesis_hash(tmp_path: Path) -> None:
    with AuditLog(RUN, directory=tmp_path) as log:
        record = log.append(stream="hazards", entry_id="1-0", data={"id": "haz_1"})
    assert record.prev_hash == GENESIS
    assert record.seq == 0


def test_a_line_hashes_prev_hash_followed_by_its_canonical_json(tmp_path: Path) -> None:
    """The digest is computable by hand from the line, which is the whole point."""
    path = write_run(tmp_path)
    for payload in lines(path):
        expected = hashlib.sha256(
            payload["prev_hash"].encode("ascii") + canonical_json(body_of(payload))
        ).hexdigest()
        assert payload["hash"] == expected


def test_each_line_names_the_one_before_it(tmp_path: Path) -> None:
    path = write_run(tmp_path)
    payloads = lines(path)
    assert payloads[0]["prev_hash"] == GENESIS
    for previous, current in pairwise(payloads):
        assert current["prev_hash"] == previous["hash"]
        assert current["seq"] == previous["seq"] + 1


def test_the_entry_is_recorded_verbatim(tmp_path: Path) -> None:
    """A chain over a summary of a decision answers nothing a year later."""
    path = write_run(tmp_path)
    recorded = records(RUN, directory=tmp_path)
    assert len(recorded) == len(ENTRIES)
    assert [record.stream for record in recorded] == [stream for stream, _, _ in ENTRIES]
    assert [record.data for record in recorded] == [data for _, _, data in ENTRIES]
    assert path.name == f"{RUN}.jsonl"


def test_the_log_is_lf_terminated_json_lines(tmp_path: Path) -> None:
    raw = write_run(tmp_path).read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")


# --- a chain that holds -----------------------------------------------------


def test_an_untouched_log_verifies(tmp_path: Path) -> None:
    write_run(tmp_path)
    report = verify(RUN, directory=tmp_path)
    assert report.ok
    assert report.broken is None
    assert report.verified == report.lines == len(ENTRIES)


def test_the_head_is_the_last_lines_hash(tmp_path: Path) -> None:
    path = write_run(tmp_path)
    assert verify(RUN, directory=tmp_path).head == hashes(path)[-1]


def test_a_trailing_blank_line_is_not_tampering(tmp_path: Path) -> None:
    """A file appended to after a crash can carry a stray newline."""
    path = write_run(tmp_path)
    with path.open("ab") as handle:
        handle.write(b"\n")
    assert verify(RUN, directory=tmp_path).ok


def test_an_empty_log_verifies_as_an_empty_chain(tmp_path: Path) -> None:
    AuditLog(RUN, directory=tmp_path).open().close()
    report = verify(RUN, directory=tmp_path)
    assert report.ok
    assert report.lines == 0
    assert report.head == GENESIS


# --- tampering --------------------------------------------------------------


def test_tampering_with_one_line_is_detected_at_that_line(tmp_path: Path) -> None:
    """The step's headline property: edit line 7, the report says line 7.

    Line 7 is the `CompileAudit` that published the advisory. Changing the
    decision it records is the edit someone would actually want to make, and the
    recomputed digest no longer matches the one the line carries.
    """
    path = write_run(tmp_path)
    original = lines(path)[6]
    assert original["data"]["decision"] == "PUBLISHED"
    tamper(path, 7, {"data": {**original["data"], "decision": "REJECTED"}})

    report = verify(RUN, directory=tmp_path)
    assert not report.ok
    assert report.broken is not None
    assert report.broken.line == 7
    assert report.broken.reason is Break.HASH_MISMATCH
    assert report.broken.stream == "audit"
    assert report.verified == 6


@pytest.mark.parametrize("line", [1, 5, len(ENTRIES)])
def test_any_single_edited_line_is_detected_at_itself(tmp_path: Path, line: int) -> None:
    path = write_run(tmp_path)
    tamper(path, line, {"data": {"id": "forged"}})
    report = verify(RUN, directory=tmp_path)
    assert report.broken is not None
    assert (report.broken.line, report.broken.reason) == (line, Break.HASH_MISMATCH)
    assert report.verified == line - 1


def test_editing_a_field_outside_data_is_detected_too(tmp_path: Path) -> None:
    """`stream`, `entry_id` and the timestamps are inside the digest."""
    path = write_run(tmp_path)
    tamper(path, 4, {"recorded_at": "2026-04-20T00:00:00+00:00"})
    report = verify(RUN, directory=tmp_path)
    assert report.broken is not None
    assert (report.broken.line, report.broken.reason) == (4, Break.HASH_MISMATCH)


def test_an_edit_whose_hash_was_recomputed_breaks_the_next_line(tmp_path: Path) -> None:
    """The careful attacker: line 7 is self-consistent, line 8 is orphaned."""
    path = write_run(tmp_path)
    tamper(path, 7, {"data": {"id": "aud_1", "decision": "REJECTED"}}, rehash=True)
    report = verify(RUN, directory=tmp_path)
    assert report.broken is not None
    assert report.broken.line == 8
    assert report.broken.reason is Break.PREV_MISMATCH
    assert report.verified == 7


def test_rewriting_prev_hash_to_match_breaks_that_line(tmp_path: Path) -> None:
    """Re-pointing a line at another predecessor breaks the line itself.

    `prev_hash` is inside the digest as well as in front of it, so an attacker
    who fixes the link they broke has changed the contents that the line's own
    hash covers.
    """
    path = write_run(tmp_path)
    tamper(path, 7, {"data": {"id": "aud_1", "decision": "REJECTED"}}, rehash=True)
    payloads = lines(path)
    payloads[7] = {**payloads[7], "prev_hash": payloads[6]["hash"]}
    rewrite(path, payloads)

    report = verify(RUN, directory=tmp_path)
    assert report.broken is not None
    assert (report.broken.line, report.broken.reason) == (8, Break.HASH_MISMATCH)


def test_deleting_a_line_is_detected_at_the_line_that_followed_it(tmp_path: Path) -> None:
    path = write_run(tmp_path)
    payloads = lines(path)
    rewrite(path, payloads[:4] + payloads[5:])
    report = verify(RUN, directory=tmp_path)
    assert report.broken is not None
    assert report.broken.line == 5
    assert report.broken.reason is Break.PREV_MISMATCH


def test_swapping_two_lines_is_detected(tmp_path: Path) -> None:
    path = write_run(tmp_path)
    payloads = lines(path)
    payloads[3], payloads[4] = payloads[4], payloads[3]
    rewrite(path, payloads)
    report = verify(RUN, directory=tmp_path)
    assert report.broken is not None
    assert report.broken.line == 4


def test_truncating_the_log_verifies_but_shortens_the_chain(tmp_path: Path) -> None:
    """Dropping the tail cannot be caught by the chain alone, and is not claimed to be.

    This is the limit the module docstring states: the chain shows that lines 1
    to n hold, not that there were never n+1. The head hash is what a reader
    would have to have pinned elsewhere.
    """
    path = write_run(tmp_path)
    full = verify(RUN, directory=tmp_path)
    rewrite(path, lines(path)[:6])
    short = verify(RUN, directory=tmp_path)
    assert short.ok
    assert short.verified == 6
    assert short.head != full.head


def test_a_renumbered_line_is_detected(tmp_path: Path) -> None:
    path = write_run(tmp_path)
    tamper(path, 3, {"seq": 99}, rehash=True)
    report = verify(RUN, directory=tmp_path)
    assert report.broken is not None
    assert (report.broken.line, report.broken.reason) == (3, Break.SEQ_MISMATCH)


def test_a_line_from_another_run_is_detected(tmp_path: Path) -> None:
    path = write_run(tmp_path)
    tamper(path, 2, {"run_id": "run_elsewhere"}, rehash=True)
    report = verify(RUN, directory=tmp_path)
    assert report.broken is not None
    assert (report.broken.line, report.broken.reason) == (2, Break.RUN_MISMATCH)


def test_a_line_with_no_hash_is_detected(tmp_path: Path) -> None:
    path = write_run(tmp_path)
    payloads = lines(path)
    payloads[2] = {key: value for key, value in payloads[2].items() if key != "hash"}
    rewrite(path, payloads)
    report = verify(RUN, directory=tmp_path)
    assert report.broken is not None
    assert (report.broken.line, report.broken.reason) == (3, Break.FIELDS)


def test_a_line_that_is_not_json_is_detected(tmp_path: Path) -> None:
    path = write_run(tmp_path)
    with path.open("ab") as handle:
        handle.write(b"{not json\n")
    report = verify(RUN, directory=tmp_path)
    assert report.broken is not None
    assert report.broken.reason is Break.MALFORMED
    assert report.broken.line == len(ENTRIES) + 1


def test_the_report_summary_names_the_broken_line(tmp_path: Path) -> None:
    path = write_run(tmp_path)
    tamper(path, 7, {"data": {"id": "forged"}})
    summary = verify(RUN, directory=tmp_path).summary()
    assert "line 7" in summary
    assert "hash_mismatch" in summary


# --- resuming, and the run id ----------------------------------------------


def test_reopening_a_log_extends_the_chain(tmp_path: Path) -> None:
    """A restarted auditor must not start a second chain in the same file."""
    write_run(tmp_path)
    with AuditLog(RUN, directory=tmp_path) as log:
        assert log.entries == len(ENTRIES)
        record = log.append(stream="acks", entry_id="99-0", data={"id": "ack_2"})
    assert record.seq == len(ENTRIES)
    report = verify(RUN, directory=tmp_path)
    assert report.ok
    assert report.verified == len(ENTRIES) + 1


def test_a_run_id_is_checked_before_it_becomes_a_filename(tmp_path: Path) -> None:
    for bad in ["../escape", "with/slash", "", "a" * 65, "with space"]:
        with pytest.raises(ChainError):
            AuditLog(bad, directory=tmp_path)
    assert check_run_id("run_20261024T154500Z")


def test_a_generated_run_id_is_usable_as_a_filename() -> None:
    assert check_run_id(new_run_id())


def test_verifying_a_log_that_is_not_there_says_so(tmp_path: Path) -> None:
    with pytest.raises(ChainError, match="no audit log"):
        verify("run_missing", directory=tmp_path)
    with pytest.raises(ChainError, match="no audit log"):
        verify_file(tmp_path / "nothing.jsonl")


def test_chain_hash_is_a_pure_function_of_prev_and_body() -> None:
    body = {"b": 2, "a": 1}
    assert chain_hash(GENESIS, body) == chain_hash(GENESIS, {"a": 1, "b": 2})
    assert chain_hash(GENESIS, body) != chain_hash("1" * 64, body)
