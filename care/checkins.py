from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from care import triage
from care.clock import Clock, load_zone, local_datetime_for, make_window, to_local
from care.config import CHECKIN_WINDOW_MINUTES, Config
from care.delivery import PRIORITY_ESCALATION, PRIORITY_ROUTINE
from care.models import UpdateEnvelope, checkin_episode_id, make_action_key
from care.redaction import redact_and_bound
from care.store import Store, WriteTxn

STEP_PROMPT = "prompt"
STEP_NUDGE = "nudge"
STEP_ESCALATION = "escalation"
STEP_OPERATIONAL_ALERT = "operational_alert"
STEP_STAND_DOWN = "stand_down"

EVENT_PROMPT_ENQUEUED = "checkin_prompt_enqueued"
EVENT_OPERATIONAL_ALERT = "checkin_operational_alert"
EVENT_NUDGE_ENQUEUED = "checkin_nudge_enqueued"
EVENT_ESCALATION_ENQUEUED = "checkin_escalation_enqueued"
EVENT_REPLY_CLASSIFIED = "checkin_reply_classified"
EVENT_STAND_DOWN = "checkin_stand_down_enqueued"
EVENT_CONCERN_ESCALATION = "checkin_concern_escalation_enqueued"
EVENT_FAMILY_ACK = "checkin_family_ack_recorded"

CLASSIFICATION_CLEAR = "clear"
CLASSIFICATION_CONCERNING = "concerning"

ACK_WITHIN_WINDOW = "within_window"
ACK_LATE = "late"
ACK_WINDOW_MINUTES = 30

REPLY_KIND_PARENT = "parent_reply"
REPLY_KIND_FAMILY_ACK = "family_ack"
REPLY_KIND_UNRELATED = "unrelated"

_CHECKIN_EPISODE_PREFIX = "checkin:"


@dataclass(frozen=True)
class CheckinTickResult:
    local_day: date
    episode_id: str
    actions_taken: tuple[str, ...]


@dataclass(frozen=True)
class ReplyOutcome:
    kind: str
    episode_id: str | None
    classification: str | None


def _primary_language(config: Config) -> str:
    return config.roster.languages[0]


def _message(config: Config, key: str, **kwargs: str) -> str:
    template = config.messages[_primary_language(config)].messages[key]
    return template.format(**kwargs)


def _is_concerning(config: Config, text: str) -> bool:
    return any(triage.find_matches(text, catalogue) for catalogue in config.tripwires.values())


def _in_quiet_hours(now_local: datetime, config: Config) -> bool:
    return config.roster.quiet_hours.contains(now_local.time())


def _enqueue(
    txn: WriteTxn,
    *,
    episode_id: str,
    step: str,
    chat_id: str,
    text: str,
    priority: int,
    now_utc: datetime,
) -> None:
    txn.enqueue_outbox(
        action_key=make_action_key(episode_id, step),
        chat_id=chat_id,
        text=text,
        priority=priority,
        available_at_utc=now_utc,
        created_at_utc=now_utc,
        episode_id=episode_id,
    )


def run_checkin_tick(store: Store, config: Config, clock: Clock) -> CheckinTickResult:
    roster = config.roster
    tz = load_zone(roster.timezone)
    now_utc = clock.now_utc()
    now_local = to_local(now_utc, tz)
    local_day = now_local.date()
    episode_id = checkin_episode_id(local_day)

    actions: list[str] = []

    with store.transaction() as txn:
        prompt_row = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_PROMPT))
        nudge_row = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_NUDGE))
        escalation_row = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_ESCALATION))
        alert_row = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_OPERATIONAL_ALERT))

        prompt_window = make_window(
            local_datetime_for(local_day, roster.checkin.time, tz),
            timedelta(minutes=CHECKIN_WINDOW_MINUTES),
        )

        if prompt_row is None:
            if prompt_window.contains(now_utc) and not _in_quiet_hours(now_local, config):
                _enqueue(
                    txn,
                    episode_id=episode_id,
                    step=STEP_PROMPT,
                    chat_id=roster.parent.chat_id,
                    text=_message(config, "checkin_prompt"),
                    priority=PRIORITY_ROUTINE,
                    now_utc=now_utc,
                )
                txn.append_event(
                    ts_utc=now_utc,
                    local_day=local_day,
                    kind=EVENT_PROMPT_ENQUEUED,
                    episode_id=episode_id,
                    payload={},
                )
                actions.append(STEP_PROMPT)
            elif now_utc >= prompt_window.closes_at_utc and alert_row is None:
                _enqueue(
                    txn,
                    episode_id=episode_id,
                    step=STEP_OPERATIONAL_ALERT,
                    chat_id=roster.group.chat_id,
                    text=_message(config, "checkin_delivery_failed", parent_name=roster.parent.name),
                    priority=PRIORITY_ESCALATION,
                    now_utc=now_utc,
                )
                txn.append_event(
                    ts_utc=now_utc,
                    local_day=local_day,
                    kind=EVENT_OPERATIONAL_ALERT,
                    episode_id=episode_id,
                    payload={"reason": "prompt_not_attempted_in_window"},
                )
                actions.append(STEP_OPERATIONAL_ALERT)
        elif (
            prompt_row.status != "delivered"
            and now_utc >= prompt_window.closes_at_utc
            and alert_row is None
        ):
            _enqueue(
                txn,
                episode_id=episode_id,
                step=STEP_OPERATIONAL_ALERT,
                chat_id=roster.group.chat_id,
                text=_message(config, "checkin_delivery_failed", parent_name=roster.parent.name),
                priority=PRIORITY_ESCALATION,
                now_utc=now_utc,
            )
            txn.append_event(
                ts_utc=now_utc,
                local_day=local_day,
                kind=EVENT_OPERATIONAL_ALERT,
                episode_id=episode_id,
                payload={"reason": "prompt_delivery_failed"},
            )
            actions.append(STEP_OPERATIONAL_ALERT)

        if prompt_row is not None and prompt_row.status == "delivered":
            events = store.list_events(episode_id=episode_id)
            replied = any(event.kind == EVENT_REPLY_CLASSIFIED for event in events)
            delivered_at = prompt_row.updated_at_utc
            nudge_open_at = delivered_at + timedelta(minutes=roster.checkin.nudge_after_minutes)
            escalate_open_at = delivered_at + timedelta(minutes=roster.checkin.escalate_after_minutes)
            day_end_utc = local_datetime_for(local_day + timedelta(days=1), time(0, 0), tz)

            if (
                not replied
                and nudge_row is None
                and escalation_row is None
                and nudge_open_at <= now_utc < escalate_open_at
                and not _in_quiet_hours(now_local, config)
            ):
                _enqueue(
                    txn,
                    episode_id=episode_id,
                    step=STEP_NUDGE,
                    chat_id=roster.parent.chat_id,
                    text=_message(config, "checkin_nudge"),
                    priority=PRIORITY_ROUTINE,
                    now_utc=now_utc,
                )
                txn.append_event(
                    ts_utc=now_utc,
                    local_day=local_day,
                    kind=EVENT_NUDGE_ENQUEUED,
                    episode_id=episode_id,
                    payload={},
                )
                actions.append(STEP_NUDGE)

            if (
                not replied
                and escalation_row is None
                and escalate_open_at <= now_utc < day_end_utc
            ):
                _enqueue(
                    txn,
                    episode_id=episode_id,
                    step=STEP_ESCALATION,
                    chat_id=roster.group.chat_id,
                    text=_message(config, "escalation_silence", parent_name=roster.parent.name),
                    priority=PRIORITY_ESCALATION,
                    now_utc=now_utc,
                )
                txn.append_event(
                    ts_utc=now_utc,
                    local_day=local_day,
                    kind=EVENT_ESCALATION_ENQUEUED,
                    episode_id=episode_id,
                    payload={},
                )
                actions.append(STEP_ESCALATION)

    return CheckinTickResult(local_day=local_day, episode_id=episode_id, actions_taken=tuple(actions))


def _checkin_outbox_rows(store: Store) -> list:
    return [row for row in store.list_outbox() if row.episode_id and row.episode_id.startswith(_CHECKIN_EPISODE_PREFIX)]


def _open_checkin_episode_ids(store: Store) -> set[str]:
    checkin_outbox = _checkin_outbox_rows(store)
    delivered_prompt_episodes = {
        row.episode_id
        for row in checkin_outbox
        if row.action_key.endswith(f"::{STEP_PROMPT}") and row.status == "delivered"
    }
    resolved = {
        event.episode_id
        for event in store.list_events()
        if event.kind == EVENT_REPLY_CLASSIFIED
        and event.payload.get("classification") == CLASSIFICATION_CLEAR
        and event.episode_id
    }
    return delivered_prompt_episodes - resolved


def _correlate_checkin_episode(store: Store, envelope: UpdateEnvelope) -> str | None:
    checkin_outbox = _checkin_outbox_rows(store)
    if envelope.reply_to_message_id is not None:
        target = str(envelope.reply_to_message_id)
        matches = {row.episode_id for row in checkin_outbox if row.receipt_id == target}
        if len(matches) == 1:
            return next(iter(matches))
        return None

    open_episodes = _open_checkin_episode_ids(store)
    if len(open_episodes) == 1:
        return next(iter(open_episodes))
    return None


def _is_escalation_step(action_key: str) -> bool:
    step = action_key.rsplit("::", 1)[1]
    return step == STEP_ESCALATION or step.startswith("concern:")


def _process_parent_reply(
    store: Store,
    txn: WriteTxn,
    config: Config,
    envelope: UpdateEnvelope,
    now_utc: datetime,
    local_day: date,
) -> ReplyOutcome:
    episode_id = _correlate_checkin_episode(store, envelope)
    if episode_id is None:
        return ReplyOutcome(kind=REPLY_KIND_UNRELATED, episode_id=None, classification=None)

    classification = CLASSIFICATION_CONCERNING if _is_concerning(config, envelope.text) else CLASSIFICATION_CLEAR
    txn.append_event(
        ts_utc=now_utc,
        local_day=local_day,
        kind=EVENT_REPLY_CLASSIFIED,
        episode_id=episode_id,
        actor_id=envelope.sender_id,
        source_update_id=envelope.update_id,
        payload={"classification": classification, "excerpt": redact_and_bound(envelope.text)},
    )

    if classification == CLASSIFICATION_CONCERNING:
        _enqueue(
            txn,
            episode_id=episode_id,
            step=f"concern:{envelope.message_id}",
            chat_id=config.roster.group.chat_id,
            text=_message(config, "escalation_concern", parent_name=config.roster.parent.name),
            priority=PRIORITY_ESCALATION,
            now_utc=now_utc,
        )
        txn.append_event(
            ts_utc=now_utc,
            local_day=local_day,
            kind=EVENT_CONCERN_ESCALATION,
            episode_id=episode_id,
            source_update_id=envelope.update_id,
            payload={},
        )
    else:
        escalation_row = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_ESCALATION))
        if escalation_row is not None:
            _enqueue(
                txn,
                episode_id=episode_id,
                step=STEP_STAND_DOWN,
                chat_id=config.roster.group.chat_id,
                text=_message(config, "stand_down"),
                priority=PRIORITY_ROUTINE,
                now_utc=now_utc,
            )
            txn.append_event(
                ts_utc=now_utc,
                local_day=local_day,
                kind=EVENT_STAND_DOWN,
                episode_id=episode_id,
                source_update_id=envelope.update_id,
                payload={},
            )

    return ReplyOutcome(kind=REPLY_KIND_PARENT, episode_id=episode_id, classification=classification)


def _process_family_reply(
    store: Store,
    txn: WriteTxn,
    config: Config,
    envelope: UpdateEnvelope,
    now_utc: datetime,
    local_day: date,
) -> ReplyOutcome:
    if envelope.reply_to_message_id is None:
        return ReplyOutcome(kind=REPLY_KIND_UNRELATED, episode_id=None, classification=None)

    target = str(envelope.reply_to_message_id)
    matches = [
        row
        for row in _checkin_outbox_rows(store)
        if row.receipt_id == target and _is_escalation_step(row.action_key)
    ]
    if len(matches) != 1:
        return ReplyOutcome(kind=REPLY_KIND_UNRELATED, episode_id=None, classification=None)

    row = matches[0]
    within_window = (now_utc - row.updated_at_utc) <= timedelta(minutes=ACK_WINDOW_MINUTES)
    classification = ACK_WITHIN_WINDOW if within_window else ACK_LATE
    txn.append_event(
        ts_utc=now_utc,
        local_day=local_day,
        kind=EVENT_FAMILY_ACK,
        episode_id=row.episode_id,
        actor_id=envelope.sender_id,
        source_update_id=envelope.update_id,
        payload={"classification": classification, "action_key": row.action_key},
    )
    return ReplyOutcome(kind=REPLY_KIND_FAMILY_ACK, episode_id=row.episode_id, classification=classification)


def process_reply(
    store: Store, txn: WriteTxn, config: Config, clock: Clock, envelope: UpdateEnvelope
) -> ReplyOutcome:
    roster = config.roster
    now_utc = clock.now_utc()
    tz = load_zone(roster.timezone)
    local_day = to_local(now_utc, tz).date()

    if envelope.chat_id == roster.parent.chat_id:
        return _process_parent_reply(store, txn, config, envelope, now_utc, local_day)
    if envelope.chat_id == roster.group.chat_id:
        return _process_family_reply(store, txn, config, envelope, now_utc, local_day)
    return ReplyOutcome(kind=REPLY_KIND_UNRELATED, episode_id=None, classification=None)
