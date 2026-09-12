# Operations runbook

> **STATUS — READ FIRST.** Parts of this runbook describe an operator CLI (`care status`,
> `care log`, `care doctor`, `care triage`, `care tick`, `care deliver`) that is **NOT BUILT YET**.
> `python3 -m care` currently exits 1 with "not implemented". Those sections are the intended
> design, retained so the setup they describe stays reviewable — they are not instructions you can
> follow today. What *is* runnable right now is `demo/care_demo.py`, which drives the same state
> machines directly. The Telegram group and privacy-mode setup in Part 1 and Part 2 below IS
> accurate and verified, and does not depend on the CLI.

Everything needed to take the bot from "exists" to "checking in on a real
person", plus what to do when it misbehaves.

Profile: `telegram-family-assistant`. Every `hermes` command below is
profile-scoped — use the wrapper `telegram-family-assistant …`, which is just
`hermes -p telegram-family-assistant …`.

---

## Part 1 — Set up the family group

### Why the group must be set up before anything else

`roster.yaml` needs the group's numeric chat id, and that id does not exist
until the bot is a member of the group and has seen at least one message
there. Nothing downstream can be configured first.

### 1.1 Create or pick the group

A normal Telegram group with the family in it. An existing family group is
fine and usually better — people already read it.

If the group was created long ago as a **basic group** rather than a
**supergroup**, its id will change the first time Telegram migrates it
(adding an admin bot can trigger this). Migration rewrites the id from
`-123456789` to `-100123456789`, and the old id stops working silently. To
avoid discovering that during an emergency, force the migration up front by
enabling any supergroup-only feature (for example set a public link, then
remove it), and only then read the id.

### 1.2 Add the bot

Group → **Add members** → search the bot's `@username` → add.

### 1.3 Make the group id known to Hermes

Send one message in the group that the bot is guaranteed to receive. Privacy
mode is still on at this point, so an ordinary "hello" will **not** reach it —
slash commands will:

```
/sethome@your_bot_username
```

`/sethome` designates the chat as the profile's home channel, which is where
cron output would land by default. The care agent addresses every chat
explicitly by id and does not depend on the home channel, so if you would
rather not repoint it, use any other command form addressed to the bot, such
as `/start@your_bot_username`. Either way the point is the same: a
command-prefixed message reaches a privacy-mode bot, and that is what teaches
Hermes the chat exists.

Now read the id back:

```bash
telegram-family-assistant send --list telegram
```

The group appears with a negative id, for example `-1001234567890`. It is
also written into the profile's channel directory:

```bash
cat ~/.hermes/profiles/telegram-family-assistant/channel_directory.json
```

Negative ids are normal for groups and supergroups. A personal DM's chat id
is the same number as that person's user id.

### 1.4 Confirm the bot can post

```bash
telegram-family-assistant send --to telegram:-1001234567890 "Care agent online (test)."
```

If that message lands in the group, the entire escalation path works. Posting
is unrestricted — the privacy setting in Part 2 governs only what the bot can
**read**.

### 1.5 Collect the people

Put the ids in `config/roster.yaml`:

```yaml
timezone: Europe/Kyiv
parent:
  chat_id: "11111111"
  name: Mum
group:
  chat_id: "-1001234567890"
family:
  - chat_id: "22222222"
    name: Dima
  - chat_id: "33333333"
    name: Tëma
```

To find a person's numeric user id, have them message the bot once and read
`send --list telegram`, or have them message `@userinfobot` and send you the
number. A `@username` is not an id and cannot be used for authorisation.

---

## Part 2 — Open up group message delivery

### What privacy mode does

Telegram bots ship with **privacy mode on**. In a group, a privacy-mode bot
receives only:

- messages starting with a `/` command
- direct replies to the bot's own messages
- service messages (joins, leaves, pins)
- everything, if the bot is an admin of a channel

It does **not** receive ordinary group chatter. This is the single most common
source of "it works in DMs but is silent in the group".

For the care agent this matters in exactly two places: the stand-down
acknowledgement (a family member confirming they have reached the parent) and
the conversational control surface (changing schedules by talking to the bot
in the group). Check-ins, reminders, nudges and escalations are all *sends*
and work regardless.

### Option A — promote the bot to group admin (recommended)

Group → **Manage group** → **Administrators** → **Add admin** → the bot.

Admin bots receive every message regardless of the privacy setting. This is
preferred because it is scoped to this one group: the bot's global privacy
setting stays on, so if the same bot is ever added to another chat it does not
start reading everything there too.

Grant no rights beyond membership — the bot needs to *see* messages, not
manage anyone. Deleting messages, banning users and pinning are all
unnecessary and should be left off.

### Option B — turn privacy mode off globally

1. Open a chat with **@BotFather**
2. Send `/mybots`
3. Select your bot
4. **Bot Settings → Group Privacy → Turn off**

**Then remove the bot from the group and add it back.** Telegram caches the
privacy state at the moment a bot joins, and an existing membership keeps the
old behaviour indefinitely. Skipping the rejoin is why this step appears not
to work.

This setting is global to the bot, across every group it is in. Prefer Option
A unless the bot is only ever used here.

### 2.1 Tell Hermes about the group

Turning on delivery at the Telegram end is half of it; the profile also has
to accept those messages. In the profile config:

```yaml
telegram:
  allowed_chats:
    - "-1001234567890"
  group_allowed_chats:
    - "-1001234567890"
  require_mention: true
  observe_unmentioned_group_messages: true
```

- `allowed_chats` — where the bot is permitted to respond at all.
- `group_allowed_chats` — authorises the shared group session that observed
  context is attached to. Use the same id.
- `require_mention: true` — the bot stays quiet during ordinary family
  conversation and answers only when replied to, `@`-mentioned, or addressed
  with `/command@botname`. Without this the bot chats back to everything,
  which in a family group is intolerable within a day.
- `observe_unmentioned_group_messages: true` — ordinary chatter is folded
  into the session transcript as context but does **not** dispatch the agent.
  So the bot knows what was said when it is next addressed, without replying
  to it.

The equivalent environment variables, if you prefer `.env`:

```
TELEGRAM_ALLOWED_CHATS=-1001234567890
TELEGRAM_GROUP_ALLOWED_CHATS=-1001234567890
TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES=true
```

Authorisation of individual people is separate and still applies:
`TELEGRAM_ALLOWED_USERS` / `TELEGRAM_GROUP_ALLOWED_USERS`, or the pairing
flow (`telegram-family-assistant pairing list` / `… approve <code>`).

### 2.2 Verify

```bash
telegram-family-assistant gateway restart
```

In the group, reply to any message the bot has sent, or `@`-mention it, and
check that the agent responds. Then have someone post ordinary chatter and
confirm the bot stays silent — that is `require_mention` working, not a
failure.

```bash
care doctor
```

reports which delivery mode it believes is in effect and warns when the
Telegram side and the profile config disagree.

### 2.3 If the bot is silent in the group but fine in DMs

Work through these in order; it is almost always the first two.

1. **Telegram is not delivering.** Privacy mode still on and the bot not an
   admin. Fix per Option A or B.
2. **Bot joined before the setting changed.** Remove and re-add the bot.
3. **The sender is not authorised.** Add their user id to
   `TELEGRAM_ALLOWED_USERS` / `TELEGRAM_GROUP_ALLOWED_USERS`, or allow the
   chat via `group_allowed_chats`.
4. **`require_mention` is doing its job.** Ordinary chatter is meant to be
   ignored. Reply to the bot or mention it.
5. **The group id changed.** A basic group that migrated to a supergroup has
   a new id. Re-read `send --list telegram` and update `roster.yaml`.
6. **Another bot took the message.** With several bots in one group, mentions
   route exclusively to the bots named. Mention this one explicitly.

---

## Part 3 — First run, safely

Nothing should reach a real parent until the ladder has been watched end to
end.

1. **Dry run.** `delivery.mode: dry-run` in `roster.yaml`. Every send is
   printed with its target and text, and nothing leaves the machine.
2. **Fast-forward the clock.** `care tick --now 2026-09-12T09:00` and walk
   forward through the day to watch the check-in, the nudge at 12:00 and the
   escalation at 15:00 without waiting six hours.
3. **Stand-in parent.** Point `parent.chat_id` at your own DM and live-run a
   full day against yourself before pointing it at anyone else.
4. **Go live.** Set `delivery.mode: live` and install the cron job:

```bash
telegram-family-assistant cron create --name family-care-tick "*/5 * * * *" \
  --script care-tick.sh --no-agent
telegram-family-assistant cron list
```

`--no-agent` means the script *is* the job: no model is invoked and empty
output sends nothing. An uneventful tick prints nothing and is silent.

---

## Part 4 — Day-to-day

```bash
care status                 # today at a glance: check-in, replies, doses, escalations
care log --day 2026-09-12   # every event, in order, with timestamps
care doctor                 # config, delivery mode, cron health, undelivered escalations
care tick --dry-run         # what would happen right now
```

### Pausing for a hospital stay or a holiday

`care pause --until 2026-09-20` stops check-ins and reminders and posts a
notice to the group. `care resume` restarts them. Both are announced; the
agent never goes quiet without the family being told.

### When the parent asks to stop

The agent honours it immediately and posts a plain notice to the group saying
it has been stopped at the parent's request. Only a family member can resume.
This is deliberate and is documented in `docs/escalation-policy.md` so nobody
is surprised by it.

### Editing the tripwire list

`config/tripwire.uk.yaml` and `config/tripwire.en.yaml` are plain data, meant
to be edited by whoever notices a gap. Test a phrase before trusting it:

```bash
care triage --text "впала в ванній, не можу встати"
```

It prints the verdict and the exact term that matched, so a false positive is
always traceable to one line in one file.

---

## Part 5 — Recovery

**Machine was off across the check-in window.** The next tick sends anything
still inside its validity window and skips what has expired. Nothing is
silently swallowed; `care log` shows what was skipped and why.

**An escalation failed to send.** It is retried on every tick until it
succeeds or the day ends. `care doctor` lists escalations that never made it
out — check it after any outage.

**The state database is lost.** The agent restarts from an empty history: no
memory of yesterday's doses, so the two-day adherence rule needs two fresh
days before it can fire again. Nothing else is affected. The database is at
`~/.hermes/profiles/telegram-family-assistant/workspace/care-state.db` and is
worth backing up precisely because it is the record of what the agent told
whom, and when.

**A bad edit to `meds.yaml` or `roster.yaml`.** The CLI validates on load and
refuses to run on an invalid config rather than half-running with defaults. A
tick that cannot read its config prints the error and exits non-zero, which
surfaces in the cron output rather than failing quietly.
