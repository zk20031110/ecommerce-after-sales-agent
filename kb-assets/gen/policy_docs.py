"""生成退换货政策文档：12 个品类细则 + 3 份通用政策 = 15 份。

    python gen/policy_docs.py

同时输出 DOCX（可打印、可编辑）与 Markdown（方便直接拖进 Dify 知识库）两种格式。
两份内容完全一致，避免同一政策出现两个口径。
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from docx_util import bullets, doc_title, heading, kv_table, new_doc, para, save  # noqa: E402
from templates import BRAND  # noqa: E402

ROOT = HERE.parent
DATA = ROOT / "data"
OUT = ROOT / "output" / "03-退换货政策"

GENERAL = [
    {
        "name": "通用售后总则",
        "scope": "全品类",
        "rules": [
            ("七天无理由退货", "自签收当日 0 点起算 7 天内，商品未使用、吊牌完整、不影响二次销售可申请无理由退货。"),
            ("退货运费", "非质量问题由买家承担，含运费险的按保单赔付；质量问题往返运费由商家承担。"),
            ("退款时效", "商品寄回并签收后 1-7 个工作日原路退回。"),
            ("换货规则", "7 天内、同款有库存可换，限一次；运费按是否质量问题区分承担方。"),
            ("价保规则", "签收后 15 天内同款同规格官方降价可退差价，每单限一次。"),
            ("质量问题", "签收后 30 天内可退货、换货或维修，需提供问题照片，运费由商家承担。"),
            ("超期处理", "超出上述期限的申请需人工审核，客服不得直接承诺结果。"),
            ("隐私保护", "客服不会索要验证码、银行卡号或完整身份证号，退款一律原路退回。"),
        ],
    },
    {
        "name": "物流服务政策",
        "scope": "全品类",
        "rules": [
            ("发货时效", "付款后 48 小时内发货，预售商品以商品页标注时间为准。"),
            ("配送时效", "普通地区 2-4 天，偏远地区 4-7 天，以快递方承诺为准。"),
            ("运费标准", "满 99 元包邮（新疆、西藏、港澳台除外），未满收取 8 元。"),
            ("异常件判定", "物流轨迹连续 48 小时未更新即视为异常件，24 小时内反馈处理结果。"),
            ("丢件赔付", "确认丢件后可重新发货或全额退款，运费由商家承担；已保价按保价金额赔付。"),
            ("破损处理", "签收后 48 小时内提供外包装与商品照片，可补发或全额退款。"),
            ("改派与改址", "未发货可免费改址；已发货需联系快递方改派，部分区域可能产生费用。"),
        ],
    },
    {
        "name": "发票与价保政策",
        "scope": "全品类",
        "rules": [
            ("电子发票", "订单详情页自助申请，24 小时内开具并发送至预留邮箱。"),
            ("企业发票", "需提供公司全称、税号；批量采购可开具增值税专用发票。"),
            ("发票重开", "开票后 30 天内可申请重开，跨月需人工处理；纸质发票不支持自助重开。"),
            ("价保范围", "签收后 15 天内同款同规格官方降价，差价 = 实付金额 − 当前售价。"),
            ("价保排除", "优惠券、红包、积分抵扣、限时秒杀、直播专享价、清仓价不参与价保。"),
            ("价保次数", "每个订单限申请一次，需提供降价截图。"),
        ],
    },
]


def category_doc(cat: dict, skus: list[dict]) -> dict:
    """按品类生成政策文档，条款从该品类 SKU 的真实字段派生。"""
    sample = skus[0] if skus else {}
    return {
        "name": f"{cat['name']}类退换货政策",
        "scope": f"{cat['name']}（{len(skus)} 个 SKU）",
        "rules": [
            ("适用范围", f"本政策适用于{BRAND}在售的{cat['name']}类商品，共 {len(skus)} 个 SKU。"),
            ("七天无理由", f"{cat['warranty']}。"),
            ("商品状态要求", f"退货需保持未使用状态；保养要求：{sample.get('保养方式', '见包装说明')}。"),
            ("退货运费", "非质量问题由买家承担；质量问题往返运费由商家承担。"),
            ("退款时效", "寄回签收后 1-7 个工作日原路退回。"),
            ("特殊限制", sample.get("风险提示", "无特殊限制")),
            ("超期处理", "超出 7 天的申请需人工审核，客服不得直接承诺结果。"),
            ("质保通道", "质量问题签收后 30 天内可申请退货、换货或维修，需提供照片。"),
        ],
    }


def to_markdown(doc_def: dict) -> str:
    lines = [f"# {BRAND} · {doc_def['name']}", "",
             f"**适用范围**：{doc_def['scope']}", "",
             "> 本文档为知识库素材，内容为合成数据，仅用于内部检索验证。", ""]
    for title, body in doc_def["rules"]:
        lines.append(f"## 【{title}】")
        lines.append(body)
        lines.append("")
    return "\n".join(lines)


def to_docx(doc_def: dict, path: Path) -> None:
    doc = new_doc()
    doc_title(doc, doc_def["name"], f"{BRAND} · 售后政策文档")
    heading(doc, "适用范围")
    para(doc, doc_def["scope"])
    for title, body in doc_def["rules"]:
        heading(doc, title, 2)
        para(doc, body)
    heading(doc, "附则")
    bullets(doc, [
        "本政策自发布之日起生效，如有调整以最新版本为准。",
        "本政策未尽事宜，按国家相关法律法规与平台规则执行。",
        "本文档为合成数据，仅用于内部知识库检索与引用链路验证。",
    ])
    save(doc, path)


def main() -> int:
    with (DATA / "sku_master.csv").open(encoding="utf-8-sig") as f:
        skus = list(csv.DictReader(f))

    docs = list(GENERAL)
    seen = []
    for sku in skus:
        if sku["品类"] not in seen:
            seen.append(sku["品类"])
    for name in seen:
        cat = next(c for c in __import__("templates").CATEGORIES if c["name"] == name)
        docs.append(category_doc(cat, [s for s in skus if s["品类"] == name]))

    print(f"生成政策文档：{len(docs)} 份（DOCX + Markdown 双份）...")
    for d in docs:
        safe = d["name"].replace("/", "-")
        to_docx(d, OUT / f"{safe}.docx")
        (OUT / f"{safe}.md").write_text(to_markdown(d), encoding="utf-8")
        print(f"  · {d['name']}")
    print(f"完成，输出目录：{OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
