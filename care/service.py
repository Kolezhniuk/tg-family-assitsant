from __future__ import annotations

from care import checkins, meds
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

    def run_meds_tick(self) -> meds.MedsTickResult:
        return meds.run_meds_tick(self._store, self._config, self._clock)

    def handle_reply(self, envelope: UpdateEnvelope) -> UpdateOutcome:
        def handler(txn: WriteTxn) -> dict:
            checkin_outcome = checkins.process_reply(self._store, txn, self._config, self._clock, envelope)
            dose_outcome = meds.process_reply(self._store, txn, self._config, self._clock, envelope)
            return {
                "kind": checkin_outcome.kind,
                "episode_id": checkin_outcome.episode_id,
                "classification": checkin_outcome.classification,
                "dose_kind": dose_outcome.kind,
                "dose_episode_id": dose_outcome.episode_id,
                "dose_id": dose_outcome.dose_id,
                "dose_reason": dose_outcome.reason,
            }

        return self._store.record_update(envelope, handler, processed_at_utc=self._clock.now_utc())
