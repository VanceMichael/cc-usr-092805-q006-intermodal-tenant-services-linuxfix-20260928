"""园区主数据：仓位单元、交付验收、租约版本、设备计量点、服务目录、价格规则、维保停机、费用承担关系。

这些记录都带业务有效期（valid_from/valid_to，左闭右开），存储在
``park_effective_records``。后来签署的变更形成新版本：新版本从未来的
valid_from 生效，并把先前与之重叠的开放版本收口到该时点，因此历史账期
永远按当时有效的版本计价。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .audit import AuditLog
from .database import Database
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .identifiers import new_id, require_safe
from .idempotency import IdempotencyStore
from .jsonutil import canonical_json
from .security import AccessContext
from .timeutil import Clock, canonical_instant, parse_instant


# record_type -> 必填载荷字段
RECORD_FIELDS: dict[str, tuple[str, ...]] = {
    "storage_units": ("code", "unit_type", "area_sqm", "location"),
    "deliveries": ("unit_code", "accepted_at", "inspector", "result"),
    "leases": ("unit_code", "tenant_org", "currency", "rent_minor"),
    "meter_points": ("code", "service_code", "unit_of_measure", "resource_id"),
    "service_catalog": ("service_code", "name", "unit", "metered"),
    "price_rules": ("service_code", "price_minor", "mode", "unit"),
    "maintenance": ("resource_id", "service_code", "reason", "comp_minor"),
    "cost_allocations": ("tenant_org", "unit_code", "service_code", "share"),
}
RECORD_TYPES = tuple(RECORD_FIELDS)
ACTIVE_STATUS = "active"


def tenant_scope(context: AccessContext, tenant_org: str | None) -> None:
    """租户上下文只能访问本企业的数据；系统/园区运营方不受限。"""
    if tenant_org is None:
        return
    if context.has_scope("*"):
        return
    if not context.has_scope(f"org:{tenant_org}"):
        raise PermissionDenied("不能越过本企业边界查询")


@dataclass(frozen=True)
class EffectiveStore:
    """有效期版本记录的通用存取。"""

    database: Database
    clock: Clock
    audit: AuditLog
    idempotency: IdempotencyStore

    def publish(self, record_type: str, payload: dict, *, valid_from: str, valid_to: str | None,
                actor: str, request_key: str, series_id: str | None = None,
                tenant_org: str | None = None, clip_predecessors: bool = True,
                future_only: bool = False, series_must_exist: bool = False) -> dict:
        require_safe(record_type, "记录类型")
        valid_from = canonical_instant(valid_from)
        valid_to = canonical_instant(valid_to) if valid_to is not None else None
        if valid_to is not None and parse_instant(valid_to) <= parse_instant(valid_from):
            raise ValidationError("有效期结束必须晚于开始")
        if future_only and parse_instant(valid_from) < parse_instant(self.clock.now()):
            raise ValidationError("后签变更只能从当前时间之后生效，历史差错须通过贷项或补单纠正")
        with self.database.transaction() as connection:
            def operation() -> dict:
                series = series_id or new_id(f"{record_type}-series")
                if series_must_exist:
                    exists = connection.execute("SELECT 1 FROM park_effective_records WHERE record_type=? AND series_id=? LIMIT 1", (record_type, series)).fetchone()
                    if not exists:
                        raise NotFoundError(f"{record_type} 版本序列 {series} 不存在")
                last = connection.execute("SELECT COALESCE(MAX(version),0) AS v FROM park_effective_records WHERE record_type=? AND series_id=?", (record_type, series)).fetchone()
                version = int(last["v"]) + 1
                prev = connection.execute("SELECT valid_from FROM park_effective_records WHERE record_type=? AND series_id=? ORDER BY version DESC LIMIT 1", (record_type, series)).fetchone()
                if prev and parse_instant(valid_from) < parse_instant(prev["valid_from"]):
                    raise ConflictError("新版本生效时间不能早于上一版本")
                # 已经存在晚于新起点的版本时不允许插入（不能在未来版本之前插段）
                future = connection.execute("SELECT 1 FROM park_effective_records WHERE record_type=? AND series_id=? AND valid_from>=? AND version<?", (record_type, series, valid_from, version)).fetchone()
                if future:
                    raise ConflictError("该有效期之后已经存在新版本")
                now = self.clock.now()
                record_id = new_id(f"{record_type.rstrip('s')}")
                connection.execute(
                    "INSERT INTO park_effective_records(record_id,series_id,record_type,version,tenant_org,payload_json,valid_from,valid_to,status,created_at,created_by,request_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (record_id, series, record_type, version, tenant_org, canonical_json(payload), valid_from, valid_to, ACTIVE_STATUS, now, actor, request_key))
                if clip_predecessors:
                    # 把与新版本重叠的旧版本收口到新起点（开放版本或结束更晚的版本）
                    connection.execute(
                        "UPDATE park_effective_records SET valid_to=? WHERE record_type=? AND series_id=? AND valid_from<? AND version<? AND (valid_to IS NULL OR valid_to>?)",
                        (valid_from, record_type, series, valid_from, version, valid_from))
                self.audit.append(connection, actor_id=actor, action="publish", entity_type=record_type, entity_id=record_id, version=version,
                                  detail={"series_id": series, "valid_from": valid_from, "valid_to": valid_to, "payload": payload})
                return self._row(connection.execute("SELECT * FROM park_effective_records WHERE record_id=?", (record_id,)).fetchone())
            return self.idempotency.execute(connection, scope=f"park-publish:{record_type}", request_key=request_key,
                                            request={"payload": payload, "valid_from": valid_from, "valid_to": valid_to, "series_id": series_id},
                                            operation=operation)

    def get(self, record_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM park_effective_records WHERE record_id=?", (record_id,)).fetchone()
            if not row:
                raise NotFoundError(f"有效期记录 {record_id} 不存在")
            return self._row(row)

    def series_versions(self, record_type: str, series_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM park_effective_records WHERE record_type=? AND series_id=? ORDER BY version", (record_type, series_id)).fetchall()
            if not rows:
                raise NotFoundError(f"{record_type}/{series_id} 没有版本")
            return [self._row(r) for r in rows]

    def active_at(self, record_type: str, instant: str, *, tenant_org: str | None = None, **filters: object) -> list[dict]:
        point = canonical_instant(instant)
        sql = "SELECT * FROM park_effective_records WHERE record_type=? AND status=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>?)"
        params: list[object] = [record_type, ACTIVE_STATUS, point, point]
        if tenant_org is not None:
            sql += " AND tenant_org=?"; params.append(tenant_org)
        with self.database.connect() as connection:
            rows = self._filter(connection, sql, params, filters)
        return [self._row(r) for r in rows]

    def active_during(self, record_type: str, start_at: str, end_at: str, *, tenant_org: str | None = None, **filters: object) -> list[dict]:
        start_at = canonical_instant(start_at); end_at = canonical_instant(end_at)
        sql = "SELECT * FROM park_effective_records WHERE record_type=? AND status=? AND valid_from<? AND (valid_to IS NULL OR valid_to>?)"
        params: list[object] = [record_type, ACTIVE_STATUS, end_at, start_at]
        if tenant_org is not None:
            sql += " AND tenant_org=?"; params.append(tenant_org)
        with self.database.connect() as connection:
            rows = self._filter(connection, sql, params, filters)
        return [self._row(r) for r in rows]

    def list_type(self, record_type: str, *, limit: int = 500, **filters: object) -> list[dict]:
        sql = "SELECT * FROM park_effective_records WHERE record_type=?"
        params: list[object] = [record_type]
        with self.database.connect() as connection:
            rows = self._filter(connection, sql, params, filters)
        return [self._row(r) for r in rows[:limit]]

    def _filter(self, connection: sqlite3.Connection, sql: str, params: list[object], filters: dict[str, object]) -> list[sqlite3.Row]:
        for key, value in filters.items():
            if value is None:
                continue
            sql += f" AND json_extract(payload_json,'$.{key}')=?"; params.append(value)
        sql += " ORDER BY valid_from, record_id"
        return connection.execute(sql, params).fetchall()

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        payload = json.loads(row["payload_json"])
        payload.update({"record_id": row["record_id"], "series_id": row["series_id"], "record_type": row["record_type"],
                        "version": row["version"], "valid_from": row["valid_from"], "valid_to": row["valid_to"],
                        "status": row["status"], "created_at": row["created_at"], "created_by": row["created_by"]})
        return payload


@dataclass(frozen=True)
class ParkMasterService:
    """园区主数据写入与查询入口。"""

    store: EffectiveStore
    clock: Clock

    # ---- 通用写入 ----
    def _publish(self, context: AccessContext, record_type: str, values: dict, *, valid_from: str,
                 valid_to: str | None, request_key: str, series_id: str | None = None,
                 clip_predecessors: bool = True, future_only: bool = False,
                 series_must_exist: bool = False) -> dict:
        context.require("write:park-master")
        payload = self._validate(record_type, values)
        tenant_org = payload.get("tenant_org")
        return self.store.publish(record_type, payload, valid_from=valid_from, valid_to=valid_to,
                                  actor=context.actor_id, request_key=request_key, series_id=series_id,
                                  tenant_org=tenant_org, clip_predecessors=clip_predecessors, future_only=future_only,
                                  series_must_exist=series_must_exist)

    # ---- 各类主数据 ----
    def register_unit(self, context: AccessContext, values: dict, *, valid_from: str, request_key: str, valid_to: str | None = None) -> dict:
        return self._publish(context, "storage_units", values, valid_from=valid_from, valid_to=valid_to, request_key=request_key, clip_predecessors=False)

    def accept_delivery(self, context: AccessContext, values: dict, *, valid_from: str, request_key: str) -> dict:
        return self._publish(context, "deliveries", values, valid_from=valid_from, valid_to=None, request_key=request_key, clip_predecessors=False)

    def sign_lease(self, context: AccessContext, values: dict, *, valid_from: str, valid_to: str, request_key: str, jobs=None, lead_days: int = 7) -> dict:
        record = self._publish(context, "leases", values, valid_from=valid_from, valid_to=valid_to, request_key=request_key, clip_predecessors=False)
        if jobs is not None:
            from datetime import timedelta
            run_at = (parse_instant(canonical_instant(valid_to)) - timedelta(days=lead_days)).isoformat().replace("+00:00", "Z")
            jobs.schedule(job_type="park.lease_expiring", subject_id=record["series_id"], run_at=run_at,
                          payload={"lease_series_id": record["series_id"], "unit_code": values["unit_code"],
                                   "tenant_org": values["tenant_org"], "expires_at": canonical_instant(valid_to)})
        return record

    def amend_lease(self, context: AccessContext, lease_series_id: str, values: dict, *, valid_from: str, valid_to: str | None, request_key: str) -> dict:
        """后签租约变更：只影响 valid_from 之后的账单。"""
        return self._publish(context, "leases", values, valid_from=valid_from, valid_to=valid_to,
                             request_key=request_key, series_id=lease_series_id,
                             clip_predecessors=True, future_only=True, series_must_exist=True)

    def register_meter_point(self, context: AccessContext, values: dict, *, valid_from: str, request_key: str) -> dict:
        return self._publish(context, "meter_points", values, valid_from=valid_from, valid_to=None, request_key=request_key, clip_predecessors=False)

    def add_catalog_service(self, context: AccessContext, values: dict, *, valid_from: str, request_key: str) -> dict:
        return self._publish(context, "service_catalog", values, valid_from=valid_from, valid_to=None, request_key=request_key, clip_predecessors=False)

    def add_price_rule(self, context: AccessContext, values: dict, *, valid_from: str, request_key: str, valid_to: str | None = None) -> dict:
        """新价格规则按 service_code 形成版本序列，旧价自动收口。

        首个价格允许回溯到服务启用时点；已有价格之后的调整只能在未来生效，
        以保证历史账期始终按当时价格结算。
        """
        self._validate("price_rules", values)
        series_id = f"price:{values['service_code']}"
        with self.store.database.connect() as connection:
            exists = connection.execute("SELECT 1 FROM park_effective_records WHERE record_type='price_rules' AND series_id=? LIMIT 1", (series_id,)).fetchone()
        return self._publish(context, "price_rules", values, valid_from=valid_from, valid_to=valid_to,
                             request_key=request_key, series_id=series_id,
                             clip_predecessors=True, future_only=bool(exists))

    def register_maintenance(self, context: AccessContext, values: dict, *, valid_from: str, valid_to: str, request_key: str, jobs=None, outbox=None) -> dict:
        """登记维保停机。停机结束时由可恢复任务为受影响租户生成维保补偿挂起项。"""
        record = self._publish(context, "maintenance", values, valid_from=valid_from, valid_to=valid_to, request_key=request_key, clip_predecessors=False)
        start_at = canonical_instant(valid_from); end_at = canonical_instant(valid_to)
        affected_tenants: list[str] = []
        with self.store.database.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT tenant_org FROM park_usage WHERE status='confirmed' AND resource_id=? AND start_at<? AND end_at>?",
                (values["resource_id"], end_at, start_at)).fetchall()
            affected_tenants = sorted(r["tenant_org"] for r in rows)
        if jobs is not None:
            jobs.schedule(job_type="park.maintenance_comp", subject_id=record["record_id"], run_at=end_at,
                          payload={"resource_id": values["resource_id"], "service_code": values["service_code"],
                                   "comp_minor": int(values["comp_minor"]), "start_at": start_at, "end_at": end_at,
                                   "tenants": affected_tenants})
        if outbox is not None:
            outbox.enqueue(topic="park.maintenance.registered", aggregate_id=record["record_id"],
                           payload={"resource_id": values["resource_id"], "start_at": start_at, "end_at": end_at,
                                    "affected_tenants": affected_tenants})
        return record

    def set_allocation(self, context: AccessContext, values: dict, *, valid_from: str, request_key: str) -> dict:
        return self._publish(context, "cost_allocations", values, valid_from=valid_from, valid_to=None, request_key=request_key)

    # ---- 查询 ----
    def get_record(self, context: AccessContext, record_id: str) -> dict:
        context.require("read:park-master")
        record = self.store.get(record_id)
        tenant_scope(context, record.get("tenant_org"))
        return record

    def series_history(self, context: AccessContext, record_type: str, series_id: str) -> list[dict]:
        context.require("history:park-master")
        if record_type not in RECORD_TYPES:
            raise ValidationError("未知主数据类型")
        rows = self.store.series_versions(record_type, series_id)
        for row in rows:
            tenant_scope(context, row.get("tenant_org"))
        return rows

    def list_active(self, context: AccessContext, record_type: str, at: str, *, tenant_org: str | None = None, **filters) -> list[dict]:
        context.require("read:park-master")
        if record_type not in RECORD_TYPES:
            raise ValidationError("未知主数据类型")
        scoped_tenant = tenant_org
        if not context.has_scope("*"):
            orgs = {s[4:] for s in context.scopes if s.startswith("org:")}
            if tenant_org is not None and tenant_org not in orgs:
                raise PermissionDenied("不能越过本企业边界查询")
            scoped_tenant = tenant_org  # None 时下面逐条过滤
        rows = self.store.active_at(record_type, at, tenant_org=scoped_tenant, **filters)
        if scoped_tenant is None and not context.has_scope("*"):
            orgs = {s[4:] for s in context.scopes if s.startswith("org:")}
            rows = [r for r in rows if r.get("tenant_org") in orgs]
        return rows

    def _validate(self, record_type: str, values: dict) -> dict:
        if record_type not in RECORD_FIELDS:
            raise ValidationError("未知主数据类型")
        unknown = set(values) - set(RECORD_FIELDS[record_type])
        if unknown:
            raise ValidationError("未知字段: " + ", ".join(sorted(unknown)))
        missing = [f for f in RECORD_FIELDS[record_type] if f not in values]
        if missing:
            raise ValidationError("缺少字段: " + ", ".join(missing))
        payload = dict(values)
        for key, value in payload.items():
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ValidationError(f"{key} 不能为空")
            if isinstance(value, str):
                payload[key] = value.strip()
        for int_field in ("rent_minor", "price_minor", "comp_minor", "area_sqm"):
            if int_field in payload:
                payload[int_field] = int(payload[int_field])
                if payload[int_field] < 0:
                    raise ValidationError(f"{int_field} 不能为负")
        if "share" in payload:
            share = float(payload["share"])
            if not 0 < share <= 1:
                raise ValidationError("承担比例必须在 0 到 1 之间")
            payload["share"] = str(share)
        if "mode" in payload and payload["mode"] not in {"hour", "meter", "quantity"}:
            raise ValidationError("计价模式必须是 hour、meter 或 quantity")
        if "metered" in payload:
            payload["metered"] = bool(payload["metered"])
        return payload
