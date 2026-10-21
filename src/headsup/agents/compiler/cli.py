"""`hu compile`, `hu verify` and `hu keygen`.

These are the hand tools for the gate: compile one proposal from a file and
look at what came back, check a signature, or make a signing key for `.env`.
The agents call `CompilerService` instead; this module is for a person at a
terminal and for the step's Done-when checks.

    uv run hu compile --file tests/fixtures/proposal_flood.json --now 2026-04-20T19:30:00Z
    uv run hu verify --file adv.json --allow-dev

The committed proposal fixtures are dated to the real S1 event (20 April 2026),
so compiling one today is refused for having already expired - which is the
right answer for a recorded proposal. `--now` injects the clock the gate judges
against, the same way `hu ingest replay` drives the feeds, and is what lets a
recording be compiled as it was at the time.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from headsup.agents.compiler.approvals import InMemoryApprovals
from headsup.agents.compiler.compiler import Compiler, CompilerError
from headsup.agents.compiler.playbooks import PlaybookError, PlaybookSet, default_playbooks
from headsup.agents.compiler.rules import Check
from headsup.agents.compiler.service import CompilerService
from headsup.agents.compiler.signing import (
    DEV_WARNING,
    Signer,
    SigningError,
    is_dev_key,
    verify,
)
from headsup.bus import BusError
from headsup.contracts import Advisory, CriticVerdict, Verdict, utcnow

console = Console()
err = Console(stderr=True)

#: Read from stdin instead of a file.
STDIN = "-"

#: What `hu compile` assumes when the file carries no critic verdict. D2.5
#: wires the proposer straight to the compiler with exactly this stub, and
#: D3.2 replaces it with the real critic.
STUB_REASON = "pass-through critic stub (D1.3); the real critic arrives in D3.2"


def _first_error(exc: ValidationError) -> str:
    """The first validation problem, short enough for one console line."""
    first = exc.errors()[0]
    where = ".".join(str(item) for item in first["loc"]) or "<root>"
    return f"{where}: {first['msg']}"


def _parse_now(value: str | None) -> datetime | None:
    """`--now` to an aware UTC datetime, or None to use the real clock.

    A naive timestamp is read as UTC rather than refused: every timestamp that
    crosses this system is UTC, and guessing the local zone would be the one
    reading that could silently shift a validity window.
    """
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--now {value!r} is not an ISO-8601 timestamp (e.g. 2026-04-20T19:30:00Z)"
        ) from exc
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _read_json(where: str) -> Any:
    """Parse JSON from a file path or from stdin."""
    try:
        text = sys.stdin.read() if where == STDIN else Path(where).read_text(encoding="utf-8")
    except OSError as exc:
        raise typer.BadParameter(f"cannot read {where}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{where} is not JSON: {exc}") from exc


def _split(payload: Any, where: str) -> tuple[Any, Any | None]:
    """A file holding a bare proposal, or `{proposal, verdict}`, or `{proposal}`."""
    if not isinstance(payload, dict):
        raise typer.BadParameter(f"{where}: expected a JSON object")
    if "proposal" in payload:
        return payload["proposal"], payload.get("verdict")
    return payload, None


def _stub_verdict(proposal_id: str) -> CriticVerdict:
    return CriticVerdict(
        proposal_id=proposal_id,
        verdict=Verdict.APPROVE,
        reasons=[STUB_REASON],
        reviewed_by="critic_stub",
    )


def _checks_table(checks: tuple[Check, ...]) -> Table:
    table = Table(title="compiler rules", show_lines=False)
    table.add_column("rule")
    table.add_column("result")
    table.add_column("detail", overflow="fold")
    for check in checks:
        mark = "[green]ok[/green]" if check.ok else "[red]FAILED[/red]"
        table.add_row(check.code.value, mark, check.detail)
    return table


def _load_playbooks(directory: str | None) -> PlaybookSet:
    try:
        return PlaybookSet.load(directory) if directory else default_playbooks()
    except PlaybookError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


def compile_proposal(
    file: str = typer.Option(..., "--file", "-f", help=f"Proposal JSON, or {STDIN} for stdin."),
    out: str | None = typer.Option(
        None, "--out", "-o", help="Write the signed advisory here instead of stdout."
    ),
    playbooks: str | None = typer.Option(
        None, "--playbooks", help="Playbook directory (default: playbooks/)."
    ),
    approval_token: str | None = typer.Option(
        None, "--approval-token", help="Human approval token for a two-key advisory."
    ),
    approved_by: str = typer.Option(
        "cli_operator", "--approved-by", help="Who issued that approval token."
    ),
    publish: bool = typer.Option(
        False, "--publish", help="Also write to the advisories and audit streams."
    ),
    now: str | None = typer.Option(
        None,
        "--now",
        help=(
            "Judge the validity window as at this ISO-8601 instant instead of the "
            "real clock, e.g. 2026-04-20T19:30:00Z. Needed to compile a recorded "
            "proposal, whose window is in the past."
        ),
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only the advisory JSON."),
) -> None:
    """Compile one proposal into a signed advisory, or show why it was refused."""
    payload = _read_json(file)
    raw_proposal, raw_verdict = _split(payload, file)
    moment = _parse_now(now)

    try:
        signer = Signer.from_env(allow_dev_key=True)
    except SigningError as exc:  # pragma: no cover - only a malformed key reaches this
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if signer.is_dev_key and not quiet:
        err.print(f"[yellow]{DEV_WARNING}[/yellow]")

    approvals = InMemoryApprovals()
    proposal_id = raw_proposal.get("id") if isinstance(raw_proposal, dict) else None
    if approval_token and proposal_id:
        try:
            approvals.grant(
                str(proposal_id),
                approved_by,
                token=approval_token,
                ttl=None,
                note="granted on the CLI",
                now=moment,
            )
        except ValidationError as exc:
            err.print(f"[red]--approval-token is not a usable token: {_first_error(exc)}[/red]")
            raise typer.Exit(1) from exc

    try:
        compiler = Compiler(
            playbooks=_load_playbooks(playbooks),
            signer=signer,
            approvals=approvals,
            clock=(lambda: moment) if moment is not None else None,
        )
    except CompilerError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    verdict = raw_verdict if raw_verdict is not None else _stub_verdict(str(proposal_id or ""))
    if raw_verdict is None and not quiet:
        err.print(f"[dim]no verdict in {file}: using the {STUB_REASON}[/dim]")

    if publish:
        result, stream_id = asyncio.run(_publish(compiler, raw_proposal, verdict))
    else:
        result, stream_id = compiler.compile(raw_proposal, verdict), None

    if not quiet:
        console.print(_checks_table(result.checks))

    if not result.ok:
        err.print(f"[red]rejected {result.proposal_id}[/red]")
        for reason in result.reasons:
            err.print(f"  [red]-[/red] {reason}")
        if result.needs_approval:
            err.print(
                "[yellow]this advisory needs a second key: re-run with "
                "--approval-token <token> --approved-by <who>[/yellow]"
            )
        raise typer.Exit(1)

    rendered = json.dumps(result.advisory.to_wire(), indent=2, ensure_ascii=False)
    if out:
        Path(out).write_text(rendered + "\n", encoding="utf-8", newline="\n")
        if not quiet:
            console.print(f"[green]wrote {Path(out).as_posix()}[/green]")
    else:
        console.print_json(rendered)

    if not quiet:
        properties = result.advisory.properties
        console.print(
            f"[green]{properties.id} v{properties.version}[/green] "
            f"{properties.action.type.value} | {properties.policy_rule} | "
            f"issuer {properties.issuer} | signed {signer.fingerprint} ({signer.label})"
        )
        if stream_id:
            console.print(f"[green]published to advisories as {stream_id}[/green]")


async def _publish(compiler: Compiler, proposal: Any, verdict: Any) -> tuple[Any, str | None]:
    """Compile and write to the bus, so `--publish` audits like an agent does."""
    service = CompilerService.from_env(compiler=compiler)
    try:
        outcome = await service.publish(proposal, verdict)
    except BusError as exc:
        err.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        await service.close()
    return outcome.result, outcome.stream_id


def verify_advisory(
    file: str = typer.Option(..., "--file", "-f", help=f"Advisory JSON, or {STDIN} for stdin."),
    key: str | None = typer.Option(
        None,
        "--key",
        help="Base64 Ed25519 public key (default: HU_VERIFY_KEY, then HU_SIGNING_KEY).",
    ),
    allow_dev: bool = typer.Option(
        False,
        "--allow-dev",
        help=(
            "Accept a signature made with the published development key. Without "
            "this, a development signature is refused: it authenticates nothing."
        ),
    ),
    now: str | None = typer.Option(
        None,
        "--now",
        help=(
            "Report whether the advisory is active at this ISO-8601 instant "
            "instead of the real clock. Does not affect the signature check."
        ),
    ),
) -> None:
    """Check an advisory's Ed25519 signature. Exit 0 only if it verifies.

    A signature made with the development key is a failure unless `--allow-dev`
    says otherwise, so a run cannot report "signature ok" for an advisory that
    nobody can be held to.
    """
    payload = _read_json(file)
    moment = _parse_now(now)
    try:
        advisory = Advisory.model_validate(payload)
    except ValueError as exc:
        err.print(f"[red]{file} is not a valid Advisory: {exc}[/red]")
        raise typer.Exit(1) from exc

    if not verify(advisory, verify_key=key):
        err.print(f"[red]signature does NOT verify for {advisory.properties.id}[/red]")
        raise typer.Exit(1)

    properties = advisory.properties
    development = is_dev_key(key)
    if development and not allow_dev:
        err.print(
            f"[red]{properties.id} verifies, but only against the published development key[/red]"
        )
        err.print(
            "[red]that key is printed in the source, so the signature authenticates nothing[/red]"
        )
        err.print(
            "[yellow]set HU_SIGNING_KEY (see `hu keygen`) for a real signature, "
            "or pass --allow-dev to accept this one[/yellow]"
        )
        raise typer.Exit(1)

    window = "active" if advisory.is_active(moment) else "not active"
    at = (moment or utcnow()).isoformat()
    console.print(
        f"[green]signature ok[/green] {properties.id} v{properties.version} "
        f"issuer {properties.issuer} | {window} at {at} "
        f"(window ends {properties.valid_until.isoformat()})"
    )
    if development:
        err.print("[yellow]accepted a development-key signature (--allow-dev)[/yellow]")


def keygen(
    show_private: bool = typer.Option(
        False, "--show-private", help="Also print the seed. It belongs in .env, never in git."
    ),
) -> None:
    """Generate an Ed25519 key pair for HU_SIGNING_KEY."""
    signer = Signer.generate()
    console.print(f"[dim]fingerprint[/dim] {signer.fingerprint}")
    console.print(f"[dim]HU_VERIFY_KEY[/dim]  {signer.verify_key_b64}")
    if show_private:
        console.print(f"[dim]HU_SIGNING_KEY[/dim] {signer.seed_b64}")
    else:
        console.print("[yellow]re-run with --show-private to print the signing seed[/yellow]")


def register(app: typer.Typer) -> None:
    """Add the compiler's commands to the top-level `hu` app."""
    app.command("compile")(compile_proposal)
    app.command("verify")(verify_advisory)
    app.command("keygen")(keygen)
