import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from care import meds
from care.clock import FixedClock
from care.config import load_config
from care.delivery import DeliveryResult, DeliveryWorker, PRIORITY_ESCALATION, Transport
from care.meds import (
    EVENT_ADHERENCE_NOTICE,
    EVENT_CLOSED_UNCONFIRMED,
    EVENT_CONFIRMED,
    REJECT_REASON_AMBIGUOUS,
    REJECT_REASON_CLOSED,
    REJECT_REASON_UNAUTHORIZED,
    REJECT_REASON_UNDELIVERED,
    REJECT_REASON_UNKNOWN,
    REPLY_KIND_CONFIRMED,
    REPLY_KIND_REJECTED,
    REPLY_KIND_UNRELATED,
    STEP_NUDGE,
    STEP_OPERATIONAL_ALERT,
    STEP_REMINDER,
    run_meds_tick,
)
from care.models import UpdateEnvelope, dose_episode_id, make_action_key
from care.service import CareService
from care.store import Store

UTC = timezone.utc

REPO_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
CATALOGUE_FILES = [
    "tripwire.uk.yaml",
    "tripwire.en.yaml",
    "affirmatives.uk.yaml",
    "affirmatives.en.yaml",
    "negatives.uk.yaml",
    "negatives.en.yaml",
    "messages.uk.yaml",
    "messages.en.yaml",
]

ROSTER_TEMPLATE = """\
timezone: Europe/Kyiv
quiet_hours:
  start: "21:30"
  end: "08:00"
checkin:
  time: "09:00"
  nudge_after_minutes: 180
  escalate_after_minutes: 360
languages: [en, uk]
delivery:
  mode: dry-run
state:
  path: /var/lib/care/care-state.db
parent:
  chat_id: "parent-1"
  name: Mum
group:
  chat_id: "group-1"
  name: Family
family:
  - chat_id: "fam-1"
    name: Dima
"""

ONE_DOSE_MEDS = '- id: med_a\n  label: "the blood pressure tablet"\n  time: "10:00"\n'

TWO_DOSE_MEDS = (
    '- id: med_a\n  label: "the blood pressure tablet"\n  time: "10:00"\n'
    '- id: med_b\n  label: "the evening heart pill"\n  time: "10:05"\n'
)

QUIET_DOSE_MEDS = '- id: med_q\n  label: "the night tablet"\n  time: "22:00"\n'

NO_DOSE_MEDS = "[]\n"


def build_config(tmp_path: Path, meds_text: str):
    for name in CATALOGUE_FILES:
        shutil.copy(REPO_CONFIG_DIR / name, tmp_path / name)
    (tmp_path / "roster.yaml").write_text(ROSTER_TEMPLATE, encoding="utf-8")
    (tmp_path / "meds.yaml").write_text(meds_text, encoding="utf-8")
    return load_config(tmp_path)


class NumericReceiptTransport(Transport):
    def __init__(self, start: int = 5000):
        self.calls = []
        self._next = start

    def send(self, target: str, text: str, idempotency_key: str) -> DeliveryResult:
        self._next += 1
        self.calls.append((target, text, idempotency_key))
        return DeliveryResult(status="delivered", receipt_id=str(self._next))


class ScriptedTransport(Transport):
    def __init__(self, plan):
        self._plan = list(plan)
        self.calls = []

    def send(self, target: str, text: str, idempotency_key: str) -> DeliveryResult:
        self.calls.append((target, text, idempotency_key))
        if self._plan:
            return self._plan.pop(0)
        return DeliveryResult(status="delivered", receipt_id=f"fallback-{len(self.calls)}")


def _deliver_due(store, transport, at_utc):
    worker = DeliveryWorker(store=store, transport=transport, clock=FixedClock(at_utc))
    return worker.run_once()


def _envelope(update_id, *, chat_id, sender_id, text, reply_to=None, received_at):
    return UpdateEnvelope(
        update_id=update_id,
        chat_id=chat_id,
        chat_type="private",
        sender_id=sender_id,
        message_id=3000 + update_id,
        reply_to_message_id=reply_to,
        received_at_utc=received_at,
        text=text,
    )


def _handle_dose_reply(store, config, clock, envelope) -> meds.DoseReplyOutcome:
    service = CareService(store=store, config=config, clock=clock)
    outcome = service.handle_reply(envelope)
    result = outcome.result
    return meds.DoseReplyOutcome(
        kind=result["dose_kind"],
        episode_id=result["dose_episode_id"],
        dose_id=result["dose_id"],
        reason=result["dose_reason"],
    )


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "state" / "care.db") as s:
        yield s


def test_reminder_enqueued_only_inside_catchup_window(tmp_path, store):
    config = build_config(tmp_path, ONE_DOSE_MEDS)
    local_day = date(2026, 9, 14)
    episode_id = dose_episode_id("med_a", local_day)
    action_key = make_action_key(episode_id, STEP_REMINDER)

    before = FixedClock(datetime(2026, 9, 14, 6, 59, tzinfo=UTC))
    run_meds_tick(store, config, before)
    assert store.get_outbox_by_action_key(action_key) is None

    inside = FixedClock(datetime(2026, 9, 14, 7, 10, tzinfo=UTC))
    result = run_meds_tick(store, config, inside)
    assert f"{STEP_REMINDER}:med_a" in result.actions_taken
    row = store.get_outbox_by_action_key(action_key)
    assert row is not None
    assert row.status == "queued"
    assert row.chat_id == config.roster.parent.chat_id
    assert row.text == "Time for the blood pressure tablet."


def test_missed_window_never_sends_late_reminder_but_alerts_family(tmp_path, store):
    config = build_config(tmp_path, ONE_DOSE_MEDS)
    local_day = date(2026, 9, 14)
    episode_id = dose_episode_id("med_a", local_day)
    reminder_key = make_action_key(episode_id, STEP_REMINDER)
    alert_key = make_action_key(episode_id, STEP_OPERATIONAL_ALERT)

    late = FixedClock(datetime(2026, 9, 14, 13, 0, tzinfo=UTC))
    result = run_meds_tick(store, config, late)

    assert store.get_outbox_by_action_key(reminder_key) is None
    assert f"{STEP_REMINDER}:med_a" not in result.actions_taken

    alert_row = store.get_outbox_by_action_key(alert_key)
    assert alert_row is not None
    assert alert_row.chat_id == config.roster.group.chat_id
    assert "could not reach" in alert_row.text.lower()


def test_reminder_scheduled_in_quiet_hours_is_suppressed_not_deferred(tmp_path, store):
    config = build_config(tmp_path, QUIET_DOSE_MEDS)
    local_day = date(2026, 9, 14)
    episode_id = dose_episode_id("med_q", local_day)
    reminder_key = make_action_key(episode_id, STEP_REMINDER)
    alert_key = make_action_key(episode_id, STEP_OPERATIONAL_ALERT)

    during = FixedClock(datetime(2026, 9, 14, 19, 5, tzinfo=UTC))
    result = run_meds_tick(store, config, during)
    assert "reminder_suppressed:med_q" in result.actions_taken
    assert store.get_outbox_by_action_key(reminder_key) is None

    next_morning = FixedClock(datetime(2026, 9, 15, 5, 0, tzinfo=UTC))
    run_meds_tick(store, config, next_morning)
    assert store.get_outbox_by_action_key(reminder_key) is None
    assert store.get_outbox_by_action_key(alert_key) is None


def test_confirmation_activates_only_after_delivery(tmp_path, store):
    config = build_config(tmp_path, ONE_DOSE_MEDS)
    local_day = date(2026, 9, 14)
    episode_id = dose_episode_id("med_a", local_day)

    enqueue_time = datetime(2026, 9, 14, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(enqueue_time))

    outcome = _handle_dose_reply(
        store,
        config,
        FixedClock(enqueue_time + timedelta(minutes=1)),
        _envelope(
            1,
            chat_id=config.roster.parent.chat_id,
            sender_id="parent",
            text="✅",
            received_at=enqueue_time + timedelta(minutes=1),
        ),
    )
    assert outcome.kind == REPLY_KIND_REJECTED
    assert outcome.reason == REJECT_REASON_UNDELIVERED
    assert not any(e.kind == EVENT_CONFIRMED for e in store.list_events(episode_id=episode_id))


def test_nudge_then_close_unconfirmed_when_never_confirmed(tmp_path, store):
    config = build_config(tmp_path, ONE_DOSE_MEDS)
    local_day = date(2026, 9, 14)
    episode_id = dose_episode_id("med_a", local_day)

    enqueue_time = datetime(2026, 9, 14, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(enqueue_time))
    _deliver_due(store, NumericReceiptTransport(), enqueue_time)

    too_soon = FixedClock(enqueue_time + timedelta(minutes=44))
    result = run_meds_tick(store, config, too_soon)
    assert f"{STEP_NUDGE}:med_a" not in result.actions_taken

    nudge_due = FixedClock(enqueue_time + timedelta(minutes=46))
    result = run_meds_tick(store, config, nudge_due)
    assert f"{STEP_NUDGE}:med_a" in result.actions_taken
    nudge_row = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_NUDGE))
    assert nudge_row is not None
    assert nudge_row.chat_id == config.roster.parent.chat_id

    too_soon_for_close = FixedClock(enqueue_time + timedelta(minutes=119))
    result = run_meds_tick(store, config, too_soon_for_close)
    assert "closed_unconfirmed:med_a" not in result.actions_taken

    close_due = FixedClock(enqueue_time + timedelta(minutes=121))
    result = run_meds_tick(store, config, close_due)
    assert "closed_unconfirmed:med_a" in result.actions_taken

    events = store.list_events(episode_id=episode_id)
    kinds = [e.kind for e in events]
    assert EVENT_CLOSED_UNCONFIRMED in kinds
    assert not any("missed" in k for k in kinds)

    again = FixedClock(enqueue_time + timedelta(minutes=200))
    result = run_meds_tick(store, config, again)
    assert "closed_unconfirmed:med_a" not in result.actions_taken


def test_confirmed_dose_never_closes_unconfirmed(tmp_path, store):
    config = build_config(tmp_path, ONE_DOSE_MEDS)
    local_day = date(2026, 9, 14)
    episode_id = dose_episode_id("med_a", local_day)

    enqueue_time = datetime(2026, 9, 14, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(enqueue_time))
    _deliver_due(store, NumericReceiptTransport(), enqueue_time)

    outcome = _handle_dose_reply(
        store,
        config,
        FixedClock(enqueue_time + timedelta(minutes=5)),
        _envelope(
            2,
            chat_id=config.roster.parent.chat_id,
            sender_id="parent",
            text="✅",
            received_at=enqueue_time + timedelta(minutes=5),
        ),
    )
    assert outcome.kind == REPLY_KIND_CONFIRMED
    assert outcome.dose_id == "med_a"

    close_due = FixedClock(enqueue_time + timedelta(minutes=121))
    result = run_meds_tick(store, config, close_due)
    assert "closed_unconfirmed:med_a" not in result.actions_taken
    assert f"{STEP_NUDGE}:med_a" not in result.actions_taken


def test_ukrainian_negation_blocks_confirmation(tmp_path, store):
    config = build_config(tmp_path, ONE_DOSE_MEDS)
    enqueue_time = datetime(2026, 9, 14, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(enqueue_time))
    _deliver_due(store, NumericReceiptTransport(), enqueue_time)

    outcome = _handle_dose_reply(
        store,
        config,
        FixedClock(enqueue_time + timedelta(minutes=5)),
        _envelope(
            3,
            chat_id=config.roster.parent.chat_id,
            sender_id="parent",
            text="не випила",
            received_at=enqueue_time + timedelta(minutes=5),
        ),
    )
    assert outcome.kind == REPLY_KIND_UNRELATED
    episode_id = dose_episode_id("med_a", date(2026, 9, 14))
    assert not any(e.kind == EVENT_CONFIRMED for e in store.list_events(episode_id=episode_id))


def test_bare_checkmark_confirms_via_fallback(tmp_path, store):
    config = build_config(tmp_path, ONE_DOSE_MEDS)
    episode_id = dose_episode_id("med_a", date(2026, 9, 14))
    enqueue_time = datetime(2026, 9, 14, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(enqueue_time))
    _deliver_due(store, NumericReceiptTransport(), enqueue_time)

    outcome = _handle_dose_reply(
        store,
        config,
        FixedClock(enqueue_time + timedelta(minutes=2)),
        _envelope(
            4,
            chat_id=config.roster.parent.chat_id,
            sender_id="parent",
            text="✅",
            received_at=enqueue_time + timedelta(minutes=2),
        ),
    )
    assert outcome.kind == REPLY_KIND_CONFIRMED
    assert outcome.episode_id == episode_id


def test_reply_to_receipt_confirms_specific_dose(tmp_path, store):
    config = build_config(tmp_path, TWO_DOSE_MEDS)
    enqueue_time = datetime(2026, 9, 14, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(enqueue_time))
    transport = NumericReceiptTransport(start=6000)
    _deliver_due(store, transport, enqueue_time)

    episode_a = dose_episode_id("med_a", date(2026, 9, 14))
    episode_b = dose_episode_id("med_b", date(2026, 9, 14))
    receipt_a = store.get_outbox_by_action_key(make_action_key(episode_a, STEP_REMINDER)).receipt_id

    outcome = _handle_dose_reply(
        store,
        config,
        FixedClock(enqueue_time + timedelta(minutes=2)),
        _envelope(
            5,
            chat_id=config.roster.parent.chat_id,
            sender_id="parent",
            text="took it",
            reply_to=int(receipt_a),
            received_at=enqueue_time + timedelta(minutes=2),
        ),
    )
    assert outcome.kind == REPLY_KIND_CONFIRMED
    assert outcome.episode_id == episode_a
    assert episode_b != episode_a
    assert not any(e.kind == EVENT_CONFIRMED for e in store.list_events(episode_id=episode_b))


def test_two_overlapping_dose_windows_do_not_auto_confirm_ambiguously(tmp_path, store):
    config = build_config(tmp_path, TWO_DOSE_MEDS)
    enqueue_time = datetime(2026, 9, 14, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(enqueue_time))
    _deliver_due(store, NumericReceiptTransport(), enqueue_time)

    outcome = _handle_dose_reply(
        store,
        config,
        FixedClock(enqueue_time + timedelta(minutes=2)),
        _envelope(
            6,
            chat_id=config.roster.parent.chat_id,
            sender_id="parent",
            text="✅",
            received_at=enqueue_time + timedelta(minutes=2),
        ),
    )
    assert outcome.kind == REPLY_KIND_REJECTED
    assert outcome.reason == REJECT_REASON_AMBIGUOUS
    assert not any(e.kind == EVENT_CONFIRMED for e in store.list_events())


def test_reject_unknown_reply_to(tmp_path, store):
    config = build_config(tmp_path, ONE_DOSE_MEDS)
    enqueue_time = datetime(2026, 9, 14, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(enqueue_time))
    _deliver_due(store, NumericReceiptTransport(), enqueue_time)

    outcome = _handle_dose_reply(
        store,
        config,
        FixedClock(enqueue_time + timedelta(minutes=2)),
        _envelope(
            7,
            chat_id=config.roster.parent.chat_id,
            sender_id="parent",
            text="✅",
            reply_to=999999,
            received_at=enqueue_time + timedelta(minutes=2),
        ),
    )
    assert outcome.kind == REPLY_KIND_REJECTED
    assert outcome.reason == REJECT_REASON_UNKNOWN


def test_reject_closed_episode_confirmation(tmp_path, store):
    config = build_config(tmp_path, ONE_DOSE_MEDS)
    episode_id = dose_episode_id("med_a", date(2026, 9, 14))
    enqueue_time = datetime(2026, 9, 14, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(enqueue_time))
    transport = NumericReceiptTransport(start=7000)
    _deliver_due(store, transport, enqueue_time)
    receipt = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_REMINDER)).receipt_id

    first = _handle_dose_reply(
        store,
        config,
        FixedClock(enqueue_time + timedelta(minutes=2)),
        _envelope(
            8,
            chat_id=config.roster.parent.chat_id,
            sender_id="parent",
            text="✅",
            reply_to=int(receipt),
            received_at=enqueue_time + timedelta(minutes=2),
        ),
    )
    assert first.kind == REPLY_KIND_CONFIRMED

    second = _handle_dose_reply(
        store,
        config,
        FixedClock(enqueue_time + timedelta(minutes=3)),
        _envelope(
            9,
            chat_id=config.roster.parent.chat_id,
            sender_id="parent",
            text="✅",
            reply_to=int(receipt),
            received_at=enqueue_time + timedelta(minutes=3),
        ),
    )
    assert second.kind == REPLY_KIND_REJECTED
    assert second.reason == REJECT_REASON_CLOSED


def test_reject_unauthorized_sender_from_family_group(tmp_path, store):
    config = build_config(tmp_path, ONE_DOSE_MEDS)
    enqueue_time = datetime(2026, 9, 14, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(enqueue_time))
    _deliver_due(store, NumericReceiptTransport(), enqueue_time)

    outcome = _handle_dose_reply(
        store,
        config,
        FixedClock(enqueue_time + timedelta(minutes=2)),
        _envelope(
            10,
            chat_id=config.roster.group.chat_id,
            sender_id="fam-1",
            text="✅ she took it",
            received_at=enqueue_time + timedelta(minutes=2),
        ),
    )
    assert outcome.kind == REPLY_KIND_REJECTED
    assert outcome.reason == REJECT_REASON_UNAUTHORIZED
    episode_id = dose_episode_id("med_a", date(2026, 9, 14))
    assert not any(e.kind == EVENT_CONFIRMED for e in store.list_events(episode_id=episode_id))


def test_reject_undelivered_dose_confirmation_via_fallback(tmp_path, store):
    config = build_config(tmp_path, ONE_DOSE_MEDS)
    enqueue_time = datetime(2026, 9, 14, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(enqueue_time))

    outcome = _handle_dose_reply(
        store,
        config,
        FixedClock(enqueue_time + timedelta(minutes=2)),
        _envelope(
            11,
            chat_id=config.roster.parent.chat_id,
            sender_id="parent",
            text="✅",
            received_at=enqueue_time + timedelta(minutes=2),
        ),
    )
    assert outcome.kind == REPLY_KIND_REJECTED
    assert outcome.reason == REJECT_REASON_UNDELIVERED


def test_concerning_reply_escalates_before_confirmation(tmp_path, store):
    config = build_config(tmp_path, ONE_DOSE_MEDS)
    episode_id = dose_episode_id("med_a", date(2026, 9, 14))
    enqueue_time = datetime(2026, 9, 14, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(enqueue_time))
    _deliver_due(store, NumericReceiptTransport(), enqueue_time)

    reply_time = enqueue_time + timedelta(minutes=5)
    service = CareService(store=store, config=config, clock=FixedClock(reply_time))
    outcome = service.handle_reply(
        _envelope(
            12,
            chat_id=config.roster.parent.chat_id,
            sender_id="parent",
            text="✅ but I fell and can't get up",
            received_at=reply_time,
        )
    )
    result = outcome.result
    assert result["dose_kind"] != REPLY_KIND_CONFIRMED
    assert not any(e.kind == EVENT_CONFIRMED for e in store.list_events(episode_id=episode_id))

    escalation_rows = [r for r in store.list_outbox() if r.priority == PRIORITY_ESCALATION]
    assert len(escalation_rows) == 1
    assert escalation_rows[0].chat_id == config.roster.group.chat_id


def test_adherence_notice_for_two_distinct_doses_unconfirmed_same_day(tmp_path, store):
    config = build_config(tmp_path, TWO_DOSE_MEDS)
    enqueue_time = datetime(2026, 9, 14, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(enqueue_time))
    _deliver_due(store, NumericReceiptTransport(), enqueue_time)

    close_due = FixedClock(enqueue_time + timedelta(minutes=121))
    result = run_meds_tick(store, config, close_due)
    assert "closed_unconfirmed:med_a" in result.actions_taken
    assert "closed_unconfirmed:med_b" in result.actions_taken
    assert "adherence_notice" in result.actions_taken

    notices = [e for e in store.list_events() if e.kind == EVENT_ADHERENCE_NOTICE]
    assert len(notices) == 1
    outbox_rows = [r for r in store.list_outbox() if r.chat_id == config.roster.group.chat_id and r.episode_id is None]
    assert len(outbox_rows) == 1
    lowered = outbox_rows[0].text.lower()
    assert "does not mean" in lowered or "не означає" in lowered


def test_adherence_notice_for_same_dose_unconfirmed_consecutive_days(tmp_path, store):
    config = build_config(tmp_path, ONE_DOSE_MEDS)
    day1_enqueue = datetime(2026, 9, 14, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(day1_enqueue))
    _deliver_due(store, NumericReceiptTransport(start=1000), day1_enqueue)
    day1_close = run_meds_tick(store, config, FixedClock(day1_enqueue + timedelta(minutes=121)))
    assert "closed_unconfirmed:med_a" in day1_close.actions_taken
    assert "adherence_notice" not in day1_close.actions_taken

    day2_enqueue = datetime(2026, 9, 15, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(day2_enqueue))
    _deliver_due(store, NumericReceiptTransport(start=2000), day2_enqueue)
    day2_close = run_meds_tick(store, config, FixedClock(day2_enqueue + timedelta(minutes=121)))
    assert "closed_unconfirmed:med_a" in day2_close.actions_taken
    assert "adherence_notice" in day2_close.actions_taken

    notices = [e for e in store.list_events() if e.kind == EVENT_ADHERENCE_NOTICE]
    assert len(notices) == 1


def test_adherence_notice_at_most_once_per_rolling_24_hours(tmp_path, store):
    config = build_config(tmp_path, ONE_DOSE_MEDS)

    day1_enqueue = datetime(2026, 9, 14, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(day1_enqueue))
    _deliver_due(store, NumericReceiptTransport(start=1000), day1_enqueue)
    day1_close_at = day1_enqueue + timedelta(minutes=121)
    day1_close = run_meds_tick(store, config, FixedClock(day1_close_at))
    assert "adherence_notice" not in day1_close.actions_taken

    day2_enqueue = datetime(2026, 9, 15, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(day2_enqueue))
    _deliver_due(store, NumericReceiptTransport(start=2000), day2_enqueue)
    day2_close_at = day2_enqueue + timedelta(minutes=121)
    day2_close = run_meds_tick(store, config, FixedClock(day2_close_at))
    assert "adherence_notice" in day2_close.actions_taken

    soon_after = run_meds_tick(store, config, FixedClock(day2_close_at + timedelta(minutes=10)))
    assert "adherence_notice" not in soon_after.actions_taken

    day3_enqueue = datetime(2026, 9, 16, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(day3_enqueue))
    _deliver_due(store, NumericReceiptTransport(start=3000), day3_enqueue)
    day3_close_at = day3_enqueue + timedelta(minutes=121)
    assert day3_close_at - day2_close_at == timedelta(hours=24)
    day3_close = run_meds_tick(store, config, FixedClock(day3_close_at))
    assert "adherence_notice" in day3_close.actions_taken

    notices = [e for e in store.list_events() if e.kind == EVENT_ADHERENCE_NOTICE]
    assert len(notices) == 2


def test_permanently_failed_reminder_never_nudges_closes_or_counts_for_adherence(tmp_path, store):
    config = build_config(tmp_path, ONE_DOSE_MEDS)
    episode_id = dose_episode_id("med_a", date(2026, 9, 14))
    enqueue_time = datetime(2026, 9, 14, 7, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(enqueue_time))

    failing_transport = ScriptedTransport(
        [DeliveryResult(status="failed", error="permanent rejection", retryable=False)]
    )
    _deliver_due(store, failing_transport, enqueue_time)
    reminder_row = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_REMINDER))
    assert reminder_row.status == "failed"

    much_later = FixedClock(enqueue_time + timedelta(hours=3))
    result = run_meds_tick(store, config, much_later)
    assert f"{STEP_NUDGE}:med_a" not in result.actions_taken
    assert "closed_unconfirmed:med_a" not in result.actions_taken
    assert "adherence_notice" not in result.actions_taken
    assert not any(e.kind == EVENT_CLOSED_UNCONFIRMED for e in store.list_events())


def test_undelivered_and_suppressed_reminders_excluded_from_adherence(tmp_path, store):
    meds_text = (
        '- id: med_q\n  label: "the night tablet"\n  time: "22:00"\n'
        '- id: med_fail\n  label: "the failing tablet"\n  time: "10:00"\n'
    )
    config = build_config(tmp_path, meds_text)

    quiet_tick_time = datetime(2026, 9, 14, 19, 5, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(quiet_tick_time))

    fail_window_time = datetime(2026, 9, 14, 7, 45, tzinfo=UTC)
    run_meds_tick(store, config, FixedClock(fail_window_time))

    much_later = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    result = run_meds_tick(store, config, FixedClock(much_later))

    assert not any(e.kind == EVENT_CLOSED_UNCONFIRMED for e in store.list_events())
    assert "adherence_notice" not in result.actions_taken
    notices = [e for e in store.list_events() if e.kind == EVENT_ADHERENCE_NOTICE]
    assert not notices
