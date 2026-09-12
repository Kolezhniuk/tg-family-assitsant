from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from care.clock import Clock, load_zone, local_datetime_for
from care.config import load_config
from care.delivery import CaptureTransport, DeliveryWorker, HermesSendTransport
from care.models import UpdateEnvelope
from care.service import CareService
from care.store import open_store

PROFILE = "telegram-family-assistant"
REPO = Path(__file__).resolve().parent.parent
DEMO_DB = REPO / ".demo" / "care-state.db"


class MovableClock(Clock):
    def __init__(self, moment: datetime):
        self._moment = moment.astimezone(timezone.utc)

    def now_utc(self) -> datetime:
        return self._moment

    def set(self, moment: datetime) -> None:
        self._moment = moment.astimezone(timezone.utc)


def banner(text: str) -> None:
    print()
    print("=" * 72)
    print(text)
    print("=" * 72)


def show_sent(transport, since: int) -> int:
    calls = transport.calls if hasattr(transport, "calls") else []
    for call in calls[since:]:
        print(f"  -> telegram:{call.target}")
        for line in call.text.replace("\\", "").splitlines():
            print(f"     {line}")
        print()
    return len(calls)


def describe_outbox(store, episode_id: str) -> None:
    print("  outbox state:")
    for row in store.list_outbox(episode_id=episode_id):
        receipt = row.receipt_id or "-"
        print(f"     {row.action_key:38} {row.status:10} receipt={receipt}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()

    if DEMO_DB.exists():
        shutil.rmtree(DEMO_DB.parent)
    DEMO_DB.parent.mkdir(parents=True, exist_ok=True)

    config = load_config(REPO / "config")
    tz = load_zone(config.roster.timezone)
    day = date(2026, 9, 14)

    store = open_store(DEMO_DB, delivery_mode="live")
    clock = MovableClock(local_datetime_for(day, time(8, 55), tz))

    if args.send:
        transport = HermesSendTransport(profile=PROFILE)
        mode = "LIVE - real Telegram messages"
    else:
        transport = CaptureTransport()
        mode = "PREVIEW - nothing is sent"

    service = CareService(store=store, config=config, clock=clock)
    worker = DeliveryWorker(store=store, transport=transport, clock=clock)
    seen = 0

    banner(f"Family care-check agent - {mode}")
    print(f"  parent DM : {config.roster.parent.chat_id} ({config.roster.parent.name})")
    print(f"  group     : {config.roster.group.chat_id} ({config.roster.group.name})")
    print(f"  timezone  : {config.roster.timezone}   check-in {config.roster.checkin.time}")

    banner("SCENARIO 1  no reply all day -> family group is told")

    for label, wall in (
        ("09:00  check-in due", time(9, 0)),
        ("12:00  no reply for 3h -> nudge", time(12, 0)),
        ("15:00  still nothing for 6h -> escalate", time(15, 0)),
    ):
        clock.set(local_datetime_for(day, wall, tz))
        result = service.run_checkin_tick()
        worker.run_once()
        print(f"\n{label}")
        print(f"  actions: {', '.join(result.actions_taken) or 'none'}")
        seen = show_sent(transport, seen)
        episode = result.episode_id

    describe_outbox(store, episode)

    store.close()

    banner("SCENARIO 2  a concerning reply -> immediate escalation")

    store = open_store(DEMO_DB.parent / "care-state-2.db", delivery_mode="live")
    service = CareService(store=store, config=config, clock=clock)
    worker = DeliveryWorker(store=store, transport=transport, clock=clock)

    day2 = date(2026, 9, 15)
    clock.set(local_datetime_for(day2, time(9, 0), tz))
    result = service.run_checkin_tick()
    worker.run_once()
    print("\n09:00  check-in delivered")
    seen = show_sent(transport, seen)

    clock.set(local_datetime_for(day2, time(9, 12), tz))
    envelope = UpdateEnvelope(
        update_id=920001,
        chat_id=config.roster.parent.chat_id,
        chat_type="private",
        sender_id=config.roster.parent.chat_id,
        message_id=55001,
        reply_to_message_id=None,
        received_at_utc=clock.now_utc(),
        text="я впала у ванній і не можу встати",
    )
    outcome = service.handle_reply(envelope)
    worker.run_once()
    print('09:12  parent replies: "я впала у ванній і не можу встати"')
    print(f"  classified: {outcome.result.get('classification')}")
    seen = show_sent(transport, seen)

    describe_outbox(store, outcome.result["episode_id"])
    store.close()

    banner("SCENARIO 3  medication: one confirmed, one not")

    store = open_store(DEMO_DB.parent / "care-state-3.db", delivery_mode="live")
    service = CareService(store=store, config=config, clock=clock)
    worker = DeliveryWorker(store=store, transport=transport, clock=clock)
    day3 = date(2026, 9, 16)

    clock.set(local_datetime_for(day3, time(8, 30), tz))
    service.run_meds_tick()
    worker.run_once()
    print("\n08:30  morning tablet reminder")
    seen = show_sent(transport, seen)

    clock.set(local_datetime_for(day3, time(8, 41), tz))
    confirm = UpdateEnvelope(
        update_id=930001,
        chat_id=config.roster.parent.chat_id,
        chat_type="private",
        sender_id=config.roster.parent.chat_id,
        message_id=56001,
        reply_to_message_id=None,
        received_at_utc=clock.now_utc(),
        text="\u2705",
    )
    result = service.handle_reply(confirm)
    worker.run_once()
    print('08:41  parent replies with a tick')
    print(f"  dose outcome: {result.result.get('dose_kind')} ({result.result.get('dose_id')})")
    seen = show_sent(transport, seen)

    clock.set(local_datetime_for(day3, time(20, 0), tz))
    service.run_meds_tick()
    worker.run_once()
    print("20:00  evening pill reminder")
    seen = show_sent(transport, seen)

    clock.set(local_datetime_for(day3, time(20, 50), tz))
    service.run_meds_tick()
    worker.run_once()
    print("20:50  no confirmation after 45 min -> nudge")
    seen = show_sent(transport, seen)

    clock.set(local_datetime_for(day3, time(22, 10), tz))
    res = service.run_meds_tick()
    worker.run_once()
    print("22:10  still nothing after 2h -> closed as UNCONFIRMED, nothing sent")
    print(f"  actions: {', '.join(res.actions_taken) or 'none'}")
    seen = show_sent(transport, seen)

    print("  dose episode state:")
    for dose_id in ("bp_morning", "heart_evening"):
        episode = f"dose:{dose_id}:{day3.isoformat()}"
        for row in store.list_outbox(episode_id=episode):
            print(f"     {row.action_key:44} {row.status}")
        kinds = [e.kind for e in store.list_events(episode_id=episode)]
        print(f"     {dose_id:14} events: {', '.join(kinds)}")

    banner("Delivery truth")
    print("  Every line above was queued, then delivered, and the receipt recorded.")
    print("  A row existing is never treated as a message sent.")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
