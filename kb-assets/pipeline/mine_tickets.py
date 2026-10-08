"""从 15 万条历史工单里挖掘高频 QA，找出知识库没覆盖的问题。

    python pipeline/mine_tickets.py

注意：工单**不直接向量化入知识库**（里面有客户隐私、口语化表达、临时状态），
正确用法是先挖掘再人工确认，把确认过的补进 FAQ 清单。

产出：
    data/tickets_mining_report.md   挖掘报告（含未覆盖问题清单）
    data/tickets_qa_candidates.csv  候选 QA，人工确认后补进 FAQ
"""

from __future__ import annotations

import csv
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB = DATA / "tickets.db"

SUFFIX = re.compile(r"（(?:着急|麻烦快点|已经等很久了|又出问题了)）$")
CLEAN = re.compile(r"[，。！？、\s]+")


def normalize(q: str) -> str:
    return CLEAN.sub("", SUFFIX.sub("", q).strip())


def main() -> int:
    if not DB.exists():
        print("先跑 python gen/tickets_db.py")
        return 1

    conn = sqlite3.connect(DB)
    rows = conn.execute("""
        SELECT question, category, resolved_by, rounds, satisfied, escalated
        FROM tickets
    """).fetchall()
    conn.close()

    agg: dict[str, dict] = defaultdict(lambda: {
        "count": 0, "category": "", "ai": 0, "human": 0,
        "rounds": 0, "sat_yes": 0, "sat_total": 0, "escalated": 0,
    })
    for q, cat, by, rounds, sat, esc in rows:
        k = normalize(q)
        a = agg[k]
        a["count"] += 1
        a["category"] = cat
        a["ai"] += 1 if by == "AI 自助" else 0
        a["human"] += 1 if by != "AI 自助" else 0
        a["rounds"] += rounds
        if sat is not None:
            a["sat_total"] += 1
            a["sat_yes"] += 1 if sat == 1 else 0
        a["escalated"] += esc

    # 知识库现有覆盖（用规范化的 FAQ 问题做比对）
    covered = set()
    with (DATA / "qa_seed.csv").open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            covered.add(normalize(r["问题"]))

    ranked = sorted(agg.items(), key=lambda kv: -kv[1]["count"])
    uncovered = [(k, v) for k, v in ranked if k not in covered]

    lines = [
        "# 工单挖掘报告", "",
        f"- 工单总量：{len(rows):,} 条",
        f"- 去重后不同问题：{len(agg):,} 个",
        f"- 知识库（FAQ 清单）已覆盖：{len(agg) - len(uncovered):,} 个",
        f"- **未覆盖：{len(uncovered):,} 个** ← 这些是应该补进 FAQ 的候选",
        "",
        "## 高频问题 Top 20", "",
        "| 排名 | 问题 | 分类 | 出现次数 | AI 自助率 | 平均轮次 | 满意度 | 已升级 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, (q, a) in enumerate(ranked[:20], 1):
        ai_rate = f"{a['ai'] / a['count']:.0%}"
        sat = f"{a['sat_yes'] / a['sat_total']:.0%}" if a["sat_total"] else "—"
        rounds = f"{a['rounds'] / a['count']:.1f}"
        lines.append(f"| {i} | {q[:28]} | {a['category']} | {a['count']:,} "
                     f"| {ai_rate} | {rounds} | {sat} | {a['escalated']} |")

    lines += ["", "## 未覆盖的高频问题 Top 30（建议补进 FAQ）", "",
              "| 问题 | 分类 | 出现次数 | AI 自助率 | 平均轮次 |", "|---|---|---|---|---|"]
    for q, a in uncovered[:30]:
        lines.append(f"| {q[:30]} | {a['category']} | {a['count']:,} "
                     f"| {a['ai'] / a['count']:.0%} | {a['rounds'] / a['count']:.1f} |")

    lines += ["", "## 结论与动作", "",
              f"1. 未覆盖问题合计出现 {sum(a['count'] for _, a in uncovered):,} 次，"
              f"占全部工单的 {sum(a['count'] for _, a in uncovered) / len(rows):.1%}。",
              "2. 优先补覆盖率低、轮次高的问题——轮次高说明既有的自动答复没解决，"
              "客户被迫重复描述。",
              "3. 补进 FAQ 时要同时写「常见问法」，工单里的原话就是最好的问法素材。",
              "4. 工单本身不入知识库：含客户隐私与临时状态，向量化后会被检索出来当成政策依据。"]

    (DATA / "tickets_mining_report.md").write_text("\n".join(lines), encoding="utf-8")

    with (DATA / "tickets_qa_candidates.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["问题", "分类", "出现次数", "AI自助率", "平均轮次", "满意度",
                    "已升级", "建议标准答案", "人工确认", "是否已补充"])
        for q, a in uncovered[:200]:
            w.writerow([
                q, a["category"], a["count"], f"{a['ai'] / a['count']:.0%}",
                f"{a['rounds'] / a['count']:.1f}",
                f"{a['sat_yes'] / a['sat_total']:.0%}" if a["sat_total"] else "",
                a["escalated"], "", "", "",
            ])

    print(f"挖掘完成：{len(agg):,} 个不同问题，其中 {len(uncovered):,} 个未覆盖")
    print("  → data/tickets_mining_report.md")
    print("  → data/tickets_qa_candidates.csv（Top 200 候选，待人工确认）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
