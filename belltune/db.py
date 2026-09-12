# -*- coding: utf-8 -*-
"""SQLite 数据层：钟体、母线、目标、测量、方案版本、车削轮次、切削记录。"""
import json
import os
import sqlite3
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "belltune.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bell (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  density REAL NOT NULL DEFAULT 8800,
  a4 REAL NOT NULL DEFAULT 440,
  min_thick REAL NOT NULL DEFAULT 8.0,
  pass_depth REAL NOT NULL DEFAULT 1.0,
  lathe_step REAL NOT NULL DEFAULT 0.1,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_point (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bell_id INTEGER NOT NULL, seq INTEGER NOT NULL,
  z REAL NOT NULL, r REAL NOT NULL, thick REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS target (
  bell_id INTEGER NOT NULL, partial TEXT NOT NULL,
  freq REAL NOT NULL, tol_cents REAL NOT NULL DEFAULT 10,
  PRIMARY KEY (bell_id, partial)
);
CREATE TABLE IF NOT EXISTS measurement (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bell_id INTEGER NOT NULL, round_tag TEXT NOT NULL DEFAULT 'as-found',
  partial TEXT NOT NULL, freq REAL NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.5, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plan (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bell_id INTEGER NOT NULL, version INTEGER NOT NULL,
  name TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
  depths TEXT NOT NULL,           -- JSON: 每环带去料深度
  status TEXT NOT NULL DEFAULT 'draft',  -- draft | active | done
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS round (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id INTEGER NOT NULL, seq INTEGER NOT NULL,
  kind TEXT NOT NULL,             -- cut | measure
  payload TEXT NOT NULL DEFAULT '{}',  -- cut: {depths:[...]}; measure: {measured:{p:Hz}|null}
  done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cut_record (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bell_id INTEGER NOT NULL, plan_id INTEGER, round_seq INTEGER,
  depths TEXT NOT NULL, before TEXT NOT NULL, after TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.7, created REAL NOT NULL
);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db():
    con = connect()
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def q(sql, args=(), one=False):
    con = connect()
    cur = con.execute(sql, args)
    rows = cur.fetchall()
    con.close()
    if one:
        return dict(rows[0]) if rows else None
    return [dict(r) for r in rows]


def execute(sql, args=()):
    con = connect()
    cur = con.execute(sql, args)
    con.commit()
    lid = cur.lastrowid
    con.close()
    return lid


# ------------------------------------------------------------- 组装状态
def bell_state(bell_id):
    bell = q("SELECT * FROM bell WHERE id=?", (bell_id,), one=True)
    if not bell:
        return None
    bell["profile"] = q(
        "SELECT z, r, thick FROM profile_point WHERE bell_id=? ORDER BY seq",
        (bell_id,))
    bell["targets"] = {t["partial"]: dict(freq=t["freq"], tol_cents=t["tol_cents"])
                       for t in q("SELECT * FROM target WHERE bell_id=?", (bell_id,))}
    bell["measurements"] = q(
        "SELECT round_tag, partial, freq, confidence, created FROM measurement"
        " WHERE bell_id=? ORDER BY created", (bell_id,))
    bell["plans"] = q(
        "SELECT id, version, name, note, status, created FROM plan"
        " WHERE bell_id=? ORDER BY version", (bell_id,))
    return bell


def latest_freqs(bell_id):
    """每分音最新一次测量频率（按时间），以及对应置信度。"""
    rows = q("SELECT partial, freq, confidence, created FROM measurement"
             " WHERE bell_id=? ORDER BY created, id", (bell_id,))
    out, conf = {}, {}
    for r in rows:
        out[r["partial"]] = r["freq"]
        conf[r["partial"]] = r["confidence"]
    return out, conf


def cut_records(bell_id):
    recs = q("SELECT depths, before, after, confidence FROM cut_record"
             " WHERE bell_id=? ORDER BY created", (bell_id,))
    return [dict(depths=json.loads(r["depths"]), before=json.loads(r["before"]),
                 after=json.loads(r["after"]), confidence=r["confidence"])
            for r in recs]


def plan_full(plan_id):
    p = q("SELECT * FROM plan WHERE id=?", (plan_id,), one=True)
    if not p:
        return None
    p["depths"] = json.loads(p["depths"])
    p["rounds"] = q("SELECT seq, kind, payload, done FROM round"
                    " WHERE plan_id=? ORDER BY seq", (plan_id,))
    for r in p["rounds"]:
        r["payload"] = json.loads(r["payload"])
    return p


def next_version(bell_id):
    row = q("SELECT MAX(version) v FROM plan WHERE bell_id=?", (bell_id,), one=True)
    return (row["v"] or 0) + 1


def now():
    return time.time()
