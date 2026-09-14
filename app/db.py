"""SQLite 持久层：方案、版本、分组快照、电芯档案。"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT,
    topology      TEXT NOT NULL,
    rated_capacity_ah REAL NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id      INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL,
    thresholds   TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    note         TEXT,
    UNIQUE(plan_id, version)
);

CREATE TABLE IF NOT EXISTS cells (
    cell_id        TEXT PRIMARY KEY,
    capacity_ah    REAL NOT NULL,
    resistance_mohm REAL NOT NULL,
    ocv_v          REAL NOT NULL,
    cycles         INTEGER NOT NULL,
    temperature_c  REAL NOT NULL,
    updated_at     TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def upsert_cell(conn: sqlite3.Connection, cell: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO cells(cell_id, capacity_ah, resistance_mohm, ocv_v,
                             cycles, temperature_c, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(cell_id) DO UPDATE SET
               capacity_ah=excluded.capacity_ah,
               resistance_mohm=excluded.resistance_mohm,
               ocv_v=excluded.ocv_v,
               cycles=excluded.cycles,
               temperature_c=excluded.temperature_c,
               updated_at=excluded.updated_at""",
        (cell["cell_id"], cell["capacity_ah"], cell["resistance_mohm"],
         cell["ocv_v"], int(cell["cycles"]), cell["temperature_c"], _now()),
    )


def create_plan(
    conn: sqlite3.Connection,
    name: str | None,
    topology: dict[str, Any],
    rated_capacity_ah: float,
    thresholds: dict[str, float],
    result: dict[str, Any],
    note: str | None = None,
) -> tuple[int, int]:
    cur = conn.execute(
        "INSERT INTO plans(name, topology, rated_capacity_ah, created_at) VALUES (?,?,?,?)",
        (name, json.dumps(topology, ensure_ascii=False), rated_capacity_ah, _now()),
    )
    plan_id = cur.lastrowid
    version = 1
    conn.execute(
        """INSERT INTO plan_versions(plan_id, version, thresholds, result_json, created_at, note)
           VALUES (?,?,?,?,?,?)""",
        (plan_id, version, json.dumps(thresholds, ensure_ascii=False),
         json.dumps(result, ensure_ascii=False), _now(), note),
    )
    return plan_id, version


def add_version(
    conn: sqlite3.Connection,
    plan_id: int,
    thresholds: dict[str, float],
    result: dict[str, Any],
    note: str | None = None,
) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM plan_versions WHERE plan_id=?",
        (plan_id,),
    ).fetchone()
    version = row["v"] + 1
    conn.execute(
        """INSERT INTO plan_versions(plan_id, version, thresholds, result_json, created_at, note)
           VALUES (?,?,?,?,?,?)""",
        (plan_id, version, json.dumps(thresholds, ensure_ascii=False),
         json.dumps(result, ensure_ascii=False), _now(), note),
    )
    return version


def get_plan(conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()


def get_version(conn: sqlite3.Connection, plan_id: int, version: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM plan_versions WHERE plan_id=? AND version=?",
        (plan_id, version),
    ).fetchone()


def list_versions(conn: sqlite3.Connection, plan_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT id, plan_id, version, thresholds, created_at, note,
                  json_array_length(json_extract(result_json, '$.groups')) AS group_count
           FROM plan_versions WHERE plan_id=? ORDER BY version""",
        (plan_id,),
    ).fetchall()


def load_result(conn: sqlite3.Connection, plan_id: int, version: int) -> dict[str, Any] | None:
    row = get_version(conn, plan_id, version)
    if row is None:
        return None
    return json.loads(row["result_json"])
