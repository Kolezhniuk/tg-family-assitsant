import shutil
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from care import checkins
from care.checkins import (
    ACK_LATE,
    ACK_WITHIN_WINDOW,
    CLASSIFICATION_CLEAR,
    CLASSIFICATION_CONCERNING,
    REPLY_KIND_FAMILY_ACK,
    REPLY_KIND_PARENT,
    REPLY_KIND_UNRELATED,
    STEP_ESCALATION,
    STEP_NUDGE,
    STEP_OPERATIONAL_ALERT,
    STEP_PROMPT,
    STEP_STAND_DOWN,
    run_checkin_tick,
)
from care.clock import FixedClock
from care.config import load_config
from care.delivery import DeliveryResult, DeliveryWorker, Transport
from care.models import UpdateEnvelope, checkin_episode_id, dose_episode_id, make_action_key
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


@pytest.fixture
def config_dir(tmp_path):
    for name in CATALOGUE_FILES:
        shutil.copy(REPO_CONFIG_DIR / name, tmp_path / name)
    (tmp_path / "roster.yaml").write_text(ROSTER_TEMPLATE, encoding="utf-8")
    shutil.copy(REPO_CONFIG_DIR / "meds.example.yaml", tmp_path / "meds.yaml")
    return tmp_path


@pytest.fixture
def config(config_dir):
    return load_config(config_dir)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "state" / "care.db"


@pytest.fixture
def store(db_path):
    with Store.open(db_path) as s:
        yield s


def _envelope(update_id, *, chat_id, sender_id, text, reply_to=None, received_at):
    return UpdateEnvelope(
        update_id=update_id,
        chat_id=chat_id,
        chat_type="private",
        sender_id=sender_id,
        message_id=2000 + update_id,
        reply_to_message_id=reply_to,
        received_at_utc=received_at,
        text=text,
    )


def _deliver_due(store, transport, at_utc):
    worker = DeliveryWorker(store=store, transport=transport, clock=FixedClock(at_utc))
    return worker.run_once()

def _handle_reply(store, config, clock, envelope):
    service = CareService(store=store, config=config, clock=clock)
    update_outcome = service.handle_reply(envelope)
    result = update_outcome.result
    return checkins.ReplyOutcome(
        kind=result["kind"],
        episode_id=result["episode_id"],
        classification=result["classification"],
    )


def test_prompt_enqueued_only_inside_catchup_window(store, config):
    episode_id = checkin_episode_id(date(2026, 9, 14))
    action_key = make_action_key(episode_id, STEP_PROMPT)

    before = FixedClock(datetime(2026, 9, 14, 5, 59, tzinfo=UTC))
    run_checkin_tick(store, config, before)
    assert store.get_outbox_by_action_key(action_key) is None

    inside = FixedClock(datetime(2026, 9, 14, 6, 10, tzinfo=UTC))
    result = run_checkin_tick(store, config, inside)
    assert result.actions_taken == (STEP_PROMPT,)
    row = store.get_outbox_by_action_key(action_key)
    assert row is not None
    assert row.status == "queued"
    assert row.chat_id == config.roster.parent.chat_id


def test_missed_window_never_sends_a_late_checkin_but_alerts_family(store, config):
    episode_id = checkin_episode_id(date(2026, 9, 14))
    prompt_key = make_action_key(episode_id, STEP_PROMPT)
    alert_key = make_action_key(episode_id, STEP_OPERATIONAL_ALERT)

    late = FixedClock(datetime(2026, 9, 14, 13, 0, tzinfo=UTC))
    result = run_checkin_tick(store, config, late)

    assert store.get_outbox_by_action_key(prompt_key) is None
    assert STEP_PROMPT not in result.actions_taken

    alert_row = store.get_outbox_by_action_key(alert_key)
    assert alert_row is not None
    assert alert_row.chat_id == config.roster.group.chat_id
    assert "could not reach" in alert_row.text.lower()
    assert "could not reach" not in config.messages["en"].messages["escalation_silence"].lower()
    assert alert_row.text != config.messages["en"].messages["escalation_silence"]


def test_operational_alert_is_not_confused_with_silence_escalation(store, config):
    episode_id = checkin_episode_id(date(2026, 9, 14))
    transport = ScriptedTransport(
        [DeliveryResult(status="failed", error="permanent rejection", retryable=False)]
    )

    inside = FixedClock(datetime(2026, 9, 14, 6, 5, tzinfo=UTC))
    run_checkin_tick(store, config, inside)
    _deliver_due(store, transport, datetime(2026, 9, 14, 6, 6, tzinfo=UTC))

    after_window = FixedClock(datetime(2026, 9, 14, 6, 31, tzinfo=UTC))
    result = run_checkin_tick(store, config, after_window)

    assert STEP_OPERATIONAL_ALERT in result.actions_taken
    assert store.get_outbox_by_action_key(make_action_key(episode_id, STEP_ESCALATION)) is None
    assert store.get_outbox_by_action_key(make_action_key(episode_id, STEP_NUDGE)) is None


def test_episode_activates_on_delivery_not_on_enqueue(store, config):
    episode_id = checkin_episode_id(date(2026, 9, 14))

    enqueue_time = datetime(2026, 9, 14, 6, 5, tzinfo=UTC)
    run_checkin_tick(store, config, FixedClock(enqueue_time))

    much_later_but_not_delivered = FixedClock(enqueue_time + timedelta(hours=5))
    result = run_checkin_tick(store, config, much_later_but_not_delivered)
    assert STEP_NUDGE not in result.actions_taken
    assert store.get_outbox_by_action_key(make_action_key(episode_id, STEP_NUDGE)) is None

    delivered_at = enqueue_time + timedelta(hours=6)
    _deliver_due(store, NumericReceiptTransport(), delivered_at)

    too_soon_after_delivery = FixedClock(delivered_at + timedelta(minutes=30))
    result = run_checkin_tick(store, config, too_soon_after_delivery)
    assert STEP_NUDGE not in result.actions_taken

    nudge_due = FixedClock(delivered_at + timedelta(minutes=181))
    result = run_checkin_tick(store, config, nudge_due)
    assert STEP_NUDGE in result.actions_taken
    nudge_row = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_NUDGE))
    assert nudge_row is not None


def test_nudge_delivery_failure_does_not_postpone_escalation(store, config):
    episode_id = checkin_episode_id(date(2026, 9, 14))
    enqueue_time = datetime(2026, 9, 14, 6, 5, tzinfo=UTC)
    run_checkin_tick(store, config, FixedClock(enqueue_time))

    delivered_at = enqueue_time
    _deliver_due(store, NumericReceiptTransport(), delivered_at)

    nudge_tick_time = delivered_at + timedelta(minutes=181)
    run_checkin_tick(store, config, FixedClock(nudge_tick_time))
    nudge_row = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_NUDGE))
    assert nudge_row is not None and nudge_row.status == "queued"

    failing_transport = ScriptedTransport(
        [DeliveryResult(status="failed", error="permanent nudge failure", retryable=False)]
    )
    _deliver_due(store, failing_transport, nudge_tick_time + timedelta(minutes=1))
    nudge_row = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_NUDGE))
    assert nudge_row.status == "failed"

    escalate_tick_time = delivered_at + timedelta(minutes=361)
    result = run_checkin_tick(store, config, FixedClock(escalate_tick_time))
    assert STEP_ESCALATION in result.actions_taken
    escalation_row = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_ESCALATION))
    assert escalation_row is not None
    assert escalation_row.chat_id == config.roster.group.chat_id


def test_exactly_one_silence_escalation_and_nudges_stop_after_it_fires(store, config):
    episode_id = checkin_episode_id(date(2026, 9, 14))
    enqueue_time = datetime(2026, 9, 14, 6, 5, tzinfo=UTC)
    run_checkin_tick(store, config, FixedClock(enqueue_time))
    _deliver_due(store, NumericReceiptTransport(), enqueue_time)

    escalate_time = enqueue_time + timedelta(minutes=361)
    first = run_checkin_tick(store, config, FixedClock(escalate_time))
    assert STEP_ESCALATION in first.actions_taken

    second = run_checkin_tick(store, config, FixedClock(escalate_time + timedelta(minutes=5)))
    assert STEP_ESCALATION not in second.actions_taken
    assert STEP_NUDGE not in second.actions_taken

    rows = [r for r in store.list_outbox(episode_id=episode_id) if r.action_key.endswith(f"::{STEP_ESCALATION}")]
    assert len(rows) == 1


def test_reply_before_delivery_does_not_correlate(store, config):
    episode_id = checkin_episode_id(date(2026, 9, 14))
    enqueue_time = datetime(2026, 9, 14, 6, 5, tzinfo=UTC)
    run_checkin_tick(store, config, FixedClock(enqueue_time))

    outcome = _handle_reply(store, config, FixedClock(enqueue_time + timedelta(minutes=1)), _envelope(
                1,
                chat_id=config.roster.parent.chat_id,
                sender_id="parent",
                text="I'm fine",
                received_at=enqueue_time + timedelta(minutes=1),
            ))
    assert outcome.kind == REPLY_KIND_UNRELATED
    assert outcome.episode_id is None


def test_medication_reply_does_not_close_checkin_episode(store, config):
    episode_id = checkin_episode_id(date(2026, 9, 14))
    enqueue_time = datetime(2026, 9, 14, 6, 5, tzinfo=UTC)
    run_checkin_tick(store, config, FixedClock(enqueue_time))
    checkin_delivery = NumericReceiptTransport(start=1000)
    _deliver_due(store, checkin_delivery, enqueue_time)

    dose_ep = dose_episode_id("morning-pill", date(2026, 9, 14))
    with store.transaction() as txn:
        txn.enqueue_outbox(
            action_key=make_action_key(dose_ep, "reminder"),
            chat_id=config.roster.parent.chat_id,
            text="Time for your morning pill.",
            priority=0,
            available_at_utc=enqueue_time,
            created_at_utc=enqueue_time,
            episode_id=dose_ep,
        )
    dose_delivery = NumericReceiptTransport(start=9000)
    _deliver_due(store, dose_delivery, enqueue_time + timedelta(minutes=2))
    dose_receipt = store.get_outbox_by_action_key(make_action_key(dose_ep, "reminder")).receipt_id

    outcome = _handle_reply(store, config, FixedClock(enqueue_time + timedelta(minutes=3)), _envelope(
                2,
                chat_id=config.roster.parent.chat_id,
                sender_id="parent",
                text="took it",
                reply_to=int(dose_receipt),
                received_at=enqueue_time + timedelta(minutes=3),
            ))
    assert outcome.kind == REPLY_KIND_UNRELATED

    events = store.list_events(episode_id=episode_id)
    assert not any(e.kind == checkins.EVENT_REPLY_CLASSIFIED for e in events)


def test_reply_correlates_via_reply_to_receipt_id(store, config):
    episode_id = checkin_episode_id(date(2026, 9, 14))
    enqueue_time = datetime(2026, 9, 14, 6, 5, tzinfo=UTC)
    run_checkin_tick(store, config, FixedClock(enqueue_time))
    transport = NumericReceiptTransport(start=4000)
    _deliver_due(store, transport, enqueue_time)
    prompt_receipt = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_PROMPT)).receipt_id

    outcome = _handle_reply(store, config, FixedClock(enqueue_time + timedelta(minutes=5)), _envelope(
                3,
                chat_id=config.roster.parent.chat_id,
                sender_id="parent",
                text="I'm doing fine today",
                reply_to=int(prompt_receipt),
                received_at=enqueue_time + timedelta(minutes=5),
            ))
    assert outcome.kind == REPLY_KIND_PARENT
    assert outcome.episode_id == episode_id
    assert outcome.classification == CLASSIFICATION_CLEAR


def test_reply_falls_back_to_exactly_one_open_episode(store, config):
    episode_id = checkin_episode_id(date(2026, 9, 14))
    enqueue_time = datetime(2026, 9, 14, 6, 5, tzinfo=UTC)
    run_checkin_tick(store, config, FixedClock(enqueue_time))
    _deliver_due(store, NumericReceiptTransport(), enqueue_time)

    outcome = _handle_reply(store, config, FixedClock(enqueue_time + timedelta(minutes=10)), _envelope(
                4,
                chat_id=config.roster.parent.chat_id,
                sender_id="parent",
                text="all good here",
                reply_to=None,
                received_at=enqueue_time + timedelta(minutes=10),
            ))
    assert outcome.kind == REPLY_KIND_PARENT
    assert outcome.episode_id == episode_id


def test_clear_reply_after_escalation_creates_one_stand_down(store, config):
    episode_id = checkin_episode_id(date(2026, 9, 14))
    enqueue_time = datetime(2026, 9, 14, 6, 5, tzinfo=UTC)
    run_checkin_tick(store, config, FixedClock(enqueue_time))
    _deliver_due(store, NumericReceiptTransport(), enqueue_time)

    escalate_time = enqueue_time + timedelta(minutes=361)
    run_checkin_tick(store, config, FixedClock(escalate_time))
    assert store.get_outbox_by_action_key(make_action_key(episode_id, STEP_ESCALATION)) is not None

    reply_time = escalate_time + timedelta(minutes=10)
    outcome = _handle_reply(store, config, FixedClock(reply_time), _envelope(
                5,
                chat_id=config.roster.parent.chat_id,
                sender_id="parent",
                text="sorry, phone was on silent, all good",
                reply_to=None,
                received_at=reply_time,
            ))
    assert outcome.classification == CLASSIFICATION_CLEAR
    stand_down_row = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_STAND_DOWN))
    assert stand_down_row is not None
    assert stand_down_row.chat_id == config.roster.group.chat_id


def test_concerning_reply_after_escalation_updates_concern_not_stand_down(store, config):
    episode_id = checkin_episode_id(date(2026, 9, 14))
    enqueue_time = datetime(2026, 9, 14, 6, 5, tzinfo=UTC)
    run_checkin_tick(store, config, FixedClock(enqueue_time))
    _deliver_due(store, NumericReceiptTransport(), enqueue_time)

    escalate_time = enqueue_time + timedelta(minutes=361)
    run_checkin_tick(store, config, FixedClock(escalate_time))

    reply_time = escalate_time + timedelta(minutes=10)
    outcome = _handle_reply(store, config, FixedClock(reply_time), _envelope(
                6,
                chat_id=config.roster.parent.chat_id,
                sender_id="parent",
                text="I fell and can't get up",
                reply_to=None,
                received_at=reply_time,
            ))
    assert outcome.classification == CLASSIFICATION_CONCERNING
    assert store.get_outbox_by_action_key(make_action_key(episode_id, STEP_STAND_DOWN)) is None
    concern_rows = [
        r for r in store.list_outbox(episode_id=episode_id) if r.action_key.split("::")[-1].startswith("concern:")
    ]
    assert len(concern_rows) == 1


def test_family_acknowledgement_classified_within_and_after_thirty_minutes(store, config):
    episode_id = checkin_episode_id(date(2026, 9, 14))
    enqueue_time = datetime(2026, 9, 14, 6, 5, tzinfo=UTC)
    run_checkin_tick(store, config, FixedClock(enqueue_time))
    _deliver_due(store, NumericReceiptTransport(), enqueue_time)

    escalate_time = enqueue_time + timedelta(minutes=361)
    run_checkin_tick(store, config, FixedClock(escalate_time))

    escalation_delivery = NumericReceiptTransport(start=7000)
    _deliver_due(store, escalation_delivery, escalate_time)
    escalation_receipt = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_ESCALATION)).receipt_id

    ack_time = escalate_time + timedelta(minutes=10)
    outcome = _handle_reply(store, config, FixedClock(ack_time), _envelope(
                7,
                chat_id=config.roster.group.chat_id,
                sender_id="fam-1",
                text="calling her now",
                reply_to=int(escalation_receipt),
                received_at=ack_time,
            ))
    assert outcome.kind == REPLY_KIND_FAMILY_ACK
    assert outcome.classification == ACK_WITHIN_WINDOW

    late_ack_time = escalate_time + timedelta(minutes=45)
    late_outcome = _handle_reply(store, config, FixedClock(late_ack_time), _envelope(
                8,
                chat_id=config.roster.group.chat_id,
                sender_id="fam-2",
                text="sorry just saw this, calling now",
                reply_to=int(escalation_receipt),
                received_at=late_ack_time,
            ))
    assert late_outcome.classification == ACK_LATE


def test_sequential_ticks_enqueue_prompt_exactly_once(config_dir, config, db_path):
    at = datetime(2026, 9, 14, 6, 10, tzinfo=UTC)
    for _ in range(3):
        with Store.open(db_path) as s:
            run_checkin_tick(s, config, FixedClock(at))
    with Store.open(db_path) as s:
        rows = [r for r in s.list_outbox() if r.action_key.endswith(f"::{STEP_PROMPT}")]
    assert len(rows) == 1


def test_concurrent_ticks_enqueue_prompt_exactly_once(config_dir, config, db_path):
    at = datetime(2026, 9, 14, 6, 10, tzinfo=UTC)
    barrier = threading.Barrier(4)
    errors = []

    def worker():
        try:
            barrier.wait(timeout=5)
            with Store.open(db_path) as s:
                run_checkin_tick(s, config, FixedClock(at))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    with Store.open(db_path) as s:
        rows = [r for r in s.list_outbox() if r.action_key.endswith(f"::{STEP_PROMPT}")]
    assert len(rows) == 1


def test_checkin_window_crosses_spring_forward_boundary(store, config):
    local_day = date(2026, 3, 29)
    episode_id = checkin_episode_id(local_day)
    inside = FixedClock(datetime(2026, 3, 29, 6, 10, tzinfo=UTC))
    result = run_checkin_tick(store, config, inside)
    assert result.actions_taken == (STEP_PROMPT,)
    row = store.get_outbox_by_action_key(make_action_key(episode_id, STEP_PROMPT))
    assert row is not None


def test_checkin_ladder_crosses_fall_back_boundary(store, config):
    local_day = date(2026, 10, 25)
    episode_id = checkin_episode_id(local_day)
    enqueue_time = datetime(2026, 10, 25, 7, 5, tzinfo=UTC)
    run_checkin_tick(store, config, FixedClock(enqueue_time))
    _deliver_due(store, NumericReceiptTransport(), enqueue_time)

    escalate_time = enqueue_time + timedelta(minutes=361)
    result = run_checkin_tick(store, config, FixedClock(escalate_time))
    assert STEP_ESCALATION in result.actions_taken

    reply_time = escalate_time + timedelta(minutes=5)
    outcome = _handle_reply(store, config, FixedClock(reply_time), _envelope(
                9,
                chat_id=config.roster.parent.chat_id,
                sender_id="parent",
                text="all fine, just tired",
                reply_to=None,
                received_at=reply_time,
            ))
    assert outcome.classification == CLASSIFICATION_CLEAR
    assert store.get_outbox_by_action_key(make_action_key(episode_id, STEP_STAND_DOWN)) is not None
