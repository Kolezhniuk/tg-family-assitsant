from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType

OUTBOX_STATUSES = ("queued", "in_flight", "delivered", "retrying", "failed")

EPISODE_KIND_CHECKIN = "checkin"
EPISODE_KIND_DOSE = "dose"
EPISODE_KIND_CLARIFY = "clarify"
EPISODE_KIND_ESCALATION = "escalation"

_LOCAL_DAY_RE = r"\d{4}-\d{2}-\d{2}"
_EPISODE_PATTERNS = (
    (EPISODE_KIND_CHECKIN, re.compile(rf"^checkin:(?P<local_day>{_LOCAL_DAY_RE})$")),
    (EPISODE_KIND_DOSE, re.compile(rf"^dose:(?P<dose_id>[^:]+):(?P<local_day>{_LOCAL_DAY_RE})$")),
    (EPISODE_KIND_CLARIFY, re.compile(r"^clarify:(?P<source_message_id>\d+)$")),
    (EPISODE_KIND_ESCALATION, re.compile(r"^escalation:(?P<action_key>.+)$")),
)


@dataclass(frozen=True)
class UpdateEnvelope:
    update_id: int
    chat_id: str
    chat_type: str
    sender_id: str
    message_id: int
    reply_to_message_id: int | None
    received_at_utc: datetime
    text: str


@dataclass(frozen=True)
class EventRecord:
    id: int
    ts_utc: datetime
    local_day: date
    kind: str
    episode_id: str | None
    subject: str | None
    actor_id: str | None
    source_update_id: int | None
    payload: MappingProxyType


@dataclass(frozen=True)
class OutboxRow:
    id: int
    action_key: str
    episode_id: str | None
    priority: int
    chat_id: str
    text: str
    status: str
    attempts: int
    available_at_utc: datetime
    lease_until_utc: datetime | None
    last_error: str | None
    receipt_id: str | None
    created_at_utc: datetime
    updated_at_utc: datetime


@dataclass(frozen=True)
class UpdateOutcome:
    replay: bool
    result: dict


@dataclass(frozen=True)
class ConfigSourceFile:
    name: str
    sha256: str


@dataclass(frozen=True)
class ConfigSnapshot:
    fingerprint: str
    files: tuple[ConfigSourceFile, ...]
    stored_at_utc: datetime
    raw_contents: MappingProxyType


def checkin_episode_id(local_day: date) -> str:
    return f"checkin:{local_day.isoformat()}"


def dose_episode_id(dose_id: str, local_day: date) -> str:
    if ":" in dose_id:
        raise ValueError(f"dose id must not contain ':': {dose_id!r}")
    return f"dose:{dose_id}:{local_day.isoformat()}"


def clarify_episode_id(source_message_id: int) -> str:
    return f"clarify:{source_message_id}"


def escalation_episode_id(action_key: str) -> str:
    if not action_key:
        raise ValueError("action_key must not be empty")
    return f"escalation:{action_key}"


def parse_episode_id(value: str) -> tuple[str, dict[str, str]]:
    for kind, pattern in _EPISODE_PATTERNS:
        match = pattern.match(value)
        if match:
            return kind, match.groupdict()
    raise ValueError(f"not a well-formed episode id: {value!r}")


def validate_episode_id(value: str) -> None:
    parse_episode_id(value)


def make_action_key(episode_id: str, step: str) -> str:
    if not step:
        raise ValueError("step must not be empty")
    return f"{episode_id}::{step}"
