from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from civicflow.security import AccessContext


SEPT_START = "2026-09-01T00:00:00+08:00"
OCT_START = "2026-10-01T00:00:00+08:00"
NOV_START = "2026-11-01T00:00:00+08:00"


class SettlementTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "settle.sqlite3")
        self.app = CivicFlow.open(self.db_path, fixed_now="2026-10-02T09:00:00+08:00")
        self.admin = AccessContext.system("ops-admin")
        self.approver = AccessContext("finance-approver", permissions=frozenset({"settle:approve", "settle:read", "settle:close", "settle:bill", "settle:admin"}))
        self.svc = self.app.settlement
        self._seed_master()

    def tearDown(self):
        self.temp.cleanup()

    # ------------------------------------------------------------------ 夹具

    def _seed_master(self):
        s = self.svc; a = self.admin
        s.register_tenant(a, tenant_id="ta", org_id="org:a", name="甲", valid_from=SEPT_START)
        s.register_tenant(a, tenant_id="tb", org_id="org:b", name="乙", valid_from=SEPT_START)
        s.register_unit(a, unit_id="u1", code="U-01", unit_type="cold", capacity_qty=100, capacity_unit="pallet", valid_from=SEPT_START)
        s.register_unit(a, unit_id="u2", code="U-02", unit_type="dry", capacity_qty=200, capacity_unit="pallet", valid_from=SEPT_START)
        s.register_acceptance(a, unit_id="u1", delivered_at=SEPT_START, accepted_at="2026-09-01T10:00:00+08:00", result="passed")
        s.sign_lease(a, lease_id="L1", unit_id="u1", tenant_id="ta", valid_from=SEPT_START, valid_to=None,
                     rate_minor=30000, currency="CNY", billing_unit="day")
        s.sign_lease(a, lease_id="L2", unit_id="u2", tenant_id="tb", valid_from=SEPT_START, valid_to=None,
                     rate_minor=20000, currency="CNY", billing_unit="day")
        s.register_service(a, service_code="refrigeration", name="制冷", service_kind="refrigeration", unit_of_measure="day", metered=False, valid_from=SEPT_START)
        s.register_price(a, service_code="refrigeration", rate_minor=10000, currency="CNY", unit_of_measure="day", charge_mode="time", valid_from=SEPT_START)
        s.register_service(a, service_code="dock", name="月台", service_kind="dock", unit_of_measure="slot-hour", metered=False, valid_from=SEPT_START)
        s.register_price(a, service_code="dock", rate_minor=10000, currency="CNY", unit_of_measure="slot-hour", charge_mode="time", valid_from=SEPT_START)
        s.register_service(a, service_code="charging", name="充电", service_kind="charging", unit_of_measure="kWh", metered=False, valid_from=SEPT_START)
        s.register_price(a, service_code="charging", rate_minor=100, currency="CNY", unit_of_measure="kWh", charge_mode="usage", valid_from=SEPT_START)
        s.register_service(a, service_code="power", name="冷机用电", service_kind="refrigeration", unit_of_measure="kWh", metered=True, valid_from=SEPT_START)
        s.register_price(a, service_code="power", rate_minor=100, currency="CNY", unit_of_measure="kWh", charge_mode="usage", valid_from=SEPT_START)
        s.register_meter(a, meter_code="M1", meter_kind="electricity", unit_of_measure="kWh", unit_id="u1", service_code="power", valid_from=SEPT_START)

    def _open_september(self):
        return self.svc.open_period(self.admin, period_start=SEPT_START, period_end=OCT_START)["period_id"]

    def _open_october(self):
        return self.svc.open_period(self.admin, period_start=OCT_START, period_end=NOV_START)["period_id"]

    def _charge_map(self, period_id, tenant_id=None):
        rows = self.svc.list_charges(self.admin, period_id=period_id, tenant_id=tenant_id)
        result = {}
        for row in rows:
            result.setdefault(row["service_code"], []).append(row)
        return result

    # ------------------------------------------------------------------ 有效期与租约版本

    def test_lease_change_only_affects_later_bills(self):
        # 09-20 签署变更：租金 300 -> 400 元/日，只影响 09-20 之后
        self.svc.sign_lease(self.admin, lease_id="L1", unit_id="u1", tenant_id="ta",
                            valid_from="2026-09-20T00:00:00+08:00", valid_to=None,
                            rate_minor=40000, currency="CNY", billing_unit="day", supersedes=1)
        pid = self._open_september()
        self.svc.generate_charges(self.admin, period_id=pid)
        storage = self._charge_map(pid, "ta")["storage"]
        amounts = sorted(c["amount_minor"] for c in storage)
        # 19 天 * 300 = 570000；11 天 * 400 = 440000（9 月 30 天）
        self.assertEqual(amounts, [440000, 570000])
        versions = [c["lease_version"] for c in storage]
        self.assertEqual(sorted(versions), [1, 2])
        # as-of 查询：变更前看 v1，变更后看 v2
        self.assertEqual(self.svc.lease_as_of(self.admin, "L1", at="2026-09-19T00:00:00+08:00")["version"], 1)
        self.assertEqual(self.svc.lease_as_of(self.admin, "L1", at="2026-09-21T00:00:00+08:00")["version"], 2)

    def test_lease_change_must_supersede_latest(self):
        with self.assertRaises(ConflictError):
            self.svc.sign_lease(self.admin, lease_id="L1", unit_id="u1", tenant_id="ta",
                                valid_from="2026-09-20T00:00:00+08:00", valid_to=None,
                                rate_minor=40000, currency="CNY", billing_unit="day", supersedes=9)

    def test_superseded_price_still_applies_to_historical_window(self):
        # 09-15 制冷调价 100 -> 150；09-01~09-15 仍按 100 元/日
        self.svc.register_price(self.admin, service_code="refrigeration", rate_minor=15000, currency="CNY",
                                unit_of_measure="day", charge_mode="time", valid_from="2026-09-15T00:00:00+08:00")
        pid = self._open_september()
        self.svc.generate_charges(self.admin, period_id=pid)
        refrigeration = self._charge_map(pid, "ta")["refrigeration"]
        amounts = sorted(c["amount_minor"] for c in refrigeration)
        self.assertEqual(amounts, [140000, 240000])  # 14 天 *100；16 天 *150

    # ------------------------------------------------------------------ 维保停机

    def test_outage_window_is_not_billed(self):
        self.svc.record_outage(self.admin, outage_id="OG1", target_type="unit", target_id="u1", service_code="refrigeration",
                               start_at="2026-09-10T00:00:00+08:00", end_at="2026-09-12T00:00:00+08:00", reason="维保")
        pid = self._open_september()
        self.svc.generate_charges(self.admin, period_id=pid)
        refrigeration = self._charge_map(pid, "ta")["refrigeration"]
        total = sum(c["amount_minor"] for c in refrigeration)
        # 30 天 - 2 天停机 = 28 天 * 100
        self.assertEqual(total, 280000)

    def test_outage_job_does_not_double_refund_after_billing_deduction(self):
        self.svc.record_outage(self.admin, outage_id="OG1", target_type="unit", target_id="u1", service_code="refrigeration",
                               start_at="2026-09-10T00:00:00+08:00", end_at="2026-09-12T00:00:00+08:00", reason="维保")
        pid = self._open_september()
        self.svc.generate_charges(self.admin, period_id=pid)
        self.svc.post_all(self.admin, period_id=pid)
        self.svc.close_period(self.admin, period_id=pid)
        outcomes = self.svc.run_due_jobs(self.admin)
        og = next(o for o in outcomes if o["job_id"] == "outage:OG1")
        self.assertEqual(og["result"]["status"], "already-deducted-at-billing")
        balance = self.svc.tenant_balance(self.admin, tenant_id="ta")
        # 仓储 9000 + 制冷 2800，无重复退款
        self.assertEqual(balance, 1180000)

    def test_late_outage_after_close_generates_credit_next_period(self):
        pid = self._open_september()
        self.svc.generate_charges(self.admin, period_id=pid)
        self.svc.post_all(self.admin, period_id=pid)
        self.svc.close_period(self.admin, period_id=pid)
        # 关账后补登记：09-10~09-12 冷机故障当时漏报
        self.svc.record_outage(self.admin, outage_id="OG-LATE", target_type="unit", target_id="u1", service_code="refrigeration",
                               start_at="2026-09-10T00:00:00+08:00", end_at="2026-09-12T00:00:00+08:00", reason="补报故障")
        oct_pid = self._open_october()
        outcomes = self.svc.run_due_jobs(self.admin)
        og = next(o for o in outcomes if o["job_id"] == "outage:OG-LATE")
        self.assertLess(og["result"]["amount_minor"], 0)
        corrections = [c for c in self.svc.list_charges(self.admin, period_id=oct_pid) if c["charge_kind"] == "compensation"]
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["amount_minor"], -20000)

    def test_released_booking_only_affects_itself(self):
        b1 = self.svc.book_capacity(self.admin, service_code="dock", resource_id="D1",
                                    start_at="2026-09-20T08:00:00+08:00", end_at="2026-09-20T10:00:00+08:00", capacity_total=4)
        b2 = self.svc.book_capacity(self.admin, service_code="dock", resource_id="D1",
                                    start_at="2026-09-20T10:00:00+08:00", end_at="2026-09-20T12:00:00+08:00", capacity_total=4)
        self.svc.release_booking(self.admin, booking_id=b1["booking_id"], reason="月台升降设备故障")
        # 释放后同一时段可重新预约，另一笔预约不受影响
        self.svc.book_capacity(self.admin, service_code="dock", resource_id="D1",
                               start_at="2026-09-20T08:00:00+08:00", end_at="2026-09-20T10:00:00+08:00", capacity_total=4)
        self.svc.allocate(self.admin, booking_id=b2["booking_id"], tenant_id="ta", quantity=4)

    # ------------------------------------------------------------------ 拼单拆分

    def test_shared_booking_split_billed_per_tenant(self):
        booking = self.svc.book_capacity(self.admin, service_code="dock", resource_id="D1",
                                         start_at="2026-09-20T08:00:00+08:00", end_at="2026-09-20T12:00:00+08:00",
                                         capacity_total=10, organizer_tenant_id="ta")
        self.svc.allocate(self.admin, booking_id=booking["booking_id"], tenant_id="ta", quantity=7)
        self.svc.allocate(self.admin, booking_id=booking["booking_id"], tenant_id="tb", quantity=3)
        pid = self._open_september()
        self.svc.generate_charges(self.admin, period_id=pid)
        ta = sum(c["amount_minor"] for c in self._charge_map(pid, "ta").get("dock", []))
        tb = sum(c["amount_minor"] for c in self._charge_map(pid, "tb").get("dock", []))
        # 7 slots * 4h * 100 = 2800；3 * 4 * 100 = 1200
        self.assertEqual(ta, 280000)
        self.assertEqual(tb, 120000)

    def test_booking_without_allocation_cannot_bill(self):
        self.svc.book_capacity(self.admin, service_code="dock", resource_id="D1",
                               start_at="2026-09-20T08:00:00+08:00", end_at="2026-09-20T12:00:00+08:00", capacity_total=10)
        pid = self._open_september()
        with self.assertRaises(ConflictError):
            self.svc.generate_charges(self.admin, period_id=pid)

    def test_allocation_cannot_exceed_capacity(self):
        booking = self.svc.book_capacity(self.admin, service_code="dock", resource_id="D1",
                                         start_at="2026-09-20T08:00:00+08:00", end_at="2026-09-20T12:00:00+08:00", capacity_total=10)
        self.svc.allocate(self.admin, booking_id=booking["booking_id"], tenant_id="ta", quantity=6)
        with self.assertRaises(ConflictError):
            self.svc.allocate(self.admin, booking_id=booking["booking_id"], tenant_id="tb", quantity=5)

    def test_billing_requires_allocation_sum_equal_capacity(self):
        booking = self.svc.book_capacity(self.admin, service_code="dock", resource_id="D1",
                                         start_at="2026-09-20T08:00:00+08:00", end_at="2026-09-20T12:00:00+08:00", capacity_total=10)
        self.svc.allocate(self.admin, booking_id=booking["booking_id"], tenant_id="ta", quantity=6)
        pid = self._open_september()
        with self.assertRaises(ConflictError):
            self.svc.generate_charges(self.admin, period_id=pid)

    def test_overlapping_capacity_booking_rejected(self):
        self.svc.book_capacity(self.admin, service_code="dock", resource_id="D1",
                               start_at="2026-09-20T08:00:00+08:00", end_at="2026-09-20T12:00:00+08:00", capacity_total=10)
        with self.assertRaises(ConflictError):
            self.svc.book_capacity(self.admin, service_code="dock", resource_id="D1",
                                   start_at="2026-09-20T09:00:00+08:00", end_at="2026-09-20T13:00:00+08:00", capacity_total=1)

    # ------------------------------------------------------------------ 抄表

    def test_duplicate_reading_is_not_posted_twice(self):
        reader = AccessContext("reader", permissions=frozenset({"settle:reading"}))
        first = self.svc.record_reading(reader, meter_code="M1", read_at="2026-09-10T08:00:00+08:00", reading_value="100")
        again = self.svc.record_reading(reader, meter_code="M1", read_at="2026-09-10T08:00:00+08:00", reading_value="100")
        self.assertEqual(first["status"], "recorded")
        self.assertEqual(again["status"], "duplicate")

    def test_different_reading_suspends_settlement(self):
        reader = AccessContext("reader", permissions=frozenset({"settle:reading"}))
        self.svc.record_reading(reader, meter_code="M1", read_at="2026-09-10T08:00:00+08:00", reading_value="100")
        with self.assertRaises(ConflictError):
            self.svc.record_reading(reader, meter_code="M1", read_at="2026-09-10T08:00:00+08:00", reading_value="120")
        self.assertEqual(len(self.svc.outstanding_disputes(self.admin)), 1)
        pid = self._open_september()
        with self.assertRaises(ConflictError):
            self.svc.generate_charges(self.admin, period_id=pid)
        # 争议按原读数处理后恢复结算
        dispute = self.svc.outstanding_disputes(self.admin)[0]
        self.svc.resolve_reading_dispute(self.admin, dispute_id=dispute["dispute_id"], keep="existing", note="以原读数为准")
        self.svc.record_reading(reader, meter_code="M1", read_at="2026-09-20T08:00:00+08:00", reading_value="300")
        self.svc.generate_charges(self.admin, period_id=pid)
        power = self._charge_map(pid, "ta")["power"]
        # 差量 300-100=200 kWh * 1.00 元
        self.assertEqual(sum(c["amount_minor"] for c in power), 20000)

    def test_reading_rollback_blocks_settlement(self):
        reader = AccessContext("reader", permissions=frozenset({"settle:reading"}))
        self.svc.record_reading(reader, meter_code="M1", read_at="2026-09-10T08:00:00+08:00", reading_value="200")
        self.svc.record_reading(reader, meter_code="M1", read_at="2026-09-20T08:00:00+08:00", reading_value="100")
        pid = self._open_september()
        with self.assertRaises(ConflictError):
            self.svc.generate_charges(self.admin, period_id=pid)

    # ------------------------------------------------------------------ 减免职责分离

    def test_submitter_cannot_approve_own_relief(self):
        pid = self._open_september()
        self.svc.generate_charges(self.admin, period_id=pid)
        charge = self._charge_map(pid, "ta")["storage"][0]
        clerk = AccessContext("clerk", permissions=frozenset({"settle:read", "settle:relief", "settle:approve"}))
        relief = self.svc.request_relief(clerk, period_id=pid, charge_id=charge["charge_id"], amount_minor=1000, reason="申诉")
        with self.assertRaises(PermissionDenied):
            self.svc.decide_relief(clerk, relief_id=relief["relief_id"], approve=True, note="自批")
        decision = self.svc.decide_relief(self.approver, relief_id=relief["relief_id"], approve=True, note="批准")
        self.assertEqual(decision["status"], "approved")
        self.assertLess(decision["credit"]["amount_minor"], 0)

    def test_relief_requires_reason_and_positive_amount(self):
        pid = self._open_september()
        self.svc.generate_charges(self.admin, period_id=pid)
        charge = self._charge_map(pid, "ta")["storage"][0]
        with self.assertRaises(ValidationError):
            self.svc.request_relief(self.admin, period_id=pid, charge_id=charge["charge_id"], amount_minor=0, reason="x")

    # ------------------------------------------------------------------ 租户边界

    def test_tenant_cannot_read_other_tenant_charges(self):
        pid = self._open_september()
        self.svc.generate_charges(self.admin, period_id=pid)
        ta_user = AccessContext.tenant_user("u-a", "ta", frozenset({"settle:read"}))
        self.assertEqual({c["tenant_id"] for c in self.svc.list_charges(ta_user, period_id=pid)}, {"ta"})
        with self.assertRaises(PermissionDenied):
            self.svc.list_charges(ta_user, period_id=pid, tenant_id="tb")
        tb_charge = self._charge_map(pid, "tb")["storage"][0]
        with self.assertRaises(PermissionDenied):
            self.svc.get_charge(ta_user, charge_id=tb_charge["charge_id"])
        with self.assertRaises(PermissionDenied):
            self.svc.drill_down(ta_user, charge_id=tb_charge["charge_id"])

    def test_drill_down_shows_full_evidence_chain(self):
        booking = self.svc.book_capacity(self.admin, service_code="dock", resource_id="D1",
                                         start_at="2026-09-20T08:00:00+08:00", end_at="2026-09-20T12:00:00+08:00", capacity_total=10)
        self.svc.allocate(self.admin, booking_id=booking["booking_id"], tenant_id="ta", quantity=10)
        pid = self._open_september()
        self.svc.generate_charges(self.admin, period_id=pid)
        self.svc.post_all(self.admin, period_id=pid)
        dock_charge = self._charge_map(pid, "ta")["dock"][0]
        drill = self.svc.drill_down(self.admin, charge_id=dock_charge["charge_id"])
        self.assertEqual(drill["booking"]["resource_id"], "D1")
        self.assertEqual(drill["allocations"][0]["quantity"], 10)
        self.assertIsNotNone(drill["price"])
        self.assertTrue(drill["entries"])

    # ------------------------------------------------------------------ 关账与历史纠正

    def test_closed_period_only_allows_credit_or_supplement(self):
        pid = self._open_september()
        self.svc.generate_charges(self.admin, period_id=pid)
        self.svc.post_all(self.admin, period_id=pid)
        self.svc.close_period(self.admin, period_id=pid)
        with self.assertRaises(ConflictError):
            self.svc.generate_charges(self.admin, period_id=pid)
        with self.assertRaises(ConflictError):
            self.svc.issue_credit(self.approver, period_id=pid, tenant_id="ta", service_code="dock", amount_minor=100, reason="关账期内不允许")
        oct_pid = self._open_october()
        original = self._charge_map(pid, "ta")["storage"][0]
        credit = self.svc.issue_credit(self.approver, period_id=oct_pid, tenant_id="ta", service_code="storage",
                                       amount_minor=500, reason="历史多收", parent_charge_id=original["charge_id"])
        supplement = self.svc.issue_supplement(self.approver, period_id=oct_pid, tenant_id="tb", service_code="storage",
                                               amount_minor=300, reason="历史漏收")
        self.assertEqual(credit["kind"], "credit")
        self.assertEqual(supplement["kind"], "supplement")
        drill = self.svc.drill_down(self.admin, charge_id=credit["charge_id"])
        self.assertEqual(drill["parent_charge"]["charge_id"], original["charge_id"])

    def test_cannot_close_with_draft_charges(self):
        pid = self._open_september()
        self.svc.generate_charges(self.admin, period_id=pid)
        with self.assertRaises(ConflictError):
            self.svc.close_period(self.admin, period_id=pid)

    # ------------------------------------------------------------------ 重启恢复

    def test_persistent_jobs_survive_restart_with_original_deadlines(self):
        self.svc.record_outage(self.admin, outage_id="OGR", target_type="unit", target_id="u1", service_code="refrigeration",
                               start_at="2026-09-10T00:00:00+08:00", end_at="2026-09-12T00:00:00+08:00", reason="维保")
        self.svc.register_reading_due(self.admin, meter_code="M1", due_at="2026-10-01T08:00:00+08:00")
        # 重新登记同一任务不会覆盖原期限
        self.assertFalse(self.svc.jobs.schedule_named("outage:OGR", job_type="outage-compensation", subject_id="OGR",
                                                      run_at="2030-01-01T00:00:00Z", payload={}))
        restarted = CivicFlow.open(self.db_path, fixed_now="2026-10-02T09:00:00+08:00")
        with self.app.database.connect() as c:
            rows = {r["job_id"]: r["run_at"] for r in c.execute("SELECT job_id,run_at FROM scheduled_jobs")}
        self.assertEqual(rows["outage:OGR"], "2026-09-11T16:00:00Z")
        claimed = restarted.settlement.claim_due_jobs(self.admin)
        self.assertIn("outage:OGR", [j["job_id"] for j in claimed])

    # ------------------------------------------------------------------ 承担关系

    def test_operator_bears_service_during_responsibility_window(self):
        self.svc.register_responsibility(self.admin, target_type="unit", target_id="u1", service_code="refrigeration",
                                         tenant_id="ta", bearer="operator",
                                         valid_from="2026-09-05T00:00:00+08:00", valid_to="2026-09-10T00:00:00+08:00")
        pid = self._open_september()
        self.svc.generate_charges(self.admin, period_id=pid)
        refrigeration = self._charge_map(pid, "ta")["refrigeration"]
        total = sum(c["amount_minor"] for c in refrigeration)
        # 5 天由运营方承担，租户只付 25 天
        self.assertEqual(total, 250000)

    # ------------------------------------------------------------------ 审计链

    def test_audit_chain_covers_settlement_actions(self):
        pid = self._open_september()
        self.svc.generate_charges(self.admin, period_id=pid)
        report = self.app.verify()
        self.assertGreater(report["audit_entries"], 0)
        self.assertEqual(report["inbox_conflicts"], 0)


if __name__ == "__main__":
    unittest.main()
