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

## Outgoing text is neutralised before it reaches Hermes

`hermes send` has no plain-text delivery path. The installed adapter
(`send_message_tool.py`, `_send_telegram`) always applies a parse mode to
whatever text it is given:

- if the text matches `<[a-zA-Z/][^>]*>` it is sent with `parse_mode=HTML`;
- otherwise it is run through the gateway's markdown-to-MarkdownV2
  converter and sent with `parse_mode=MARKDOWN_V2`. If that conversion
  raises, the *original, unescaped* text is sent anyway, still under
  `MARKDOWN_V2`.

There is no flag to disable either behaviour. Our escalations and
check-in messages embed a verbatim quote of whatever the parent typed,
and MarkdownV2 gives special meaning to `_ * [ ] ( ) ~ \` > # + - = | { }
. !` (and to a literal backslash). An ordinary reply containing a full
stop, a bracket, or a dash is completely normal parent input, and left
unescaped it can make Telegram reject the send with a 400 — meaning the
message that fails to send is the escalation itself, so the family is
never told. Untouched `<...>`-shaped text can also be silently
reinterpreted as HTML instead of being shown literally.

`DeliveryWorker._process` calls `care.delivery._neutralise_markup` on
`row.text` before it is ever handed to `Transport.send`, so this holds
for every transport, not just `HermesSendTransport`:

1. Every occurrence of `<` gets a zero-width space (`​`) inserted
   immediately after it. This is invisible when rendered, but it means
   the text can never match Hermes's `<[a-zA-Z/][^>]*>` autodetection
   regex, so Hermes always takes the MarkdownV2 path — never the HTML
   one — regardless of what the parent typed.
2. Every MarkdownV2-reserved character, and a literal backslash, is
   escaped with a preceding backslash. Because this operates on the
   original character stream (not a re-scan of its own output), it
   cannot double-escape the backslashes it just inserted.

The net effect: whatever parse mode Hermes ends up using, the family
sees exactly the characters the parent typed (up to an invisible
zero-width space), and the payload we send Hermes is never invalid
MarkdownV2 that could cause Telegram to reject the whole send.

This escaping can inflate the length of `text` (each escaped character
becomes two). `MAX_OUTBOX_TEXT_LENGTH` enforcement happens on the raw,
pre-escaping length, so a message sitting very close to the 4096 cap
with heavy special-character density could still be rejected downstream
by Telegram after escaping; no code path in this codebase currently
produces text anywhere near that dense, so it is a known, narrow edge
rather than something actively guarded against here.

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
  missing binary is `retryable=True`; a `UnicodeDecodeError` while reading
  the subprocess's output (a `ValueError`, not an `OSError`, so it needs
  its own `except` clause) is also `retryable=True`; an exit-2 usage error
  is always `retryable=False` (it is our own invocation bug, retrying
  changes nothing); an exit-1 backend error is `retryable=True` only if its
  message contains a transient marker (rate limit/429/5xx/timeout
  wording), otherwise it is treated as permanent (e.g. "Chat not found").
- A permanent failure (`_finish_failed` succeeding, i.e. not a benign
  `LeaseLostError`) is logged at `logging.ERROR` with the action key,
  target chat id, and error string, so it is visible in process logs even
  before `care doctor` (a later task) can surface it as a first-class
  operator signal.

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
