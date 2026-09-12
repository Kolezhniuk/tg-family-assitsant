import os
import sqlite3
import stat
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from care.errors import ConfigError
from care.models import (
    UpdateEnvelope,
    checkin_episode_id,
    clarify_episode_id,
    dose_episode_id,
    escalation_episode_id,
    make_action_key,
    parse_episode_id,
    validate_episode_id,
)
from care.store import (
    ConfigSnapshotDriftError,
    LeaseLostError,
    Store,
    ensure_secure_state_path,
    open_store,
)

UTC = timezone.utc


def _envelope(update_id: int, *, text: str = "hello", reply_to=None) -> UpdateEnvelope:
    return UpdateEnvelope(
        update_id=update_id,
        chat_id="1",
        chat_type="private",
        sender_id="1",
        message_id=1000 + update_id,
        reply_to_message_id=reply_to,
        received_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
        text=text,
    )


def _handler_factory(calls: list):
    def handler(txn):
        calls.append(1)
        event_id = txn.append_event(
            ts_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
            local_day=date(2026, 9, 12),
            kind="checkin_prompt_sent",
            payload={"note": "ok"},
            episode_id=checkin_episode_id(date(2026, 9, 12)),
        )
        row = txn.enqueue_outbox(
            action_key=make_action_key(checkin_episode_id(date(2026, 9, 12)), "prompt"),
            chat_id="1",
            text="Доброго ранку!",
            priority=0,
            available_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
            created_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
            episode_id=checkin_episode_id(date(2026, 9, 12)),
        )
        return {"event_id": event_id, "outbox_id": row.id}

    return handler


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "state" / "care-state.db"


def test_migration_creates_schema_and_is_idempotent(db_path):
    store = Store.open(db_path)
    version = store._conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 1
    tables = {
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"incoming_updates", "events", "outbox", "config_snapshot"} <= tables
    store.close()

    store2 = Store.open(db_path)
    assert store2._conn.execute("PRAGMA user_version").fetchone()[0] == 1
    store2.close()


def test_state_directory_and_file_permissions(db_path):
    Store.open(db_path).close()
    if os.name != "posix":
        pytest.skip("permission bits are not meaningful on this platform")
    dir_mode = stat.S_IMODE(db_path.parent.stat().st_mode)
    file_mode = stat.S_IMODE(db_path.stat().st_mode)
    assert dir_mode == 0o700
    assert file_mode == 0o600


def test_insecure_directory_permissions_are_corrected(db_path):
    db_path.parent.mkdir(parents=True)
    os.chmod(db_path.parent, 0o755)
    if os.name != "posix":
        pytest.skip("permission bits are not meaningful on this platform")
    Store.open(db_path).close()
    assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_insecure_existing_file_permissions_are_corrected(db_path):
    db_path.parent.mkdir(parents=True, mode=0o700)
    db_path.touch()
    os.chmod(db_path, 0o644)
    if os.name != "posix":
        pytest.skip("permission bits are not meaningful on this platform")
    ensure_secure_state_path(db_path)
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_open_store_rejects_dry_run_and_touches_nothing(db_path):
    with pytest.raises(RuntimeError):
        open_store(db_path, delivery_mode="dry-run")
    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_open_store_live_creates_database(db_path):
    store = open_store(db_path, delivery_mode="live")
    try:
        assert db_path.exists()
    finally:
        store.close()


def test_record_update_first_time_runs_handler_and_persists(db_path):
    store = Store.open(db_path)
    calls: list = []
    outcome = store.record_update(
        _envelope(1),
        _handler_factory(calls),
        processed_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
    )
    assert outcome.replay is False
    assert len(calls) == 1
    events = store.list_events()
    assert len(events) == 1
    assert store.get_outbox_by_action_key(
        make_action_key(checkin_episode_id(date(2026, 9, 12)), "prompt")
    ) is not None
    store.close()


def test_replaying_update_returns_prior_result_with_no_new_effects(db_path):
    store = Store.open(db_path)
    calls: list = []
    handler = _handler_factory(calls)
    first = store.record_update(
        _envelope(1), handler, processed_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
    )
    second = store.record_update(
        _envelope(1), handler, processed_at_utc=datetime(2026, 9, 12, 9, 5, tzinfo=UTC)
    )
    assert first.replay is False
    assert second.replay is True
    assert second.result == first.result
    assert len(calls) == 1
    assert len(store.list_events()) == 1
    action_key = make_action_key(checkin_episode_id(date(2026, 9, 12)), "prompt")
    rows = store._conn.execute(
        "SELECT COUNT(*) FROM outbox WHERE action_key = ?", (action_key,)
    ).fetchone()[0]
    assert rows == 1
    store.close()


def test_failed_transaction_leaves_no_half_event_or_outbox_row(db_path):
    store = Store.open(db_path)

    def handler(txn):
        txn.append_event(
            ts_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
            local_day=date(2026, 9, 12),
            kind="checkin_prompt_sent",
            payload={"note": "ok"},
            episode_id=checkin_episode_id(date(2026, 9, 12)),
        )
        raise RuntimeError("boom before outbox insert")

    with pytest.raises(RuntimeError):
        store.record_update(
            _envelope(1), handler, processed_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
        )

    assert store.list_events() == []
    row = store._conn.execute(
        "SELECT COUNT(*) FROM incoming_updates WHERE update_id = ?", (1,)
    ).fetchone()[0]
    assert row == 0
    assert store._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    store.close()


def test_append_event_rejects_overlong_payload_string(db_path):
    store = Store.open(db_path)
    with store.transaction() as txn, pytest.raises(ValueError):
        txn.append_event(
            ts_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
            local_day=date(2026, 9, 12),
            kind="k",
            payload={"quote": "x" * 300},
        )
    store.close()


def test_append_event_rejects_unredacted_dosage_notation(db_path):
    store = Store.open(db_path)
    with store.transaction() as txn, pytest.raises(ValueError):
        txn.append_event(
            ts_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
            local_day=date(2026, 9, 12),
            kind="k",
            payload={"quote": "take 5 mg now"},
        )
    store.close()


def test_enqueue_outbox_rejects_dosage_notation_text(db_path):
    store = Store.open(db_path)
    with store.transaction() as txn, pytest.raises(ValueError):
        txn.enqueue_outbox(
            action_key="a",
            chat_id="1",
            text="take 5 mg now",
            priority=0,
            available_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
            created_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
        )
    store.close()


def test_enqueue_outbox_is_idempotent_for_same_action_key(db_path):
    store = Store.open(db_path)
    with store.transaction() as txn:
        first = txn.enqueue_outbox(
            action_key="a",
            chat_id="1",
            text="hello",
            priority=0,
            available_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
            created_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
        )
    with store.transaction() as txn:
        second = txn.enqueue_outbox(
            action_key="a",
            chat_id="1",
            text="hello again",
            priority=5,
            available_at_utc=datetime(2026, 9, 12, 10, 0, tzinfo=UTC),
            created_at_utc=datetime(2026, 9, 12, 10, 0, tzinfo=UTC),
        )
    assert first.id == second.id
    assert second.text == "hello"
    assert store._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    store.close()


def test_claim_due_marks_in_flight_and_hides_until_lease_expires(db_path):
    store = Store.open(db_path)
    with store.transaction() as txn:
        txn.enqueue_outbox(
            action_key="a",
            chat_id="1",
            text="hello",
            priority=0,
            available_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
            created_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
        )
    now = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
    claimed = store.claim_due(now_utc=now, limit=10, lease_seconds=60)
    assert len(claimed) == 1
    assert claimed[0].status == "in_flight"

    still_leased = store.claim_due(
        now_utc=now + timedelta(seconds=30), limit=10, lease_seconds=60
    )
    assert still_leased == []

    reclaimed = store.claim_due(
        now_utc=now + timedelta(seconds=61), limit=10, lease_seconds=60
    )
    assert len(reclaimed) == 1
    assert reclaimed[0].attempts == 2
    store.close()


def test_mark_delivered_requires_in_flight(db_path):
    store = Store.open(db_path)
    with store.transaction() as txn:
        txn.enqueue_outbox(
            action_key="a",
            chat_id="1",
            text="hello",
            priority=0,
            available_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
            created_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
        )
    with pytest.raises(LeaseLostError):
        store.mark_delivered(
            "a", receipt_id="tg-1", delivered_at_utc=datetime(2026, 9, 12, 9, 1, tzinfo=UTC)
        )

    store.claim_due(now_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC), limit=1, lease_seconds=60)
    store.mark_delivered(
        "a", receipt_id="tg-1", delivered_at_utc=datetime(2026, 9, 12, 9, 1, tzinfo=UTC)
    )
    row = store.get_outbox_by_action_key("a")
    assert row.status == "delivered"
    assert row.receipt_id == "tg-1"

    with pytest.raises(LeaseLostError):
        store.mark_delivered(
            "a", receipt_id="tg-1", delivered_at_utc=datetime(2026, 9, 12, 9, 1, tzinfo=UTC)
        )
    store.close()


def test_mark_retry_and_mark_failed(db_path):
    store = Store.open(db_path)
    with store.transaction() as txn:
        txn.enqueue_outbox(
            action_key="a",
            chat_id="1",
            text="hello",
            priority=0,
            available_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
            created_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
        )
    store.claim_due(now_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC), limit=1, lease_seconds=60)
    store.mark_retry(
        "a",
        error="timeout",
        available_at_utc=datetime(2026, 9, 12, 9, 5, tzinfo=UTC),
        now_utc=datetime(2026, 9, 12, 9, 1, tzinfo=UTC),
    )
    row = store.get_outbox_by_action_key("a")
    assert row.status == "retrying"
    assert row.last_error == "timeout"

    store.claim_due(now_utc=datetime(2026, 9, 12, 9, 5, tzinfo=UTC), limit=1, lease_seconds=60)
    store.mark_failed("a", error="permanent", now_utc=datetime(2026, 9, 12, 9, 6, tzinfo=UTC))
    row = store.get_outbox_by_action_key("a")
    assert row.status == "failed"
    assert row.last_error == "permanent"
    store.close()


def test_episode_id_helpers_round_trip():
    checkin = checkin_episode_id(date(2026, 9, 12))
    assert checkin == "checkin:2026-09-12"
    kind, parts = parse_episode_id(checkin)
    assert kind == "checkin"
    assert parts["local_day"] == "2026-09-12"

    dose = dose_episode_id("morning-pills", date(2026, 9, 12))
    assert parse_episode_id(dose)[0] == "dose"

    clarify = clarify_episode_id(555)
    assert parse_episode_id(clarify)[1]["source_message_id"] == "555"

    escalation = escalation_episode_id(make_action_key(checkin, "escalation"))
    assert parse_episode_id(escalation)[0] == "escalation"

    with pytest.raises(ValueError):
        validate_episode_id("not-an-episode")

    with pytest.raises(ValueError):
        dose_episode_id("bad:id", date(2026, 9, 12))


def test_config_snapshot_initialize_and_drift(tmp_path, db_path):
    roster = tmp_path / "roster.yaml"
    roster.write_text("timezone: Europe/Kyiv\n", encoding="utf-8")
    files = {"roster.yaml": roster}

    store = Store.open(db_path)
    snap = store.initialize_config_snapshot(
        files, stored_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
    )
    assert len(snap.files) == 1

    same = store.initialize_config_snapshot(
        files, stored_at_utc=datetime(2026, 9, 12, 9, 30, tzinfo=UTC)
    )
    assert same.fingerprint == snap.fingerprint
    assert same.stored_at_utc == snap.stored_at_utc

    roster.write_text("timezone: Europe/Warsaw\n", encoding="utf-8")
    with pytest.raises(ConfigSnapshotDriftError):
        store.initialize_config_snapshot(
            files, stored_at_utc=datetime(2026, 9, 12, 10, 0, tzinfo=UTC)
        )

    reconciled = store.reconcile_config_snapshot(
        files, stored_at_utc=datetime(2026, 9, 12, 10, 5, tzinfo=UTC), actor_id="family:2"
    )
    assert reconciled.fingerprint != snap.fingerprint
    events = store.list_events()
    assert any(e.kind == "config_reconciled" for e in events)
    store.close()


def test_two_connections_cannot_create_duplicate_action_keys(db_path):
    Store.open(db_path).close()
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker():
        store = Store.open(db_path)
        try:
            barrier.wait(timeout=5)
            with store.transaction() as txn:
                txn.enqueue_outbox(
                    action_key="race-key",
                    chat_id="1",
                    text="hello",
                    priority=0,
                    available_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
                    created_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
                )
        except BaseException as exc:
            errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    store = Store.open(db_path)
    count = store._conn.execute(
        "SELECT COUNT(*) FROM outbox WHERE action_key = ?", ("race-key",)
    ).fetchone()[0]
    assert count == 1
    store.close()


def test_two_connections_cannot_duplicate_replayed_update_results(db_path):
    Store.open(db_path).close()
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    replay_flags: list[bool] = []
    lock = threading.Lock()

    def worker():
        store = Store.open(db_path)
        calls: list = []
        try:
            barrier.wait(timeout=5)
            outcome = store.record_update(
                _envelope(42),
                _handler_factory(calls),
                processed_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
            )
            with lock:
                replay_flags.append(outcome.replay)
        except BaseException as exc:
            errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert sorted(replay_flags) == [False, True]

    store = Store.open(db_path)
    assert len(store.list_events()) == 1
    assert store._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    store.close()


def test_two_workers_cannot_claim_the_same_outbox_row(db_path):
    store = Store.open(db_path)
    with store.transaction() as txn:
        txn.enqueue_outbox(
            action_key="only-one",
            chat_id="1",
            text="hello",
            priority=0,
            available_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
            created_at_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
        )
    store.close()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    claimed_counts: list[int] = []
    lock = threading.Lock()

    def worker():
        store = Store.open(db_path)
        try:
            barrier.wait(timeout=5)
            claimed = store.claim_due(
                now_utc=datetime(2026, 9, 12, 9, 0, tzinfo=UTC), limit=10, lease_seconds=60
            )
            with lock:
                claimed_counts.append(len(claimed))
        except BaseException as exc:
            errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert sorted(claimed_counts) == [0, 1]
