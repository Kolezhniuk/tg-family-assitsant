# Delivery worker (`care/delivery.py`)

## Transport contract

```python
class Transport:
    def send(self, target: str, text: str, idempotency_key: str) -> DeliveryResult: ...

@dataclass(frozen=True)
class DeliveryResult:
    status: str          # "delivered" or "failed"
    receipt_id: str | None = None
    error: str | None = None
    retryable: bool = False
```

`Transport.send` never raises for ordinary delivery failure. Every failure
mode a real transport can encounter (timeout, missing binary, backend
error, usage error) is converted to a typed `DeliveryResult` inside the
transport implementation itself, so `DeliveryWorker` never has to guess
what an exception meant.

Three transports exist:

- `CaptureTransport` — records every call in `.calls`, always "delivers"
  with an incrementing synthetic receipt id. Used to assert on what the
  worker actually tried to send.
- `FailureInjectionTransport` — takes a list of canned `DeliveryResult`
  values and returns them in order (repeating/defaulting to a delivered
  result once the plan is exhausted), so tests can script timeouts,
  retryable backend errors, and permanent errors without touching a
  network or a subprocess.
- `HermesSendTransport` — the real adapter. Builds
  `[hermes_binary, "-p", profile, "send", "--to", f"telegram:{target}", "--json"]`
  as an argument list (never a shell string) and passes `text` on stdin via
  `subprocess.run(argv, input=text, text=True, timeout=..., shell=False)`.
  The `run` callable is injectable so tests can fake `subprocess.run`
  without ever invoking a real `hermes` binary.

## Idempotency key: accepted, not usable

`idempotency_key` is part of the `Transport.send` signature so the
interface is ready for a transport that can use one. `HermesSendTransport`
accepts the parameter but does **not** pass it to the `hermes send`
command, because Task 0's contract evidence
(`docs/hermes-contract.md`, section 3) confirms `hermes send` has no such
flag. This is a verified fact about the installed binary, not an
oversight: passing a key that means nothing to the transport would create
false confidence.

## At-least-once delivery: the duplicate is real, not theoretical

Because there is no idempotency key, the crash window between "Telegram
accepted the message" and "we recorded the receipt" is a genuine duplicate
risk, not just a hypothetical one:

1. Worker claims a row (`Store.claim_due`, transactional, with a lease).
2. Worker calls `transport.send(...)` **outside** any transaction.
3. If the process dies here — after Telegram already delivered the
   message but before `Store.mark_delivered` runs — the row is still
   `in_flight` with a lease that will expire.
4. Once the lease expires, any worker (the same one restarted, or another
   one) claims the row again and sends it again. There is nothing
   downstream that can deduplicate this for `HermesSendTransport`, because
   Hermes/Telegram itself has no de-dup key to give us.

`tests/test_delivery.py::test_crash_after_accepted_send_before_receipt_write_can_duplicate`
demonstrates this directly: a send is accepted, the worker "crashes"
before calling `mark_delivered`, the lease expires, and a fresh
`DeliveryWorker.run_once()` reclaims and sends the same action a second
time — the test asserts two real send calls happened for the same
`action_key`, i.e. a duplicate parent/family message is the honest outcome
here, not a bug to hide. The design spec (section 13) explicitly prefers
this over silent loss: "duplicates are preferable to silent loss."

`care doctor` (a later task) is expected to surface "transport idempotency
support: none" so operators know this is the deployed behavior, not an
assumption.

## Retry policy

- `attempts` is incremented by `Store.claim_due` on every claim (including
  reclaims after a retry or an expired lease).
- A `DeliveryResult` with `retryable=True` becomes `mark_retry` with
  `available_at_utc = now + min(base * 2**(attempts-1), max_backoff)`,
  as long as `attempts < max_attempts`.
- A `DeliveryResult` with `retryable=False`, or a retryable one that has
  exhausted `max_attempts`, becomes `mark_failed` — a permanent, visible
  failure. Defaults: `max_attempts=6`, `base_backoff_seconds=30`,
  `max_backoff_seconds=1800` (30 minutes).
- `HermesSendTransport` classifies its own failures: a network timeout or
  missing binary is `retryable=True`; an exit-2 usage error is always
  `retryable=False` (it is our own invocation bug, retrying changes
  nothing); an exit-1 backend error is `retryable=True` only if its message
  contains a transient marker (rate limit/429/5xx/timeout wording),
  otherwise it is treated as permanent (e.g. "Chat not found").

## Priority

`PRIORITY_ESCALATION` / `PRIORITY_STOP_NOTICE` (100) outrank
`PRIORITY_ROUTINE` (0). `Store.claim_due` already orders candidates by
`priority DESC, available_at_utc ASC`, so a worker's claimed batch is
already escalation-first; `DeliveryWorker` processes the claimed list in
the order it was returned and never reorders it.

## Message size

`Store.enqueue_outbox` already refuses to insert text longer than
`MAX_OUTBOX_TEXT_LENGTH` (4096, Telegram's hard cap), so in normal
operation no row can ever be oversized. `DeliveryWorker` still checks
`len(row.text)` before calling the transport, as defense in depth (e.g.
against a future writer that bypasses `enqueue_outbox`, or a manually
edited row): an oversized row is marked permanently failed with a visible
error and is never truncated or split. Splitting logic was deliberately
not built, since no code path in this codebase can currently produce a
row that would need it.

## Dry-run isolation

`Store.open_store(path, delivery_mode="dry-run")` refuses to open the
operational database at all (raises `RuntimeError`) — this is a Task 2
guarantee that `DeliveryWorker` relies on rather than duplicates.
`DeliveryWorker` has no dry-run-specific branch: it is wired to whatever
`Store`/`Transport` it is given, so dry-run isolation is structural (a
dry-run caller simply never constructs a `DeliveryWorker` against the
operational store) rather than a runtime check inside the worker.
