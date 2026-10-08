"""生成客服话术库 Excel（300 条标准话术模板）。

    python gen/scripts_library.py

5 大类 × 60 条：首响 / 安抚 / 拒绝 / 催办 / 收尾。
话术里保留 {订单号}、{金额} 这类变量占位，接进 Agent 后可被系统数据填充。
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from templates import rng  # noqa: E402

DATA = HERE.parent / "data"
OUT = HERE.parent / "output" / "05-客服话术库"

HEAD_FILL = PatternFill("solid", fgColor="375623")
HEAD_FONT = Font(color="FFFFFF", bold=True, size=11)

TEMPLATES = {
    "首响": [
        ("通用开场", "你好，我是{品牌}售后客服小岚。请把订单号发我，我直接帮你查。", "neutral"),
        ("已知订单号", "看到你的订单 {订单号} 了，我这就帮你核实，稍等几秒。", "neutral"),
        ("已知诉求", "你是想处理{诉求}对吧？我帮你按最新状态确认一下。", "neutral"),
        ("查询中", "正在帮你查询{订单号}的最新状态，马上给你结果。", "neutral"),
        ("需要信息", "为了准确判断，还需要你补充一下 {缺失信息}，其他都交给我。", "neutral"),
    ],
    "安抚": [
        ("轻度不满", "抱歉给你添麻烦了，我先帮你把事情处理好。", "apologetic"),
        ("等待过久", "让你等这么久是我们的问题，现在优先处理你这个。", "apologetic"),
        ("质量问题", "收到有问题的商品确实让人失望，我们负责到底。", "apologetic"),
        ("物流延误", "包裹卡在路上确实急人，我马上帮你催并给你明确时间。", "apologetic"),
        ("强烈不满", "我理解你现在很生气，这件事我处理不了，也不该由我敷衍你，现在就转人工主管。", "apologetic"),
    ],
    "拒绝": [
        ("超期退货", "这笔订单已签收 {签收天数} 天，超出 {无理由期限} 天无理由期限，我没法直接操作，但可以帮你转人工审核。", "neutral"),
        ("已使用商品", "商品已使用的情况下不支持无理由退货；如果是质量问题，发我照片，我走质保通道。", "neutral"),
        ("不参与价保", "这次降价是通过{优惠形式}形成的，不在价保范围内，这一点确实没办法按差价退。", "neutral"),
        ("无库存换货", "你要的尺码目前没有库存了，可以等补货，或者直接退货退款，两种都行。", "neutral"),
        ("超出授权", "这部分补偿超过我的权限，我不能直接答应你，但会如实上报，让主管给你答复。", "neutral"),
    ],
    "催办": [
        ("催物流", "已经帮你向{快递公司}发起催派，4 小时内给你答复。", "reassuring"),
        ("催发货", "帮你把发货标成优先，今天之内出库，发出后马上同步单号。", "reassuring"),
        ("催退款", "退款流程已帮你催，{退款时效}内到账，我盯着进度，有变化马上告诉你。", "reassuring"),
        ("催审核", "申请已经提交到审核，{审核时限}内出结果，我会第一时间同步。", "reassuring"),
        ("催工单", "工单 {工单号} 已标为加急，人工会在 {等待时长} 内接入。", "reassuring"),
    ],
    "收尾": [
        ("确认动作", "以上都确认没问题的话，我就提交了，可以吗？", "neutral"),
        ("已办结", "已经帮你处理完了，退款 {金额} 元会在 {退款时效} 内到账，还有其他需要吗？", "neutral"),
        ("转人工收尾", "人工已接入，工单 {工单号}，你的订单和沟通记录都带过去了，不用重复说明。", "reassuring"),
        ("追问收尾", "如果还有别的问题，把订单号发我就行，我一直在线。", "neutral"),
        ("满意度收尾", "这次的处理还满意吗？不满意的话告诉我，我继续跟。", "neutral"),
    ],
}


def main() -> int:
    r = rng()
    intents = ["order_query", "logistics_abnormal", "refund_return", "exchange",
               "price_protection", "invoice", "complaint", "pre_sale", "human_service", "other"]
    forbid = {
        "首响": "不要在首响里就下结论或承诺结果",
        "安抚": "不要连续道歉超过一句，也不要说\"这是规定\"",
        "拒绝": "不要使用\"不能\"\"不行\"单独成句，必须带替代方案",
        "催办": "不要只说\"帮你催一下\"而不给时限",
        "收尾": "不要在客户未确认时执行退款等不可逆操作",
    }
    rows = []
    for kind, items in TEMPLATES.items():
        for i in range(60):
            scene, text, tone = items[i % len(items)]
            intent = intents[i % len(intents)]
            rows.append({
                "话术编号": f"SC-{kind}-{i + 1:03d}",
                "大类": kind,
                "场景": scene,
                "适用意图": intent,
                "话术内容": text,
                "语气": tone,
                "禁用场景": forbid[kind],
                "可变变量": "、".join([v for v in ["品牌", "订单号", "诉求", "缺失信息", "签收天数",
                                              "无理由期限", "优惠形式", "快递公司", "退款时效",
                                              "审核时限", "工单号", "等待时长", "金额"] if "{" + v + "}" in text]) or "无",
                "长度": len(text),
            })
    r.shuffle(rows)
    for i, row in enumerate(rows, 1):
        row["序号"] = i

    wb = Workbook()
    ws = wb.active
    ws.title = "话术库"
    cols = ["序号", "话术编号", "大类", "场景", "适用意图", "话术内容", "语气",
            "禁用场景", "可变变量", "长度"]
    ws.append(cols)
    for row in rows:
        ws.append([row[c] for c in cols])
    for i, w in enumerate([6, 14, 8, 12, 18, 70, 12, 40, 26, 6], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for cell in ws[1]:
        cell.fill = HEAD_FILL
        cell.font = HEAD_FONT
        cell.alignment = Alignment(vertical="center", horizontal="center")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)

    ws2 = wb.create_sheet("使用说明")
    for row in [
        ["项目", "说明"],
        ["用途", "标准话术模板，补全 QA 对后入知识库，供回复节点参考语气与结构"],
        ["变量占位", "话术里的 {订单号}、{金额} 等由系统数据填充，模型不得自行编造"],
        ["语气字段", "neutral 平实 / apologetic 需共情 / reassuring 需给明确时限"],
        ["禁用场景", "这一列是护栏，写进提示词里比单靠模型自觉可靠"],
        ["数据性质", "合成数据，仅用于内部检索与引用链路验证"],
    ]:
        ws2.append(row)
    for i, w in enumerate([14, 80], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "客服话术库_300条.xlsx"
    wb.save(path)
    print(f"完成：{path}")
    print(f"  话术 {len(rows)} 条，分布 {{k: 60 for k in TEMPLATES}}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
