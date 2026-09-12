import logging
import re
import subprocess
import threading
from datetime import datetime, timedelta, timezone

import pytest

from care.clock import FixedClock
from care.delivery import (
    PRIORITY_ESCALATION,
    PRIORITY_ROUTINE,
    CaptureTransport,
    DeliveryResult,
    DeliveryWorker,
    FailureInjectionTransport,
    HermesSendTransport,
    _neutralise_markup,
    permanent_error_result,
    retryable_error_result,
    timeout_result,
)
from care.store import MAX_OUTBOX_TEXT_LENGTH, Store


def _strip_markup_neutralisation(sent: str) -> str:
    without_zwsp = sent.replace("​", "")
    return re.sub(r"\\(.)", r"\1", without_zwsp)

UTC = timezone.utc
T0 = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "state" / "care-state.db"


def _enqueue(store, *, action_key, text="hello", priority=0, chat_id="1", available_at=T0):
    with store.transaction() as txn:
        return txn.enqueue_outbox(
            action_key=action_key,
            chat_id=chat_id,
            text=text,
            priority=priority,
            available_at_utc=available_at,
            created_at_utc=T0,
        )


def test_capture_transport_records_calls_and_delivers(db_path):
    store = Store.open(db_path)
    _enqueue(store, action_key="a", text="good morning")
    worker = DeliveryWorker(store=store, transport=(transport := CaptureTransport()), clock=FixedClock(T0))

    outcomes = worker.run_once()

    assert len(outcomes) == 1
    assert outcomes[0].outcome == "delivered"
    assert len(transport.calls) == 1
    assert transport.calls[0].target == "1"
    assert transport.calls[0].text == "good morning"
    assert transport.calls[0].idempotency_key == "a"
    row = store.get_outbox_by_action_key("a")
    assert row.status == "delivered"
    assert row.receipt_id == "capture-1"
    store.close()


def test_escalation_and_stop_notice_priority_over_routine(db_path):
    store = Store.open(db_path)
    _enqueue(store, action_key="routine", text="good morning", priority=PRIORITY_ROUTINE)
    _enqueue(store, action_key="escalation", text="no reply since 09:00", priority=PRIORITY_ESCALATION)
    transport = CaptureTransport()
    worker = DeliveryWorker(store=store, transport=transport, clock=FixedClock(T0), batch_limit=10)

    outcomes = worker.run_once()

    assert [c.idempotency_key for c in transport.calls] == ["escalation", "routine"]
    assert [o.action_key for o in outcomes] == ["escalation", "routine"]
    store.close()


def test_batch_limit_still_prioritises_escalation_first(db_path):
    store = Store.open(db_path)
    _enqueue(store, action_key="routine", text="good morning", priority=PRIORITY_ROUTINE)
    _enqueue(store, action_key="escalation", text="no reply since 09:00", priority=PRIORITY_ESCALATION)
    transport = CaptureTransport()
    worker = DeliveryWorker(store=store, transport=transport, clock=FixedClock(T0), batch_limit=1)

    outcomes = worker.run_once()

    assert len(outcomes) == 1
    assert outcomes[0].action_key == "escalation"
    remaining = store.get_outbox_by_action_key("routine")
    assert remaining.status == "queued"
    store.close()


def test_retryable_failure_then_recovery(db_path):
    store = Store.open(db_path)
    _enqueue(store, action_key="a")
    transport = FailureInjectionTransport([timeout_result("simulated timeout")])
    clock = FixedClock(T0)
    worker = DeliveryWorker(store=store, transport=transport, clock=clock, base_backoff_seconds=30)

    outcomes = worker.run_once()
    assert outcomes[0].outcome == "retrying"
    row = store.get_outbox_by_action_key("a")
    assert row.status == "retrying"
    assert "timeout" in row.last_error

    later = FixedClock(T0 + timedelta(seconds=31))
    worker2 = DeliveryWorker(store=store, transport=transport, clock=later)
    outcomes2 = worker2.run_once()
    assert outcomes2[0].outcome == "delivered"
    row2 = store.get_outbox_by_action_key("a")
    assert row2.status == "delivered"
    store.close()


def test_permanent_failure_is_not_retried(db_path):
    store = Store.open(db_path)
    _enqueue(store, action_key="a")
    transport = FailureInjectionTransport([permanent_error_result("Chat not found")])
    worker = DeliveryWorker(store=store, transport=transport, clock=FixedClock(T0))

    outcomes = worker.run_once()

    assert outcomes[0].outcome == "failed"
    row = store.get_outbox_by_action_key("a")
    assert row.status == "failed"
    assert row.last_error == "Chat not found"
    store.close()


def test_retryable_failure_exhausts_attempts_into_permanent_failure(db_path):
    store = Store.open(db_path)
    _enqueue(store, action_key="a")
    transport = FailureInjectionTransport(
        [retryable_error_result("rate limited") for _ in range(10)]
    )
    clock_time = T0
    worker = DeliveryWorker(
        store=store,
        transport=transport,
        clock=FixedClock(clock_time),
        max_attempts=3,
        base_backoff_seconds=1,
        max_backoff_seconds=2,
    )

    for _ in range(3):
        clock_time = clock_time + timedelta(seconds=5)
        worker = DeliveryWorker(
            store=store,
            transport=transport,
            clock=FixedClock(clock_time),
            max_attempts=3,
            base_backoff_seconds=1,
            max_backoff_seconds=2,
        )
        outcomes = worker.run_once()

    row = store.get_outbox_by_action_key("a")
    assert row.status == "failed"
    assert outcomes[0].outcome == "failed"
    store.close()


def test_backoff_grows_and_is_bounded(db_path):
    store = Store.open(db_path)
    _enqueue(store, action_key="a")
    transport = FailureInjectionTransport(
        [retryable_error_result("timeout") for _ in range(5)]
    )
    now = T0
    worker = DeliveryWorker(
        store=store,
        transport=transport,
        clock=FixedClock(now),
        max_attempts=10,
        base_backoff_seconds=10,
        max_backoff_seconds=25,
    )
    worker.run_once()
    row = store.get_outbox_by_action_key("a")
    first_delay = (row.available_at_utc - now).total_seconds()
    assert first_delay == 10

    now2 = row.available_at_utc
    worker2 = DeliveryWorker(
        store=store,
        transport=transport,
        clock=FixedClock(now2),
        max_attempts=10,
        base_backoff_seconds=10,
        max_backoff_seconds=25,
    )
    worker2.run_once()
    row2 = store.get_outbox_by_action_key("a")
    second_delay = (row2.available_at_utc - now2).total_seconds()
    assert second_delay == 20

    now3 = row2.available_at_utc
    worker3 = DeliveryWorker(
        store=store,
        transport=transport,
        clock=FixedClock(now3),
        max_attempts=10,
        base_backoff_seconds=10,
        max_backoff_seconds=25,
    )
    worker3.run_once()
    row3 = store.get_outbox_by_action_key("a")
    third_delay = (row3.available_at_utc - now3).total_seconds()
    assert third_delay == 25
    store.close()


def test_oversized_text_is_rejected_not_truncated(db_path):
    store = Store.open(db_path)
    store._conn.execute(
        "INSERT INTO outbox (action_key, episode_id, priority, chat_id, text, status,"
        " attempts, available_at_utc, lease_until_utc, last_error, receipt_id,"
        " created_at_utc, updated_at_utc)"
        " VALUES ('oversize', NULL, 0, '1', ?, 'queued', 0, ?, NULL, NULL, NULL, ?, ?)",
        ("x" * (MAX_OUTBOX_TEXT_LENGTH + 1), T0.isoformat(), T0.isoformat(), T0.isoformat()),
    )
    transport = CaptureTransport()
    worker = DeliveryWorker(store=store, transport=transport, clock=FixedClock(T0))

    outcomes = worker.run_once()

    assert outcomes[0].outcome == "failed"
    assert transport.calls == []
    row = store.get_outbox_by_action_key("oversize")
    assert row.status == "failed"
    assert "exceeds the maximum" in row.last_error
    store.close()


def test_worker_treats_lease_lost_on_mark_delivered_as_benign_not_a_crash(db_path):
    store = Store.open(db_path)
    _enqueue(store, action_key="a")
    store.claim_due(now_utc=T0, limit=1, lease_seconds=60)
    store.mark_delivered("a", receipt_id="tg-1", delivered_at_utc=T0)

    transport = CaptureTransport()
    worker = DeliveryWorker(store=store, transport=transport, clock=FixedClock(T0))
    outcome = worker._finish_delivered("a", receipt_id="tg-2")

    assert outcome.outcome == "lease_lost"
    row = store.get_outbox_by_action_key("a")
    assert row.receipt_id == "tg-1"
    store.close()


def test_worker_treats_lease_lost_on_mark_retry_and_mark_failed_as_benign(db_path):
    store = Store.open(db_path)
    _enqueue(store, action_key="a")
    _enqueue(store, action_key="b")
    store.claim_due(now_utc=T0, limit=2, lease_seconds=60)
    store.mark_failed("a", error="already resolved", now_utc=T0)
    store.mark_delivered("b", receipt_id="tg-1", delivered_at_utc=T0)

    transport = CaptureTransport()
    worker = DeliveryWorker(store=store, transport=transport, clock=FixedClock(T0))

    retry_outcome = worker._finish_retry("a", error="late", attempts=1)
    failed_outcome = worker._finish_failed("b", target="1", error="late")

    assert retry_outcome.outcome == "lease_lost"
    assert failed_outcome.outcome == "lease_lost"
    store.close()


def test_two_workers_do_not_both_deliver_the_same_row(db_path):
    store_setup = Store.open(db_path)
    _enqueue(store_setup, action_key="only-one")
    store_setup.close()

    barrier = threading.Barrier(2)
    errors = []
    delivered_counts = []
    lock = threading.Lock()

    def worker_run():
        store = Store.open(db_path)
        transport = CaptureTransport()
        worker = DeliveryWorker(store=store, transport=transport, clock=FixedClock(T0))
        try:
            barrier.wait(timeout=5)
            outcomes = worker.run_once()
            delivered = [o for o in outcomes if o.outcome == "delivered"]
            with lock:
                delivered_counts.append(len(delivered))
        except BaseException as exc:
            errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=worker_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert sorted(delivered_counts) == [0, 1]

    store = Store.open(db_path)
    row = store.get_outbox_by_action_key("only-one")
    assert row.status == "delivered"
    store.close()


def test_crash_before_send_recovers_and_delivers_later(db_path):
    store = Store.open(db_path)
    _enqueue(store, action_key="a")

    store.claim_due(now_utc=T0, limit=1, lease_seconds=60)
    row_mid_crash = store.get_outbox_by_action_key("a")
    assert row_mid_crash.status == "in_flight"

    transport = CaptureTransport()
    later = FixedClock(T0 + timedelta(seconds=61))
    worker = DeliveryWorker(store=store, transport=transport, clock=later, lease_seconds=60)

    outcomes = worker.run_once()

    assert len(outcomes) == 1
    assert outcomes[0].outcome == "delivered"
    assert len(transport.calls) == 1
    row = store.get_outbox_by_action_key("a")
    assert row.status == "delivered"
    store.close()


def test_crash_after_accepted_send_before_receipt_write_can_duplicate(db_path):
    store = Store.open(db_path)
    _enqueue(store, action_key="a")

    claimed = store.claim_due(now_utc=T0, limit=1, lease_seconds=60)
    assert len(claimed) == 1
    transport = CaptureTransport()
    first_result = transport.send(target="1", text="hello", idempotency_key="a")
    assert first_result.status == "delivered"

    later = FixedClock(T0 + timedelta(seconds=61))
    worker = DeliveryWorker(store=store, transport=transport, clock=later, lease_seconds=60)
    outcomes = worker.run_once()

    assert outcomes[0].outcome == "delivered"
    assert len(transport.calls) == 2
    assert transport.calls[0].idempotency_key == transport.calls[1].idempotency_key == "a"
    row = store.get_outbox_by_action_key("a")
    assert row.receipt_id == "capture-2"
    store.close()


def test_hermes_transport_success_captures_message_id():
    calls = []

    def fake_run(argv, *, input, capture_output, text, timeout, shell):
        calls.append((argv, input, timeout, shell))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='{"success": true, "platform": "telegram", "chat_id": "42", "message_id": "999"}',
            stderr="",
        )

    transport = HermesSendTransport(profile="telegram-family-assistant", run=fake_run)
    result = transport.send(target="42", text="hello there", idempotency_key="checkin:2026-09-12::prompt")

    assert result.status == "delivered"
    assert result.receipt_id == "999"
    assert len(calls) == 1
    argv, stdin_text, timeout, shell = calls[0]
    assert argv == [
        "hermes",
        "-p",
        "telegram-family-assistant",
        "send",
        "--to",
        "telegram:42",
        "--json",
    ]
    assert stdin_text == "hello there"
    assert shell is False
    assert timeout == 20


def test_hermes_transport_failure_payload_is_permanent_by_default():
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout='{"error": "Telegram send failed: Chat not found"}', stderr=""
        )

    transport = HermesSendTransport(profile="p", run=fake_run)
    result = transport.send(target="1", text="x", idempotency_key="k")

    assert result.status == "failed"
    assert result.retryable is False
    assert result.error == "Telegram send failed: Chat not found"


def test_hermes_transport_failure_payload_recognised_as_transient():
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout='{"error": "Telegram send failed: 429 Too Many Requests"}', stderr=""
        )

    transport = HermesSendTransport(profile="p", run=fake_run)
    result = transport.send(target="1", text="x", idempotency_key="k")

    assert result.status == "failed"
    assert result.retryable is True


def test_hermes_transport_timeout_is_retryable():
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    transport = HermesSendTransport(profile="p", timeout_seconds=5, run=fake_run)
    result = transport.send(target="1", text="x", idempotency_key="k")

    assert result.status == "failed"
    assert result.retryable is True
    assert "timed out" in result.error


def test_hermes_transport_missing_binary_is_retryable():
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("no such file: hermes")

    transport = HermesSendTransport(profile="p", run=fake_run)
    result = transport.send(target="1", text="x", idempotency_key="k")

    assert result.status == "failed"
    assert result.retryable is True
    assert "hermes binary not found" in result.error


def test_hermes_transport_usage_error_is_permanent():
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="usage: hermes send ...")

    transport = HermesSendTransport(profile="p", run=fake_run)
    result = transport.send(target="1", text="x", idempotency_key="k")

    assert result.status == "failed"
    assert result.retryable is False


def test_hermes_transport_never_uses_shell_with_hostile_text():
    calls = []

    def fake_run(argv, *, input, capture_output, text, timeout, shell):
        calls.append(input)
        assert isinstance(argv, list)
        assert shell is False
        return subprocess.CompletedProcess(
            argv, 0, stdout='{"success": true, "message_id": "1"}', stderr=""
        )

    hostile = "hello $(rm -rf /) `whoami` ; echo pwned && cat /etc/passwd\nline2"
    transport = HermesSendTransport(profile="p", run=fake_run)
    result = transport.send(target="1", text=hostile, idempotency_key="k")

    assert result.status == "delivered"
    assert calls == [hostile]


def test_hermes_transport_unicode_decode_error_is_retryable():
    def fake_run(argv, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    transport = HermesSendTransport(profile="p", run=fake_run)
    result = transport.send(target="1", text="x", idempotency_key="k")

    assert result.status == "failed"
    assert result.retryable is True


def test_neutralise_markup_round_trips_parent_verbatim_text():
    raw = "-no reply since 09:00. Please call *now* and check `status_flag` (urgent) [ref] !"

    escaped = _neutralise_markup(raw)

    assert escaped != raw
    for ch in "_*[]()~`>#+-=|{}.!\\":
        if ch in raw:
            assert f"\\{ch}" in escaped
    assert _strip_markup_neutralisation(escaped) == raw


def test_neutralise_markup_defeats_hermes_html_autodetection():
    raw = "check this <b>bold</b> tag"

    escaped = _neutralise_markup(raw)

    assert re.search(r"<[a-zA-Z/][^>]*>", escaped) is None
    assert _strip_markup_neutralisation(escaped) == raw


def test_outgoing_text_is_neutralised_before_reaching_transport(db_path):
    store = Store.open(db_path)
    hostile = "-no reply since 09:00. Please call *now* and check `status_flag` (urgent) [ref] <b>!"
    _enqueue(store, action_key="a", text=hostile, priority=PRIORITY_ESCALATION)
    transport = CaptureTransport()
    worker = DeliveryWorker(store=store, transport=transport, clock=FixedClock(T0))

    worker.run_once()

    sent = transport.calls[0].text
    assert sent != hostile
    assert re.search(r"<[a-zA-Z/][^>]*>", sent) is None
    assert _strip_markup_neutralisation(sent) == hostile
    store.close()


def test_permanent_failure_logs_error_with_action_key_target_and_error(db_path, caplog):
    store = Store.open(db_path)
    _enqueue(store, action_key="a", chat_id="555")
    transport = FailureInjectionTransport([permanent_error_result("Chat not found")])
    worker = DeliveryWorker(store=store, transport=transport, clock=FixedClock(T0))

    with caplog.at_level(logging.ERROR, logger="care.delivery"):
        outcomes = worker.run_once()

    assert outcomes[0].outcome == "failed"
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(error_records) == 1
    message = error_records[0].getMessage()
    assert "a" in message
    assert "555" in message
    assert "Chat not found" in message
    store.close()


def test_dry_run_delivery_mode_never_opens_operational_store(tmp_path):
    from care.store import open_store

    with pytest.raises(RuntimeError):
        open_store(tmp_path / "should-not-exist.db", delivery_mode="dry-run")
    assert not (tmp_path / "should-not-exist.db").exists()
