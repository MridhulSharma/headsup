"""`hu compile`, `hu verify` and `hu keygen` against the committed fixtures.

These are the step's Done-when checks in test form, so a regression shows up
in the default test run and not only when someone types the command.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from headsup.agents.compiler import Signer, new_token
from headsup.cli import app
from headsup.contracts import Advisory

runner = CliRunner()


def flat(output: str) -> str:
    """Rich wraps to the console width, so match on unwrapped text."""
    return " ".join(output.split())


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PROPOSAL = FIXTURES / "proposal_flood.json"
SEVERE = FIXTURES / "proposal_flood_severe.json"

#: Inside the fixtures' real validity window (20 April 2026, 18:55-20:55Z).
#: Without `--now` the compiler is right to refuse a recorded proposal whose
#: window closed months ago, so every compile here injects the clock.
DURING_S1 = "2026-04-20T19:30:00Z"
AFTER_S1 = "2026-04-21T06:00:00Z"


@pytest.fixture(autouse=True)
def no_key_in_the_environment(monkeypatch):
    """Run as a fresh clone does: no signing key, so the dev key is used.

    `HU_CITY` goes too: with it set, `hu compile` loads that city's road graph
    for the critical-access rule, and these tests would then pass or fail on
    what somebody had built into `data/cache/`.
    """
    monkeypatch.delenv("HU_SIGNING_KEY", raising=False)
    monkeypatch.delenv("HU_VERIFY_KEY", raising=False)
    monkeypatch.delenv("HU_CITY", raising=False)


def compile_fixture(path: Path, out: Path, *args: str, now: str | None = DURING_S1):
    argv = ["compile", "--file", str(path), "--out", str(out)]
    if now is not None:
        argv += ["--now", now]
    return runner.invoke(app, [*argv, *args])


# --- the critical-access rule's POI data ------------------------------------


def test_compile_says_the_access_rule_is_unguarded_without_a_city(tmp_path: Path) -> None:
    """No `--city` and no `HU_CITY`: visibly unguarded, not quietly."""
    result = compile_fixture(PROPOSAL, tmp_path / "advisory.json")
    assert result.exit_code == 0, result.output
    assert "unguarded" in flat(result.output)
    assert "hu world build" in flat(result.output)


def test_compile_is_unguarded_for_a_city_that_is_not_built(tmp_path: Path) -> None:
    empty = tmp_path / "cache"
    empty.mkdir()
    result = compile_fixture(
        PROPOSAL, tmp_path / "advisory.json", "--city", "austin", "--cache", str(empty)
    )
    assert result.exit_code == 0, result.output
    assert "unguarded" in flat(result.output)


def test_compile_names_the_city_whose_graph_answered(tmp_path: Path) -> None:
    """With a built city, the audit record names its graph, not the stub."""
    import shutil

    from fixtures.world import GRAPH_FILE, grid_world
    from headsup.world import graph_path, pois_path, save_pois
    from headsup.world.model import clear_world_cache

    cache = tmp_path / "cache"
    cache.mkdir()
    shutil.copy(GRAPH_FILE, graph_path("austin", cache))
    save_pois(pois_path("austin", cache), "austin", grid_world().pois)
    clear_world_cache()
    try:
        result = compile_fixture(
            PROPOSAL, tmp_path / "advisory.json", "--city", "austin", "--cache", str(cache)
        )
        assert result.exit_code == 0, result.output
        assert "world-poi-checker" in flat(result.output)
        assert "austin graph" in flat(result.output)
    finally:
        clear_world_cache()


# --- hu compile -------------------------------------------------------------


def test_compile_signs_the_s1_proposal(tmp_path: Path) -> None:
    out = tmp_path / "advisory.json"
    result = compile_fixture(PROPOSAL, out)
    assert result.exit_code == 0, result.output

    advisory = Advisory.model_validate(json.loads(out.read_text(encoding="utf-8")))
    assert advisory.properties.id == "adv_s1_flood_0001"
    assert advisory.properties.policy_rule == "flood.no_entry.v1"
    assert advisory.properties.issuer == "headsup/san_antonio"
    assert advisory.properties.signature


def test_compile_prints_the_rule_checklist(tmp_path: Path) -> None:
    result = compile_fixture(PROPOSAL, tmp_path / "a.json")
    for rule in ("area_under_cap", "lifetime_under_cap", "two_key_approved", "advisory_valid"):
        assert rule in result.output


def test_compile_warns_that_the_development_key_is_not_authentication(tmp_path: Path) -> None:
    result = compile_fixture(PROPOSAL, tmp_path / "a.json")
    assert "published development key" in flat(result.output)


def test_compile_says_it_used_the_pass_through_critic_stub(tmp_path: Path) -> None:
    """The fixture carries no verdict, and the output must not hide that."""
    result = compile_fixture(PROPOSAL, tmp_path / "a.json")
    assert "pass-through critic stub" in flat(result.output)


def test_compile_writes_json_to_stdout_when_quiet() -> None:
    result = runner.invoke(app, ["compile", "--file", str(PROPOSAL), "--quiet", "--now", DURING_S1])
    assert result.exit_code == 0, result.output
    assert Advisory.model_validate(json.loads(result.output)).properties.signature


def test_compile_refuses_a_severity_four_proposal_without_a_token(tmp_path: Path) -> None:
    result = compile_fixture(SEVERE, tmp_path / "a.json")
    assert result.exit_code == 1
    assert "two_key_approved" in result.output
    assert "--approval-token" in flat(result.output)
    assert not (tmp_path / "a.json").exists()


def test_compile_publishes_a_severity_four_proposal_with_a_token(tmp_path: Path) -> None:
    out = tmp_path / "advisory.json"
    result = compile_fixture(
        SEVERE, out, "--approval-token", new_token(), "--approved-by", "duty_officer_7"
    )
    assert result.exit_code == 0, result.output
    advisory = Advisory.model_validate(json.loads(out.read_text(encoding="utf-8")))
    assert advisory.properties.hazard.severity == 4


def test_compile_rejects_an_unusable_approval_token(tmp_path: Path) -> None:
    result = compile_fixture(SEVERE, tmp_path / "a.json", "--approval-token", "short")
    assert result.exit_code == 1
    assert "not a usable token" in flat(result.output)


def test_compile_reports_a_proposal_that_is_not_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    result = compile_fixture(bad, tmp_path / "a.json")
    assert result.exit_code != 0
    assert "not JSON" in result.output


def test_compile_accepts_a_file_holding_a_proposal_and_a_verdict(tmp_path: Path) -> None:
    """D2.5 wires proposer to compiler; a verdict in the file is honoured."""
    proposal = json.loads(PROPOSAL.read_text(encoding="utf-8"))
    bundle = tmp_path / "bundle.json"
    bundle.write_text(
        json.dumps(
            {
                "proposal": proposal,
                "verdict": {
                    "proposal_id": proposal["id"],
                    "verdict": "ESCALATE",
                    "reasons": ["The evidence does not explain an area this large."],
                },
            }
        ),
        encoding="utf-8",
    )
    result = compile_fixture(bundle, tmp_path / "a.json")
    assert result.exit_code == 1
    assert "escalated" in result.output
    assert "pass-through critic stub" not in flat(result.output)


def test_compile_honours_a_playbook_directory(tmp_path: Path) -> None:
    """A playbook that forbids NO_ENTRY for floods refuses the S1 proposal."""
    import yaml

    books = tmp_path / "playbooks"
    books.mkdir()
    (books / "flood.yaml").write_text(
        yaml.safe_dump(
            {
                "class": "FLOOD",
                "policy_rule": "flood.v1",
                "description": "Speed caps only.",
                "allowed_actions": ["SPEED_CAP"],
                "max_area_km2": 1.0,
                "max_lifetime_minutes": 60,
                "two_key_area_km2": 1.0,
            }
        ),
        encoding="utf-8",
    )
    result = compile_fixture(PROPOSAL, tmp_path / "a.json", "--playbooks", str(books))
    assert result.exit_code == 1
    assert "action_allowed" in result.output


# --- hu verify --------------------------------------------------------------


def test_verify_accepts_what_compile_produced(tmp_path: Path) -> None:
    """The Done-when check: `hu compile | hu verify`."""
    out = tmp_path / "advisory.json"
    assert compile_fixture(PROPOSAL, out).exit_code == 0

    result = runner.invoke(app, ["verify", "--file", str(out), "--allow-dev"])
    assert result.exit_code == 0, result.output
    assert "signature ok" in flat(result.output)
    assert "adv_s1_flood_0001" in flat(result.output)


def test_verify_refuses_a_tampered_advisory(tmp_path: Path) -> None:
    out = tmp_path / "advisory.json"
    assert compile_fixture(PROPOSAL, out).exit_code == 0

    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["properties"]["valid_until"] = "2027-04-20T03:00:00Z"
    out.write_text(json.dumps(payload), encoding="utf-8")

    result = runner.invoke(app, ["verify", "--file", str(out), "--allow-dev"])
    assert result.exit_code == 1
    assert "does NOT verify" in flat(result.output)


def test_verify_refuses_another_signers_key(tmp_path: Path) -> None:
    out = tmp_path / "advisory.json"
    assert compile_fixture(PROPOSAL, out).exit_code == 0

    result = runner.invoke(
        app,
        ["verify", "--file", str(out), "--key", Signer.generate().verify_key_b64, "--allow-dev"],
    )
    assert result.exit_code == 1


def test_verify_refuses_the_hand_written_sample_advisory() -> None:
    """tests/fixtures/advisory_s1_flood.json carries a made-up signature."""
    result = runner.invoke(app, ["verify", "--file", str(FIXTURES / "advisory_s1_flood.json")])
    assert result.exit_code == 1


def test_verify_reports_a_file_that_is_not_an_advisory(tmp_path: Path) -> None:
    result = runner.invoke(app, ["verify", "--file", str(PROPOSAL)])
    assert result.exit_code == 1
    assert "is not a valid Advisory" in flat(result.output)


# --- the injectable clock (--now) ------------------------------------------


def test_compile_refuses_a_recorded_proposal_on_todays_clock(tmp_path: Path) -> None:
    """The fixtures carry the real S1 window, which closed in April 2026.

    Refusing it is the correct answer, not a broken fixture: an advisory whose
    window is over is noise on the bus, and a fleet acting on it acts on
    nothing.
    """
    result = compile_fixture(PROPOSAL, tmp_path / "a.json", now=None)
    assert result.exit_code == 1
    assert "window_usable" in result.output
    assert "already passed" in flat(result.output)
    assert not (tmp_path / "a.json").exists()


def test_compile_judges_the_window_at_the_injected_instant(tmp_path: Path) -> None:
    out = tmp_path / "advisory.json"
    assert compile_fixture(PROPOSAL, out, now=DURING_S1).exit_code == 0
    advisory = Advisory.model_validate(json.loads(out.read_text(encoding="utf-8")))
    assert advisory.properties.valid_from.isoformat() == "2026-04-20T18:55:00+00:00"


def test_compile_refuses_an_instant_after_the_window_closed(tmp_path: Path) -> None:
    result = compile_fixture(PROPOSAL, tmp_path / "a.json", now=AFTER_S1)
    assert result.exit_code == 1
    assert "window_usable" in result.output


def test_the_injected_clock_does_not_move_the_signed_window(tmp_path: Path) -> None:
    """`--now` is the judge's clock, not an edit to the advisory."""
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    assert compile_fixture(PROPOSAL, first, now="2026-04-20T18:56:00Z").exit_code == 0
    assert compile_fixture(PROPOSAL, second, now="2026-04-20T20:54:00Z").exit_code == 0
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "value",
    ["yesterday", "2026-13-40T00:00:00Z", "", "19:30"],
)
def test_compile_reports_an_unusable_now(tmp_path: Path, value: str) -> None:
    result = compile_fixture(PROPOSAL, tmp_path / "a.json", now=value)
    assert result.exit_code != 0


def test_a_naive_now_is_read_as_utc(tmp_path: Path) -> None:
    """Every timestamp in this system is UTC; guessing a local zone could
    silently shift a validity window."""
    assert compile_fixture(PROPOSAL, tmp_path / "a.json", now="2026-04-20T19:30:00").exit_code == 0


def test_verify_reports_the_window_at_the_injected_instant(tmp_path: Path) -> None:
    out = tmp_path / "advisory.json"
    assert compile_fixture(PROPOSAL, out).exit_code == 0

    active = runner.invoke(app, ["verify", "--file", str(out), "--allow-dev", "--now", DURING_S1])
    assert active.exit_code == 0, active.output
    assert "active at 2026-04-20T19:30:00" in flat(active.output)

    expired = runner.invoke(app, ["verify", "--file", str(out), "--allow-dev", "--now", AFTER_S1])
    # An expired advisory still carries a good signature: expiry is a separate
    # fact from authenticity, and `hu verify` answers the signature question.
    assert expired.exit_code == 0, expired.output
    assert "not active at 2026-04-21T06:00:00" in flat(expired.output)


# --- the development-key gate (--allow-dev) --------------------------------


def test_verify_refuses_a_development_signature_by_default(tmp_path: Path) -> None:
    """A key printed in the source authenticates nothing, so "ok" would lie."""
    out = tmp_path / "advisory.json"
    assert compile_fixture(PROPOSAL, out).exit_code == 0

    result = runner.invoke(app, ["verify", "--file", str(out)])
    assert result.exit_code == 1
    assert "development key" in flat(result.output)
    assert "authenticates nothing" in flat(result.output)
    assert "--allow-dev" in flat(result.output)
    assert "signature ok" not in flat(result.output)


def test_verify_accepts_a_development_signature_when_asked(tmp_path: Path) -> None:
    out = tmp_path / "advisory.json"
    assert compile_fixture(PROPOSAL, out).exit_code == 0

    result = runner.invoke(app, ["verify", "--file", str(out), "--allow-dev"])
    assert result.exit_code == 0, result.output
    assert "signature ok" in flat(result.output)
    assert "accepted a development-key signature" in flat(result.output)


def test_verify_needs_no_flag_for_a_real_key(tmp_path: Path, monkeypatch) -> None:
    """With a real HU_SIGNING_KEY there is nothing to warn about."""
    signer = Signer.generate()
    monkeypatch.setenv("HU_SIGNING_KEY", signer.seed_b64)
    out = tmp_path / "advisory.json"
    assert compile_fixture(PROPOSAL, out).exit_code == 0
    assert "development key" not in flat(compile_fixture(PROPOSAL, tmp_path / "b.json").output)

    result = runner.invoke(app, ["verify", "--file", str(out)])
    assert result.exit_code == 0, result.output
    assert "signature ok" in flat(result.output)
    assert "development" not in flat(result.output)


def test_verify_refuses_a_development_signature_named_explicitly(tmp_path: Path) -> None:
    """Naming the dev key with --key is still the dev key."""
    out = tmp_path / "advisory.json"
    assert compile_fixture(PROPOSAL, out).exit_code == 0

    result = runner.invoke(
        app, ["verify", "--file", str(out), "--key", Signer.dev().verify_key_b64]
    )
    assert result.exit_code == 1
    assert "development key" in flat(result.output)


# --- hu keygen --------------------------------------------------------------


def test_keygen_prints_a_public_key_but_not_the_seed() -> None:
    result = runner.invoke(app, ["keygen"])
    assert result.exit_code == 0, result.output
    assert "HU_VERIFY_KEY" in result.output
    assert "HU_SIGNING_KEY" not in result.output


def test_keygen_prints_the_seed_when_asked() -> None:
    result = runner.invoke(app, ["keygen", "--show-private"])
    assert result.exit_code == 0, result.output
    assert "HU_SIGNING_KEY" in result.output


def test_bus_acl_help_lists_apply_show_and_remove() -> None:
    result = runner.invoke(app, ["bus", "acl", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("apply", "show", "remove"):
        assert command in result.output
