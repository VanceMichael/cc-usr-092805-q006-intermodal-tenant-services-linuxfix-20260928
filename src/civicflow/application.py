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
from .outbox import Outbox
from .repository import EntityRepository
from .reservations import ReservationBook
from .settlement import SettlementService
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
    settlement: SettlementService

    @classmethod
    def open(cls, path: str | Path, *, fixed_now: str | None = None) -> "CivicFlow":
        database = Database(path); database.initialize(); clock = Clock(fixed_now)
        audit = AuditLog(clock); idempotency = IdempotencyStore(clock)
        repository = EntityRepository(database, clock, audit, idempotency)
        inbox = Inbox(database, clock)
        outbox = Outbox(database, clock)
        ledger = Ledger(database, clock)
        reservations = ReservationBook(database)
        jobs = JobQueue(database, clock)
        settlement = SettlementService(database, clock, ledger, reservations, jobs, audit, inbox, outbox)
        return cls(database, clock, repository, inbox, outbox, ledger, reservations, jobs, settlement)

    def verify(self) -> dict:
        with self.database.connect() as connection:
            audit_count = AuditLog(self.clock).verify(connection)
            entity_count = connection.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
            conflict_count = connection.execute("SELECT COUNT(*) AS n FROM inbox_conflicts").fetchone()["n"]
            reading_disputes = connection.execute("SELECT COUNT(*) AS n FROM stl_reading_disputes WHERE status='open'").fetchone()["n"]
            open_periods = connection.execute("SELECT COUNT(*) AS n FROM stl_billing_periods WHERE status='open'").fetchone()["n"]
        return {"audit_entries": audit_count, "entities": entity_count, "inbox_conflicts": conflict_count,
                "open_reading_disputes": reading_disputes, "open_periods": open_periods}
