"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .application import CivicFlow
from .cases import CaseService
from .errors import ConflictError, PermissionDenied
from .security import AccessContext


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def demo(app: CivicFlow) -> dict:
    context = AccessContext.system("demo-operator")
    cases = CaseService(app.repository)
    created = cases.create(context, {"case_type": "协同事项", "subject": "示例联合处置", "owner_org": "org:demo", "priority": "high", "opened_at": app.clock.now()}, request_key="demo-case")
    accepted = app.inbox.receive(source="demo", source_key=created["entity_id"], sequence=1, payload={"kind": "opened"}, occurred_at=app.clock.now())
    reservation = app.reservations.reserve(resource_id="room:joint", subject_id=created["entity_id"], quantity=2, capacity=10, start_at="2026-09-28T10:00:00+08:00", end_at="2026-09-28T11:00:00+08:00", actor=context.actor_id)
    debit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="debit", reference="demo-debit", actor=context.actor_id)
    credit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="credit", reference="demo-credit", actor=context.actor_id)
    message = app.outbox.enqueue(topic="case.opened", aggregate_id=created["entity_id"], payload={"case_id": created["entity_id"]})
    return {"case": created, "inbox": accepted, "reservation": reservation, "entries": [debit, credit], "balance": app.ledger.balance("demo", currency="CNY"), "message_id": message, "verification": app.verify()}


def settlement_demo(app: CivicFlow) -> dict:
    """广州公铁联运枢纽高标仓：从仓位租约到园区服务结算的端到端闭环。"""
    admin = AccessContext.system("ops-admin")
    svc = app.settlement
    notes: list[str] = []

    # 1. 主数据：两家首批租户、冷库仓位单元与交付验收（全部带有效期间）
    svc.register_tenant(admin, tenant_id="tenant-a", org_id="org:a", name="甲企业", valid_from="2026-09-01T00:00:00+08:00")
    svc.register_tenant(admin, tenant_id="tenant-b", org_id="org:b", name="乙企业", valid_from="2026-09-01T00:00:00+08:00")
    svc.register_unit(admin, unit_id="unit-cold-1", code="COLD-01", unit_type="cold", capacity_qty=500, capacity_unit="pallet", valid_from="2026-09-01T00:00:00+08:00")
    svc.register_acceptance(admin, unit_id="unit-cold-1", delivered_at="2026-09-01T08:00:00+08:00", accepted_at="2026-09-01T10:00:00+08:00", result="passed", note="高标仓交付验收通过")

    # 2. 租约 v1：1000 元/日；09-25 签署变更为 1200 元/日，变更只影响之后账单
    svc.sign_lease(admin, lease_id="lease-cold-1", unit_id="unit-cold-1", tenant_id="tenant-a", valid_from="2026-09-01T00:00:00+08:00", valid_to=None,
                   rate_minor=100000, currency="CNY", billing_unit="day")
    svc.sign_lease(admin, lease_id="lease-cold-1", unit_id="unit-cold-1", tenant_id="tenant-a", valid_from="2026-09-25T00:00:00+08:00", valid_to="2026-10-01T00:00:00+08:00",
                   rate_minor=120000, currency="CNY", billing_unit="day", supersedes=1)

    # 3. 服务目录与价格（带有效期；制冷 09-15 调价，跨账期分段取价）
    svc.register_service(admin, service_code="refrigeration", name="冷库制冷", service_kind="refrigeration", unit_of_measure="day", metered=False, valid_from="2026-09-01T00:00:00+08:00")
    svc.register_price(admin, service_code="refrigeration", rate_minor=20000, currency="CNY", unit_of_measure="day", charge_mode="time", valid_from="2026-09-01T00:00:00+08:00")
    svc.register_price(admin, service_code="refrigeration", rate_minor=22000, currency="CNY", unit_of_measure="day", charge_mode="time", valid_from="2026-09-15T00:00:00+08:00")
    svc.register_service(admin, service_code="cold-power", name="冷机用电", service_kind="refrigeration", unit_of_measure="kWh", metered=True, valid_from="2026-09-01T00:00:00+08:00")
    svc.register_price(admin, service_code="cold-power", rate_minor=120, currency="CNY", unit_of_measure="kWh", charge_mode="usage", valid_from="2026-09-01T00:00:00+08:00")
    svc.register_service(admin, service_code="dock", name="月台作业", service_kind="dock", unit_of_measure="slot-hour", metered=False, valid_from="2026-09-01T00:00:00+08:00")
    svc.register_price(admin, service_code="dock", rate_minor=5000, currency="CNY", unit_of_measure="slot-hour", charge_mode="time", valid_from="2026-09-01T00:00:00+08:00")
    svc.register_service(admin, service_code="charging", name="充电服务", service_kind="charging", unit_of_measure="kWh", metered=False, valid_from="2026-09-01T00:00:00+08:00")
    svc.register_price(admin, service_code="charging", rate_minor=120, currency="CNY", unit_of_measure="kWh", charge_mode="usage", valid_from="2026-09-01T00:00:00+08:00")
    svc.register_service(admin, service_code="rail", name="铁路接驳", service_kind="rail", unit_of_measure="path-hour", metered=False, valid_from="2026-09-01T00:00:00+08:00")
    svc.register_price(admin, service_code="rail", rate_minor=30000, currency="CNY", unit_of_measure="path-hour", charge_mode="time", valid_from="2026-09-01T00:00:00+08:00")

    # 4. 冷机电表计量点；登记 10-01 抄表期限（持久任务，重启后仍按原期限出现）
    svc.register_meter(admin, meter_code="M-COLD-01", meter_kind="electricity", unit_of_measure="kWh", unit_id="unit-cold-1", service_code="cold-power", valid_from="2026-09-01T00:00:00+08:00")
    svc.register_reading_due(admin, meter_code="M-COLD-01", due_at="2026-10-01T08:00:00+08:00")

    # 5. 冷库 09-10 至 09-12 停机检修：停机期间不计制冷费，补偿任务持久化
    svc.record_outage(admin, outage_id="outage-0910", target_type="unit", target_id="unit-cold-1", service_code="refrigeration",
                      start_at="2026-09-10T00:00:00+08:00", end_at="2026-09-12T00:00:00+08:00", reason="压缩机维保")

    # 6. 9 月账期
    september = svc.open_period(admin, period_start="2026-09-01T00:00:00+08:00", period_end="2026-10-01T00:00:00+08:00")

    # 7. 拼单作业：月台、充电按能力预约，实际数量逐户拆清，不再全落到一家
    dock = svc.book_capacity(admin, service_code="dock", resource_id="dock-3", start_at="2026-09-20T08:00:00+08:00", end_at="2026-09-20T12:00:00+08:00", capacity_total=10, organizer_tenant_id="tenant-a")
    svc.allocate(admin, booking_id=dock["booking_id"], tenant_id="tenant-a", quantity=6)
    svc.allocate(admin, booking_id=dock["booking_id"], tenant_id="tenant-b", quantity=4)
    charging = svc.book_capacity(admin, service_code="charging", resource_id="charger-bank-1", start_at="2026-09-20T08:00:00+08:00", end_at="2026-09-20T12:00:00+08:00", capacity_total=500, organizer_tenant_id="tenant-a")
    svc.allocate(admin, booking_id=charging["booking_id"], tenant_id="tenant-a", quantity=300)
    svc.allocate(admin, booking_id=charging["booking_id"], tenant_id="tenant-b", quantity=200)
    rail = svc.book_capacity(admin, service_code="rail", resource_id="rail-line-2", start_at="2026-09-22T06:00:00+08:00", end_at="2026-09-22T09:00:00+08:00", capacity_total=2, organizer_tenant_id="tenant-a")
    svc.allocate(admin, booking_id=rail["booking_id"], tenant_id="tenant-a", quantity=2)

    # 8. 抄表：重复编号同读数判重；不同读数挂争议并暂停结算，处理后恢复
    reader = AccessContext("meter-reader", permissions=frozenset({"settle:reading"}))
    svc.record_reading(reader, meter_code="M-COLD-01", read_at="2026-09-01T08:00:00+08:00", reading_value="0")
    svc.record_reading(reader, meter_code="M-COLD-01", read_at="2026-09-15T08:00:00+08:00", reading_value="1200")
    again = svc.record_reading(reader, meter_code="M-COLD-01", read_at="2026-09-15T08:00:00+08:00", reading_value="1200")
    notes.append(f"重复抄表判重: {again['status']}")
    try:
        svc.record_reading(reader, meter_code="M-COLD-01", read_at="2026-09-15T08:00:00+08:00", reading_value="1250")
    except ConflictError as exc:
        notes.append(f"读数不一致暂停结算: {exc}")
    svc.record_reading(reader, meter_code="M-COLD-01", read_at="2026-09-30T08:00:00+08:00", reading_value="2000")

    # 9. 进程重启视角：到期任务（维保补偿、缺抄表、租约到期）仍按原期限登记，无需人工重新登记
    restarted = CivicFlow.open(Path(app.database.path), fixed_now=app.clock.fixed)
    waiting = [r["job_id"] for r in restarted.database.connect().execute(
        "SELECT job_id FROM scheduled_jobs WHERE job_type IN ('outage-compensation','missing-reading','lease-expiry') ORDER BY job_id").fetchall()]
    notes.append(f"重启后持久任务仍按原期限登记: {sorted(waiting)}")

    # 10. 争议未处理时禁止出账；处理争议（维持原读数）后恢复
    try:
        svc.generate_charges(admin, period_id=september["period_id"])
    except ConflictError as exc:
        notes.append(f"争议未处理出账被拒: {exc}")
    disputes = svc.outstanding_disputes(admin)
    svc.resolve_reading_dispute(admin, dispute_id=disputes[0]["dispute_id"], keep="existing", note="以现场封条读数 1200 为准")

    generated = svc.generate_charges(admin, period_id=september["period_id"])

    # 11. 减免：即使拥有审批权限，提交人也不能批准自己的申请
    clerk_a = AccessContext.tenant_user("clerk-a", "tenant-a", frozenset({"settle:read", "settle:relief", "settle:approve"}))
    refrigeration_charge = next(c for c in svc.list_charges(admin, period_id=september["period_id"], tenant_id="tenant-a") if c["service_code"] == "refrigeration")
    relief = svc.request_relief(clerk_a, period_id=september["period_id"], charge_id=refrigeration_charge["charge_id"], amount_minor=40000, reason="停机期间制冷体验受损申请减免")
    try:
        svc.decide_relief(clerk_a, relief_id=relief["relief_id"], approve=True, note="自批应被拒绝")
    except PermissionDenied as exc:
        notes.append(f"自批减免被拒: {exc}")
    approver = AccessContext("finance-approver", permissions=frozenset({"settle:approve", "settle:read", "settle:close", "settle:bill", "settle:admin"}))
    decision = svc.decide_relief(approver, relief_id=relief["relief_id"], approve=True, note="情况属实，批准减免 400 元")

    # 12. 租户边界：甲企业查不到乙企业费用
    try:
        svc.list_charges(clerk_a, period_id=september["period_id"], tenant_id="tenant-b")
    except PermissionDenied as exc:
        notes.append(f"租户越界查询被拒: {exc}")

    # 13. 过账、关账；关账后执行重启恢复的到期任务（停机已在出账扣减则确认不重复退）
    svc.post_all(admin, period_id=september["period_id"])
    closed = svc.close_period(admin, period_id=september["period_id"])
    job_outcomes = svc.run_due_jobs(admin)
    sample = svc.list_charges(admin, period_id=september["period_id"])
    drill = svc.drill_down(admin, charge_id=next(c["charge_id"] for c in sample if c["source_type"] == "booking" and c["service_code"] == "dock" and c["tenant_id"] == "tenant-a"))
    reading_drill = svc.drill_down(admin, charge_id=next(c["charge_id"] for c in sample if c["source_type"] == "reading"))

    # 14. 关账后历史差错：在 10 月账期贷项/补单纠正
    october = svc.open_period(admin, period_start="2026-10-01T00:00:00+08:00", period_end="2026-11-01T00:00:00+08:00")
    credit = svc.issue_credit(approver, period_id=october["period_id"], tenant_id="tenant-b", service_code="dock", amount_minor=5000, reason="复核发现 9 月拼单月台多计 50 元", parent_charge_id=next(c["charge_id"] for c in sample if c["service_code"] == "dock" and c["tenant_id"] == "tenant-b"))
    supplement = svc.issue_supplement(approver, period_id=october["period_id"], tenant_id="tenant-a", service_code="rail", amount_minor=3000, reason="复核发现 9 月铁路接驳接驳时长漏计")

    summary = {
        "job_outcomes": job_outcomes,
        "charges_generated": generated["count"],
        "refrigeration_charges": [{"tenant": c["tenant_id"], "window": [c["window_start"], c["window_end"]], "amount_yuan": c["amount_minor"] / 100, "price_id": c["price_id"], "detail": json.loads(c["detail_json"])} for c in sample if c["service_code"] == "refrigeration"],
        "shared_dock_charges": [{"tenant": c["tenant_id"], "amount_yuan": c["amount_minor"] / 100} for c in sample if c["service_code"] == "dock"],
        "shared_charging_charges": [{"tenant": c["tenant_id"], "amount_yuan": c["amount_minor"] / 100} for c in sample if c["service_code"] == "charging"],
        "relief_credit_charge": decision["credit"],
        "tenant_balances_minor": {"tenant-a": svc.tenant_balance(admin, tenant_id="tenant-a"), "tenant-b": svc.tenant_balance(admin, tenant_id="tenant-b")},
        "september_closed": closed["status"],
        "october_corrections": {"credit": credit, "supplement": supplement},
        "drilldown_keys": sorted(drill.keys()),
        "reading_drilldown_keys": sorted(reading_drill.keys()),
        "notes": notes,
        "verification": app.verify(),
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="协同事务平台")
    parser.add_argument("--db", default=os.getenv("CIVICFLOW_DB", "civicflow.sqlite3"))
    parser.add_argument("--now", default=None, help="测试或演示使用的固定时间")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("settlement-demo")
    commands.add_parser("verify")
    commands.add_parser("list-cases")
    args = parser.parse_args(argv)
    fixed_now = args.now
    if args.command == "settlement-demo" and fixed_now is None:
        fixed_now = "2026-10-02T10:00:00+08:00"
    app = CivicFlow.open(Path(args.db), fixed_now=fixed_now)
    if args.command == "demo": emit(demo(app))
    elif args.command == "settlement-demo": emit(settlement_demo(app))
    elif args.command == "verify": emit(app.verify())
    elif args.command == "list-cases": emit(CaseService(app.repository).list_current(AccessContext.system("cli")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
