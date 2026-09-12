# Family Care-Check Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Telegram agent that checks in on one parent daily, reminds them about medication, and escalates to the family group when replies stop or sound concerning.

**Architecture:** A deterministic Python state machine (`care`) owns every timer, the tripwire word list, and the escalation ladder; it is driven by a `*/5` Hermes cron job with `--no-agent`, so no model is in the safety path. The Hermes agent handles only the conversational edges — judging whether an ordinary-looking reply is *off*, asking one clarifying question, and phrasing DMs warmly — calling back into the same CLI. State is an append-only SQLite event log; day state is derived by folding it, never stored.

**Tech Stack:** Python 3.12, standard library plus PyYAML, `sqlite3`, `pytest`. Scheduling by `hermes cron`, delivery by `hermes send`. No web framework, no ORM, no scheduler library, no HTTP client.

**Spec:** `docs/superpowers/specs/2026-09-12-family-care-check-agent-design.md` — read it alongside this plan. Every task references the spec sections it implements.

## Global Constraints

- **Python 3.12**, standard library plus **PyYAML only**. No other runtime dependency. `pytest` is test-only.
- **No comments and no docstrings in source files.** Every explanation goes in `docs/` or the spec (spec §4.1). A file that needs a comment to be understood should be simplified instead.
- **No dosage amounts anywhere** — not in `meds.yaml`, not in messages, not in the event log. Labels only: "the blood pressure tablet", never "50mg" (spec §7.1).
- **No health advice in any message** — no diagnosis, no symptom interpretation, no medical reassurance, no instruction to take, skip, split or double a dose (spec §1).
- **Inference may add care, never remove it** (spec §3). The deterministic layer never imports, calls, or awaits a model. Escalations are composed and sent by the CLI, never handed to the agent to send.
- **Escalations ignore quiet hours and are never suppressed** by anything the parent says (spec §5.3, §6.3).
- **All schedule times are local** to `roster.timezone`. The event log stores UTC plus a denormalised `local_day` (spec §9).
- **Tripwire and affirmative lists are data**, Ukrainian and English, with `prefix` / `word` / `phrase` matching modes (spec §6.1).
- **Deterministic message text is data** in `config/messages.<lang>.yaml`, never a literal in a `.py` file (spec §10).
- **Tests never touch the network, a model, or the wall clock.** `FixedClock`, in-memory SQLite, `Capture` delivery.
- **Commits carry no co-author or generated-by trailers.** Subject line only, imperative mood.

---

## File Structure

Modules, and the one responsibility each owns:

| File | Responsibility |
|---|---|
| `care/clock.py` | Time. `Clock` protocol, `SystemClock`, `FixedClock`, timezone and quiet-hour maths. The only module that knows about `ZoneInfo`. |
| `care/config.py` | Reading and validating `roster.yaml` + `meds.yaml` into frozen dataclasses. Refuses invalid config rather than defaulting. |
| `care/state.py` | The append-only SQLite event log, event-kind constants, and the derived-state queries everything else asks questions with. |
| `care/triage.py` | Text normalisation, term-list loading, tripwire matching, affirmative matching. Pure; no I/O beyond reading YAML. |
| `care/messages.py` | Loading a language catalogue and rendering deterministic message text. Knows nothing about when a message is sent. |
| `care/delivery.py` | Getting text to a chat id: `HermesSend`, `DryRun`, `Capture`. The only module that shells out. |
| `care/ladder.py` | The check-in state machine, and the `Action` type both state machines emit. |
| `care/meds.py` | The dose state machine and the adherence-repeat rule. |
| `care/conversation.py` | What happens when the parent says something: verdict, clarify budget, stand-down. |
| `care/control.py` | Who is allowed to change what, and pause / skip / stop / resume / schedule. |
| `care/cli.py` | Argument parsing and command wiring. Holds no policy. |

Two of these (`conversation.py`, `control.py`) were added to spec §4 while writing this plan — the reply contract and the consent rules are each their own state machine and do not belong inside `cli.py`.

Data lives outside code: `config/tripwire.{uk,en}.yaml`, `config/affirmatives.{uk,en}.yaml`, `config/messages.{uk,en}.yaml`.

---

## Task 1: Skeleton, packaging, and the clock

Implements spec §4.1 (conventions), §5.3 (quiet hours), §13 (test harness foundation).

**Files:**
- Create: `pyproject.toml`
- Create: `care/__init__.py`
- Create: `care/__main__.py`
- Create: `care/clock.py`
- Create: `bin/care`
- Test: `tests/test_clock.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Clock` protocol with `now_utc() -> datetime`; `SystemClock()`; `FixedClock(moment: datetime)` with `.now_utc()` and `.advance(minutes=0, hours=0, days=0)`; `parse_hhmm(value: str) -> time`; `to_local(moment: datetime, tz: str) -> datetime`; `local_day(moment: datetime, tz: str) -> str`; `local_time_on(day: date, hhmm: str, tz: str) -> datetime` (returns UTC-aware); `in_quiet_hours(moment: datetime, start: str, end: str, tz: str) -> bool`.

- [ ] **Step 1: Create the package skeleton**

`pyproject.toml`:

```toml
[project]
name = "care"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["PyYAML>=6.0"]

[project.optional-dependencies]
test = ["pytest>=8.0"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["care*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`care/__init__.py` — empty file.

`care/__main__.py`:

```python
from care.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

`bin/care`:

```sh
#!/bin/sh
exec python3 -m care "$@"
```

Then: `chmod +x bin/care`

Note: `care/__main__.py` imports `care.cli`, which does not exist until Task 11. That is expected — nothing runs `bin/care` before then, and the tests in Tasks 1–10 import modules directly.

- [ ] **Step 2: Write the failing test**

`tests/test_clock.py`:

```python
from datetime import date, datetime, timedelta, timezone

from care.clock import (
    FixedClock,
    in_quiet_hours,
    local_day,
    local_time_on,
    to_local,
)

KYIV = "Europe/Kyiv"


def test_local_day_uses_configured_timezone():
    moment = datetime(2026, 9, 12, 22, 30, tzinfo=timezone.utc)
    assert local_day(moment, KYIV) == "2026-09-13"


def test_local_time_on_converts_to_utc():
    assert local_time_on(date(2026, 9, 12), "09:00", KYIV) == datetime(
        2026, 9, 12, 6, 0, tzinfo=timezone.utc
    )


def test_quiet_hours_wrap_past_midnight():
    assert in_quiet_hours(local_time_on(date(2026, 9, 12), "22:00", KYIV), "21:30", "08:00", KYIV)
    assert in_quiet_hours(local_time_on(date(2026, 9, 12), "07:00", KYIV), "21:30", "08:00", KYIV)
    assert not in_quiet_hours(local_time_on(date(2026, 9, 12), "09:00", KYIV), "21:30", "08:00", KYIV)


def test_local_times_hold_across_dst_transition():
    before = local_time_on(date(2026, 10, 24), "09:00", KYIV)
    after = local_time_on(date(2026, 10, 26), "09:00", KYIV)
    assert to_local(before, KYIV).hour == 9
    assert to_local(after, KYIV).hour == 9
    assert after - before == timedelta(days=2, hours=1)


def test_fixed_clock_advances():
    clock = FixedClock(datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc))
    clock.advance(hours=3)
    assert clock.now_utc() == datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc)
```

- [ ] **Step 3: Run the test and verify it fails**

Run: `python3 -m pytest tests/test_clock.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'care.clock'`

- [ ] **Step 4: Implement the clock**

`care/clock.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now_utc(self) -> datetime: ...


class SystemClock:
    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class FixedClock:
    moment: datetime

    def now_utc(self) -> datetime:
        return self.moment

    def advance(self, minutes: int = 0, hours: int = 0, days: int = 0) -> None:
        self.moment = self.moment + timedelta(minutes=minutes, hours=hours, days=days)


def parse_hhmm(value: str) -> time:
    hour, _, minute = value.partition(":")
    return time(int(hour), int(minute))


def to_local(moment: datetime, tz: str) -> datetime:
    return moment.astimezone(ZoneInfo(tz))


def local_day(moment: datetime, tz: str) -> str:
    return to_local(moment, tz).date().isoformat()


def local_time_on(day: date, hhmm: str, tz: str) -> datetime:
    naive = datetime.combine(day, parse_hhmm(hhmm))
    return naive.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


def in_quiet_hours(moment: datetime, start: str, end: str, tz: str) -> bool:
    current = to_local(moment, tz).time()
    opening = parse_hhmm(start)
    closing = parse_hhmm(end)
    if opening <= closing:
        return opening <= current < closing
    return current >= opening or current < closing
```

- [ ] **Step 5: Run the tests and verify they pass**

Run: `python3 -m pytest tests/test_clock.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml care/ bin/care tests/test_clock.py
git commit -m "Add package skeleton and timezone-aware clock"
```

---

## Task 2: Configuration loading and validation

Implements spec §5.1 (ladder timings), §7.1 (dose schedule), §8.1 (roster), §10 (delivery mode), §14.4 (timezone).

**Files:**
- Create: `care/config.py`
- Create: `config/roster.example.yaml`
- Create: `config/meds.example.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: frozen dataclasses `Person(chat_id: str, name: str)`, `Dose(id: str, label: str, at: str, days: tuple[str, ...])`, `Ladder(checkin_at: str, nudge_after_minutes: int, escalate_after_minutes: int)`, `DoseLadder(nudge_after_minutes: int, close_after_minutes: int)`, `QuietHours(start: str, end: str)`, `Config(timezone, parent, group_chat_id, family, ladder, dose_ladder, quiet_hours, doses, delivery_mode, languages, message_language, state_db)`; `ConfigError(Exception)`; `load_config(roster_path: Path, meds_path: Path) -> Config`; `Config.is_family(chat_id: str) -> bool`; `Config.dose(dose_id: str) -> Dose | None`; `Config.doses_on(weekday: str) -> tuple[Dose, ...]`.

- [ ] **Step 1: Write the example configs**

`config/roster.example.yaml`:

```yaml
timezone: Europe/Kyiv
message_language: uk
languages: [uk, en]
delivery_mode: dry-run
state_db: ~/.hermes/profiles/telegram-family-assistant/workspace/care-state.db

parent:
  chat_id: "11111111"
  name: Mum

group:
  chat_id: "-1001234567890"

family:
  - chat_id: "22222222"
    name: Dima
  - chat_id: "111111111"
    name: Tema

ladder:
  checkin_at: "09:00"
  nudge_after_minutes: 180
  escalate_after_minutes: 360

dose_ladder:
  nudge_after_minutes: 45
  close_after_minutes: 120

quiet_hours:
  start: "21:30"
  end: "08:00"
```

`config/meds.example.yaml`:

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

- [ ] **Step 2: Write the failing test**

`tests/test_config.py`:

```python
import pytest

from care.config import ConfigError, load_config

ROSTER = "config/roster.example.yaml"
MEDS = "config/meds.example.yaml"


def test_loads_example_config():
    config = load_config(ROSTER, MEDS)
    assert config.timezone == "Europe/Kyiv"
    assert config.parent.chat_id == "11111111"
    assert config.group_chat_id == "-1001234567890"
    assert config.ladder.checkin_at == "09:00"
    assert config.ladder.escalate_after_minutes == 360
    assert config.message_language == "uk"
    assert len(config.doses) == 2


def test_is_family_excludes_parent_and_strangers():
    config = load_config(ROSTER, MEDS)
    assert config.is_family("22222222")
    assert not config.is_family("11111111")
    assert not config.is_family("999999999")


def test_doses_on_respects_day_pattern(tmp_path):
    meds = tmp_path / "meds.yaml"
    meds.write_text(
        "doses:\n"
        "  - id: weekly\n"
        "    label: the weekly tablet\n"
        '    at: "10:00"\n'
        "    days: [mon]\n"
    )
    config = load_config(ROSTER, meds)
    assert [dose.id for dose in config.doses_on("mon")] == ["weekly"]
    assert config.doses_on("tue") == ()


def test_rejects_missing_group(tmp_path):
    roster = tmp_path / "roster.yaml"
    roster.write_text(
        "timezone: Europe/Kyiv\n"
        "parent:\n"
        '  chat_id: "1"\n'
        "  name: Mum\n"
    )
    with pytest.raises(ConfigError, match="group.chat_id"):
        load_config(roster, MEDS)


def test_rejects_duplicate_dose_ids(tmp_path):
    meds = tmp_path / "meds.yaml"
    meds.write_text(
        "doses:\n"
        "  - id: same\n"
        "    label: one\n"
        '    at: "08:00"\n'
        "  - id: same\n"
        "    label: two\n"
        '    at: "20:00"\n'
    )
    with pytest.raises(ConfigError, match="duplicate dose id"):
        load_config(ROSTER, meds)


def test_rejects_bad_time_format(tmp_path):
    meds = tmp_path / "meds.yaml"
    meds.write_text("doses:\n  - id: x\n    label: one\n    at: 8am\n")
    with pytest.raises(ConfigError, match="at"):
        load_config(ROSTER, meds)


def test_rejects_unknown_delivery_mode(tmp_path):
    roster = tmp_path / "roster.yaml"
    roster.write_text(
        "timezone: Europe/Kyiv\n"
        "delivery_mode: telepathy\n"
        "parent:\n"
        '  chat_id: "1"\n'
        "  name: Mum\n"
        "group:\n"
        '  chat_id: "-100"\n'
    )
    with pytest.raises(ConfigError, match="delivery_mode"):
        load_config(roster, MEDS)
```

- [ ] **Step 3: Run the test and verify it fails**

Run: `python3 -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'care.config'`

- [ ] **Step 4: Implement the config loader**

`care/config.py`:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DELIVERY_MODES = ("live", "dry-run", "capture")
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Person:
    chat_id: str
    name: str


@dataclass(frozen=True)
class Dose:
    id: str
    label: str
    at: str
    days: tuple[str, ...]


@dataclass(frozen=True)
class Ladder:
    checkin_at: str
    nudge_after_minutes: int
    escalate_after_minutes: int


@dataclass(frozen=True)
class DoseLadder:
    nudge_after_minutes: int
    close_after_minutes: int


@dataclass(frozen=True)
class QuietHours:
    start: str
    end: str


@dataclass(frozen=True)
class Config:
    timezone: str
    parent: Person
    group_chat_id: str
    family: tuple[Person, ...]
    ladder: Ladder
    dose_ladder: DoseLadder
    quiet_hours: QuietHours
    doses: tuple[Dose, ...]
    delivery_mode: str
    languages: tuple[str, ...]
    message_language: str
    state_db: str

    def is_family(self, chat_id: str) -> bool:
        return any(person.chat_id == chat_id for person in self.family)

    def dose(self, dose_id: str) -> Dose | None:
        for candidate in self.doses:
            if candidate.id == dose_id:
                return candidate
        return None

    def doses_on(self, weekday: str) -> tuple[Dose, ...]:
        return tuple(dose for dose in self.doses if weekday in dose.days)


def _read(path: Path | str) -> dict:
    location = Path(path).expanduser()
    if not location.exists():
        raise ConfigError(f"config file not found: {location}")
    loaded = yaml.safe_load(location.read_text()) or {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"config file is not a mapping: {location}")
    return loaded


def _require(mapping: dict, path: str) -> object:
    node: object = mapping
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"missing required key: {path}")
        node = node[part]
    return node


def _time(value: object, path: str) -> str:
    if not isinstance(value, str) or not TIME_PATTERN.match(value):
        raise ConfigError(f"{path} must be a 24-hour HH:MM string, got {value!r}")
    return value


def _person(entry: object, path: str) -> Person:
    if not isinstance(entry, dict) or "chat_id" not in entry:
        raise ConfigError(f"{path} must have a chat_id")
    return Person(chat_id=str(entry["chat_id"]), name=str(entry.get("name", "")))


def _doses(raw: dict) -> tuple[Dose, ...]:
    entries = raw.get("doses") or []
    if not isinstance(entries, list):
        raise ConfigError("doses must be a list")
    doses: list[Dose] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"doses[{index}] must be a mapping")
        dose_id = str(entry.get("id", "")).strip()
        if not dose_id:
            raise ConfigError(f"doses[{index}] is missing an id")
        if dose_id in seen:
            raise ConfigError(f"duplicate dose id: {dose_id}")
        seen.add(dose_id)
        label = str(entry.get("label", "")).strip()
        if not label:
            raise ConfigError(f"dose {dose_id} is missing a label")
        days = entry.get("days") or list(WEEKDAYS)
        unknown = [day for day in days if day not in WEEKDAYS]
        if unknown:
            raise ConfigError(f"dose {dose_id} has unknown days: {unknown}")
        doses.append(
            Dose(
                id=dose_id,
                label=label,
                at=_time(entry.get("at"), f"dose {dose_id} at"),
                days=tuple(days),
            )
        )
    return tuple(doses)


def load_config(roster_path: Path | str, meds_path: Path | str) -> Config:
    roster = _read(roster_path)
    meds = _read(meds_path)

    delivery_mode = str(roster.get("delivery_mode", "dry-run"))
    if delivery_mode not in DELIVERY_MODES:
        raise ConfigError(f"delivery_mode must be one of {DELIVERY_MODES}, got {delivery_mode!r}")

    ladder = roster.get("ladder") or {}
    dose_ladder = roster.get("dose_ladder") or {}
    quiet = roster.get("quiet_hours") or {}
    family = roster.get("family") or []
    if not isinstance(family, list):
        raise ConfigError("family must be a list")

    group = _require(roster, "group.chat_id")

    return Config(
        timezone=str(roster.get("timezone", "UTC")),
        parent=_person(_require(roster, "parent"), "parent"),
        group_chat_id=str(group),
        family=tuple(_person(entry, f"family[{i}]") for i, entry in enumerate(family)),
        ladder=Ladder(
            checkin_at=_time(ladder.get("checkin_at", "09:00"), "ladder.checkin_at"),
            nudge_after_minutes=int(ladder.get("nudge_after_minutes", 180)),
            escalate_after_minutes=int(ladder.get("escalate_after_minutes", 360)),
        ),
        dose_ladder=DoseLadder(
            nudge_after_minutes=int(dose_ladder.get("nudge_after_minutes", 45)),
            close_after_minutes=int(dose_ladder.get("close_after_minutes", 120)),
        ),
        quiet_hours=QuietHours(
            start=_time(quiet.get("start", "21:30"), "quiet_hours.start"),
            end=_time(quiet.get("end", "08:00"), "quiet_hours.end"),
        ),
        doses=_doses(meds),
        delivery_mode=delivery_mode,
        languages=tuple(roster.get("languages") or ("uk", "en")),
        message_language=str(roster.get("message_language", "uk")),
        state_db=str(roster.get("state_db", "care-state.db")),
    )
```

- [ ] **Step 5: Run the tests and verify they pass**

Run: `python3 -m pytest tests/test_config.py -v`
Expected: 7 passed

- [ ] **Step 6: Commit**

```bash
git add care/config.py config/roster.example.yaml config/meds.example.yaml tests/test_config.py
git commit -m "Add roster and medication config loading with validation"
```

---

## Task 3: The event log

Implements spec §9 (data model), §5.2 (idempotency foundation).

**Files:**
- Create: `care/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: nothing.
- Produces: event-kind constants (`CHECKIN_SENT`, `CHECKIN_NUDGED`, `REPLY_RECEIVED`, `CLARIFY_ASKED`, `ESCALATED_SILENCE`, `ESCALATED_CONCERN`, `STOOD_DOWN`, `ESCALATION_ACKNOWLEDGED`, `DOSE_REMINDED`, `DOSE_NUDGED`, `DOSE_CONFIRMED`, `DOSE_UNCONFIRMED`, `ESCALATED_ADHERENCE`, `SUPPRESSED_QUIET`, `SNOOZED`, `SKIPPED_DAY`, `PAUSED`, `RESUMED`, `STOPPED`, `SCHEDULE_CHANGED`, `UNAUTHORISED_ATTEMPT`, `SEND_FAILED`); `Event(id, ts_utc, local_day, kind, subject, payload)`; `EventLog.open(path)`, `EventLog.in_memory()`, `.append(ts_utc, local_day, kind, subject=None, payload=None) -> int`, `.events(local_day=None, kind=None) -> list[Event]`, `.has(local_day, kind, subject=None) -> bool`, `.last(kind, subject=None, local_day=None) -> Event | None`, `.latest_of(kinds: tuple[str, ...], local_day=None) -> Event | None`.

- [ ] **Step 1: Write the failing test**

`tests/test_state.py`:

```python
from datetime import datetime, timezone

from care.state import CHECKIN_SENT, DOSE_CONFIRMED, REPLY_RECEIVED, EventLog


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 12, hour, minute, tzinfo=timezone.utc)


def test_append_and_read_back():
    log = EventLog.in_memory()
    log.append(at(6), "2026-09-12", CHECKIN_SENT)
    events = log.events("2026-09-12")
    assert len(events) == 1
    assert events[0].kind == CHECKIN_SENT
    assert events[0].ts_utc == at(6)
    assert events[0].payload == {}


def test_payload_round_trips():
    log = EventLog.in_memory()
    log.append(at(7), "2026-09-12", REPLY_RECEIVED, payload={"text": "все добре"})
    assert log.events()[0].payload["text"] == "все добре"


def test_has_is_scoped_to_day_and_subject():
    log = EventLog.in_memory()
    log.append(at(6), "2026-09-12", DOSE_CONFIRMED, subject="morning-bp")
    assert log.has("2026-09-12", DOSE_CONFIRMED, "morning-bp")
    assert not log.has("2026-09-12", DOSE_CONFIRMED, "evening")
    assert not log.has("2026-09-13", DOSE_CONFIRMED, "morning-bp")


def test_last_returns_most_recent():
    log = EventLog.in_memory()
    log.append(at(6), "2026-09-12", REPLY_RECEIVED, payload={"text": "first"})
    log.append(at(8), "2026-09-12", REPLY_RECEIVED, payload={"text": "second"})
    assert log.last(REPLY_RECEIVED).payload["text"] == "second"


def test_last_returns_none_when_absent():
    assert EventLog.in_memory().last(CHECKIN_SENT) is None


def test_events_are_ordered_by_time():
    log = EventLog.in_memory()
    log.append(at(9), "2026-09-12", REPLY_RECEIVED, payload={"text": "late"})
    log.append(at(6), "2026-09-12", CHECKIN_SENT)
    assert [event.kind for event in log.events()] == [CHECKIN_SENT, REPLY_RECEIVED]


def test_persists_to_disk(tmp_path):
    location = tmp_path / "care-state.db"
    first = EventLog.open(location)
    first.append(at(6), "2026-09-12", CHECKIN_SENT)
    second = EventLog.open(location)
    assert second.has("2026-09-12", CHECKIN_SENT)
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `python3 -m pytest tests/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'care.state'`

- [ ] **Step 3: Implement the event log**

`care/state.py`:

```python
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

CHECKIN_SENT = "checkin_sent"
CHECKIN_NUDGED = "checkin_nudged"
REPLY_RECEIVED = "reply_received"
CLARIFY_ASKED = "clarify_asked"
ESCALATED_SILENCE = "escalated_silence"
ESCALATED_CONCERN = "escalated_concern"
STOOD_DOWN = "stood_down"
ESCALATION_ACKNOWLEDGED = "escalation_acknowledged"
DOSE_REMINDED = "dose_reminded"
DOSE_NUDGED = "dose_nudged"
DOSE_CONFIRMED = "dose_confirmed"
DOSE_UNCONFIRMED = "dose_unconfirmed"
ESCALATED_ADHERENCE = "escalated_adherence"
SUPPRESSED_QUIET = "suppressed_quiet"
SNOOZED = "snoozed"
SKIPPED_DAY = "skipped_day"
PAUSED = "paused"
RESUMED = "resumed"
STOPPED = "stopped"
SCHEDULE_CHANGED = "schedule_changed"
UNAUTHORISED_ATTEMPT = "unauthorised_attempt"
SEND_FAILED = "send_failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id        INTEGER PRIMARY KEY,
  ts_utc    TEXT NOT NULL,
  local_day TEXT NOT NULL,
  kind      TEXT NOT NULL,
  subject   TEXT,
  payload   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_day_kind ON events (local_day, kind);
"""


@dataclass(frozen=True)
class Event:
    id: int
    ts_utc: datetime
    local_day: str
    kind: str
    subject: str | None
    payload: dict = field(default_factory=dict)


class EventLog:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    @classmethod
    def open(cls, path: Path | str) -> "EventLog":
        location = Path(path).expanduser()
        location.parent.mkdir(parents=True, exist_ok=True)
        return cls(sqlite3.connect(location))

    @classmethod
    def in_memory(cls) -> "EventLog":
        return cls(sqlite3.connect(":memory:"))

    def append(
        self,
        ts_utc: datetime,
        local_day: str,
        kind: str,
        subject: str | None = None,
        payload: dict | None = None,
    ) -> int:
        cursor = self._connection.execute(
            "INSERT INTO events (ts_utc, local_day, kind, subject, payload)"
            " VALUES (?, ?, ?, ?, ?)",
            (ts_utc.isoformat(), local_day, kind, subject, json.dumps(payload or {})),
        )
        self._connection.commit()
        return int(cursor.lastrowid)

    def events(self, local_day: str | None = None, kind: str | None = None) -> list[Event]:
        query = "SELECT id, ts_utc, local_day, kind, subject, payload FROM events"
        clauses: list[str] = []
        values: list[str] = []
        if local_day is not None:
            clauses.append("local_day = ?")
            values.append(local_day)
        if kind is not None:
            clauses.append("kind = ?")
            values.append(kind)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY ts_utc, id"
        rows = self._connection.execute(query, values).fetchall()
        return [
            Event(
                id=row[0],
                ts_utc=datetime.fromisoformat(row[1]),
                local_day=row[2],
                kind=row[3],
                subject=row[4],
                payload=json.loads(row[5]),
            )
            for row in rows
        ]

    def has(self, local_day: str, kind: str, subject: str | None = None) -> bool:
        return any(
            event.subject == subject or subject is None
            for event in self.events(local_day, kind)
        )

    def last(
        self,
        kind: str,
        subject: str | None = None,
        local_day: str | None = None,
    ) -> Event | None:
        matches = [
            event
            for event in self.events(local_day, kind)
            if subject is None or event.subject == subject
        ]
        return matches[-1] if matches else None

    def latest_of(
        self,
        kinds: tuple[str, ...],
        local_day: str | None = None,
    ) -> Event | None:
        matches = [event for event in self.events(local_day) if event.kind in kinds]
        return matches[-1] if matches else None
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `python3 -m pytest tests/test_state.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add care/state.py tests/test_state.py
git commit -m "Add append-only SQLite event log"
```

---

## Task 4: Triage — tripwire and affirmative matching

Implements spec §6.1 (two layers, matching modes), §7.2 (affirmative fast path).

**Files:**
- Create: `care/triage.py`
- Create: `config/tripwire.uk.yaml`
- Create: `config/tripwire.en.yaml`
- Create: `config/affirmatives.uk.yaml`
- Create: `config/affirmatives.en.yaml`
- Test: `tests/test_triage.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Term(match: str, value: str, category: str, language: str)`; `Verdict(verdict: str, matched: tuple[str, ...], categories: tuple[str, ...])` where `verdict` is `"tripwire"` or `"clear"`; `normalise(text: str) -> list[str]`; `load_terms(directory: Path | str, prefix: str, languages: tuple[str, ...]) -> tuple[Term, ...]`; `triage(text: str, terms: tuple[Term, ...]) -> Verdict`; `is_affirmative(text: str, terms: tuple[Term, ...]) -> bool`.

- [ ] **Step 1: Write the term lists**

`config/tripwire.uk.yaml`:

```yaml
language: uk
terms:
  - {match: prefix, value: "впал", category: fall}
  - {match: prefix, value: "упал", category: fall}
  - {match: prefix, value: "падаю", category: fall}
  - {match: phrase, value: "не можу встати", category: fall}
  - {match: phrase, value: "не можу піднятися", category: fall}
  - {match: phrase, value: "болить у грудях", category: chest}
  - {match: phrase, value: "біль у грудях", category: chest}
  - {match: phrase, value: "тисне в грудях", category: chest}
  - {match: phrase, value: "серце болить", category: chest}
  - {match: phrase, value: "не можу дихати", category: breathing}
  - {match: phrase, value: "важко дихати", category: breathing}
  - {match: prefix, value: "задиха", category: breathing}
  - {match: prefix, value: "кровотеч", category: bleeding}
  - {match: phrase, value: "йде кров", category: bleeding}
  - {match: prefix, value: "запаморочен", category: confusion}
  - {match: prefix, value: "заплута", category: confusion}
  - {match: phrase, value: "не розумію де я", category: confusion}
  - {match: prefix, value: "оніміл", category: weakness}
  - {match: prefix, value: "допоможіть", category: help}
  - {match: prefix, value: "поможіть", category: help}
  - {match: prefix, value: "рятуйте", category: help}
  - {match: phrase, value: "викличте швидку", category: help}
  - {match: phrase, value: "потрібна швидка", category: help}
  - {match: phrase, value: "мені погано", category: not_okay}
  - {match: phrase, value: "мені зле", category: not_okay}
  - {match: phrase, value: "дуже погано", category: not_okay}
  - {match: phrase, value: "не приймаю ліки", category: meds_stopped}
  - {match: phrase, value: "перестала пити таблетки", category: meds_stopped}
  - {match: phrase, value: "закінчилися ліки", category: meds_stopped}
  - {match: phrase, value: "ліки закінчилися", category: meds_stopped}
```

`config/tripwire.en.yaml`:

```yaml
language: en
terms:
  - {match: phrase, value: "i fell", category: fall}
  - {match: phrase, value: "i've fallen", category: fall}
  - {match: phrase, value: "had a fall", category: fall}
  - {match: phrase, value: "can't get up", category: fall}
  - {match: phrase, value: "chest pain", category: chest}
  - {match: phrase, value: "pain in my chest", category: chest}
  - {match: phrase, value: "can't breathe", category: breathing}
  - {match: phrase, value: "short of breath", category: breathing}
  - {match: word, value: "bleeding", category: bleeding}
  - {match: word, value: "dizzy", category: confusion}
  - {match: word, value: "confused", category: confusion}
  - {match: phrase, value: "call an ambulance", category: help}
  - {match: word, value: "help", category: help}
  - {match: phrase, value: "not okay", category: not_okay}
  - {match: phrase, value: "feel terrible", category: not_okay}
  - {match: phrase, value: "stopped taking", category: meds_stopped}
  - {match: phrase, value: "ran out of", category: meds_stopped}
```

`config/affirmatives.uk.yaml`:

```yaml
language: uk
terms:
  - {match: word, value: "так", category: yes}
  - {match: word, value: "ок", category: yes}
  - {match: word, value: "окей", category: yes}
  - {match: word, value: "ага", category: yes}
  - {match: word, value: "угу", category: yes}
  - {match: prefix, value: "добре", category: yes}
  - {match: prefix, value: "гаразд", category: yes}
  - {match: prefix, value: "готово", category: yes}
  - {match: prefix, value: "випи", category: taken}
  - {match: prefix, value: "прийня", category: taken}
  - {match: prefix, value: "зробил", category: taken}
```

`config/affirmatives.en.yaml`:

```yaml
language: en
terms:
  - {match: word, value: "yes", category: yes}
  - {match: word, value: "yep", category: yes}
  - {match: word, value: "yeah", category: yes}
  - {match: word, value: "ok", category: yes}
  - {match: word, value: "okay", category: yes}
  - {match: word, value: "done", category: taken}
  - {match: phrase, value: "took it", category: taken}
  - {match: phrase, value: "taken it", category: taken}
```

Note: `yes` is a YAML 1.1 boolean. `yaml.safe_load` parses the bare word `yes` as `True`, so `category: yes` becomes `category: True`. Quote it — write `category: "yes"` in all four files above wherever the value is `yes`. Do this while creating the files; the test in Step 2 asserts it.

- [ ] **Step 2: Write the failing test**

`tests/test_triage.py`:

```python
from care.triage import is_affirmative, load_terms, normalise, triage

TRIPWIRE = load_terms("config", "tripwire", ("uk", "en"))
AFFIRMATIVE = load_terms("config", "affirmatives", ("uk", "en"))


def test_categories_are_strings_not_booleans():
    assert all(isinstance(term.category, str) for term in AFFIRMATIVE)


def test_normalise_strips_punctuation_and_case():
    assert normalise("Все ДОБРЕ, дякую!") == ["все", "добре", "дякую"]


def test_prefix_term_matches_every_inflection():
    for text in ("я впала", "він впав", "вони впали"):
        assert triage(text, TRIPWIRE).verdict == "tripwire"


def test_prefix_term_reports_what_matched():
    verdict = triage("я впала у ванній", TRIPWIRE)
    assert verdict.matched == ("впал",)
    assert verdict.categories == ("fall",)


def test_phrase_term_matches_word_sequence():
    assert triage("не можу встати з підлоги", TRIPWIRE).verdict == "tripwire"


def test_phrase_term_does_not_match_scattered_words():
    assert triage("встати не можу я рано", TRIPWIRE).verdict == "clear"


def test_ordinary_reply_is_clear():
    assert triage("все добре, снідаю", TRIPWIRE).verdict == "clear"


def test_english_terms_work_too():
    assert triage("I fell in the kitchen", TRIPWIRE).verdict == "tripwire"


def test_affirmative_recognised_in_both_languages():
    assert is_affirmative("так", AFFIRMATIVE)
    assert is_affirmative("випила", AFFIRMATIVE)
    assert is_affirmative("прийняв", AFFIRMATIVE)
    assert is_affirmative("done", AFFIRMATIVE)


def test_non_affirmative_is_not_confirmation():
    assert not is_affirmative("ще ні", AFFIRMATIVE)
    assert not is_affirmative("пізніше", AFFIRMATIVE)
```

- [ ] **Step 3: Run the test and verify it fails**

Run: `python3 -m pytest tests/test_triage.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'care.triage'`

- [ ] **Step 4: Implement triage**

`care/triage.py`:

```python
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml

TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
HOMOGLYPHS = str.maketrans({"ё": "е", "ґ": "г", "’": "", "'": "", "ʼ": ""})


@dataclass(frozen=True)
class Term:
    match: str
    value: str
    category: str
    language: str


@dataclass(frozen=True)
class Verdict:
    verdict: str
    matched: tuple[str, ...]
    categories: tuple[str, ...]


def normalise(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", text).casefold().translate(HOMOGLYPHS)
    return TOKEN_PATTERN.findall(folded)


def load_terms(
    directory: Path | str,
    prefix: str,
    languages: tuple[str, ...],
) -> tuple[Term, ...]:
    base = Path(directory).expanduser()
    terms: list[Term] = []
    for language in languages:
        location = base / f"{prefix}.{language}.yaml"
        if not location.exists():
            continue
        document = yaml.safe_load(location.read_text()) or {}
        for entry in document.get("terms") or []:
            terms.append(
                Term(
                    match=str(entry["match"]),
                    value=str(entry["value"]).casefold(),
                    category=str(entry.get("category", "")),
                    language=language,
                )
            )
    return tuple(terms)


def _hits(tokens: list[str], term: Term) -> bool:
    if term.match == "word":
        return term.value in tokens
    if term.match == "prefix":
        return any(token.startswith(term.value) for token in tokens)
    if term.match == "phrase":
        wanted = normalise(term.value)
        if not wanted:
            return False
        span = len(wanted)
        return any(
            tokens[index : index + span] == wanted
            for index in range(len(tokens) - span + 1)
        )
    raise ValueError(f"unknown match mode: {term.match}")


def _matching(text: str, terms: tuple[Term, ...]) -> list[Term]:
    tokens = normalise(text)
    return [term for term in terms if _hits(tokens, term)]


def triage(text: str, terms: tuple[Term, ...]) -> Verdict:
    hits = _matching(text, terms)
    if not hits:
        return Verdict(verdict="clear", matched=(), categories=())
    return Verdict(
        verdict="tripwire",
        matched=tuple(dict.fromkeys(term.value for term in hits)),
        categories=tuple(dict.fromkeys(term.category for term in hits)),
    )


def is_affirmative(text: str, terms: tuple[Term, ...]) -> bool:
    return bool(_matching(text, terms))
```

- [ ] **Step 5: Run the tests and verify they pass**

Run: `python3 -m pytest tests/test_triage.py -v`
Expected: 10 passed

- [ ] **Step 6: Commit**

```bash
git add care/triage.py config/tripwire.*.yaml config/affirmatives.*.yaml tests/test_triage.py
git commit -m "Add Ukrainian and English tripwire and affirmative matching"
```

---

## Task 5: The message catalogue

Implements spec §10 (message text is data), §7.3 (unconfirmed is not missed), §1 (no health advice).

**Files:**
- Create: `care/messages.py`
- Create: `config/messages.uk.yaml`
- Create: `config/messages.en.yaml`
- Test: `tests/test_messages.py`

**Interfaces:**
- Consumes: `care.config.Config`, `care.config.Dose`.
- Produces: `Catalogue.load(directory: Path | str, language: str) -> Catalogue`; `Catalogue.render(key: str, **values) -> str`; `MessageError(Exception)`.

- [ ] **Step 1: Write the catalogues**

`config/messages.uk.yaml`:

```yaml
language: uk
messages:
  checkin: "Доброго ранку, {parent_name}. Як ви сьогодні?"
  checkin_nudge: "Просто перевіряю, {parent_name}. Дайте знати, що у вас все гаразд."
  dose_reminder: "Нагадування: {dose_label}."
  dose_nudge: "Ще раз нагадую про {dose_label}. Напишіть «так», коли приймете."
  acknowledge_escalation: "Я повідомив(ла) родину про це повідомлення. Хочете, щоб хтось зателефонував?"
  escalated_silence: |
    Не маю відповіді від {parent_name} сьогодні.
    Надіслано о {checkin_time}, нагадування о {nudge_time}, відповіді немає станом на {now_time}.
    Останній контакт: {last_contact}.
    Будь ласка, зв'яжіться з нею. Відповідайте на це повідомлення, коли хтось додзвониться.
  escalated_concern: |
    {parent_name} написала о {when}:
    «{quote}»
    Я не оцінюю стан здоров'я — передаю дослівно.
    Будь ласка, зв'яжіться з нею. Відповідайте на це повідомлення, коли хтось додзвониться.
  stood_down: "{parent_name} відповіла о {when}: «{quote}». Відбій."
  escalated_adherence: |
    {parent_name} не підтвердила {dose_label} — {reason}.
    Це не означає, що ліки не прийнято: підтвердження могло просто не надійти.
    Варто перепитати при нагоді.
  stopped_notice: "{parent_name} попросила зупинити щоденні перевірки. Я більше не пишу їй. Відновити може будь-хто з родини."
  paused_notice: "Перевірки призупинено до {until}."
  resumed_notice: "Перевірки відновлено."
  skipped_notice: "Сьогодні перевірок і нагадувань не буде — на прохання {actor_name}."
  schedule_changed: "Розклад змінено: {detail} (змінив(ла) {actor_name})."
```

`config/messages.en.yaml`:

```yaml
language: en
messages:
  checkin: "Good morning, {parent_name}. How are you today?"
  checkin_nudge: "Just checking in, {parent_name}. Let me know you're alright."
  dose_reminder: "Reminder: {dose_label}."
  dose_nudge: "Another reminder about {dose_label}. Say \"yes\" once you've taken it."
  acknowledge_escalation: "I've let the family know about that message. Would you like someone to call?"
  escalated_silence: |
    No reply from {parent_name} today.
    Sent at {checkin_time}, reminded at {nudge_time}, still nothing as of {now_time}.
    Last contact: {last_contact}.
    Please reach her. Reply to this message once someone has.
  escalated_concern: |
    {parent_name} wrote at {when}:
    "{quote}"
    I don't assess health — this is passed on word for word.
    Please reach her. Reply to this message once someone has.
  stood_down: "{parent_name} replied at {when}: \"{quote}\". Stand down."
  escalated_adherence: |
    {parent_name} hasn't confirmed {dose_label} — {reason}.
    This does not mean the medication was skipped: the confirmation may simply not have arrived.
    Worth asking when convenient.
  stopped_notice: "{parent_name} asked to stop the daily check-ins. I'm no longer messaging her. Any family member can resume."
  paused_notice: "Check-ins paused until {until}."
  resumed_notice: "Check-ins resumed."
  skipped_notice: "No check-in or reminders today — at {actor_name}'s request."
  schedule_changed: "Schedule changed: {detail} (by {actor_name})."
```

- [ ] **Step 2: Write the failing test**

`tests/test_messages.py`:

```python
import pytest

from care.messages import Catalogue, MessageError

UK = Catalogue.load("config", "uk")
EN = Catalogue.load("config", "en")


def test_renders_named_values():
    assert UK.render("checkin", parent_name="Мамо") == "Доброго ранку, Мамо. Як ви сьогодні?"


def test_both_catalogues_define_the_same_keys():
    assert set(UK.keys()) == set(EN.keys())


def test_unknown_key_is_an_error():
    with pytest.raises(MessageError, match="no such message"):
        UK.render("nonexistent")


def test_missing_value_is_an_error_not_a_stray_brace():
    with pytest.raises(MessageError, match="parent_name"):
        UK.render("checkin")


def test_adherence_message_does_not_assert_a_missed_dose():
    text = EN.render(
        "escalated_adherence",
        parent_name="Mum",
        dose_label="the evening tablet",
        reason="two days running",
    )
    assert "does not mean the medication was skipped" in text
    assert "missed" not in text.lower()
```

- [ ] **Step 3: Run the test and verify it fails**

Run: `python3 -m pytest tests/test_messages.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'care.messages'`

- [ ] **Step 4: Implement the catalogue**

`care/messages.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class MessageError(Exception):
    pass


@dataclass(frozen=True)
class Catalogue:
    language: str
    templates: dict[str, str]

    @classmethod
    def load(cls, directory: Path | str, language: str) -> "Catalogue":
        location = Path(directory).expanduser() / f"messages.{language}.yaml"
        if not location.exists():
            raise MessageError(f"no message catalogue for language {language!r}: {location}")
        document = yaml.safe_load(location.read_text()) or {}
        templates = document.get("messages") or {}
        if not templates:
            raise MessageError(f"message catalogue is empty: {location}")
        return cls(language=language, templates={str(k): str(v) for k, v in templates.items()})

    def keys(self) -> set[str]:
        return set(self.templates)

    def render(self, key: str, **values: object) -> str:
        if key not in self.templates:
            raise MessageError(f"no such message: {key}")
        try:
            return self.templates[key].format(**values).strip()
        except KeyError as missing:
            raise MessageError(
                f"message {key!r} needs a value for {missing.args[0]!r}"
            ) from missing
```

- [ ] **Step 5: Run the tests and verify they pass**

Run: `python3 -m pytest tests/test_messages.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add care/messages.py config/messages.*.yaml tests/test_messages.py
git commit -m "Add per-language message catalogue"
```

---

## Task 6: Delivery

Implements spec §10 (delivery modes, retry on failure).

**Files:**
- Create: `care/delivery.py`
- Test: `tests/test_delivery.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Delivery` protocol with `send(chat_id: str, text: str) -> bool`; `Capture()` with `.sent: list[tuple[str, str]]` and `.fail_next(count: int)`; `DryRun(stream)`; `HermesSend(profile: str, runner=subprocess.run)`; `build(mode: str, profile: str, stream=sys.stdout) -> Delivery`.

- [ ] **Step 1: Write the failing test**

`tests/test_delivery.py`:

```python
import io

from care.delivery import Capture, DryRun, HermesSend, build


def test_capture_records_sends():
    delivery = Capture()
    assert delivery.send("-100", "hello")
    assert delivery.sent == [("-100", "hello")]


def test_capture_can_simulate_failure():
    delivery = Capture()
    delivery.fail_next(1)
    assert not delivery.send("-100", "first")
    assert delivery.send("-100", "second")
    assert delivery.sent == [("-100", "second")]


def test_dry_run_prints_and_sends_nothing():
    stream = io.StringIO()
    assert DryRun(stream).send("-100", "hello")
    printed = stream.getvalue()
    assert "-100" in printed
    assert "hello" in printed


def test_hermes_send_shells_out_with_text_on_stdin():
    calls = []

    class Result:
        returncode = 0
        stderr = ""

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return Result()

    delivery = HermesSend(profile="telegram-family-assistant", runner=runner)
    assert delivery.send("-1001234567890", "падіння")

    argv, kwargs = calls[0]
    assert argv[:3] == ["hermes", "-p", "telegram-family-assistant"]
    assert "send" in argv
    assert "--to" in argv
    assert "telegram:-1001234567890" in argv
    assert kwargs["input"] == "падіння"


def test_hermes_send_reports_failure():
    class Result:
        returncode = 1
        stderr = "chat not found"

    delivery = HermesSend(profile="p", runner=lambda argv, **kwargs: Result())
    assert not delivery.send("-100", "hello")


def test_build_selects_mode():
    assert isinstance(build("capture", "p"), Capture)
    assert isinstance(build("dry-run", "p", io.StringIO()), DryRun)
    assert isinstance(build("live", "p"), HermesSend)
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `python3 -m pytest tests/test_delivery.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'care.delivery'`

- [ ] **Step 3: Implement delivery**

`care/delivery.py`:

```python
from __future__ import annotations

import subprocess
import sys
from typing import Protocol, TextIO


class Delivery(Protocol):
    def send(self, chat_id: str, text: str) -> bool: ...


class Capture:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self._failures = 0

    def fail_next(self, count: int) -> None:
        self._failures = count

    def send(self, chat_id: str, text: str) -> bool:
        if self._failures > 0:
            self._failures -= 1
            return False
        self.sent.append((chat_id, text))
        return True


class DryRun:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def send(self, chat_id: str, text: str) -> bool:
        self._stream.write(f"[dry-run] telegram:{chat_id}\n{text}\n\n")
        return True


class HermesSend:
    def __init__(self, profile: str, runner=subprocess.run) -> None:
        self._profile = profile
        self._runner = runner

    def send(self, chat_id: str, text: str) -> bool:
        result = self._runner(
            [
                "hermes",
                "-p",
                self._profile,
                "send",
                "--to",
                f"telegram:{chat_id}",
                "--quiet",
            ],
            input=text,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0


def build(mode: str, profile: str, stream: TextIO | None = None) -> Delivery:
    if mode == "capture":
        return Capture()
    if mode == "dry-run":
        return DryRun(stream)
    if mode == "live":
        return HermesSend(profile)
    raise ValueError(f"unknown delivery mode: {mode}")
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `python3 -m pytest tests/test_delivery.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add care/delivery.py tests/test_delivery.py
git commit -m "Add delivery layer with live, dry-run and capture modes"
```

---

## Task 7: The check-in ladder

Implements spec §5.1 (timings), §5.2 (tick semantics, idempotency, missed windows), §5.3 (quiet hours).

**Files:**
- Create: `care/ladder.py`
- Test: `tests/test_ladder.py`

**Interfaces:**
- Consumes: `care.config.Config`, `care.state.EventLog` and its kind constants, `care.messages.Catalogue`, `care.clock` helpers.
- Produces: `Action(kind: str, chat_id: str, text: str, subject: str | None = None)`; `due_actions(config: Config, log: EventLog, catalogue: Catalogue, now_utc: datetime) -> list[Action]`; `is_stopped(log: EventLog) -> bool`; `is_paused(config: Config, log: EventLog, now_utc: datetime) -> bool`; `is_skipped(log: EventLog, local_day: str) -> bool`; `last_contact(log: EventLog) -> datetime | None`.

- [ ] **Step 1: Write the failing test**

`tests/test_ladder.py`:

```python
from datetime import date, datetime, timezone

from care.config import load_config
from care.clock import local_day, local_time_on
from care.ladder import due_actions
from care.messages import Catalogue
from care.state import (
    CHECKIN_NUDGED,
    CHECKIN_SENT,
    ESCALATED_SILENCE,
    REPLY_RECEIVED,
    EventLog,
)

CONFIG = load_config("config/roster.example.yaml", "config/meds.example.yaml")
CATALOGUE = Catalogue.load("config", "uk")
TZ = CONFIG.timezone
DAY = date(2026, 9, 12)


def moment(hhmm: str) -> datetime:
    return local_time_on(DAY, hhmm, TZ)


def record(log: EventLog, hhmm: str, kind: str, **payload) -> None:
    when = moment(hhmm)
    log.append(when, local_day(when, TZ), kind, payload=payload or None)


def kinds(actions) -> list[str]:
    return [action.kind for action in actions]


def test_checkin_is_due_at_the_configured_time():
    log = EventLog.in_memory()
    assert kinds(due_actions(CONFIG, log, CATALOGUE, moment("09:00"))) == [CHECKIN_SENT]


def test_checkin_is_not_due_before_its_time():
    log = EventLog.in_memory()
    assert due_actions(CONFIG, log, CATALOGUE, moment("08:30")) == []


def test_checkin_goes_to_the_parent():
    log = EventLog.in_memory()
    action = due_actions(CONFIG, log, CATALOGUE, moment("09:00"))[0]
    assert action.chat_id == CONFIG.parent.chat_id


def test_checkin_is_sent_once_per_day():
    log = EventLog.in_memory()
    record(log, "09:00", CHECKIN_SENT)
    assert due_actions(CONFIG, log, CATALOGUE, moment("09:05")) == []


def test_missed_window_still_sends_late():
    log = EventLog.in_memory()
    assert kinds(due_actions(CONFIG, log, CATALOGUE, moment("09:20"))) == [CHECKIN_SENT]


def test_nudge_after_three_hours_of_silence():
    log = EventLog.in_memory()
    record(log, "09:00", CHECKIN_SENT)
    assert due_actions(CONFIG, log, CATALOGUE, moment("11:30")) == []
    assert kinds(due_actions(CONFIG, log, CATALOGUE, moment("12:00"))) == [CHECKIN_NUDGED]


def test_reply_stops_the_ladder():
    log = EventLog.in_memory()
    record(log, "09:00", CHECKIN_SENT)
    record(log, "09:30", REPLY_RECEIVED, text="все добре")
    assert due_actions(CONFIG, log, CATALOGUE, moment("12:00")) == []
    assert due_actions(CONFIG, log, CATALOGUE, moment("15:00")) == []


def test_escalation_after_six_hours_goes_to_the_group():
    log = EventLog.in_memory()
    record(log, "09:00", CHECKIN_SENT)
    record(log, "12:00", CHECKIN_NUDGED)
    actions = due_actions(CONFIG, log, CATALOGUE, moment("15:00"))
    assert kinds(actions) == [ESCALATED_SILENCE]
    assert actions[0].chat_id == CONFIG.group_chat_id


def test_nudging_stops_after_escalation():
    log = EventLog.in_memory()
    record(log, "09:00", CHECKIN_SENT)
    record(log, "12:00", CHECKIN_NUDGED)
    record(log, "15:00", ESCALATED_SILENCE)
    assert due_actions(CONFIG, log, CATALOGUE, moment("16:00")) == []
    assert due_actions(CONFIG, log, CATALOGUE, moment("18:00")) == []


def test_checkin_inside_quiet_hours_is_suppressed(tmp_path):
    roster = tmp_path / "roster.yaml"
    roster.write_text(
        "timezone: Europe/Kyiv\n"
        "message_language: uk\n"
        "parent: {chat_id: \"1\", name: Mum}\n"
        "group: {chat_id: \"-100\"}\n"
        "ladder: {checkin_at: \"22:00\"}\n"
        "quiet_hours: {start: \"21:30\", end: \"08:00\"}\n"
    )
    config = load_config(roster, "config/meds.example.yaml")
    log = EventLog.in_memory()
    assert due_actions(config, log, CATALOGUE, local_time_on(DAY, "22:00", TZ)) == []


def test_tick_is_idempotent_within_a_minute():
    log = EventLog.in_memory()
    first = due_actions(CONFIG, log, CATALOGUE, moment("09:00"))
    when = moment("09:00")
    log.append(when, local_day(when, TZ), first[0].kind)
    assert due_actions(CONFIG, log, CATALOGUE, moment("09:00")) == []
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `python3 -m pytest tests/test_ladder.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'care.ladder'`

- [ ] **Step 3: Implement the ladder**

`care/ladder.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from care.clock import in_quiet_hours, local_day, local_time_on, to_local
from care.config import Config
from care.messages import Catalogue
from care.state import (
    CHECKIN_NUDGED,
    CHECKIN_SENT,
    ESCALATED_SILENCE,
    PAUSED,
    REPLY_RECEIVED,
    RESUMED,
    SKIPPED_DAY,
    STOPPED,
    EventLog,
)


@dataclass(frozen=True)
class Action:
    kind: str
    chat_id: str
    text: str
    subject: str | None = None


def is_stopped(log: EventLog) -> bool:
    latest = log.latest_of((STOPPED, RESUMED))
    return latest is not None and latest.kind == STOPPED


def is_paused(config: Config, log: EventLog, now_utc: datetime) -> bool:
    latest = log.latest_of((PAUSED, RESUMED))
    if latest is None or latest.kind == RESUMED:
        return False
    until = latest.payload.get("until")
    if not until:
        return True
    return local_day(now_utc, config.timezone) <= str(until)


def is_skipped(log: EventLog, local_day_value: str) -> bool:
    return bool(log.events(local_day_value, SKIPPED_DAY))


def last_contact(log: EventLog) -> datetime | None:
    latest = log.last(REPLY_RECEIVED)
    return latest.ts_utc if latest else None


def _quiet(config: Config, now_utc: datetime) -> bool:
    return in_quiet_hours(
        now_utc, config.quiet_hours.start, config.quiet_hours.end, config.timezone
    )


def _hhmm(moment: datetime, config: Config) -> str:
    return to_local(moment, config.timezone).strftime("%H:%M")


def due_actions(
    config: Config,
    log: EventLog,
    catalogue: Catalogue,
    now_utc: datetime,
) -> list[Action]:
    day = local_day(now_utc, config.timezone)
    if is_stopped(log) or is_paused(config, log, now_utc) or is_skipped(log, day):
        return []

    today = to_local(now_utc, config.timezone).date()
    checkin_due = local_time_on(today, config.ladder.checkin_at, config.timezone)
    sent = log.last(CHECKIN_SENT, local_day=day)

    if sent is None:
        if now_utc < checkin_due or _quiet(config, now_utc):
            return []
        return [
            Action(
                kind=CHECKIN_SENT,
                chat_id=config.parent.chat_id,
                text=catalogue.render("checkin", parent_name=config.parent.name),
            )
        ]

    if log.last(REPLY_RECEIVED, local_day=day) is not None:
        return []
    if log.last(ESCALATED_SILENCE, local_day=day) is not None:
        return []

    nudged = log.last(CHECKIN_NUDGED, local_day=day)
    nudge_due = sent.ts_utc + timedelta(minutes=config.ladder.nudge_after_minutes)
    escalate_due = sent.ts_utc + timedelta(minutes=config.ladder.escalate_after_minutes)

    if now_utc >= escalate_due:
        return [
            Action(
                kind=ESCALATED_SILENCE,
                chat_id=config.group_chat_id,
                text=catalogue.render(
                    "escalated_silence",
                    parent_name=config.parent.name,
                    checkin_time=_hhmm(sent.ts_utc, config),
                    nudge_time=_hhmm(nudged.ts_utc, config) if nudged else "—",
                    now_time=_hhmm(now_utc, config),
                    last_contact=_last_contact_label(log, config),
                ),
            )
        ]

    if nudged is None and now_utc >= nudge_due and not _quiet(config, now_utc):
        return [
            Action(
                kind=CHECKIN_NUDGED,
                chat_id=config.parent.chat_id,
                text=catalogue.render("checkin_nudge", parent_name=config.parent.name),
            )
        ]

    return []


def _last_contact_label(log: EventLog, config: Config) -> str:
    moment = last_contact(log)
    if moment is None:
        return "—"
    return to_local(moment, config.timezone).strftime("%Y-%m-%d %H:%M")
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `python3 -m pytest tests/test_ladder.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add care/ladder.py tests/test_ladder.py
git commit -m "Add check-in ladder state machine"
```

---

## Task 8: The dose ladder and the adherence rule

Implements spec §7.2 (dose ladder), §7.3 (unconfirmed is not missed, repeat rule).

**Files:**
- Create: `care/meds.py`
- Test: `tests/test_meds.py`

**Interfaces:**
- Consumes: `care.ladder.Action`, `care.config.Config`/`Dose`, `care.state.EventLog`, `care.messages.Catalogue`.
- Produces: `due_actions(config: Config, log: EventLog, catalogue: Catalogue, now_utc: datetime) -> list[Action]`; `weekday_key(moment: datetime, tz: str) -> str`.

- [ ] **Step 1: Write the failing test**

`tests/test_meds.py`:

```python
from datetime import date, datetime

from care.clock import local_day, local_time_on
from care.config import load_config
from care.meds import due_actions, weekday_key
from care.messages import Catalogue
from care.state import (
    DOSE_CONFIRMED,
    DOSE_NUDGED,
    DOSE_REMINDED,
    DOSE_UNCONFIRMED,
    ESCALATED_ADHERENCE,
    SUPPRESSED_QUIET,
    EventLog,
)

CONFIG = load_config("config/roster.example.yaml", "config/meds.example.yaml")
CATALOGUE = Catalogue.load("config", "uk")
TZ = CONFIG.timezone


def moment(day: date, hhmm: str) -> datetime:
    return local_time_on(day, hhmm, TZ)


def record(log, when, kind, subject=None, **payload):
    log.append(when, local_day(when, TZ), kind, subject=subject, payload=payload or None)


def kinds(actions):
    return [(action.kind, action.subject) for action in actions]


DAY = date(2026, 9, 12)
NEXT = date(2026, 9, 13)


def test_weekday_key():
    assert weekday_key(moment(DAY, "12:00"), TZ) == "sat"


def test_reminder_fires_at_dose_time():
    log = EventLog.in_memory()
    assert kinds(due_actions(CONFIG, log, CATALOGUE, moment(DAY, "08:30"))) == [
        (DOSE_REMINDED, "morning-bp")
    ]


def test_reminder_goes_to_the_parent():
    log = EventLog.in_memory()
    action = due_actions(CONFIG, log, CATALOGUE, moment(DAY, "08:30"))[0]
    assert action.chat_id == CONFIG.parent.chat_id


def test_nudge_after_forty_five_minutes():
    log = EventLog.in_memory()
    record(log, moment(DAY, "08:30"), DOSE_REMINDED, "morning-bp")
    assert due_actions(CONFIG, log, CATALOGUE, moment(DAY, "09:00")) == []
    assert kinds(due_actions(CONFIG, log, CATALOGUE, moment(DAY, "09:15"))) == [
        (DOSE_NUDGED, "morning-bp")
    ]


def test_confirmation_stops_the_dose_ladder():
    log = EventLog.in_memory()
    record(log, moment(DAY, "08:30"), DOSE_REMINDED, "morning-bp")
    record(log, moment(DAY, "08:40"), DOSE_CONFIRMED, "morning-bp")
    assert due_actions(CONFIG, log, CATALOGUE, moment(DAY, "09:15")) == []
    assert due_actions(CONFIG, log, CATALOGUE, moment(DAY, "10:30")) == []


def test_dose_closes_as_unconfirmed_and_sends_nothing():
    log = EventLog.in_memory()
    record(log, moment(DAY, "08:30"), DOSE_REMINDED, "morning-bp")
    record(log, moment(DAY, "09:15"), DOSE_NUDGED, "morning-bp")
    actions = due_actions(CONFIG, log, CATALOGUE, moment(DAY, "10:30"))
    assert kinds(actions) == [(DOSE_UNCONFIRMED, "morning-bp")]
    assert actions[0].text == ""


def test_two_unconfirmed_in_one_day_escalates_once():
    log = EventLog.in_memory()
    record(log, moment(DAY, "10:30"), DOSE_UNCONFIRMED, "morning-bp")
    record(log, moment(DAY, "22:00"), DOSE_UNCONFIRMED, "evening")
    actions = due_actions(CONFIG, log, CATALOGUE, moment(DAY, "22:01"))
    assert kinds(actions) == [(ESCALATED_ADHERENCE, None)]
    assert actions[0].chat_id == CONFIG.group_chat_id

    record(log, moment(DAY, "22:01"), ESCALATED_ADHERENCE)
    assert due_actions(CONFIG, log, CATALOGUE, moment(DAY, "22:30")) == []


def test_same_dose_unconfirmed_two_days_running_escalates():
    log = EventLog.in_memory()
    record(log, moment(DAY, "10:30"), DOSE_UNCONFIRMED, "morning-bp")
    record(log, moment(NEXT, "10:30"), DOSE_UNCONFIRMED, "morning-bp")
    actions = due_actions(CONFIG, log, CATALOGUE, moment(NEXT, "10:31"))
    assert kinds(actions) == [(ESCALATED_ADHERENCE, None)]


def test_single_unconfirmed_dose_does_not_escalate():
    log = EventLog.in_memory()
    record(log, moment(DAY, "10:30"), DOSE_UNCONFIRMED, "morning-bp")
    assert due_actions(CONFIG, log, CATALOGUE, moment(DAY, "10:31")) == []


def test_dose_inside_quiet_hours_is_suppressed(tmp_path):
    meds = tmp_path / "meds.yaml"
    meds.write_text('doses:\n  - id: night\n    label: the night tablet\n    at: "22:00"\n')
    config = load_config("config/roster.example.yaml", meds)
    log = EventLog.in_memory()
    actions = due_actions(config, log, CATALOGUE, moment(DAY, "22:00"))
    assert kinds(actions) == [(SUPPRESSED_QUIET, "night")]
    assert actions[0].text == ""
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `python3 -m pytest tests/test_meds.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'care.meds'`

- [ ] **Step 3: Implement the dose ladder**

`care/meds.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta

from care.clock import in_quiet_hours, local_day, local_time_on, to_local
from care.config import WEEKDAYS, Config, Dose
from care.ladder import Action, is_paused, is_skipped, is_stopped
from care.messages import Catalogue
from care.state import (
    DOSE_CONFIRMED,
    DOSE_NUDGED,
    DOSE_REMINDED,
    DOSE_UNCONFIRMED,
    ESCALATED_ADHERENCE,
    SUPPRESSED_QUIET,
    EventLog,
)


def weekday_key(moment: datetime, tz: str) -> str:
    return WEEKDAYS[to_local(moment, tz).weekday()]


def _quiet(config: Config, moment: datetime) -> bool:
    return in_quiet_hours(
        moment, config.quiet_hours.start, config.quiet_hours.end, config.timezone
    )


def _dose_actions(
    config: Config,
    log: EventLog,
    catalogue: Catalogue,
    now_utc: datetime,
    day: str,
    dose: Dose,
) -> list[Action]:
    today = to_local(now_utc, config.timezone).date()
    dose_due = local_time_on(today, dose.at, config.timezone)

    if log.last(DOSE_CONFIRMED, dose.id, day) is not None:
        return []
    if log.last(DOSE_UNCONFIRMED, dose.id, day) is not None:
        return []
    if log.last(SUPPRESSED_QUIET, dose.id, day) is not None:
        return []

    reminded = log.last(DOSE_REMINDED, dose.id, day)

    if reminded is None:
        if now_utc < dose_due:
            return []
        if _quiet(config, now_utc):
            return [Action(kind=SUPPRESSED_QUIET, chat_id="", text="", subject=dose.id)]
        return [
            Action(
                kind=DOSE_REMINDED,
                chat_id=config.parent.chat_id,
                text=catalogue.render("dose_reminder", dose_label=dose.label),
                subject=dose.id,
            )
        ]

    close_due = reminded.ts_utc + timedelta(minutes=config.dose_ladder.close_after_minutes)
    if now_utc >= close_due:
        return [Action(kind=DOSE_UNCONFIRMED, chat_id="", text="", subject=dose.id)]

    nudge_due = reminded.ts_utc + timedelta(minutes=config.dose_ladder.nudge_after_minutes)
    if log.last(DOSE_NUDGED, dose.id, day) is None and now_utc >= nudge_due:
        if _quiet(config, now_utc):
            return []
        return [
            Action(
                kind=DOSE_NUDGED,
                chat_id=config.parent.chat_id,
                text=catalogue.render("dose_nudge", dose_label=dose.label),
                subject=dose.id,
            )
        ]

    return []


def _adherence_reason(config: Config, log: EventLog, now_utc: datetime) -> tuple[str, str] | None:
    day = local_day(now_utc, config.timezone)
    today_unconfirmed = log.events(day, DOSE_UNCONFIRMED)
    if len(today_unconfirmed) >= 2:
        labels = []
        for event in today_unconfirmed:
            dose = config.dose(str(event.subject))
            labels.append(dose.label if dose else str(event.subject))
        return (", ".join(labels), "two doses in one day")

    yesterday = local_day(now_utc - timedelta(days=1), config.timezone)
    previous = {event.subject for event in log.events(yesterday, DOSE_UNCONFIRMED)}
    for event in today_unconfirmed:
        if event.subject in previous:
            dose = config.dose(str(event.subject))
            label = dose.label if dose else str(event.subject)
            return (label, "two days running")
    return None


def due_actions(
    config: Config,
    log: EventLog,
    catalogue: Catalogue,
    now_utc: datetime,
) -> list[Action]:
    day = local_day(now_utc, config.timezone)
    if is_stopped(log) or is_paused(config, log, now_utc) or is_skipped(log, day):
        return []

    actions: list[Action] = []
    for dose in config.doses_on(weekday_key(now_utc, config.timezone)):
        actions.extend(_dose_actions(config, log, catalogue, now_utc, day, dose))

    if _recent_adherence_escalation(config, log, now_utc):
        return actions

    breach = _adherence_reason(config, log, now_utc)
    if breach is not None:
        label, reason = breach
        actions.append(
            Action(
                kind=ESCALATED_ADHERENCE,
                chat_id=config.group_chat_id,
                text=catalogue.render(
                    "escalated_adherence",
                    parent_name=config.parent.name,
                    dose_label=label,
                    reason=reason,
                ),
            )
        )
    return actions


def _recent_adherence_escalation(config: Config, log: EventLog, now_utc: datetime) -> bool:
    latest = log.last(ESCALATED_ADHERENCE)
    return latest is not None and now_utc - latest.ts_utc < timedelta(hours=24)
```

**Adherence lags the closing tick by one tick, deliberately.** `_adherence_reason` reads the event log, and the `DOSE_UNCONFIRMED` event for the dose closing on *this* tick has not been appended yet — the CLI appends it after `due_actions` returns. So the dose that completes the pattern closes on one tick and the group is told on the next, at most five minutes later. The tests in Step 1 assert exactly that.

This is left as-is rather than fixed by folding pending actions into the check. Five minutes on an adherence notice is meaningless — it is the "worth asking when convenient" path, not an emergency — and making `due_actions` reason about events it is about to create would put a second, subtler source of truth next to the log. The one place where a tick of delay would matter is the tripwire, and that path does not go through here at all.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `python3 -m pytest tests/test_meds.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add care/meds.py tests/test_meds.py
git commit -m "Add dose ladder and adherence repeat rule"
```

---

## Task 9: Reply handling — verdict, clarify budget, stand-down

Implements spec §6.2 (reply contract), §6.3 (non-suppression), §5.4 (stand-down), §7.2 (affirmative fast path ordering).

**Files:**
- Create: `care/conversation.py`
- Test: `tests/test_conversation.py`

**Interfaces:**
- Consumes: `care.triage`, `care.ladder.Action`, `care.state`, `care.messages.Catalogue`, `care.config.Config`.
- Produces: `Directive(verdict: str, do: str, matched: tuple[str, ...], question_budget: int, actions: tuple[Action, ...])`; `handle_reply(config, log, catalogue, tripwire, affirmatives, text, now_utc, judgement="clear") -> Directive`; `acknowledge(config, log, chat_id, now_utc) -> bool`. `do` is one of `"acknowledge"`, `"clarify"`, `"acknowledge_escalation"`.

Note on the clarify budget: it is enforced from the event log, never from a flag the agent has to remember. A clarify is *open* when the most recent `CLARIFY_ASKED` today has no later `REPLY_RECEIVED` carrying `judgement == "clear"`. A second `unclear` while one is open escalates.

- [ ] **Step 1: Write the failing test**

`tests/test_conversation.py`:

```python
from datetime import date, datetime

from care.clock import local_day, local_time_on
from care.config import load_config
from care.conversation import acknowledge, handle_reply
from care.messages import Catalogue
from care.state import (
    CLARIFY_ASKED,
    ESCALATED_CONCERN,
    ESCALATED_SILENCE,
    ESCALATION_ACKNOWLEDGED,
    REPLY_RECEIVED,
    STOOD_DOWN,
    EventLog,
)
from care.triage import load_terms

CONFIG = load_config("config/roster.example.yaml", "config/meds.example.yaml")
CATALOGUE = Catalogue.load("config", "uk")
TRIPWIRE = load_terms("config", "tripwire", ("uk", "en"))
AFFIRMATIVES = load_terms("config", "affirmatives", ("uk", "en"))
TZ = CONFIG.timezone
DAY = date(2026, 9, 12)


def moment(hhmm: str) -> datetime:
    return local_time_on(DAY, hhmm, TZ)


def reply(log, hhmm, text, judgement="clear"):
    return handle_reply(
        CONFIG, log, CATALOGUE, TRIPWIRE, AFFIRMATIVES, text, moment(hhmm), judgement
    )


def test_ordinary_reply_is_acknowledged():
    log = EventLog.in_memory()
    directive = reply(log, "09:30", "все добре, дякую")
    assert directive.verdict == "clear"
    assert directive.do == "acknowledge"
    assert directive.actions == ()
    assert log.has("2026-09-12", REPLY_RECEIVED)


def test_tripwire_escalates_to_the_group_without_the_agent():
    log = EventLog.in_memory()
    directive = reply(log, "09:30", "я впала і не можу встати")
    assert directive.verdict == "tripwire"
    assert directive.do == "acknowledge_escalation"
    assert [action.kind for action in directive.actions] == [ESCALATED_CONCERN]
    assert directive.actions[0].chat_id == CONFIG.group_chat_id


def test_escalation_quotes_the_parent_verbatim():
    log = EventLog.in_memory()
    directive = reply(log, "09:30", "я впала і не можу встати")
    assert "я впала і не можу встати" in directive.actions[0].text


def test_tripwire_overrides_a_clear_judgement_from_the_agent():
    log = EventLog.in_memory()
    directive = reply(log, "09:30", "все добре, але болить у грудях", judgement="clear")
    assert directive.verdict == "tripwire"


def test_tripwire_beats_the_affirmative_fast_path():
    log = EventLog.in_memory()
    directive = reply(log, "08:40", "так, випила, але дуже погано")
    assert directive.verdict == "tripwire"
    assert directive.do == "acknowledge_escalation"


def test_unclear_buys_exactly_one_question():
    log = EventLog.in_memory()
    first = reply(log, "09:30", "нормально", judgement="unclear")
    assert first.do == "clarify"
    assert first.question_budget == 1
    assert log.has("2026-09-12", CLARIFY_ASKED)


def test_second_unclear_escalates_instead_of_asking_again():
    log = EventLog.in_memory()
    reply(log, "09:30", "нормально", judgement="unclear")
    second = reply(log, "09:40", "та нічого", judgement="unclear")
    assert second.do == "acknowledge_escalation"
    assert [action.kind for action in second.actions] == [ESCALATED_CONCERN]


def test_clear_answer_closes_the_clarify_episode():
    log = EventLog.in_memory()
    reply(log, "09:30", "нормально", judgement="unclear")
    reply(log, "09:40", "все добре, просто втомилася")
    third = reply(log, "18:00", "хм", judgement="unclear")
    assert third.do == "clarify"


def test_reply_after_silence_escalation_stands_down():
    log = EventLog.in_memory()
    when = moment("15:00")
    log.append(when, local_day(when, TZ), ESCALATED_SILENCE)
    directive = reply(log, "16:00", "вибач, була у саду")
    assert [action.kind for action in directive.actions] == [STOOD_DOWN]
    assert directive.actions[0].chat_id == CONFIG.group_chat_id


def test_stand_down_happens_only_once():
    log = EventLog.in_memory()
    when = moment("15:00")
    log.append(when, local_day(when, TZ), ESCALATED_SILENCE)
    reply(log, "16:00", "вибач, була у саду")
    log.append(moment("16:00"), local_day(when, TZ), STOOD_DOWN)
    assert reply(log, "16:30", "все добре").actions == ()


def test_family_acknowledgement_is_recorded():
    log = EventLog.in_memory()
    when = moment("15:00")
    log.append(when, local_day(when, TZ), ESCALATED_SILENCE)
    assert acknowledge(CONFIG, log, "22222222", moment("15:10"))
    assert log.has("2026-09-12", ESCALATION_ACKNOWLEDGED)


def test_acknowledgement_from_a_stranger_is_refused():
    log = EventLog.in_memory()
    when = moment("15:00")
    log.append(when, local_day(when, TZ), ESCALATED_SILENCE)
    assert not acknowledge(CONFIG, log, "999999999", moment("15:10"))
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `python3 -m pytest tests/test_conversation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'care.conversation'`

- [ ] **Step 3: Implement reply handling**

`care/conversation.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from care.clock import local_day, to_local
from care.config import Config
from care.ladder import Action
from care.messages import Catalogue
from care.state import (
    CLARIFY_ASKED,
    ESCALATED_CONCERN,
    ESCALATED_SILENCE,
    ESCALATION_ACKNOWLEDGED,
    REPLY_RECEIVED,
    STOOD_DOWN,
    EventLog,
)
from care.triage import Term, triage


@dataclass(frozen=True)
class Directive:
    verdict: str
    do: str
    matched: tuple[str, ...] = ()
    question_budget: int = 0
    actions: tuple[Action, ...] = field(default_factory=tuple)


def _clarify_is_open(log: EventLog, day: str) -> bool:
    asked = log.last(CLARIFY_ASKED, local_day=day)
    if asked is None:
        return False
    for event in log.events(day, REPLY_RECEIVED):
        if event.ts_utc > asked.ts_utc and event.payload.get("judgement") == "clear":
            return False
    return True


def _needs_stand_down(log: EventLog, day: str) -> bool:
    escalated = log.last(ESCALATED_SILENCE, local_day=day)
    if escalated is None:
        return False
    stood_down = log.last(STOOD_DOWN, local_day=day)
    return stood_down is None or stood_down.ts_utc < escalated.ts_utc


def _concern_action(
    config: Config,
    catalogue: Catalogue,
    quote: str,
    now_utc: datetime,
) -> Action:
    return Action(
        kind=ESCALATED_CONCERN,
        chat_id=config.group_chat_id,
        text=catalogue.render(
            "escalated_concern",
            parent_name=config.parent.name,
            quote=quote,
            when=to_local(now_utc, config.timezone).strftime("%H:%M"),
        ),
    )


def handle_reply(
    config: Config,
    log: EventLog,
    catalogue: Catalogue,
    tripwire: tuple[Term, ...],
    affirmatives: tuple[Term, ...],
    text: str,
    now_utc: datetime,
    judgement: str = "clear",
) -> Directive:
    day = local_day(now_utc, config.timezone)
    verdict = triage(text, tripwire)
    effective = "tripwire" if verdict.verdict == "tripwire" else judgement

    log.append(
        now_utc,
        day,
        REPLY_RECEIVED,
        payload={"text": text, "judgement": effective, "matched": list(verdict.matched)},
    )

    actions: list[Action] = []
    if _needs_stand_down(log, day):
        actions.append(
            Action(
                kind=STOOD_DOWN,
                chat_id=config.group_chat_id,
                text=catalogue.render(
                    "stood_down",
                    parent_name=config.parent.name,
                    quote=text,
                    when=to_local(now_utc, config.timezone).strftime("%H:%M"),
                ),
            )
        )

    if effective == "tripwire":
        actions.append(_concern_action(config, catalogue, text, now_utc))
        return Directive(
            verdict="tripwire",
            do="acknowledge_escalation",
            matched=verdict.matched,
            actions=tuple(actions),
        )

    if effective == "unclear":
        if _clarify_is_open(log, day):
            actions.append(_concern_action(config, catalogue, text, now_utc))
            return Directive(
                verdict="unclear",
                do="acknowledge_escalation",
                actions=tuple(actions),
            )
        log.append(now_utc, day, CLARIFY_ASKED, payload={"text": text})
        return Directive(
            verdict="unclear",
            do="clarify",
            question_budget=1,
            actions=tuple(actions),
        )

    return Directive(verdict="clear", do="acknowledge", actions=tuple(actions))


def acknowledge(
    config: Config,
    log: EventLog,
    chat_id: str,
    now_utc: datetime,
) -> bool:
    if not config.is_family(chat_id):
        return False
    day = local_day(now_utc, config.timezone)
    log.append(now_utc, day, ESCALATION_ACKNOWLEDGED, payload={"chat_id": chat_id})
    return True
```

Note: `_needs_stand_down` is called after the `REPLY_RECEIVED` append, which is harmless — it inspects `ESCALATED_SILENCE` and `STOOD_DOWN` only.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `python3 -m pytest tests/test_conversation.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add care/conversation.py tests/test_conversation.py
git commit -m "Add reply handling with clarify budget and stand-down"
```

---

## Task 10: Authorisation and the control surface

Implements spec §8.1 (who may change what), §8.2 (pause, skip, stop), §6.3 (stop is honoured but announced).

**Files:**
- Create: `care/control.py`
- Test: `tests/test_control.py`

**Interfaces:**
- Consumes: `care.config.Config`, `care.state.EventLog`, `care.messages.Catalogue`, `care.ladder.Action`.
- Produces: `Outcome(ok: bool, confirmation: str, actions: tuple[Action, ...])`; `capabilities(config, chat_id) -> frozenset[str]`; `pause(config, log, catalogue, actor, now_utc, until=None) -> Outcome`; `resume(...) -> Outcome`; `skip_today(...) -> Outcome`; `stop(...) -> Outcome`; `change_schedule(config, log, catalogue, actor, now_utc, detail) -> Outcome`.

- [ ] **Step 1: Write the failing test**

`tests/test_control.py`:

```python
from datetime import date, datetime

from care.clock import local_time_on
from care.config import load_config
from care.control import (
    capabilities,
    change_schedule,
    pause,
    resume,
    skip_today,
    stop,
)
from care.ladder import due_actions, is_paused, is_stopped
from care.messages import Catalogue
from care.state import (
    PAUSED,
    SKIPPED_DAY,
    STOPPED,
    UNAUTHORISED_ATTEMPT,
    EventLog,
)

CONFIG = load_config("config/roster.example.yaml", "config/meds.example.yaml")
CATALOGUE = Catalogue.load("config", "uk")
TZ = CONFIG.timezone
DAY = date(2026, 9, 12)
FAMILY = "22222222"
PARENT = CONFIG.parent.chat_id
STRANGER = "999999999"


def moment(hhmm: str) -> datetime:
    return local_time_on(DAY, hhmm, TZ)


def test_family_may_change_the_schedule():
    assert "schedule" in capabilities(CONFIG, FAMILY)


def test_parent_may_not_change_the_schedule():
    assert "schedule" not in capabilities(CONFIG, PARENT)


def test_parent_may_stop_and_skip():
    assert {"stop", "skip"} <= capabilities(CONFIG, PARENT)


def test_stranger_has_no_capabilities():
    assert capabilities(CONFIG, STRANGER) == frozenset()


def test_stop_by_parent_is_honoured_and_announced():
    log = EventLog.in_memory()
    outcome = stop(CONFIG, log, CATALOGUE, PARENT, moment("10:00"))
    assert outcome.ok
    assert is_stopped(log)
    assert [action.chat_id for action in outcome.actions] == [CONFIG.group_chat_id]
    assert log.has("2026-09-12", STOPPED)


def test_stopped_agent_sends_nothing_on_the_next_tick():
    log = EventLog.in_memory()
    stop(CONFIG, log, CATALOGUE, PARENT, moment("08:00"))
    assert due_actions(CONFIG, log, CATALOGUE, moment("09:00")) == []


def test_family_can_resume_after_a_stop():
    log = EventLog.in_memory()
    stop(CONFIG, log, CATALOGUE, PARENT, moment("08:00"))
    assert resume(CONFIG, log, CATALOGUE, FAMILY, moment("08:30")).ok
    assert not is_stopped(log)
    assert due_actions(CONFIG, log, CATALOGUE, moment("09:00")) != []


def test_parent_may_not_resume():
    log = EventLog.in_memory()
    stop(CONFIG, log, CATALOGUE, PARENT, moment("08:00"))
    assert not resume(CONFIG, log, CATALOGUE, PARENT, moment("08:30")).ok
    assert is_stopped(log)


def test_pause_until_a_date():
    log = EventLog.in_memory()
    outcome = pause(CONFIG, log, CATALOGUE, FAMILY, moment("08:00"), until="2026-09-20")
    assert outcome.ok
    assert log.has("2026-09-12", PAUSED)
    assert is_paused(CONFIG, log, moment("09:00"))
    assert not is_paused(CONFIG, log, local_time_on(date(2026, 9, 21), "09:00", TZ))


def test_skip_today_suppresses_the_ladder_and_tells_the_group():
    log = EventLog.in_memory()
    outcome = skip_today(CONFIG, log, CATALOGUE, PARENT, moment("08:00"))
    assert outcome.ok
    assert log.has("2026-09-12", SKIPPED_DAY)
    assert outcome.actions[0].chat_id == CONFIG.group_chat_id
    assert due_actions(CONFIG, log, CATALOGUE, moment("09:00")) == []


def test_schedule_change_is_echoed_to_the_group():
    log = EventLog.in_memory()
    outcome = change_schedule(
        CONFIG, log, CATALOGUE, FAMILY, moment("12:00"), detail="evening tablet 20:00 to 20:30"
    )
    assert outcome.ok
    assert outcome.actions[0].chat_id == CONFIG.group_chat_id
    assert "20:30" in outcome.actions[0].text


def test_unauthorised_change_is_refused_and_recorded():
    log = EventLog.in_memory()
    outcome = change_schedule(
        CONFIG, log, CATALOGUE, STRANGER, moment("12:00"), detail="checkin 06:00"
    )
    assert not outcome.ok
    assert outcome.actions == ()
    assert log.has("2026-09-12", UNAUTHORISED_ATTEMPT)
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `python3 -m pytest tests/test_control.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'care.control'`

- [ ] **Step 3: Implement the control surface**

`care/control.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from care.clock import local_day
from care.config import Config
from care.ladder import Action
from care.messages import Catalogue
from care.state import (
    PAUSED,
    RESUMED,
    SCHEDULE_CHANGED,
    SKIPPED_DAY,
    STOPPED,
    UNAUTHORISED_ATTEMPT,
    EventLog,
)

FAMILY_CAPABILITIES = frozenset({"schedule", "pause", "resume", "stop", "skip", "snooze", "status"})
PARENT_CAPABILITIES = frozenset({"snooze", "skip", "stop"})


@dataclass(frozen=True)
class Outcome:
    ok: bool
    confirmation: str = ""
    actions: tuple[Action, ...] = field(default_factory=tuple)


def capabilities(config: Config, chat_id: str) -> frozenset[str]:
    if config.is_family(chat_id):
        return FAMILY_CAPABILITIES
    if chat_id == config.parent.chat_id:
        return PARENT_CAPABILITIES
    return frozenset()


def _actor_name(config: Config, chat_id: str) -> str:
    if chat_id == config.parent.chat_id:
        return config.parent.name
    for person in config.family:
        if person.chat_id == chat_id:
            return person.name
    return chat_id


def _refuse(
    config: Config,
    log: EventLog,
    actor: str,
    now_utc: datetime,
    capability: str,
) -> Outcome:
    log.append(
        now_utc,
        local_day(now_utc, config.timezone),
        UNAUTHORISED_ATTEMPT,
        payload={"chat_id": actor, "capability": capability},
    )
    return Outcome(ok=False, confirmation=f"not permitted: {capability}")


def _announce(config: Config, text: str) -> tuple[Action, ...]:
    return (Action(kind=SCHEDULE_CHANGED, chat_id=config.group_chat_id, text=text),)


def pause(
    config: Config,
    log: EventLog,
    catalogue: Catalogue,
    actor: str,
    now_utc: datetime,
    until: str | None = None,
) -> Outcome:
    if "pause" not in capabilities(config, actor):
        return _refuse(config, log, actor, now_utc, "pause")
    log.append(
        now_utc, local_day(now_utc, config.timezone), PAUSED, payload={"until": until, "by": actor}
    )
    text = catalogue.render("paused_notice", until=until or "further notice")
    return Outcome(ok=True, confirmation=text, actions=_announce(config, text))


def resume(
    config: Config,
    log: EventLog,
    catalogue: Catalogue,
    actor: str,
    now_utc: datetime,
) -> Outcome:
    if "resume" not in capabilities(config, actor):
        return _refuse(config, log, actor, now_utc, "resume")
    log.append(now_utc, local_day(now_utc, config.timezone), RESUMED, payload={"by": actor})
    text = catalogue.render("resumed_notice")
    return Outcome(ok=True, confirmation=text, actions=_announce(config, text))


def skip_today(
    config: Config,
    log: EventLog,
    catalogue: Catalogue,
    actor: str,
    now_utc: datetime,
) -> Outcome:
    if "skip" not in capabilities(config, actor):
        return _refuse(config, log, actor, now_utc, "skip")
    log.append(now_utc, local_day(now_utc, config.timezone), SKIPPED_DAY, payload={"by": actor})
    text = catalogue.render("skipped_notice", actor_name=_actor_name(config, actor))
    return Outcome(ok=True, confirmation=text, actions=_announce(config, text))


def stop(
    config: Config,
    log: EventLog,
    catalogue: Catalogue,
    actor: str,
    now_utc: datetime,
) -> Outcome:
    if "stop" not in capabilities(config, actor):
        return _refuse(config, log, actor, now_utc, "stop")
    log.append(now_utc, local_day(now_utc, config.timezone), STOPPED, payload={"by": actor})
    text = catalogue.render("stopped_notice", parent_name=config.parent.name)
    return Outcome(ok=True, confirmation=text, actions=_announce(config, text))


def change_schedule(
    config: Config,
    log: EventLog,
    catalogue: Catalogue,
    actor: str,
    now_utc: datetime,
    detail: str,
) -> Outcome:
    if "schedule" not in capabilities(config, actor):
        return _refuse(config, log, actor, now_utc, "schedule")
    log.append(
        now_utc,
        local_day(now_utc, config.timezone),
        SCHEDULE_CHANGED,
        payload={"detail": detail, "by": actor},
    )
    text = catalogue.render(
        "schedule_changed", detail=detail, actor_name=_actor_name(config, actor)
    )
    return Outcome(ok=True, confirmation=text, actions=_announce(config, text))
```

Note: `change_schedule` records and announces the change; editing `meds.yaml` itself is the agent's job via the `family-care` skill (Task 13), because the agent is what parses "move the evening one to 8" into a concrete edit. The audit trail and the group announcement are deterministic; the parse is not.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `python3 -m pytest tests/test_control.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add care/control.py tests/test_control.py
git commit -m "Add authorisation and pause, skip, stop, resume controls"
```

---

## Task 11: The CLI, the tick script, and the cron job

Implements spec §5.2 (tick execution), §6.2 (reply contract as JSON), §10 (send failure recorded and retried), §11 (cron).

**Files:**
- Create: `care/cli.py`
- Create: `scripts/care-tick.sh`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: every module above.
- Produces: `main(argv: list[str] | None = None) -> int`; commands `tick`, `reply`, `confirm`, `triage`, `acknowledge`, `status`, `log`, `pause`, `resume`, `skip`, `stop`, `schedule`, `doctor`. Global options `--roster`, `--meds`, `--config-dir`, `--profile`, `--now`, `--dry-run`.

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:

```python
import json

from care.cli import main


def run(capsys, *args) -> str:
    assert main(list(args)) == 0
    return capsys.readouterr().out


def base(tmp_path) -> list[str]:
    return [
        "--roster",
        "config/roster.example.yaml",
        "--meds",
        "config/meds.example.yaml",
        "--config-dir",
        "config",
        "--state-db",
        str(tmp_path / "state.db"),
    ]


def test_tick_prints_nothing_when_nothing_is_due(tmp_path, capsys):
    out = run(capsys, *base(tmp_path), "--now", "2026-09-12T05:00:00+00:00", "tick")
    assert out == ""


def test_tick_sends_the_checkin_and_records_it(tmp_path, capsys):
    run(capsys, *base(tmp_path), "--now", "2026-09-12T06:00:00+00:00", "tick")
    out = run(capsys, *base(tmp_path), "--now", "2026-09-12T06:05:00+00:00", "log", "--day", "2026-09-12")
    assert "checkin_sent" in out


def test_tick_is_idempotent(tmp_path, capsys):
    run(capsys, *base(tmp_path), "--now", "2026-09-12T06:00:00+00:00", "tick")
    run(capsys, *base(tmp_path), "--now", "2026-09-12T06:01:00+00:00", "tick")
    out = run(capsys, *base(tmp_path), "--now", "2026-09-12T06:05:00+00:00", "log", "--day", "2026-09-12")
    assert out.count("checkin_sent") == 1


def test_reply_emits_the_directive_as_json(tmp_path, capsys):
    out = run(
        capsys,
        *base(tmp_path),
        "--now",
        "2026-09-12T06:30:00+00:00",
        "reply",
        "--text",
        "все добре",
    )
    directive = json.loads(out)
    assert directive["verdict"] == "clear"
    assert directive["do"] == "acknowledge"


def test_reply_with_tripwire_reports_escalation(tmp_path, capsys):
    out = run(
        capsys,
        *base(tmp_path),
        "--now",
        "2026-09-12T06:30:00+00:00",
        "reply",
        "--text",
        "я впала",
    )
    directive = json.loads(out)
    assert directive["verdict"] == "tripwire"
    assert directive["do"] == "acknowledge_escalation"
    assert directive["escalated"] is True


def test_triage_reports_the_matched_term(tmp_path, capsys):
    out = run(capsys, *base(tmp_path), "triage", "--text", "впала в ванній")
    result = json.loads(out)
    assert result["verdict"] == "tripwire"
    assert result["matched"] == ["впал"]


def test_confirm_records_a_dose(tmp_path, capsys):
    run(capsys, *base(tmp_path), "--now", "2026-09-12T05:30:00+00:00", "tick")
    run(
        capsys,
        *base(tmp_path),
        "--now",
        "2026-09-12T05:40:00+00:00",
        "confirm",
        "--dose",
        "morning-bp",
    )
    out = run(capsys, *base(tmp_path), "--now", "2026-09-12T06:00:00+00:00", "log", "--day", "2026-09-12")
    assert "dose_confirmed" in out


def test_stop_by_stranger_exits_non_zero(tmp_path):
    argv = base(tmp_path) + ["--now", "2026-09-12T06:00:00+00:00", "stop", "--actor", "999999999"]
    assert main(argv) == 1


def test_status_reports_the_day(tmp_path, capsys):
    run(capsys, *base(tmp_path), "--now", "2026-09-12T06:00:00+00:00", "tick")
    out = run(capsys, *base(tmp_path), "--now", "2026-09-12T06:05:00+00:00", "status")
    assert "2026-09-12" in out
    assert "checkin_sent" in out


def test_doctor_reports_config_and_delivery_mode(tmp_path, capsys):
    out = run(capsys, *base(tmp_path), "doctor")
    assert "dry-run" in out
    assert "Europe/Kyiv" in out
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `python3 -m pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'care.cli'`

- [ ] **Step 3: Implement the CLI**

`care/cli.py`:

```python
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from care import control, ladder, meds
from care.clock import local_day, to_local
from care.config import ConfigError, load_config
from care.conversation import acknowledge, handle_reply
from care.delivery import build
from care.messages import Catalogue, MessageError
from care.state import DOSE_CONFIRMED, SEND_FAILED, EventLog
from care.triage import load_terms, triage

QUIET_KINDS = ("dose_unconfirmed", "suppressed_quiet")


@dataclass
class Context:
    config: object
    log: EventLog
    catalogue: Catalogue
    delivery: object
    tripwire: tuple
    affirmatives: tuple
    now_utc: datetime


def _now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _context(args: argparse.Namespace) -> Context:
    config = load_config(args.roster, args.meds)
    mode = "dry-run" if args.dry_run else config.delivery_mode
    return Context(
        config=config,
        log=EventLog.open(args.state_db or config.state_db),
        catalogue=Catalogue.load(args.config_dir, config.message_language),
        delivery=build(mode, args.profile),
        tripwire=load_terms(args.config_dir, "tripwire", config.languages),
        affirmatives=load_terms(args.config_dir, "affirmatives", config.languages),
        now_utc=_now(args.now),
    )


def _execute(context: Context, actions: list) -> None:
    day = local_day(context.now_utc, context.config.timezone)
    for action in actions:
        if not action.chat_id:
            context.log.append(context.now_utc, day, action.kind, subject=action.subject)
            continue
        if context.delivery.send(action.chat_id, action.text):
            context.log.append(
                context.now_utc,
                day,
                action.kind,
                subject=action.subject,
                payload={"chat_id": action.chat_id},
            )
        else:
            context.log.append(
                context.now_utc,
                day,
                SEND_FAILED,
                subject=action.subject,
                payload={"chat_id": action.chat_id, "kind": action.kind},
            )


def _cmd_tick(context: Context, args: argparse.Namespace) -> int:
    actions = ladder.due_actions(
        context.config, context.log, context.catalogue, context.now_utc
    )
    actions += meds.due_actions(
        context.config, context.log, context.catalogue, context.now_utc
    )
    _execute(context, actions)
    return 0


def _cmd_reply(context: Context, args: argparse.Namespace) -> int:
    directive = handle_reply(
        context.config,
        context.log,
        context.catalogue,
        context.tripwire,
        context.affirmatives,
        args.text,
        context.now_utc,
        args.judgement,
    )
    _execute(context, list(directive.actions))
    print(
        json.dumps(
            {
                "verdict": directive.verdict,
                "do": directive.do,
                "matched": list(directive.matched),
                "question_budget": directive.question_budget,
                "escalated": any(
                    action.kind.startswith("escalated") for action in directive.actions
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_confirm(context: Context, args: argparse.Namespace) -> int:
    context.log.append(
        context.now_utc,
        local_day(context.now_utc, context.config.timezone),
        DOSE_CONFIRMED,
        subject=args.dose,
    )
    return 0


def _cmd_triage(context: Context, args: argparse.Namespace) -> int:
    verdict = triage(args.text, context.tripwire)
    print(
        json.dumps(
            {
                "verdict": verdict.verdict,
                "matched": list(verdict.matched),
                "categories": list(verdict.categories),
            },
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_acknowledge(context: Context, args: argparse.Namespace) -> int:
    return 0 if acknowledge(context.config, context.log, args.actor, context.now_utc) else 1


def _cmd_log(context: Context, args: argparse.Namespace) -> int:
    day = args.day or local_day(context.now_utc, context.config.timezone)
    for event in context.log.events(day):
        stamp = to_local(event.ts_utc, context.config.timezone).strftime("%H:%M")
        subject = f" {event.subject}" if event.subject else ""
        print(f"{stamp} {event.kind}{subject}")
    return 0


def _cmd_status(context: Context, args: argparse.Namespace) -> int:
    day = local_day(context.now_utc, context.config.timezone)
    print(f"day: {day}")
    print(f"stopped: {ladder.is_stopped(context.log)}")
    print(f"paused: {ladder.is_paused(context.config, context.log, context.now_utc)}")
    for event in context.log.events(day):
        stamp = to_local(event.ts_utc, context.config.timezone).strftime("%H:%M")
        subject = f" {event.subject}" if event.subject else ""
        print(f"  {stamp} {event.kind}{subject}")
    return 0


def _cmd_doctor(context: Context, args: argparse.Namespace) -> int:
    config = context.config
    print(f"timezone: {config.timezone}")
    print(f"delivery: {config.delivery_mode}")
    print(f"message language: {config.message_language}")
    print(f"parent: {config.parent.name} ({config.parent.chat_id})")
    print(f"group: {config.group_chat_id}")
    print(f"family: {len(config.family)}")
    print(f"doses: {len(config.doses)}")
    print(f"tripwire terms: {len(context.tripwire)}")
    failures = context.log.events(kind=SEND_FAILED)
    print(f"undelivered sends: {len(failures)}")
    return 0


def _control(handler):
    def run(context: Context, args: argparse.Namespace) -> int:
        kwargs = {}
        if hasattr(args, "until") and args.until:
            kwargs["until"] = args.until
        if hasattr(args, "detail"):
            kwargs["detail"] = args.detail
        outcome = handler(
            context.config,
            context.log,
            context.catalogue,
            args.actor,
            context.now_utc,
            **kwargs,
        )
        if not outcome.ok:
            print(outcome.confirmation, file=sys.stderr)
            return 1
        _execute(context, list(outcome.actions))
        print(outcome.confirmation)
        return 0

    return run


COMMANDS = {
    "tick": _cmd_tick,
    "reply": _cmd_reply,
    "confirm": _cmd_confirm,
    "triage": _cmd_triage,
    "acknowledge": _cmd_acknowledge,
    "log": _cmd_log,
    "status": _cmd_status,
    "doctor": _cmd_doctor,
    "pause": _control(control.pause),
    "resume": _control(control.resume),
    "skip": _control(control.skip_today),
    "stop": _control(control.stop),
    "schedule": _control(control.change_schedule),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="care")
    parser.add_argument("--roster", default="config/roster.yaml")
    parser.add_argument("--meds", default="config/meds.yaml")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--profile", default="telegram-family-assistant")
    parser.add_argument("--state-db", default=None)
    parser.add_argument("--now", default=None)
    parser.add_argument("--dry-run", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("tick")

    reply = subparsers.add_parser("reply")
    reply.add_argument("--text", required=True)
    reply.add_argument("--judgement", default="clear", choices=["clear", "unclear"])

    confirm = subparsers.add_parser("confirm")
    confirm.add_argument("--dose", required=True)

    triage_parser = subparsers.add_parser("triage")
    triage_parser.add_argument("--text", required=True)

    ack = subparsers.add_parser("acknowledge")
    ack.add_argument("--actor", required=True)

    log_parser = subparsers.add_parser("log")
    log_parser.add_argument("--day", default=None)

    subparsers.add_parser("status")
    subparsers.add_parser("doctor")

    pause_parser = subparsers.add_parser("pause")
    pause_parser.add_argument("--actor", required=True)
    pause_parser.add_argument("--until", default=None)

    for name in ("resume", "skip", "stop"):
        control_parser = subparsers.add_parser(name)
        control_parser.add_argument("--actor", required=True)

    schedule = subparsers.add_parser("schedule")
    schedule.add_argument("--actor", required=True)
    schedule.add_argument("--detail", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        context = _context(args)
    except (ConfigError, MessageError) as problem:
        print(str(problem), file=sys.stderr)
        return 2
    return COMMANDS[args.command](context, args)
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `python3 -m pytest tests/test_cli.py -v`
Expected: 10 passed

- [ ] **Step 5: Write the cron script**

`scripts/care-tick.sh`:

```sh
#!/bin/sh
set -eu
REPO="${CARE_REPO:-/root/tg-family-assitsant}"
cd "$REPO"
exec python3 -m care \
  --roster config/roster.yaml \
  --meds config/meds.yaml \
  --config-dir config \
  tick
```

Then: `chmod +x scripts/care-tick.sh`

`care tick` prints nothing when nothing is due, so with `--no-agent` an uneventful tick delivers nothing. Install the job with:

```bash
telegram-family-assistant cron create --name family-care-tick "*/5 * * * *" \
  --script care-tick.sh --no-agent
```

Hermes runs `--script` from `~/.hermes/scripts/`, so symlink it there:

```bash
mkdir -p ~/.hermes/scripts
ln -sf /root/tg-family-assitsant/scripts/care-tick.sh ~/.hermes/scripts/care-tick.sh
```

- [ ] **Step 6: Verify the whole suite passes**

Run: `python3 -m pytest -v`
Expected: all tests from Tasks 1–11 pass

- [ ] **Step 7: Commit**

```bash
git add care/cli.py scripts/care-tick.sh tests/test_cli.py
git commit -m "Add CLI commands and cron tick script"
```

---

## Task 12: End-to-end scenario suite

Implements spec §13 (every listed scenario). This is the gate that proves the ladder behaves as a whole, not just per-module.

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/test_scenarios.py`

**Interfaces:**
- Consumes: everything.
- Produces: pytest fixture `world` — a `World` with `.config`, `.log`, `.catalogue`, `.delivery` (a `Capture`), `.tick(hhmm, day=None)`, `.reply(hhmm, text, judgement="clear")`, `.sent_to_group()`, `.sent_to_parent()`, `.clear()`.

- [ ] **Step 1: Write the harness**

`tests/conftest.py`:

```python
from dataclasses import dataclass, field
from datetime import date, datetime

import pytest

from care import ladder, meds
from care.clock import local_day, local_time_on
from care.config import Config, load_config
from care.conversation import handle_reply
from care.delivery import Capture
from care.messages import Catalogue
from care.state import SEND_FAILED, EventLog
from care.triage import load_terms


@dataclass
class World:
    config: Config
    log: EventLog
    catalogue: Catalogue
    delivery: Capture
    tripwire: tuple
    affirmatives: tuple
    day: date = field(default_factory=lambda: date(2026, 9, 12))

    def moment(self, hhmm: str, day: date | None = None) -> datetime:
        return local_time_on(day or self.day, hhmm, self.config.timezone)

    def _execute(self, actions, now):
        stamp = local_day(now, self.config.timezone)
        for action in actions:
            if not action.chat_id:
                self.log.append(now, stamp, action.kind, subject=action.subject)
                continue
            if self.delivery.send(action.chat_id, action.text):
                self.log.append(now, stamp, action.kind, subject=action.subject)
            else:
                self.log.append(now, stamp, SEND_FAILED, subject=action.subject)

    def tick(self, hhmm: str, day: date | None = None) -> None:
        now = self.moment(hhmm, day)
        actions = ladder.due_actions(self.config, self.log, self.catalogue, now)
        actions += meds.due_actions(self.config, self.log, self.catalogue, now)
        self._execute(actions, now)

    def reply(self, hhmm: str, text: str, judgement: str = "clear", day: date | None = None):
        now = self.moment(hhmm, day)
        directive = handle_reply(
            self.config,
            self.log,
            self.catalogue,
            self.tripwire,
            self.affirmatives,
            text,
            now,
            judgement,
        )
        self._execute(list(directive.actions), now)
        return directive

    def sent_to_group(self) -> list[str]:
        return [text for chat, text in self.delivery.sent if chat == self.config.group_chat_id]

    def sent_to_parent(self) -> list[str]:
        return [text for chat, text in self.delivery.sent if chat == self.config.parent.chat_id]

    def clear(self) -> None:
        self.delivery.sent.clear()


@pytest.fixture
def world() -> World:
    config = load_config("config/roster.example.yaml", "config/meds.example.yaml")
    return World(
        config=config,
        log=EventLog.in_memory(),
        catalogue=Catalogue.load("config", config.message_language),
        delivery=Capture(),
        tripwire=load_terms("config", "tripwire", config.languages),
        affirmatives=load_terms("config", "affirmatives", config.languages),
    )
```

- [ ] **Step 2: Write the scenario tests**

`tests/test_scenarios.py`:

```python
from datetime import date

from care.control import resume, skip_today, stop
from care.state import ESCALATED_ADHERENCE, STOPPED, EventLog

NEXT = date(2026, 9, 13)


def test_scenario_1_happy_path(world):
    world.tick("09:00")
    world.reply("09:12", "все добре, снідаю")
    world.tick("12:00")
    world.tick("15:00")
    assert len(world.sent_to_parent()) >= 1
    assert world.sent_to_group() == []


def test_scenario_2_silence_escalates(world):
    world.tick("09:00")
    world.tick("12:00")
    assert len(world.sent_to_parent()) == 2
    world.tick("15:00")
    assert len(world.sent_to_group()) == 1
    world.tick("18:00")
    assert len(world.sent_to_group()) == 1


def test_scenario_3_late_reply_stands_down(world):
    world.tick("09:00")
    world.tick("12:00")
    world.tick("15:00")
    world.clear()
    world.reply("16:00", "вибач, була у саду")
    assert len(world.sent_to_group()) == 1
    assert "16:00" in world.sent_to_group()[0]


def test_scenario_4_tripwire_at_night_ignores_quiet_hours(world):
    directive = world.reply("23:40", "я впала, не можу встати")
    assert directive.do == "acknowledge_escalation"
    assert len(world.sent_to_group()) == 1


def test_scenario_5_unclear_then_clear_does_not_escalate(world):
    world.tick("09:00")
    assert world.reply("09:20", "нормально", judgement="unclear").do == "clarify"
    assert world.reply("09:25", "все добре, просто не виспалася").do == "acknowledge"
    assert world.sent_to_group() == []


def test_scenario_6_unclear_twice_escalates_with_both_quoted(world):
    world.tick("09:00")
    world.reply("09:20", "нормально", judgement="unclear")
    world.reply("09:30", "та нічого", judgement="unclear")
    assert len(world.sent_to_group()) == 1
    assert "та нічого" in world.sent_to_group()[0]


def test_scenario_7_parent_asking_for_silence_does_not_suppress(world):
    world.reply("10:00", "я впала, тільки не кажи дітям")
    assert len(world.sent_to_group()) == 1


def test_scenario_8_dose_confirmed_by_bare_affirmative(world):
    world.tick("08:30")
    assert world.sent_to_parent() == ["Нагадування: the blood pressure tablet."]
    directive = world.reply("08:40", "так")
    assert directive.verdict == "clear"


def test_scenario_8b_ukrainian_inflection_all_trip(world):
    for text in ("впала", "впав", "упали"):
        fresh = EventLog.in_memory()
        world.log = fresh
        world.clear()
        assert world.reply("10:00", text).verdict == "tripwire"


def test_scenario_8c_affirmative_plus_tripwire_escalates(world):
    world.tick("08:30")
    world.clear()
    directive = world.reply("08:40", "так, випила, але дуже погано")
    assert directive.verdict == "tripwire"
    assert len(world.sent_to_group()) == 1


def test_scenario_9_same_dose_two_days_escalates_once(world):
    world.tick("08:30")
    world.tick("09:15")
    world.tick("10:30")
    world.clear()
    world.tick("08:30", day=NEXT)
    world.tick("09:15", day=NEXT)
    world.tick("10:30", day=NEXT)
    assert world.sent_to_group() == []
    world.tick("10:35", day=NEXT)
    assert len(world.sent_to_group()) == 1
    world.tick("11:00", day=NEXT)
    assert len(world.sent_to_group()) == 1


def test_scenario_10_two_doses_one_day_escalates_once(world):
    for hhmm in ("08:30", "09:15", "10:30", "20:00", "20:45", "22:00"):
        world.tick(hhmm)
    assert world.sent_to_group() == []
    world.tick("22:05")
    assert len(world.sent_to_group()) == 1
    world.tick("22:30")
    assert len(world.sent_to_group()) == 1


def test_scenario_11_evening_dose_in_quiet_hours_is_dropped(world, tmp_path):
    meds_file = tmp_path / "meds.yaml"
    meds_file.write_text('doses:\n  - id: night\n    label: the night tablet\n    at: "22:00"\n')
    from care.config import load_config

    world.config = load_config("config/roster.example.yaml", meds_file)
    world.tick("22:00")
    assert world.sent_to_parent() == []


def test_scenario_12_skip_today_silences_the_ladder(world):
    outcome = skip_today(
        world.config, world.log, world.catalogue, world.config.parent.chat_id, world.moment("08:00")
    )
    world._execute(list(outcome.actions), world.moment("08:00"))
    world.tick("09:00")
    world.tick("12:00")
    world.tick("15:00")
    assert world.sent_to_parent() == []
    assert len(world.sent_to_group()) == 1


def test_scenario_13_stop_then_family_resume(world):
    stopped = stop(
        world.config, world.log, world.catalogue, world.config.parent.chat_id, world.moment("08:00")
    )
    world._execute(list(stopped.actions), world.moment("08:00"))
    world.tick("09:00")
    assert world.sent_to_parent() == []
    assert world.log.has("2026-09-12", STOPPED)

    resumed = resume(
        world.config, world.log, world.catalogue, "22222222", world.moment("08:00", NEXT)
    )
    assert resumed.ok
    world.tick("09:00", day=NEXT)
    assert len(world.sent_to_parent()) == 1


def test_scenario_14_unauthorised_schedule_change_refused(world):
    from care.control import change_schedule

    outcome = change_schedule(
        world.config, world.log, world.catalogue, "999999999", world.moment("12:00"), detail="06:00"
    )
    assert not outcome.ok
    assert world.sent_to_group() == []


def test_scenario_15_repeated_tick_sends_once(world):
    world.tick("09:00")
    world.tick("09:00")
    world.tick("09:00")
    assert len(world.sent_to_parent()) == 1


def test_scenario_16_missed_window_sends_late(world):
    world.tick("09:20")
    assert len(world.sent_to_parent()) == 1


def test_scenario_17_failed_escalation_is_retried(world):
    world.tick("09:00")
    world.tick("12:00")
    world.clear()
    world.delivery.fail_next(1)
    world.tick("15:00")
    assert world.sent_to_group() == []
    world.tick("15:05")
    assert len(world.sent_to_group()) == 1


def test_scenario_18_dst_boundary_keeps_local_times(world):
    world.tick("09:00", day=date(2026, 10, 24))
    world.tick("09:00", day=date(2026, 10, 26))
    assert len(world.sent_to_parent()) == 2
```

- [ ] **Step 3: Run the scenario tests**

Run: `python3 -m pytest tests/test_scenarios.py -v`
Expected: some failures on first run — this suite is the integration gate and will expose gaps the unit tests did not.

- [ ] **Step 4: Fix what the scenarios expose**

Work each failure back to its owning module and fix it there, not in the test. Two are known in advance and must be handled:

*Scenario 17 (failed escalation retried).* `care.ladder.due_actions` returns nothing once `ESCALATED_SILENCE` is in the log. On a send failure the CLI records `SEND_FAILED`, not `ESCALATED_SILENCE`, so the next tick re-derives the escalation and retries. Confirm this holds; if `ESCALATED_SILENCE` is being recorded on failure, fix `_execute`.

*Scenario 11 (quiet-hours dose).* The suppression event has an empty `chat_id`, so `_execute` records it without sending. Confirm `Action(kind=SUPPRESSED_QUIET, chat_id="", ...)` is what `meds.due_actions` emits.

*Scenarios 9 and 10 (adherence).* The escalation arrives on the tick *after* the one that closes the final unconfirmed dose — see the note at the end of Task 8. The tests already expect this; do not "fix" it by making `meds.due_actions` count actions it has not yet recorded.

- [ ] **Step 5: Run the whole suite**

Run: `python3 -m pytest -v`
Expected: every test passes

- [ ] **Step 6: Commit**

```bash
git add tests/conftest.py tests/test_scenarios.py care/
git commit -m "Add end-to-end scenario suite covering the full ladder"
```

---

## Task 13: Hermes profile integration

Implements spec §12 (agent integration), §1 and §6.3 (boundaries in SOUL.md), §2 (telegram group gates).

**Files:**
- Create: `profile/SOUL.md`
- Create: `profile/config-fragment.yaml`
- Create: `skills/family-care/SKILL.md`
- Create: `scripts/install-profile.sh`

**Interfaces:**
- Consumes: the `care` CLI contract from Task 11.
- Produces: nothing importable; this task wires the agent to the CLI.

- [ ] **Step 1: Write SOUL.md**

`profile/SOUL.md`:

```markdown
You check in on one person each day and make sure their family knows when
something needs a human. You are not a health tool.

## What you never do

- You do not give health advice, of any kind, ever.
- You do not diagnose, guess at a cause, or say what a symptom might mean.
- You do not tell anyone to start, stop, split, double, or delay a dose.
- You do not reassure medically. Never "I'm sure it's nothing", never "that
  sounds normal", never "don't worry about it".
- You do not interpret lab results, prescriptions, or anything from a doctor.
- You do not repeat dosage amounts. You say "the evening tablet", never a
  number of milligrams, even if someone tells you one.

When asked for any of these, say plainly that it is not something you do, and
that their doctor or pharmacist is the person to ask.

## The rule that overrides everything else

If a message trips the concern list, the family is told. This is not
negotiable and is not yours to decide.

If she asks you not to tell them — "don't worry the kids", "keep this between
us" — you do not comply. Say so honestly, once, without arguing:

> I always let the family know about messages like this, and I've told them
> now. You can talk to them yourself any time.

Then stop. Do not negotiate, do not moralise, do not repeat it, do not
apologise for it. A check-in agent that can be talked out of escalating is
worse than no agent at all, because the family believes someone is watching
and nobody is.

Never say you have contacted anyone unless the tool actually reported that it
succeeded.

## How you talk to her

Short sentences. Plain words. One question per message, never two.

Warm, not chirpy. No walls of emoji. No exclamation marks stacked up.

Never guilt her about a dose she did not confirm. Not confirming is not the
same as not taking it, and you do not know which happened.

If she sounds frightened, answer that first, in one sentence, before anything
else.

Write in the language she writes in.

## How you talk to the family group

Rarely. A message from you in the group means something needs attention, and
that only stays true if you do not chatter there.

When you post a schedule change or a confirmation, post the exact line the
`care` tool gave you. Do not rewrite it, summarise it, or add to it.
```

- [ ] **Step 2: Write the agent skill**

`skills/family-care/SKILL.md`:

````markdown
---
name: family-care
description: Use on every message from the parent or the family group in the care-check profile - records the reply, gets the verdict, and obeys the returned directive.
---

# Family care check

The `care` CLI owns every timer, the concern list, and every escalation. You
own phrasing and judgement. You never decide whether to escalate.

Run from `/root/tg-family-assitsant`.

## On every message from the parent

Call this before writing anything:

```
python3 -m care --roster config/roster.yaml --meds config/meds.yaml \
  --config-dir config reply --text "<her message, verbatim>" \
  --judgement <clear|unclear>
```

`--judgement` is your reading of the message, and only that:

- `clear` — ordinary. Answers the question, sounds like her.
- `unclear` — something is off: unusually terse for her, vague unwellness,
  mentions skipping or running out of medication, confused phrasing, or does
  not answer what was asked.

Pass the message **verbatim**. Do not tidy it, translate it, or summarise it.
The tool matches against the raw text and quotes it to the family.

It prints JSON. Obey `do`:

| `do` | What you write |
|---|---|
| `acknowledge` | One or two warm sentences. Nothing else. |
| `clarify` | Exactly one question. Never a second — the tool will not give you another. |
| `acknowledge_escalation` | Tell her plainly that you have let the family know, and ask if she would like someone to call. No advice. |

The tool has already sent any escalation itself by the time you read the JSON.
Never send one yourself, and never tell her the family has been told unless
`escalated` is `true`.

## When she confirms a dose

```
python3 -m care ... confirm --dose <dose-id>
```

Dose ids are in `config/meds.yaml`. Only call this when she has actually said
she took it.

## In the family group

A family member replying to an escalation:

```
python3 -m care ... acknowledge --actor <their chat id>
```

A schedule change ("move the evening one to 8"):

1. Edit `config/meds.yaml` or `config/roster.yaml` to make the change.
2. Record and announce it:

```
python3 -m care ... schedule --actor <their chat id> \
  --detail "evening tablet 20:00 to 20:30"
```

3. Post the line the tool printed to the group, **verbatim**.

If the tool exits non-zero, the person was not allowed to make that change.
Say so plainly and change nothing.

## Pause, skip, stop

```
python3 -m care ... pause  --actor <chat id> [--until YYYY-MM-DD]
python3 -m care ... skip   --actor <chat id>
python3 -m care ... stop   --actor <chat id>
python3 -m care ... resume --actor <chat id>
```

She may `skip` and `stop`. Only family may `pause`, `resume`, and `schedule`.
Each prints a line for the group — post it verbatim.

## Never

- Never compose an escalation yourself.
- Never skip the `reply` call because a message looks obviously fine. The
  concern list catches things you will not.
- Never ask a second clarifying question.
- Never say a message was delivered when the tool did not say so.
````

- [ ] **Step 3: Write the profile config fragment**

`profile/config-fragment.yaml`:

```yaml
telegram:
  allowed_chats:
    - "-1001234567890"
  group_allowed_chats:
    - "-1001234567890"
  require_mention: true
  observe_unmentioned_group_messages: true

skills:
  external_dirs:
    - /root/tg-family-assitsant/skills
  write_approval: true

terminal:
  backend: local
  cwd: /root/tg-family-assitsant
  timeout: 60

platform_toolsets:
  telegram:
    - terminal
    - skills
    - todo
    - clarify

plugins:
  enabled:
    - safety-gate
```

Replace `-1001234567890` with the real group id from `docs/operations.md` Part 1 before installing.

Note the deliberately narrow `platform_toolsets.telegram`: the agent needs `terminal` to call `care` and nothing else. No `web`, no `code_execution`, no `delegation` — an agent talking to an elderly parent has no business browsing or spawning subagents.

- [ ] **Step 4: Write the install script**

`scripts/install-profile.sh`:

```sh
#!/bin/sh
set -eu
PROFILE="${CARE_PROFILE:-telegram-family-assistant}"
REPO="${CARE_REPO:-/root/tg-family-assitsant}"
TARGET="$HOME/.hermes/profiles/$PROFILE"

if [ ! -d "$TARGET" ]; then
  echo "no such profile: $TARGET" >&2
  exit 1
fi

cp "$REPO/profile/SOUL.md" "$TARGET/SOUL.md"
mkdir -p "$HOME/.hermes/scripts"
ln -sf "$REPO/scripts/care-tick.sh" "$HOME/.hermes/scripts/care-tick.sh"

echo "Installed SOUL.md and linked care-tick.sh."
echo "Merge $REPO/profile/config-fragment.yaml into $TARGET/config.yaml by hand,"
echo "replacing the placeholder group chat id first."
```

Then: `chmod +x scripts/install-profile.sh`

The config fragment is merged by hand on purpose: the profile's `config.yaml` already holds the model and plugin settings, and a script that rewrites it is a script that can silently break a working gateway.

- [ ] **Step 5: Verify the skill's commands actually run**

Run each against the example config to confirm the documented invocations are correct:

```bash
python3 -m care --roster config/roster.example.yaml --meds config/meds.example.yaml \
  --config-dir config --state-db /tmp/care-check.db --dry-run \
  reply --text "все добре" --judgement clear
python3 -m care --roster config/roster.example.yaml --meds config/meds.example.yaml \
  --config-dir config --state-db /tmp/care-check.db --dry-run \
  triage --text "я впала"
rm -f /tmp/care-check.db
```

Expected: the first prints `{"verdict": "clear", "do": "acknowledge", ...}`, the second prints a `tripwire` verdict with `впал` in `matched`.

- [ ] **Step 6: Commit**

```bash
git add profile/ skills/ scripts/install-profile.sh
git commit -m "Add profile SOUL, agent skill, config fragment and installer"
```

---

## Task 14: Documentation

Implements spec §4 (README), §13 and §15 (what the docs must say), and completes `docs/` referenced throughout.

**Files:**
- Create: `README.md`
- Create: `docs/escalation-policy.md`
- Create: `docs/message-catalogue.md`
- Modify: `docs/operations.md` (add a "Commands" cross-reference to the README)

**Interfaces:**
- Consumes: everything.
- Produces: nothing importable.

- [ ] **Step 1: Write the README**

`README.md` must cover, in this order:

1. **What this is** — one paragraph: a Telegram care-check agent for one parent, running on the Hermes `telegram-family-assistant` profile. State the boundary in the second sentence: reminders and escalation, never health advice.
2. **What it does, concretely** — the ladder as a table (check-in 09:00, nudge +3h, group +6h; dose reminder, nudge +45m, close +2h), and the two-layer concern detection.
3. **How it is built** — the deterministic-spine diagram from spec §3, and the one-line rule: inference may add care, never remove it.
4. **Install** — clone, `pip install -e .`, copy `config/roster.example.yaml` to `config/roster.yaml`, same for meds, then point at `docs/operations.md` Part 1 for the group id.
5. **Configure** — every key in `roster.yaml` and `meds.yaml` in a table, with type, default, and what happens if it is wrong.
6. **Run** — the cron install, `--no-agent`, and why an uneventful tick is silent.
7. **Every command** — `tick`, `reply`, `confirm`, `triage`, `acknowledge`, `status`, `log`, `pause`, `resume`, `skip`, `stop`, `schedule`, `doctor`; each with a real invocation and its output.
8. **The rules that are not negotiable** — the non-suppression rule, escalations ignoring quiet hours, unconfirmed ≠ missed, no dosage amounts. Link to `docs/escalation-policy.md`.
9. **Editing the word lists** — the three matching modes, why Ukrainian needs `prefix`, and `care triage --text` as the way to test a change.
10. **Testing** — `python3 -m pytest`, no network or model needed, and what the 20 scenarios in `tests/test_scenarios.py` cover.
11. **Development conventions** — no comments, no docstrings, explanation lives in `docs/`; stdlib plus PyYAML only.
12. **What this deliberately does not do** — spec §15, verbatim in spirit: no health advice, no daily all-fine digest, no SMS or voice fallback, no multi-parent support, no dosage storage, no dashboard.
13. **Links** — spec, operations runbook, escalation policy, message catalogue.

- [ ] **Step 2: Write the escalation policy, for the family to read**

`docs/escalation-policy.md` is written for a non-technical family member, not an engineer. It states, in plain language:

- what time the check-in arrives and what happens if there is no answer
- exactly when the group gets a message, and what that message will contain
- that a reply to the bot's escalation is how you tell it someone is handling it
- that an unconfirmed dose is not a missed dose, and what the group will and will not be told
- that the parent can pause or stop, that stopping is honoured, and that the group is always told when it happens
- that the bot will never give health advice and will never be talked out of escalating, including by the parent
- who may change the schedule, and that every change is announced

- [ ] **Step 3: Write the message catalogue doc**

`docs/message-catalogue.md` quotes every template from `config/messages.uk.yaml` and `config/messages.en.yaml` side by side, with the trigger for each and the values it interpolates. This is the file someone reads when they want to change wording without reading code.

- [ ] **Step 4: Cross-reference from the runbook**

In `docs/operations.md`, under "Part 4 — Day-to-day", add a line pointing at the README's command reference so the runbook does not duplicate it.

- [ ] **Step 5: Verify every command in the README actually works**

Run each documented invocation against the example config with `--dry-run` and a throwaway `--state-db`. Fix the README where output differs from what is documented. Do not fix it by changing the expectation — if a command's output is confusing, change the command.

- [ ] **Step 6: Commit**

```bash
git add README.md docs/
git commit -m "Add README, escalation policy and message catalogue"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §1 purpose, out of scope | 13 (SOUL.md), 14 (README) |
| §2 channel topology | 2 (roster), 13 (config fragment) |
| §3 architecture, inference rule | 6, 7, 11 (CLI never calls a model), 14 |
| §4 layout, §4.1 conventions | 1, and every task |
| §5.1 ladder timings | 7 |
| §5.2 tick semantics, idempotency | 7, 11, 12 (scenarios 15, 16) |
| §5.3 quiet hours | 1 (`in_quiet_hours`), 7, 8, 12 (scenario 11) |
| §5.4 stand-down, acknowledgement | 9, 12 (scenario 3) |
| §6.1 tripwire, matching modes | 4 |
| §6.2 reply contract | 9, 11 |
| §6.3 non-suppression | 9, 12 (scenario 7), 13 (SOUL.md) |
| §7.1 dose schedule, no dosages | 2, 14 |
| §7.2 dose ladder, affirmative path | 8, 4, 12 (scenarios 8, 8c) |
| §7.3 unconfirmed ≠ missed, repeat rule | 5 (message), 8, 12 (scenarios 9, 10) |
| §8.1 authorisation | 10, 12 (scenario 14) |
| §8.2 pause, skip, stop | 10, 12 (scenarios 12, 13) |
| §9 data model | 3 |
| §10 delivery, retry, catalogues | 5, 6, 11, 12 (scenario 17) |
| §11 cron | 11 |
| §12 agent integration | 13 |
| §13 testing | every task, gated by 12 |
| §14 prerequisites | 14 (README), `docs/operations.md` |
| §15 not built | 14 |

No spec section is unimplemented.

**Placeholder scan:** no `TBD`, no "add error handling", no "similar to Task N". Every code step carries the code. Task 14 specifies documentation by required content rather than by full prose, which is the one place a literal transcription would be longer than useful — each item is a concrete, checkable requirement.

**Type consistency:** `Action(kind, chat_id, text, subject)` is defined once in `ladder.py` and imported by `meds.py`, `conversation.py`, `control.py`, `cli.py`. `Action.kind` is always an event-kind constant from `state.py`, so executing an action and recording it use the same string. `due_actions(config, log, catalogue, now_utc)` has the same signature in `ladder.py` and `meds.py`. `EventLog.last(kind, subject, local_day)` keeps that argument order at every call site. `Catalogue.render(key, **values)` raises `MessageError` on a missing value everywhere.

**One risk worth naming:** `EventLog.has(day, kind, subject=None)` returns true for *any* subject when `subject` is `None`, which reads oddly next to `last()`. It is only used in tests and `_cmd_log`; the state machines use `last()`. If Task 12 exposes a misuse, tighten `has()` rather than working around it.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-12-family-care-check-agent.md`.
