# Family Care-Check Agent — Design

Date: 2026-09-12
Status: proposed
Hermes profile: `telegram-family-assistant`
Repository: `/root/tg-family-assitsant`

## 1. Purpose

A Telegram agent that checks in on one parent every day, reminds them about
their medication, and tells the rest of the family when something needs a
human. It is a presence-and-escalation tool, not a health tool.

The agent exists because families are spread across generations and time
zones, and the parent already uses Telegram. The channel is the point: no new
app, no new login, no device to learn.

### Out of scope, permanently

The agent gives no health advice. It does not diagnose, interpret symptoms,
suggest what a symptom might mean, recommend starting, stopping, splitting or
doubling a dose, reassure medically ("I'm sure it's nothing"), or triage
urgency clinically. When a reply sounds worrying, its entire job is to put a
human in the loop and quote what was said, verbatim.

## 2. Channel topology

Two Telegram surfaces, with different jobs.

| Surface | Direction | Contents |
|---|---|---|
| Parent DM | agent ↔ parent | daily check-in, medication reminders, nudges, clarifying question, pause/snooze |
| Family group | agent → group, family → agent | escalations, stand-downs, schedule changes, `status` on request |

The parent's ordinary day is not narrated to the group. The group hears from
the agent only when something needs attention, so a message from the bot in
the group always means *look at this*. That signal is the reason for the
split, and it is destroyed by routine chatter — the design deliberately has
no daily "all fine" digest.

The bot lives in the family group as a member. Posting to a group requires
nothing special — `hermes send --to telegram:<group_id>` works the moment the
bot is a member.

**Reading** the group is gated by Telegram's bot privacy mode, which is on by
default. With it on, the bot receives only slash commands, replies to its own
messages, and service messages. Turning it off — or, preferably, promoting
the bot to group admin, which bypasses the setting without changing it
globally for every group the bot is in — delivers everything. This is needed
for the stand-down acknowledgement in §5.4 and the conversational control
surface in §8. Full procedure in `docs/operations.md`.

The design degrades cleanly if neither is done: escalations, check-ins,
reminders and the whole ladder still work, because they are sends. Only
passive group awareness is lost, and the escalation message already asks for
an explicit reply rather than relying on it.

## 3. Architecture

A deterministic spine with the model at the edges.

```
                    cron (*/5)
                        │
                  scripts/care-tick.sh
                        │
   ┌────────────────────▼─────────────────────┐
   │  care CLI  (pure state machine)          │
   │  config → events → due actions → sends   │
   └────────┬─────────────────────┬───────────┘
            │                     │
    hermes send (DM)      hermes send (group)
            │                     │
        parent               family group
            │                     │
            └──────► gateway ◄────┘
                        │
                  Hermes agent
                 (family-care skill)
                        │
                   care CLI  (reply / confirm / schedule)
```

**Deterministic, in the CLI — never decided by a model:**

- when the check-in goes out, when it is nudged, when it escalates
- when a medication reminder goes out, when it is nudged, when it is closed
- the tripwire word list and the immediate escalation it triggers
- the repeat rule that turns unconfirmed doses into a group message
- quiet hours, pause, stop
- the wording of every escalation message posted to the group

**Model-decided, in the agent:**

- whether an ordinary-looking reply is nonetheless *off*
- the single clarifying question, when one is warranted
- the warmth and phrasing of DMs to the parent
- interpreting conversational schedule changes in the group

The split follows one rule: **inference may add care, never remove it.** The
model can cause an escalation that the rules alone would have missed. It can
never prevent, delay, or soften one the rules require. If the model, the
model provider, or the whole Hermes gateway is down, the ladder still runs
and the family is still told — the tick is a script with no LLM in it
(`hermes cron ... --no-agent`).

### 3.1 Why not the alternatives

*Agent-first* (cron jobs carrying prompts, model decides each tick) was
rejected: the timers are the safety feature, and a model that reasons poorly
at 15:00 fails silently — nobody learns that the escalation did not happen.

*Fully deterministic* (no LLM at all) was rejected because it cannot support
the soft-judgement layer in §6 or the conversational control surface in §8,
both of which were chosen requirements.

## 4. Repository layout

```
README.md                         setup, daily operation, every command
docs/
  superpowers/specs/              this document
  operations.md                   runbook: install, group registration, dry-run, recovery
  escalation-policy.md            the ladder in plain language, written for the family to read
  message-catalogue.md            every deterministic message the CLI can send, verbatim
care/
  __init__.py
  clock.py                        Clock protocol; SystemClock and FixedClock
  config.py                       roster.yaml + meds.yaml loading and validation
  state.py                        SQLite append-only event log, derived day state
  ladder.py                       check-in state machine
  meds.py                         dose state machine
  triage.py                       tripwire matching, verdict contract
  messages.py                     deterministic message composition
  delivery.py                     hermes send wrapper, dry-run and capture modes
  cli.py                          argparse entry points
bin/care                          executable shim
config/
  roster.example.yaml
  meds.example.yaml
  tripwire.en.yaml
  tripwire.uk.yaml
  affirmatives.en.yaml
  affirmatives.uk.yaml
scripts/
  care-tick.sh                    cron entry point
  install-profile.sh              copies SOUL.md and config fragment into the Hermes profile
profile/
  SOUL.md                         agent persona and boundaries, versioned here
  config-fragment.yaml            telegram group gates, platform_toolsets, skills.external_dirs, safety-gate
skills/
  family-care/SKILL.md            agent-side skill: how to call the CLI on every reply
tests/
  ...
```

### 4.1 Code conventions

Source files carry **no comments**. Every explanation lives in `docs/` or in
this spec. Names and function boundaries are expected to carry the meaning;
where they cannot, the file is too clever and should be simplified rather
than annotated. Docstrings are likewise omitted — module behaviour is
documented per-module in `docs/`.

Python 3.12, standard library plus PyYAML. No web framework, no ORM, no
scheduler library: the scheduler is `hermes cron`, and the store is SQLite
via `sqlite3`.

## 5. The check-in ladder

### 5.1 Timings

Defaults, all overridable in `roster.yaml`:

| Step | Default | Target |
|---|---|---|
| check-in | 09:00 local | parent DM |
| nudge | +3h (12:00) if no reply | parent DM |
| escalation | +6h (15:00) if still no reply | family group |

At most one silence escalation per calendar day. After it fires, nudging
stops for the day — the humans have it now, and a bot continuing to poke the
parent while a daughter is driving over is noise.

### 5.2 Tick semantics

`care tick` runs every five minutes and is a pure function of
`(config, event log, now)`. It computes the set of actions currently **due
and not already recorded**, executes them, and appends one event per action.
Running it twice in the same minute produces no duplicate messages; a missed
window (machine asleep, gateway down) is picked up on the next tick rather
than lost, provided it is still within the step's validity window.

Five-minute granularity means a 09:00 check-in may arrive as late as 09:04.
This is deliberate: minute-exact delivery would need a per-minute cron with
no benefit to a human reading a phone.

### 5.3 Quiet hours

`quiet_hours` (default 21:30–08:00 local) suppresses check-ins, nudges and
medication reminders. A reminder whose window closes entirely inside quiet
hours is dropped and recorded as `suppressed_quiet`, never queued to fire at
07:00 the next morning.

**Escalations ignore quiet hours entirely.** A tripwire at 23:40 posts to the
group at 23:40.

### 5.4 Stand-down

If the parent replies after a silence escalation has been posted, the agent
posts a stand-down to the group naming the time of the reply and quoting it.

Acknowledgement by a family member is recorded when someone replies to the
bot's escalation message, or mentions the bot, within 30 minutes. The agent
then does not re-raise the same day. It does **not** infer resolution from
arbitrary chatter — the escalation message asks for exactly the gesture it
needs: *"reply to this message once someone has reached her."*

This choice is forced by how Hermes handles groups, and it is the right one
anyway. With `require_mention: true` and
`observe_unmentioned_group_messages: true`, ordinary group chatter is folded
into the session transcript as context but does **not** dispatch the agent —
only a reply or a mention does. So a sibling typing "I called her, all fine"
into the void is visible later but triggers nothing in the moment; a reply to
the bot triggers the stand-down immediately. Requiring the deliberate gesture
makes the acknowledgement an explicit act by a named person, recorded with
their chat id, rather than an inference from whoever happened to type
something reassuring.

## 6. Reading replies

### 6.1 Two layers

**Tripwire — deterministic, unconditional.** A curated term list, normalised
(case-folded, punctuation-stripped, Cyrillic homoglyphs folded) and matched
as whole words or phrases. A hit escalates immediately, without model
involvement, and the escalation message is composed by the CLI. Term lists
ship per language in `config/tripwire.*.yaml`; **Ukrainian and English** are
both enabled by default, since the family writes in both.

Seed categories: falls, chest pain, breathlessness, bleeding, confusion or
disorientation, sudden weakness, explicit calls for help, explicit statements
of not being okay, statements about having stopped taking medication.

The list is data, not code, and is expected to be edited over time. Terms are
matched, not interpreted — `care triage` prints the matched term so a false
positive is traceable to a line in a YAML file.

#### Matching modes, and why Ukrainian needs them

Whole-word matching is wrong for Ukrainian. The language inflects heavily,
so a single concept has many surface forms: *впала, впав, впали, упала,
падаю*. Enumerating every form by hand guarantees the one that gets typed at
04:00 is the one nobody listed.

Each term therefore declares how it matches:

```yaml
terms:
  - match: prefix
    value: "впал"
    note: fell (feminine, masculine, plural)
  - match: prefix
    value: "упал"
  - match: phrase
    value: "не можу встати"
  - match: word
    value: "кров"
```

- `prefix` — matches a word starting with the value. Covers inflection with
  one line. Used for verbs and adjectives.
- `word` — exact token match. Used where a prefix would over-fire.
- `phrase` — a normalised token sequence. Used for multi-word idioms such as
  *не можу дихати*, *болить у грудях*, *викличте швидку*.

`prefix` is chosen over stemming deliberately: a stemmer is a dependency, a
source of surprises, and untestable by reading. A prefix is auditable by a
family member who does not write code, which matters because the list is
meant to be edited by whoever notices a gap.

English keeps `word` and `phrase` and needs no prefixes.

The trade is more false positives, accepted knowingly: a tripwire that fires
on an innocent message costs one unnecessary group notice, and the family can
delete the offending line from a YAML file. A tripwire that fails to fire
costs the thing the agent exists to prevent.

**Soft judgement — model, additive only.** The agent classifies a
non-tripwire reply as `clear` or `unclear`. `unclear` covers: unusually terse
relative to that person's norm, vague unwellness, a mention of skipping or
running out of medication, confusion in phrasing, or a reply that does not
answer the question asked.

`unclear` buys exactly **one** clarifying question. The question budget is
enforced by the CLI, not by the model's restraint. The parent's answer is
re-triaged; if it does not resolve to `clear`, the agent escalates with both
messages quoted. The agent is never permitted to ask a third time — a parent
being interrogated by a bot is worse than a family member being called.

### 6.2 Reply handling contract

The agent calls `care reply --text "<verbatim>"` on every parent message and
obeys the returned directive:

| Verdict | CLI action | Agent action |
|---|---|---|
| `clear` | records `reply_received` | warm one- or two-sentence acknowledgement |
| `tripwire` | records, **escalates to group itself** | tells the parent plainly that the family has been told; asks if they want someone to call; no advice |
| `unclear` | records, opens question budget of 1 | asks exactly one clarifying question |
| `unclear` after follow-up | records, escalates to group itself | tells the parent the family has been told |

The CLI performs tripwire escalation itself rather than instructing the agent
to do it, so that a model failure between verdict and send cannot swallow the
escalation.

### 6.3 The non-suppression rule

If the parent asks the agent not to tell the family — after a tripwire, or at
any point — the agent does not comply, and says so honestly and without
argument: it explains that it always tells the family about this kind of
message, that it is telling them now, and that they can talk to the family
directly. It does not negotiate, moralise, or repeat itself.

This is the single most important behavioural rule in the system. A
care-check agent that can be talked out of escalating by the person it is
checking on provides negative value: the family believes someone is watching,
and nobody is. It is stated in `SOUL.md` in these terms.

## 7. Medication reminders

### 7.1 Schedule

`meds.yaml` lists doses, each with a stable `id`, a human `label` used in
messages, a local time, and an optional day pattern:

```yaml
doses:
  - id: morning-bp
    label: the blood pressure tablet
    at: "08:30"
  - id: evening
    label: the evening tablet
    at: "20:00"
    days: [mon, tue, wed, thu, fri, sat, sun]
```

No dosage amounts are stored or spoken. The agent says "the blood pressure
tablet", never "50mg". Storing a dose amount invites the agent to discuss it,
which is outside its boundary, and invites the family to treat the bot's copy
as authoritative over the pharmacy label.

### 7.2 Dose ladder

| Step | Default | Target |
|---|---|---|
| reminder | at dose time | parent DM |
| nudge | +45m if unconfirmed | parent DM |
| close as unconfirmed | +2h | recorded only, nothing sent |

Confirmation is recorded by `care confirm --dose <id>`, called either by the
agent when the parent says they took it, or by a deterministic fast path: a
bare affirmative (`так`, `добре`, `гаразд`, `випила`, `прийняла`, `готово`,
`yes`, `done`, `✅` and similar, per the language lists) received inside an
open dose window confirms without the model.

Affirmatives live in `config/affirmatives.uk.yaml` and
`config/affirmatives.en.yaml` and use the same matching modes as the tripwire
lists, so the Ukrainian gendered verb forms (*випила* / *випив*, *прийняла* /
*прийняв*) are one `prefix` entry each rather than four `word` entries.

Tripwire matching runs **before** the affirmative fast path. A message that
hits both — *"так, випила, але дуже болить голова"* — escalates; it does not
quietly close the dose and stop there.

### 7.3 Unconfirmed is not missed

A closed dose is recorded as `unconfirmed`, never as `missed`. Most
unconfirmed doses are a parent who took the pill and put the phone down. The
distinction is carried into the group message wording, which says so
explicitly.

Adherence escalation fires when either:

- the same dose is unconfirmed on two consecutive days, or
- two doses are unconfirmed within one calendar day

At most one adherence escalation per rolling 24 hours. It is phrased as
information, not accusation, and it asks the family to check rather than
asserting that medication was skipped.

## 8. Control surface and consent

### 8.1 Who may change what

Authorisation is by Telegram chat id, from `roster.yaml`.

| Actor | May do |
|---|---|
| family members (group) | set check-in and dose times, add/edit/remove doses, pause, resume, stop, `status` |
| parent (DM) | snooze a single reminder, skip a single day, request full stop |
| anyone else | nothing; the CLI refuses and the attempt is recorded |

Every mutation prints a confirmation line that the agent posts **verbatim**
to the family group. Schedule changes are never made quietly: a family where
one sibling can silently move a reminder is a family that will later disagree
about what the bot was supposed to do.

### 8.2 Pause, skip, stop

- **snooze** — one reminder, moved by a stated interval within the same day.
- **skip today** — suppresses remaining check-in and reminders for the
  calendar day. The silence ladder does not run. The group is told.
- **stop** — the parent may stop the agent entirely. The CLI honours it
  immediately and posts a plain notice to the group: the agent has been
  stopped at the parent's request and is no longer checking in. Only a family
  member can resume.

Honoured-but-announced is the whole of the consent design. A daily message
the parent cannot escape is surveillance; an agent that can go dark without
the family noticing is a false sense of safety. Announcing the stop is what
makes both false.

## 9. Data model

One SQLite database, one append-only table.

```sql
events(
  id        INTEGER PRIMARY KEY,
  ts_utc    TEXT NOT NULL,
  local_day TEXT NOT NULL,
  kind      TEXT NOT NULL,
  subject   TEXT,
  payload   TEXT
)
```

Event kinds: `checkin_sent`, `checkin_nudged`, `reply_received`,
`clarify_asked`, `escalated_silence`, `escalated_concern`, `stood_down`,
`escalation_acknowledged`, `dose_reminded`, `dose_nudged`, `dose_confirmed`,
`dose_unconfirmed`, `escalated_adherence`, `suppressed_quiet`, `snoozed`,
`skipped_day`, `paused`, `resumed`, `stopped`, `schedule_changed`,
`unauthorised_attempt`.

Day state is **derived** by folding the event log, never stored. There is one
source of truth, history is replayable, and any dispute about what the agent
did is answered by `care log --day 2026-09-12`. `local_day` is denormalised
onto each row so that day-boundary queries do not depend on timezone maths at
read time.

Location: outside the repository, default
`~/.hermes/profiles/telegram-family-assistant/workspace/care-state.db`,
overridable by `CARE_STATE_DB`.

## 10. Delivery

`care` never calls the Telegram API. It shells out to
`hermes send --to telegram:<chat_id>`, reusing the gateway's credentials, so
there is one place where bot tokens live and the tick works whether or not
the gateway process is running.

Three delivery modes:

- `live` — sends.
- `dry-run` — prints what would be sent, with target and text. Default in
  tests and during initial setup.
- `capture` — in-process list, used by the test suite.

A send failure is recorded as an event with the error, and retried on the
next tick while the step's window is still open. A failed escalation is
retried on every tick until it succeeds or the day ends, and a
never-delivered escalation is surfaced by `care doctor`.

## 11. Cron

One job:

```
hermes cron create --name family-care-tick "*/5 * * * *" \
  --script care-tick.sh --no-agent
```

`--no-agent` is load-bearing: the script is the job, no model is invoked, and
empty stdout means silence. `care tick` prints nothing on an uneventful tick,
so the classic watchdog pattern applies and the profile is not spammed.

## 12. Agent integration

`skills/family-care/SKILL.md` is registered through
`skills.external_dirs` in the profile config, matching how the `medassist`
profile consumes `/root/hermes-medical-assistant/skills`. It instructs the
agent to call `care reply` on every parent DM before composing anything, to
obey the returned directive exactly, to post CLI confirmation lines verbatim,
and never to describe an escalation as done unless the tool reported success.

`profile/SOUL.md` replaces the profile's stock Hermes boilerplate. It carries
the boundaries from §1, the non-suppression rule from §6.3, and a tone
section: short sentences, one question per message, plain words, no emoji
storm, never guilt about a missed dose, never claim to have contacted anyone
the tools did not actually reach.

## 13. Testing

Everything deterministic is tested without network, model, or wall clock:
`FixedClock`, an in-memory SQLite event log, and `capture` delivery.

Scenario tests, each asserting the exact sequence of sends:

1. happy path — check-in, reply, acknowledgement, no group traffic
2. silence — check-in, nudge at +3h, escalation at +6h, nothing after
3. late reply — escalation fired, reply at +7h, stand-down posted
4. tripwire at 23:40 — immediate escalation despite quiet hours
5. unclear then clear — one clarifying question, no escalation
6. unclear then still unclear — escalation quoting both messages
7. parent asks the agent not to tell — escalation still posted
8. dose confirmed by bare affirmative — no model involved
8b. Ukrainian inflection — `впала`, `впав`, `упали` all trip one `prefix` term
8c. affirmative plus tripwire in one message — escalates, does not just confirm
9. dose unconfirmed two days running — one adherence escalation, not two
10. two doses unconfirmed in one day — one adherence escalation
11. quiet hours — evening dose after 21:30 dropped, recorded as suppressed
12. skip today — no check-in, no ladder, group told
13. stop by parent — group notice, subsequent ticks silent, family resume works
14. unauthorised chat id attempts a schedule change — refused and recorded
15. idempotency — the same tick run three times sends once
16. missed window — machine asleep across 09:00, tick at 09:20 still sends
17. delivery failure — escalation retried next tick, then succeeds
18. DST boundary — local times hold across the transition

## 14. Prerequisites and open items

1. **The family group is not registered.** `channel_directory.json` currently
   knows only two DMs: Tëma (`11111111`) and Dima K (`22222222`). The bot
   must be added to the group and one message sent there before Hermes learns
   the chat id. Until then `roster.yaml` cannot be completed.
2. **Group message delivery** must be opened up — either BotFather privacy
   mode off, or the bot promoted to group admin — for the stand-down
   acknowledgement (§5.4) and the group control surface (§8), plus the
   matching `telegram.group_allowed_chats` / `require_mention` /
   `observe_unmentioned_group_messages` settings in the profile config.
   Without it the system still works, minus those two behaviours. Step by
   step in `docs/operations.md`; `care doctor` reports which mode is in
   effect and warns when the config and the Telegram side disagree.
3. **There is no parent in the directory.** Development and acceptance use a
   stand-in DM plus `dry-run`, so no real check-ins go out before the roster
   is real.
4. **Timezone** defaults to the host's `Europe/Berlin`. The parent's actual
   timezone goes in `roster.yaml` and is the only one that governs schedule
   times.
5. **Model.** The profile currently runs `openai/gpt-5.6-terra` via
   OpenRouter. Nothing in the design depends on the model choice, because
   nothing safety-critical is delegated to it.

## 15. What is deliberately not built

- No health advice, symptom checking, or triage of any kind.
- No daily "all fine" digest to the group — it would train the family to
  ignore the channel where escalations arrive.
- No voice calls, SMS, or non-Telegram fallback.
- No multi-parent support. One parent, one group. The data model would allow
  more; the messages and the ladder assume one, and pretending otherwise
  would ship an untested path.
- No dosage amounts, prescriptions, or medical record storage.
- No web dashboard. `care status` and `care log` are the interface.
