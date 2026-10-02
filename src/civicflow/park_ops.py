"""园区能力预约与跨租户作业。

月台、冷库、充电和铁路接驳都按“能力 + 容量窗口”预约（复用
resource_reservations）。一次拼单作业在确认时必须把实际数量按租户全部拆清，
写入 park_usage；设备故障只取消该故障资源窗口内的安排，不波及其它能力。
"""

from __future__ import annotations

from dataclasses import dataclass

from .audit import AuditLog
from .database import Database
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .identifiers import new_id, require_safe
from .idempotency import IdempotencyStore
from .security import AccessContext
from .timeutil import Clock, canonical_instant, parse_instant


CAPABILITIES = ("dock", "cold", "charging", "rail",)


@dataclass(frozen=True)
class ParkOperations:
    database: Database
    clock: Clock
    idempotency: IdempotencyStore
    audit: AuditLog

    def reserve(self, context: AccessContext, *, resource_id: str, capability: str, quantity: int,
                capacity: int, start_at: str, end_at: str, tenants: list[str],
                request_key: str) -> dict:
        """预约能力窗口。tenants 为本次作业声明的参与租户，确认时必须全部覆盖。"""
        context.require("write:park-ops")
        require_safe(resource_id, "资源标识")
        if capability not in CAPABILITIES:
            raise ValidationError("能力类型必须是 dock、cold、charging 或 rail")
        if not isinstance(quantity, int) or quantity <= 0 or capacity <= 0 or quantity > capacity:
            raise ValidationError("预约数量或容量不合法")
        tenants = self._clean_tenants(tenants)
        start_at = canonical_instant(start_at); end_at = canonical_instant(end_at)
        if parse_instant(start_at) >= parse_instant(end_at):
            raise ValidationError("预约结束时间必须晚于开始时间")
        with self.database.transaction() as connection:
            def operation() -> dict:
                row = connection.execute("SELECT COALESCE(SUM(quantity),0) AS used FROM resource_reservations WHERE resource_id=? AND status IN ('held','confirmed') AND start_at<? AND end_at>?", (resource_id, end_at, start_at)).fetchone()
                if int(row["used"]) + quantity > capacity:
                    raise ConflictError(f"{capability} 能力容量不足")
                reservation_id = new_id("reservation")
                connection.execute("INSERT INTO resource_reservations(reservation_id,resource_id,subject_id,quantity,start_at,end_at,status,version,created_by) VALUES(?,?,?,?,?,?,?,?,?)",
                                   (reservation_id, resource_id, f"operation:{reservation_id}", quantity, start_at, end_at, "confirmed", 1, context.actor_id))
                self.audit.append(connection, actor_id=context.actor_id, action="park-reserve", entity_type="resource_reservations",
                                  entity_id=reservation_id, version=1,
                                  detail={"resource_id": resource_id, "capability": capability, "quantity": quantity, "tenants": tenants})
                return {"reservation_id": reservation_id, "resource_id": resource_id, "capability": capability,
                        "quantity": quantity, "capacity": capacity, "start_at": start_at, "end_at": end_at,
                        "tenants": tenants, "status": "confirmed"}
            return self.idempotency.execute(connection, scope=f"park-reserve:{resource_id}:{start_at}", request_key=request_key,
                                            request={"resource_id": resource_id, "quantity": quantity, "start_at": start_at,
                                                     "end_at": end_at, "tenants": tenants}, operation=operation)

    def confirm_operation(self, context: AccessContext, *, reservation_id: str, allocations: list[dict],
                          request_key: str, outbox=None) -> dict:
        """确认拼单作业并按租户拆清实际数量。

        allocations: [{"tenant_org": ..., "quantity": ...}, ...]
        实际总量必须为正、不超过预约容量，且覆盖预约声明的全部参与租户，
        每家数量大于零——多租户拼单不允许把用量全部落到一家。
        """
        context.require("write:park-ops")
        allocations = self._validate_allocations(allocations)
        with self.database.transaction() as connection:
            def operation() -> dict:
                reservation = connection.execute("SELECT * FROM resource_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
                if not reservation:
                    raise NotFoundError("预约不存在")
                if reservation["status"] != "confirmed":
                    raise ConflictError(f"预约状态为 {reservation['status']}，不能确认作业")
                detail = self._audit_detail(connection, reservation_id)
                declared = set(detail["tenants"]); actual_tenants = {item["tenant_org"] for item in allocations}
                if len(declared) > 1 and actual_tenants != declared:
                    missing = sorted(declared - actual_tenants)
                    raise ValidationError("跨租户作业必须拆清全部参与租户的实际数量，缺少: " + ", ".join(missing))
                total = sum(item["quantity"] for item in allocations)
                if total <= 0 or total > reservation["quantity"]:
                    raise ValidationError("实际总量必须为正且不超过预约数量")
                existing = connection.execute("SELECT COUNT(*) AS n FROM park_usage WHERE reservation_id=? AND status='confirmed'", (reservation_id,)).fetchone()
                if int(existing["n"]):
                    raise ConflictError("该作业已经确认，不能重复拆分")
                usage_ids = []
                for item in allocations:
                    usage_id = new_id("usage")
                    connection.execute("INSERT INTO park_usage(usage_id,reservation_id,resource_id,capability,tenant_org,quantity,start_at,end_at,status,created_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                       (usage_id, reservation_id, reservation["resource_id"], detail["capability"], item["tenant_org"],
                                        item["quantity"], reservation["start_at"], reservation["end_at"], "confirmed", context.actor_id))
                    usage_ids.append(usage_id)
                self.audit.append(connection, actor_id=context.actor_id, action="park-confirm", entity_type="resource_reservations",
                                  entity_id=reservation_id, version=reservation["version"],
                                  detail={"allocations": allocations, "total": total})
                result = {"reservation_id": reservation_id, "total": total, "allocations": allocations, "usage_ids": usage_ids}
                if outbox is not None:
                    outbox.enqueue_in(connection, topic="park.operation.confirmed", aggregate_id=reservation_id,
                                      payload={"resource_id": reservation["resource_id"], "total": total, "allocations": allocations})
                return result
            return self.idempotency.execute(connection, scope=f"park-confirm:{reservation_id}", request_key=request_key,
                                            request={"allocations": allocations}, operation=operation)

    def cancel_for_fault(self, context: AccessContext, *, resource_id: str, start_at: str, end_at: str,
                         reason: str, request_key: str, outbox=None) -> dict:
        """设备故障：只取消该资源故障窗口内的预约和作业用量，其它能力安排不动。"""
        context.require("write:park-ops")
        if not reason.strip():
            raise ValidationError("故障取消必须说明原因")
        start_at = canonical_instant(start_at); end_at = canonical_instant(end_at)
        with self.database.transaction() as connection:
            def operation() -> dict:
                reservations = connection.execute(
                    "SELECT reservation_id FROM resource_reservations WHERE resource_id=? AND status='confirmed' AND start_at<? AND end_at>?",
                    (resource_id, end_at, start_at)).fetchall()
                cancelled_reservations = []
                for row in reservations:
                    connection.execute("UPDATE resource_reservations SET status='fault_cancelled',version=version+1 WHERE reservation_id=?", (row["reservation_id"],))
                    connection.execute("UPDATE park_usage SET status='cancelled' WHERE reservation_id=?", (row["reservation_id"],))
                    cancelled_reservations.append(row["reservation_id"])
                self.audit.append(connection, actor_id=context.actor_id, action="park-fault-cancel", entity_type="resource_reservations",
                                  entity_id=resource_id, version=1, detail={"window": [start_at, end_at], "reason": reason.strip(),
                                                                            "reservations": cancelled_reservations})
                if outbox is not None:
                    outbox.enqueue_in(connection, topic="park.resource.fault", aggregate_id=resource_id,
                                      payload={"start_at": start_at, "end_at": end_at, "reason": reason.strip(),
                                               "reservations": cancelled_reservations})
                return {"resource_id": resource_id, "cancelled_reservations": cancelled_reservations}
            return self.idempotency.execute(connection, scope=f"park-fault:{resource_id}:{start_at}", request_key=request_key,
                                            request={"start_at": start_at, "end_at": end_at, "reason": reason},
                                            operation=operation)

    def usage_for_tenant(self, context: AccessContext, tenant_org: str, *, start_at: str, end_at: str) -> list[dict]:
        """租户只能查询本企业的作业用量。"""
        context.require("read:park-ops")
        self._assert_org_scope(context, tenant_org)
        start_at = canonical_instant(start_at); end_at = canonical_instant(end_at)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM park_usage WHERE tenant_org=? AND status='confirmed' AND start_at<? AND end_at>? ORDER BY start_at,usage_id",
                (tenant_org, end_at, start_at)).fetchall()
            return [dict(r) for r in rows]

    def usage_in_window(self, *, resource_id: str | None = None, capability: str | None = None,
                        start_at: str = "", end_at: str = "") -> list[dict]:
        """结算内部使用：按资源/能力读取窗口内有效用量。"""
        sql = "SELECT * FROM park_usage WHERE status='confirmed' AND start_at<? AND end_at>?"
        params: list[object] = [canonical_instant(end_at), canonical_instant(start_at)]
        if resource_id is not None:
            sql += " AND resource_id=?"; params.append(resource_id)
        if capability is not None:
            sql += " AND capability=?"; params.append(capability)
        sql += " ORDER BY start_at,usage_id"
        with self.database.connect() as connection:
            return [dict(r) for r in connection.execute(sql, params).fetchall()]

    def _audit_detail(self, connection, reservation_id: str) -> dict:
        row = connection.execute("SELECT detail_json FROM audit_entries WHERE entity_id=? AND action='park-reserve' ORDER BY audit_id DESC LIMIT 1", (reservation_id,)).fetchone()
        if not row:
            raise NotFoundError("预约审计记录缺失")
        import json
        return json.loads(row["detail_json"])

    @staticmethod
    def _clean_tenants(tenants: list[str]) -> list[str]:
        if not tenants or not all(isinstance(t, str) and t.strip() for t in tenants):
            raise ValidationError("至少声明一个参与租户")
        cleaned = sorted({t.strip() for t in tenants})
        return cleaned

    @staticmethod
    def _validate_allocations(allocations: list[dict]) -> list[dict]:
        if not allocations:
            raise ValidationError("作业拆分不能为空")
        result: list[dict] = []
        seen: set[str] = set()
        for item in allocations:
            org = item.get("tenant_org"); qty = item.get("quantity")
            if not isinstance(org, str) or not org.strip():
                raise ValidationError("拆分行缺少租户")
            org = org.strip()
            if org in seen:
                raise ValidationError(f"租户 {org} 在同一作业中出现多次")
            if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
                raise ValidationError("拆分数量必须为正整数")
            seen.add(org); result.append({"tenant_org": org, "quantity": qty})
        return result

    @staticmethod
    def _assert_org_scope(context: AccessContext, tenant_org: str) -> None:
        if context.has_scope("*"):
            return
        if not context.has_scope(f"org:{tenant_org}"):
            raise PermissionDenied("不能越过本企业边界查询")
