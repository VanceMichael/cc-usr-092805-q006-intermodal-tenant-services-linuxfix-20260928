"""园区服务结算：从仓位租约到月台、冷库、充电与铁路接驳费用的完整结算依据。

所有主数据（仓位、租约版本、计量点、服务目录、价格、维保停机、费用承担）
都带有效期间；账期出账时按费用发生时点取 as-of 版本，后来签署的变更不会
改写历史账单，历史差错只能通过贷项或补单纠正。

关键规则：
- 冷库停机区间按时间扣减，运营方承担区间不计租户费用；
- 月台、冷库、充电、铁路接驳按能力预约，拼单作业必须逐户拆清实际数量；
- 设备故障只释放受影响的那一笔服务安排；
- 计量编号重复到达不重复入账，不同读数挂争议并暂停结算；
- 减免申请人不能批准自己的申请；
- 缺抄表、维保补偿、租约到期都是持久化定时任务，进程重启后仍按原期限出现。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from .audit import AuditLog
from .database import Database
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .identifiers import new_id, require_safe
from .inbox import Inbox
from .jobs import JobQueue
from .jsonutil import canonical_json
from .ledger import Ledger
from .outbox import Outbox
from .reservations import ReservationBook
from .security import AccessContext, assert_distinct
from .timeutil import Clock, canonical_instant, parse_instant

D = Decimal
HOUR = D(3600)
DAY = D(24)
MONTH_HOURS = D(730)
OPEN_ENDED = "9999-12-31T23:59:59Z"
SERVICE_KINDS = {"storage", "refrigeration", "dock", "charging", "rail"}
CHARGE_MODES = {"time", "usage"}
BEARERS = {"tenant", "operator"}


# --------------------------------------------------------------------------- 区间工具

def _dt(value: str):
    return parse_instant(value)


def _iso(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _intersection(s1, e1, s2, e2) -> tuple[str, str] | None:
    start = max(_dt(s1), _dt(s2)); end = min(_dt(e1), _dt(e2))
    if start >= end:
        return None
    return _iso(start), _iso(end)


def _hours(start: str, end: str) -> D:
    return D((_dt(end) - _dt(start)).total_seconds()) / HOUR


def _open_end(valid_to: str | None) -> str:
    return valid_to or OPEN_ENDED


def _money(value: D) -> int:
    return int(value.quantize(D("1"), rounding=ROUND_HALF_EVEN))


# --------------------------------------------------------------------------- 服务

@dataclass(frozen=True)
class SettlementService:
    database: Database
    clock: Clock
    ledger: Ledger
    reservations: ReservationBook
    jobs: JobQueue
    audit: AuditLog
    inbox: Inbox
    outbox: Outbox

    # ------------------------------------------------------------------ 权限

    @staticmethod
    def _assert_tenant(context: AccessContext, tenant_id: str) -> None:
        if context.allows("*"):
            return
        if context.tenant_id != tenant_id:
            raise PermissionDenied("租户查询不得越过本企业边界")

    # ------------------------------------------------------------------ 主数据

    def register_tenant(self, context: AccessContext, *, tenant_id: str, org_id: str, name: str, valid_from: str) -> dict:
        context.require("settle:admin")
        require_safe(tenant_id, "租户标识"); require_safe(org_id, "机构标识")
        valid_from = canonical_instant(valid_from)
        with self.database.transaction() as c:
            self._close_overlapping(c, "stl_tenants", "tenant_id", tenant_id, valid_from)
            c.execute("INSERT INTO stl_tenants(tenant_id,org_id,name,valid_from,status,created_by,created_at) VALUES(?,?,?,?,'active',?,?)",
                      (tenant_id, org_id, name, valid_from, context.actor_id, self.clock.now()))
            self._audit(c, context, "register-tenant", tenant_id, {"valid_from": valid_from})
            return self._get(c, "SELECT * FROM stl_tenants WHERE tenant_id=? AND valid_from=?", (tenant_id, valid_from))

    def register_unit(self, context: AccessContext, *, unit_id: str, code: str, unit_type: str, capacity_qty: int, capacity_unit: str, valid_from: str) -> dict:
        context.require("settle:admin")
        require_safe(unit_id, "仓位标识")
        if capacity_qty <= 0:
            raise ValidationError("仓位容量必须大于零")
        valid_from = canonical_instant(valid_from)
        with self.database.transaction() as c:
            self._close_overlapping(c, "stl_storage_units", "unit_id", unit_id, valid_from)
            c.execute("INSERT INTO stl_storage_units(unit_id,code,unit_type,capacity_qty,capacity_unit,valid_from,status,created_by,created_at) VALUES(?,?,?,?,?,?,'active',?,?)",
                      (unit_id, code, unit_type, capacity_qty, capacity_unit, valid_from, context.actor_id, self.clock.now()))
            self._audit(c, context, "register-unit", unit_id, {"code": code, "valid_from": valid_from})
            return self._get(c, "SELECT * FROM stl_storage_units WHERE unit_id=? AND valid_from=?", (unit_id, valid_from))

    def register_acceptance(self, context: AccessContext, *, unit_id: str, delivered_at: str, accepted_at: str, result: str, note: str = "", valid_from: str | None = None) -> dict:
        """交付验收同样带有效期间；关账下钻时作为租约起算的交付依据。"""
        context.require("settle:admin")
        delivered_at = canonical_instant(delivered_at); accepted_at = canonical_instant(accepted_at)
        valid_from = canonical_instant(valid_from or accepted_at)
        if _dt(accepted_at) < _dt(delivered_at):
            raise ValidationError("验收时间不能早于交付时间")
        acceptance_id = new_id("acceptance")
        with self.database.transaction() as c:
            if not c.execute("SELECT 1 FROM stl_storage_units WHERE unit_id=? AND ?>=valid_from AND ?<COALESCE(valid_to,?)", (unit_id, valid_from, valid_from, OPEN_ENDED)).fetchone():
                raise NotFoundError("验收时仓位单元不存在或未生效")
            c.execute("INSERT INTO stl_acceptances(acceptance_id,unit_id,delivered_at,accepted_at,result,note,valid_from,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (acceptance_id, unit_id, delivered_at, accepted_at, result, note, valid_from, context.actor_id, self.clock.now()))
            self._audit(c, context, "register-acceptance", acceptance_id, {"unit_id": unit_id, "result": result})
            return self._get(c, "SELECT * FROM stl_acceptances WHERE acceptance_id=?", (acceptance_id,))

    def register_meter(self, context: AccessContext, *, meter_code: str, meter_kind: str, unit_of_measure: str, valid_from: str, unit_id: str | None = None, resource_id: str | None = None, service_code: str = "") -> dict:
        context.require("settle:admin")
        require_safe(meter_code, "计量编号")
        if not unit_id and not resource_id:
            raise ValidationError("计量点必须绑定仓位单元或能力资源")
        valid_from = canonical_instant(valid_from)
        meter_id = new_id("meter")
        with self.database.transaction() as c:
            c.execute("INSERT INTO stl_meters(meter_id,meter_code,meter_kind,service_code,unit_id,resource_id,unit_of_measure,valid_from,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,'active',?,?)",
                      (meter_id, meter_code, meter_kind, service_code, unit_id, resource_id, unit_of_measure, valid_from, context.actor_id, self.clock.now()))
            self._audit(c, context, "register-meter", meter_id, {"meter_code": meter_code, "valid_from": valid_from})
            return self._get(c, "SELECT * FROM stl_meters WHERE meter_id=?", (meter_id,))

    def register_service(self, context: AccessContext, *, service_code: str, name: str, service_kind: str, unit_of_measure: str, metered: bool, applies_to: str = "unit", valid_from: str | None = None) -> dict:
        context.require("settle:admin")
        require_safe(service_code, "服务编号")
        if service_kind not in SERVICE_KINDS:
            raise ValidationError(f"服务类型必须是 {sorted(SERVICE_KINDS)}")
        valid_from = canonical_instant(valid_from or self.clock.now())
        with self.database.transaction() as c:
            self._close_overlapping(c, "stl_catalog", "service_code", service_code, valid_from)
            c.execute("INSERT INTO stl_catalog(service_code,name,service_kind,applies_to,unit_of_measure,metered,valid_from,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,'active',?,?)",
                      (service_code, name, service_kind, applies_to, unit_of_measure, 1 if metered else 0, valid_from, context.actor_id, self.clock.now()))
            self._audit(c, context, "register-service", service_code, {"kind": service_kind, "valid_from": valid_from})
            return self._get(c, "SELECT * FROM stl_catalog WHERE service_code=? AND valid_from=?", (service_code, valid_from))

    def register_price(self, context: AccessContext, *, service_code: str, rate_minor: int, currency: str, unit_of_measure: str, charge_mode: str, valid_from: str) -> dict:
        """价格规则带有效期间；出账按费用窗口落在哪个价格版本就用哪个版本。"""
        context.require("settle:admin")
        require_safe(service_code, "服务编号")
        if charge_mode not in CHARGE_MODES:
            raise ValidationError(f"计价方式必须是 {sorted(CHARGE_MODES)}")
        if rate_minor < 0:
            raise ValidationError("单价不能为负")
        valid_from = canonical_instant(valid_from)
        price_id = new_id("price")
        with self.database.transaction() as c:
            if not c.execute("SELECT 1 FROM stl_catalog WHERE service_code=? AND ?>=valid_from AND ?<COALESCE(valid_to,?) AND status='active'", (service_code, valid_from, valid_from, OPEN_ENDED)).fetchone():
                raise NotFoundError("价格生效时服务目录不存在或已失效")
            self._close_overlapping(c, "stl_prices", "service_code", service_code, valid_from)
            c.execute("INSERT INTO stl_prices(price_id,service_code,rate_minor,currency,unit_of_measure,charge_mode,valid_from,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,'active',?,?)",
                      (price_id, service_code, rate_minor, currency, unit_of_measure, charge_mode, valid_from, context.actor_id, self.clock.now()))
            self._audit(c, context, "register-price", price_id, {"service_code": service_code, "rate_minor": rate_minor, "valid_from": valid_from})
            return self._get(c, "SELECT * FROM stl_prices WHERE price_id=?", (price_id,))

    def register_responsibility(self, context: AccessContext, *, target_type: str, target_id: str, service_code: str, tenant_id: str, bearer: str, valid_from: str, valid_to: str | None = None) -> dict:
        """登记费用承担关系（带有效期），例如维保期间费用转由运营方承担。"""
        context.require("settle:admin")
        if bearer not in BEARERS:
            raise ValidationError(f"承担方必须是 {sorted(BEARERS)}")
        valid_from = canonical_instant(valid_from)
        valid_to = canonical_instant(valid_to) if valid_to else None
        responsibility_id = new_id("resp")
        with self.database.transaction() as c:
            c.execute(
                "INSERT INTO stl_responsibilities(responsibility_id,target_type,target_id,service_code,tenant_id,bearer,valid_from,valid_to,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,'active',?,?)",
                (responsibility_id, target_type, target_id, service_code, tenant_id, bearer, valid_from, valid_to, context.actor_id, self.clock.now()))
            self._audit(c, context, "register-responsibility", responsibility_id, {"bearer": bearer, "valid_from": valid_from})
            return self._get(c, "SELECT * FROM stl_responsibilities WHERE responsibility_id=?", (responsibility_id,))

    @staticmethod
    def _close_overlapping(c, table: str, id_column: str, entity_id: str, valid_from: str) -> None:
        c.execute(
            f"UPDATE {table} SET valid_to=?, status='superseded' WHERE {id_column}=? AND valid_to IS NULL AND valid_from<?",
            (valid_from, entity_id, valid_from))

    # ------------------------------------------------------------------ 租约版本

    def sign_lease(self, context: AccessContext, *, lease_id: str, unit_id: str, tenant_id: str, valid_from: str, valid_to: str | None, rate_minor: int, currency: str, billing_unit: str, signed_at: str | None = None, supersedes: int | None = None) -> dict:
        """签署租约或租约变更；变更版本从 valid_from 起生效，只影响之后的账单。"""
        context.require("settle:admin")
        require_safe(lease_id, "租约标识")
        if billing_unit not in {"day", "month"}:
            raise ValidationError("租约计价单位必须是 day 或 month")
        if rate_minor < 0:
            raise ValidationError("租金不能为负")
        valid_from = canonical_instant(valid_from)
        valid_to = canonical_instant(valid_to) if valid_to else None
        signed_at = canonical_instant(signed_at or self.clock.now())
        if valid_to and _dt(valid_to) <= _dt(valid_from):
            raise ValidationError("租约结束时间必须晚于开始时间")
        with self.database.transaction() as c:
            latest = c.execute("SELECT MAX(version) AS v FROM stl_lease_versions WHERE lease_id=?", (lease_id,)).fetchone()
            version = (latest["v"] or 0) + 1
            if version > 1 and supersedes != version - 1:
                raise ConflictError("租约变更必须显式接替最新版本")
            if version > 1:
                changed = c.execute("UPDATE stl_lease_versions SET valid_to=? WHERE lease_id=? AND version=? AND valid_to IS NULL", (valid_from, lease_id, version - 1)).rowcount
                if changed != 1:
                    raise ConflictError("被接替的租约版本已经关闭，不能再次变更")
            c.execute(
                "INSERT INTO stl_lease_versions(lease_id,version,unit_id,tenant_id,valid_from,valid_to,signed_at,rate_minor,currency,billing_unit,supersedes,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'active',?,?)",
                (lease_id, version, unit_id, tenant_id, valid_from, valid_to, signed_at, rate_minor, currency, billing_unit, supersedes, context.actor_id, self.clock.now()))
            # 即将到期租约：持久化提醒任务，进程重启后仍按原期限出现
            if valid_to:
                self.jobs.schedule_named(f"lease-expiry:{lease_id}:v{version}", job_type="lease-expiry", subject_id=lease_id, run_at=valid_to,
                                         payload={"lease_id": lease_id, "version": version, "valid_to": valid_to, "tenant_id": tenant_id}, connection=c)
            self._audit(c, context, "sign-lease", f"{lease_id}#v{version}", {"unit_id": unit_id, "tenant_id": tenant_id, "valid_from": valid_from})
            return self._get(c, "SELECT * FROM stl_lease_versions WHERE lease_id=? AND version=?", (lease_id, version))

    def lease_as_of(self, context: AccessContext, lease_id: str, *, at: str) -> dict:
        context.require("settle:read")
        with self.database.connect() as c:
            row = c.execute("SELECT * FROM stl_lease_versions WHERE lease_id=? AND valid_from<=? ORDER BY valid_from DESC,version DESC LIMIT 1", (lease_id, canonical_instant(at))).fetchone()
            if not row:
                raise NotFoundError("该时点没有生效租约")
            return dict(row)

    @staticmethod
    def _active_leases(c, start: str, end: str) -> list[dict]:
        return [dict(r) for r in c.execute(
            "SELECT * FROM stl_lease_versions WHERE status='active' AND valid_from<? AND COALESCE(valid_to,?)>?", (end, OPEN_ENDED, start))]

    @staticmethod
    def _lease_for_unit_at(c, unit_id: str, at: str) -> dict | None:
        row = c.execute("SELECT * FROM stl_lease_versions WHERE unit_id=? AND status='active' AND valid_from<=? AND COALESCE(valid_to,?)>? ORDER BY valid_from DESC LIMIT 1", (unit_id, at, OPEN_ENDED, at)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ 维保停机

    def record_outage(self, context: AccessContext, *, outage_id: str, target_type: str, target_id: str, service_code: str, start_at: str, end_at: str | None, reason: str, compensation_policy: str = "waive_and_credit") -> dict:
        context.require("settle:admin")
        require_safe(outage_id, "停机标识")
        start_at = canonical_instant(start_at)
        end_at = canonical_instant(end_at) if end_at else None
        if end_at and _dt(end_at) <= _dt(start_at):
            raise ValidationError("停机结束时间必须晚于开始时间")
        with self.database.transaction() as c:
            c.execute(
                "INSERT INTO stl_outages(outage_id,target_type,target_id,service_code,start_at,end_at,reason,compensation_policy,job_id,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,'open',?,?)",
                (outage_id, target_type, target_id, service_code, start_at, end_at, reason, compensation_policy, f"outage:{outage_id}", context.actor_id, self.clock.now()))
            # 维保补偿：持久化任务，期限=停机结束时刻；重启后不丢、不重复登记
            self.jobs.schedule_named(f"outage:{outage_id}", job_type="outage-compensation", subject_id=outage_id, run_at=end_at or start_at,
                                     payload={"outage_id": outage_id, "target_type": target_type, "target_id": target_id, "service_code": service_code}, connection=c)
            self._audit(c, context, "record-outage", outage_id, {"target": f"{target_type}:{target_id}", "start_at": start_at, "end_at": end_at})
            return self._get(c, "SELECT * FROM stl_outages WHERE outage_id=?", (outage_id,))

    def close_outage(self, context: AccessContext, *, outage_id: str, end_at: str) -> dict:
        """停机结束：补齐结束时间，并把维保补偿任务排到结束时刻。"""
        context.require("settle:admin")
        end_at = canonical_instant(end_at)
        with self.database.transaction() as c:
            row = c.execute("SELECT * FROM stl_outages WHERE outage_id=?", (outage_id,)).fetchone()
            if not row:
                raise NotFoundError("停机记录不存在")
            if _dt(end_at) <= _dt(row["start_at"]):
                raise ValidationError("停机结束时间必须晚于开始时间")
            c.execute("UPDATE stl_outages SET end_at=? WHERE outage_id=?", (end_at, outage_id))
            self.jobs.schedule_named(f"outage:{outage_id}", job_type="outage-compensation", subject_id=outage_id, run_at=end_at,
                                     payload={"outage_id": outage_id, "target_type": row["target_type"], "target_id": row["target_id"], "service_code": row["service_code"]}, connection=c)
            self._audit(c, context, "close-outage", outage_id, {"end_at": end_at})
            return self._get(c, "SELECT * FROM stl_outages WHERE outage_id=?", (outage_id,))

    @staticmethod
    def _outages_for(c, *, target_type: str, target_id: str, service_code: str, start: str, end: str) -> list[dict]:
        rows = c.execute(
            "SELECT * FROM stl_outages WHERE target_type=? AND target_id=? AND service_code=? AND status IN ('open','closed','compensated') AND start_at<? AND COALESCE(end_at,?)>?",
            (target_type, target_id, service_code, end, OPEN_ENDED, start)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _outage_cut_hours(c, outages, window: tuple[str, str]) -> tuple[D, list[dict]]:
        total = D(0); detail = []
        for outage in outages:
            cut = _intersection(outage["start_at"], _open_end(outage["end_at"]), window[0], window[1])
            if cut:
                total += _hours(*cut)
                detail.append({"outage_id": outage["outage_id"], "from": cut[0], "to": cut[1]})
        return total, detail

    # ------------------------------------------------------------------ 能力预约与拼单拆分

    def book_capacity(self, context: AccessContext, *, service_code: str, resource_id: str, start_at: str, end_at: str, capacity_total: int, organizer_tenant_id: str | None = None) -> dict:
        """按能力预约月台、冷库、充电、铁路接驳；同一资源时间重叠受容量约束。"""
        context.require("settle:book")
        start_at = canonical_instant(start_at); end_at = canonical_instant(end_at)
        if capacity_total <= 0:
            raise ValidationError("能力总量必须大于零")
        with self.database.transaction() as c:
            self._require_service(c, service_code, start_at)
            held = self.reservations.reserve(resource_id=f"{service_code}:{resource_id}", subject_id=organizer_tenant_id or resource_id,
                                             quantity=capacity_total, capacity=capacity_total, start_at=start_at, end_at=end_at,
                                             actor=context.actor_id, connection=c)
            booking_id = new_id("booking")
            c.execute(
                "INSERT INTO stl_capacity_bookings(booking_id,service_code,resource_id,start_at,end_at,capacity_total,reservation_id,organizer_tenant_id,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,'confirmed',?,?)",
                (booking_id, service_code, resource_id, start_at, end_at, capacity_total, held["reservation_id"], organizer_tenant_id, context.actor_id, self.clock.now()))
            self._audit(c, context, "book-capacity", booking_id, {"service_code": service_code, "resource_id": resource_id})
            return self._get(c, "SELECT * FROM stl_capacity_bookings WHERE booking_id=?", (booking_id,))

    def allocate(self, context: AccessContext, *, booking_id: str, tenant_id: str, quantity: int) -> dict:
        """登记拼单作业中某一租户的实际数量；跨租户作业必须逐户拆清。"""
        context.require("settle:book")
        if quantity <= 0:
            raise ValidationError("分配数量必须大于零")
        with self.database.transaction() as c:
            booking = c.execute("SELECT * FROM stl_capacity_bookings WHERE booking_id=?", (booking_id,)).fetchone()
            if not booking:
                raise NotFoundError("预约不存在")
            if booking["status"] != "confirmed":
                raise ConflictError("只能对已确认的预约登记实际数量")
            used_others = c.execute("SELECT COALESCE(SUM(quantity),0) AS n FROM stl_booking_allocations WHERE booking_id=? AND tenant_id<>?", (booking_id, tenant_id)).fetchone()["n"]
            if int(used_others) + quantity > booking["capacity_total"]:
                raise ConflictError("分拆数量超过预约能力")
            existing = c.execute("SELECT quantity FROM stl_booking_allocations WHERE booking_id=? AND tenant_id=?", (booking_id, tenant_id)).fetchone()
            if existing:
                c.execute("UPDATE stl_booking_allocations SET quantity=?, declared_by=?, declared_at=? WHERE booking_id=? AND tenant_id=?",
                          (quantity, context.actor_id, self.clock.now(), booking_id, tenant_id))
            else:
                c.execute("INSERT INTO stl_booking_allocations(booking_id,tenant_id,quantity,declared_by,declared_at) VALUES(?,?,?,?,?)",
                          (booking_id, tenant_id, quantity, context.actor_id, self.clock.now()))
            self._audit(c, context, "allocate-booking", booking_id, {"tenant_id": tenant_id, "quantity": quantity})
            return self._get(c, "SELECT * FROM stl_capacity_bookings WHERE booking_id=?", (booking_id,))

    def release_booking(self, context: AccessContext, *, booking_id: str, reason: str) -> dict:
        """设备故障时只释放受影响的这一笔服务安排，不触碰其他预约。"""
        context.require("settle:book")
        if not reason.strip():
            raise ValidationError("释放预约必须说明原因")
        with self.database.transaction() as c:
            row = c.execute("SELECT * FROM stl_capacity_bookings WHERE booking_id=?", (booking_id,)).fetchone()
            if not row:
                raise NotFoundError("预约不存在")
            if row["status"] != "confirmed":
                raise ConflictError("预约状态不允许释放")
            c.execute("UPDATE stl_capacity_bookings SET status='released' WHERE booking_id=?", (booking_id,))
            self.reservations.release(row["reservation_id"], expected_version=1, connection=c)
            self._audit(c, context, "release-booking", booking_id, {"reason": reason.strip()})
            return self._get(c, "SELECT * FROM stl_capacity_bookings WHERE booking_id=?", (booking_id,))

    def booking_allocations(self, context: AccessContext, booking_id: str) -> list[dict]:
        context.require("settle:read")
        with self.database.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM stl_booking_allocations WHERE booking_id=? ORDER BY tenant_id", (booking_id,))]

    # ------------------------------------------------------------------ 抄表

    def register_reading_due(self, context: AccessContext, *, meter_code: str, due_at: str) -> str:
        """登记抄表期限；任务持久化，重启后仍按原期限到期，无需人工重新登记。"""
        context.require("settle:admin")
        require_safe(meter_code, "计量编号")
        due_at = canonical_instant(due_at)
        job_id = f"reading-due:{meter_code}:{int(_dt(due_at).timestamp())}"
        self.jobs.schedule_named(job_id, job_type="missing-reading", subject_id=meter_code, run_at=due_at,
                                 payload={"meter_code": meter_code, "due_at": due_at})
        return job_id

    def record_reading(self, context: AccessContext, *, meter_code: str, read_at: str, reading_value: str, kind: str = "actual") -> dict:
        """录入设备原始读数（同时进入事件收件箱）。

        同一计量编号、同一时刻、同一类别再次到达：读数一致判重，不重复入账；
        读数不一致则挂起争议、记录收件箱冲突并暂停该计量点的结算。
        """
        context.require("settle:reading")
        require_safe(meter_code, "计量编号")
        if kind not in {"actual", "estimated"}:
            raise ValidationError("读数类别必须是 actual 或 estimated")
        value = self._reading_number(reading_value)
        read_at = canonical_instant(read_at)
        sequence = int(_dt(read_at).timestamp())
        dispute_info: dict | None = None
        duplicate_info: dict | None = None
        with self.database.transaction() as c:
            meter = c.execute("SELECT * FROM stl_meters WHERE meter_code=?", (meter_code,)).fetchone()
            if not meter:
                raise NotFoundError("计量点不存在")
            # 事件收件箱：同序号同内容判重，不同内容落冲突表
            inbox_result = self.inbox.receive(connection=c, source="meter", source_key=meter_code, sequence=sequence,
                                              payload={"read_at": read_at, "value": str(value), "kind": kind}, occurred_at=read_at,
                                              raise_on_conflict=False)
            if inbox_result["status"] == "duplicate":
                duplicate_info = {"meter_code": meter_code, "read_at": read_at}
            else:
                now = self.clock.now()
                previous = c.execute("SELECT * FROM stl_meter_readings WHERE meter_code=? AND read_at=? ORDER BY reading_id LIMIT 1", (meter_code, read_at)).fetchone()
                if previous and D(previous["reading_value"]) == value:
                    duplicate_info = {"meter_code": meter_code, "read_at": read_at}
                elif previous:
                    dispute_id = new_id("dispute")
                    c.execute(
                        "INSERT INTO stl_reading_disputes(dispute_id,meter_code,read_at,kind,existing_value,incoming_value,status,created_by,created_at) VALUES(?,?,?,?,?,?,'open',?,?)",
                        (dispute_id, meter_code, read_at, kind, previous["reading_value"], str(value), context.actor_id, now))
                    c.execute("INSERT INTO stl_meter_readings(reading_id,meter_code,read_at,reading_value,kind,status,dispute_key,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)",
                              (new_id("reading"), meter_code, read_at, str(value), kind, "disputed", dispute_id, context.actor_id, now))
                    dispute_info = {"dispute_id": dispute_id, "meter_code": meter_code, "read_at": read_at,
                                    "existing": previous["reading_value"], "incoming": str(value)}
                else:
                    reading_id = new_id("reading")
                    c.execute("INSERT INTO stl_meter_readings(reading_id,meter_code,read_at,reading_value,kind,status,recorded_by,recorded_at) VALUES(?,?,?,?,?,'recorded',?,?)",
                              (reading_id, meter_code, read_at, str(value), kind, context.actor_id, now))
                    self._audit(c, context, "record-reading", reading_id, {"meter_code": meter_code, "read_at": read_at, "value": str(value)})
        # 事务提交后再发通知；争议在持久化之后才抛出，保证不会随回滚丢失
        if dispute_info:
            self.outbox.enqueue(topic="reading.disputed", aggregate_id=dispute_info["meter_code"], payload=dispute_info)
            raise ConflictError(f"同一计量编号在 {read_at} 出现不同读数，已暂停结算: {dispute_info['dispute_id']}")
        if duplicate_info:
            return {"status": "duplicate", "meter_code": meter_code, "read_at": read_at}
        return {"status": "recorded", "meter_code": meter_code, "read_at": read_at}

    def resolve_reading_dispute(self, context: AccessContext, *, dispute_id: str, keep: str, note: str) -> dict:
        """争议处理：keep='existing' 采用原读数，keep='incoming' 采用新读数，随后恢复结算。"""
        context.require("settle:admin")
        if keep not in {"existing", "incoming"}:
            raise ValidationError("必须指定采用 existing 还是 incoming")
        if not note.strip():
            raise ValidationError("争议处理必须说明依据")
        with self.database.transaction() as c:
            row = c.execute("SELECT * FROM stl_reading_disputes WHERE dispute_id=?", (dispute_id,)).fetchone()
            if not row:
                raise NotFoundError("争议不存在")
            if row["status"] != "open":
                raise ConflictError("争议已经处理")
            if keep == "existing":
                c.execute("UPDATE stl_meter_readings SET status='rejected' WHERE dispute_key=?", (dispute_id,))
                chosen = row["existing_value"]
            else:
                c.execute("UPDATE stl_meter_readings SET status='superseded' WHERE meter_code=? AND read_at=? AND status='recorded' AND reading_value=?",
                          (row["meter_code"], row["read_at"], row["existing_value"]))
                c.execute("UPDATE stl_meter_readings SET status='recorded', dispute_key=NULL WHERE dispute_key=?", (dispute_id,))
                chosen = row["incoming_value"]
            c.execute("UPDATE stl_reading_disputes SET status='resolved', resolved_at=?, resolved_by=?, resolution=? WHERE dispute_id=?",
                      (self.clock.now(), context.actor_id, f"{keep}:{note.strip()}", dispute_id))
            self._audit(c, context, "resolve-dispute", dispute_id, {"keep": keep, "chosen": chosen})
            return {"dispute_id": dispute_id, "chosen_value": chosen}

    @staticmethod
    def _reading_number(value: str) -> D:
        try:
            number = D(str(value))
        except Exception as exc:
            raise ValidationError("读数必须是数字") from exc
        if not number.is_finite() or number < 0:
            raise ValidationError("读数必须是非负有限数")
        return number

    # ------------------------------------------------------------------ 账期

    def open_period(self, context: AccessContext, *, period_start: str, period_end: str, currency: str = "CNY") -> dict:
        context.require("settle:admin")
        period_start = canonical_instant(period_start); period_end = canonical_instant(period_end)
        if _dt(period_end) <= _dt(period_start):
            raise ValidationError("账期结束时间必须晚于开始时间")
        period_id = new_id("period")
        with self.database.transaction() as c:
            c.execute("INSERT INTO stl_billing_periods(period_id,period_start,period_end,currency,status,created_at) VALUES(?,?,?,?,'open',?)",
                      (period_id, period_start, period_end, currency, self.clock.now()))
            self._audit(c, context, "open-period", period_id, {"start": period_start, "end": period_end})
            return self._get(c, "SELECT * FROM stl_billing_periods WHERE period_id=?", (period_id,))

    def generate_charges(self, context: AccessContext, *, period_id: str) -> dict:
        """按账期生成费用草稿。每笔费用都带来源窗口、适用价目、租约版本与明细。"""
        context.require("settle:bill")
        charges: list[str] = []
        with self.database.transaction() as c:
            period = c.execute("SELECT * FROM stl_billing_periods WHERE period_id=?", (period_id,)).fetchone()
            if not period:
                raise NotFoundError("账期不存在")
            if period["status"] != "open":
                raise ConflictError("账期已关账，历史差错请通过贷项或补单处理")
            if c.execute("SELECT 1 FROM stl_charges WHERE period_id=? AND charge_kind='regular' LIMIT 1", (period_id,)).fetchone():
                raise ConflictError("账期已经生成过常规费用")
            start, end, currency = period["period_start"], period["period_end"], period["currency"]
            disputes = [dict(r) for r in c.execute(
                "SELECT * FROM stl_reading_disputes WHERE status='open' AND read_at>=? AND read_at<?", (start, end))]
            if disputes:
                raise ConflictError("存在读数争议，暂停结算: " + ", ".join(r["dispute_id"] for r in disputes))
            charges += self._gen_storage(c, period, start, end, currency)
            charges += self._gen_refrigeration(c, period, start, end, currency)
            charges += self._gen_bookings(c, period, start, end, currency)
            charges += self._gen_metered(c, period, start, end, currency)
            self._audit(c, context, "generate-charges", period_id, {"count": len(charges)})
        if charges:
            self.outbox.enqueue(topic="billing.generated", aggregate_id=period_id, payload={"period_id": period_id, "count": len(charges)})
        return {"period_id": period_id, "charge_ids": charges, "count": len(charges)}

    def _gen_storage(self, c, period, start, end, currency) -> list[str]:
        """仓租：每个租约版本只对自己的有效窗口计费，跨账期/跨版本自然分段。"""
        ids = []
        for lease in self._active_leases(c, start, end):
            window = _intersection(lease["valid_from"], _open_end(lease["valid_to"]), start, end)
            if not window:
                continue
            if lease["currency"] != currency:
                raise ConflictError(f"租约 {lease['lease_id']} 币种与账期不一致")
            hours = _hours(*window)
            divisor = DAY if lease["billing_unit"] == "day" else MONTH_HOURS
            amount = _money(D(lease["rate_minor"]) / divisor * hours)
            if amount == 0:
                continue
            ids.append(self._insert_charge(c, period, tenant_id=lease["tenant_id"], service_code="storage",
                                           quantity=str(hours / DAY), unit_of_measure="day", rate_minor=lease["rate_minor"],
                                           amount=amount, currency=currency, window=window,
                                           source_type="lease", source_ref=f"{lease['lease_id']}#v{lease['version']}",
                                           price_id="", lease=lease, detail={"hours": str(hours), "billing_unit": lease["billing_unit"]}))
        return ids

    def _gen_refrigeration(self, c, period, start, end, currency) -> list[str]:
        """制冷费：按价格版本分段，逐段扣除维保停机与运营方承担区间。"""
        ids = []
        compensated = {r["source_ref"] for r in c.execute("SELECT source_ref FROM stl_charges WHERE source_type='outage' AND charge_kind='compensation'")}
        services = [dict(r) for r in c.execute(
            "SELECT * FROM stl_catalog WHERE service_kind='refrigeration' AND metered=0 AND valid_from<? AND COALESCE(valid_to,?)>? AND status IN ('active','superseded')",
            (end, OPEN_ENDED, start))]
        for service in services:
            for lease in self._active_leases(c, start, end):
                window = _intersection(lease["valid_from"], _open_end(lease["valid_to"]), start, end)
                if not window:
                    continue
                # 同时与该目录版本的有效期取交集，避免目录换版后跨版本重复计费
                window = _intersection(service["valid_from"], _open_end(service["valid_to"]), window[0], window[1])
                if not window:
                    continue
                outages = [o for o in self._outages_for(c, target_type="unit", target_id=lease["unit_id"], service_code=service["service_code"], start=window[0], end=window[1]) if o["outage_id"] not in compensated]
                for seg_start, seg_end, price in self._price_segments(c, service["service_code"], currency, window):
                    billable_hours = _hours(seg_start, seg_end)
                    outage_hours, outage_detail = self._outage_cut_hours(c, outages, (seg_start, seg_end))
                    billable_hours -= outage_hours
                    billable_hours -= self._operator_hours(c, "unit", lease["unit_id"], service["service_code"], lease["tenant_id"], (seg_start, seg_end))
                    if billable_hours <= 0:
                        continue
                    amount = _money(D(price["rate_minor"]) / DAY * billable_hours)
                    if amount == 0:
                        continue
                    ids.append(self._insert_charge(c, period, tenant_id=lease["tenant_id"], service_code=service["service_code"],
                                                   quantity=str(billable_hours / DAY), unit_of_measure=service["unit_of_measure"],
                                                   rate_minor=price["rate_minor"], amount=amount, currency=currency,
                                                   window=(seg_start, seg_end), source_type="service-unit", source_ref=lease["unit_id"],
                                                   price_id=price["price_id"], lease=lease,
                                                   detail={"hours": str(billable_hours), "outages": outage_detail}))
        return ids

    def _gen_bookings(self, c, period, start, end, currency) -> list[str]:
        """月台、充电、铁路接驳：按能力预约出账，拼单作业按实际拆数量逐户计费。"""
        ids = []
        rows = c.execute(
            "SELECT * FROM stl_capacity_bookings WHERE status='confirmed' AND start_at<? AND end_at>?",
            (end, start)).fetchall()
        for booking_row in rows:
            booking = dict(booking_row)
            allocations = [dict(r) for r in c.execute("SELECT * FROM stl_booking_allocations WHERE booking_id=? ORDER BY tenant_id", (booking["booking_id"],))]
            if not allocations:
                raise ConflictError(f"拼单预约 {booking['booking_id']} 未拆清各租户实际数量，不能出账")
            total_allocated = sum(int(a["quantity"]) for a in allocations)
            if total_allocated != booking["capacity_total"]:
                raise ConflictError(f"预约 {booking['booking_id']} 分拆数量合计 {total_allocated} 与预约能力 {booking['capacity_total']} 不一致")
            service = self._require_service(c, booking["service_code"], booking["start_at"])
            window = _intersection(booking["start_at"], booking["end_at"], start, end)
            for alloc in allocations:
                if service["metered"] == 0 and self._price_is_time(c, booking["service_code"], currency, window):
                    # 时间计价且窗口跨价目版本：逐段计费，临时调价也能找到适用价目
                    for seg_start, seg_end, price in self._price_segments(c, booking["service_code"], currency, window):
                        quantity = D(alloc["quantity"]) * _hours(seg_start, seg_end)
                        self._booking_charge(c, period, booking, alloc, price, quantity, (seg_start, seg_end), currency, ids)
                else:
                    # 用量计价：用量不可按时间拆分，按窗口中点取当时生效价目
                    midpoint = _iso(_dt(window[0]) + (_dt(window[1]) - _dt(window[0])) / 2)
                    price = self._price_as_of(c, booking["service_code"], midpoint, currency)
                    if price["charge_mode"] == "time":
                        quantity = D(alloc["quantity"]) * _hours(*window)
                    else:
                        quantity = D(alloc["quantity"])
                    self._booking_charge(c, period, booking, alloc, price, quantity, window, currency, ids)
        return ids

    def _booking_charge(self, c, period, booking, alloc, price, quantity: D, window, currency, ids) -> None:
        amount = _money(D(price["rate_minor"]) * quantity)
        if amount == 0:
            return
        ids.append(self._insert_charge(c, period, tenant_id=alloc["tenant_id"], service_code=booking["service_code"],
                                       quantity=str(quantity), unit_of_measure=price["unit_of_measure"],
                                       rate_minor=price["rate_minor"], amount=amount, currency=currency,
                                       window=window, source_type="booking", source_ref=booking["booking_id"],
                                       price_id=price["price_id"], lease=None,
                                       detail={"resource_id": booking["resource_id"], "booking_id": booking["booking_id"],
                                               "hours": str(_hours(*window)), "charge_mode": price["charge_mode"],
                                               "organizer_tenant_id": booking["organizer_tenant_id"]}))

    def _gen_metered(self, c, period, start, end, currency) -> list[str]:
        """绑定仓位单元的计量表：按账期内读数差量计费，租户取读数时点的租约版本。"""
        ids = []
        meters = [dict(r) for r in c.execute("SELECT * FROM stl_meters WHERE unit_id IS NOT NULL AND status='active' AND service_code<>''")]
        for meter in meters:
            service = c.execute("SELECT * FROM stl_catalog WHERE service_code=? AND metered=1 AND status IN ('active','superseded')", (meter["service_code"],)).fetchone()
            if not service:
                continue
            rows = c.execute("SELECT * FROM stl_meter_readings WHERE meter_code=? AND status='recorded' AND read_at<? ORDER BY read_at,reading_id",
                             (meter["meter_code"], end)).fetchall()
            series = [dict(r) for r in rows]
            prev = None
            for index, reading in enumerate(series):
                if index == 0:
                    # 序列第一条读数作为期初基准（账期开始前或期初抄见）
                    prev = reading
                    continue
                in_period = _dt(start) <= _dt(reading["read_at"]) < _dt(end)
                if not in_period:
                    prev = reading
                    continue
                delta = D(reading["reading_value"]) - D(prev["reading_value"])
                if delta < 0:
                    raise ConflictError(f"计量点 {meter['meter_code']} 读数倒退，暂停结算")
                lease = self._lease_for_unit_at(c, meter["unit_id"], reading["read_at"])
                if not lease:
                    prev = reading
                    continue
                window = (_iso(max(_dt(start), _dt(prev["read_at"]))), reading["read_at"])
                price = self._price_as_of(c, meter["service_code"], reading["read_at"], currency)
                outage_hours = D(0)
                for outage in self._outages_for(c, target_type="meter", target_id=meter["meter_code"], service_code=meter["service_code"], start=window[0], end=window[1]):
                    cut = _intersection(outage["start_at"], _open_end(outage["end_at"]), window[0], window[1])
                    if cut:
                        outage_hours += _hours(*cut)
                total_hours = _hours(*window)
                if total_hours > 0 and outage_hours:
                    delta *= max(D(0), total_hours - outage_hours) / total_hours
                if self._bearer_at(c, "meter", meter["meter_code"], meter["service_code"], lease["tenant_id"], reading["read_at"]) == "operator":
                    prev = reading
                    continue
                amount = _money(D(price["rate_minor"]) * delta)
                if amount:
                    ids.append(self._insert_charge(c, period, tenant_id=lease["tenant_id"], service_code=meter["service_code"],
                                                   quantity=str(delta), unit_of_measure=meter["unit_of_measure"],
                                                   rate_minor=price["rate_minor"], amount=amount, currency=currency,
                                                   window=window, source_type="reading", source_ref=reading["reading_id"],
                                                   price_id=price["price_id"], lease=lease,
                                                   detail={"meter_code": meter["meter_code"], "from_reading": prev["reading_id"], "to_reading": reading["reading_id"]}))
                prev = reading
        return ids

    # ------------------------------------------------------------------ 过账、关账

    def post_charge(self, context: AccessContext, *, charge_id: str, connection=None) -> dict:
        context.require("settle:bill")
        def work(c) -> dict:
            row = c.execute("SELECT * FROM stl_charges WHERE charge_id=?", (charge_id,)).fetchone()
            if not row:
                raise NotFoundError("费用不存在")
            if row["status"] != "draft":
                raise ConflictError("只有草稿费用可以过账")
            amount = abs(row["amount_minor"])
            direction = "debit" if row["amount_minor"] >= 0 else "credit"
            entry = self.ledger.post(journal_key=f"tenant:{row['tenant_id']}", account=f"receivable:{row['service_code']}",
                                     currency=row["currency"], amount=str(D(amount) / D(100)),
                                     direction=direction, reference=charge_id, actor=context.actor_id, connection=c)
            c.execute("INSERT INTO stl_charge_entries(charge_id,entry_id,role) VALUES(?,?,'receivable')", (charge_id, entry["entry_id"]))
            c.execute("UPDATE stl_charges SET status='posted' WHERE charge_id=?", (charge_id,))
            self._audit(c, context, "post-charge", charge_id, {"entry_id": entry["entry_id"], "direction": direction})
            return {"charge_id": charge_id, "entry_id": entry["entry_id"], "status": "posted"}
        if connection is not None:
            return work(connection)
        with self.database.transaction() as c:
            return work(c)

    def post_all(self, context: AccessContext, *, period_id: str) -> list[dict]:
        context.require("settle:bill")
        with self.database.connect() as c:
            ids = [r["charge_id"] for r in c.execute("SELECT charge_id FROM stl_charges WHERE period_id=? AND status='draft' ORDER BY charge_id", (period_id,))]
        return [self.post_charge(context, charge_id=i) for i in ids]

    def close_period(self, context: AccessContext, *, period_id: str) -> dict:
        context.require("settle:close")
        with self.database.transaction() as c:
            period = c.execute("SELECT * FROM stl_billing_periods WHERE period_id=?", (period_id,)).fetchone()
            if not period:
                raise NotFoundError("账期不存在")
            if period["status"] != "open":
                raise ConflictError("账期已经关闭")
            draft = c.execute("SELECT COUNT(*) AS n FROM stl_charges WHERE period_id=? AND status='draft'", (period_id,)).fetchone()["n"]
            if draft:
                raise ConflictError(f"还有 {draft} 笔草稿费用未过账")
            disputes = c.execute("SELECT COUNT(*) AS n FROM stl_reading_disputes WHERE status='open' AND read_at>=? AND read_at<?", (period["period_start"], period["period_end"])).fetchone()["n"]
            if disputes:
                raise ConflictError("账期内仍有读数争议未处理")
            c.execute("UPDATE stl_billing_periods SET status='closed',closed_at=?,closed_by=? WHERE period_id=?", (self.clock.now(), context.actor_id, period_id))
            self._audit(c, context, "close-period", period_id, {})
            return self._get(c, "SELECT * FROM stl_billing_periods WHERE period_id=?", (period_id,))

    # ------------------------------------------------------------------ 减免、贷项、补单

    def request_relief(self, context: AccessContext, *, period_id: str, charge_id: str, amount_minor: int, reason: str) -> dict:
        context.require("settle:relief")
        if amount_minor <= 0:
            raise ValidationError("减免金额必须大于零")
        if not reason.strip():
            raise ValidationError("减免必须说明理由")
        with self.database.transaction() as c:
            charge = c.execute("SELECT * FROM stl_charges WHERE charge_id=?", (charge_id,)).fetchone()
            if not charge:
                raise NotFoundError("费用不存在")
            relief_id = new_id("relief")
            c.execute(
                "INSERT INTO stl_relief_requests(relief_id,period_id,tenant_id,amount_minor,currency,reason,charge_id,status,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                (relief_id, period_id, charge["tenant_id"], amount_minor, charge["currency"], reason.strip(), charge_id, context.actor_id, self.clock.now()))
            self._audit(c, context, "request-relief", relief_id, {"charge_id": charge_id, "amount_minor": amount_minor})
            return self._get(c, "SELECT * FROM stl_relief_requests WHERE relief_id=?", (relief_id,))

    def decide_relief(self, context: AccessContext, *, relief_id: str, approve: bool, note: str) -> dict:
        """批准或驳回减免；提交人不能批准自己的申请。批准后自动生成贷项费用草稿。"""
        context.require("settle:approve")
        if not note.strip():
            raise ValidationError("审批必须填写意见")
        result: dict
        with self.database.transaction() as c:
            relief = c.execute("SELECT * FROM stl_relief_requests WHERE relief_id=?", (relief_id,)).fetchone()
            if not relief:
                raise NotFoundError("减免申请不存在")
            if relief["status"] != "pending":
                raise ConflictError("减免申请已经处理")
            assert_distinct(relief["submitted_by"], context.actor_id)
            decision = "approved" if approve else "rejected"
            c.execute("UPDATE stl_relief_requests SET status=?,decided_by=?,decided_at=?,decision_note=? WHERE relief_id=?",
                      (decision, context.actor_id, self.clock.now(), note.strip(), relief_id))
            self._audit(c, context, "decide-relief", relief_id, {"decision": decision})
            result = {"relief_id": relief_id, "status": decision}
            if approve:
                service_code = c.execute("SELECT service_code FROM stl_charges WHERE charge_id=?", (relief["charge_id"],)).fetchone()["service_code"]
                credit = self._correction(c, context, period_id=relief["period_id"], tenant_id=relief["tenant_id"],
                                          service_code=service_code, amount_minor=-int(relief["amount_minor"]),
                                          currency=relief["currency"], kind="credit",
                                          reason=f"减免批准: {relief_id}", parent_charge_id=relief["charge_id"], approval_id=relief_id)
                result["credit"] = credit
        if approve:
            self.outbox.enqueue(topic="relief.approved", aggregate_id=relief_id, payload={"relief_id": relief_id})
        return result

    def issue_credit(self, context: AccessContext, *, period_id: str, tenant_id: str, service_code: str, amount_minor: int, reason: str, parent_charge_id: str | None = None) -> dict:
        """历史差错纠正：向（已关账账期之后的）未关账账期开贷款项。"""
        context.require("settle:approve")
        if not reason.strip():
            raise ValidationError("贷项必须说明原因")
        with self.database.transaction() as c:
            if parent_charge_id and not c.execute("SELECT 1 FROM stl_charges WHERE charge_id=? AND tenant_id=?", (parent_charge_id, tenant_id)).fetchone():
                raise NotFoundError("原费用不存在或不属于该租户")
            result = self._correction(c, context, period_id=period_id, tenant_id=tenant_id, service_code=service_code,
                                      amount_minor=-abs(amount_minor), currency=self._period(c, period_id)["currency"],
                                      kind="credit", reason=reason.strip(), parent_charge_id=parent_charge_id, approval_id=None)
            self._audit(c, context, "issue-credit", result["charge_id"], {"parent": parent_charge_id})
            return result

    def issue_supplement(self, context: AccessContext, *, period_id: str, tenant_id: str, service_code: str, amount_minor: int, reason: str, parent_charge_id: str | None = None) -> dict:
        """历史差错纠正：漏收费用在之后账期补单。"""
        context.require("settle:approve")
        if not reason.strip():
            raise ValidationError("补单必须说明原因")
        with self.database.transaction() as c:
            if parent_charge_id and not c.execute("SELECT 1 FROM stl_charges WHERE charge_id=? AND tenant_id=?", (parent_charge_id, tenant_id)).fetchone():
                raise NotFoundError("原费用不存在或不属于该租户")
            result = self._correction(c, context, period_id=period_id, tenant_id=tenant_id, service_code=service_code,
                                      amount_minor=abs(amount_minor), currency=self._period(c, period_id)["currency"],
                                      kind="supplement", reason=reason.strip(), parent_charge_id=parent_charge_id, approval_id=None)
            self._audit(c, context, "issue-supplement", result["charge_id"], {"parent": parent_charge_id})
            return result

    def _correction(self, c, context, *, period_id, tenant_id, service_code, amount_minor, currency, kind, reason, parent_charge_id, approval_id) -> dict:
        period = self._period(c, period_id)
        if period["status"] != "open":
            raise ConflictError("贷项或补单只能进入未关账的账期")
        charge_id = new_id("charge")
        c.execute(
            "INSERT INTO stl_charges(charge_id,period_id,tenant_id,service_code,quantity,unit_of_measure,rate_minor,amount_minor,currency,charge_kind,status,window_start,window_end,source_type,source_ref,price_id,parent_charge_id,approval_id,detail_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (charge_id, period_id, tenant_id, service_code, "1", "correction", abs(amount_minor), amount_minor, currency, kind, "draft",
             period["period_start"], period["period_end"], "correction", reason, "", parent_charge_id, approval_id, "{}", context.actor_id, self.clock.now()))
        return {"charge_id": charge_id, "amount_minor": amount_minor, "kind": kind}

    # ------------------------------------------------------------------ 查询与下钻

    def list_charges(self, context: AccessContext, *, period_id: str, tenant_id: str | None = None) -> list[dict]:
        context.require("settle:read")
        if tenant_id is not None:
            self._assert_tenant(context, tenant_id)
        elif not context.allows("*"):
            tenant_id = context.tenant_id
        sql = "SELECT * FROM stl_charges WHERE period_id=?"
        params: list[object] = [period_id]
        if tenant_id:
            sql += " AND tenant_id=?"; params.append(tenant_id)
        sql += " ORDER BY charge_id"
        with self.database.connect() as c:
            return [dict(r) for r in c.execute(sql, params)]

    def get_charge(self, context: AccessContext, *, charge_id: str) -> dict:
        """租户只能读取本企业费用；关账人员不受限。"""
        context.require("settle:read")
        with self.database.connect() as c:
            row = c.execute("SELECT * FROM stl_charges WHERE charge_id=?", (charge_id,)).fetchone()
            if not row:
                raise NotFoundError("费用不存在")
            self._assert_tenant(context, row["tenant_id"])
            return dict(row)

    def drill_down(self, context: AccessContext, *, charge_id: str) -> dict:
        """关账人员从一笔费用直接下钻到占用记录、原始读数、适用价目、租约版本与批准人。"""
        context.require("settle:close")
        with self.database.connect() as c:
            charge_row = c.execute("SELECT * FROM stl_charges WHERE charge_id=?", (charge_id,)).fetchone()
            if not charge_row:
                raise NotFoundError("费用不存在")
            charge = dict(charge_row)
            detail = json.loads(charge["detail_json"]) if charge["detail_json"] else {}
            result: dict = {
                "charge": charge,
                "entries": [dict(r) for r in c.execute("SELECT je.* FROM journal_entries je JOIN stl_charge_entries ce ON ce.entry_id=je.entry_id WHERE ce.charge_id=?", (charge_id,))],
            }
            if charge["price_id"]:
                result["price"] = dict(c.execute("SELECT * FROM stl_prices WHERE price_id=?", (charge["price_id"],)).fetchone())
                catalog = c.execute("SELECT * FROM stl_catalog WHERE service_code=?", (charge["service_code"],)).fetchone()
                result["catalog"] = dict(catalog) if catalog else None
            if charge["lease_id"]:
                lease = c.execute("SELECT * FROM stl_lease_versions WHERE lease_id=? AND version=?", (charge["lease_id"], charge["lease_version"])).fetchone()
                result["lease_version"] = dict(lease)
                unit = c.execute("SELECT * FROM stl_storage_units WHERE unit_id=? ORDER BY valid_from DESC LIMIT 1", (lease["unit_id"],)).fetchone()
                result["unit"] = dict(unit) if unit else None
                result["acceptance"] = self._optional(c, "SELECT * FROM stl_acceptances WHERE unit_id=? ORDER BY accepted_at DESC LIMIT 1", (lease["unit_id"],))
            if charge["approval_id"]:
                result["approval"] = self._optional(c, "SELECT * FROM stl_relief_requests WHERE relief_id=?", (charge["approval_id"],))
            if charge["source_type"] == "booking":
                booking_id = charge["source_ref"]
                result["booking"] = dict(c.execute("SELECT * FROM stl_capacity_bookings WHERE booking_id=?", (booking_id,)).fetchone())
                result["allocations"] = [dict(r) for r in c.execute("SELECT * FROM stl_booking_allocations WHERE booking_id=? ORDER BY tenant_id", (booking_id,))]
            if charge["source_type"] == "reading":
                result["readings"] = [dict(c.execute("SELECT * FROM stl_meter_readings WHERE reading_id=?", (detail["from_reading"],)).fetchone()),
                                      dict(c.execute("SELECT * FROM stl_meter_readings WHERE reading_id=?", (detail["to_reading"],)).fetchone())]
                result["meter"] = dict(c.execute("SELECT * FROM stl_meters WHERE meter_code=?", (detail["meter_code"],)).fetchone())
                outages = c.execute("SELECT * FROM stl_outages WHERE target_type='meter' AND target_id=?", (detail["meter_code"],)).fetchall()
                result["outages"] = [dict(r) for r in outages]
            if charge["source_type"] == "service-unit" and detail.get("outages"):
                result["outages"] = [dict(c.execute("SELECT * FROM stl_outages WHERE outage_id=?", (item["outage_id"],)).fetchone()) for item in detail["outages"]]
            if charge["source_type"] == "outage":
                result["outage"] = dict(c.execute("SELECT * FROM stl_outages WHERE outage_id=?", (charge["source_ref"],)).fetchone())
            if charge["parent_charge_id"]:
                result["parent_charge"] = dict(c.execute("SELECT * FROM stl_charges WHERE charge_id=?", (charge["parent_charge_id"],)).fetchone())
            return result

    def tenant_balance(self, context: AccessContext, *, tenant_id: str, currency: str = "CNY") -> int:
        self._assert_tenant(context, tenant_id)
        return self.ledger.balance(f"tenant:{tenant_id}", currency=currency)

    def outstanding_disputes(self, context: AccessContext) -> list[dict]:
        context.require("settle:read")
        with self.database.connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM stl_reading_disputes WHERE status='open' ORDER BY created_at")]

    # ------------------------------------------------------------------ 到期任务处理

    def claim_due_jobs(self, context: AccessContext) -> list[dict]:
        context.require("settle:admin")
        return self.jobs.claim_due()

    def run_due_jobs(self, context: AccessContext, *, limit: int = 20) -> list[dict]:
        """认领并处理到期任务；任务持久化，进程重启后仍按原期限被认领。"""
        context.require("settle:admin")
        results = []
        for job in self.jobs.claim_due(limit=limit):
            outcome: dict = {"job_id": job["job_id"], "job_type": job["job_type"]}
            try:
                if job["job_type"] == "outage-compensation":
                    applied = self.apply_outage_compensation(context, job=job)
                    outcome["result"] = applied or {"status": "deferred"}
                elif job["job_type"] == "missing-reading":
                    payload = json.loads(job["payload_json"])
                    self.outbox.enqueue(topic="reading.missing", aggregate_id=payload["meter_code"], payload=payload)
                    outcome["result"] = {"status": "missing-reading-notified"}
                elif job["job_type"] == "lease-expiry":
                    payload = json.loads(job["payload_json"])
                    self.outbox.enqueue(topic="lease.expiring", aggregate_id=payload["lease_id"], payload=payload)
                    outcome["result"] = {"status": "lease-expiry-notified"}
                else:
                    outcome["result"] = {"status": "unknown-job-type"}
                self.jobs.finish(job["job_id"])
                results.append(outcome)
            except Exception as exc:
                self.jobs.retry(job["job_id"], error=str(exc), retry_at=self.clock.now())
                outcome["result"] = {"status": "retry", "error": str(exc)}
                results.append(outcome)
        return results

    def apply_outage_compensation(self, context: AccessContext, *, job: dict) -> dict | None:
        """维保补偿任务到期执行。

        - 停机在出账前已登记：出账时已按停机时长扣减，任务只做确认，不重复退款；
        - 停机在账期关账后才补登记（历史错收）：在之后第一个未关账账期生成贷项纠正；
        - 尚未出账：出账时会自动扣减，任务关闭即可。
        """
        context.require("settle:bill")
        payload = json.loads(job["payload_json"])
        result: dict | None = None
        with self.database.transaction() as c:
            outage = c.execute("SELECT * FROM stl_outages WHERE outage_id=?", (payload["outage_id"],)).fetchone()
            if not outage:
                return {"status": "outage-missing"}
            if outage["status"] == "compensated":
                return {"status": "already-applied"}
            anchor = outage["end_at"] or outage["start_at"]

            # 出账时已经把本次停机从常规费用中扣减过（明细留有 outage_id），不重复退
            deducted = c.execute(
                "SELECT 1 FROM stl_charges WHERE detail_json LIKE ? LIMIT 1",
                (f'%"outage_id":"{outage["outage_id"]}"%',)).fetchone()
            if deducted:
                c.execute("UPDATE stl_outages SET status='closed' WHERE outage_id=?", (outage["outage_id"],))
                return {"status": "already-deducted-at-billing"}

            lease = self._lease_for_unit_at(c, outage["target_id"], anchor) if outage["target_type"] == "unit" else None
            period_row = c.execute("SELECT * FROM stl_billing_periods WHERE status='open' AND period_end>=? ORDER BY period_start LIMIT 1", (anchor,)).fetchone()
            if not period_row or not lease:
                # 还没有可用账期或没有适用租约：出账时尚未关账会自动扣减；这里关闭等待
                c.execute("UPDATE stl_outages SET status='closed' WHERE outage_id=?", (outage["outage_id"],))
                return {"status": "no-charge-yet"}

            price = self._price_as_of(c, outage["service_code"], outage["start_at"], period_row["currency"])
            lease_window = _intersection(outage["start_at"], _open_end(outage["end_at"]), lease["valid_from"], _open_end(lease["valid_to"]))
            hours = _hours(*lease_window) if lease_window else D(0)
            amount_minor = -_money(D(price["rate_minor"]) / DAY * hours) if price["charge_mode"] == "time" else 0
            if amount_minor == 0:
                c.execute("UPDATE stl_outages SET status='closed' WHERE outage_id=?", (outage["outage_id"],))
                return {"status": "zero-amount"}
            period = dict(period_row)
            charge_id = self._insert_charge(c, period, tenant_id=lease["tenant_id"], service_code=outage["service_code"],
                                            quantity=str(hours / DAY), unit_of_measure="day", rate_minor=price["rate_minor"],
                                            amount=amount_minor, currency=period_row["currency"], charge_kind="compensation",
                                            window=(lease_window[0] if lease_window else outage["start_at"], lease_window[1] if lease_window else anchor),
                                            source_type="outage", source_ref=outage["outage_id"], price_id=price["price_id"],
                                            lease=lease, detail={"outage_id": outage["outage_id"], "hours": str(hours), "reason": outage["reason"], "late_registered": True})
            c.execute("UPDATE stl_outages SET status='compensated' WHERE outage_id=?", (outage["outage_id"],))
            result = {"charge_id": charge_id, "amount_minor": amount_minor}
        if result:
            self.outbox.enqueue(topic="outage.compensated", aggregate_id=payload["outage_id"], payload=result)
        return result

    # ------------------------------------------------------------------ 内部助手

    def _insert_charge(self, c, period, *, tenant_id, service_code, quantity, unit_of_measure, rate_minor, amount, currency, window, source_type, source_ref, price_id, lease, detail, charge_kind: str = "regular") -> str:
        charge_id = new_id("charge")
        c.execute(
            "INSERT INTO stl_charges(charge_id,period_id,tenant_id,service_code,quantity,unit_of_measure,rate_minor,amount_minor,currency,charge_kind,status,window_start,window_end,source_type,source_ref,price_id,lease_id,lease_version,detail_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (charge_id, period["period_id"], tenant_id, service_code, str(quantity), unit_of_measure, rate_minor, amount, currency, charge_kind, "draft",
             window[0], window[1], source_type, source_ref, price_id, lease["lease_id"] if lease else None, lease["version"] if lease else None,
             canonical_json(detail), "system", self.clock.now()))
        return charge_id

    def _audit(self, c, context: AccessContext, action: str, entity_id: str, detail: dict) -> None:
        self.audit.append(c, actor_id=context.actor_id, action=action, entity_type="settlement", entity_id=entity_id, version=1, detail=detail)

    @staticmethod
    def _require_service(c, service_code: str, at: str) -> dict:
        at = canonical_instant(at)
        row = c.execute("SELECT * FROM stl_catalog WHERE service_code=? AND valid_from<=? AND COALESCE(valid_to,?)>? AND status IN ('active','superseded') ORDER BY valid_from DESC LIMIT 1", (service_code, at, OPEN_ENDED, at)).fetchone()
        if not row:
            raise NotFoundError(f"服务 {service_code} 在该时点不在目录中")
        return dict(row)

    @staticmethod
    def _price_as_of(c, service_code: str, at: str, currency: str) -> dict:
        at = canonical_instant(at)
        row = c.execute("SELECT * FROM stl_prices WHERE service_code=? AND valid_from<=? AND status IN ('active','superseded') AND currency=? ORDER BY valid_from DESC LIMIT 1", (service_code, at, currency)).fetchone()
        if not row:
            raise ConflictError(f"服务 {service_code} 在 {at} 没有适用的{currency}价格")
        return dict(row)

    def _price_is_time(self, c, service_code: str, currency: str, window: tuple[str, str]) -> bool:
        """窗口内全部生效价格都是时间计价时才允许按时间分段。"""
        rows = c.execute(
            "SELECT DISTINCT charge_mode FROM stl_prices WHERE service_code=? AND status IN ('active','superseded') AND currency=? AND valid_from<? AND COALESCE(valid_to,?)>?",
            (service_code, currency, window[1], OPEN_ENDED, window[0])).fetchall()
        modes = {r["charge_mode"] for r in rows}
        return bool(modes) and modes == {"time"}

    def _price_segments(self, c, service_code: str, currency: str, window: tuple[str, str]) -> list[tuple[str, str, dict]]:
        """把窗口按价格版本切成段；临时调价跨过账期时逐段都能找到适用价目。"""
        rows = c.execute(
            "SELECT * FROM stl_prices WHERE service_code=? AND status IN ('active','superseded') AND currency=? AND valid_from<? ORDER BY valid_from",
            (service_code, currency, window[1])).fetchall()
        prices = [dict(r) for r in rows]
        if not prices:
            raise ConflictError(f"服务 {service_code} 在窗口内没有适用的{currency}价格")
        segments = []
        for index, price in enumerate(prices):
            seg_start = _iso(max(_dt(window[0]), _dt(price["valid_from"])))
            next_from = prices[index + 1]["valid_from"] if index + 1 < len(prices) else None
            seg_end = _iso(min(_dt(window[1]), _dt(next_from))) if next_from else window[1]
            if _dt(seg_start) < _dt(seg_end):
                segments.append((seg_start, seg_end, price))
        if not segments:
            raise ConflictError(f"服务 {service_code} 在窗口起点之前没有生效价格")
        return segments

    @staticmethod
    def _bearer_at(c, target_type: str, target_id: str, service_code: str, tenant_id: str, at: str) -> str:
        row = c.execute(
            "SELECT bearer FROM stl_responsibilities WHERE target_type=? AND target_id=? AND service_code=? AND tenant_id=? AND status='active' AND valid_from<=? AND COALESCE(valid_to,?)>? ORDER BY valid_from DESC LIMIT 1",
            (target_type, target_id, service_code, tenant_id, at, OPEN_ENDED, at)).fetchone()
        return row["bearer"] if row else "tenant"

    @staticmethod
    def _operator_hours(c, target_type: str, target_id: str, service_code: str, tenant_id: str, window: tuple[str, str]) -> D:
        """运营方承担区间与窗口的重叠小时数，按实际时长扣减租户费用。"""
        rows = c.execute(
            "SELECT valid_from, COALESCE(valid_to,?) AS valid_to FROM stl_responsibilities WHERE target_type=? AND target_id=? AND service_code=? AND tenant_id=? AND status='active' AND bearer='operator' AND valid_from<? AND COALESCE(valid_to,?)>?",
            (OPEN_ENDED, target_type, target_id, service_code, tenant_id, window[1], OPEN_ENDED, window[0])).fetchall()
        total = D(0)
        for row in rows:
            cut = _intersection(row["valid_from"], row["valid_to"], window[0], window[1])
            if cut:
                total += _hours(*cut)
        return total

    @staticmethod
    def _period(c, period_id: str) -> dict:
        row = c.execute("SELECT * FROM stl_billing_periods WHERE period_id=?", (period_id,)).fetchone()
        if not row:
            raise NotFoundError("账期不存在")
        return dict(row)

    @staticmethod
    def _get(c, sql: str, params) -> dict:
        return dict(c.execute(sql, params).fetchone())

    @staticmethod
    def _optional(c, sql: str, params):
        row = c.execute(sql, params).fetchone()
        return dict(row) if row else None
