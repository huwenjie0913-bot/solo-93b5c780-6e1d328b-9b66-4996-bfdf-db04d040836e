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

-- 静置复测筛查：批次参数留存。筛查只追加新批次，绝不回写 plans/plan_versions。
CREATE TABLE IF NOT EXISTS screening_batches (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT,
    parameters     TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS screening_cells (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         INTEGER NOT NULL
                     REFERENCES screening_batches(id) ON DELETE CASCADE,
    cell_id          TEXT NOT NULL,
    verdict          TEXT NOT NULL,
    sample_count     INTEGER NOT NULL,
    used_sample_count INTEGER NOT NULL,
    observation_hours REAL,
    slope_v_per_day  REAL,
    drop_rate_v_per_day REAL,
    r_squared        REAL,
    result_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE(batch_id, cell_id)
);

-- 原始复测测量：只插入、不更新（后续筛查/重算不得改写原始测量）。
CREATE TABLE IF NOT EXISTS screening_samples (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    screening_cell_id  INTEGER NOT NULL
                       REFERENCES screening_cells(id) ON DELETE CASCADE,
    seq                INTEGER NOT NULL,
    sampled_at         TEXT NOT NULL,
    ocv_v              REAL NOT NULL,
    temperature_c      REAL NOT NULL,
    ocv_corrected_v    REAL NOT NULL,
    used_in_fit        INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_screening_cells_cell
    ON screening_cells(cell_id, batch_id);
CREATE INDEX IF NOT EXISTS idx_screening_samples_cell
    ON screening_samples(screening_cell_id);
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


# ---------------------------------------------------------------------------
# 静置复测筛查持久化（只追加；不触碰 plans / plan_versions / cells）
# ---------------------------------------------------------------------------

def create_screening_batch(
    conn: sqlite3.Connection,
    name: str | None,
    parameters: dict[str, float],
    cell_results: list[dict[str, Any]],
) -> tuple[int, str]:
    """原子写入一个筛查批次（批次 + 逐只结论 + 原始测量），返回 (batch_id, created_at)。"""
    created_at = _now()
    cur = conn.execute(
        "INSERT INTO screening_batches(name, parameters, created_at) VALUES (?,?,?)",
        (name, json.dumps(parameters, ensure_ascii=False), created_at),
    )
    batch_id = cur.lastrowid

    for result in cell_results:
        fit = result.get("fit") or {}
        sc = conn.execute(
            """INSERT INTO screening_cells(batch_id, cell_id, verdict, sample_count,
                       used_sample_count, observation_hours, slope_v_per_day,
                       drop_rate_v_per_day, r_squared, result_json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (batch_id, result["cell_id"], result["verdict"],
             result["sample_count"], result["used_sample_count"],
             result["observation_hours"], fit.get("slope_v_per_day"),
             fit.get("drop_rate_v_per_day"), fit.get("r_squared"),
             json.dumps(result, ensure_ascii=False), created_at),
        )
        screening_cell_id = sc.lastrowid
        for sample in result["samples"]:
            conn.execute(
                """INSERT INTO screening_samples(screening_cell_id, seq, sampled_at,
                           ocv_v, temperature_c, ocv_corrected_v, used_in_fit)
                   VALUES (?,?,?,?,?,?,?)""",
                (screening_cell_id, sample["seq"], sample["sampled_at"],
                 sample["ocv_v"], sample["temperature_c"],
                 sample["ocv_corrected_v"], 1 if sample["used_in_fit"] else 0),
            )
    return batch_id, created_at


def get_screening_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM screening_batches WHERE id=?", (batch_id,)
    ).fetchone()


def list_screening_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT b.id, b.name, b.parameters, b.created_at,
                  COUNT(sc.id) AS cell_count,
                  SUM(CASE WHEN sc.verdict='STABLE' THEN 1 ELSE 0 END) AS stable_count,
                  SUM(CASE WHEN sc.verdict='RETEST' THEN 1 ELSE 0 END) AS retest_count,
                  SUM(CASE WHEN sc.verdict='QUARANTINE' THEN 1 ELSE 0 END)
                      AS quarantined_count
           FROM screening_batches b
           LEFT JOIN screening_cells sc ON sc.batch_id = b.id
           GROUP BY b.id
           ORDER BY b.id""",
    ).fetchall()


def list_screening_cells(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT id, batch_id, cell_id, verdict, sample_count, used_sample_count,
                  observation_hours, slope_v_per_day, drop_rate_v_per_day, r_squared,
                  result_json, created_at
           FROM screening_cells WHERE batch_id=? ORDER BY id""",
        (batch_id,),
    ).fetchall()


def list_screening_samples(conn: sqlite3.Connection, screening_cell_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT seq, sampled_at, ocv_v, temperature_c, ocv_corrected_v, used_in_fit
           FROM screening_samples WHERE screening_cell_id=? ORDER BY seq""",
        (screening_cell_id,),
    ).fetchall()


def get_latest_screening_map(
    conn: sqlite3.Connection, cell_ids: list[str]
) -> dict[str, sqlite3.Row]:
    """查询给定电芯**最新批次**的筛查结论（batch id 最大即最新）。

    从未做过筛查的电芯不在返回字典中；筛查历史不影响已有方案版本。
    """
    if not cell_ids:
        return {}
    placeholders = ",".join("?" for _ in cell_ids)
    rows = conn.execute(
        f"""SELECT sc.* FROM screening_cells sc
            JOIN (
                SELECT cell_id, MAX(batch_id) AS max_batch
                FROM screening_cells WHERE cell_id IN ({placeholders})
                GROUP BY cell_id
            ) latest ON latest.cell_id = sc.cell_id
                     AND latest.max_batch = sc.batch_id""",
        cell_ids,
    ).fetchall()
    return {row["cell_id"]: row for row in rows}
