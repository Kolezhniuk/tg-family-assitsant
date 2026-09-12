# Hermes Contract — Task 0 Evidence

Evidence gathered by reading the installed Hermes source at
`/usr/local/lib/hermes-agent/` and by running read-only CLI commands against
the `telegram-family-assistant` profile. The running gateway was **not**
restarted, stopped, or reconfigured, and no real Telegram message was sent.
All chat ids and bot identifiers below are redacted.

## 0. Installed version and locations

```
$ /root/.local/bin/hermes --version
Hermes Agent v0.18.2 (2026.7.7.2) · upstream 5ecc0798
Install directory: /usr/local/lib/hermes-agent
Install method: git
Python: 3.11.15
OpenAI SDK: 2.24.0
Up to date
```

- Profile home (`HERMES_HOME` for `-p telegram-family-assistant`):
  `/root/.hermes/profiles/telegram-family-assistant/`
- `get_hermes_home()` (`/usr/local/lib/hermes-agent/hermes_constants.py:55`)
  resolves this directory; it is the single source of truth every other
  subsystem below (`hooks/`, `plugins/`, `scripts/`, `cron/`) is scoped under.
- `config.yaml` for this profile (redacted):
  ```yaml
  model:
    default: openai/gpt-5.6-terra
    provider: openrouter
  _config_version: 33
  plugins:
    enabled:
      - model-providers/openrouter
      - platforms/telegram
      - web/exa
  telegram:
    require_mention: true
    allowed_chats: '<FAMILY_GROUP_CHAT_ID>'
    group_allowed_chats: '<FAMILY_GROUP_CHAT_ID>'
  ```
- `hermes cron status` (read-only): gateway running, PIDs redacted-format
  `PID: <pid>, <pid>, <pid>`, ticker heartbeat 30s ago, no active jobs.
- `hermes cron list`: "No scheduled jobs." (profile has none yet.)
- `hermes plugins list`: `telegram-platform` plugin is `enabled`, version
  `1.0.0`, source `bundled` — this is the built-in Telegram adapter itself,
  registered as a `kind: platform` plugin
  (`/usr/local/lib/hermes-agent/plugins/platforms/telegram/plugin.yaml`).

## 1. Pre-model composition point — THE GATE

**Finding: YES, a deterministic pre-model handler exists, but it is a
*plugin* hook (`pre_gateway_dispatch`), not the directory-based "gateway
hook" system. It receives the full, un-trimmed `MessageEvent` dataclass —
every required field is present.**

### Three distinct hook systems exist (verified in source, not just docs)

| System | Registered via | Runs in | Return value honored? |
|---|---|---|---|
| Gateway hooks | `HOOK.yaml`+`handler.py` under `~/.hermes/hooks/` | Gateway process only | `agent:start`/`agent:end`/`agent:step`/`session:*` → **fire-and-forget, return value discarded** (`HookRegistry.emit`, `/usr/local/lib/hermes-agent/gateway/hooks.py:181-199`). `command:<name>` → **decision-capable** via `emit_collect` (deny/handled/rewrite), but only fires for messages `MessageEvent.is_command()` recognizes as slash commands (`gateway/run.py:9653-9706`). |
| Plugin hooks | `ctx.register_hook(name, fn)` in a plugin's `register()` | CLI **and** Gateway | Several are decision-capable, incl. `pre_gateway_dispatch` and `pre_llm_call` (below). |
| Shell hooks | `hooks:` block in `~/.hermes/config.yaml` pointing at scripts | CLI and Gateway | Inspected via `hermes hooks list/test/doctor`. Profile currently has none: `hermes hooks list` → "No shell hooks configured in ~/.hermes/config.yaml." |

### `pre_gateway_dispatch` (plugin hook) — the actual gate

- **Source:** `/usr/local/lib/hermes-agent/gateway/run.py:8905-8944`, inside
  `GatewayRunner._handle_message()`.
- Fires **once per incoming `MessageEvent`**, immediately after the
  `is_internal` check and **before** `_is_user_authorized()` / pairing / any
  agent dispatch (confirmed: the auth check at `run.py:8955-8958` runs
  strictly after the hook block at `run.py:8912-8944`).
- Invoked via `hermes_cli.plugins.invoke_hook("pre_gateway_dispatch", event=event, gateway=self, session_store=self.session_store)`.
  `invoke_hook` is defined at `/usr/local/lib/hermes-agent/hermes_cli/plugins.py:2047-2052`.
- The callback receives the **entire `MessageEvent` object** (not a
  hand-picked dict). `MessageEvent` is defined at
  `/usr/local/lib/hermes-agent/gateway/platforms/base.py:1715-1780` and
  carries, verified field-by-field against the brief's checklist:
  - `message_id: Optional[str]` (base.py:1731) — ✅ message_id
  - `platform_update_id: Optional[int]` (base.py:1740) — ✅ update_id (Telegram's PTB `update_id`; comment confirms "For Telegram this is the `update_id`")
  - `reply_to_message_id: Optional[str]` (base.py:1748) — ✅ reply_to_message_id
  - `timestamp: datetime` (base.py:1780) — ✅ timestamp
  - `text: str` (base.py:1723) — ✅ text
  - `source: SessionSource` carries `chat_id`, `chat_type`, `user_id` (sender_id) — ✅ chat_id, chat_type, sender_id
- Return value contract (source-verified at `run.py:8925-8944`):
  - `{"action": "skip", "reason": "..."}` → message dropped, no reply, no auth, no agent dispatch.
  - `{"action": "rewrite", "text": "..."}` → `event.text` replaced via `dataclasses.replace(event, text=_new_text)`, normal dispatch continues with the modified event.
  - `{"action": "allow"}` / `None` → normal dispatch (auth → pairing → agent loop).
  - First recognized action wins; remaining plugin results ignored; exceptions caught and logged, falls through to normal dispatch (`run.py:8921-8923`).
- **Internal events bypass this hook entirely** (`is_internal` check precedes it) — correct, since those are system-generated (e.g. background-process completions), not user Telegram traffic.

**This satisfies checklist item 1 in full**: all eight required fields
(update_id, chat_id, chat_type, sender_id, message_id, reply_to_message_id,
timestamp, text) are present and observed directly on the object passed to
a hook that fires strictly before authorization and before any model call.

### Where the deterministic directive must live

To use this, our adapter must ship as a Hermes **plugin** (not a
`~/.hermes/hooks/` directory hook — those don't receive `pre_gateway_dispatch`
at all; only plugins registered via `ctx.register_hook()` do). Plugin
discovery for user plugins is `get_hermes_home() / "plugins"`
(`/usr/local/lib/hermes-agent/hermes_cli/plugins.py:1348`) — for this profile
that is `/root/.hermes/profiles/telegram-family-assistant/plugins/` (currently
empty; confirmed by directory listing).

## 2. Attaching a deterministic directive to the model turn

Two independent, source-verified mechanisms, in order of directness:

1. **`pre_gateway_dispatch` → `{"action": "rewrite", "text": ...}`.** The
   handler can prepend a machine-generated directive directly into
   `event.text` before the model ever sees the turn — no cross-hook state
   needed, single hook, single source of truth. (`run.py:8937-8942`.)
2. **`pre_llm_call` (plugin hook) → `{"context": "..."}`.** Fires once per
   turn, before the tool-calling loop, in
   `/usr/local/lib/hermes-agent/agent/turn_context.py:478-529`. Verified
   call site passes `session_id`, `task_id`, `turn_id`, `user_message`,
   `conversation_history`, `is_first_turn`, `model`, `platform`, `sender_id`
   as kwargs (turn_context.py:482-493). Any dict return with a `context` key
   (or a bare non-empty string) is collected, oversized output is spilled to
   disk (`tools/hook_output_spill.py`) and the pieces are joined and injected
   into the user message (**not the system prompt** — turn_context.py:478
   comment is explicit about this).

For a care-check agent, option 1 (rewrite) is the simpler and more
deterministic of the two — it puts the directive directly in front of the
model as literal input with no separate correlation step. Option 2 is the
documented general-purpose mechanism when the directive must be computed
later than dispatch time (e.g. inside the agent's own turn setup) or when
multiple plugins need to compose context independently.

Both are plugin hooks (`ctx.register_hook`) — confirmed callable together
from the same plugin's `register(ctx)` function per
`website/docs/user-guide/features/hooks.md:360-368`, cross-checked against
the two call sites above.

## 3. `hermes send` — delivery result, size limits, timeout, idempotency

**Command inspected:** `/root/.local/bin/hermes send --help`
(`/usr/local/lib/hermes-agent/hermes_cli/send_cmd.py`, 471 lines; core logic
in `/usr/local/lib/hermes-agent/tools/send_message_tool.py`, 1965 lines).

- **Input, no shell interpolation:** message body is taken as a literal
  Python string (positional arg, `--file` contents, or raw `stdin.read()`);
  it is passed to `python-telegram-bot`'s `Bot.send_message()` as a keyword
  argument, never through a shell. No `subprocess`/`shell=True` path exists
  between CLI input and Telegram delivery. (`send_message_tool.py:1116-1230`.)
- **Exit codes** (`send_cmd.py:20-24`, confirmed against real run below):
  `0` = success, `1` = delivery/backend error, `2` = usage error.
- **Live test (safe — invalid chat id, no real send):**
  ```
  $ /root/.local/bin/telegram-family-assistant send --to "telegram:1" --json "TASK0_PROBE_DO_NOT_DELIVER"
  {
    "error": "Telegram send failed: Chat not found"
  }
  EXIT: 1
  ```
  This confirms: success/failure **are** distinguishable — an `"error"` key
  present ⇒ exit 1; a `"success": true` payload (with `"message_id"`) ⇒ exit 0
  (`send_cmd.py:130-139`, `send_message_tool.py:1414-1417`: on success the
  result dict is `{"success": True, "platform": "telegram", "chat_id":
  ..., "message_id": str(last_msg.message_id)}` — the real Telegram
  `message_id` is recoverable).
- **`--list` test (safe, read-only):**
  ```
  $ /root/.local/bin/telegram-family-assistant send --list telegram --json
  {
    "platforms": {
      "telegram": [
        {"id": "<REDACTED_ID>", "name": "<REDACTED_GROUP_NAME>", "type": "group", "thread_id": null},
        {"id": "<REDACTED_ID>", "name": "<REDACTED_NAME>", "type": "dm", "thread_id": null},
        {"id": "<REDACTED_ID>", "name": "<REDACTED_NAME>", "type": "dm", "thread_id": null}
      ]
    }
  }
  EXIT: 0
  ```
  Confirms the channel directory resolves both the family group and the two
  known DMs (`<PARENT_DM_ID>` / `<FAMILY_MEMBER_ID>` in our naming) as valid
  send targets.
- **Max message size:** `TelegramAdapter.MAX_MESSAGE_LENGTH = 4096`
  (`/usr/local/lib/hermes-agent/plugins/platforms/telegram/adapter.py:439`),
  read by `send_message_tool.py:819-839` and used to chunk oversized text via
  `BasePlatformAdapter.truncate_message`. Caption limit for
  media-with-text is a separate, smaller `_TELEGRAM_CAPTION_LIMIT` constant
  (Telegram's 1024-char caption cap) — verified referenced at
  `send_message_tool.py:1222,1417` region.
- **Timeout/retry behavior:** `_send_telegram_message_with_retry`
  (`send_message_tool.py:179-194`) retries up to 3 attempts total.
  `_telegram_retry_delay` (`send_message_tool.py:154-176`) computes backoff
  **only** for rate-limit (`retry_after` / 429) and 5xx-class errors
  (502/503/504/"bad gateway"/"gateway timeout"); a plain "timed out"/"timeout"
  error returns `delay=None`, which the retry loop treats as **immediate
  re-raise, no retry**. No explicit connect/read timeout override is set in
  this file — it relies on `python-telegram-bot`'s `Bot`/`HTTPXRequest`
  defaults (or the configured `TELEGRAM_PROXY`, if set).
- **Idempotency key: NOT SUPPORTED.** `hermes send --help` exposes no such
  flag (`message`, `-t/--to`, `-f/--file`, `-s/--subject`, `-l/--list`,
  `-q/--quiet`, `--json` only — confirmed against the real `--help` output).
  The only de-duplication in `send_message_tool.py` is
  `_maybe_skip_cron_duplicate_send` (`send_message_tool.py:653-679`), which
  is scoped to skipping a `send_message` tool call the **model** makes
  inside a cron job when the target is identical to that job's configured
  cron auto-delivery target — it is not a caller-supplied idempotency token
  and does not protect against duplicate `hermes send` CLI invocations with
  the same content. **Do not rely on `hermes send` for idempotent delivery**;
  any at-most-once guarantee must be implemented by the caller (e.g. a
  local "already sent today" state file/DB row checked before invoking
  `hermes send`).

## 4. Cron — script lookup, locking, exit-code/stdout handling

- **Script lookup path:** scripts MUST resolve under
  `HERMES_HOME/scripts/` (i.e., for this profile,
  `/root/.hermes/profiles/telegram-family-assistant/scripts/`).
  `_run_job_script` (`/usr/local/lib/hermes-agent/cron/scheduler.py:2014-2066`)
  resolves relative paths against this directory and validates absolute/`~`
  paths with `path.relative_to(scripts_dir_resolved)`, returning
  `(False, "Blocked: script path resolves outside the scripts directory ...")`
  on any path-traversal or absolute-path escape attempt.
- **Interpreter selection:** `.sh`/`.bash` → run via `bash` (resolved with
  `shutil.which`); everything else → run via `sys.executable` (the same
  Python interpreter running Hermes). The script's own shebang is
  deliberately ignored (scheduler.py:2072-2094).
- **Timeout:** `subprocess.run(..., timeout=script_timeout, capture_output=True, text=True, cwd=path.parent, env=_sanitize_subprocess_env(...))`
  (scheduler.py:2096-2108). Default `script_timeout` is
  `_DEFAULT_SCRIPT_TIMEOUT = 3600` seconds (1 hour), overridable via
  `HERMES_CRON_SCRIPT_TIMEOUT` env var (scheduler.py:1976-1996).
- **Subprocess env is sanitized** — provider credentials and other
  Hermes-managed secrets are stripped before the script process starts
  (`_sanitize_subprocess_env`), matching terminal/MCP child-process policy.
- **`--no-agent` exit-code/stdout semantics** (verified at
  scheduler.py:2518-2608, comment block is explicit and matches the observed
  code below it):
  - non-zero exit / timeout / crash → `ok=False` → delivered as a
    `⚠ Cron watchdog '<name>' script failed` alert, `success=False`.
  - `wakeAgent: false` gate parsed from stdout → treated as silent (no
    delivery), `success=True`.
  - empty (trimmed) stdout → silent, no delivery, `success=True`.
  - otherwise → stdout (trimmed) delivered **verbatim** as the final
    message, `success=True`.
  - This matches what was told to me in advance ("hermes cron create
    --script X --no-agent runs a script with no model and treats empty
    stdout as silence") — **confirmed against source, not just told**.
- **Overlap / locking:**
  - A **file-based lock** at `~/.hermes/cron/.tick.lock` (per-profile:
    `/root/.hermes/profiles/telegram-family-assistant/cron/.tick.lock`)
    ensures only one scheduler **tick** runs at a time across processes
    (scheduler.py:6-8, `_get_lock_paths` at scheduler.py:555-559).
  - Independently, an in-process `_running_job_ids` set
    (guarded by `_running_lock`) tracks jobs currently executing; if a
    job's next scheduled tick fires while its previous run is still
    in-flight, the scheduler **skips** that tick rather than running
    concurrently or queuing:
    ```
    if job_id in _running_job_ids:
        logger.info("Job '%s' already running — skipping", job.get("name", job_id))
    ```
    (scheduler.py:3676-3677).
  - A separate single-worker `_sequential_pool` additionally serializes
    "workdir" jobs (those that mutate process-global cwd/env state) against
    each other so two different workdir jobs never overlap either
    (scheduler.py:475-485).
  - On gateway shutdown, in-flight cron jobs are marked interrupted
    (`mark_running_jobs_interrupted`, scheduler.py:331-365) so a job whose
    subprocess was killed mid-run can never report a false "ok".

## 5. Chat/user allowlist and mention config — schema verification

Verified in `/usr/local/lib/hermes-agent/gateway/config.py` that the
following keys are recognized and bridged into the Telegram platform config
from `config.yaml` (not merely documented):

- `telegram.allowed_chats` — bridged at `config.py:1201-1202`.
- `telegram.group_allowed_chats` — bridged at `config.py:1203-1204`.
- `telegram.require_mention` (also a top-level `require_mention` fallback) —
  bridged at `config.py:1199-1200`, `1311-1329`.
- `telegram.observe_unmentioned_group_messages` — bridged at
  `config.py:1213-1214`.

This profile's live `config.yaml` sets `require_mention: true`,
`allowed_chats: '<FAMILY_GROUP_CHAT_ID>'`,
`group_allowed_chats: '<FAMILY_GROUP_CHAT_ID>'` (redacted; the real value is
the family group chat id). `observe_unmentioned_group_messages` is not set
in this profile (defaults apply) but the key is confirmed present in the
schema.

BotFather privacy-mode / group-admin requirement for ordinary group message
visibility is documented at
`/usr/local/lib/hermes-agent/website/docs/user-guide/messaging/telegram.md:109-158`
and is a Telegram Bot API server-side behavior, not something enforced by
Hermes code — consistent with what was already known/verified going in.
Not independently re-tested here (would require toggling BotFather settings
against the live bot, out of scope for a read-only Task 0 pass).

## 6. Per-item PASS / NOT_RUN / BLOCKED table

| # | Checklist item | Status | Evidence |
|---|---|---|---|
| 1 | Record installed Hermes version and exact profile/config locations | **PASS** | `hermes --version` output above; profile dir listing; `get_hermes_home()` source. |
| 2 | Prove or disprove a pre-model Telegram update hook/plugin exposing update, chat, sender, message, reply-to, text, timestamp | **PASS** | `pre_gateway_dispatch` plugin hook, `gateway/run.py:8905-8944`, fires before auth/dispatch, receives full `MessageEvent` (`gateway/platforms/base.py:1715-1780`) with every required field. |
| 3 | Verify how a deterministic directive is passed to later model handling | **PASS** | `pre_gateway_dispatch` → `{"action": "rewrite", "text": ...}` (`run.py:8937-8942`) and/or `pre_llm_call` → `{"context": ...}` (`agent/turn_context.py:478-529`), both source-verified. |
| 4 | Verify send input without shell interpolation, bounded timeout, max message size, error shape, receipt/message id | **PASS** | No shell interpolation (`send_message_tool.py:1116-1230`, direct PTB `Bot.send_message`); max length 4096 (`adapter.py:439`); live error probe returned `{"error": "Telegram send failed: Chat not found"}` exit 1; success shape carries `message_id` (`send_message_tool.py:1414-1417`). Timeout is retry-bounded (3 attempts, backoff only on 429/5xx) but has **no explicit hard cap** beyond PTB/HTTPX defaults — noted as a gap, not a failure. |
| 5 | Determine whether send accepts an idempotency key | **PASS (negative result)** | No such flag in `hermes send --help`; only cron-target duplicate-skip exists, which is not a caller idempotency key (`send_message_tool.py:653-679`). Documented as NOT SUPPORTED — do not rely on it. |
| 6 | Verify exact cron creation, script lookup, overlap, exit-code, stdout behavior | **PASS** | `cron create --help` output; `_run_job_script` path-jail (`scheduler.py:2014-2066`); overlap skip (`scheduler.py:3676-3677`); tick-level file lock (`scheduler.py:555-559`); exit-code/stdout semantics (`scheduler.py:2518-2608`). |
| 7 | Verify `allowed_chats`, `group_allowed_chats`, user allowlists, mention behavior, parent-DM admission against installed schema | **PASS** (schema); **NOT_RUN** (live parent-DM admission behavior) | Keys confirmed bridged in `gateway/config.py:1199-1214`; this profile's live config uses all three. Live end-to-end admission test (sending a real message from the parent DM to confirm the gateway lets it through) was **not attempted** — doing so would require sending a real Telegram message, which is out of scope for Task 0's no-live-send constraint. |

### Explicitly NOT_RUN / BLOCKED items (for completeness)

- **Runtime firing of a real `pre_gateway_dispatch` plugin** was **NOT_RUN**:
  confirming this in-process (not just by reading the dispatch code) would
  require installing a plugin under
  `/root/.hermes/profiles/telegram-family-assistant/plugins/` and
  restarting the gateway so `discover_plugins()` picks it up — explicitly
  forbidden by this task's hard constraints ("Do NOT restart, stop, or
  reconfigure the running gateway"). The finding above rests on direct
  reading of the call site and the dataclass it's called with, not on
  observed runtime behavior.
- **BotFather privacy-mode/group-admin requirement**: NOT_RUN live (see §5)
  — matches prior operator knowledge and Hermes's own docs, not
  independently re-verified against the live bot in this pass.
- **Sending any real Telegram message**: correctly BLOCKED by design (hard
  constraint) — only an invalid-chat-id probe and a read-only `--list` were
  run.

## Gate verdict

**Pre-model authenticated ingress with full metadata: YES.** The
`pre_gateway_dispatch` plugin hook is a genuine, source-confirmed
composition point that fires before authorization and before any model
call, carrying every field required by the brief (update_id, chat_id,
chat_type, sender_id, message_id, reply_to_message_id, timestamp, text) on
an unmodified `MessageEvent` object. A deterministic directive can be
attached either by rewriting `event.text` in that same hook, or via a
`pre_llm_call` hook returning `{"context": ...}`. Delivery
(`hermes send`) distinguishes success from failure via an `"error"` key
plus process exit code, and returns a real Telegram `message_id` on
success — so live integration is not blocked by the second gate condition
either. The one caveat worth carrying into design: no idempotency key
exists on the send path, so at-most-once delivery must be enforced by our
own state, not by Hermes.
