"""设备抄表接入。

抄表经事件收件箱接入：计量编号（meter_code）即来源标识。同一编号再次到达
且读数相同视为重复投递，不再入账；同一编号出现不同读数会写入收件箱冲突并
开启 ``reading_conflict`` 挂起项，相关账期暂停结算，待运营方裁决后才能继续。
读数按累计量存储，用量为相邻两次已接受读数之差。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .database import Database
from .errors import ConflictError, NotFoundError, ValidationError
from .identifiers import new_id
from .inbox import Inbox
from .park_holds import HoldBook
from .security import AccessContext
from .timeutil import Clock, canonical_instant


def parse_meter_value(value: object) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValidationError("读数格式错误") from exc
    if not number.is_finite() or number < 0:
        raise ValidationError("读数必须是非负有限数")
    return number


@dataclass(frozen=True)
class MeteringService:
    database: Database
    clock: Clock
    inbox: Inbox
    holds: HoldBook

    def ingest(self, *, meter_code: str, meter_point_id: str, read_value: str, read_at: str,
               source: str, actor: str) -> dict:
        """接入一笔抄表（不走 AccessContext，通常由收件箱适配器/系统调用）。"""
        read_at = canonical_instant(read_at)
        value = parse_meter_value(read_value)
        payload = {"meter_code": meter_code, "meter_point_id": meter_point_id,
                   "read_value": str(value), "read_at": read_at}
        try:
            received = self.inbox.receive(source=source, source_key=meter_code, sequence=1,
                                          payload=payload, occurred_at=read_at)
        except ConflictError:
            # 同编号不同读数：暂停结算，等待裁决
            self.holds.open("reading_conflict", meter_code,
                            detail={"meter_code": meter_code, "meter_point_id": meter_point_id,
                                    "incoming_value": str(value), "read_at": read_at, "source": source})
            return {"meter_code": meter_code, "status": "conflict"}
        if received["status"] == "duplicate":
            return {"meter_code": meter_code, "status": "duplicate"}
        reading_id = new_id("reading")
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO park_meter_readings(reading_id,meter_code,meter_point_id,read_value,read_at,source,recorded_by,status) VALUES(?,?,?,?,?,?,?,?)",
                (reading_id, meter_code, meter_point_id, str(value), read_at, source, actor, "accepted"))
        return {"meter_code": meter_code, "reading_id": reading_id, "status": "accepted"}

    def adjudicate(self, context: AccessContext, *, meter_code: str, accepted_value: str, reason: str) -> dict:
        """运营方裁决冲突读数：确定正确读数后恢复结算。"""
        context.require("approve:park-adjust")
        if not reason.strip():
            raise ValidationError("裁决必须说明依据")
        value = parse_meter_value(accepted_value)
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM park_meter_readings WHERE meter_code=?", (meter_code,)).fetchone()
            if not row:
                raise NotFoundError(f"计量编号 {meter_code} 无已接收读数")
            connection.execute("UPDATE park_meter_readings SET status='adjudicated',adjudicated_value=?,adjudicated_by=? WHERE meter_code=?",
                               (str(value), context.actor_id, meter_code))
        resolved = self.holds.resolve("reading_conflict", meter_code)
        return {"meter_code": meter_code, "accepted_value": str(value), "hold": resolved["status"]}

    def get_reading(self, meter_code: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM park_meter_readings WHERE meter_code=?", (meter_code,)).fetchone()
            if not row:
                raise NotFoundError(f"计量编号 {meter_code} 不存在")
            return dict(row)

    def list_readings(self, context: AccessContext, meter_point_id: str | None = None, *, limit: int = 200) -> list[dict]:
        context.require("read:park-meter")
        sql = "SELECT * FROM park_meter_readings"; params: list[object] = []
        if meter_point_id is not None:
            sql += " WHERE meter_point_id=?"; params.append(meter_point_id)
        sql += " ORDER BY read_at,meter_code LIMIT ?"; params.append(limit)
        with self.database.connect() as connection:
            return [dict(r) for r in connection.execute(sql, params).fetchall()]

    # ---- 结算辅助 ----
    def last_reading_before(self, connection, meter_point_id: str, instant: str, *, strict: bool = True) -> dict | None:
        operator = "<" if strict else "<="
        row = connection.execute(
            f"SELECT *,COALESCE(adjudicated_value,read_value) AS effective_value FROM park_meter_readings WHERE meter_point_id=? AND status IN ('accepted','adjudicated') AND read_at{operator}? ORDER BY read_at DESC,meter_code LIMIT 1",
            (meter_point_id, canonical_instant(instant))).fetchone()
        return dict(row) if row else None

    def consumption(self, meter_point_id: str, *, period_start: str, period_end: str) -> dict | None:
        """账期 [start,end) 的用量：期末读数减期初读数，无期末读数返回 None（触发缺抄表挂起）。"""
        period_start = canonical_instant(period_start); period_end = canonical_instant(period_end)
        with self.database.connect() as connection:
            closing = self.last_reading_before(connection, meter_point_id, period_end, strict=True)
            if closing is None or closing["read_at"] <= period_start:
                return None
            opening = self.last_reading_before(connection, meter_point_id, period_start, strict=True)
            close_value = Decimal(closing["effective_value"])
            open_value = Decimal(opening["effective_value"]) if opening else Decimal(0)
            delta = close_value - open_value
            if delta < 0:
                raise ConflictError(f"计量点 {meter_point_id} 读数倒退，需核查")
            return {"meter_point_id": meter_point_id, "quantity_minor": None,
                    "consumption": str(delta), "opening_meter_code": opening["meter_code"] if opening else None,
                    "closing_meter_code": closing["meter_code"],
                    "opening_value": str(open_value), "closing_value": str(close_value)}
