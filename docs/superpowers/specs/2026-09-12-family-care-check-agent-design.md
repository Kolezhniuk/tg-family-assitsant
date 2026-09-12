# Family Care-Check Agent — Design

Date: 2026-09-12
Status: revised proposal — not approved for live deployment
Hermes profile: `telegram-family-assistant`
Repository: `/root/tg-family-assitsant`

## 1. Purpose and safety boundary

The system checks in with one parent through Telegram, sends medication-label reminders, and tells a family group when a human should make contact. It is a presence-and-escalation tool, not a health tool or emergency service. It must never be presented as the parent's only safety mechanism.

The system never:

- diagnoses, interprets symptoms, or assigns clinical urgency;
- gives health advice or medical reassurance;
- recommends starting, stopping, delaying, splitting, or doubling medication;
- stores configured dosage amounts or repeats dosage amounts to the family;
- claims a person was contacted unless the transport returned success;
- lets model output suppress, delay, authorize, or fabricate a required action.

When a message is concerning, its only health-adjacent action is to notify the configured family group with a redacted quotation and delivery status. It does not contact emergency services.

## 2. Accepted scope

One deployment supports one parent DM, one family group, a configured family-member allowlist, one daily check-in episode, zero or more label-only medication reminders, Ukrainian and English deterministic term lists, deterministic controls, and optional model-written warmth and one clarifying question.

Multi-parent support, a dashboard, SMS, voice calls, medical records, dosage management, and emergency-service contact are out of scope.

## 3. Trust boundaries and invariants

### 3.1 Trust boundaries

- Telegram text is untrusted input.
- Sender, chat, update, message, and reply-to identifiers are trusted only when supplied by the Telegram/Hermes adapter, never when inferred by a model or accepted from message text.
- The model is untrusted for authorization, timing, tripwire suppression, confirmation, and delivery claims.
- Operator configuration is fully validated before work begins.
- Hermes is an external dependency. Its hook and send contracts must be verified against the installed version before acceptance.

### 3.2 Non-negotiable invariants

1. A required escalation is never reported delivered without transport success and, when available, a receipt or Telegram message id.
2. Deterministic incoming-message checks run before optional model reasoning.
3. Every incoming update has authenticated adapter metadata and replay protection by update id.
4. A reply closes only its correlated check-in, dose, clarification, or escalation episode.
5. Every outgoing message uses one durable outbox and retry mechanism.
6. Model inference may add care but never remove required care.
7. Dry-run never mutates operational state.
8. Runtime schedule changes are typed, validated, atomic, and attributable.
9. The parent may stop immediately; the family notice is durably retried.
10. Missing or malformed safety configuration prevents startup.
11. Configured dosage amounts are rejected; dosage-like inbound content is redacted before care persistence or family delivery.
12. Escalations ignore quiet hours.

## 4. Architecture

The deterministic ingress and timer paths meet in one application service. The model is downstream of deterministic policy.

```text
Telegram update
      │ trusted adapter metadata
      ▼
Hermes pre-model hook / care adapter
      │
      ▼
care ingest service ───────► optional model directive
      │                           │
      │ transaction               │ constrained response tool
      ▼                           ▼
SQLite: incoming ids + events + durable outbox
                              │
cron: care run ───────────────┤
                              ▼
                       delivery worker
                              │
                       Hermes send adapter
                              │
                    parent DM / family group
```

`care run` derives and enqueues due actions, then attempts eligible outbox deliveries. It invokes no model.

### 4.1 Required Hermes integration

A skill telling a model to run `care reply` is not deterministic ingress. Live deployment requires a pre-model Hermes hook, plugin, or equivalent adapter that provides immutable Telegram metadata and calls `care ingest` before model handling.

The integration must expose update id, chat id/type, sender id, message id, reply-to id, event timestamp, text, a way to attach the deterministic directive to model handling, and send success/error/receipt data.

If Hermes cannot provide this composition point, live reply processing is blocked. A prompt plus arbitrary terminal command is not an acceptable substitute.

### 4.2 Component boundaries

| Component | Owns | Must not own |
|---|---|---|
| `clock` | UTC/local conversion, schedule timestamps, DST policy | state or delivery |
| `config` | complete validation of operator data | runtime mutation |
| `redaction` | dosage-like and output-length redaction | triage policy |
| `triage` | deterministic term matching | model judgement or sending |
| `store` | transactions, update dedupe, events, episodes, outbox | timing policy |
| `checkins` | check-in transitions | subprocesses or model calls |
| `meds` | dose transitions and adherence rules | clinical interpretation |
| `conversation` | reply directives and clarify budget | chat authorization |
| `controls` | typed authorized runtime changes | model YAML editing |
| `service` | coordinate policy and persistence transactionally | Telegram parsing |
| `delivery` | leases, retries, transport results | deciding what is due |
| `hermes_adapter` | translate verified Hermes contracts | care policy |
| `readmodels` | status, log, doctor projections | mutations |
| `cli` | operator/test entry points | duplicated policy |

Domain modules do not import Hermes. The adapter depends on public application contracts.

## 5. Channel topology

| Surface | Direction | Contents |
|---|---|---|
| Parent DM | system ↔ parent | check-in, label-only reminders, nudges, one clarification, snooze/skip/stop confirmations |
| Family group | system → group | escalations, delivery failures requiring action, stand-downs, adherence and control notices |
| Family group | family → system | replies to escalation and controls routed through authenticated metadata |

Routine successful days are not posted to the group. Telegram privacy/admin behavior and Hermes allowlists remain deployment prerequisites. The adapter rejects unconfigured chats and senders before domain handling.

## 6. Identity, replay protection, and correlation

Every accepted update carries a trusted envelope:

```text
update_id, chat_id, chat_type, sender_id, message_id,
reply_to_message_id, received_at_utc, text
```

`update_id` is unique in storage. Replays return the prior result without new events or sends.

Each interaction belongs to an episode:

- `checkin:<local-day>`;
- `dose:<dose-id>:<local-day>`;
- `clarify:<source-message-id>`;
- `escalation:<logical-action-key>`.

Outgoing prompt receipts are recorded on their episode. Direct replies correlate through `reply_to_message_id`. Without a reply target, fallback correlation is allowed only when exactly one compatible episode is open for the parent. Otherwise the message does not close an episode automatically. A medication reply never closes a check-in merely because both occur on the same day.

## 7. Time model and windows

Configured times use `roster.timezone`; storage uses UTC plus local day. Default quiet hours are 21:30–08:00 local. Routine check-ins, nudges, and dose reminders are suppressed during quiet hours; escalations and operational failure notices are not.

### 7.1 DST policy

- A nonexistent spring-forward time moves to the first valid local instant after the gap.
- An ambiguous fall-back time uses the first occurrence.
- Logical action keys prevent duplicates during repeated wall-clock time.

### 7.2 Catch-up policy

| Action | Opens | Closes |
|---|---:|---:|
| Daily check-in | configured time | +30 minutes |
| Dose reminder | configured time | +30 minutes |
| Check-in nudge | successful check-in +180 minutes | escalation time |
| Dose nudge | successful reminder +45 minutes | close time |
| Silence escalation | successful check-in +360 minutes | end of local day |
| Dose close | successful reminder +120 minutes | first later run |

The silence-escalation window controls when its outbox action may first be created. Once created, delivery retries may continue beyond the local-day boundary until delivered or explicitly resolved.

Actions catch up only inside their window. Expired routine prompts are not sent hours late. Failure to deliver a check-in during its window creates a family-facing operational alert because silence cannot be inferred when the parent was never contacted. A dose that was never delivered is a delivery failure, not unconfirmed.

A reminder scheduled inside quiet hours is recorded as `suppressed_quiet` and is not queued for morning. Typed schedule mutation warns before accepting such a schedule.

## 8. Check-in state machine

Default ladder:

| Step | Time | Target |
|---|---|---|
| Check-in | 09:00 local | parent DM |
| Nudge | successful check-in +180 minutes | parent DM |
| Silence escalation | successful check-in +360 minutes | family group |

Rules:

1. One check-in episode exists per local day.
2. It becomes active only after successful delivery.
3. Only a correlated parent reply received after delivery resolves it.
4. Nudge failure does not delay escalation.
5. Silence escalation has one action key and is retried until delivered or explicitly resolved.
6. Once escalation is queued, routine nudging stops.
7. A later clear reply creates one stand-down.
8. A later concerning reply does not create a contradictory stand-down; it creates or updates a concern escalation.
9. Timely family acknowledgement requires an authenticated reply to the escalation message, or an explicit reference to its id, within 30 minutes. Later acknowledgement is logged but does not claim the timely-response SLA.

## 9. Reply handling

### 9.1 Tripwire first

Required Ukrainian and English term files support `word`, `prefix`, and `phrase`. Missing files, empty required languages, malformed entries, and unknown modes are fatal configuration errors.

Tripwire matching precedes affirmative and model processing. A hit records the authenticated update, redacts and bounds the quote, transactionally creates a concern event and outbox row, and returns delivery state as `queued`, `delivered`, or `failed`. Parent or model text cannot cancel it.

The parent may be told the system **is trying to notify** the family while queued. It may say the family **has been notified** only after confirmed delivery.

### 9.2 Additive model judgement

A non-tripwire reply may be marked `clear` or `unclear` by the model. `unclear` permits one clarifying question. The budget is consumed only when that question is durably queued and sent. A second unresolved reply escalates with redacted quotations of both parent messages.

If model or clarification delivery fails, the system records that boundary and does not pretend a question was asked. A configurable timeout escalates an unresolved clarification rather than leaving it open forever.

## 10. Medication reminders

`meds.yaml` contains stable ids, label-only descriptions, local times, and optional weekdays. Obvious dosage notation such as number + `mg`, `мг`, `ml`, or `мл` is rejected.

| Step | Time | Target |
|---|---|---|
| Reminder | configured local time | parent DM |
| Nudge | successful reminder +45 minutes | parent DM |
| Close | successful reminder +120 minutes | event only |

Rules:

1. One episode exists per dose and scheduled local day.
2. It becomes confirmable only after successful reminder delivery.
3. Tripwire matching always precedes confirmation.
4. A direct reply can confirm when it is a conservative affirmative with no negation.
5. Without reply metadata, auto-confirmation requires exactly one open dose.
6. `не`, `ні`, `not`, `no`, and configured negative patterns block the fast path. `✅` is matched explicitly.
7. Unknown/closed doses, unauthorized senders, and ambiguous replies are not confirmed.
8. Model requests use the same typed authenticated service and cannot invent identity.
9. Delivered but unconfirmed closes as `unconfirmed`, never `missed`.
10. Undelivered reminders do not count toward adherence rules.

Adherence notification occurs for the same dose unconfirmed on consecutive scheduled days, or two distinct unconfirmed dose episodes in one local day. At most one notice is delivered per rolling 24 hours, and it states that unconfirmed does not mean skipped.

## 11. Control and consent

Authorization uses trusted sender/chat metadata.

| Actor | Allowed actions |
|---|---|
| Parent in configured DM | snooze one open reminder, skip today, stop |
| Configured family member in configured group | pause, resume, stop, skip today, set check-in time, add/edit/remove label-only doses, status, acknowledge |
| Anyone else | none; refusal is recorded |

Base YAML seeds a versioned configuration snapshot when a state database is initialized. Runtime mutations are typed append-only events layered over that snapshot. The model never edits YAML. Later operator edits to the seed files are not adopted silently: `doctor` reports the fingerprint mismatch and an explicit validated import/reconciliation is required. Effective configuration therefore replays from the stored snapshot plus mutation events with attributable, atomic history.

- **Snooze:** moves one open routine reminder within the same local day; never an escalation or into quiet hours.
- **Skip today:** suppresses remaining routine prompts and announces the change.
- **Pause:** suppresses routine episodes until resumed or through an inclusive date; queued escalations remain active.
- **Stop:** prevents new routine episodes immediately. The family notice enters the outbox in the same transaction. Only family can resume.
- **Schedule mutation:** validates typed before/after values and commits the event plus announcement atomically.

The deterministic outbox is the only sender of announcements; the model does not repost them.

## 12. Persistence and privacy

SQLite contains:

```text
incoming_updates(update_id UNIQUE, chat_id, sender_id, message_id,
                 reply_to_message_id, received_at_utc, result)
events(id, ts_utc, local_day, kind, episode_id, subject, actor_id,
       source_update_id, payload)
outbox(id, action_key UNIQUE, priority, chat_id, text, status, attempts,
       available_at_utc, lease_until_utc, last_error, receipt_id)
```

Events are append-only; outbox delivery state is mutable. SQLite uses WAL, busy timeout, foreign keys, and `BEGIN IMMEDIATE` for claims/transitions. The directory is user-only (`0700`) and database `0600`.

Care persists only redacted, bounded excerpts. Default retention for excerpts and completed outbox bodies is 30 days; structural events and delivery metadata remain until operator purge. `care purge` applies policy. Backups inherit the same access restrictions; disk encryption is an operator concern.

Redaction occurs before event payloads, model directives, logs, and family messages, replacing dosage-like number/unit combinations with `[redacted dosage]`. Telegram/Hermes may still possess the original transport message; operations documentation must state that external boundary.

## 13. Durable delivery and idempotency

Policy transitions and outbox inserts occur in one transaction with a unique logical action key. A worker claims an eligible row with a lease, sends outside the transaction, then records success/failure.

This prevents overlapping workers from sending the same unclaimed action. A crash after Telegram accepts a send but before receipt storage can still duplicate. If Hermes supports idempotency keys, the adapter uses the action key. Otherwise escalation delivery is explicitly **at least once**: duplicates are preferable to silent loss. `doctor` and family documentation expose this limitation.

Transport requirements:

- bounded timeout;
- safe arguments/stdin with no shell interpolation;
- typed exception/error capture;
- bounded Telegram message size and safe markup behavior;
- receipt/message id capture when available;
- exponential retry with bounded maximum interval;
- escalation and stop-notice priority;
- permanent failures visible and causing non-zero `doctor` status.

Dry-run uses an in-memory or explicit separate database and never completes operational outbox rows.

## 14. Messages

Deterministic text remains in `config/messages.<lang>.yaml`. Required catalogues define identical validated keys. Values are redacted and bounded before rendering.

Wording distinguishes `queued`, `delivered`, and `failed/pending`. No message claims unconfirmed means missed, includes configured dosage amounts, gives medical advice, or claims delivery without evidence.

## 15. Commands and integration contracts

Operator CLI:

```text
care run                 derive due actions and attempt delivery
care tick                derive/enqueue only
care deliver             deliver eligible outbox rows
care status              read-only projection
care log --day DATE      redacted history
care doctor              config, adapter, outbox, cron and permission checks
care triage --text-stdin local test, no persistence
care purge               apply retention policy
care config-import       validate and explicitly adopt changed seed config
```

Authenticated adapter operations are typed service/tool calls, not shell strings:

```text
ingest(envelope)
acknowledge(envelope, escalation_id)
snooze(envelope, episode_id, minutes)
skip_today(envelope)
stop(envelope)
pause(envelope, until)
resume(envelope)
set_checkin_time(envelope, hhmm)
add_or_update_dose(envelope, dose fields)
remove_dose(envelope, dose_id)
submit_model_judgement(update_id, clear|unclear)
submit_clarifying_question(update_id, text)
```

Diagnostic message bodies enter through stdin/JSON stdin. Trusted actor fields remain adapter-only. Telegram text is never interpolated into shell commands.

## 16. Configuration validation

Startup rejects unknown timezones; invalid times, dates, weekdays, windows, and ladder ordering; duplicate/conflicting identities or dose ids; missing roster identities; unsupported delivery mode; missing/empty/malformed term lists; unknown match modes; missing/mismatched message catalogues; dosage-like labels; check-ins wholly inside quiet hours; and insecure/unwritable live state paths.

Environment overrides such as `CARE_STATE_DB` are either implemented and tested or omitted from documentation. No fallback silently changes safety behavior.

## 17. Testing and acceptance

Deterministic tests use a fixed clock, temporary SQLite, fake transport, and authenticated envelopes. They cover:

- happy check-in, silence, late clear reply, and late concerning reply;
- night tripwire and non-suppression requests;
- clarify success, failure, timeout, and second unresolved reply;
- correlated affirmative, emoji, negation, ambiguity, closed/unknown dose;
- adherence using only delivered reminders;
- pause, snooze, skip, stop, resume, and typed schedule changes;
- unauthorized actors and forged actor text;
- replayed updates, concurrent ticks/workers, and SQLite contention;
- crashes before send, after accepted send, and before receipt write;
- timeout, missing transport, permanent failure, and recovery;
- dry-run isolation;
- size, redaction, shell-metacharacter, and multiline cases;
- catch-up expiry, quiet hours, local-day, spring-forward, and fall-back;
- missing safety files and malformed/insecure config;
- acknowledgement correlation and 30-minute classification.

Scenario tests use isolated configurations and assert action/event kinds rather than broad message counts that mix check-ins and medication reminders.

Live acceptance requires a staging profile and stand-in parent. Real Hermes metadata, privacy behavior, send receipts, cron overlap behavior, and a full-day ladder are verified before configuring the real parent.

## 18. Operations and failure policy

`care doctor` reports configuration/catalogue validity, state permissions, adapter version/contract, observable cron health, outbox state by category, undelivered escalations and stop notices, transport idempotency support, and retention status.

Outages catch up only inside defined windows. Failed safety messages remain pending and are never shown as successful. State database loss is a visible operational event, not an invisible clean start.

## 19. Deployment gates

1. Register the real family supergroup and parent/family ids.
2. Configure Telegram privacy/admin behavior and Hermes allowlists.
3. Verify a pre-model hook with immutable sender/update metadata.
4. Verify safe send input, timeout, text limits, and receipt/message id.
5. Determine transport idempotency support.
6. Validate exact profile and cron syntax against installed Hermes.
7. Obtain parent/family agreement on schedule, non-suppression, retention, and non-emergency scope.
8. Complete dry-run and stand-in staging acceptance.

Until gates 3 and 4 are proven, integration is `NOT_RUN` and live deployment is a no-go.

## 20. Deliberately not built

- Health advice, symptom interpretation, clinical triage, or emergency dispatch.
- Daily all-fine group digests.
- Voice, SMS, or a second transport.
- Multi-parent behavior.
- Dosage, prescription, laboratory, or medical-record storage.
- Arbitrary terminal access for the conversational agent.
- A web dashboard.
- Exact-once Telegram claims when the transport cannot prove them.
