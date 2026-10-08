"""SQLite 持久化层：会话、消息、工单、退款单。

为什么要落库：内存字典在后端一重启就清空。真实客服系统里，
客户拿着工单号回来问"上次那个处理得怎么样了"，你查不到才是事故。
而且转人工时，坐席必须能看到这通对话的历史——不然客户还得重讲一遍。

表结构刻意保持简单，够用即可：
    sessions  会话状态（最近处理的订单、轮次、连续负面次数）
    messages  每一轮的消息，用于生成坐席交接摘要
    tickets   工单
    refunds   退款单
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
# 允许用环境变量指定库位置：测试必须跑在独立的库上。
# 踩过的坑：测试和正在运行的后端共用同一个 state.db，会话轮次互相累加，
# 测试结果完全不可复现（第一句就显示成第 9 轮）。
DB_PATH = Path(os.getenv("MOCK_DB_PATH") or (HERE / "data" / "state.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    last_order_no   TEXT NOT NULL DEFAULT '',
    dify_conversation_id TEXT NOT NULL DEFAULT '',   -- Dify 那边的对话 ID
    turn_count      INTEGER NOT NULL DEFAULT 0,
    negative_streak INTEGER NOT NULL DEFAULT 0,
    escalated       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,          -- customer / agent / system
    content     TEXT NOT NULL,
    intent      TEXT NOT NULL DEFAULT '',
    sentiment   TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id   TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL DEFAULT '',
    order_no    TEXT,
    intent      TEXT NOT NULL DEFAULT '',
    sentiment   TEXT NOT NULL DEFAULT '',
    priority    TEXT NOT NULL DEFAULT '普通',
    summary     TEXT NOT NULL DEFAULT '',
    handoff     TEXT NOT NULL DEFAULT '',   -- 交给坐席的完整摘要
    status      TEXT NOT NULL DEFAULT '排队中',
    agent       TEXT NOT NULL DEFAULT '',   -- 接入的坐席
    claimed_at  TEXT NOT NULL DEFAULT '',
    closed_at   TEXT NOT NULL DEFAULT '',
    resolution  TEXT NOT NULL DEFAULT '',   -- 已退款 / 已换货 / 已解释 …
    unresolved_reason TEXT NOT NULL DEFAULT '',  -- 周度迭代的输入
    csat        INTEGER,
    replies     TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refunds (
    refund_id   TEXT PRIMARY KEY,
    request_id  TEXT NOT NULL,
    session_id  TEXT NOT NULL DEFAULT '',
    order_no    TEXT NOT NULL,
    amount      REAL NOT NULL DEFAULT 0,
    item_name   TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT '已受理',
    payload     TEXT NOT NULL DEFAULT '{}',  -- 完整响应，便于原样回放
    created_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_refunds_request ON refunds(request_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> sqlite3.Connection:
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    migrate(conn)
    return conn


# 老库补列：SQLite 没有 ADD COLUMN IF NOT EXISTS，只能先查表结构再补
TICKET_EXTRA_COLUMNS = [
    ("agent", "TEXT NOT NULL DEFAULT ''"),
    ("claimed_at", "TEXT NOT NULL DEFAULT ''"),
    ("closed_at", "TEXT NOT NULL DEFAULT ''"),
    ("resolution", "TEXT NOT NULL DEFAULT ''"),
    ("unresolved_reason", "TEXT NOT NULL DEFAULT ''"),
    ("csat", "INTEGER"),
    ("replies", "TEXT NOT NULL DEFAULT '[]'"),
]

SESSION_EXTRA_COLUMNS = [
    ("dify_conversation_id", "TEXT NOT NULL DEFAULT ''"),
]


def migrate(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(tickets)").fetchall()}
    for col, ddl in TICKET_EXTRA_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE tickets ADD COLUMN {col} {ddl}")
    have_s = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    for col, ddl in SESSION_EXTRA_COLUMNS:
        if col not in have_s:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {ddl}")
    conn.commit()


# ---------------------------------------------------------------- 会话

def get_session(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if not row:
        return {"session_id": session_id, "last_order_no": "", "turn_count": 0,
                "negative_streak": 0, "escalated": 0}
    return dict(row)


def touch_session(conn: sqlite3.Connection, session_id: str, order_no: str = "",
                  negative: bool = False, escalate_reset: bool = False) -> dict[str, Any]:
    """更新会话状态，返回更新后的记录。"""
    now = _now()
    s = get_session(conn, session_id)
    if not s.get("created_at"):
        conn.execute(
            "INSERT INTO sessions (session_id, last_order_no, turn_count, negative_streak,"
            " escalated, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (session_id, order_no or "", 0, 0, 0, now, now))
        s = get_session(conn, session_id)

    streak = 0 if escalate_reset else (s["negative_streak"] + 1 if negative else 0)
    conn.execute(
        "UPDATE sessions SET last_order_no = ?, negative_streak = ?, updated_at = ? "
        "WHERE session_id = ?",
        (order_no or s["last_order_no"], streak, now, session_id))
    conn.commit()
    return get_session(conn, session_id)


# ---------------------------------------------------------------- 消息

def add_message(conn: sqlite3.Connection, session_id: str, role: str, content: str,
                intent: str = "", sentiment: str = "") -> None:
    conn.execute(
        "INSERT INTO messages (session_id, role, content, intent, sentiment, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (session_id, role, content, intent, sentiment, _now()))
    conn.execute("UPDATE sessions SET turn_count = turn_count + 1, updated_at = ? "
                 "WHERE session_id = ?", (_now(), session_id))
    conn.commit()


def set_last_message_intent(conn: sqlite3.Connection, session_id: str, intent: str) -> bool:
    """把这一轮客户消息的意图补写上去（分类器判完之后才拿得到）。

    为什么是"补写最后一条"而不是插入时带上：分类器在「记录对话」之后才跑，
    意图分两步才拿到。补写能保证**一轮对话只有一条客户消息**，
    不会因为多打了一个上报点就多出一条。

    这一列的唯一用途是让"意图识别准确率"有实测数据（tools/eval_intent.py）。
    """
    if not intent:
        return False
    row = conn.execute(
        "SELECT id FROM messages WHERE session_id = ? AND role = 'customer' "
        "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
    if not row:
        return False
    conn.execute("UPDATE messages SET intent = ? WHERE id = ?", (intent, row["id"]))
    conn.commit()
    return True


def set_dify_conversation(conn: sqlite3.Connection, session_id: str, cid: str) -> None:
    """记下"Dify 的对话 ID"，重启后还能把两边对上。"""
    now = _now()
    if not get_session(conn, session_id).get("created_at"):
        conn.execute(
            "INSERT INTO sessions (session_id, last_order_no, dify_conversation_id, turn_count,"
            " negative_streak, escalated, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (session_id, "", cid, 0, 0, 0, now, now))
    conn.execute("UPDATE sessions SET dify_conversation_id = ?, updated_at = ? "
                 "WHERE session_id = ?", (cid, now, session_id))
    conn.commit()


def find_session_by_dify_id(conn: sqlite3.Connection, cid: str) -> str:
    """反查：Dify 的对话 ID 对应哪个客户端会话。查不到返回空串。"""
    if not cid:
        return ""
    row = conn.execute("SELECT session_id FROM sessions WHERE dify_conversation_id = ? "
                       "LIMIT 1", (cid,)).fetchone()
    return row["session_id"] if row else ""


def all_dify_mappings(conn: sqlite3.Connection) -> dict[str, str]:
    """启动时把映射全部读进内存，避免每次请求都查库。"""
    rows = conn.execute("SELECT session_id, dify_conversation_id FROM sessions "
                        "WHERE dify_conversation_id != ''").fetchall()
    return {r["dify_conversation_id"]: r["session_id"] for r in rows}


def active_ticket_for(conn: sqlite3.Connection, session_id: str) -> Optional[dict]:
    """这个会话当前有没有"人工正在管"的工单。

    排队中和处理中都算——工单一旦建出来，就说明这事归人工了，
    AI 不该再插话。否则客户每说一句就新建一张工单，
    人工永远在追着新工单跑，客户觉得根本没人管他。
    """
    if not session_id:
        return None
    row = conn.execute(
        "SELECT * FROM tickets WHERE session_id = ? AND status IN ('排队中', '处理中') "
        "ORDER BY created_at DESC LIMIT 1", (session_id,)).fetchone()
    if not row:
        return None
    t = dict(row)
    try:
        t["replies"] = json.loads(t.get("replies") or "[]")
    except Exception:
        t["replies"] = []
    return t


def list_messages(conn: sqlite3.Connection, session_id: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content, intent, sentiment, created_at FROM messages "
        "WHERE session_id = ? ORDER BY id DESC LIMIT ?", (session_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def build_handoff(conn: sqlite3.Connection, session_id: str, order_no: str = "") -> str:
    """生成给人工坐席的交接摘要。

    这是"不让客户重复叙述"的落地：坐席打开工单就知道客户说过什么、
    系统查到了什么、为什么没解决，不用让客户再讲一遍。
    """
    s = get_session(conn, session_id)
    msgs = list_messages(conn, session_id, limit=8)
    if not msgs and not order_no:
        return ""
    lines = []
    if order_no or s.get("last_order_no"):
        lines.append(f"订单号：{order_no or s['last_order_no']}")
    lines.append(f"会话轮次：{s.get('turn_count', 0)}　连续负面：{s.get('negative_streak', 0)}")
    recent = [m for m in msgs if m["role"] == "customer"][-3:]
    if recent:
        lines.append("客户原话：" + " ／ ".join(m["content"][:40] for m in recent))
    return "\n".join(lines)


# ---------------------------------------------------------------- 工单 / 退款

def save_ticket(conn: sqlite3.Connection, t: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO tickets (ticket_id, session_id, order_no, intent, sentiment,"
        " priority, summary, handoff, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (t["ticket_id"], t.get("session_id", ""), t.get("order_no"), t.get("intent", ""),
         t.get("sentiment", ""), t.get("priority", "普通"), t.get("summary", ""),
         t.get("handoff", ""), t.get("status", "排队中"), t.get("created_at", _now())))
    conn.commit()


TICKET_EDITABLE = {"status", "agent", "claimed_at", "closed_at", "resolution",
                   "unresolved_reason", "csat", "replies", "priority", "summary"}


def list_tickets(conn: sqlite3.Connection, limit: int = 100, status: str = "") -> list[dict]:
    """列工单。高优先级排前面——坐席应该先看到最急的。"""
    order = "CASE priority WHEN '高' THEN 0 ELSE 1 END, created_at DESC"
    if status:
        rows = conn.execute(
            f"SELECT * FROM tickets WHERE status = ? ORDER BY {order} LIMIT ?",
            (status, limit)).fetchall()
    else:
        rows = conn.execute(f"SELECT * FROM tickets ORDER BY {order} LIMIT ?",
                            (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["replies"] = json.loads(d.get("replies") or "[]")
        except Exception:
            d["replies"] = []
        out.append(d)
    return out


def update_ticket(conn: sqlite3.Connection, ticket_id: str, **fields) -> Optional[dict]:
    """更新工单字段（坐席接入 / 回复 / 关闭都会用到）。"""
    sets, vals = [], []
    for k, v in fields.items():
        if k in TICKET_EDITABLE:
            sets.append(f"{k} = ?")
            vals.append(v)
    if sets:
        vals.append(ticket_id)
        conn.execute(f"UPDATE tickets SET {', '.join(sets)} WHERE ticket_id = ?", vals)
        conn.commit()
    t = get_ticket(conn, ticket_id)
    if t:
        try:
            t["replies"] = json.loads(t.get("replies") or "[]")
        except Exception:
            t["replies"] = []
    return t


def append_ticket_reply(conn: sqlite3.Connection, ticket_id: str, agent: str,
                        text: str) -> Optional[dict]:
    t = get_ticket(conn, ticket_id)
    if not t:
        return None
    try:
        replies = json.loads(t.get("replies") or "[]")
    except Exception:
        replies = []
    replies.append({"agent": agent, "text": text, "at": _now()})
    return update_ticket(conn, ticket_id, replies=json.dumps(replies, ensure_ascii=False))


def ticket_stats(conn: sqlite3.Connection) -> dict:
    """坐席工作台顶部的看板数据。"""
    def one(sql: str, *a) -> int:
        return conn.execute(sql, a).fetchone()[0]
    return {
        "排队中": one("SELECT COUNT(*) FROM tickets WHERE status = '排队中'"),
        "处理中": one("SELECT COUNT(*) FROM tickets WHERE status = '处理中'"),
        "已解决": one("SELECT COUNT(*) FROM tickets WHERE status = '已解决'"),
        "高优先级": one("SELECT COUNT(*) FROM tickets WHERE priority = '高' AND status != '已解决'"),
        "总数": one("SELECT COUNT(*) FROM tickets"),
    }


def unresolved_ranking(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """未解决原因排行——周度迭代就看这张表。"""
    rows = conn.execute(
        "SELECT unresolved_reason AS reason, COUNT(*) AS n FROM tickets "
        "WHERE unresolved_reason != '' GROUP BY unresolved_reason ORDER BY n DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


def resolution_distribution(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT resolution, COUNT(*) AS n FROM tickets WHERE resolution != '' "
        "GROUP BY resolution ORDER BY n DESC").fetchall()
    return [dict(r) for r in rows]


def business_metrics(conn: sqlite3.Connection) -> dict:
    """算给管理者看的业务指标。

    定义必须写死，否则这些数字可以被随便美化：
        转人工率   = 工单数 ÷ 有对话的会话数
        自助解决率 = 1 − 转人工率（粗口径，严格口径要看客户是否复访）
        平均轮次   = 会话平均消息轮数，轮次越高说明一次没说清
    """
    def scalar(sql: str):
        row = conn.execute(sql).fetchone()
        return row[0] if row and row[0] is not None else 0

    sessions = scalar("SELECT COUNT(*) FROM sessions WHERE turn_count > 0")
    tickets = scalar("SELECT COUNT(*) FROM tickets")
    resolved = scalar("SELECT COUNT(*) FROM tickets WHERE status = '已解决'")
    messages = scalar("SELECT COUNT(*) FROM messages")
    avg_turns = scalar("SELECT AVG(CAST(turn_count AS REAL)) FROM sessions WHERE turn_count > 0")
    csat = scalar("SELECT AVG(CAST(csat AS REAL)) FROM tickets WHERE csat IS NOT NULL")
    csat_n = scalar("SELECT COUNT(*) FROM tickets WHERE csat IS NOT NULL")
    refunds = scalar("SELECT COUNT(*) FROM refunds")
    refund_amount = scalar("SELECT COALESCE(SUM(amount), 0) FROM refunds")

    handoff_rate = (tickets / sessions) if sessions else 0
    return {
        "sessions": sessions,
        "messages": messages,
        "avg_turns": round(avg_turns, 2),
        "tickets": tickets,
        "tickets_resolved": resolved,
        "handoff_rate": round(handoff_rate, 4),
        "self_service_rate": round(max(0.0, 1 - handoff_rate), 4),
        "csat": round(csat, 2) if csat_n else None,
        "csat_count": csat_n,
        "refunds": refunds,
        "refund_amount": round(refund_amount, 2),
    }


def get_ticket(conn: sqlite3.Connection, ticket_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
    return dict(row) if row else None


def save_refund(conn: sqlite3.Connection, r: dict, request_id: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO refunds (refund_id, request_id, session_id, order_no, amount,"
        " item_name, status, payload, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (r["refund_id"], request_id, r.get("session_id", ""), r["order_no"],
         r.get("amount", 0), r.get("item", ""), r.get("status", "已受理"),
         json.dumps(r, ensure_ascii=False), r.get("created_at", _now())))
    conn.commit()


def get_refund_by_request(conn: sqlite3.Connection, request_id: str) -> Optional[dict]:
    row = conn.execute("SELECT payload FROM refunds WHERE request_id = ?",
                       (request_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def get_refund_by_id(conn: sqlite3.Connection, refund_id: str) -> Optional[dict]:
    row = conn.execute("SELECT payload FROM refunds WHERE refund_id = ?",
                       (refund_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def stats(conn: sqlite3.Connection) -> dict:
    def one(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]
    return {
        "sessions": one("SELECT COUNT(*) FROM sessions"),
        "messages": one("SELECT COUNT(*) FROM messages"),
        "tickets": one("SELECT COUNT(*) FROM tickets"),
        "refunds": one("SELECT COUNT(*) FROM refunds"),
    }
