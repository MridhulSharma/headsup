"""The disk cache: the same prompt is paid for once, and nothing else is.

Two properties matter. The key has to cover everything that could change the
answer - model, prompts, turns, schema - so a replay of S1 after a prompt edit
asks again instead of serving the old answer. And a damaged or outdated entry
has to read as a miss: a cache is an optimisation and must never be able to
fail a run.
"""

from __future__ import annotations

from fixtures.llm_calls import FLOOD_PAYLOAD, FloodFields, FusionRationale
from headsup.llm import Message, Request, ResponseCache, Tier, fingerprint
from headsup.llm.cache import CACHE_VERSION


def request(**kwargs) -> Request:
    defaults = {
        "purpose": "sentinel_flood_fields",
        "tier": Tier.VOLUME,
        "schema": FloodFields,
        "system": ("fixed rules", "playbook: flood"),
        "messages": (Message("user", "the alert text"),),
    }
    return Request(**{**defaults, **kwargs})


# --- the key ----------------------------------------------------------------


def test_the_same_request_fingerprints_the_same_way_twice() -> None:
    assert fingerprint("claude-haiku-5-5", request()) == fingerprint("claude-haiku-5-5", request())


def test_a_different_model_is_a_different_entry() -> None:
    """A local answer must never be served to a run that asked for the API."""
    assert fingerprint("claude-haiku-5-5", request()) != fingerprint(
        "ollama:llama3.1:8b", request()
    )


def test_editing_a_prompt_or_a_turn_invalidates_the_entry() -> None:
    base = fingerprint("claude-haiku-5-5", request())
    assert base != fingerprint("claude-haiku-5-5", request(system=("fixed rules.",)))
    assert base != fingerprint(
        "claude-haiku-5-5", request(messages=(Message("user", "other text"),))
    )


def test_changing_the_contract_invalidates_the_entry() -> None:
    """The schema is in the key, so a reply can never be stale against it."""
    assert fingerprint("claude-haiku-5-5", request()) != fingerprint(
        "claude-haiku-5-5", request(schema=FusionRationale)
    )


def test_two_call_sites_sending_the_same_text_are_still_different_questions() -> None:
    assert fingerprint("claude-haiku-5-5", request()) != fingerprint(
        "claude-haiku-5-5", request(purpose="fusion_rationale")
    )


# --- the files --------------------------------------------------------------


def test_a_stored_reply_comes_back(tmp_path) -> None:
    cache = ResponseCache(tmp_path)
    key = cache.key("claude-haiku-5-5", request())
    assert cache.get(key) is None
    cache.put(key, FLOOD_PAYLOAD, model="claude-haiku-5-5", purpose="sentinel_flood_fields")
    assert cache.get(key) == FLOOD_PAYLOAD
    assert cache.hits == 1
    assert cache.misses == 1


def test_entries_are_sharded_so_one_directory_does_not_hold_the_whole_run(
    tmp_path,
) -> None:
    cache = ResponseCache(tmp_path)
    key = cache.key("claude-haiku-5-5", request())
    assert cache.path_for(key).parent.name == key[:2]
    cache.put(key, FLOOD_PAYLOAD, model="claude-haiku-5-5", purpose="p")
    assert cache.path_for(key).is_file()


def test_a_half_written_entry_reads_as_a_miss(tmp_path) -> None:
    """Ctrl+C during a replay must not poison the next run."""
    cache = ResponseCache(tmp_path)
    key = cache.key("claude-haiku-5-5", request())
    path = cache.path_for(key)
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 1, "payload": {"waterway"', encoding="utf-8")
    assert cache.get(key) is None


def test_an_entry_from_an_older_build_reads_as_a_miss(tmp_path) -> None:
    cache = ResponseCache(tmp_path)
    key = cache.key("claude-haiku-5-5", request())
    path = cache.path_for(key)
    path.parent.mkdir(parents=True)
    path.write_text(f'{{"version": {CACHE_VERSION - 1}, "payload": {{}}}}', encoding="utf-8")
    assert cache.get(key) is None


def test_a_disabled_cache_never_reads_and_never_writes(tmp_path) -> None:
    """How mock mode runs: fixture answers stay out of the real cache directory."""
    cache = ResponseCache(tmp_path, enabled=False)
    key = cache.key("mock", request())
    cache.put(key, FLOOD_PAYLOAD, model="mock", purpose="sentinel_flood_fields")
    assert cache.get(key) is None
    assert not cache.path_for(key).exists()


def test_clear_removes_every_entry(tmp_path) -> None:
    cache = ResponseCache(tmp_path)
    for purpose in ("sentinel_flood_fields", "fusion_rationale"):
        cache.put(
            cache.key("claude-haiku-5-5", request(purpose=purpose)),
            FLOOD_PAYLOAD,
            model="claude-haiku-5-5",
            purpose=purpose,
        )
    assert cache.clear() == 2
    assert cache.clear() == 0
