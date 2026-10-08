"""生成 300 份产品手册 DOCX（一 SKU 一份）。

    python gen/product_manuals.py [--limit 30]

每份手册包含：基本信息、产品简介、核心卖点、规格参数、使用保养、保修售后、风险提示。
内容全部由 data/sku_master.csv 派生，保证与 FAQ、SOP、话术库口径一致。

注意：这里**故意不放"常见问题"章节**。
早期版本从 FAQ 库里挑 5 条问答塞进手册，结果同一句答案在"产品手册"和"FAQ 清单"
两处各出现一次，向量检索时互相抢排名，导致政策类问题经常引用到一份产品手册。
实测 30 条检索用例里素材类型命中率只有 43%，主要就是这个造成的。
手册只讲产品本身，问答交给 FAQ 库——这是分工，不是遗漏。
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from docx_util import bullets, doc_title, heading, kv_table, new_doc, para, save, table  # noqa: E402

ROOT = HERE.parent
DATA = ROOT / "data"
OUT = ROOT / "output" / "01-产品手册"


def build_manual(sku: dict) -> "object":
    doc = new_doc()
    doc_title(doc, sku["商品名称"], f"产品手册 · 型号 {sku['型号']}")

    heading(doc, "一、基本信息")
    kv_table(doc, [
        ("商品名称", sku["商品名称"]),
        ("SKU 编号", sku["sku_id"]),
        ("商品型号", sku["型号"]),
        ("所属品类", sku["品类"]),
        ("子类", sku["子类"]),
        ("尺码规格", sku["尺码规格"]),
        ("颜色", sku["颜色"]),
        ("主要材质", sku["材质"]),
        ("零售价", f"￥{sku['售价']}"),
        ("产地", sku["产地"]),
        ("上市日期", sku["上市日期"]),
    ])

    heading(doc, "二、产品简介")
    para(doc, f"{sku['商品名称']}属于{sku['品类']}类目下的{sku['子类']}，"
              f"采用{sku['材质']}，提供{sku['尺码规格']}可选，当前配色为{sku['颜色']}。"
              f"本产品定位日常使用场景，兼顾实用性与耐用性，适合大多数用户群体。")

    heading(doc, "三、核心卖点")
    bullets(doc, sku["核心卖点"].split("；"))

    heading(doc, "四、规格参数")
    table(doc, ["项目", "内容"], [
        ["材质", sku["材质"]],
        ["尺码 / 规格", sku["尺码规格"]],
        ["颜色", sku["颜色"]],
        ["执行标准", "以商品包装标注为准"],
        ["产地", sku["产地"]],
    ])

    heading(doc, "五、使用与保养")
    para(doc, f"保养方式：{sku['保养方式']}。")
    bullets(doc, [
        "首次使用前请先阅读本手册，并检查配件是否齐全。",
        "按上述方式清洁与存放，可有效延长使用寿命。",
        "避免高温、暴晒与长时间潮湿环境。",
    ])

    heading(doc, "六、保修与售后")
    para(doc, f"保修政策：{sku['保修政策']}。")
    bullets(doc, [
        "签收后 7 天内，商品未使用且吊牌/包装完整，可申请七天无理由退货。",
        "属质量问题的退换，往返运费由商家承担；非质量问题由买家承担。",
        "退款在商品寄回并签收后 1-7 个工作日原路退回。",
        "保修需保留订单号或发票，凭订单号可直接查询保修状态。",
    ])

    heading(doc, "七、风险提示")
    para(doc, sku["风险提示"])
    para(doc, "本手册为知识库素材，内容为合成数据，仅用于内部检索与引用链路验证，不作为对外承诺。")
    return doc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只生成前 N 份，用于快速验证")
    args = ap.parse_args()

    with (DATA / "sku_master.csv").open(encoding="utf-8-sig") as f:
        skus = list(csv.DictReader(f))
    if args.limit:
        skus = skus[: args.limit]

    print(f"生成产品手册：{len(skus)} 份 ...")
    for i, sku in enumerate(skus, 1):
        doc = build_manual(sku)
        save(doc, OUT / sku["品类"] / f"{sku['sku_id']}_{sku['子类']}.docx")
        if i % 50 == 0:
            print(f"  已生成 {i}/{len(skus)}")
    print(f"完成，输出目录：{OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
