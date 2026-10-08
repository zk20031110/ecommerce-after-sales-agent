"""生成 15 万条历史客服工单（SQLite + 抽样 CSV）。\n
    python gen/tickets_db.py [--rows 150000]

工单**不直接向量化入知识库**，用途是从中挖掘高频 QA 对，
补充进 FAQ 清单（见 pipeline/mine_tickets.py）。
"""

from __future__ import annotations

import argparse
import csv
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ROOT = HERE.parent
DATA = ROOT / "data"
DB = DATA / "tickets.db"

CHANNELS = ["App 内客服", "微信小程序", "电话", "网页在线咨询", "抖音私信"]
RESOLVERS = ["AI 自助", "人工客服"]


def load_questions() -> dict[str, list[tuple[str, str]]]:
    idx: dict[str, list[tuple[str, str]]] = {}
    with (DATA / "qa_seed.csv").open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            idx.setdefault(r["分类"], []).append((r["问题"], r["标准答案"]))
    return idx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=150000)
    ap.add_argument("--sample", type=int, default=10000)
    args = ap.parse_args()

    rnd = random.Random(20261004)
    qidx = load_questions()
    kinds = list(qidx.keys())
    orders = [r["sku_id"].replace("-", "") + "2024" for r in
              csv.DictReader((DATA / "sku_master.csv").open(encoding="utf-8-sig"))]
    # 真实工单里一定有知识库没覆盖的说法。留出 30% 用「场景名 + 口语后缀」造未覆盖问题，
    # 否则挖掘脚本永远找不到缺口，那份报告就是自欺欺人。
    scenes = [r["场景名称"] for r in
              csv.DictReader((DATA / "scenario_master.csv").open(encoding="utf-8-sig"))]
    loose_suffix = ["怎么处理", "是什么情况", "麻烦帮我看看", "这个怎么弄",
                    "有规定吗", "到底行不行", "能不能给个说法"]
    generic_answers = [
        "", "这个问题我帮你转人工核实一下。", "麻烦提供订单号，我帮你确认。",
        "稍等，我需要核实一下再回复你。",
    ]

    if DB.exists():
        DB.unlink()
    conn = sqlite3.connect(DB)
    conn.execute("""
        CREATE TABLE tickets (
            ticket_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            channel TEXT NOT NULL,
            user_id TEXT NOT NULL,
            order_no TEXT,
            category TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT,
            resolved_by TEXT NOT NULL,
            first_response_seconds INTEGER NOT NULL,
            rounds INTEGER NOT NULL,
            satisfied INTEGER,
            refund_amount REAL DEFAULT 0,
            escalated INTEGER DEFAULT 0
        )
    """)

    start = datetime(2026, 4, 1, 9, 0, 0)
    batch = []
    for i in range(args.rows):
        cat = rnd.choice(kinds)
        if rnd.random() < 0.3:
            q = f"{rnd.choice(scenes)}{rnd.choice(loose_suffix)}"
            a = rnd.choice(generic_answers)
        else:
            q, a = rnd.choice(qidx[cat])
            if rnd.random() < 0.35:
                q = f"{q}（{rnd.choice(['着急', '麻烦快点', '已经等很久了', '又出问题了'])}）"
        created = start + timedelta(minutes=rnd.randint(0, 180 * 24 * 60))
        by = "AI 自助" if rnd.random() < 0.62 else "人工客服"
        first_resp = rnd.randint(1, 12) if by == "AI 自助" else rnd.randint(30, 600)
        rounds = 1 if by == "AI 自助" else rnd.randint(2, 12)
        satisfied = None if rnd.random() < 0.55 else (1 if (by == "AI 自助" and rnd.random() < 0.86)
                                                      else 1 if rnd.random() < 0.72 else 0)
        escalated = 1 if (cat == "投诉" and rnd.random() < 0.8) or rnd.random() < 0.08 else 0
        batch.append((
            f"T{created.strftime('%y%m%d')}{i:07d}",
            created.isoformat(timespec="seconds"),
            rnd.choice(CHANNELS),
            f"U{rnd.randint(100000, 999999)}",
            rnd.choice(orders) if rnd.random() > 0.25 else None,
            cat, q, a, by, first_resp, rounds, satisfied,
            round(rnd.choice([0, 0, 0, 0, 0, 49, 89, 199, 268, 599]), 2),
            escalated,
        ))
        if len(batch) >= 20000:
            conn.executemany("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        conn.executemany("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)

    conn.execute("CREATE INDEX idx_cat ON tickets(category)")
    conn.execute("CREATE INDEX idx_question ON tickets(question)")
    conn.execute("CREATE INDEX idx_resolver ON tickets(resolved_by)")
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    ai_rate = conn.execute(
        "SELECT ROUND(100.0*SUM(resolved_by='AI 自助')/COUNT(*),1) FROM tickets").fetchone()[0]
    print(f"工单库生成完成：{DB.name}  共 {total} 条，AI 自助占比 {ai_rate}%")

    top = conn.execute("""
        SELECT question, category, COUNT(*) c FROM tickets
        GROUP BY question ORDER BY c DESC LIMIT 20
    """).fetchall()
    with (DATA / "tickets_top_questions.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["问题", "分类", "出现次数"])
        w.writerows(top)
    print(f"高频问题 Top20 已导出：tickets_top_questions.csv（最高 {top[0][2]} 次）")

    with (DATA / "tickets_sample.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["ticket_id", "created_at", "channel", "order_no", "category",
                    "question", "answer", "resolved_by", "first_response_seconds",
                    "rounds", "satisfied", "refund_amount", "escalated"])
        w.writerows(conn.execute(
            f"SELECT * FROM tickets ORDER BY created_at DESC LIMIT {args.sample}"))
    print(f"抽样 CSV 已导出：tickets_sample.csv（{args.sample} 条）")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
