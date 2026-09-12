import shutil
from pathlib import Path

import pytest

from care.config import Config, load_config
from care.errors import ConfigError

REPO_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

CATALOGUE_FILES = [
    "tripwire.uk.yaml",
    "tripwire.en.yaml",
    "affirmatives.uk.yaml",
    "affirmatives.en.yaml",
    "negatives.uk.yaml",
    "negatives.en.yaml",
    "messages.uk.yaml",
    "messages.en.yaml",
]


@pytest.fixture
def config_dir(tmp_path) -> Path:
    for name in CATALOGUE_FILES:
        shutil.copy(REPO_CONFIG_DIR / name, tmp_path / name)
    shutil.copy(REPO_CONFIG_DIR / "roster.example.yaml", tmp_path / "roster.yaml")
    shutil.copy(REPO_CONFIG_DIR / "meds.example.yaml", tmp_path / "meds.yaml")
    return tmp_path


def _write_roster(config_dir: Path, text: str) -> None:
    (config_dir / "roster.yaml").write_text(text, encoding="utf-8")


def _write_meds(config_dir: Path, text: str) -> None:
    (config_dir / "meds.yaml").write_text(text, encoding="utf-8")


BASE_ROSTER = """\
timezone: Europe/Kyiv
quiet_hours:
  start: "21:30"
  end: "08:00"
checkin:
  time: "09:00"
  nudge_after_minutes: 180
  escalate_after_minutes: 360
languages: [uk, en]
delivery:
  mode: dry-run
state:
  path: /var/lib/care/care-state.db
parent:
  chat_id: "1"
  name: Mum
group:
  chat_id: "-100"
  name: Family
family:
  - chat_id: "2"
    name: Dima
"""


def test_load_config_succeeds_with_example_files(config_dir):
    config = load_config(config_dir)
    assert isinstance(config, Config)
    assert config.roster.timezone == "Europe/Kyiv"
    assert config.roster.delivery_mode == "dry-run"
    assert len(config.doses) == 2
    assert set(config.tripwires.keys()) == {"uk", "en"}
    assert set(config.affirmatives.keys()) == {"uk", "en"}
    assert set(config.negatives.keys()) == {"uk", "en"}
    assert set(config.messages.keys()) == {"uk", "en"}


def test_invalid_timezone_fails_cleanly(config_dir):
    _write_roster(config_dir, BASE_ROSTER.replace("Europe/Kyiv", "Not/AZone"))
    with pytest.raises(ConfigError):
        load_config(config_dir)


def test_zero_nudge_interval_fails_cleanly(config_dir):
    _write_roster(config_dir, BASE_ROSTER.replace("nudge_after_minutes: 180", "nudge_after_minutes: 0"))
    with pytest.raises(ConfigError):
        load_config(config_dir)


def test_negative_nudge_interval_fails_cleanly(config_dir):
    _write_roster(config_dir, BASE_ROSTER.replace("nudge_after_minutes: 180", "nudge_after_minutes: -5"))
    with pytest.raises(ConfigError):
        load_config(config_dir)


def test_ladder_ordering_violation_fails(config_dir):
    _write_roster(
        config_dir,
        BASE_ROSTER.replace("nudge_after_minutes: 180", "nudge_after_minutes: 400"),
    )
    with pytest.raises(ConfigError):
        load_config(config_dir)


def test_role_conflict_parent_equals_group_fails(config_dir):
    _write_roster(config_dir, BASE_ROSTER.replace('chat_id: "-100"', 'chat_id: "1"'))
    with pytest.raises(ConfigError):
        load_config(config_dir)


def test_duplicate_family_chat_id_fails(config_dir):
    roster = BASE_ROSTER + '  - chat_id: "2"\n    name: Duplicate\n'
    _write_roster(config_dir, roster)
    with pytest.raises(ConfigError):
        load_config(config_dir)


def test_invalid_delivery_mode_fails(config_dir):
    _write_roster(config_dir, BASE_ROSTER.replace("mode: dry-run", "mode: chaotic"))
    with pytest.raises(ConfigError):
        load_config(config_dir)


def test_relative_state_path_fails(config_dir):
    _write_roster(config_dir, BASE_ROSTER.replace("/var/lib/care/care-state.db", "relative/care.db"))
    with pytest.raises(ConfigError):
        load_config(config_dir)


def test_checkin_wholly_inside_quiet_hours_fails(config_dir):
    _write_roster(config_dir, BASE_ROSTER.replace('time: "09:00"', 'time: "22:00"'))
    with pytest.raises(ConfigError):
        load_config(config_dir)


def test_checkin_touching_but_not_wholly_inside_quiet_hours_passes(config_dir):
    _write_roster(config_dir, BASE_ROSTER.replace('time: "09:00"', 'time: "07:50"'))
    load_config(config_dir)


@pytest.mark.parametrize("label", ["50mg tablet", "take 2 tablets", "10 ml syrup"])
def test_dosage_like_med_label_fails(config_dir, label):
    _write_meds(
        config_dir,
        f'- id: bad_dose\n  label: "{label}"\n  time: "08:00"\n',
    )
    with pytest.raises(ConfigError):
        load_config(config_dir)


def test_ordinary_med_label_passes(config_dir):
    _write_meds(config_dir, '- id: ok_dose\n  label: "the blood pressure tablet"\n  time: "08:00"\n')
    load_config(config_dir)


def test_duplicate_dose_id_fails(config_dir):
    _write_meds(
        config_dir,
        '- id: dup\n  label: "a"\n  time: "08:00"\n'
        '- id: dup\n  label: "b"\n  time: "09:00"\n',
    )
    with pytest.raises(ConfigError):
        load_config(config_dir)


def test_missing_enabled_language_catalogue_fails(config_dir):
    (config_dir / "tripwire.en.yaml").unlink()
    with pytest.raises(ConfigError):
        load_config(config_dir)


def test_mismatched_message_keys_across_languages_fails(config_dir):
    (config_dir / "messages.en.yaml").write_text(
        "language: en\nmessages:\n  only_here: \"hello\"\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(config_dir)


def test_unknown_match_mode_in_catalogue_fails(config_dir):
    (config_dir / "tripwire.en.yaml").write_text(
        "language: en\nentries:\n  - category: x\n    mode: fuzzy\n    terms:\n      - foo\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(config_dir)


def test_malformed_yaml_fails_with_config_error(config_dir):
    (config_dir / "roster.yaml").write_text("timezone: [unterminated", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(config_dir)
