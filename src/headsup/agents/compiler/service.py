"""The compiler on the bus: the single writer of `advisories`, and its audit trail.

`Compiler` decides; `CompilerService` is the only thing that writes what it
decided. Every call writes one record to `audit`, whatever the outcome - a
rejected proposal leaves exactly as much evidence as a published one, which is
the point of having an audit stream at all.

    async with CompilerService.from_env() as service:
        outcome = await service.publish(proposal, verdict)
        print(outcome.audit.summary())

Two locks hold the single-writer rule. `Bus(writer="compiler")` refuses to
`XADD` to `advisories` from any other writer name, and the Redis ACL in
`headsup.bus.acl` refuses it from any other user. The first is a typo guard;
the second is the control.

A third rule holds the critical-access guard. `from_env` refuses to start
without `HU_CITY` and a world built for it, because this class is the only
thing that writes to `advisories`: a service that came up with the null POI
checker would publish closures without ever checking whether they seal off a
hospital. `hu compile` stays runnable unguarded - it prints the advisory and
writes nothing - which is where the "unguarded" line in an audit record comes
from. Here, unguarded means do not start.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from headsup.agents.compiler.audit import CompileAudit, Decision
from headsup.agents.compiler.compiler import (
    ENV_CITY,
    Compiler,
    CompilerError,
    CompileResult,
)
from headsup.agents.compiler.poi import NullPoiChecker, PoiChecker
from headsup.bus import STREAM_ADVISORIES, STREAM_AUDIT, Bus, BusError
from headsup.bus.acl import PROTECTED_WRITER, Role, url_for
from headsup.contracts import CriticVerdict, ProposedAdvisory


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """What one trip through the gate produced."""

    result: CompileResult
    audit: CompileAudit
    #: The `advisories` entry id, or None if nothing was published.
    stream_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.result.ok and self.stream_id is not None

    def __str__(self) -> str:
        return self.audit.summary()


def env_poi_checker() -> PoiChecker:
    """The critical-access checker `HU_CITY` asks for, or raise.

    `headsup.world` is imported here rather than at module scope because it
    brings in igraph and pyproj, which a process that only reads the bus has
    no use for.
    """
    city = os.environ.get(ENV_CITY, "").strip()
    if not city:
        raise CompilerError(
            f"{ENV_CITY} is unset, so the compiler service has no road graph to "
            "answer the critical-access rule with and will not start. Set "
            f"{ENV_CITY}=austin|san_antonio and run `hu world build --city <city>`."
        )

    from headsup.world.checker import default_poi_checker

    return default_poi_checker(city)


def require_guarded(compiler: Compiler) -> None:
    """Raise unless this compiler can answer the critical-access rule."""
    if isinstance(compiler.poi, NullPoiChecker):
        city = os.environ.get(ENV_CITY, "").strip()
        where = f" for {city}" if city else ""
        raise CompilerError(
            f"the compiler service will not start with no POI data{where}: the "
            "critical-access rule would pass every closure, including one that "
            "seals off a hospital. Run `hu world build --city "
            f"{city or '<city>'}` first."
        )


class CompilerService:
    """Compiles proposals and writes the results to the bus."""

    def __init__(
        self,
        bus: Bus | None = None,
        compiler: Compiler | None = None,
        *,
        advisories_stream: str = STREAM_ADVISORIES,
        audit_stream: str = STREAM_AUDIT,
        allow_unguarded: bool = False,
    ) -> None:
        """Refuses to start unguarded, however it is constructed.

        With no `compiler`, `HU_CITY` names the city whose road graph answers
        the critical-access rule, and that city has to be built. With one
        given, its POI checker has to be a real one.

        `allow_unguarded` is for the tests that exercise the bus and the ACL
        rather than the POI rule: it has to be asked for in writing, so a
        service that publishes without the guard is always a deliberate one.
        """
        self.bus = bus if bus is not None else Bus.from_env(writer=PROTECTED_WRITER)
        self.compiler = (
            compiler
            if compiler is not None
            else Compiler(poi=NullPoiChecker() if allow_unguarded else env_poi_checker())
        )
        if not allow_unguarded:
            require_guarded(self.compiler)
        self.advisories_stream = advisories_stream
        self.audit_stream = audit_stream
        self._owns_bus = bus is None

    @classmethod
    def from_env(
        cls, *, namespace: str | None = None, compiler: Compiler | None = None
    ) -> CompilerService:
        """A service connected as the `compiler` ACL user, if one is configured.

        `url_for` returns `REDIS_URL` untouched when no compiler password is
        in the environment, so a dev box with no ACLs applied still works.

        Refuses to start unguarded, like every other way of constructing one:
        see `__init__`.
        """
        bus = Bus(
            url_for(Role.COMPILER),
            namespace=(
                namespace if namespace is not None else os.environ.get("HU_STREAM_NAMESPACE", "")
            ),
            writer=PROTECTED_WRITER,
        )
        return cls(bus, compiler)

    async def __aenter__(self) -> CompilerService:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_bus:
            await self.bus.close()

    async def publish(
        self,
        proposal: ProposedAdvisory | Mapping[str, Any],
        verdict: CriticVerdict | Mapping[str, Any],
        *,
        version: int = 1,
    ) -> PublishOutcome:
        """Compile one proposal, publish it if it passed, and audit either way.

        The advisory is written first and the audit record second, carrying the
        entry id of the advisory it describes. If the advisory write fails the
        audit record says `PUBLISH_FAILED` and the `BusError` is re-raised:
        the log never claims an advisory a reader cannot find.
        """
        result = self.compiler.compile(proposal, verdict, version=version)
        stream_id: str | None = None
        decision = Decision.PUBLISHED if result.ok else Decision.REJECTED
        failure: BusError | None = None

        if result.ok:
            try:
                stream_id = await self.bus.publish(result.advisory, stream=self.advisories_stream)
            except BusError as exc:
                decision, failure = Decision.PUBLISH_FAILED, exc

        signer = self.compiler.signer
        audit = CompileAudit.from_result(
            result,
            decision=decision,
            key_fingerprint=signer.fingerprint,
            key_label=signer.label,
            stream_id=stream_id,
        )
        await self.bus.publish(audit, stream=self.audit_stream)
        if failure is not None:
            raise failure
        return PublishOutcome(result=result, audit=audit, stream_id=stream_id)
