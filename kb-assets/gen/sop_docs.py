"""生成 200 份 SOP 标准流程文档 DOCX（售前/售后/投诉/物流 各 50 份）。

    python gen/sop_docs.py [--limit 20]

每份 SOP 包含：适用场景、处理流程、时限要求、升级条件、话术要点、常见错误、关联知识。
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from docx_util import bullets, doc_title, heading, kv_table, new_doc, numbered, para, save  # noqa: E402

ROOT = HERE.parent
DATA = ROOT / "data"
OUT = ROOT / "output" / "02-SOP标准流程"

RELATED = {
    "售前": ["03-退换货政策／通用售后总则", "01-产品手册（该商品手册）", "SOP-售后-001 7 天无理由退货"],
    "售后": ["03-退换货政策／通用售后总则（七天无理由、退款时效）", "SOP-物流-039 退货物流跟进",
             "prompts/reply_prompts.md（回复话术约束）"],
    "投诉": ["prompts/handoff.md（转人工话术与交接摘要）", "SOP-售后-041 补偿方案沟通",
             "SOP-投诉-048 投诉归档与复盘"],
    "物流": ["03-退换货政策／物流服务政策（异常件 48 小时判定）", "SOP-售后-025 退货物流异常",
             "物流／订单 API（实时轨迹查询）"],
}


def build_sop(row: dict) -> "object":
    doc = new_doc()
    doc_title(doc, f"SOP：{row['场景名称']}", f"{row['大类']}类标准处理流程 · 编号 {row['sop_id']}")

    heading(doc, "一、适用场景")
    kv_table(doc, [("流程编号", row["sop_id"]), ("业务大类", row["大类"]),
                   ("场景名称", row["场景名称"]), ("触发条件", row["触发条件"])])

    heading(doc, "二、处理流程")
    steps = [s.strip() for s in row["处理步骤"].split("→") if s.strip()]
    clean = []
    for s in steps:
        parts = s.split(".", 1)
        clean.append(parts[1].strip() if len(parts) == 2 and parts[0].isdigit() else s)
    numbered(doc, clean)

    heading(doc, "三、时限要求")
    para(doc, row["时限要求"])

    heading(doc, "四、升级条件")
    para(doc, row["升级条件"])
    para(doc, "升级时必须附带：订单号、已查到的数据、已告知客户的内容、未解决原因。")

    heading(doc, "五、话术要点")
    bullets(doc, [s for s in row["话术要点"].split("；") if s])

    heading(doc, "六、常见错误（不要这么做）")
    bullets(doc, [s for s in row["常见错误"].split("；") if s])

    heading(doc, "七、关联知识")
    bullets(doc, RELATED.get(row["大类"], []))

    heading(doc, "八、合规提示")
    para(doc, "涉及金额、时效、单号的表述必须逐字引用系统数据，不得自行换算或估算；"
              "涉及赔偿、免运费等承诺，客服无权直接决定，需按升级条件上报。")
    return doc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    with (DATA / "scenario_master.csv").open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if args.limit:
        rows = rows[: args.limit]

    print(f"生成 SOP 文档：{len(rows)} 份 ...")
    for i, row in enumerate(rows, 1):
        doc = build_sop(row)
        save(doc, OUT / row["大类"] / f"{row['sop_id']}_{row['场景名称']}.docx")
        if i % 50 == 0:
            print(f"  已生成 {i}/{len(rows)}")
    print(f"完成，输出目录：{OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
