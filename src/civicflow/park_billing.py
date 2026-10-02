"""园区服务结算：账期计价、暂停挂起、关账、贷项/补单、费用下钻。

计价依据链：租约/占用（park_usage、租约版本）→ 原始读数（park_meter_readings）
→ 适用价目（price_rules 有效期版本）→ 批准人（park_adjustments）。
维保停机窗口从计时服务中扣除；后签变更只作用于其生效之后的切片；
历史差错不回改已关账费用，而是经审批后以贷项或补单纠正。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN

from .database import Database
from .effective import overlap, hours_between, slice_by_effectivity
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .identifiers import new_id, require_safe
from .idempotency import IdempotencyStore
from .jsonutil import canonical_json
from .ledger import to_minor
from .park_holds import HoldBook
from .park_master import ParkMasterService, tenant_scope
from .park_metering import MeteringService
from .park_ops import ParkOperations
from .security import AccessContext, assert_distinct
from .timeutil import Clock, canonical_instant, parse_instant


BLOCKING_HOLDS = {"reading_conflict", "missing_meter", "maintenance_comp", "price_gap"}
SERVICE_CODES = ("dock", "cold", "charging", "rail")


class _BillingHalt(Exception):
    """计价过程中需要暂停结算：携带待持久化的挂起项。"""

    def __init__(self, holds: list[tuple]):
        super().__init__("暂停结算")
        self.holds = holds


def round_minor(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


@dataclass(frozen=True)
class ParkBilling:
    database: Database
    clock: Clock
    master: ParkMasterService
    ops: ParkOperations
    metering: MeteringService
    holds: HoldBook
    jobs: object
    outbox: object
    idempotency: IdempotencyStore

    # ---- 账期 ----
    def open_period(self, context: AccessContext, *, tenant_org: str, period_start: str,
                    period_end: str, currency: str, request_key: str) -> dict:
        context.require("write:park-billing")
        require_safe(tenant_org, "租户")
        ps = canonical_instant(period_start); pe = canonical_instant(period_end)
        if parse_instant(ps) >= parse_instant(pe):
            raise ValidationError("账期结束必须晚于开始")
        with self.database.transaction() as connection:
            def operation() -> dict:
                period_id = new_id("period")
                connection.execute(
                    "INSERT INTO park_periods(period_id,tenant_org,period_start,period_end,currency,status,opened_at,created_by) VALUES(?,?,?,?,?,'open',?,?)",
                    (period_id, tenant_org, ps, pe, currency, self.clock.now(), context.actor_id))
                # 账期结束时核查缺失抄表：任务持久化，重启后仍按原期限触发
                self.jobs.schedule_in(connection, job_type="park.period_meters", subject_id=period_id, run_at=pe,
                                      payload={"period_id": period_id, "tenant_org": tenant_org})
                return dict(connection.execute("SELECT * FROM park_periods WHERE period_id=?", (period_id,)).fetchone())
            return self.idempotency.execute(connection, scope="park-period-open", request_key=request_key,
                                            request={"tenant_org": tenant_org, "start": ps, "end": pe}, operation=operation)

    def get_period(self, period_id: str) -> dict:
        return self._period(period_id)

    def list_periods(self, context: AccessContext, tenant_org: str | None = None) -> list[dict]:
        context.require("read:park-billing")
        sql = "SELECT * FROM park_periods"; params: list[object] = []
        if tenant_org is not None:
            self._assert_org(context, tenant_org)
            sql += " WHERE tenant_org=?"; params.append(tenant_org)
        elif not context.has_scope("*"):
            raise PermissionDenied("租户查询必须指定本企业")
        sql += " ORDER BY period_start,period_id"
        with self.database.connect() as connection:
            return [dict(r) for r in connection.execute(sql, params).fetchall()]

    # ---- 计价 ----
    def generate(self, context: AccessContext, period_id: str, *, request_key: str) -> dict:
        context.require("write:park-billing")
        period = self._period(period_id)
        if period["status"] != "open":
            raise ConflictError("账期已关账，历史差错须通过贷项或补单纠正")
        with self.database.transaction() as connection:
            def operation() -> dict:
                pending: list[tuple] = []
                period_rows = self._ensure_meter_readings(connection, period)
                pending.extend(period_rows)
                blocking = self._blocking_holds(connection, period)
                if blocking or pending:
                    raise _BillingHalt(pending)
                connection.execute("DELETE FROM park_charge_lines WHERE period_id=? AND status='billed'", (period_id,))
                specs: list[dict] = []
                specs.extend(self._rent_specs(connection, period))
                specs.extend(self._usage_specs(connection, period, pending))
                specs.extend(self._meter_specs(connection, period, pending))
                lines = []
                for spec in specs:
                    if spec["amount_minor"] == 0:
                        continue
                    line_id = new_id("line")
                    connection.execute(
                        "INSERT INTO park_charge_lines(line_id,period_id,line_key,tenant_org,service_code,amount_minor,currency,basis,evidence_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,'billed',?)",
                        (line_id, period_id, spec["line_key"], period["tenant_org"], spec["service_code"],
                         spec["amount_minor"], period["currency"], spec["basis"], canonical_json(spec["evidence"]), self.clock.now()))
                    lines.append({"line_id": line_id, "service_code": spec["service_code"],
                                  "amount_minor": spec["amount_minor"], "basis": spec["basis"]})
                return {"period_id": period_id, "lines": lines, "total_minor": sum(l["amount_minor"] for l in lines)}
            try:
                return self.idempotency.execute(connection, scope=f"park-generate:{period_id}", request_key=request_key,
                                                request={"period_id": period_id}, operation=operation)
            except _BillingHalt as halt:
                connection.rollback()
                kinds: set[str] = set()
                for kind, ref_key, detail, tenant in halt.holds:
                    self.holds.open(kind, ref_key, detail=detail, tenant_org=tenant, period_id=period_id)
                    kinds.add(kind)
                kinds |= self._blocking_holds_now(period)
                raise ConflictError("存在未决挂起项，暂停结算: " + ", ".join(sorted(kinds)))

    def _rent_specs(self, connection, period: dict) -> list[dict]:
        ps, pe, tenant = period["period_start"], period["period_end"], period["tenant_org"]
        versions = self.master.store.active_during("leases", ps, pe, tenant_org=tenant)
        specs: list[dict] = []
        by_series: dict[str, list[dict]] = {}
        for version in versions:
            by_series.setdefault(version["series_id"], []).append(version)
        for series_id, rows in by_series.items():
            for version in sorted(rows, key=lambda r: r["version"]):
                window = overlap(version["valid_from"], version["valid_to"] or pe, ps, pe)
                if window is None:
                    continue
                hours = Decimal(str(hours_between(*window)))
                amount = round_minor(Decimal(version["rent_minor"]) * hours)
                specs.append({"line_key": f"rent:{series_id}:v{version['version']}", "service_code": f"rent:{version['unit_code']}",
                              "amount_minor": amount, "basis": "lease_hourly",
                              "evidence": {"lease_series_id": series_id, "lease_version": version["version"],
                                           "lease_record_id": version["record_id"], "unit_code": version["unit_code"],
                                           "window": list(window), "hours": str(hours),
                                           "rent_minor_per_hour": version["rent_minor"]}})
        return specs

    def _usage_specs(self, connection, period: dict, pending: list[tuple]) -> list[dict]:
        ps, pe, tenant = period["period_start"], period["period_end"], period["tenant_org"]
        usages = [u for u in self.ops.usage_in_window(start_at=ps, end_at=pe) if u["tenant_org"] == tenant]
        metered_pairs: set[tuple[str, str]] = set(self._metered_pairs(connection, ps, pe, usages))
        specs: list[dict] = []

        def halt(kind: str, ref_key: str, detail: dict) -> None:
            pending.append((kind, ref_key, detail, tenant))
            raise _BillingHalt(pending)

        for usage in usages:
            if (usage["resource_id"], usage["capability"]) in metered_pairs:
                continue  # 该能力按计量读数结算，避免重复
            window = overlap(usage["start_at"], usage["end_at"], ps, pe)
            if window is None:
                continue
            service_code = usage["capability"]
            catalog = self._catalog_at(connection, service_code, window[0])
            if catalog is None:
                halt("price_gap", f"{period['period_id']}:{usage['usage_id']}",
                     {"reason": "服务目录中找不到该服务", "service_code": service_code, "usage_id": usage["usage_id"]})
            prices = self.master.store.active_during("price_rules", window[0], window[1], service_code=service_code)
            if not prices:
                halt("price_gap", f"{period['period_id']}:{usage['usage_id']}",
                     {"reason": "账期窗口内找不到适用价格", "service_code": service_code,
                      "window": list(window), "usage_id": usage["usage_id"]})
            maintenance = [m for m in self.master.store.active_during("maintenance", window[0], window[1])
                           if m["resource_id"] == usage["resource_id"] and m["service_code"] == service_code]
            billable_windows = self._subtract_maintenance(window, maintenance)
            amount = 0; slices_detail = []
            for index, (w_start, w_end) in enumerate(billable_windows):
                for s_start, s_end, rule in slice_by_effectivity(w_start, w_end, prices):
                    if rule is None:
                        halt("price_gap", f"{period['period_id']}:{usage['usage_id']}:{index}",
                             {"service_code": service_code, "window": [s_start, s_end]})
                    hours = Decimal(str(hours_between(s_start, s_end)))
                    units = Decimal(usage["quantity"]) * hours if rule["mode"] == "hour" else Decimal(usage["quantity"])
                    piece = round_minor(Decimal(rule["price_minor"]) * units)
                    amount += piece
                    slices_detail.append({"window": [s_start, s_end], "hours": str(hours),
                                          "price_record_id": rule["record_id"], "price_version": rule["version"],
                                          "price_minor": rule["price_minor"], "mode": rule["mode"], "amount_minor": piece})
            specs.append({"line_key": f"usage:{usage['usage_id']}", "service_code": service_code, "amount_minor": amount,
                          "basis": "capability_usage",
                          "evidence": {"usage_id": usage["usage_id"], "reservation_id": usage["reservation_id"],
                                       "resource_id": usage["resource_id"], "quantity": usage["quantity"],
                                       "window": list(window), "maintenance": [{"record_id": m["record_id"],
                                                                               "window": [m["valid_from"], m["valid_to"]]} for m in maintenance],
                                       "slices": slices_detail}})
        return specs

    def _meter_specs(self, connection, period: dict, pending: list[tuple]) -> list[dict]:
        ps, pe, tenant = period["period_start"], period["period_end"], period["tenant_org"]
        specs: list[dict] = []
        all_usages = self.ops.usage_in_window(start_at=ps, end_at=pe)

        def halt(kind: str, ref_key: str, detail: dict) -> None:
            pending.append((kind, ref_key, detail, tenant))
            raise _BillingHalt(pending)

        for resource_id, service_code in self._metered_pairs(connection, ps, pe, all_usages):
            point = next((p for p in self.master.store.active_during("meter_points", ps, pe)
                          if p["resource_id"] == resource_id and p["service_code"] == service_code), None)
            if point is None:
                continue
            participant_usages = [u for u in all_usages if u["resource_id"] == resource_id
                                  and u["capability"] == service_code]
            tenant_qty = sum(u["quantity"] for u in participant_usages if u["tenant_org"] == tenant)
            if tenant_qty == 0:
                continue  # 共享计量点：本租户本期无占用则不分摊
            prices = self.master.store.active_during("price_rules", ps, pe, service_code=service_code)
            if not prices:
                halt("price_gap", f"{period['period_id']}:meter:{point['record_id']}",
                     {"service_code": service_code, "meter_point_id": point["record_id"]})
            usage = self.metering.consumption(point["record_id"], period_start=ps, period_end=pe)
            if usage is None:
                halt("missing_meter", f"{period['period_id']}:{point['record_id']}",
                     {"meter_point_id": point["record_id"], "meter_point_code": point["code"], "resource_id": resource_id})
            # 共享计量点按各租户已拆清的实际数量比例分摊总用量
            total_qty = sum(u["quantity"] for u in participant_usages)
            share = Decimal(tenant_qty) / Decimal(total_qty)
            total_consumption = Decimal(usage["consumption"])
            tenant_consumption = (total_consumption * share).quantize(Decimal("0.0001"))
            closing_at = self.metering.get_reading(usage["closing_meter_code"])["read_at"]
            rule = max((p for p in prices if parse_instant(p["valid_from"]) <= parse_instant(closing_at)),
                       key=lambda p: (parse_instant(p["valid_from"]), p["version"]), default=None)
            if rule is None:
                rule = sorted(prices, key=lambda p: (parse_instant(p["valid_from"]), p["version"]))[-1]
            amount = round_minor(Decimal(rule["price_minor"]) * tenant_consumption)
            specs.append({"line_key": f"meter:{point['record_id']}:{usage['closing_meter_code']}", "service_code": service_code,
                          "amount_minor": amount, "basis": "meter_consumption_allocated",
                          "evidence": {"meter_point_id": point["record_id"], "meter_point_code": point["code"],
                                       "resource_id": resource_id, "consumption": str(tenant_consumption),
                                       "meter_total_consumption": str(total_consumption), "allocation_share": str(share.quantize(Decimal("0.000001"))),
                                       "tenant_quantity": tenant_qty, "total_quantity": total_qty,
                                       "participants": [{"tenant_org": u["tenant_org"], "quantity": u["quantity"],
                                                         "usage_id": u["usage_id"]} for u in participant_usages],
                                       "opening_meter_code": usage["opening_meter_code"], "opening_value": usage["opening_value"],
                                       "closing_meter_code": usage["closing_meter_code"], "closing_value": usage["closing_value"],
                                       "price_record_id": rule["record_id"], "price_version": rule["version"],
                                       "price_minor": rule["price_minor"], "mode": rule["mode"]}})
        return specs

    # ---- 贷项 / 补单 ----
    def request_adjustment(self, context: AccessContext, *, period_id: str, kind: str, amount: str,
                           reason: str, request_key: str, hold_ref: str | None = None) -> dict:
        context.require("request:park-adjust")
        if kind not in {"credit", "supplement"}:
            raise ValidationError("调整类型必须是 credit 或 supplement")
        if not reason.strip():
            raise ValidationError("调整必须说明原因")
        amount_minor = to_minor(amount)
        if amount_minor <= 0:
            raise ValidationError("调整金额必须大于零")
        period = self._period(period_id)
        with self.database.transaction() as connection:
            def operation() -> dict:
                adjustment_id = new_id("adjustment")
                connection.execute(
                    "INSERT INTO park_adjustments(adjustment_id,period_id,tenant_org,kind,amount_minor,currency,reason,status,requested_by,hold_ref,request_key,created_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?)",
                    (adjustment_id, period_id, period["tenant_org"], kind, amount_minor, period["currency"],
                     reason.strip(), context.actor_id, hold_ref, request_key, self.clock.now()))
                return dict(connection.execute("SELECT * FROM park_adjustments WHERE adjustment_id=?", (adjustment_id,)).fetchone())
            return self.idempotency.execute(connection, scope="park-adjustment-request", request_key=request_key,
                                            request={"period_id": period_id, "kind": kind, "amount_minor": amount_minor, "reason": reason, "hold_ref": hold_ref},
                                            operation=operation)

    def decide_adjustment(self, context: AccessContext, *, adjustment_id: str, decision: str, reason: str) -> dict:
        context.require("approve:park-adjust")
        if decision not in {"approved", "rejected"}:
            raise ValidationError("审批结论必须是 approved 或 rejected")
        if not reason.strip():
            raise ValidationError("审批必须说明依据")
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM park_adjustments WHERE adjustment_id=?", (adjustment_id,)).fetchone()
            if not row:
                raise NotFoundError("调整申请不存在")
            if row["status"] != "pending":
                raise ConflictError(f"申请状态为 {row['status']}")
            # 录入人不能批准自己的减免申请
            assert_distinct(row["requested_by"], context.actor_id)
            connection.execute("UPDATE park_adjustments SET status=?,reviewed_by=?,decided_at=? WHERE adjustment_id=?",
                               (decision, context.actor_id, self.clock.now(), adjustment_id))
            result = {"adjustment_id": adjustment_id, "decision": decision, "reviewed_by": context.actor_id}
            if decision == "rejected":
                return result
            line_id = new_id("line")
            basis = "credit_memo" if row["kind"] == "credit" else "supplement_bill"
            evidence = {"adjustment_id": adjustment_id, "requested_by": row["requested_by"],
                        "approved_by": context.actor_id, "reason": row["reason"], "period_status_when_applied":
                        connection.execute("SELECT status FROM park_periods WHERE period_id=?", (row["period_id"],)).fetchone()["status"]}
            connection.execute(
                "INSERT INTO park_charge_lines(line_id,period_id,line_key,tenant_org,service_code,amount_minor,currency,basis,evidence_json,status,created_at,approval_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (line_id, row["period_id"], f"adjustment:{adjustment_id}", row["tenant_org"], f"adjustment:{row['kind']}",
                 row["amount_minor"], row["currency"], basis, canonical_json(evidence), "posted", self.clock.now(), adjustment_id))
            connection.execute("UPDATE park_adjustments SET line_id=? WHERE adjustment_id=?", (line_id, adjustment_id))
            # 资金分录：贷项冲减应收，补单追加应收（历史账期也立即入账，不回改原费用）
            direction = "credit" if row["kind"] == "credit" else "debit"
            connection.execute(
                "INSERT INTO journal_entries(entry_id,journal_key,account,currency,amount_minor,direction,reference,occurred_at,posted_by) VALUES(?,?,?,?,?,?,?,?,?)",
                (new_id("entry"), f"park:{row['tenant_org']}", f"adjustment:{row['kind']}", row["currency"],
                 row["amount_minor"], direction, line_id, self.clock.now(), context.actor_id))
            # 关联挂起项（如维保补偿）在批准落账后解除
            if row["hold_ref"]:
                kind, _, ref_key = row["hold_ref"].partition(":")
                changed = connection.execute("UPDATE park_holds SET status='resolved',resolved_at=? WHERE kind=? AND ref_key=? AND status='open'",
                                             (self.clock.now(), kind, ref_key)).rowcount
                if changed:
                    result["resolved_hold"] = row["hold_ref"]
            self.outbox.enqueue_in(connection, topic="park.adjustment.decided", aggregate_id=adjustment_id,
                                   payload={"period_id": row["period_id"], "kind": row["kind"], "decision": decision})
            result["line_id"] = line_id
            return result

    # ---- 关账 ----
    def close_period(self, context: AccessContext, *, period_id: str, request_key: str) -> dict:
        context.require("close:park-billing")
        period = self._period(period_id)
        if period["status"] != "open":
            raise ConflictError("账期不是开放状态")
        with self.database.transaction() as connection:
            def operation() -> dict:
                blocking = self._blocking_holds(connection, period)
                if blocking:
                    raise ConflictError("存在未决挂起项，不能关账: " + ", ".join(sorted(blocking)))
                lines = connection.execute("SELECT * FROM park_charge_lines WHERE period_id=? AND status='billed'", (period_id,)).fetchall()
                posted = []
                for line in lines:
                    connection.execute(
                        "INSERT INTO journal_entries(entry_id,journal_key,account,currency,amount_minor,direction,reference,occurred_at,posted_by) VALUES(?,?,?,?,?,?,?,?,?)",
                        (new_id("entry"), f"park:{period['tenant_org']}", line["service_code"], line["currency"],
                         line["amount_minor"], "debit", line["line_id"], self.clock.now(), context.actor_id))
                    connection.execute("UPDATE park_charge_lines SET status='posted' WHERE line_id=?", (line["line_id"],))
                    posted.append(line["line_id"])
                connection.execute("UPDATE park_periods SET status='closed',closed_at=? WHERE period_id=?", (self.clock.now(), period_id))
                self.outbox.enqueue_in(connection, topic="park.period.closed", aggregate_id=period_id,
                                       payload={"tenant_org": period["tenant_org"], "lines": len(posted)})
                return {"period_id": period_id, "status": "closed", "posted_lines": len(posted)}
            return self.idempotency.execute(connection, scope=f"park-close:{period_id}", request_key=request_key,
                                            request={"period_id": period_id}, operation=operation)

    # ---- 查询与下钻 ----
    def period_lines(self, context: AccessContext, period_id: str) -> list[dict]:
        context.require("read:park-billing")
        period = self._period(period_id)
        self._assert_org(context, period["tenant_org"])
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM park_charge_lines WHERE period_id=? ORDER BY created_at,line_id", (period_id,)).fetchall()
            return [self._line_with_evidence(r) for r in rows]

    def line_detail(self, context: AccessContext, line_id: str) -> dict:
        """从一笔费用直接下钻：占用/租约记录、原始读数、适用价目版本、批准人。"""
        context.require("read:park-billing")
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM park_charge_lines WHERE line_id=?", (line_id,)).fetchone()
            if not row:
                raise NotFoundError("费用行不存在")
            self._assert_org(context, row["tenant_org"])
            detail = self._line_with_evidence(row)
            evidence = detail["evidence"]
            drill: dict = {}
            if "lease_record_id" in evidence:
                drill["lease_version"] = self.master.store.get(evidence["lease_record_id"])
            if "usage_id" in evidence:
                usage = connection.execute("SELECT * FROM park_usage WHERE usage_id=?", (evidence["usage_id"],)).fetchone()
                drill["usage"] = dict(usage) if usage else None
                drill["reservation_window"] = evidence.get("window")
            if "closing_meter_code" in evidence:
                drill["readings"] = {"opening": self._reading_or_none(evidence["opening_meter_code"]),
                                     "closing": self._reading_or_none(evidence["closing_meter_code"])}
            if "price_record_id" in evidence:
                drill["price"] = self.master.store.get(evidence["price_record_id"])
            if evidence.get("slices"):
                drill["price_versions"] = [self.master.store.get(s["price_record_id"]) for s in evidence["slices"]]
            if row["approval_id"]:
                approval = connection.execute("SELECT * FROM park_adjustments WHERE adjustment_id=?", (row["approval_id"],)).fetchone()
                drill["approval"] = dict(approval) if approval else None
            detail["drill_down"] = drill
            return detail

    def tenant_balance(self, context: AccessContext, tenant_org: str, *, currency: str) -> int:
        context.require("read:park-billing")
        self._assert_org(context, tenant_org)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(CASE direction WHEN 'debit' THEN amount_minor ELSE -amount_minor END),0) AS value FROM journal_entries WHERE journal_key=? AND currency=?",
                (f"park:{tenant_org}", currency)).fetchone()
            return int(row["value"])

    def open_holds(self, context: AccessContext, *, tenant_org: str | None = None, kind: str | None = None) -> list[dict]:
        context.require("read:park-billing")
        if tenant_org is not None:
            self._assert_org(context, tenant_org)
        elif not context.has_scope("*"):
            raise PermissionDenied("租户查询必须限定本企业")
        return self.holds.list_open(kind=kind, tenant_org=tenant_org)

    # ---- 定时任务处理（重启后按原期限继续，无需重新登记）----
    def process_due_jobs(self, *, limit: int = 20) -> list[dict]:
        results: list[dict] = []
        for job in self.jobs.claim_due(limit=limit):
            try:
                if job["job_type"] == "park.maintenance_comp":
                    results.append(self._process_maintenance_job(job))
                elif job["job_type"] == "park.lease_expiring":
                    results.append(self._process_lease_expiring_job(job))
                elif job["job_type"] == "park.period_meters":
                    results.append(self._process_missing_meter_job(job))
                else:
                    self.jobs.finish(job["job_id"])
                    results.append({"job_id": job["job_id"], "ignored": True})
                self.jobs.finish(job["job_id"])
            except Exception as exc:  # 保留任务按退避重试
                from datetime import timedelta
                retry_at = (parse_instant(self.clock.now()) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
                self.jobs.retry(job["job_id"], error=str(exc), retry_at=retry_at)
                results.append({"job_id": job["job_id"], "retried": str(exc)})
        return results

    def _process_maintenance_job(self, job: dict) -> dict:
        payload = json.loads(job["payload_json"])
        opened = []; credits = []
        for tenant in payload.get("tenants") or []:
            ref_key = f"{job['subject_id']}:{tenant}"
            hold = self.holds.open("maintenance_comp", ref_key,
                                   detail={"maintenance_record_id": job["subject_id"], "resource_id": payload["resource_id"],
                                           "service_code": payload["service_code"], "comp_minor": payload["comp_minor"],
                                           "window": [payload["start_at"], payload["end_at"]]}, tenant_org=tenant)
            opened.append(hold["hold_id"])
            # 自动生成待审批贷项申请；申请人为系统维保任务，批准仍须由另一名有权限的人完成
            period_id = self._open_period_overlapping(tenant, payload["start_at"], payload["end_at"])
            if period_id is not None:
                credit_id = self._auto_maintenance_credit(period_id, tenant, payload["comp_minor"],
                                                          f"maintenance_comp:{ref_key}", job["subject_id"])
                if credit_id:
                    credits.append(credit_id)
        self.outbox.enqueue(topic="park.maintenance.compensation_due", aggregate_id=job["subject_id"],
                            payload={"tenants": payload.get("tenants") or [], "comp_minor": payload["comp_minor"], "credits": credits})
        return {"job_id": job["job_id"], "maintenance_comp_holds": opened, "pending_credits": credits}

    def _open_period_overlapping(self, tenant: str, start_at: str, end_at: str) -> str | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT period_id FROM park_periods WHERE tenant_org=? AND status='open' AND period_start<? AND period_end>? ORDER BY period_start LIMIT 1",
                (tenant, canonical_instant(end_at), canonical_instant(start_at))).fetchone()
            return row["period_id"] if row else None

    def _auto_maintenance_credit(self, period_id: str, tenant: str, comp_minor: int, hold_ref: str, maintenance_record_id: str) -> str | None:
        with self.database.transaction() as connection:
            existing = connection.execute("SELECT adjustment_id FROM park_adjustments WHERE hold_ref=?", (hold_ref,)).fetchone()
            if existing:
                return existing["adjustment_id"]
            period = connection.execute("SELECT currency FROM park_periods WHERE period_id=?", (period_id,)).fetchone()
            adjustment_id = new_id("adjustment")
            connection.execute(
                "INSERT INTO park_adjustments(adjustment_id,period_id,tenant_org,kind,amount_minor,currency,reason,status,requested_by,hold_ref,request_key,created_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?)",
                (adjustment_id, period_id, tenant, "credit", comp_minor, period["currency"],
                 f"维保停机补偿 {maintenance_record_id}", "system:maintenance", hold_ref,
                 f"auto:{hold_ref}", self.clock.now()))
            return adjustment_id

    def _process_lease_expiring_job(self, job: dict) -> dict:
        payload = json.loads(job["payload_json"])
        hold = self.holds.open("lease_expiring", payload["lease_series_id"],
                               detail=payload, tenant_org=payload.get("tenant_org"))
        self.outbox.enqueue(topic="park.lease.expiring", aggregate_id=payload["lease_series_id"], payload=payload)
        return {"job_id": job["job_id"], "hold_id": hold["hold_id"], "informational": True}

    def _process_missing_meter_job(self, job: dict) -> dict:
        payload = json.loads(job["payload_json"])
        period = self._period(payload["period_id"])
        with self.database.connect() as connection:
            pending = self._ensure_meter_readings(connection, period)
        for kind, ref_key, detail, tenant in pending:
            self.holds.open(kind, ref_key, detail=detail, tenant_org=tenant, period_id=period["period_id"])
        return {"job_id": job["job_id"], "checked": True, "holds_opened": len(pending)}

    # ---- 内部辅助 ----
    def _ensure_meter_readings(self, connection, period: dict) -> list[tuple]:
        """返回需要开启的缺抄表挂起项（不在事务内写入）。"""
        ps, pe, tenant = period["period_start"], period["period_end"], period["tenant_org"]
        needed_resources = {r[0] for r in connection.execute(
            "SELECT DISTINCT resource_id FROM park_usage WHERE tenant_org=? AND status='confirmed' AND start_at<? AND end_at>?",
            (tenant, pe, ps)).fetchall()}
        pending: list[tuple] = []
        point_ids = {p["record_id"] for p in self.master.store.active_during("meter_points", ps, pe)
                     if p["resource_id"] in needed_resources}
        for point in self.master.store.active_during("meter_points", ps, pe):
            if point["resource_id"] not in needed_resources:
                continue
            last = self.metering.last_reading_before(connection, point["record_id"], pe, strict=True)
            if last is None or last["read_at"] <= ps:
                pending.append(("missing_meter", f"{period['period_id']}:{point['record_id']}",
                                {"meter_point_id": point["record_id"], "meter_point_code": point["code"],
                                 "resource_id": point["resource_id"]}, tenant))
        for conflict in connection.execute("SELECT ref_key,detail_json FROM park_holds WHERE status='open' AND kind='reading_conflict'").fetchall():
            detail = json.loads(conflict["detail_json"])
            if detail.get("meter_point_id") in point_ids:
                pending.append(("reading_conflict", conflict["ref_key"], detail, tenant))
        return pending

    def _blocking_holds_now(self, period: dict) -> set[str]:
        with self.database.connect() as connection:
            return self._blocking_holds(connection, period)

    def _blocking_holds(self, connection, period: dict) -> set[str]:
        rows = connection.execute(
            "SELECT kind FROM park_holds WHERE status='open' AND kind IN (%s) AND (tenant_org=? OR period_id=?)" %
            ",".join("?" * len(BLOCKING_HOLDS)),
            (*sorted(BLOCKING_HOLDS), period["tenant_org"], period["period_id"])).fetchall()
        return {r["kind"] for r in rows}

    def _metered_pairs(self, connection, ps: str, pe: str, usages: list[dict] | None) -> list[tuple[str, str]]:
        usages = self.ops.usage_in_window(start_at=ps, end_at=pe) if usages is None else usages
        pairs: set[tuple[str, str]] = set()
        points = self.master.store.active_during("meter_points", ps, pe)
        for point in points:
            catalog = self._catalog_at(connection, point["service_code"], ps)
            if catalog is not None and catalog.get("metered"):
                if any(u["resource_id"] == point["resource_id"] and u["capability"] == point["service_code"] for u in usages):
                    pairs.add((point["resource_id"], point["service_code"]))
        return sorted(pairs)

    def _catalog_at(self, connection, service_code: str, instant: str) -> dict | None:
        rows = self.master.store.active_at("service_catalog", instant, service_code=service_code)
        if not rows:
            return None
        return sorted(rows, key=lambda r: r["valid_from"])[-1]

    @staticmethod
    def _subtract_maintenance(window: tuple[str, str], maintenance: list[dict]) -> list[tuple[str, str]]:
        """从计费窗口中扣除维保停机区间（停机不计服务量）。"""
        pieces = [window]
        for record in sorted(maintenance, key=lambda m: m["valid_from"]):
            down = overlap(record["valid_from"], record["valid_to"], window[0], window[1])
            if down is None:
                continue
            next_pieces = []
            for start, end in pieces:
                cut = overlap(start, end, down[0], down[1])
                if cut is None:
                    next_pieces.append((start, end)); continue
                if parse_instant(cut[0]) > parse_instant(start):
                    next_pieces.append((start, cut[0]))
                if parse_instant(end) > parse_instant(cut[1]):
                    next_pieces.append((cut[1], end))
            pieces = next_pieces
        return pieces

    def _period(self, period_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM park_periods WHERE period_id=?", (period_id,)).fetchone()
            if not row:
                raise NotFoundError("账期不存在")
            return dict(row)

    def _reading_or_none(self, meter_code: str | None) -> dict | None:
        if meter_code is None:
            return None
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM park_meter_readings WHERE meter_code=?", (meter_code,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _line_with_evidence(row) -> dict:
        result = {k: row[k] for k in row.keys()}
        result["evidence"] = json.loads(row["evidence_json"])
        return result

    @staticmethod
    def _assert_org(context: AccessContext, tenant_org: str) -> None:
        tenant_scope(context, tenant_org)
