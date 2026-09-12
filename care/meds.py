from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from care import triage
from care.clock import Clock, load_zone, local_datetime_for, make_window, to_local
from care.config import WEEKDAYS, Config, Dose
from care.delivery import PRIORITY_ESCALATION, PRIORITY_ROUTINE
from care.models import UpdateEnvelope, dose_episode_id, make_action_key, parse_episode_id
from care.redaction import redact_and_bound
from care.store import Store, WriteTxn

STEP_REMINDER = "reminder"
STEP_NUDGE = "nudge"
STEP_OPERATIONAL_ALERT = "operational_alert"

REMINDER_WINDOW_MINUTES = 30
NUDGE_AFTER_MINUTES = 45
CLOSE_AFTER_MINUTES = 120
ADHERENCE_COOLDOWN = timedelta(hours=24)

EVENT_REMINDER_ENQUEUED = "dose_reminder_enqueued"
EVENT_REMINDER_SUPPRESSED_QUIET = "dose_reminder_suppressed_quiet"
EVENT_OPERATIONAL_ALERT = "dose_operational_alert"
EVENT_NUDGE_ENQUEUED = "dose_nudge_enqueued"
EVENT_CLOSED_UNCONFIRMED = "dose_closed_unconfirmed"
EVENT_CONFIRMED = "dose_confirmed"
EVENT_REPLY_REJECTED = "dose_reply_rejected"
EVENT_ADHERENCE_NOTICE = "dose_adherence_notice_enqueued"

REPLY_KIND_CONFIRMED = "dose_confirmed"
REPLY_KIND_REJECTED = "dose_rejected"
REPLY_KIND_UNRELATED = "unrelated"

REJECT_REASON_UNKNOWN = "unknown"
REJECT_REASON_CLOSED = "closed"
REJECT_REASON_AMBIGUOUS = "ambiguous"
REJECT_REASON_UNAUTHORIZED = "unauthorized"
REJECT_REASON_UNDELIVERED = "undelivered"

_DOSE_EPISODE_PREFIX = "dose:"


@dataclass(frozen=True)
class MedsTickResult:
    local_day: date
    actions_taken: tuple[str, ...]


@dataclass(frozen=True)
class DoseReplyOutcome:
    kind: str
    episode_id: str | None
    dose_id: str | None
    reason: str | None


def _primary_language(config: Config) -> str:
    return config.roster.languages[0]


def _message(config: Config, key: str, **kwargs: str) -> str:
    template = config.messages[_primary_language(config)].messages[key]
    return template.format(**kwargs)


def _is_concerning(config: Config, text: str) -> bool:
    return any(triage.find_matches(text, catalogue) for catalogue in config.tripwires.values())


def _confirms_dose_any_language(config: Config, text: str) -> bool:
    languages = config.roster.languages
    if any(triage.is_negated(text, config.negatives[lang]) for lang in languages):
        return False
    return any(triage.is_affirmative(text, config.affirmatives[lang]) for lang in languages)


def _in_quiet_hours(now_local: datetime, config: Config) -> bool:
    return config.roster.quiet_hours.contains(now_local.time())


def _dose_applicable(dose: Dose, local_day: date) -> bool:
    return WEEKDAYS[local_day.weekday()] in dose.weekdays


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


def _dose_outbox_rows(store: Store) -> list:
    return [row for row in store.list_outbox() if row.episode_id and row.episode_id.startswith(_DOSE_EPISODE_PREFIX)]


def _dose_reminder_rows(store: Store) -> list:
    return [row for row in _dose_outbox_rows(store) if row.action_key.endswith(f"::{STEP_REMINDER}")]


def _delivered_dose_episode_ids(store: Store) -> set[str]:
    return {row.episode_id for row in _dose_reminder_rows(store) if row.status == "delivered"}


def _resolved_dose_episode_ids(store: Store) -> set[str]:
    kinds = (EVENT_CONFIRMED, EVENT_CLOSED_UNCONFIRMED)
    return {event.episode_id for event in store.list_events() if event.kind in kinds and event.episode_id}


def _open_dose_episode_ids(store: Store) -> set[str]:
    return _delivered_dose_episode_ids(store) - _resolved_dose_episode_ids(store)


def _pending_dose_episode_ids(store: Store) -> set[str]:
    pending = {row.episode_id for row in _dose_reminder_rows(store) if row.status != "delivered"}
    return pending - _resolved_dose_episode_ids(store)


def _episode_resolved(store: Store, episode_id: str) -> bool:
    kinds = (EVENT_CONFIRMED, EVENT_CLOSED_UNCONFIRMED)
    return any(event.kind in kinds for event in store.list_events(episode_id=episode_id))


def _reminder_suppressed(store: Store, episode_id: str) -> bool:
    return any(
        event.kind == EVENT_REMINDER_SUPPRESSED_QUIET for event in store.list_events(episode_id=episode_id)
    )


def _dose_id_from_episode(episode_id: str) -> str:
    _, groups = parse_episode_id(episode_id)
    return groups["dose_id"]


def _tick_dose(
    store: Store,
    txn: WriteTxn,
    config: Config,
    dose: Dose,
    local_day: date,
    now_utc: datetime,
    now_local: datetime,
) -> list[str]:
    roster = config.roster
    tz = load_zone(roster.timezone)
    episode_id = dose_episode_id(dose.id, local_day)

    actions: list[str] = []

    reminder_row = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_REMINDER))
    nudge_row = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_NUDGE))
    alert_row = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_OPERATIONAL_ALERT))
    suppressed = _reminder_suppressed(store, episode_id)

    reminder_window = make_window(
        local_datetime_for(local_day, dose.time, tz), timedelta(minutes=REMINDER_WINDOW_MINUTES)
    )

    if reminder_row is None and not suppressed:
        if reminder_window.contains(now_utc):
            if _in_quiet_hours(now_local, config):
                txn.append_event(
                    ts_utc=now_utc,
                    local_day=local_day,
                    kind=EVENT_REMINDER_SUPPRESSED_QUIET,
                    episode_id=episode_id,
                    payload={},
                )
                actions.append(f"reminder_suppressed:{dose.id}")
            else:
                _enqueue(
                    txn,
                    episode_id=episode_id,
                    step=STEP_REMINDER,
                    chat_id=roster.parent.chat_id,
                    text=_message(config, "dose_reminder", label=dose.label),
                    priority=PRIORITY_ROUTINE,
                    now_utc=now_utc,
                )
                txn.append_event(
                    ts_utc=now_utc,
                    local_day=local_day,
                    kind=EVENT_REMINDER_ENQUEUED,
                    episode_id=episode_id,
                    payload={},
                )
                actions.append(f"{STEP_REMINDER}:{dose.id}")
        elif now_utc >= reminder_window.closes_at_utc and alert_row is None:
            _enqueue(
                txn,
                episode_id=episode_id,
                step=STEP_OPERATIONAL_ALERT,
                chat_id=roster.group.chat_id,
                text=_message(config, "dose_delivery_failed", parent_name=roster.parent.name, label=dose.label),
                priority=PRIORITY_ESCALATION,
                now_utc=now_utc,
            )
            txn.append_event(
                ts_utc=now_utc,
                local_day=local_day,
                kind=EVENT_OPERATIONAL_ALERT,
                episode_id=episode_id,
                payload={"reason": "reminder_not_attempted_in_window"},
            )
            actions.append(f"{STEP_OPERATIONAL_ALERT}:{dose.id}")
    elif (
        reminder_row is not None
        and reminder_row.status != "delivered"
        and now_utc >= reminder_window.closes_at_utc
        and alert_row is None
    ):
        _enqueue(
            txn,
            episode_id=episode_id,
            step=STEP_OPERATIONAL_ALERT,
            chat_id=roster.group.chat_id,
            text=_message(config, "dose_delivery_failed", parent_name=roster.parent.name, label=dose.label),
            priority=PRIORITY_ESCALATION,
            now_utc=now_utc,
        )
        txn.append_event(
            ts_utc=now_utc,
            local_day=local_day,
            kind=EVENT_OPERATIONAL_ALERT,
            episode_id=episode_id,
            payload={"reason": "reminder_delivery_failed"},
        )
        actions.append(f"{STEP_OPERATIONAL_ALERT}:{dose.id}")

    if reminder_row is not None and reminder_row.status == "delivered":
        delivered_at = reminder_row.updated_at_utc
        resolved = _episode_resolved(store, episode_id)
        nudge_open_at = delivered_at + timedelta(minutes=NUDGE_AFTER_MINUTES)
        close_open_at = delivered_at + timedelta(minutes=CLOSE_AFTER_MINUTES)

        if not resolved and nudge_row is None and nudge_open_at <= now_utc < close_open_at:
            _enqueue(
                txn,
                episode_id=episode_id,
                step=STEP_NUDGE,
                chat_id=roster.parent.chat_id,
                text=_message(config, "dose_nudge", label=dose.label),
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
            actions.append(f"{STEP_NUDGE}:{dose.id}")

        if not resolved and now_utc >= close_open_at:
            txn.append_event(
                ts_utc=now_utc,
                local_day=local_day,
                kind=EVENT_CLOSED_UNCONFIRMED,
                episode_id=episode_id,
                payload={},
            )
            actions.append(f"closed_unconfirmed:{dose.id}")

    return actions


def _closed_unconfirmed_by_day(store: Store) -> dict[date, set[str]]:
    result: dict[date, set[str]] = {}
    for event in store.list_events():
        if event.kind == EVENT_CLOSED_UNCONFIRMED and event.episode_id:
            _, groups = parse_episode_id(event.episode_id)
            day = date.fromisoformat(groups["local_day"])
            result.setdefault(day, set()).add(groups["dose_id"])
    return result


def _adherence_trigger(store: Store, local_day: date) -> bool:
    by_day = _closed_unconfirmed_by_day(store)
    today = by_day.get(local_day, set())
    if len(today) >= 2:
        return True
    yesterday = by_day.get(local_day - timedelta(days=1), set())
    return bool(today & yesterday)


def _adherence_notice_allowed(store: Store, now_utc: datetime) -> bool:
    notices = [event for event in store.list_events() if event.kind == EVENT_ADHERENCE_NOTICE]
    if not notices:
        return True
    last_ts = max(event.ts_utc for event in notices)
    return (now_utc - last_ts) >= ADHERENCE_COOLDOWN


def _enqueue_adherence_notice(txn: WriteTxn, config: Config, now_utc: datetime, local_day: date) -> None:
    roster = config.roster
    txn.enqueue_outbox(
        action_key=f"adherence-notice::{now_utc.isoformat()}",
        chat_id=roster.group.chat_id,
        text=_message(config, "dose_adherence_notice", parent_name=roster.parent.name),
        priority=PRIORITY_ROUTINE,
        available_at_utc=now_utc,
        created_at_utc=now_utc,
        episode_id=None,
    )
    txn.append_event(
        ts_utc=now_utc,
        local_day=local_day,
        kind=EVENT_ADHERENCE_NOTICE,
        episode_id=None,
        payload={},
    )


def run_meds_tick(store: Store, config: Config, clock: Clock) -> MedsTickResult:
    tz = load_zone(config.roster.timezone)
    now_utc = clock.now_utc()
    now_local = to_local(now_utc, tz)
    local_day = now_local.date()

    actions: list[str] = []

    with store.transaction() as txn:
        for dose in config.doses:
            if _dose_applicable(dose, local_day):
                actions.extend(_tick_dose(store, txn, config, dose, local_day, now_utc, now_local))

        if _adherence_trigger(store, local_day) and _adherence_notice_allowed(store, now_utc):
            _enqueue_adherence_notice(txn, config, now_utc, local_day)
            actions.append("adherence_notice")

    return MedsTickResult(local_day=local_day, actions_taken=tuple(actions))


def _reject(
    txn: WriteTxn,
    envelope: UpdateEnvelope,
    now_utc: datetime,
    local_day: date,
    *,
    reason: str,
    episode_id: str | None,
) -> DoseReplyOutcome:
    excerpt = redact_and_bound(envelope.text)
    txn.append_event(
        ts_utc=now_utc,
        local_day=local_day,
        kind=EVENT_REPLY_REJECTED,
        episode_id=episode_id,
        actor_id=envelope.sender_id,
        source_update_id=envelope.update_id,
        payload={"reason": reason, "excerpt": excerpt},
    )
    dose_id = _dose_id_from_episode(episode_id) if episode_id is not None else None
    return DoseReplyOutcome(kind=REPLY_KIND_REJECTED, episode_id=episode_id, dose_id=dose_id, reason=reason)


def _confirm(
    txn: WriteTxn, envelope: UpdateEnvelope, now_utc: datetime, local_day: date, episode_id: str
) -> DoseReplyOutcome:
    txn.append_event(
        ts_utc=now_utc,
        local_day=local_day,
        kind=EVENT_CONFIRMED,
        episode_id=episode_id,
        actor_id=envelope.sender_id,
        source_update_id=envelope.update_id,
        payload={},
    )
    return DoseReplyOutcome(
        kind=REPLY_KIND_CONFIRMED, episode_id=episode_id, dose_id=_dose_id_from_episode(episode_id), reason=None
    )


def process_reply(
    store: Store, txn: WriteTxn, config: Config, clock: Clock, envelope: UpdateEnvelope
) -> DoseReplyOutcome:
    roster = config.roster
    now_utc = clock.now_utc()
    tz = load_zone(roster.timezone)
    local_day = to_local(now_utc, tz).date()

    if _is_concerning(config, envelope.text):
        return DoseReplyOutcome(kind=REPLY_KIND_UNRELATED, episode_id=None, dose_id=None, reason="concerning")

    confirming_text = _confirms_dose_any_language(config, envelope.text)

    if envelope.chat_id != roster.parent.chat_id:
        if confirming_text:
            return _reject(txn, envelope, now_utc, local_day, reason=REJECT_REASON_UNAUTHORIZED, episode_id=None)
        return DoseReplyOutcome(kind=REPLY_KIND_UNRELATED, episode_id=None, dose_id=None, reason=None)

    if not confirming_text:
        return DoseReplyOutcome(kind=REPLY_KIND_UNRELATED, episode_id=None, dose_id=None, reason=None)

    dose_outbox = _dose_outbox_rows(store)

    if envelope.reply_to_message_id is not None:
        target = str(envelope.reply_to_message_id)
        matches = {row.episode_id for row in dose_outbox if row.receipt_id == target}
        if len(matches) != 1:
            return _reject(txn, envelope, now_utc, local_day, reason=REJECT_REASON_UNKNOWN, episode_id=None)
        episode_id = next(iter(matches))
        if episode_id not in _open_dose_episode_ids(store):
            return _reject(txn, envelope, now_utc, local_day, reason=REJECT_REASON_CLOSED, episode_id=episode_id)
        return _confirm(txn, envelope, now_utc, local_day, episode_id)

    open_episodes = _open_dose_episode_ids(store)
    if len(open_episodes) > 1:
        return _reject(txn, envelope, now_utc, local_day, reason=REJECT_REASON_AMBIGUOUS, episode_id=None)
    if len(open_episodes) == 1:
        episode_id = next(iter(open_episodes))
        return _confirm(txn, envelope, now_utc, local_day, episode_id)

    if _pending_dose_episode_ids(store):
        return _reject(txn, envelope, now_utc, local_day, reason=REJECT_REASON_UNDELIVERED, episode_id=None)

    return DoseReplyOutcome(kind=REPLY_KIND_UNRELATED, episode_id=None, dose_id=None, reason=None)
