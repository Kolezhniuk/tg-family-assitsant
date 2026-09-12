from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from types import MappingProxyType

import yaml

from care import triage
from care.clock import QuietHours, add_minutes, load_zone, parse_hhmm, times_wholly_in_quiet_hours
from care.errors import ConfigError
from care.redaction import contains_dosage_notation

SUPPORTED_LANGUAGES = ("uk", "en")
DELIVERY_MODES = ("dry-run", "live")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CHECKIN_WINDOW_MINUTES = 30


@dataclass(frozen=True)
class Person:
    chat_id: str
    name: str


@dataclass(frozen=True)
class CheckinSchedule:
    time: time
    nudge_after_minutes: int
    escalate_after_minutes: int


@dataclass(frozen=True)
class Roster:
    timezone: str
    quiet_hours: QuietHours
    checkin: CheckinSchedule
    languages: tuple[str, ...]
    delivery_mode: str
    state_path: Path
    parent: Person
    group: Person
    family: tuple[Person, ...]


@dataclass(frozen=True)
class Dose:
    id: str
    label: str
    time: time
    weekdays: tuple[str, ...]


@dataclass(frozen=True)
class MessageCatalogue:
    language: str
    messages: MappingProxyType


@dataclass(frozen=True)
class Config:
    roster: Roster
    doses: tuple[Dose, ...]
    tripwires: MappingProxyType
    affirmatives: MappingProxyType
    negatives: MappingProxyType
    messages: MappingProxyType


def _read_yaml(path: Path):
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read file: {exc}") from exc
    try:
        return yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc


def _require_str(data: dict, key: str, path: Path, section: str = "") -> str:
    label = f"{section}.{key}" if section else key
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}: {label} must be a non-empty string, got {value!r}")
    return value


def _require_hhmm(data: dict, key: str, path: Path, section: str = "") -> time:
    label = f"{section}.{key}" if section else key
    value = _require_str(data, key, path, section)
    try:
        return parse_hhmm(value)
    except ValueError as exc:
        raise ConfigError(f"{path}: {label} is not a valid HH:MM time: {value!r}") from exc


def _require_positive_int(data: dict, key: str, path: Path, section: str = "") -> int:
    label = f"{section}.{key}" if section else key
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{path}: {label} must be a positive integer, got {value!r}")
    return value


def _validate_languages(raw, path: Path) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{path}: languages must be a non-empty list")
    languages = []
    for item in raw:
        if item not in SUPPORTED_LANGUAGES:
            raise ConfigError(f"{path}: unsupported language {item!r}; supported: {SUPPORTED_LANGUAGES}")
        if item in languages:
            raise ConfigError(f"{path}: duplicate language {item!r}")
        languages.append(item)
    return tuple(languages)


def _load_person(data: dict, key: str, path: Path) -> Person:
    raw = data.get(key)
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: {key} section is required")
    chat_id = _require_str(raw, "chat_id", path, section=key)
    name = raw.get("name", key)
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{path}: {key}.name must be a non-empty string")
    return Person(chat_id=chat_id, name=name)


def _load_family(data: dict, path: Path) -> tuple[Person, ...]:
    raw = data.get("family", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ConfigError(f"{path}: family must be a list")
    people = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"{path}: family[{index}] must be a mapping")
        chat_id = _require_str(item, "chat_id", path, section=f"family[{index}]")
        name = _require_str(item, "name", path, section=f"family[{index}]")
        people.append(Person(chat_id=chat_id, name=name))
    return tuple(people)


def _validate_role_conflicts(parent: Person, group: Person, family: tuple[Person, ...], path: Path) -> None:
    seen: dict[str, str] = {}
    for role, person in [("parent", parent), ("group", group), *[(f"family[{i}]", p) for i, p in enumerate(family)]]:
        if person.chat_id in seen:
            raise ConfigError(
                f"{path}: chat_id {person.chat_id!r} is used by both {seen[person.chat_id]!r} and {role!r}"
            )
        seen[person.chat_id] = role


def _load_roster(path: Path) -> Roster:
    data = _read_yaml(path)
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: roster must be a mapping")

    tz_name = _require_str(data, "timezone", path)
    try:
        load_zone(tz_name)
    except ValueError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    quiet_raw = data.get("quiet_hours", {"start": "21:30", "end": "08:00"})
    if not isinstance(quiet_raw, dict):
        raise ConfigError(f"{path}: quiet_hours must be a mapping")
    quiet = QuietHours(
        start=_require_hhmm(quiet_raw, "start", path, section="quiet_hours"),
        end=_require_hhmm(quiet_raw, "end", path, section="quiet_hours"),
    )

    checkin_raw = data.get("checkin")
    if not isinstance(checkin_raw, dict):
        raise ConfigError(f"{path}: checkin section is required")
    checkin_time = _require_hhmm(checkin_raw, "time", path, section="checkin")
    nudge_after = _require_positive_int(checkin_raw, "nudge_after_minutes", path, section="checkin")
    escalate_after = _require_positive_int(checkin_raw, "escalate_after_minutes", path, section="checkin")
    if escalate_after <= nudge_after:
        raise ConfigError(
            f"{path}: checkin.escalate_after_minutes ({escalate_after}) must be greater than "
            f"checkin.nudge_after_minutes ({nudge_after})"
        )

    window_end = add_minutes(checkin_time, CHECKIN_WINDOW_MINUTES)
    if times_wholly_in_quiet_hours(checkin_time, window_end, quiet):
        raise ConfigError(
            f"{path}: checkin.time {checkin_time} falls wholly inside quiet hours "
            f"({quiet.start}-{quiet.end})"
        )

    languages = _validate_languages(data.get("languages", list(SUPPORTED_LANGUAGES)), path)

    delivery_raw = data.get("delivery")
    if not isinstance(delivery_raw, dict):
        raise ConfigError(f"{path}: delivery section is required")
    mode = delivery_raw.get("mode")
    if mode not in DELIVERY_MODES:
        raise ConfigError(f"{path}: delivery.mode must be one of {DELIVERY_MODES}, got {mode!r}")

    state_raw = data.get("state")
    if not isinstance(state_raw, dict):
        raise ConfigError(f"{path}: state section is required")
    state_value = _require_str(state_raw, "path", path, section="state")
    state_path = Path(os.path.expanduser(state_value))
    if not state_path.is_absolute():
        raise ConfigError(f"{path}: state.path must be an absolute path, got {state_value!r}")

    parent = _load_person(data, "parent", path)
    group = _load_person(data, "group", path)
    family = _load_family(data, path)
    _validate_role_conflicts(parent, group, family, path)

    return Roster(
        timezone=tz_name,
        quiet_hours=quiet,
        checkin=CheckinSchedule(
            time=checkin_time,
            nudge_after_minutes=nudge_after,
            escalate_after_minutes=escalate_after,
        ),
        languages=languages,
        delivery_mode=mode,
        state_path=state_path,
        parent=parent,
        group=group,
        family=family,
    )


def _load_meds(path: Path) -> tuple[Dose, ...]:
    data = _read_yaml(path)
    if not isinstance(data, list):
        raise ConfigError(f"{path}: meds file must be a list")

    doses = []
    seen_ids: set[str] = set()
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ConfigError(f"{path}: doses[{index}] must be a mapping")
        dose_id = _require_str(item, "id", path, section=f"doses[{index}]")
        if dose_id in seen_ids:
            raise ConfigError(f"{path}: duplicate dose id {dose_id!r}")
        seen_ids.add(dose_id)

        label = _require_str(item, "label", path, section=f"doses[{index}]")
        if contains_dosage_notation(label):
            raise ConfigError(
                f"{path}: dose {dose_id!r} label contains dosage notation (labels must describe, "
                f"never quantify): {label!r}"
            )

        dose_time = _require_hhmm(item, "time", path, section=f"doses[{index}]")

        weekdays_raw = item.get("weekdays", list(WEEKDAYS))
        if not isinstance(weekdays_raw, list) or not weekdays_raw:
            raise ConfigError(f"{path}: doses[{index}].weekdays must be a non-empty list")
        weekdays = []
        for day in weekdays_raw:
            if day not in WEEKDAYS:
                raise ConfigError(f"{path}: doses[{index}].weekdays has unknown weekday {day!r}")
            if day in weekdays:
                raise ConfigError(f"{path}: doses[{index}].weekdays has duplicate {day!r}")
            weekdays.append(day)

        doses.append(Dose(id=dose_id, label=label, time=dose_time, weekdays=tuple(weekdays)))

    return tuple(doses)


def _load_term_catalogues(config_dir: Path, kind: str, languages: tuple[str, ...]):
    result = {}
    for lang in languages:
        path = config_dir / f"{kind}.{lang}.yaml"
        if not path.exists():
            raise ConfigError(f"{path}: required {kind} catalogue is missing")
        result[lang] = triage.load_catalogue(path)
    return result


def _load_messages(config_dir: Path, languages: tuple[str, ...]):
    catalogues: dict[str, MessageCatalogue] = {}
    for lang in languages:
        path = config_dir / f"messages.{lang}.yaml"
        if not path.exists():
            raise ConfigError(f"{path}: required messages catalogue is missing")
        data = _read_yaml(path)
        if not isinstance(data, dict) or data.get("language") != lang:
            raise ConfigError(f"{path}: messages catalogue must declare language: {lang!r}")
        messages = data.get("messages")
        if not isinstance(messages, dict) or not messages:
            raise ConfigError(f"{path}: messages must be a non-empty mapping")
        for key, value in messages.items():
            if not isinstance(key, str) or not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{path}: message {key!r} must map to a non-empty string")
        catalogues[lang] = MessageCatalogue(language=lang, messages=MappingProxyType(dict(messages)))

    reference_lang = None
    reference_keys = None
    for lang, catalogue in catalogues.items():
        keys = set(catalogue.messages.keys())
        if reference_keys is None:
            reference_keys = keys
            reference_lang = lang
            continue
        if keys != reference_keys:
            raise ConfigError(
                f"messages.{lang}.yaml keys do not match messages.{reference_lang}.yaml: "
                f"{sorted(reference_keys ^ keys)}"
            )
    return catalogues


def load_config(
    config_dir: Path,
    *,
    roster_path: Path | None = None,
    meds_path: Path | None = None,
) -> Config:
    roster_path = roster_path or (config_dir / "roster.yaml")
    meds_path = meds_path or (config_dir / "meds.yaml")

    errors: list[str] = []
    roster: Roster | None = None
    doses: tuple[Dose, ...] = ()
    tripwires: dict = {}
    affirmatives: dict = {}
    negatives: dict = {}
    messages: dict = {}

    try:
        roster = _load_roster(roster_path)
    except ConfigError as exc:
        errors.append(str(exc))

    try:
        doses = _load_meds(meds_path)
    except ConfigError as exc:
        errors.append(str(exc))

    languages = roster.languages if roster is not None else tuple(SUPPORTED_LANGUAGES)

    for kind, target in (("tripwire", "tripwires"), ("affirmatives", "affirmatives"), ("negatives", "negatives")):
        try:
            loaded = _load_term_catalogues(config_dir, kind, languages)
        except ConfigError as exc:
            errors.append(str(exc))
            continue
        if target == "tripwires":
            tripwires = loaded
        elif target == "affirmatives":
            affirmatives = loaded
        else:
            negatives = loaded

    try:
        messages = _load_messages(config_dir, languages)
    except ConfigError as exc:
        errors.append(str(exc))

    if errors:
        raise ConfigError("; ".join(errors))

    assert roster is not None
    return Config(
        roster=roster,
        doses=doses,
        tripwires=MappingProxyType(tripwires),
        affirmatives=MappingProxyType(affirmatives),
        negatives=MappingProxyType(negatives),
        messages=MappingProxyType(messages),
    )
