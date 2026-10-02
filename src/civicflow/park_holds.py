"""结算挂起项：读号冲突、缺失抄表、维保补偿。

开放挂起项带部分唯一索引（同 kind+ref_key 只能有一条 open），因此
重启后任务重复执行也不会产生重复挂起；关账前必须没有开放挂起项。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .database import Database
from .errors import NotFoundError
from .jsonutil import canonical_json
from .timeutil import Clock


OPEN = "open"
RESOLVED = "resolved"


@dataclass(frozen=True)
class HoldBook:
    database: Database
    clock: Clock

    def open(self, kind: str, ref_key: str, *, detail: dict, tenant_org: str | None = None,
             period_id: str | None = None) -> dict:
        with self.database.transaction() as connection:
            return self.open_in(connection, kind, ref_key, detail=detail, tenant_org=tenant_org, period_id=period_id)

    def open_in(self, connection: sqlite3.Connection, kind: str, ref_key: str, *, detail: dict,
                tenant_org: str | None = None, period_id: str | None = None) -> dict:
        now = self.clock.now()
        connection.execute(
            "INSERT OR IGNORE INTO park_holds(hold_id,kind,ref_key,tenant_org,period_id,detail_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (f"hold:{kind}:{ref_key}", kind, ref_key, tenant_org, period_id, canonical_json(detail), OPEN, now))
        if tenant_org is not None or period_id is not None:
            connection.execute(
                "UPDATE park_holds SET tenant_org=COALESCE(tenant_org,?),period_id=COALESCE(period_id,?) WHERE kind=? AND ref_key=? AND status=?",
                (tenant_org, period_id, kind, ref_key, OPEN))
        row = connection.execute("SELECT * FROM park_holds WHERE kind=? AND ref_key=? AND status=?", (kind, ref_key, OPEN)).fetchone()
        return dict(row)

    def resolve(self, kind: str, ref_key: str) -> dict:
        with self.database.transaction() as connection:
            changed = connection.execute("UPDATE park_holds SET status=?,resolved_at=? WHERE kind=? AND ref_key=? AND status=?",
                                         (RESOLVED, self.clock.now(), kind, ref_key, OPEN)).rowcount
            if not changed:
                raise NotFoundError(f"没有开放的挂起项 {kind}/{ref_key}")
            row = connection.execute("SELECT * FROM park_holds WHERE kind=? AND ref_key=?", (kind, ref_key)).fetchone()
            return dict(row)

    def list_open(self, *, kind: str | None = None, tenant_org: str | None = None) -> list[dict]:
        sql = "SELECT * FROM park_holds WHERE status=?"; params: list[object] = [OPEN]
        if kind is not None:
            sql += " AND kind=?"; params.append(kind)
        if tenant_org is not None:
            sql += " AND tenant_org=?"; params.append(tenant_org)
        sql += " ORDER BY created_at,hold_id"
        with self.database.connect() as connection:
            return [dict(r) for r in connection.execute(sql, params).fetchall()]

    def open_keys(self, kind: str) -> set[str]:
        with self.database.connect() as connection:
            return {r["ref_key"] for r in connection.execute("SELECT ref_key FROM park_holds WHERE status=? AND kind=?", (OPEN, kind)).fetchall()}

    def attach_period(self, kind: str, ref_keys: set[str], period_id: str, tenant_org: str) -> None:
        if not ref_keys:
            return
        with self.database.transaction() as connection:
            for key in ref_keys:
                connection.execute("UPDATE park_holds SET period_id=?,tenant_org=COALESCE(tenant_org,?) WHERE kind=? AND ref_key=? AND status=? AND period_id IS NULL",
                                   (period_id, tenant_org, kind, key, OPEN))

    @staticmethod
    def detail(row: dict) -> dict:
        import json
        return json.loads(row["detail_json"])
