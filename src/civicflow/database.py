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
-- 园区结算：带业务有效期的主数据版本（仓位、验收、租约、计量点、服务目录、价格、维保停机、承担关系）
CREATE TABLE IF NOT EXISTS park_effective_records (
    record_id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL,
    record_type TEXT NOT NULL,
    version INTEGER NOT NULL,
    tenant_org TEXT,
    payload_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    request_key TEXT NOT NULL,
    UNIQUE(record_type, series_id, version),
    UNIQUE(record_type, request_key)
);
CREATE INDEX IF NOT EXISTS park_eff_type_window ON park_effective_records(record_type, valid_from, valid_to, status);
CREATE INDEX IF NOT EXISTS park_eff_tenant ON park_effective_records(tenant_org, record_type, valid_from);
-- 园区结算：账期
CREATE TABLE IF NOT EXISTS park_periods (
    period_id TEXT PRIMARY KEY,
    tenant_org TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS park_period_tenant ON park_periods(tenant_org, period_start, status);
-- 园区结算：作业用量（预约确认后按租户拆分的实际数量）
CREATE TABLE IF NOT EXISTS park_usage (
    usage_id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    tenant_org TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS park_usage_window ON park_usage(tenant_org, start_at, end_at, status);
CREATE INDEX IF NOT EXISTS park_usage_resource ON park_usage(resource_id, start_at, end_at, status);
-- 园区结算：设备原始读数（经事件收件箱接入，编号唯一）
CREATE TABLE IF NOT EXISTS park_meter_readings (
    reading_id TEXT PRIMARY KEY,
    meter_code TEXT NOT NULL UNIQUE,
    meter_point_id TEXT NOT NULL,
    read_value TEXT NOT NULL,
    read_at TEXT NOT NULL,
    source TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    status TEXT NOT NULL,
    adjudicated_value TEXT,
    adjudicated_by TEXT
);
CREATE INDEX IF NOT EXISTS park_reading_point ON park_meter_readings(meter_point_id, read_at, status);
-- 园区结算：暂停结算挂起项（读号冲突、缺抄表、维保补偿）
CREATE TABLE IF NOT EXISTS park_holds (
    hold_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    ref_key TEXT NOT NULL,
    tenant_org TEXT,
    period_id TEXT,
    detail_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS park_holds_status ON park_holds(status, kind, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS park_holds_open_key ON park_holds(kind, ref_key) WHERE status='open';
-- 园区结算：费用行（每笔费用可下钻到占用、读数、价目、批准）
CREATE TABLE IF NOT EXISTS park_charge_lines (
    line_id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL,
    line_key TEXT NOT NULL,
    tenant_org TEXT NOT NULL,
    service_code TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    basis TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reversed_line_id TEXT,
    approval_id TEXT,
    FOREIGN KEY(period_id) REFERENCES park_periods(period_id),
    FOREIGN KEY(reversed_line_id) REFERENCES park_charge_lines(line_id),
    UNIQUE(period_id, line_key)
);
CREATE INDEX IF NOT EXISTS park_lines_period ON park_charge_lines(period_id, tenant_org, status);
-- 园区结算：减免/调整申请与审批（录入人不能批准自己的申请）
CREATE TABLE IF NOT EXISTS park_adjustments (
    adjustment_id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL,
    tenant_org TEXT NOT NULL,
    kind TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    reviewed_by TEXT,
    line_id TEXT,
    hold_ref TEXT,
    request_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(request_key)
);
CREATE INDEX IF NOT EXISTS park_adjustments_status ON park_adjustments(status, tenant_org, created_at);
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
