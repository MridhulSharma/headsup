"""The ACL rules the single-writer control is built from, without a Redis.

`tests/integration/test_compiler_acl.py` proves the server enforces these.
These tests prove the rules say what we think they say - which matters,
because Redis has no "deny one key" rule and a single wide pattern in the
wrong place would silently hand `advisories` back as writable.
"""

from __future__ import annotations

import pytest

from headsup.bus import ALL_STREAMS, SINGLE_WRITER_STREAMS, STREAM_ADVISORIES
from headsup.bus.acl import (
    APPEND_COMMANDS,
    COMMAND_RULES,
    CONSUMER_COMMANDS,
    DEFAULT_PASSWORD,
    ENV_PASSWORD,
    PROTECTED_STREAM,
    PROTECTED_WRITER,
    Role,
    key_rules,
    lock_default_args,
    password_for,
    selector_rules,
    setuser_args,
    url_for,
)

URL = "redis://localhost:6379/0"


# --- what the rules say -----------------------------------------------------


def test_the_protected_stream_is_the_advisories_stream() -> None:
    assert PROTECTED_STREAM == STREAM_ADVISORIES
    assert PROTECTED_WRITER == "compiler"
    assert SINGLE_WRITER_STREAMS[STREAM_ADVISORIES] == PROTECTED_WRITER


def test_the_compiler_may_write_every_stream() -> None:
    assert key_rules(Role.COMPILER) == ["~*"]


def test_the_agents_root_set_does_not_mention_advisories() -> None:
    """The protected stream is granted through a selector, nowhere else.

    One place describes what an agent may do with `advisories`, so there is
    one place to read and one place to get wrong.
    """
    rules = key_rules(Role.AGENT)
    assert not [rule for rule in rules if "advisories" in rule]


def test_the_agent_gets_advisories_through_one_selector() -> None:
    selectors = selector_rules(Role.AGENT)
    assert len(selectors) == 1
    selector = selectors[0]
    assert selector.startswith("(") and selector.endswith(")")
    assert "~advisories" in selector
    assert "~*:advisories" in selector


def test_the_selector_allows_reading_and_consuming() -> None:
    """XREADGROUP and XACK are write-flagged in Redis, so they must be named."""
    selector = selector_rules(Role.AGENT)[0]
    for command in ("+xread", "+xrange", "+xreadgroup", "+xack", "+xgroup", "+xpending"):
        assert command in selector


def test_the_selector_allows_nothing_that_mutates_the_log() -> None:
    selector = selector_rules(Role.AGENT)[0]
    for command in APPEND_COMMANDS:
        assert f"+{command}" not in selector


def test_the_consumer_allow_list_and_the_append_commands_are_disjoint() -> None:
    """The allow-list is the safe direction: a new stream command stays denied
    until someone adds it here on purpose."""
    allowed = {rule.removeprefix("+") for rule in CONSUMER_COMMANDS}
    assert allowed.isdisjoint(set(APPEND_COMMANDS))


def test_the_compiler_needs_no_selector() -> None:
    """It already has every key; a selector could only narrow that."""
    assert selector_rules(Role.COMPILER) == []


def test_the_agent_may_write_every_other_stream() -> None:
    rules = set(key_rules(Role.AGENT))
    for stream in ALL_STREAMS:
        if stream == STREAM_ADVISORIES:
            continue
        pattern = "~events.*" if stream.startswith("events.") else f"~{stream}"
        assert pattern in rules, stream


def test_no_rule_hands_advisories_back_as_writable() -> None:
    """A pattern like `~*` or `~*:*` alongside `%R~advisories` would undo it."""
    for role in (Role.AGENT,):
        for rule in key_rules(role):
            assert rule not in {"~*", "~*:*", "allkeys"}


def test_namespaced_streams_are_covered() -> None:
    """`Bus` prefixes a namespace for tests and parallel replays."""
    rules = set(key_rules(Role.AGENT))
    assert "~*:hazards" in rules
    assert "~*:events.*" in rules
    assert "~*:advisories" in selector_rules(Role.AGENT)[0]


# --- the SETUSER argument list ---------------------------------------------


@pytest.mark.parametrize("role", list(Role))
def test_setuser_resets_first_so_applying_twice_is_the_same(role: Role) -> None:
    args = setuser_args(role, "pw")
    assert args[0] == role.value
    assert args[1] == "reset"
    assert "on" in args
    assert ">pw" in args


@pytest.mark.parametrize("role", list(Role))
def test_neither_user_gets_administrative_commands(role: Role) -> None:
    args = setuser_args(role, "pw")
    assert "-@dangerous" in args
    assert args.index("+@all") < args.index("-@dangerous")
    for rule in COMMAND_RULES:
        assert rule in args


@pytest.mark.parametrize("role", list(Role))
def test_neither_user_gets_pubsub_channels(role: Role) -> None:
    assert "resetchannels" in setuser_args(role, "pw")


def test_the_selector_comes_after_the_root_rules() -> None:
    """Redis tries the root set first, then each selector."""
    args = setuser_args(Role.AGENT, "pw")
    assert args[-1] == selector_rules(Role.AGENT)[0]
    assert args.index("+@all") < len(args) - 1


def test_locking_the_default_user_leaves_it_unable_to_append() -> None:
    args = lock_default_args()
    assert args[0] == "default"
    assert "nopass" in args
    assert "~advisories" not in args
    assert "~*" not in args
    assert "~*:*" not in args
    # Same arrangement as the agent: consume it, never append to it.
    assert selector_rules(Role.AGENT)[0] in args


def test_locking_the_default_user_keeps_the_key_to_the_lock() -> None:
    """`default` keeps `+@all` so `ACL SETUSER` can undo this from redis-cli.

    The ACL lives in memory, so a `default` user without `@admin` would leave
    restarting Redis as the only way back.
    """
    args = lock_default_args()
    assert "+@all" in args
    assert "-@dangerous" not in args


# --- passwords and URLs -----------------------------------------------------


@pytest.mark.parametrize("role", list(Role))
def test_a_password_comes_from_the_environment_first(role: Role, monkeypatch) -> None:
    monkeypatch.setenv(ENV_PASSWORD[role], "from-the-env")
    assert password_for(role) == "from-the-env"


@pytest.mark.parametrize("role", list(Role))
def test_a_password_falls_back_to_the_development_default(role: Role, monkeypatch) -> None:
    monkeypatch.delenv(ENV_PASSWORD[role], raising=False)
    assert password_for(role) == DEFAULT_PASSWORD[role]


def test_a_url_is_untouched_when_no_password_is_configured(monkeypatch) -> None:
    """A dev box with no ACLs applied keeps connecting as `default`."""
    for role in Role:
        monkeypatch.delenv(ENV_PASSWORD[role], raising=False)
    assert url_for(Role.COMPILER, URL) == URL


def test_a_url_carries_the_role_and_its_password(monkeypatch) -> None:
    monkeypatch.setenv(ENV_PASSWORD[Role.COMPILER], "s3cret")
    assert url_for(Role.COMPILER, URL) == "redis://compiler:s3cret@localhost:6379/0"


def test_a_password_with_url_characters_is_quoted(monkeypatch) -> None:
    monkeypatch.setenv(ENV_PASSWORD[Role.AGENT], "p@ss/word:1")
    url = url_for(Role.AGENT, URL)
    assert "p%40ss%2Fword%3A1" in url
    assert url.endswith("@localhost:6379/0")


def test_a_role_may_be_named_by_string(monkeypatch) -> None:
    monkeypatch.setenv(ENV_PASSWORD[Role.AGENT], "pw")
    assert url_for("agent", URL).startswith("redis://agent:pw@")


def test_an_unknown_role_is_refused() -> None:
    with pytest.raises(ValueError):
        url_for("viewer", URL)
