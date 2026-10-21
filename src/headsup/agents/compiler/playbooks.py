"""Playbook loading: the only place the per-hazard-class caps come from.

A playbook is a YAML file in `playbooks/`, one per `HazardClass`, that says
what an advisory of that class may ask for and how far it may reach. The
compiler reads nothing else: no agent, prompt or feed can widen a cap, because
widening one means editing a file in this directory and committing it.

    playbooks = PlaybookSet.load()          # playbooks/ at the repo root
    book = playbooks.require(HazardClass.FLOOD)
    book.allows(ActionType.NO_ENTRY)        # True
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, ValidationError, model_validator

from headsup.contracts import ActionType, FreeText, HazardClass, HeadsUpModel, Identifier, Severity

PROJECT_ROOT = Path(__file__).resolve().parents[4]

#: Where `PlaybookSet.load()` looks when the caller names no directory.
DEFAULT_PLAYBOOK_DIR = PROJECT_ROOT / "playbooks"

#: Overrides the directory above, for a scenario that ships its own caps.
ENV_PLAYBOOK_DIR = "HU_PLAYBOOK_DIR"

PLAYBOOK_GLOB = "*.yaml"


class PlaybookError(RuntimeError):
    """A playbook file is missing, unreadable, or does not match the contract."""


class Playbook(HeadsUpModel):
    """The caps and the allowed actions for one hazard class."""

    hazard_class: HazardClass = Field(alias="class")
    policy_rule: Identifier
    description: FreeText
    allowed_actions: list[ActionType] = Field(min_length=1)
    max_area_km2: float = Field(gt=0.0)
    max_lifetime_minutes: float = Field(gt=0.0)
    #: Above this area an advisory needs a human approval token, whatever its
    #: severity. Always at or below `max_area_km2`.
    two_key_area_km2: float = Field(gt=0.0)
    #: At or above this severity an advisory needs a human approval token.
    two_key_severity: Severity = 4
    #: Per-action `policy_rule` ids; the class-level `policy_rule` is the default.
    action_rules: dict[ActionType, Identifier] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _caps_are_consistent(self) -> Playbook:
        if len(set(self.allowed_actions)) != len(self.allowed_actions):
            raise ValueError("allowed_actions lists the same action twice")
        if self.two_key_area_km2 > self.max_area_km2:
            raise ValueError(
                f"two_key_area_km2 {self.two_key_area_km2} is above "
                f"max_area_km2 {self.max_area_km2}, so the two-key rule could never fire"
            )
        unknown = [
            action.value for action in self.action_rules if action not in set(self.allowed_actions)
        ]
        if unknown:
            raise ValueError(f"action_rules names actions this class does not allow: {unknown}")
        return self

    def allows(self, action: ActionType) -> bool:
        """True if this class may ask a fleet to do `action`."""
        return action in self.allowed_actions

    def rule_for(self, action: ActionType) -> str:
        """The `policy_rule` id an advisory with this action is stamped with."""
        return self.action_rules.get(action, self.policy_rule)

    def needs_two_keys(self, severity: int, area_km2: float) -> bool:
        """True if this advisory needs a human approval token to publish."""
        return severity >= self.two_key_severity or area_km2 > self.two_key_area_km2

    @property
    def action_names(self) -> tuple[str, ...]:
        return tuple(action.value for action in self.allowed_actions)


class PlaybookSet:
    """Every playbook on disk, indexed by hazard class."""

    def __init__(
        self, books: Mapping[HazardClass, Playbook], *, source: Path | None = None
    ) -> None:
        self._books = dict(books)
        self.source = source

    @classmethod
    def load(cls, directory: Path | str | None = None) -> PlaybookSet:
        """Read every `*.yaml` in `directory` (default `playbooks/`)."""
        target = Path(directory) if directory is not None else _default_dir()
        if not target.is_dir():
            raise PlaybookError(f"no playbook directory at {target}")
        books: dict[HazardClass, Playbook] = {}
        for path in sorted(target.glob(PLAYBOOK_GLOB)):
            book = _read(path)
            previous = books.get(book.hazard_class)
            if previous is not None:
                raise PlaybookError(
                    f"{path.name}: {book.hazard_class.value} already has a playbook; "
                    "one file per hazard class"
                )
            books[book.hazard_class] = book
        if not books:
            raise PlaybookError(f"{target} has no {PLAYBOOK_GLOB} playbooks")
        return cls(books, source=target)

    def get(self, hazard_class: HazardClass) -> Playbook | None:
        """The playbook for a class, or None if this set has none."""
        return self._books.get(hazard_class)

    def require(self, hazard_class: HazardClass) -> Playbook:
        """The playbook for a class, or raise PlaybookError."""
        book = self._books.get(hazard_class)
        if book is None:
            raise PlaybookError(
                f"no playbook for {hazard_class.value} in {self.source or '<memory>'}"
            )
        return book

    @property
    def classes(self) -> tuple[HazardClass, ...]:
        return tuple(self._books)

    @property
    def missing(self) -> tuple[HazardClass, ...]:
        """Hazard classes with no playbook: nothing may be published for these."""
        return tuple(item for item in HazardClass if item not in self._books)

    def __iter__(self) -> Iterator[Playbook]:
        return iter(self._books.values())

    def __len__(self) -> int:
        return len(self._books)

    def __repr__(self) -> str:
        return f"PlaybookSet({len(self._books)} classes from {self.source or '<memory>'})"


def _default_dir() -> Path:
    override = os.environ.get(ENV_PLAYBOOK_DIR, "").strip()
    return Path(override) if override else DEFAULT_PLAYBOOK_DIR


def _read(path: Path) -> Playbook:
    """One YAML file to one validated Playbook, with the file in every error."""
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PlaybookError(f"{path.name}: cannot read as YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise PlaybookError(f"{path.name}: expected a YAML mapping, got {type(raw).__name__}")
    try:
        return Playbook.model_validate(raw)
    except ValidationError as exc:
        raise PlaybookError(f"{path.name}: {exc.error_count()} problem(s): {exc}") from exc


_cache: dict[Path, PlaybookSet] = {}


def default_playbooks() -> PlaybookSet:
    """The process-wide playbook set, read once per directory.

    Every agent that needs the caps calls this, so one run cannot end up with
    two different sets of limits in memory.
    """
    target = _default_dir().resolve()
    cached = _cache.get(target)
    if cached is None:
        cached = PlaybookSet.load(target)
        _cache[target] = cached
    return cached


def clear_cache() -> None:
    """Forget the cached sets; tests that write their own playbooks need this."""
    _cache.clear()
