"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .application import CivicFlow
from .cases import CaseService
from .security import AccessContext


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def park_demo(app: CivicFlow) -> dict:
    """广州公铁联运枢纽高标仓：从仓位租约到园区服务结算的完整依据链。"""
    operator = AccessContext.system("park-operator")
    officer = AccessContext.system("finance-officer")
    master, ops, metering, billing = app.park_master, app.park_ops, app.park_metering, app.park_billing
    tenant = "org:enterprise-a"
    # 1. 主数据：仓位、服务目录、计量点、9 月有效价格
    master.register_unit(operator, {"code": "A1", "unit_type": "高标仓", "area_sqm": 2000, "location": "广州枢纽1号库"},
                         valid_from="2026-01-01T00:00:00Z", request_key="unit-a1")
    lease = master.sign_lease(operator, {"unit_code": "A1", "tenant_org": tenant, "currency": "CNY", "rent_minor": 1000},
                              valid_from="2026-09-01T00:00:00Z", valid_to="2026-10-01T00:00:00Z",
                              request_key="lease-a1", jobs=app.jobs)
    master.add_catalog_service(operator, {"service_code": "dock", "name": "月台作业", "unit": "车位小时", "metered": False},
                               valid_from="2026-01-01T00:00:00Z", request_key="cat-dock")
    master.add_catalog_service(operator, {"service_code": "charging", "name": "充电", "unit": "kWh", "metered": True},
                               valid_from="2026-01-01T00:00:00Z", request_key="cat-charging")
    master.add_catalog_service(operator, {"service_code": "cold", "name": "冷库制冷", "unit": "小时", "metered": False},
                               valid_from="2026-01-01T00:00:00Z", request_key="cat-cold")
    master.add_price_rule(operator, {"service_code": "dock", "price_minor": 500, "mode": "hour", "unit": "车位小时"},
                          valid_from="2026-09-01T00:00:00Z", request_key="price-dock")
    master.add_price_rule(operator, {"service_code": "charging", "price_minor": 200, "mode": "meter", "unit": "kWh"},
                          valid_from="2026-09-01T00:00:00Z", request_key="price-charging")
    master.add_price_rule(operator, {"service_code": "cold", "price_minor": 300, "mode": "hour", "unit": "小时"},
                          valid_from="2026-09-01T00:00:00Z", request_key="price-cold")
    master.register_meter_point(operator, {"code": "CP-A1", "service_code": "charging", "unit_of_measure": "kWh", "resource_id": "charger:1"},
                                valid_from="2026-09-01T00:00:00Z", request_key="meter-cp")
    # 2. 拼单预约：月台与充电均跨 A、B 两家企业，确认时拆清数量
    dock = ops.reserve(operator, resource_id="dock:3", capability="dock", quantity=2, capacity=10,
                       start_at="2026-09-10T08:00:00Z", end_at="2026-09-10T12:00:00Z",
                       tenants=[tenant, "org:enterprise-b"], request_key="res-dock")
    ops.confirm_operation(operator, reservation_id=dock["reservation_id"],
                          allocations=[{"tenant_org": tenant, "quantity": 1}, {"tenant_org": "org:enterprise-b", "quantity": 1}],
                          request_key="confirm-dock", outbox=app.outbox)
    charge = ops.reserve(operator, resource_id="charger:1", capability="charging", quantity=2, capacity=8,
                         start_at="2026-09-15T10:00:00Z", end_at="2026-09-15T11:00:00Z",
                         tenants=[tenant, "org:enterprise-b"], request_key="res-charge")
    ops.confirm_operation(operator, reservation_id=charge["reservation_id"],
                          allocations=[{"tenant_org": tenant, "quantity": 1}, {"tenant_org": "org:enterprise-b", "quantity": 1}],
                          request_key="confirm-charge", outbox=app.outbox)
    # 3. 冷库预约 + 当天 4 小时维保停机：停机窗口不计制冷费，停机补偿挂起到期生成贷项
    cold = ops.reserve(operator, resource_id="cold:1", capability="cold", quantity=1, capacity=6,
                       start_at="2026-09-20T00:00:00Z", end_at="2026-09-21T00:00:00Z",
                       tenants=[tenant], request_key="res-cold")
    ops.confirm_operation(operator, reservation_id=cold["reservation_id"],
                          allocations=[{"tenant_org": tenant, "quantity": 1}],
                          request_key="confirm-cold", outbox=app.outbox)
    master.register_maintenance(operator, {"resource_id": "cold:1", "service_code": "cold", "reason": "压缩机检修", "comp_minor": 1200},
                                valid_from="2026-09-20T10:00:00Z", valid_to="2026-09-20T14:00:00Z",
                                request_key="maint-cold", jobs=app.jobs, outbox=app.outbox)
    # 4. 抄表经收件箱接入：同编号重复不重入账
    meter_point_id = master.store.list_type("meter_points", code="CP-A1")[0]["record_id"]
    metering.ingest(meter_code="CP-A1-0001", meter_point_id=meter_point_id,
                    read_value="0", read_at="2026-09-01T00:00:00Z", source="scada", actor="scada")
    repeated = metering.ingest(meter_code="CP-A1-0001", meter_point_id=meter_point_id, read_value="0",
                               read_at="2026-09-01T00:00:00Z", source="scada", actor="scada")
    metering.ingest(meter_code="CP-A1-0002", meter_point_id=meter_point_id,
                    read_value="100", read_at="2026-09-30T23:59:00Z", source="scada", actor="scada")
    # 5. 账期计价（停机已扣减；充电总用量按拆清数量分摊）
    period = billing.open_period(operator, tenant_org=tenant, period_start="2026-09-01T00:00:00Z",
                                 period_end="2026-10-01T00:00:00Z", currency="CNY", request_key="period-sep")
    generated = billing.generate(operator, period["period_id"], request_key="gen-1")
    # 6. 到期任务按原期限触发（进程重启后依旧）：维保补偿生成待审批贷项
    job_results = billing.process_due_jobs()
    pending_credit = next(r for r in job_results if "pending_credits" in r and r["pending_credits"])["pending_credits"][0]
    # 录入人（system:maintenance）不能批准自己的申请，由财务另一名人员批准
    decision = billing.decide_adjustment(officer, adjustment_id=pending_credit, decision="approved", reason="核对维保停机与补偿标准无误")
    # 7. 关账并下钻一笔费用
    closed = billing.close_period(operator, period_id=period["period_id"], request_key="close-sep")
    first_line = generated["lines"][0]["line_id"]
    drill = billing.line_detail(operator, first_line)
    return {"lease_series": lease["series_id"], "duplicate_reading": repeated["status"],
            "generated": generated, "due_jobs": job_results, "credit_decision": decision,
            "closed": closed, "drill_down_keys": sorted(drill["drill_down"].keys()),
            "balance_minor": billing.tenant_balance(operator, tenant, currency="CNY"),
            "verification": app.verify()}


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="协同事务平台")
    parser.add_argument("--db", default=os.getenv("CIVICFLOW_DB", "civicflow.sqlite3"))
    parser.add_argument("--now", default=None, help="测试或演示使用的固定时间")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("park-demo")
    commands.add_parser("verify")
    commands.add_parser("list-cases")
    args = parser.parse_args(argv)
    app = CivicFlow.open(Path(args.db), fixed_now=args.now)
    if args.command == "demo": emit(demo(app))
    elif args.command == "park-demo": emit(park_demo(app))
    elif args.command == "verify": emit(app.verify())
    elif args.command == "list-cases": emit(CaseService(app.repository).list_current(AccessContext.system("cli")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
