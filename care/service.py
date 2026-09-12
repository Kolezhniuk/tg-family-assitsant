from __future__ import annotations

from care import checkins
from care.clock import Clock
from care.config import Config
from care.models import UpdateEnvelope, UpdateOutcome
from care.store import Store, WriteTxn


class CareService:
    def __init__(self, *, store: Store, config: Config, clock: Clock):
        self._store = store
        self._config = config
        self._clock = clock

    def run_checkin_tick(self) -> checkins.CheckinTickResult:
        return checkins.run_checkin_tick(self._store, self._config, self._clock)

    def handle_reply(self, envelope: UpdateEnvelope) -> UpdateOutcome:
        def handler(txn: WriteTxn) -> dict:
            outcome = checkins.process_reply(self._store, txn, self._config, self._clock, envelope)
            return {
                "kind": outcome.kind,
                "episode_id": outcome.episode_id,
                "classification": outcome.classification,
            }

        return self._store.record_update(envelope, handler, processed_at_utc=self._clock.now_utc())
