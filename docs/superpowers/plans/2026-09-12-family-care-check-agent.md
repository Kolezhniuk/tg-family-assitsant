# Family Care-Check Agent Implementation Plan

> Status: revised after architecture, safety, privacy, reliability, and operations review. Do not execute the superseded code snippets from the previous version.

**Goal:** Build a Telegram care-check system for one parent whose timers, inbound tripwires, authorization, persistence, and delivery truth are deterministic. The model may add warmth or one clarification but cannot suppress or fabricate a safety action.

**Source of truth:** `docs/superpowers/specs/2026-09-12-family-care-check-agent-design.md`.

**Tech stack:** Python 3.12, standard library, PyYAML, SQLite, pytest. Hermes supplies Telegram integration after its installed contracts are verified. No framework, ORM, scheduler library, or arbitrary terminal access for the conversational agent.

## 1. Execution rules

- Implement in vertical safety slices, not by copying old task snippets.
- Keep one public contract between domain services and the Hermes adapter.
- Domain modules never import Hermes or invoke a model.
- All state transitions and outbox inserts that belong together use one SQLite transaction.
- All outgoing messages, including control notices and stand-downs, use the outbox.
- Trusted identity comes only from adapter metadata.
- Message text enters through typed calls or stdin/JSON stdin, never shell interpolation.
- Dry-run never opens or mutates the operational database.
- No configured dosage amounts; inbound dosage-like values are redacted before care persistence or forwarding.
- Escalations ignore quiet hours and are never suppressed by model or parent text.
- Add only dependencies explicitly approved in the design. Prefer standard library.
- Use comments only for a non-obvious invariant or external contract; do not narrate ordinary code.
- Every non-trivial transition gets the smallest test that would fail if it regressed.
- Do not mark an external Hermes or Telegram gate passed from mocks alone.

## 2. Planned repository shape

```text
care/
  __init__.py
  __main__.py
  clock.py                 local schedule and DST policy
  config.py                complete startup validation
  redaction.py             dosage-like and output-length redaction
  triage.py                deterministic term matching
  models.py                envelopes, episodes, actions, directives
  store.py                 schema, transactions, dedupe, events, outbox
  checkins.py              check-in state machine
  meds.py                  dose state machine and adherence
  conversation.py          reply directives and clarify budget
  controls.py              typed authorized mutations
  service.py               transaction coordinator
  delivery.py              outbox worker and transport protocol
  readmodels.py            status, log, doctor, purge
  cli.py                   operator/test commands
  hermes_adapter.py        verified Hermes boundary only
config/
  roster.example.yaml
  meds.example.yaml
  tripwire.{uk,en}.yaml
  affirmatives.{uk,en}.yaml
  negatives.{uk,en}.yaml
  messages.{uk,en}.yaml
profile/
  SOUL.md
  config-fragment.yaml
skills/family-care/SKILL.md
scripts/
  care-run.sh
  install-profile.sh
tests/
  ...
docs/
  operations.md
  escalation-policy.md
  message-catalogue.md
```

The exact split may shrink when adjacent files remain trivial. Do not merge the Hermes adapter into domain policy or split a module merely to preserve this diagram.

## 3. Requirement-to-task map

| Design concern | Task |
|---|---|
| Hermes pre-model and send contract | 0, 10 |
| Clock, windows, quiet hours, DST | 1 |
| Config, catalogues, dosage rejection | 1 |
| Privacy redaction and retention | 1, 2, 11 |
| Update replay protection and episodes | 2, 4, 6 |
| Durable outbox and delivery truth | 2, 3 |
| Check-in ladder | 4 |
| Medication reminders/confirmation | 5 |
| Tripwire and clarification | 6, 7 |
| Authorization and typed controls | 8 |
| CLI, status, doctor, purge | 9 |
| Hermes/profile integration | 10 |
| Failure/scenario acceptance | 11 |
| Operations and family documentation | 12 |

## Task 0 — Verify Hermes contracts before coding the adapter

**Purpose:** Resolve the external integration assumptions that determine whether the architecture is possible.

**May create:** `docs/hermes-contract.md` containing observed commands, versions, sample redacted envelopes, and delivery results. Do not claim support that was not observed.

- [ ] Record installed Hermes version and exact profile/config locations.
- [ ] Prove or disprove a pre-model Telegram update hook/plugin that exposes update, chat, sender, message, reply-to, text, and timestamp metadata.
- [ ] Verify how a deterministic directive is passed to later model handling.
- [ ] Verify send input without shell interpolation, bounded timeout behavior, maximum message size, error shape, and receipt/message id.
- [ ] Determine whether send accepts an idempotency key.
- [ ] Verify exact cron creation, script lookup, overlap, exit-code, and stdout behavior.
- [ ] Verify `allowed_chats`, `group_allowed_chats`, user allowlists, mention behavior, and parent-DM admission against the installed schema.

**Gate:**

- If trusted pre-model metadata is unavailable, stop. Propose a generic Hermes hook/plugin extension or an independent authenticated Telegram adapter. Do not fall back to a model skill invoking shell commands.
- If delivery result cannot distinguish success from failure, stop live integration and retain only fake transport development.

**Evidence:** version output, redacted sample events/results, exact commands, and a clear `PASS`, `NOT_RUN`, or externally blocked classification for every item.

## Task 1 — Package, clock, configuration, catalogues, and redaction

**Files:** packaging skeleton, `care/clock.py`, `care/config.py`, `care/redaction.py`, example configuration and language catalogues, focused tests.

### Required behavior

- [ ] Create the Python package, executable module, and pytest configuration.
- [ ] Implement an injectable UTC clock and timezone conversion using `zoneinfo`.
- [ ] Implement explicit schedule windows and quiet-hour calculations.
- [ ] Implement DST policy: first valid instant after a spring gap; first occurrence during a fall overlap.
- [ ] Load base roster and medication configuration into immutable typed values.
- [ ] Validate timezone, HH:MM/date/weekday formats, positive windows, ladder ordering, role conflicts, duplicate ids, delivery mode, and state path.
- [ ] Reject check-in times wholly inside quiet hours.
- [ ] Reject obvious dosage notation in configured labels.
- [ ] Require every enabled tripwire, affirmative, negative, and message catalogue; validate entry modes and matching message keys.
- [ ] Normalize configured term values with the same function used for incoming text.
- [ ] Redact dosage-like number/unit sequences and bound persisted/forwarded excerpts.
- [ ] Implement and test `CARE_STATE_DB` only if retained in operations documentation.

### Acceptance checks

- [ ] Spring-forward and fall-back tests pass for `Europe/Kyiv`.
- [ ] Malformed or missing safety files fail startup with one configuration error type.
- [ ] Negative/zero intervals and invalid timezones fail cleanly.
- [ ] Dosage-like labels fail; ordinary labels pass.
- [ ] Multiline, Unicode, apostrophe, emoji, and long-input redaction tests pass.
- [ ] No raw inbound dosage value appears in test event/log/render output.

## Task 2 — Transactional store, update dedupe, episodes, and outbox

**Files:** `care/models.py`, `care/store.py`, schema/migration tests.

### Required behavior

- [ ] Create versioned SQLite schema for `incoming_updates`, append-only `events`, and mutable `outbox`.
- [ ] Store authenticated envelope metadata and enforce unique Telegram update id.
- [ ] Represent check-in, dose, clarify, and escalation episode ids explicitly.
- [ ] Give every logical outgoing action a unique deterministic action key.
- [ ] Provide transaction APIs that append events and insert outbox rows atomically.
- [ ] Configure foreign keys, WAL, busy timeout, and bounded transaction behavior.
- [ ] Use `BEGIN IMMEDIATE` for claims and state transitions that must serialize.
- [ ] Create state directory as `0700` and database as `0600`; reject insecure live paths when they cannot be corrected safely.
- [ ] Store only redacted bounded excerpts.
- [ ] Store the validated base-configuration snapshot and source fingerprint when initializing a database.
- [ ] Reject silent seed-file drift; require an explicit validated import/reconciliation path.
- [ ] Return the previous deterministic result for a replayed update without repeating side effects.

### Acceptance checks

- [ ] Two connections cannot create duplicate action keys or update results.
- [ ] Replaying an update produces no additional event or outbox row.
- [ ] A transaction failure leaves neither a half-event nor half-outbox action.
- [ ] State permissions are verified on supported systems.
- [ ] Migration from an empty database and repeated startup are idempotent.

## Task 3 — Durable delivery worker

**Files:** `care/delivery.py`, delivery/outbox tests.

### Public contracts

- `Transport.send(target, text, idempotency_key) -> DeliveryResult`
- `DeliveryResult(status, receipt_id, error, retryable)`
- Worker claims one or a bounded batch of outbox rows using leases.

### Required behavior

- [ ] Implement capture and failure-injection transports first.
- [ ] Claim rows transactionally; send outside the transaction; record result afterward.
- [ ] Recover expired leases.
- [ ] Prioritize escalations and stop notices over routine prompts.
- [ ] Add bounded exponential retry and permanent-failure state.
- [ ] Preserve queued, delivered, retrying, and permanently failed as distinct states.
- [ ] Apply transport timeout and exception conversion.
- [ ] Enforce message size before transport; split only at safe deterministic boundaries or reject to a visible permanent failure.
- [ ] Never use a shell to interpolate message text.
- [ ] Capture receipt/message id and idempotency support where the verified Hermes contract permits.
- [ ] Document at-least-once escalation semantics when downstream idempotency is absent.
- [ ] Keep dry-run isolated from operational rows and state.

### Acceptance checks

- [ ] Concurrent workers do not claim the same live lease.
- [ ] Crash before send recovers and sends later.
- [ ] Crash after accepted send but before receipt storage demonstrates either downstream dedupe or the documented possible duplicate.
- [ ] Missing binary, timeout, retryable failure, permanent failure, and later recovery are tested.
- [ ] Delivery success is never inferred from action existence.

## Task 4 — Check-in vertical slice

**Files:** `care/checkins.py`, relevant `service.py` orchestration, focused tests.

### Required behavior

- [ ] Create one check-in episode per local day.
- [ ] Enqueue the prompt only inside its configured catch-up window.
- [ ] Activate the episode only after delivery success.
- [ ] Correlate replies using outgoing receipt/reply-to id, with exactly-one-open fallback.
- [ ] Anchor nudge and escalation to successful check-in delivery.
- [ ] Do not let nudge delivery failure postpone escalation.
- [ ] Emit a family operational alert if no check-in can be delivered during its window; do not call that parent silence.
- [ ] Enqueue one silence escalation action and stop routine nudges afterward.
- [ ] Stand down only after a correlated clear reply.
- [ ] For a concerning late reply, update/escalate concern without contradictory stand-down.
- [ ] Bind family acknowledgement to the specific outgoing escalation receipt/id and classify whether it was within 30 minutes.

### Acceptance checks

- [ ] Pre-check-in and medication replies do not close the episode.
- [ ] Sequential and concurrent ticks enqueue each logical action once.
- [ ] Missed window does not send a many-hours-late check-in.
- [ ] Check-in, nudge, escalation, acknowledgement, and stand-down transitions pass across local-day and DST boundaries.

## Task 5 — Medication vertical slice

**Files:** `care/meds.py`, focused tests.

### Required behavior

- [ ] Create one episode per configured dose id and scheduled local day.
- [ ] Suppress a reminder scheduled inside quiet hours.
- [ ] Catch up only inside the reminder window.
- [ ] Activate confirmation only after reminder delivery.
- [ ] Nudge and close relative to successful delivery.
- [ ] Confirm direct replies only when affirmative and not negated.
- [ ] Match `✅` explicitly.
- [ ] Without reply-to metadata, confirm only if exactly one compatible dose episode is open.
- [ ] Reject unknown, closed, ambiguous, unauthorized, and undelivered dose confirmations.
- [ ] Close delivered unresolved episodes as `unconfirmed`, never `missed`.
- [ ] Exclude undelivered/suppressed reminders from adherence calculations.
- [ ] Enqueue at most one adherence notice per rolling 24 hours.

### Acceptance checks

- [ ] Ukrainian inflections work without matching `не випила` as affirmative.
- [ ] A message matching both tripwire and affirmative escalates before any confirmation.
- [ ] Two overlapping dose windows do not auto-confirm ambiguously.
- [ ] Consecutive-day and same-day adherence rules use isolated test configuration and exact event kinds.

## Task 6 — Authenticated deterministic ingress and tripwire

**Files:** `care/triage.py`, ingress methods in `care/service.py`, tests using trusted envelopes.

### Required behavior

- [ ] Validate chat type, configured chat id, and sender authorization from adapter metadata.
- [ ] Deduplicate by update id before any side effect.
- [ ] Run deterministic normalization, redaction, and tripwire before model judgement or affirmative matching.
- [ ] On a hit, append the concern event and outbox escalation in one transaction.
- [ ] Return separate queued/delivered/failed state; never a boolean derived from an action list.
- [ ] Apply non-suppression when text asks not to notify family.
- [ ] Correlate ordinary replies only to compatible episodes.
- [ ] Keep raw Telegram text out of care logs and model directives after redaction.

### Acceptance checks

- [ ] Ukrainian prefix, word, and phrase cases pass.
- [ ] Missing/malformed lists cannot produce a clear-but-blind system.
- [ ] Forged actor ids inside message text have no authorization effect.
- [ ] Replayed tripwire update creates one logical escalation.
- [ ] Night tripwire enqueues despite quiet hours.

## Task 7 — Optional model conversation boundary

**Files:** `care/conversation.py`, constrained adapter/tool contract, tests with a fake model caller only.

### Required behavior

- [ ] Give the model only the redacted deterministic directive and minimum conversation context.
- [ ] Accept only typed `clear` or `unclear` judgement for non-tripwire updates.
- [ ] Let `unclear` create one pending clarification opportunity.
- [ ] Accept a clarifying question only through the constrained service and durable outbox.
- [ ] Consume the question budget after successful delivery, not before.
- [ ] On the second unresolved reply, quote redacted excerpts from both parent messages.
- [ ] Add a clarification timeout that escalates when the model/question path never completes.
- [ ] Keep the model unable to send group notices, edit files, choose actor ids, or invoke arbitrary terminal commands.

### Acceptance checks

- [ ] Model outage cannot block deterministic tripwire or timer behavior.
- [ ] Clarification send failure does not claim a question was asked.
- [ ] One clear follow-up resolves; one unresolved follow-up escalates; no third question is possible.

## Task 8 — Typed controls and effective runtime configuration

**Files:** `care/controls.py`, effective-config projection, tests.

### Required behavior

- [ ] Implement capabilities from trusted actor/chat metadata.
- [ ] Implement parent snooze, skip today, and stop.
- [ ] Implement family pause, resume, stop, skip, status, acknowledgement, check-in time change, and dose add/edit/remove.
- [ ] Validate snooze remains in the same day and outside quiet hours.
- [ ] Represent runtime mutations as typed events over the stored base snapshot; never let the model edit YAML.
- [ ] Commit each mutation and its group announcement outbox row atomically.
- [ ] Store normalized before/after values and actor id.
- [ ] Ensure queued escalations survive pause/skip/stop.
- [ ] Ensure only family can resume after stop.
- [ ] Use the outbox as the sole sender so announcements cannot be duplicated by the model.

### Acceptance checks

- [ ] Unauthorized attempts change nothing except an audit event.
- [ ] Invalid schedule/dose mutations do not partially commit.
- [ ] Stop takes effect even if its announcement is still retrying.
- [ ] Effective configuration replays identically after restart.

## Task 9 — Operator CLI, projections, doctor, and retention

**Files:** `care/readmodels.py`, `care/cli.py`, CLI tests.

### Commands

- `run`, `tick`, `deliver`, `status`, `log`, `doctor`, `triage --text-stdin`, `purge`, `config-import`.
- Adapter-only mutations are not exposed with user-selectable actor ids in the production CLI.

### Required behavior

- [ ] Keep `tick` enqueue-only and `deliver` delivery-only; `run` performs both with bounded work.
- [ ] Make an uneventful `run` silent while failures produce non-zero exit and concise stderr.
- [ ] Show redacted episode/action state in `status` and `log`.
- [ ] Make `doctor` inspect config/catalogues, seed snapshot drift, state permissions, adapter version/contract, observable cron status, outbox states, undelivered escalations/stop notices, idempotency support, and purge age.
- [ ] Distinguish historical failed attempts from messages still undelivered.
- [ ] Implement retention purge for excerpts and completed outbox bodies.
- [ ] Implement explicit validated `config-import` for adopting changed seed configuration without discarding runtime history.
- [ ] Ensure dry-run prints its isolated state mode and cannot mutate a configured operational DB.
- [ ] Convert expected config, SQLite, and transport errors to stable exit codes.

### Acceptance checks

- [ ] Every documented command is exercised against temporary config/state.
- [ ] `doctor` exits non-zero for pending permanent safety-message failure or insecure state.
- [ ] Purge removes eligible text but preserves structural audit metadata.

## Task 10 — Hermes adapter, constrained tool, profile, and cron

**Depends on:** Task 0 contract evidence. Do not invent APIs missing from that evidence.

**Files:** `care/hermes_adapter.py`, adapter/plugin files required by verified Hermes extension points, profile files, scripts, contract tests.

### Required behavior

- [ ] Convert Hermes Telegram updates into immutable authenticated envelopes.
- [ ] Invoke deterministic ingress before model dispatch.
- [ ] Attach only the redacted directive needed by later model handling.
- [ ] Expose a narrow typed tool for judgement, clarification, and controls; do not expose arbitrary terminal.
- [ ] Translate outbox sends through the verified safe-input API and capture receipts/errors.
- [ ] Configure both family group and parent DM admission plus user allowlists.
- [ ] Install one `care run` cron entry with verified non-overlap/locking behavior.
- [ ] Keep profile installation explicit and fail before replacing working configuration with unresolved placeholders.
- [ ] Keep secrets outside the repository.

### Acceptance checks

- [ ] Adapter contract tests use captured redacted fixtures from Task 0.
- [ ] A forged message body cannot change envelope identity.
- [ ] A live staging update triggers deterministic triage even when model handling is disabled.
- [ ] A model outage leaves cron and tripwire paths operational.

## Task 11 — Failure-oriented and end-to-end suites

**Files:** isolated scenario fixtures and tests.

- [ ] Create separate minimal configuration per scenario so check-in and medication traffic cannot distort each other's assertions.
- [ ] Assert event/action keys, targets, and delivery states rather than broad message counts.
- [ ] Cover every deterministic scenario in design §17.
- [ ] Add multiprocessing tests for concurrent ticks and workers.
- [ ] Add crash injection at transaction, claim, send, and receipt boundaries.
- [ ] Add transport timeout, permanent failure, recovery, and duplicate-risk tests.
- [ ] Add replay, forged identity, wrong chat, wrong escalation reply, and late acknowledgement tests.
- [ ] Add negated affirmative, ambiguous dose, pre-check-in reply, and cross-episode tests.
- [ ] Add redaction, long text, multiline, quotes, backticks, `$()`, and shell-metacharacter tests.
- [ ] Add dry-run isolation, retention, state permission, DST, and catch-up expiry tests.

**Gate:** All local deterministic tests pass from a clean checkout. No scenario remains `xfail`, skipped, or “known failure” for a required behavior.

## Task 12 — Documentation and staged acceptance

**Files:** `README.md`, `docs/operations.md`, `docs/escalation-policy.md`, `docs/message-catalogue.md`, profile installation notes.

### Documentation requirements

- [ ] Explain that this is not an emergency service or sole safety mechanism.
- [ ] Explain at-least-once duplicate risk when transport idempotency is unavailable.
- [ ] Document queued versus delivered wording.
- [ ] Document catch-up windows, quiet hours, failed check-in alerts, and DST policy.
- [ ] Document parent consent, stop behavior, non-suppression, and family acknowledgement gesture.
- [ ] Document redaction, 30-day text retention default, database permissions, backup boundary, and purge.
- [ ] Document Telegram/Hermes retention as an external boundary.
- [ ] Document exact verified group/privacy/allowlist setup and recovery commands.
- [ ] Keep config key names and environment overrides consistent with implemented code.
- [ ] Generate or manually reconcile message-catalogue documentation from actual validated templates.

### Staging acceptance

- [ ] Use a separate staging profile, state DB, group, and stand-in parent.
- [ ] Demonstrate pre-model tripwire with model disabled.
- [ ] Demonstrate check-in, nudge, silence escalation, acknowledgement, and stand-down.
- [ ] Demonstrate concern send failure, visible pending state, retry, and honest parent wording.
- [ ] Demonstrate stop during transport failure and later family-notice delivery.
- [ ] Demonstrate dry-run cannot consume live actions.
- [ ] Demonstrate privacy mode/admin and mention behavior.
- [ ] Run one full staged day before entering real ids.

## Final acceptance matrix

Before live deployment, complete this matrix against one candidate commit:

| Requirement | Local tests | Staging evidence | Status |
|---|---|---|---|
| Pre-model authenticated ingress | contract tests | model-disabled Telegram update | NOT_RUN |
| Replay protection | concurrent tests | repeated captured update | NOT_RUN |
| Check-in ladder | fixed-clock tests | full staged ladder | NOT_RUN |
| Tripwire/non-suppression | deterministic tests | night staging message | NOT_RUN |
| Dose confirmation/adherence | isolated tests | stand-in confirmation | NOT_RUN |
| Durable escalation delivery | failure/crash tests | forced transport failure/retry | NOT_RUN |
| Typed controls and stop | authorization tests | parent stop + family resume | NOT_RUN |
| Privacy/redaction/retention | content and purge tests | state/log inspection | NOT_RUN |
| Telegram privacy/allowlists | adapter tests | real group checks | NOT_RUN |
| Cron and recovery | process tests | observed staged cron | NOT_RUN |

`NOT_RUN` is not `PASS`. A missing Hermes capability is an external blocker only after Task 0 records the exact missing hook/API and the required operator or upstream action.

## Superseded approaches

The following approaches are superseded and must not be reintroduced:

- model/skill invocation as the only path to `care reply`;
- action existence reported as delivery success;
- sequential-only event checks presented as exact idempotency;
- free-form schedule descriptions plus model-edited YAML;
- actor ids accepted from model-authored CLI arguments;
- parent text interpolated into shell commands;
- dry-run writes to operational state;
- one-day `REPLY_RECEIVED` as check-in resolution;
- acknowledgements without escalation/message correlation;
- blanket scenario send counts that mix independent features;
- duplicate control announcements from both CLI and agent;
- blanket “no comments/docstrings” as a substitute for clarity.

## Execution handoff

Start with Task 0. Tasks 1–9 may use a fake adapter while external Hermes evidence is gathered, but Task 10 and live acceptance cannot pass until the pre-model and delivery contracts are verified. Do not configure the real parent or live delivery before every row in the final acceptance matrix is `PASS` or has an explicit user-approved external exception that does not violate a non-negotiable invariant.
