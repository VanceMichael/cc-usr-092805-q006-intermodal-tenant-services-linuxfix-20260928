"""应用装配。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .audit import AuditLog
from .database import Database
from .idempotency import IdempotencyStore
from .inbox import Inbox
from .jobs import JobQueue
from .ledger import Ledger
from .park_billing import ParkBilling
from .park_holds import HoldBook
from .park_master import EffectiveStore, ParkMasterService
from .park_metering import MeteringService
from .park_ops import ParkOperations
from .outbox import Outbox
from .repository import EntityRepository
from .reservations import ReservationBook
from .timeutil import Clock


@dataclass(frozen=True)
class CivicFlow:
    database: Database
    clock: Clock
    repository: EntityRepository
    inbox: Inbox
    outbox: Outbox
    ledger: Ledger
    reservations: ReservationBook
    jobs: JobQueue
    park_master: ParkMasterService
    park_ops: ParkOperations
    park_metering: MeteringService
    park_billing: ParkBilling
    park_holds: HoldBook

    @classmethod
    def open(cls, path: str | Path, *, fixed_now: str | None = None) -> "CivicFlow":
        database = Database(path); database.initialize(); clock = Clock(fixed_now)
        audit = AuditLog(clock); idempotency = IdempotencyStore(clock)
        repository = EntityRepository(database, clock, audit, idempotency)
        inbox = Inbox(database, clock); outbox = Outbox(database, clock)
        ledger = Ledger(database, clock); reservations = ReservationBook(database); jobs = JobQueue(database, clock)
        effective = EffectiveStore(database, clock, audit, idempotency)
        park_master = ParkMasterService(effective, clock)
        park_ops = ParkOperations(database, clock, idempotency, audit)
        park_holds = HoldBook(database, clock)
        park_metering = MeteringService(database, clock, inbox, park_holds)
        park_billing = ParkBilling(database, clock, park_master, park_ops, park_metering, park_holds,
                                   jobs, outbox, idempotency)
        return cls(database, clock, repository, inbox, outbox, ledger, reservations, jobs,
                   park_master, park_ops, park_metering, park_billing, park_holds)

    def verify(self) -> dict:
        with self.database.connect() as connection:
            audit_count = AuditLog(self.clock).verify(connection)
            entity_count = connection.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
            conflict_count = connection.execute("SELECT COUNT(*) AS n FROM inbox_conflicts").fetchone()["n"]
        return {"audit_entries": audit_count, "entities": entity_count, "inbox_conflicts": conflict_count}
