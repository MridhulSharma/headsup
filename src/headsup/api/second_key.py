"""A stand-in for the person holding the second key, for `hu demo`.

Severity 4 and above needs a human approval token, and S1's narrowed flash
flood hazard is a severity 4. So a demo that ran with nobody at the keyboard
would reach a proposal, be refused on `two_key_approved`, and stop - which is
the rule working, and a demo of two thirds of the pipeline.

This is the missing third: a task that watches what the gate is waiting on and
issues a token for it, through `POST /approvals/{proposal_id}` over HTTP, the
same request the viewer's approve button makes. It has no privileges of its
own. It cannot see a proposal the gate did not refuse for want of a key, its
token is checked like any other, and a proposal that fails any other rule is
refused again with its reasons.

It is a stand-in and says so everywhere it can be read: `approved_by` is
`demo_operator`, never an operator id, and the note names this module. An
audit record of a demo run can therefore never be mistaken for one where a
person looked at the hazard and agreed.

`hu serve` does not run it. `hu demo` does, and `--no-approve` turns it off to
watch the gate refuse instead.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

#: Written into every approval this module issues. Not an operator id: no
#: `Selector` matches it, and a reader grepping the audit stream for what a
#: person approved will not find it.
DEMO_APPROVER = "demo_operator"

#: Said in the approval's own note, so the record carries its own disclaimer.
DEMO_NOTE = "issued by headsup.api.second_key for a demo run; no person reviewed this hazard"

#: How long a demo token counts for. Long enough for the compile that follows
#: the grant, short enough that it is not a standing waiver over a long replay.
DEMO_MINUTES = 15

#: Seconds between looks at the pending list. A proposal that needs a key is
#: already waiting when it appears, so this is the demo's approval latency.
POLL_S = 0.5


async def hold_the_second_key(
    base_url: str,
    *,
    approver: str = DEMO_APPROVER,
    poll_s: float = POLL_S,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Approve whatever the gate is waiting on, until cancelled.

    Runs forever. Every error is logged and retried on the next poll: a demo
    must not die because one request lost a race with a scenario stopping.
    """
    owned = client is None
    http = client or httpx.AsyncClient(base_url=base_url, timeout=30.0)
    granted: set[str] = set()
    try:
        while True:
            try:
                await _one_pass(http, approver, granted)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("second key: %s", exc)
            await asyncio.sleep(poll_s)
    finally:
        if owned:
            await http.aclose()


async def _one_pass(http: httpx.AsyncClient, approver: str, granted: set[str]) -> None:
    """Issue a token for each proposal waiting on one, once each."""
    listing = await http.get("/approvals")
    if listing.status_code != 200:
        return
    for row in listing.json().get("awaiting_approval", []):
        proposal_id = str(row.get("proposal_id") or "")
        if not proposal_id or proposal_id in granted:
            continue
        granted.add(proposal_id)
        response = await http.post(
            f"/approvals/{proposal_id}",
            json={"approved_by": approver, "minutes": DEMO_MINUTES, "note": DEMO_NOTE},
        )
        if response.status_code != 201:
            log.warning(
                "second key: %s refused the token for %s: %s",
                response.status_code,
                proposal_id,
                response.text[:200],
            )
            continue
        advisory_id = response.json().get("advisory_id")
        log.info(
            "second key: approved %s (%s %s, severity %s, %.4f km2) -> %s",
            proposal_id,
            row.get("hazard_class"),
            row.get("action"),
            row.get("severity"),
            float(row.get("area_km2") or 0.0),
            advisory_id or "still refused; see the audit stream",
        )
