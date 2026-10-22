"""One client every agent calls, whatever `HU_LLM_MODE` says is behind it.

    client = LlmClient.from_env(bus=bus)
    result = await client.complete(
        purpose="sentinel_flood_fields",
        tier=Tier.VOLUME,
        schema=FloodFields,
        system=(SENTINEL_RULES, playbook_text),
        messages=data_block("nws_description", alert.description),
    )
    result.value.waterway        # a validated FloodFields, or an exception

What a call does, in order: refuse to start if the budget is spent, pick the
backend for the tier, look the prompt up in the disk cache, send it, retry
once if the reply does not validate, fall back to the local model if a volume
call cannot reach the API, write the audit record, and store the reply.

Three failures are worth telling apart at the call site:

* `JudgmentUnavailable` - there is no judgment-grade model right now. Take the
  rules path. Variant V1 of the ablation runs on nothing but these paths, so
  every judgment call site has one.
* `LlmParseError` - the model answered twice and neither reply fitted the
  contract. Also a rules-path signal, but worth a loud log: it usually means a
  prompt and a schema have drifted apart.
* `BudgetExceeded` - stop the run. Nothing further will be sent.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from headsup.llm.backends import Backend, ClaudeBackend, MockBackend, OllamaBackend
from headsup.llm.base import (
    DEFAULT_MAX_TOKENS,
    JudgmentUnavailable,
    LlmParseError,
    LlmUnavailable,
    M,
    Message,
    Mode,
    Request,
    Tier,
    TokenUse,
    mode_from_env,
    schema_correction,
)
from headsup.llm.cache import ResponseCache
from headsup.llm.meter import AuditSink, LlmCall, SpendMeter
from headsup.llm.pricing import cost_usd, effort_for, model_for

#: One reply, one retry. A second retry has never been the difference between
#: a usable answer and a rules fallback, and it doubles the worst-case latency
#: of a path the simulator is waiting on.
MAX_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class LlmResult[M: BaseModel]:
    """A validated reply and the audit record of what it cost."""

    value: M
    call: LlmCall

    @property
    def cached(self) -> bool:
        """True if no model was called: the disk cache had this prompt."""
        return self.call.cached

    @property
    def usd(self) -> float:
        return self.call.usd

    @property
    def model(self) -> str:
        return self.call.model


class LlmClient:
    """The tiered client: judgment on Sonnet, volume on Haiku, or neither.

    Hold one per process. The cache, the meter and the backends are injected
    so a test can drive any of them directly; `from_env` and `mock` build the
    two combinations anyone actually wants.
    """

    def __init__(
        self,
        *,
        mode: Mode,
        backends: Mapping[Tier, Backend | None],
        cache: ResponseCache,
        meter: SpendMeter,
        fallback: Backend | None = None,
    ) -> None:
        self.mode = mode
        self.cache = cache
        self.meter = meter
        self._backends = dict(backends)
        self._fallback = fallback

    @classmethod
    def from_env(
        cls,
        *,
        bus: AuditSink | None = None,
        mode: Mode | None = None,
        cache: ResponseCache | None = None,
        meter: SpendMeter | None = None,
    ) -> LlmClient:
        """The client `HU_LLM_MODE` asks for.

        In `claude` mode both tiers share one HTTP client, and a local model
        stands by for volume calls. In `ollama` mode the judgment tier is
        deliberately empty rather than quietly pointed at the local model: a
        7B model writing the rationale attached to a published advisory would
        be worse than the rules path, and no reader of the audit log could
        tell which they had got.
        """
        chosen = mode or mode_from_env()
        backends: dict[Tier, Backend | None] = {}
        fallback: Backend | None = None
        if chosen is Mode.MOCK:
            backend = MockBackend()
            backends = {Tier.JUDGMENT: backend, Tier.VOLUME: backend}
        elif chosen is Mode.OLLAMA:
            backends = {Tier.JUDGMENT: None, Tier.VOLUME: OllamaBackend()}
        else:
            import anthropic

            http = anthropic.AsyncAnthropic()
            backends = {
                tier: ClaudeBackend(model_for(tier), effort=effort_for(tier), client=http)
                for tier in Tier
            }
            fallback = OllamaBackend()
        return cls(
            mode=chosen,
            backends=backends,
            cache=cache or ResponseCache.from_env(enabled=chosen is not Mode.MOCK),
            meter=meter or SpendMeter.from_env(bus=bus),
            fallback=fallback,
        )

    @classmethod
    def mock(
        cls,
        *,
        backend: MockBackend | None = None,
        cache: ResponseCache | None = None,
        meter: SpendMeter | None = None,
    ) -> LlmClient:
        """A client for tests: fixtures in, no cache, no network, no spend."""
        chosen = backend or MockBackend()
        return cls(
            mode=Mode.MOCK,
            backends={Tier.JUDGMENT: chosen, Tier.VOLUME: chosen},
            cache=cache or ResponseCache(enabled=False),
            meter=meter or SpendMeter(),
        )

    @property
    def backend(self) -> Backend | None:
        """The volume backend, which is the one every mode has."""
        return self._backends.get(Tier.VOLUME)

    def backend_for(self, tier: Tier) -> Backend:
        """The backend for a tier, or `JudgmentUnavailable` if there is none."""
        backend = self._backends.get(tier)
        if backend is None:
            raise JudgmentUnavailable(
                f"no {tier.value} model in {self.mode.value} mode; "
                "the caller must use its rules path"
            )
        return backend

    async def complete(
        self,
        *,
        purpose: str,
        tier: Tier,
        schema: type[M],
        system: str | Sequence[str] = (),
        messages: str | Message | Sequence[Message],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        variant: str | None = None,
    ) -> LlmResult[M]:
        """Ask for one structured answer.

        `system` is fixed text - prompts and playbooks - and is where the
        prompt cache breakpoint goes. Everything that varies, and everything
        that came off a feed, goes in `messages` through `data_block`.
        """
        request: Request[M] = Request(
            purpose=purpose,
            tier=tier,
            schema=schema,
            system=_blocks(system),
            messages=_turns(messages),
            max_tokens=max_tokens,
            variant=variant,
        )
        self.meter.check()
        backend = self.backend_for(tier)

        hit = self._from_cache(backend.model, request)
        if hit is not None:
            call = self._call(request, model=backend.model, cached=True)
            return LlmResult(value=hit, call=await self.meter.record(call))

        started = time.perf_counter()
        use = TokenUse()
        attempts = 0
        current = backend
        attempt_request = request
        fallback_to: str | None = None

        while True:
            attempts += 1
            try:
                reply = await current.send(attempt_request)
            except LlmParseError as exc:
                use = use + (exc.use or TokenUse())
                if attempts >= MAX_ATTEMPTS:
                    await self._record_failure(
                        request,
                        model=current.model,
                        use=use,
                        attempts=attempts,
                        started=started,
                        fallback=fallback_to,
                        error=exc.detail,
                    )
                    raise
                attempt_request = request.with_messages(
                    [*request.messages, Message("user", schema_correction(schema, exc.detail))]
                )
                continue
            except LlmUnavailable as exc:
                stand_in = self._stand_in(tier, current)
                if stand_in is None:
                    await self._record_failure(
                        request,
                        model=current.model,
                        use=use,
                        attempts=attempts,
                        started=started,
                        fallback=fallback_to,
                        error=str(exc)[:500],
                    )
                    raise self._no_model(tier, exc) from exc
                current = stand_in
                fallback_to = stand_in.model
                attempt_request = request
                attempts = 0
                continue
            break

        use = use + reply.use
        call = self._call(
            request,
            model=reply.model,
            use=use,
            attempts=attempts,
            started=started,
            fallback=fallback_to,
        )
        self.cache.put(
            self.cache.key(reply.model, request),
            reply.value.model_dump(mode="json"),
            model=reply.model,
            purpose=request.purpose,
        )
        return LlmResult(value=reply.value, call=await self.meter.record(call))

    async def judgment(self, **kwargs: Any) -> LlmResult[Any]:
        """`complete` on the judgment tier."""
        return await self.complete(tier=Tier.JUDGMENT, **kwargs)

    async def volume(self, **kwargs: Any) -> LlmResult[Any]:
        """`complete` on the volume tier."""
        return await self.complete(tier=Tier.VOLUME, **kwargs)

    async def aclose(self) -> None:
        """Close every backend's connection, each one once."""
        seen: set[int] = set()
        for backend in (*self._backends.values(), self._fallback):
            if backend is None or id(backend) in seen:
                continue
            seen.add(id(backend))
            await backend.aclose()

    # --- internals ----------------------------------------------------------

    def _from_cache(self, model: str, request: Request[M]) -> M | None:
        """A stored reply for this prompt, if there is one that still validates."""
        payload = self.cache.get(self.cache.key(model, request))
        if payload is None:
            return None
        try:
            return request.schema.model_validate(payload)
        except ValidationError:
            # The contract moved under the entry. A miss, not a failure.
            return None

    def _stand_in(self, tier: Tier, current: Backend) -> Backend | None:
        """The local model, for a volume call whose primary backend is down."""
        if tier is not Tier.VOLUME or self._fallback is None:
            return None
        if current is self._fallback:
            return None
        return self._fallback

    def _no_model(self, tier: Tier, exc: LlmUnavailable) -> LlmUnavailable:
        """What a caller sees when nothing could answer.

        A judgment call gets `JudgmentUnavailable` whichever way it failed, so
        one `except` at the call site covers both "this mode has no judgment
        model" and "the API is down".
        """
        if tier is Tier.JUDGMENT:
            return JudgmentUnavailable(f"no judgment model answered: {exc}")
        return exc

    def _call(
        self,
        request: Request[Any],
        *,
        model: str,
        use: TokenUse | None = None,
        attempts: int = 1,
        started: float | None = None,
        fallback: str | None = None,
        cached: bool = False,
        ok: bool = True,
        error: str | None = None,
    ) -> LlmCall:
        tokens = use or TokenUse()
        return LlmCall(
            purpose=request.purpose,
            tier=request.tier,
            mode=self.mode,
            model=model,
            cached=cached,
            fallback=fallback,
            attempts=attempts,
            ok=ok,
            error=error,
            input_tokens=tokens.input_tokens,
            output_tokens=tokens.output_tokens,
            cache_read_tokens=tokens.cache_read_tokens,
            cache_write_tokens=tokens.cache_write_tokens,
            usd=0.0 if cached else round(cost_usd(model, tokens), 8),
            latency_ms=0.0 if started is None else round((time.perf_counter() - started) * 1000, 3),
        )

    async def _record_failure(
        self,
        request: Request[Any],
        *,
        model: str,
        use: TokenUse,
        attempts: int,
        started: float,
        fallback: str | None,
        error: str,
    ) -> LlmCall:
        """Audit a call that produced no usable answer. Tokens still count."""
        call = self._call(
            request,
            model=model,
            use=use,
            attempts=attempts,
            started=started,
            fallback=fallback,
            ok=False,
            error=error,
        )
        return await self.meter.record(call)


def _blocks(system: str | Sequence[str]) -> tuple[str, ...]:
    if isinstance(system, str):
        return (system,) if system else ()
    return tuple(block for block in system if block)


def _turns(messages: str | Message | Sequence[Message]) -> tuple[Message, ...]:
    if isinstance(messages, str):
        return (Message("user", messages),)
    if isinstance(messages, Message):
        return (messages,)
    return tuple(messages)
