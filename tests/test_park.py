from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.errors import ConflictError, PermissionDenied, ValidationError
from civicflow.security import AccessContext


SEPT = ("2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
OCT = ("2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")
TENANT_A = "enterprise-a"
TENANT_B = "enterprise-b"


class ParkTestBase(unittest.TestCase):
    now = "2026-09-28T12:00:00Z"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "park.sqlite3")
        self.app = CivicFlow.open(self.db_path, fixed_now=self.now)
        self.op = AccessContext.system("park-operator")
        self.finance = AccessContext.system("finance-officer")

    def tearDown(self):
        self.temp.cleanup()

    def tenant_ctx(self, tenant: str) -> AccessContext:
        return AccessContext(actor_id=f"user:{tenant}",
                             permissions=frozenset({"read:park-billing", "read:park-ops", "read:park-master"}),
                             scopes=frozenset({f"org:{tenant}"}))

    def seed_catalog(self):
        m = self.app.park_master
        m.register_unit(self.op, {"code": "A1", "unit_type": "高标仓", "area_sqm": 2000, "location": "广州枢纽1号库"},
                        valid_from="2026-01-01T00:00:00Z", request_key="unit-a1")
        m.sign_lease(self.op, {"unit_code": "A1", "tenant_org": TENANT_A, "currency": "CNY", "rent_minor": 1},
                     valid_from=SEPT[0], valid_to="2027-01-01T00:00:00Z", request_key="lease-a")
        for code, name, unit, metered in (("dock", "月台", "车位小时", False),
                                          ("charging", "充电", "kWh", True),
                                          ("cold", "冷库", "小时", False)):
            m.add_catalog_service(self.op, {"service_code": code, "name": name, "unit": unit, "metered": metered},
                                  valid_from="2026-01-01T00:00:00Z", request_key=f"cat-{code}")
        m.add_price_rule(self.op, {"service_code": "dock", "price_minor": 500, "mode": "hour", "unit": "车位小时"},
                         valid_from=SEPT[0], request_key="p-dock")
        m.add_price_rule(self.op, {"service_code": "cold", "price_minor": 300, "mode": "hour", "unit": "小时"},
                         valid_from=SEPT[0], request_key="p-cold")
        m.add_price_rule(self.op, {"service_code": "charging", "price_minor": 200, "mode": "meter", "unit": "kWh"},
                         valid_from=SEPT[0], request_key="p-charge")
        m.register_meter_point(self.op, {"code": "CP-1", "service_code": "charging", "unit_of_measure": "kWh", "resource_id": "charger:1"},
                               valid_from=SEPT[0], request_key="mp-1")
        return m

    def meter_point_id(self) -> str:
        return self.app.park_master.store.list_type("meter_points", code="CP-1")[0]["record_id"]

    def reserve_and_confirm(self, *, resource_id, capability, capacity, start, end, allocations, declared=None, rk="r"):
        ops = self.app.park_ops
        declared = declared or [a["tenant_org"] for a in allocations]
        reservation = ops.reserve(self.op, resource_id=resource_id, capability=capability,
                                  quantity=sum(a["quantity"] for a in allocations), capacity=capacity,
                                  start_at=start, end_at=end, tenants=declared, request_key=f"res:{rk}")
        return ops.confirm_operation(self.op, reservation_id=reservation["reservation_id"],
                                     allocations=allocations, request_key=f"confirm:{rk}", outbox=self.app.outbox)

    def open_period(self, tenant=TENANT_A, window=SEPT, rk="period"):
        return self.app.park_billing.open_period(
            self.op, tenant_org=tenant, period_start=window[0], period_end=window[1],
            currency="CNY", request_key=rk)

    def lines_by_service(self, period_id):
        rows = self.app.park_billing.period_lines(self.op, period_id)
        return {row["service_code"]: row for row in rows}


class ColdStorageOutageTests(ParkTestBase):
    def test_outage_hours_not_billed_and_compensation_credit(self):
        self.seed_catalog()
        # 冷库占用 24 小时，其中 10:00-14:00 停机 4 小时
        self.reserve_and_confirm(resource_id="cold:1", capability="cold", capacity=6,
                                 start="2026-09-20T00:00:00Z", end="2026-09-21T00:00:00Z",
                                 allocations=[{"tenant_org": TENANT_A, "quantity": 1}], rk="cold")
        self.app.park_master.register_maintenance(
            self.op, {"resource_id": "cold:1", "service_code": "cold", "reason": "压缩机检修", "comp_minor": 1200},
            valid_from="2026-09-20T10:00:00Z", valid_to="2026-09-20T14:00:00Z",
            request_key="maint", jobs=self.app.jobs, outbox=self.app.outbox)
        period = self.open_period()
        # 停机结束时刻的补偿任务在进程重启/轮询后仍按原期限出现
        results = self.app.park_billing.process_due_jobs()
        pending = next(r for r in results if r.get("pending_credits"))["pending_credits"]
        self.assertEqual(len(pending), 1)
        # 系统录入的补偿申请不能自己批准
        with self.assertRaises(PermissionDenied):
            self.app.park_billing.decide_adjustment(AccessContext.system("system:maintenance"),
                                                    adjustment_id=pending[0], decision="approved", reason="x")
        self.app.park_billing.decide_adjustment(self.finance, adjustment_id=pending[0],
                                                decision="approved", reason="核对停机与补偿标准")
        generated = self.app.park_billing.generate(self.op, period["period_id"], request_key="gen")
        cold = next(l for l in generated["lines"] if l["service_code"] == "cold")
        self.assertEqual(cold["amount_minor"], 20 * 300)  # 停机 4 小时不计费
        self.app.park_billing.close_period(self.op, period_id=period["period_id"], request_key="close")
        lines = self.app.park_billing.period_lines(self.op, period["period_id"])
        rent_line = next(l for l in lines if l["service_code"] == "rent:A1")
        self.assertEqual(rent_line["amount_minor"], 24 * 30)  # 9 月 720 小时 × 1
        # 应收 = 租金 720 + 制冷 6000，维保补偿贷项 1200 冲减
        self.assertEqual(self.app.park_billing.tenant_balance(self.op, TENANT_A, currency="CNY"), 720 + 6000 - 1200)


class SharedOperationTests(ParkTestBase):
    def test_shared_dock_and_charging_split_by_tenant(self):
        self.seed_catalog()
        window = ("2026-09-10T08:00:00Z", "2026-09-10T12:00:00Z")
        # 声明两家拼单，确认时只报一家 → 拒绝
        reservation = self.app.park_ops.reserve(
            self.op, resource_id="dock:3", capability="dock", quantity=2, capacity=10,
            start_at=window[0], end_at=window[1], tenants=[TENANT_A, TENANT_B], request_key="res:bad")
        with self.assertRaises(ValidationError):
            self.app.park_ops.confirm_operation(
                self.op, reservation_id=reservation["reservation_id"],
                allocations=[{"tenant_org": TENANT_A, "quantity": 2}], request_key="bad")
        # 拆清后确认
        self.reserve_and_confirm(resource_id="dock:3", capability="dock", capacity=10,
                                 start=window[0], end=window[1],
                                 allocations=[{"tenant_org": TENANT_A, "quantity": 1},
                                              {"tenant_org": TENANT_B, "quantity": 1}], rk="dock")
        # 充电拼单：共享计量点总用量 100kWh，按实际数量 1:1 分摊
        self.reserve_and_confirm(resource_id="charger:1", capability="charging", capacity=8,
                                 start="2026-09-15T10:00:00Z", end="2026-09-15T11:00:00Z",
                                 allocations=[{"tenant_org": TENANT_A, "quantity": 1},
                                              {"tenant_org": TENANT_B, "quantity": 1}], rk="charge")
        pid = self.meter_point_id()
        self.app.park_metering.ingest(meter_code="CP-0000", meter_point_id=pid, read_value="0",
                                      read_at="2026-08-31T23:00:00Z", source="scada", actor="scada")
        self.app.park_metering.ingest(meter_code="CP-0100", meter_point_id=pid, read_value="100",
                                      read_at="2026-09-30T23:00:00Z", source="scada", actor="scada")
        for tenant, expected_dock in ((TENANT_A, 4 * 500), (TENANT_B, 4 * 500)):
            period = self.open_period(tenant=tenant, rk=f"period:{tenant}")
            generated = self.app.park_billing.generate(self.op, period["period_id"], request_key=f"gen:{tenant}")
            by_service = {l["service_code"]: l for l in generated["lines"]}
            self.assertEqual(by_service["dock"]["amount_minor"], expected_dock)
            self.assertEqual(by_service["charging"]["amount_minor"], 50 * 200)  # 各承担一半


class MeterIntegrityTests(ParkTestBase):
    def test_duplicate_reading_ignored_conflict_suspends(self):
        self.seed_catalog()
        pid = self.meter_point_id()
        self.reserve_and_confirm(resource_id="charger:1", capability="charging", capacity=8,
                                 start="2026-09-15T10:00:00Z", end="2026-09-15T11:00:00Z",
                                 allocations=[{"tenant_org": TENANT_A, "quantity": 1}], rk="charge")
        self.app.park_metering.ingest(meter_code="CP-0000", meter_point_id=pid, read_value="0",
                                      read_at="2026-08-31T23:00:00Z", source="scada", actor="scada")
        first = self.app.park_metering.ingest(meter_code="CP-0100", meter_point_id=pid, read_value="100",
                                              read_at="2026-09-30T23:00:00Z", source="scada", actor="scada")
        repeat = self.app.park_metering.ingest(meter_code="CP-0100", meter_point_id=pid, read_value="100",
                                               read_at="2026-09-30T23:00:00Z", source="scada", actor="scada")
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(repeat["status"], "duplicate")  # 同编号再次到达不重复入账
        period = self.open_period()
        # 正常计价成功
        self.app.park_billing.generate(self.op, period["period_id"], request_key="gen-ok")
        # 同编号不同读数：冲突挂起，相关账期暂停结算
        conflict = self.app.park_metering.ingest(meter_code="CP-0100", meter_point_id=pid, read_value="130",
                                                 read_at="2026-09-30T23:05:00Z", source="scada", actor="scada")
        self.assertEqual(conflict["status"], "conflict")
        with self.assertRaises(ConflictError):
            self.app.park_billing.generate(self.op, period["period_id"], request_key="gen-again")
        with self.assertRaises(ConflictError):
            self.app.park_billing.close_period(self.op, period_id=period["period_id"], request_key="close-blocked")
        # 裁决后恢复结算
        self.app.park_metering.adjudicate(self.finance, meter_code="CP-0100", accepted_value="100", reason="现场复核表计")
        self.app.park_billing.generate(self.op, period["period_id"], request_key="gen-final")
        closed = self.app.park_billing.close_period(self.op, period_id=period["period_id"], request_key="close")
        self.assertEqual(closed["status"], "closed")

    def test_missing_reading_blocks_until_supplied(self):
        self.seed_catalog()
        self.reserve_and_confirm(resource_id="charger:1", capability="charging", capacity=8,
                                 start="2026-09-15T10:00:00Z", end="2026-09-15T11:00:00Z",
                                 allocations=[{"tenant_org": TENANT_A, "quantity": 1}], rk="charge")
        period = self.open_period()
        with self.assertRaises(ConflictError):
            self.app.park_billing.generate(self.op, period["period_id"], request_key="gen")
        kinds = {h["kind"] for h in self.app.park_billing.open_holds(self.op)}
        self.assertIn("missing_meter", kinds)
        pid = self.meter_point_id()
        self.app.park_metering.ingest(meter_code="CP-0000", meter_point_id=pid, read_value="0",
                                      read_at="2026-08-31T23:00:00Z", source="scada", actor="scada")
        self.app.park_metering.ingest(meter_code="CP-0080", meter_point_id=pid, read_value="80",
                                      read_at="2026-09-30T23:00:00Z", source="scada", actor="scada")
        self.app.park_holds.resolve("missing_meter", f"{period['period_id']}:{pid}")
        generated = self.app.park_billing.generate(self.op, period["period_id"], request_key="gen2")
        self.assertEqual(next(l for l in generated["lines"] if l["service_code"] == "charging")["amount_minor"], 80 * 200)


class LeaseAmendmentTests(ParkTestBase):
    def test_later_signed_amendment_only_affects_later_bills(self):
        m = self.seed_catalog()
        sept = self.open_period(window=SEPT, rk="p-sept")
        self.app.park_billing.generate(self.op, sept["period_id"], request_key="g-sept")
        self.app.park_billing.close_period(self.op, period_id=sept["period_id"], request_key="c-sept")
        # 9 月 28 日签署的扩仓变更 10 月 1 日生效：9 月账单仍按 v1
        lease_series = m.store.list_type("leases")[0]["series_id"]
        m.amend_lease(self.finance, lease_series,
                      {"unit_code": "A1", "tenant_org": TENANT_A, "currency": "CNY", "rent_minor": 2},
                      valid_from=OCT[0], valid_to="2027-01-01T00:00:00Z", request_key="amend-1")
        sept_rent = self.lines_by_service(sept["period_id"])["rent:A1"]
        self.assertEqual(sept_rent["evidence"]["lease_version"], 1)
        # 10 月账期按新价 v2
        later_app = CivicFlow.open(self.db_path, fixed_now="2026-10-02T12:00:00Z")
        octp = later_app.park_billing.open_period(self.op, tenant_org=TENANT_A, period_start=OCT[0],
                                                  period_end=OCT[1], currency="CNY", request_key="p-oct")
        generated = later_app.park_billing.generate(self.op, octp["period_id"], request_key="g-oct")
        self.assertTrue(any(l["service_code"] == "rent:A1" for l in generated["lines"]))
        oct_rent = later_app.park_billing.period_lines(self.op, octp["period_id"])
        oct_rent = next(l for l in oct_rent if l["service_code"] == "rent:A1")
        self.assertEqual(oct_rent["evidence"]["lease_version"], 2)
        # 历史差错不改原账单，经他人审批以贷项纠正
        clerk = AccessContext(actor_id="clerk:1", permissions=frozenset({"request:park-adjust"}))
        adj = later_app.park_billing.request_adjustment(
            clerk, period_id=sept["period_id"], kind="credit", amount="3.00", reason="9月服务费申诉",
            request_key="adj-1")
        with self.assertRaises(PermissionDenied):
            later_app.park_billing.decide_adjustment(clerk, adjustment_id=adj["adjustment_id"],
                                                     decision="approved", reason="self")
        later_app.park_billing.decide_adjustment(self.finance, adjustment_id=adj["adjustment_id"],
                                                 decision="approved", reason="申诉成立")
        self.assertEqual(sept_rent["status"], "posted")  # 原费用行未被回改


class FaultScopingTests(ParkTestBase):
    def test_fault_only_cancels_affected_resource(self):
        self.seed_catalog()
        self.reserve_and_confirm(resource_id="dock:3", capability="dock", capacity=10,
                                 start="2026-09-10T08:00:00Z", end="2026-09-10T12:00:00Z",
                                 allocations=[{"tenant_org": TENANT_A, "quantity": 1}], rk="dock")
        self.reserve_and_confirm(resource_id="cold:1", capability="cold", capacity=6,
                                 start="2026-09-20T00:00:00Z", end="2026-09-21T00:00:00Z",
                                 allocations=[{"tenant_org": TENANT_A, "quantity": 1}], rk="cold")
        result = self.app.park_ops.cancel_for_fault(
            self.op, resource_id="dock:3", start_at="2026-09-10T00:00:00Z", end_at="2026-09-11T00:00:00Z",
            reason="月台升降平台故障", request_key="fault", outbox=self.app.outbox)
        self.assertEqual(len(result["cancelled_reservations"]), 1)
        # 冷库安排不受影响，仍正常计费
        period = self.open_period()
        generated = self.app.park_billing.generate(self.op, period["period_id"], request_key="gen")
        services = {l["service_code"] for l in generated["lines"]}
        self.assertIn("cold", services)
        self.assertNotIn("dock", services)


class BoundaryAndDrilldownTests(ParkTestBase):
    def test_tenant_boundary_and_drill_down(self):
        self.seed_catalog()
        self.reserve_and_confirm(resource_id="dock:3", capability="dock", capacity=10,
                                 start="2026-09-10T08:00:00Z", end="2026-09-10T12:00:00Z",
                                 allocations=[{"tenant_org": TENANT_A, "quantity": 1},
                                              {"tenant_org": TENANT_B, "quantity": 1}], rk="dock")
        pa = self.open_period(tenant=TENANT_A, rk="pa")
        pb = self.open_period(tenant=TENANT_B, rk="pb")
        self.app.park_billing.generate(self.op, pa["period_id"], request_key="ga")
        self.app.park_billing.generate(self.op, pb["period_id"], request_key="gb")
        ctx_a = self.tenant_ctx(TENANT_A)
        self.assertEqual(len(self.app.park_billing.list_periods(ctx_a, tenant_org=TENANT_A)), 1)
        with self.assertRaises(PermissionDenied):
            self.app.park_billing.list_periods(ctx_a, tenant_org=TENANT_B)
        with self.assertRaises(PermissionDenied):
            self.app.park_billing.period_lines(ctx_a, pb["period_id"])
        lines = self.app.park_billing.period_lines(ctx_a, pa["period_id"])
        dock_line = next(l for l in lines if l["service_code"] == "dock")
        detail = self.app.park_billing.line_detail(self.op, dock_line["line_id"])
        self.assertEqual(detail["drill_down"]["usage"]["resource_id"], "dock:3")
        self.assertTrue(detail["drill_down"]["price_versions"])
        self.assertEqual(detail["evidence"]["slices"][0]["price_minor"], 500)


class CrossPeriodPriceGapTests(ParkTestBase):
    def test_service_without_price_suspends_then_recovers(self):
        self.seed_catalog()
        # 临时扩仓使用铁路接驳：目录里有服务，但账期内没有任何适用价格
        self.app.park_master.add_catalog_service(
            self.op, {"service_code": "rail", "name": "铁路接驳", "unit": "列时", "metered": False},
            valid_from="2026-01-01T00:00:00Z", request_key="cat-rail")
        self.reserve_and_confirm(resource_id="rail:1", capability="rail", capacity=4,
                                 start="2026-10-05T08:00:00Z", end="2026-10-05T12:00:00Z",
                                 allocations=[{"tenant_org": TENANT_A, "quantity": 1}], rk="rail")
        octp = self.open_period(window=OCT, rk="oct")
        with self.assertRaises(ConflictError):
            self.app.park_billing.generate(self.op, octp["period_id"], request_key="g-gap")
        kinds = {h["kind"] for h in self.app.park_billing.open_holds(self.op)}
        self.assertIn("price_gap", kinds)
        with self.assertRaises(ConflictError):
            self.app.park_billing.close_period(self.op, period_id=octp["period_id"], request_key="c-gap")
        # 补登 10 月 1 日生效的价格（首条价格允许回溯登记到服务启用时点）
        self.app.park_master.add_price_rule(
            self.op, {"service_code": "rail", "price_minor": 600, "mode": "hour", "unit": "列时"},
            valid_from="2026-10-01T00:00:00Z", request_key="p-rail-oct")
        gap = next(h["ref_key"] for h in self.app.park_billing.open_holds(self.op, kind="price_gap"))
        self.app.park_holds.resolve("price_gap", gap)
        generated = self.app.park_billing.generate(self.op, octp["period_id"], request_key="g-ok")
        rail_line = next(l for l in generated["lines"] if l["service_code"] == "rail")
        self.assertEqual(rail_line["amount_minor"], 4 * 600)


class RestartRecoveryTests(ParkTestBase):
    def test_due_jobs_survive_restart_with_original_deadline(self):
        self.seed_catalog()
        self.reserve_and_confirm(resource_id="cold:1", capability="cold", capacity=6,
                                 start="2026-09-20T00:00:00Z", end="2026-09-21T00:00:00Z",
                                 allocations=[{"tenant_org": TENANT_A, "quantity": 1}], rk="cold")
        self.app.park_master.register_maintenance(
            self.op, {"resource_id": "cold:1", "service_code": "cold", "reason": "检修", "comp_minor": 500},
            valid_from="2026-09-20T10:00:00Z", valid_to="2026-09-20T14:00:00Z",
            request_key="maint", jobs=self.app.jobs)
        self.open_period(rk="p")
        # 模拟进程重启：新的应用实例打开同一个库，到期任务无需人工重新登记
        restarted = CivicFlow.open(self.db_path, fixed_now="2026-09-29T00:00:00Z")
        results = restarted.park_billing.process_due_jobs()
        self.assertTrue(any(r.get("maintenance_comp_holds") for r in results))


if __name__ == "__main__":
    unittest.main()
