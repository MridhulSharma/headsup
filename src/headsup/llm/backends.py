"""The three things that can answer a request, behind one method.

Each backend takes a `Request` and returns a `Reply` holding a validated
Pydantic model, or raises: `LlmParseError` if the reply did not satisfy the
schema (the client retries once), `LlmUnavailable` if the backend could not be
reached (a volume call falls back to the local model), `LlmError` for anything
the caller has to fix.

Parsing is one function for all three - `parse_payload` - so a mocked reply
has to clear exactly the bar a real one does. A fixture that no longer matches
its contract fails the unit run instead of passing it and failing the demo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import anthropic
import ollama
import orjson
from pydantic import BaseModel, ValidationError

from headsup.llm.base import (
    ENV_OLLAMA_MODEL,
    LlmError,
    LlmParseError,
    LlmUnavailable,
    M,
    Mode,
    Reply,
    Request,
    TokenUse,
    validation_detail,
)
from headsup.llm.fixtures import FixtureStore

#: The local model Ollama serves when `HU_LLM_MODE=ollama`, or when a volume
#: call falls back. Small on purpose: it runs on the demo laptop beside the
#: simulator and only ever fills fixed fields.
DEFAULT_OLLAMA_MODEL = "llama3.1:8b"

#: Rough characters per token, for the token counts mock mode reports. The
#: numbers are not billed; they are there so an eval run in mock mode still
#: produces a plausible token profile.
MOCK_CHARS_PER_TOKEN = 4

#: The SDK turns a Pydantic model into the strict JSON Schema the API will
#: constrain a reply to. Reached through a private module because the public
#: entry point, `messages.parse`, raises before handing back the raw reply and
#: its token counts - and this layer has to record what every attempt cost,
#: including one that failed to validate. `test_backends` asserts this is not
#: None, so an SDK upgrade that moves it fails the unit run, not the demo.
try:  # pragma: no cover - exercised by the import itself
    from anthropic.lib._parse._transform import transform_schema as SDK_SCHEMA_TRANSFORM
except ImportError:  # pragma: no cover
    SDK_SCHEMA_TRANSFORM = None  # type: ignore[assignment]


@runtime_checkable
class Backend(Protocol):
    """Whatever answers a request in the current mode."""

    #: The model name that goes in the audit record and the cache key.
    model: str

    async def send(self, request: Request[M]) -> Reply[M]: ...

    async def aclose(self) -> None: ...


def json_schema_for(schema: type[BaseModel]) -> dict[str, Any]:
    """The schema the reply is constrained to, in the form the API accepts."""
    if SDK_SCHEMA_TRANSFORM is not None:
        return SDK_SCHEMA_TRANSFORM(schema)
    return schema.model_json_schema()


def parse_payload(
    schema: type[M], text: str, *, use: TokenUse | None = None, model: str | None = None
) -> M:
    """Validate a raw reply into the model the caller asked for.

    Invalid JSON and a schema violation come back the same way, because the
    client treats them the same way: one retry, then an error the caller can
    fall back from. `use` travels on the error so the attempt that failed is
    still billed: the provider charged for it either way.
    """
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        raise LlmParseError(validation_detail(exc), use=use, model=model) from exc


def system_blocks(request: Request[Any]) -> list[dict[str, Any]]:
    """The fixed system prefix, with one prompt-cache breakpoint at its end.

    Everything in `request.system` comes from the codebase or a playbook, so
    the whole prefix is identical call to call and a run of a thousand volume
    calls pays for it once. A prefix shorter than the model's minimum (about a
    thousand tokens) silently will not cache; the breakpoint costs nothing
    then, and `cache_read_tokens` in the audit record is how you find out.
    """
    blocks = [{"type": "text", "text": text} for text in request.system]
    if blocks:
        blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
    return blocks


def wire_messages(request: Request[Any]) -> list[dict[str, str]]:
    """The turns, as both providers want them."""
    return [{"role": message.role, "content": message.content} for message in request.messages]


@dataclass(slots=True)
class MockBackend:
    """Replies from `tests/fixtures/llm/`, or from a script a test queued.

    `script` exists for the paths a fixture file cannot express: a reply that
    does not parse, then one that does, is how the retry is tested. Scripted
    replies are consumed in order and the fixture answers once they run out.
    """

    store: FixtureStore = field(default_factory=FixtureStore)
    model: str = "mock"
    scripted: dict[str, list[dict[str, Any] | str]] = field(default_factory=dict)
    #: Every request this backend was asked, for tests that check prompt shape.
    seen: list[Request[Any]] = field(default_factory=list)

    def script(self, purpose: str, *replies: dict[str, Any] | str) -> None:
        """Queue replies for one call site. A `str` is sent as raw text."""
        self.scripted.setdefault(purpose, []).extend(replies)

    async def send(self, request: Request[M]) -> Reply[M]:
        self.seen.append(request)
        queue = self.scripted.get(request.purpose)
        raw = queue.pop(0) if queue else self.store.load(request.purpose, request.variant)
        text = raw if isinstance(raw, str) else orjson.dumps(raw).decode()
        use = _mock_use(request, text)
        value = parse_payload(request.schema, text, use=use, model=self.model)
        return Reply(value=value, use=use, model=self.model)

    async def aclose(self) -> None:
        return None


class OllamaBackend:
    """A local model over Ollama, for volume calls and the offline demo.

    Ollama constrains the reply with the Pydantic schema as it comes, without
    the API's strict-schema transform: a local model is laxer about what it
    will accept and the same `parse_payload` catches whatever it gets wrong.
    """

    def __init__(
        self,
        model: str | None = None,
        *,
        client: Any | None = None,
        host: str | None = None,
    ) -> None:
        self.name = model or _ollama_model()
        #: What the audit record and the cache key see. A local answer must
        #: never be served back to a run that asked for the real model.
        self.model = f"ollama:{self.name}"
        self._client = client or ollama.AsyncClient(host=host)

    async def send(self, request: Request[M]) -> Reply[M]:
        messages: list[dict[str, str]] = []
        if request.system:
            messages.append({"role": "system", "content": "\n\n".join(request.system)})
        messages.extend(wire_messages(request))
        try:
            response = await self._client.chat(
                model=self.name,
                messages=messages,
                format=request.schema.model_json_schema(),
                options={"num_predict": request.max_tokens},
            )
        except (ollama.RequestError, ollama.ResponseError, OSError) as exc:
            raise LlmUnavailable(
                f"Ollama at {getattr(self._client, 'host', 'the configured host')} "
                f"could not answer with {self.name}: {exc}"
            ) from exc
        text = getattr(getattr(response, "message", None), "content", None) or ""
        use = TokenUse(
            input_tokens=int(getattr(response, "prompt_eval_count", 0) or 0),
            output_tokens=int(getattr(response, "eval_count", 0) or 0),
        )
        value = parse_payload(request.schema, text, use=use, model=self.model)
        return Reply(value=value, use=use, model=self.model)

    async def aclose(self) -> None:
        closer = getattr(self._client, "close", None)
        if closer is not None:
            result = closer()
            if hasattr(result, "__await__"):
                await result


class ClaudeBackend:
    """The real API, with structured output and prompt caching.

    `messages.create` with `output_config.format` rather than the SDK's
    `messages.parse`, so the raw reply and its token counts are in hand
    whatever the reply turns out to be - see `SDK_SCHEMA_TRANSFORM`.
    """

    def __init__(
        self,
        model: str,
        *,
        effort: str = "medium",
        client: Any | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key)

    async def send(self, request: Request[M]) -> Reply[M]:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "messages": wire_messages(request),
            "output_config": {
                "effort": self.effort,
                "format": {
                    "type": "json_schema",
                    "schema": json_schema_for(request.schema),
                },
            },
        }
        blocks = system_blocks(request)
        if blocks:
            payload["system"] = blocks
        try:
            response = await self._client.messages.create(**payload)
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            raise LlmUnavailable(f"the Claude API could not be reached: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code == 429 or exc.status_code >= 500:
                raise LlmUnavailable(
                    f"the Claude API returned {exc.status_code}: {exc.message}"
                ) from exc
            raise LlmError(
                f"the Claude API refused the request ({exc.status_code}): {exc.message}"
            ) from exc

        use = _claude_use(response)
        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            raise LlmError(f"{self.model} declined the request ({category})")
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        if stop == "max_tokens":
            raise LlmParseError(
                f"the reply was cut off at max_tokens={request.max_tokens} "
                "before the JSON object closed",
                use=use,
                model=self.model,
            )
        value = parse_payload(request.schema, text, use=use, model=self.model)
        return Reply(value=value, use=use, model=self.model)

    async def aclose(self) -> None:
        closer = getattr(self._client, "close", None)
        if closer is not None:
            await closer()


def backend_name(mode: Mode) -> str:
    """The backend class name a mode uses, for error messages."""
    return {
        Mode.MOCK: MockBackend.__name__,
        Mode.OLLAMA: OllamaBackend.__name__,
        Mode.CLAUDE: ClaudeBackend.__name__,
    }[mode]


def _ollama_model() -> str:
    return os.environ.get(ENV_OLLAMA_MODEL, "").strip() or DEFAULT_OLLAMA_MODEL


def _mock_use(request: Request[Any], text: str) -> TokenUse:
    prompt = sum(len(block) for block in request.system) + sum(
        len(message.content) for message in request.messages
    )
    return TokenUse(
        input_tokens=prompt // MOCK_CHARS_PER_TOKEN,
        output_tokens=len(text) // MOCK_CHARS_PER_TOKEN,
    )


def _claude_use(response: Any) -> TokenUse:
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUse()
    return TokenUse(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    )
