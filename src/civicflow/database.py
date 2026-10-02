"""SQLite 连接、事务和数据库初始化。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = r"""
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS entities (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS entity_versions (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    PRIMARY KEY(entity_type, entity_id, version)
);
CREATE INDEX IF NOT EXISTS entity_versions_asof ON entity_versions(entity_type, entity_id, valid_from, version);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    request_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, request_key)
);
CREATE TABLE IF NOT EXISTS audit_entries (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    previous_digest TEXT NOT NULL,
    entry_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox_messages (
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    payload_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY(source, source_key, sequence)
);
CREATE TABLE IF NOT EXISTS inbox_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    existing_digest TEXT NOT NULL,
    incoming_digest TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox_messages (
    message_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    available_at TEXT NOT NULL,
    lease_until TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS outbox_ready ON outbox_messages(status, available_at, lease_until);
CREATE TABLE IF NOT EXISTS journal_entries (
    entry_id TEXT PRIMARY KEY,
    journal_key TEXT NOT NULL,
    account TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    direction TEXT NOT NULL,
    reference TEXT NOT NULL,
    reversed_entry_id TEXT,
    occurred_at TEXT NOT NULL,
    posted_by TEXT NOT NULL,
    FOREIGN KEY(reversed_entry_id) REFERENCES journal_entries(entry_id)
);
CREATE INDEX IF NOT EXISTS journal_reference ON journal_entries(journal_key, reference, occurred_at);
CREATE TABLE IF NOT EXISTS resource_reservations (
    reservation_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reservation_window ON resource_reservations(resource_id, start_at, end_at, status);
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    run_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_due ON scheduled_jobs(status, run_at, lease_until);
CREATE TABLE IF NOT EXISTS stl_tenants (
    tenant_id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    name TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stl_storage_units (
    unit_id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    unit_type TEXT NOT NULL,
    capacity_qty INTEGER NOT NULL,
    capacity_unit TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stl_acceptances (
    acceptance_id TEXT PRIMARY KEY,
    unit_id TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    result TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stl_lease_versions (
    lease_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    unit_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    signed_at TEXT NOT NULL,
    rate_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    billing_unit TEXT NOT NULL,
    supersedes INTEGER,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(lease_id, version)
);
CREATE INDEX IF NOT EXISTS lease_tenant_window ON stl_lease_versions(tenant_id, valid_from, valid_to, status);
CREATE INDEX IF NOT EXISTS lease_unit_window ON stl_lease_versions(unit_id, valid_from, valid_to, status);
CREATE TABLE IF NOT EXISTS stl_meters (
    meter_id TEXT PRIMARY KEY,
    meter_code TEXT NOT NULL UNIQUE,
    meter_kind TEXT NOT NULL,
    service_code TEXT NOT NULL DEFAULT '',
    unit_id TEXT,
    resource_id TEXT,
    unit_of_measure TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stl_meter_readings (
    reading_id TEXT PRIMARY KEY,
    meter_code TEXT NOT NULL,
    read_at TEXT NOT NULL,
    reading_value TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    dispute_key TEXT,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS reading_active_unique ON stl_meter_readings(meter_code, read_at, kind) WHERE status='recorded';
CREATE INDEX IF NOT EXISTS reading_window ON stl_meter_readings(meter_code, read_at);
CREATE TABLE IF NOT EXISTS stl_reading_disputes (
    dispute_id TEXT PRIMARY KEY,
    meter_code TEXT NOT NULL,
    read_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    existing_value TEXT NOT NULL,
    incoming_value TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS dispute_open ON stl_reading_disputes(meter_code, read_at, status);
CREATE TABLE IF NOT EXISTS stl_catalog (
    service_code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    service_kind TEXT NOT NULL,
    applies_to TEXT NOT NULL DEFAULT '',
    unit_of_measure TEXT NOT NULL,
    metered INTEGER NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stl_prices (
    price_id TEXT PRIMARY KEY,
    service_code TEXT NOT NULL,
    rate_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    unit_of_measure TEXT NOT NULL,
    charge_mode TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS price_service_window ON stl_prices(service_code, valid_from, valid_to, status);
CREATE TABLE IF NOT EXISTS stl_outages (
    outage_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    service_code TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT,
    reason TEXT NOT NULL,
    compensation_policy TEXT NOT NULL,
    job_id TEXT,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outage_target_window ON stl_outages(target_type, target_id, start_at, status);
CREATE TABLE IF NOT EXISTS stl_capacity_bookings (
    booking_id TEXT PRIMARY KEY,
    service_code TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    capacity_total INTEGER NOT NULL,
    reservation_id TEXT NOT NULL,
    organizer_tenant_id TEXT,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS booking_window ON stl_capacity_bookings(service_code, resource_id, start_at, end_at, status);
CREATE TABLE IF NOT EXISTS stl_booking_allocations (
    booking_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    declared_by TEXT NOT NULL,
    declared_at TEXT NOT NULL,
    PRIMARY KEY(booking_id, tenant_id)
);
CREATE TABLE IF NOT EXISTS stl_billing_periods (
    period_id TEXT PRIMARY KEY,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    closed_at TEXT,
    closed_by TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stl_charges (
    charge_id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    service_code TEXT NOT NULL,
    quantity TEXT NOT NULL,
    unit_of_measure TEXT NOT NULL,
    rate_minor INTEGER NOT NULL,
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    charge_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    price_id TEXT NOT NULL,
    lease_id TEXT,
    lease_version INTEGER,
    approval_id TEXT,
    parent_charge_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS charge_period_tenant ON stl_charges(period_id, tenant_id, status);
CREATE INDEX IF NOT EXISTS charge_source ON stl_charges(source_type, source_ref);
CREATE TABLE IF NOT EXISTS stl_charge_entries (
    charge_id TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY(charge_id, entry_id)
);
CREATE INDEX IF NOT EXISTS charge_entry_lookup ON stl_charge_entries(entry_id);
CREATE TABLE IF NOT EXISTS stl_responsibilities (
    responsibility_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    service_code TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    bearer TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS responsibility_lookup ON stl_responsibilities(target_type, target_id, service_code, tenant_id, valid_from);
CREATE TABLE IF NOT EXISTS stl_relief_requests (
    relief_id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    reason TEXT NOT NULL,
    charge_id TEXT,
    status TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    decision_note TEXT NOT NULL DEFAULT ''
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
