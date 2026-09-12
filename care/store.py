from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

from care.errors import ConfigError
from care.models import (
    ConfigSnapshot,
    ConfigSourceFile,
    EventRecord,
    OutboxRow,
    UpdateEnvelope,
    UpdateOutcome,
    validate_episode_id,
)
from care.redaction import MAX_EXCERPT_LENGTH, contains_dosage_notation

SCHEMA_VERSION = 1
MAX_OUTBOX_TEXT_LENGTH = 4096
_BUSY_TIMEOUT_MS = 5000

_SCHEMA_STATEMENTS = """
CREATE TABLE IF NOT EXISTS config_snapshot (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    stored_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incoming_updates (
    update_id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL,
    chat_type TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    reply_to_message_id INTEGER,
    received_at_utc TEXT NOT NULL,
    processed_at_utc TEXT NOT NULL,
    result TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    local_day TEXT NOT NULL,
    kind TEXT NOT NULL,
    episode_id TEXT,
    subject TEXT,
    actor_id TEXT,
    source_update_id INTEGER,
    payload TEXT NOT NULL,
    FOREIGN KEY (source_update_id) REFERENCES incoming_updates(update_id)
);

CREATE INDEX IF NOT EXISTS idx_events_episode ON events(episode_id);
CREATE INDEX IF NOT EXISTS idx_events_source_update ON events(source_update_id);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_key TEXT NOT NULL UNIQUE,
    episode_id TEXT,
    priority INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at_utc TEXT NOT NULL,
    lease_until_utc TEXT,
    last_error TEXT,
    receipt_id TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outbox_claim ON outbox(status, available_at_utc);
"""


class ConfigSnapshotDriftError(Exception):
    pass


class LeaseLostError(Exception):
    pass


class UnknownActionKeyError(LeaseLostError):
    pass


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("naive datetimes are not accepted; pass a timezone-aware UTC datetime")
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _check_bounded_payload(value) -> None:
    if isinstance(value, str):
        if len(value) > MAX_EXCERPT_LENGTH:
            raise ValueError("event payload string exceeds bounded excerpt length")
        if contains_dosage_notation(value):
            raise ValueError("event payload contains unredacted dosage notation")
    elif isinstance(value, Mapping):
        for item in value.values():
            _check_bounded_payload(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_bounded_payload(item)


def _check_bounded_text_column(value: str | None, column: str) -> None:
    if value is None:
        return
    if len(value) > MAX_EXCERPT_LENGTH:
        raise ValueError(f"event {column} exceeds bounded excerpt length")
    if contains_dosage_notation(value):
        raise ValueError(f"event {column} contains unredacted dosage notation")


def ensure_secure_state_path(db_path: Path) -> None:
    directory = db_path.parent
    _ensure_directory_secure(directory)
    _ensure_file_secure(db_path)


def _ensure_directory_secure(directory: Path) -> None:
    if not directory.exists():
        try:
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"{directory}: cannot create state directory: {exc}") from exc
    if os.name != "posix":
        return
    try:
        st = directory.stat()
    except OSError as exc:
        raise ConfigError(f"{directory}: cannot stat state directory: {exc}") from exc
    if st.st_uid != os.getuid():
        raise ConfigError(f"{directory}: state directory is not owned by the current user")
    mode = stat.S_IMODE(st.st_mode)
    if mode != 0o700:
        try:
            os.chmod(directory, 0o700)
        except OSError as exc:
            raise ConfigError(
                f"{directory}: insecure permissions {oct(mode)} and could not be corrected: {exc}"
            ) from exc
        mode = stat.S_IMODE(directory.stat().st_mode)
        if mode != 0o700:
            raise ConfigError(f"{directory}: insecure permissions {oct(mode)} could not be corrected")


def _ensure_file_secure(path: Path) -> None:
    if not path.exists():
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        except OSError as exc:
            raise ConfigError(f"{path}: cannot create state database file: {exc}") from exc
    if os.name != "posix":
        return
    try:
        st = path.stat()
    except OSError as exc:
        raise ConfigError(f"{path}: cannot stat state database file: {exc}") from exc
    if st.st_uid != os.getuid():
        raise ConfigError(f"{path}: state database file is not owned by the current user")
    mode = stat.S_IMODE(st.st_mode)
    if mode != 0o600:
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            raise ConfigError(
                f"{path}: insecure permissions {oct(mode)} and could not be corrected: {exc}"
            ) from exc
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o600:
            raise ConfigError(f"{path}: insecure permissions {oct(mode)} could not be corrected")


def _compute_fingerprint(files: Mapping[str, Path]):
    entries = []
    contents: dict[str, str] = {}
    hasher = hashlib.sha256()
    for name in sorted(files):
        path = files[name]
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ConfigError(f"{path}: cannot read config source file: {exc}") from exc
        digest = hashlib.sha256(data).hexdigest()
        entries.append(ConfigSourceFile(name=name, sha256=digest))
        contents[name] = data.decode("utf-8")
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(data)
        hasher.update(b"\x00")
    return hasher.hexdigest(), tuple(entries), contents


def _decode_snapshot(fingerprint: str, payload_json: str, stored_at_iso: str) -> ConfigSnapshot:
    data = json.loads(payload_json)
    entries = tuple(ConfigSourceFile(name=item["name"], sha256=item["sha256"]) for item in data["files"])
    return ConfigSnapshot(
        fingerprint=fingerprint,
        files=entries,
        stored_at_utc=_parse_iso(stored_at_iso),
        raw_contents=MappingProxyType(dict(data["contents"])),
    )


class WriteTxn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def append_event(
        self,
        *,
        ts_utc: datetime,
        local_day: date,
        kind: str,
        payload: dict,
        episode_id: str | None = None,
        subject: str | None = None,
        actor_id: str | None = None,
        source_update_id: int | None = None,
    ) -> int:
        if not kind:
            raise ValueError("event kind must not be empty")
        if episode_id is not None:
            validate_episode_id(episode_id)
        _check_bounded_text_column(subject, "subject")
        _check_bounded_text_column(actor_id, "actor_id")
        _check_bounded_payload(payload)
        cursor = self._conn.execute(
            "INSERT INTO events"
            " (ts_utc, local_day, kind, episode_id, subject, actor_id, source_update_id, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(ts_utc),
                local_day.isoformat(),
                kind,
                episode_id,
                subject,
                actor_id,
                source_update_id,
                json.dumps(payload, sort_keys=True),
            ),
        )
        return cursor.lastrowid

    def enqueue_outbox(
        self,
        *,
        action_key: str,
        chat_id: str,
        text: str,
        priority: int,
        available_at_utc: datetime,
        created_at_utc: datetime,
        episode_id: str | None = None,
    ) -> OutboxRow:
        if not action_key:
            raise ValueError("action_key must not be empty")
        if episode_id is not None:
            validate_episode_id(episode_id)
        if len(text) > MAX_OUTBOX_TEXT_LENGTH:
            raise ValueError("outbox text exceeds the maximum transportable message length")
        if contains_dosage_notation(text):
            raise ValueError("outbox text contains unredacted dosage notation")

        existing = self._conn.execute(
            "SELECT * FROM outbox WHERE action_key = ?", (action_key,)
        ).fetchone()
        if existing is not None:
            return _row_to_outbox(existing)

        created_iso = _iso(created_at_utc)
        self._conn.execute(
            "INSERT INTO outbox"
            " (action_key, episode_id, priority, chat_id, text, status, attempts,"
            "  available_at_utc, lease_until_utc, last_error, receipt_id, created_at_utc, updated_at_utc)"
            " VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, NULL, NULL, NULL, ?, ?)",
            (
                action_key,
                episode_id,
                priority,
                chat_id,
                text,
                _iso(available_at_utc),
                created_iso,
                created_iso,
            ),
        )
        row = self._conn.execute("SELECT * FROM outbox WHERE action_key = ?", (action_key,)).fetchone()
        return _row_to_outbox(row)


def _row_to_outbox(row: sqlite3.Row) -> OutboxRow:
    return OutboxRow(
        id=row["id"],
        action_key=row["action_key"],
        episode_id=row["episode_id"],
        priority=row["priority"],
        chat_id=row["chat_id"],
        text=row["text"],
        status=row["status"],
        attempts=row["attempts"],
        available_at_utc=_parse_iso(row["available_at_utc"]),
        lease_until_utc=_parse_iso(row["lease_until_utc"]),
        last_error=row["last_error"],
        receipt_id=row["receipt_id"],
        created_at_utc=_parse_iso(row["created_at_utc"]),
        updated_at_utc=_parse_iso(row["updated_at_utc"]),
    )


def _row_to_event(row: sqlite3.Row) -> EventRecord:
    return EventRecord(
        id=row["id"],
        ts_utc=_parse_iso(row["ts_utc"]),
        local_day=date.fromisoformat(row["local_day"]),
        kind=row["kind"],
        episode_id=row["episode_id"],
        subject=row["subject"],
        actor_id=row["actor_id"],
        source_update_id=row["source_update_id"],
        payload=MappingProxyType(json.loads(row["payload"])),
    )


class Store:
    def __init__(self, connection: sqlite3.Connection):
        self._conn = connection

    @classmethod
    def open(cls, db_path: Path | str, *, delivery_mode: str | None = None) -> "Store":
        if delivery_mode == "dry-run":
            raise RuntimeError("dry-run delivery mode must not open the operational database")
        is_memory = str(db_path) == ":memory:"
        if not is_memory:
            db_path = Path(db_path)
            ensure_secure_state_path(db_path)
        conn = sqlite3.connect(
            str(db_path),
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=True,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if not is_memory:
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        store = cls(conn)
        store._migrate()
        return store

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _migrate(self) -> None:
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise ConfigError(
                f"database schema version {current} is newer than supported version {SCHEMA_VERSION}"
            )
        self._conn.executescript(_SCHEMA_STATEMENTS)
        if current < SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _safe_rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

    @contextmanager
    def transaction(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield WriteTxn(self._conn)
        except Exception:
            self._safe_rollback()
            raise
        else:
            self._conn.execute("COMMIT")

    def record_update(
        self,
        envelope: UpdateEnvelope,
        handler: Callable[[WriteTxn], dict],
        *,
        processed_at_utc: datetime,
    ) -> UpdateOutcome:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT result FROM incoming_updates WHERE update_id = ?", (envelope.update_id,)
            ).fetchone()
            if row is not None:
                self._conn.execute("COMMIT")
                return UpdateOutcome(replay=True, result=json.loads(row["result"]))

            txn = WriteTxn(self._conn)
            result = handler(txn)
            result_json = json.dumps(result, sort_keys=True)
            self._conn.execute(
                "INSERT INTO incoming_updates"
                " (update_id, chat_id, chat_type, sender_id, message_id, reply_to_message_id,"
                "  received_at_utc, processed_at_utc, result)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    envelope.update_id,
                    envelope.chat_id,
                    envelope.chat_type,
                    envelope.sender_id,
                    envelope.message_id,
                    envelope.reply_to_message_id,
                    _iso(envelope.received_at_utc),
                    _iso(processed_at_utc),
                    result_json,
                ),
            )
            self._conn.execute("COMMIT")
            return UpdateOutcome(replay=False, result=result)
        except Exception:
            self._safe_rollback()
            raise

    def claim_due(self, *, now_utc: datetime, limit: int, lease_seconds: int) -> list[OutboxRow]:
        now_iso = _iso(now_utc)
        lease_until_iso = _iso(now_utc + timedelta(seconds=lease_seconds))
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            candidates = self._conn.execute(
                "SELECT id FROM outbox"
                " WHERE status IN ('queued', 'retrying', 'in_flight')"
                " AND available_at_utc <= ?"
                " AND (lease_until_utc IS NULL OR lease_until_utc <= ?)"
                " ORDER BY priority DESC, available_at_utc ASC"
                " LIMIT ?",
                (now_iso, now_iso, limit),
            ).fetchall()
            claimed: list[OutboxRow] = []
            for candidate in candidates:
                self._conn.execute(
                    "UPDATE outbox SET status = 'in_flight', attempts = attempts + 1,"
                    " lease_until_utc = ?, updated_at_utc = ? WHERE id = ?",
                    (lease_until_iso, now_iso, candidate["id"]),
                )
                row = self._conn.execute("SELECT * FROM outbox WHERE id = ?", (candidate["id"],)).fetchone()
                claimed.append(_row_to_outbox(row))
            self._conn.execute("COMMIT")
            return claimed
        except Exception:
            self._safe_rollback()
            raise

    def _raise_lease_lost(self, action_key: str, *, action: str) -> None:
        row = self._conn.execute(
            "SELECT 1 FROM outbox WHERE action_key = ?", (action_key,)
        ).fetchone()
        if row is None:
            raise UnknownActionKeyError(f"{action_key}: no outbox row with this action key; cannot {action}")
        raise LeaseLostError(f"{action_key}: not currently in_flight; cannot {action}")

    def mark_delivered(self, action_key: str, *, receipt_id: str, delivered_at_utc: datetime) -> None:
        cursor = self._conn.execute(
            "UPDATE outbox SET status = 'delivered', receipt_id = ?, last_error = NULL,"
            " lease_until_utc = NULL, updated_at_utc = ?"
            " WHERE action_key = ? AND status = 'in_flight'",
            (receipt_id, _iso(delivered_at_utc), action_key),
        )
        if cursor.rowcount != 1:
            self._raise_lease_lost(action_key, action="mark delivered")

    def mark_retry(
        self, action_key: str, *, error: str, available_at_utc: datetime, now_utc: datetime
    ) -> None:
        cursor = self._conn.execute(
            "UPDATE outbox SET status = 'retrying', last_error = ?, lease_until_utc = NULL,"
            " available_at_utc = ?, updated_at_utc = ?"
            " WHERE action_key = ? AND status = 'in_flight'",
            (error, _iso(available_at_utc), _iso(now_utc), action_key),
        )
        if cursor.rowcount != 1:
            self._raise_lease_lost(action_key, action="mark retrying")

    def mark_failed(self, action_key: str, *, error: str, now_utc: datetime) -> None:
        cursor = self._conn.execute(
            "UPDATE outbox SET status = 'failed', last_error = ?, lease_until_utc = NULL,"
            " updated_at_utc = ?"
            " WHERE action_key = ? AND status = 'in_flight'",
            (error, _iso(now_utc), action_key),
        )
        if cursor.rowcount != 1:
            self._raise_lease_lost(action_key, action="mark failed")

    def get_outbox_by_action_key(self, action_key: str) -> OutboxRow | None:
        row = self._conn.execute("SELECT * FROM outbox WHERE action_key = ?", (action_key,)).fetchone()
        return _row_to_outbox(row) if row is not None else None

    def list_events(self, *, episode_id: str | None = None) -> list[EventRecord]:
        if episode_id is None:
            rows = self._conn.execute("SELECT * FROM events ORDER BY id ASC").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE episode_id = ? ORDER BY id ASC", (episode_id,)
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def list_outbox(self, *, episode_id: str | None = None) -> list[OutboxRow]:
        if episode_id is None:
            rows = self._conn.execute("SELECT * FROM outbox ORDER BY id ASC").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM outbox WHERE episode_id = ? ORDER BY id ASC", (episode_id,)
            ).fetchall()
        return [_row_to_outbox(row) for row in rows]

    def current_config_snapshot(self) -> ConfigSnapshot | None:
        row = self._conn.execute(
            "SELECT fingerprint, payload, stored_at_utc FROM config_snapshot WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return _decode_snapshot(row["fingerprint"], row["payload"], row["stored_at_utc"])

    def initialize_config_snapshot(
        self, files: Mapping[str, Path], *, stored_at_utc: datetime
    ) -> ConfigSnapshot:
        fingerprint, entries, contents = _compute_fingerprint(files)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT fingerprint, payload, stored_at_utc FROM config_snapshot WHERE id = 1"
            ).fetchone()
            if row is not None:
                if row["fingerprint"] != fingerprint:
                    self._safe_rollback()
                    raise ConfigSnapshotDriftError(
                        "stored configuration snapshot differs from current source files; "
                        "an explicit config-import/reconciliation is required"
                    )
                self._conn.execute("COMMIT")
                return _decode_snapshot(row["fingerprint"], row["payload"], row["stored_at_utc"])

            payload = json.dumps(
                {
                    "files": [{"name": e.name, "sha256": e.sha256} for e in entries],
                    "contents": contents,
                },
                sort_keys=True,
            )
            self._conn.execute(
                "INSERT INTO config_snapshot (id, fingerprint, payload, stored_at_utc) VALUES (1, ?, ?, ?)",
                (fingerprint, payload, _iso(stored_at_utc)),
            )
            self._conn.execute("COMMIT")
            return ConfigSnapshot(
                fingerprint=fingerprint,
                files=entries,
                stored_at_utc=stored_at_utc,
                raw_contents=MappingProxyType(contents),
            )
        except ConfigSnapshotDriftError:
            raise
        except Exception:
            self._safe_rollback()
            raise

    def reconcile_config_snapshot(
        self, files: Mapping[str, Path], *, stored_at_utc: datetime, actor_id: str | None = None
    ) -> ConfigSnapshot:
        fingerprint, entries, contents = _compute_fingerprint(files)
        _check_bounded_text_column(actor_id, "actor_id")
        payload = json.dumps(
            {
                "files": [{"name": e.name, "sha256": e.sha256} for e in entries],
                "contents": contents,
            },
            sort_keys=True,
        )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            previous = self._conn.execute(
                "SELECT fingerprint FROM config_snapshot WHERE id = 1"
            ).fetchone()
            previous_fingerprint = previous["fingerprint"] if previous is not None else None
            self._conn.execute(
                "INSERT INTO config_snapshot (id, fingerprint, payload, stored_at_utc) VALUES (1, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "  fingerprint = excluded.fingerprint,"
                "  payload = excluded.payload,"
                "  stored_at_utc = excluded.stored_at_utc",
                (fingerprint, payload, _iso(stored_at_utc)),
            )
            event_payload = {"previous_fingerprint": previous_fingerprint, "new_fingerprint": fingerprint}
            self._conn.execute(
                "INSERT INTO events"
                " (ts_utc, local_day, kind, episode_id, subject, actor_id, source_update_id, payload)"
                " VALUES (?, ?, 'config_reconciled', NULL, NULL, ?, NULL, ?)",
                (
                    _iso(stored_at_utc),
                    stored_at_utc.astimezone(timezone.utc).date().isoformat(),
                    actor_id,
                    json.dumps(event_payload, sort_keys=True),
                ),
            )
            self._conn.execute("COMMIT")
            return ConfigSnapshot(
                fingerprint=fingerprint,
                files=entries,
                stored_at_utc=stored_at_utc,
                raw_contents=MappingProxyType(contents),
            )
        except Exception:
            self._safe_rollback()
            raise


def open_store(db_path: Path, *, delivery_mode: str) -> Store:
    return Store.open(db_path, delivery_mode=delivery_mode)
