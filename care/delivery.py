from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import timedelta

from care.clock import Clock
from care.models import OutboxRow
from care.store import MAX_OUTBOX_TEXT_LENGTH, LeaseLostError, Store

PRIORITY_ROUTINE = 0
PRIORITY_ESCALATION = 100
PRIORITY_STOP_NOTICE = 100

STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"

OUTCOME_DELIVERED = "delivered"
OUTCOME_RETRYING = "retrying"
OUTCOME_FAILED = "failed"
OUTCOME_LEASE_LOST = "lease_lost"

DEFAULT_LEASE_SECONDS = 60
DEFAULT_BATCH_LIMIT = 10
DEFAULT_MAX_ATTEMPTS = 6
DEFAULT_BASE_BACKOFF_SECONDS = 30
DEFAULT_MAX_BACKOFF_SECONDS = 1800
DEFAULT_SEND_TIMEOUT_SECONDS = 20

_TRANSIENT_MARKERS = (
    "timed out",
    "timeout",
    "rate limit",
    "too many requests",
    "429",
    "502",
    "503",
    "504",
    "bad gateway",
    "gateway timeout",
    "temporarily unavailable",
    "service unavailable",
)

_logger = logging.getLogger("care.delivery")


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    receipt_id: str | None = None
    error: str | None = None
    retryable: bool = False


class Transport:
    def send(self, target: str, text: str, idempotency_key: str) -> DeliveryResult:
        raise NotImplementedError


@dataclass
class SendCall:
    target: str
    text: str
    idempotency_key: str


class CaptureTransport(Transport):
    def __init__(self, *, receipt_prefix: str = "capture"):
        self.calls: list[SendCall] = []
        self._receipt_prefix = receipt_prefix

    def send(self, target: str, text: str, idempotency_key: str) -> DeliveryResult:
        self.calls.append(SendCall(target=target, text=text, idempotency_key=idempotency_key))
        return DeliveryResult(status=STATUS_DELIVERED, receipt_id=f"{self._receipt_prefix}-{len(self.calls)}")


class FailureInjectionTransport(Transport):
    def __init__(self, plan: list[DeliveryResult]):
        self._plan = list(plan)
        self.calls: list[SendCall] = []

    def send(self, target: str, text: str, idempotency_key: str) -> DeliveryResult:
        self.calls.append(SendCall(target=target, text=text, idempotency_key=idempotency_key))
        if self._plan:
            return self._plan.pop(0)
        return DeliveryResult(status=STATUS_DELIVERED, receipt_id=f"injected-{len(self.calls)}")


def timeout_result(detail: str = "transport timed out") -> DeliveryResult:
    return DeliveryResult(status=STATUS_FAILED, error=detail, retryable=True)


def retryable_error_result(detail: str) -> DeliveryResult:
    return DeliveryResult(status=STATUS_FAILED, error=detail, retryable=True)


def permanent_error_result(detail: str) -> DeliveryResult:
    return DeliveryResult(status=STATUS_FAILED, error=detail, retryable=False)


def _looks_transient(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


class HermesSendTransport(Transport):
    def __init__(
        self,
        *,
        profile: str,
        hermes_binary: str = "hermes",
        timeout_seconds: int = DEFAULT_SEND_TIMEOUT_SECONDS,
        run=subprocess.run,
    ):
        self._profile = profile
        self._hermes_binary = hermes_binary
        self._timeout_seconds = timeout_seconds
        self._run = run

    def send(self, target: str, text: str, idempotency_key: str) -> DeliveryResult:
        argv = [
            self._hermes_binary,
            "-p",
            self._profile,
            "send",
            "--to",
            f"telegram:{target}",
            "--json",
        ]
        try:
            proc = self._run(
                argv,
                input=text,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return timeout_result(f"hermes send timed out after {self._timeout_seconds}s")
        except FileNotFoundError as exc:
            return retryable_error_result(f"hermes binary not found: {exc}")
        except OSError as exc:
            return retryable_error_result(f"hermes send subprocess error: {exc}")

        if proc.returncode == 0:
            try:
                payload = json.loads(proc.stdout)
            except (json.JSONDecodeError, TypeError):
                return permanent_error_result(
                    f"hermes exited 0 with unparseable output: {proc.stdout!r}"
                )
            if not payload.get("success"):
                return permanent_error_result(
                    f"hermes exited 0 without a success payload: {proc.stdout!r}"
                )
            message_id = payload.get("message_id")
            return DeliveryResult(
                status=STATUS_DELIVERED,
                receipt_id=str(message_id) if message_id is not None else None,
            )

        if proc.returncode == 2:
            detail = proc.stderr.strip() or proc.stdout.strip() or "hermes usage error"
            return permanent_error_result(f"hermes usage error: {detail}")

        message = None
        try:
            payload = json.loads(proc.stdout)
            message = payload.get("error")
        except (json.JSONDecodeError, TypeError):
            pass
        if not message:
            message = proc.stdout.strip() or proc.stderr.strip() or f"hermes exited {proc.returncode}"
        return DeliveryResult(status=STATUS_FAILED, error=message, retryable=_looks_transient(message))


@dataclass(frozen=True)
class DeliveryAttemptOutcome:
    action_key: str
    outcome: str
    error: str | None = None


class DeliveryWorker:
    def __init__(
        self,
        *,
        store: Store,
        transport: Transport,
        clock: Clock,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        batch_limit: int = DEFAULT_BATCH_LIMIT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_backoff_seconds: int = DEFAULT_BASE_BACKOFF_SECONDS,
        max_backoff_seconds: int = DEFAULT_MAX_BACKOFF_SECONDS,
    ):
        self._store = store
        self._transport = transport
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._batch_limit = batch_limit
        self._max_attempts = max_attempts
        self._base_backoff_seconds = base_backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds

    def _backoff_seconds(self, attempts: int) -> int:
        exponent = max(attempts - 1, 0)
        return min(self._base_backoff_seconds * (2**exponent), self._max_backoff_seconds)

    def run_once(self) -> list[DeliveryAttemptOutcome]:
        claim_time = self._clock.now_utc()
        rows = self._store.claim_due(
            now_utc=claim_time, limit=self._batch_limit, lease_seconds=self._lease_seconds
        )
        outcomes = []
        for row in rows:
            outcomes.append(self._process(row))
        return outcomes

    def _process(self, row: OutboxRow) -> DeliveryAttemptOutcome:
        if len(row.text) > MAX_OUTBOX_TEXT_LENGTH:
            return self._finish_failed(
                row.action_key,
                error=(
                    f"outbox text length {len(row.text)} exceeds the maximum transportable "
                    f"length {MAX_OUTBOX_TEXT_LENGTH}; refusing to truncate"
                ),
            )

        result = self._transport.send(
            target=row.chat_id, text=row.text, idempotency_key=row.action_key
        )

        if result.status == STATUS_DELIVERED:
            return self._finish_delivered(row.action_key, receipt_id=result.receipt_id)

        error = result.error or "delivery failed with no error detail"
        if result.retryable and row.attempts < self._max_attempts:
            return self._finish_retry(row.action_key, error=error, attempts=row.attempts)

        return self._finish_failed(row.action_key, error=error)

    def _finish_delivered(self, action_key: str, *, receipt_id: str | None) -> DeliveryAttemptOutcome:
        try:
            self._store.mark_delivered(
                action_key, receipt_id=receipt_id, delivered_at_utc=self._clock.now_utc()
            )
        except LeaseLostError:
            _logger.info("lease already resolved by another worker: %s", action_key)
            return DeliveryAttemptOutcome(action_key=action_key, outcome=OUTCOME_LEASE_LOST)
        return DeliveryAttemptOutcome(action_key=action_key, outcome=OUTCOME_DELIVERED)

    def _finish_retry(self, action_key: str, *, error: str, attempts: int) -> DeliveryAttemptOutcome:
        now = self._clock.now_utc()
        backoff = self._backoff_seconds(attempts)
        try:
            self._store.mark_retry(
                action_key,
                error=error,
                available_at_utc=now + timedelta(seconds=backoff),
                now_utc=now,
            )
        except LeaseLostError:
            _logger.info("lease already resolved by another worker: %s", action_key)
            return DeliveryAttemptOutcome(action_key=action_key, outcome=OUTCOME_LEASE_LOST)
        return DeliveryAttemptOutcome(action_key=action_key, outcome=OUTCOME_RETRYING, error=error)

    def _finish_failed(self, action_key: str, *, error: str) -> DeliveryAttemptOutcome:
        try:
            self._store.mark_failed(action_key, error=error, now_utc=self._clock.now_utc())
        except LeaseLostError:
            _logger.info("lease already resolved by another worker: %s", action_key)
            return DeliveryAttemptOutcome(action_key=action_key, outcome=OUTCOME_LEASE_LOST)
        return DeliveryAttemptOutcome(action_key=action_key, outcome=OUTCOME_FAILED, error=error)
