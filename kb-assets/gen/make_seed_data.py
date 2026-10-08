"""生成知识库素材的三张主表。

    cd kb-assets
    python gen/make_seed_data.py

产出：
    data/sku_master.csv       300 个 SKU（12 品类 × 25）
    data/scenario_master.csv  200 个 SOP 场景（4 大类 × 50）
    data/qa_seed.csv          500 条 FAQ 种子（政策 200 / 售前 120 / 物流 120 / 投诉 60）

后续所有文档生成器只读这三张表，保证跨文档口径一致。
全部为合成数据，固定随机种子，重复运行结果一致。
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from answers import ANSWER_RULES, COMPLAINT_ANSWERS, LOGISTICS_ANSWERS, SALES_ANSWERS  # noqa: E402
from templates import (  # noqa: E402
    BRAND, CATEGORIES, QA_SEED_QUESTIONS, SOP_ESCALATION, SOP_SCENARIOS,
    SOP_STEP_TEMPLATES, rng,
)

OUT = HERE.parent / "data"
OUT.mkdir(parents=True, exist_ok=True)

SOP_DEADLINE = {
    "售前": ["首次响应 ≤ 30 秒；涉及库存核查 ≤ 2 分钟",
             "首次响应 ≤ 30 秒；复杂咨询 ≤ 5 分钟给出结论",
             "即时答复；需人工核实的 2 小时内回复"],
    "售后": ["首次响应 ≤ 30 秒；退款申请 2 小时内审核",
             "退货审核 ≤ 4 小时；退款到账 1-7 个工作日",
             "换货申请 24 小时内确认库存并回复"],
    "投诉": ["首次响应 ≤ 60 秒；15 分钟内转人工主管",
             "投诉工单 30 分钟内响应，24 小时内给出方案",
             "高危投诉（曝光/监管）10 分钟内升级"],
    "物流": ["物流查询即时返回；异常件 24 小时内反馈结果",
             "催件后 4 小时内同步快递方答复",
             "丢件核实 48 小时内给出赔付方案"],
}

SOP_TALK_POINTS = {
    "售前": ["先给结论再解释原因，不要让客户自己判断",
             "用具体数字回答（尺码、时效、价格），不用“大概”“应该”",
             "主动提供对比信息，帮助客户做决定",
             "不承诺库存以外的内容"],
    "售后": ["先共情一句，再给方案，共情不超过一句",
             "金额、天数、时效必须逐字引用系统数据",
             "需要客户确认的动作必须以问句结尾",
             "不承诺政策之外的补偿"],
    "投诉": ["先道歉再解释，顺序不能反",
             "不复述客户的过激用语",
             "给具体工单号与等待时长",
             "不承诺赔偿金额，只说会如实上报"],
    "物流": ["主动给出最新轨迹节点与时间，不用“应该快了”",
             "异常件先给处理时限，再给处理动作",
             "丢件先安抚再谈赔付，不推诿给快递公司"],
}

SOP_MISTAKES = {
    "售前": ["用“大概”“应该”回答尺码与时效", "过度承诺库存或到货时间",
             "忽略客户真实使用场景", "直接报最低价却不说明适用条件"],
    "售后": ["第一条消息就要求客户提供一堆信息", "把规则直接甩给客户说“这是规定”",
             "未经客户确认就提交退款", "口头承诺系统里没有的时效"],
    "投诉": ["连续道歉三行反而显得在拖延", "与客户争辩责任归属",
             "承诺自己无权决定的赔偿", "把责任推给快递或平台"],
    "物流": ["只说“帮你催一下”却不给时限", "把丢件责任直接推给快递公司",
             "在未核实前承认是商家责任", "用“已经发出去了”结束对话"],
}


def find_answer(question: str, rules, cat: dict) -> str:
    """按关键词匹配答案模板，匹配不到返回空串由调用方兜底。"""
    ctx = dict(
        free_days=7,
        warranty=cat.get("warranty", "按国家三包规定执行"),
        category_risk=(cat.get("risks") or [""])[0],
        express_default="顺丰速运",
        sizes=(cat.get("sizes") or ["均码"])[0],
        fit="标准版型",
        material=(cat.get("materials") or ["见包装标注"])[0],
        care=(cat.get("care") or ["按洗涤标签说明"])[0],
        origin="浙江杭州",
        period="24 个月",
        standard="执行标准以包装标注为准",
        audience="普通成年人",
        assembly="需要简单组装" if cat.get("code") in ("JJ", "CQ") else "无需组装",
        extra_parts="配件包",
    )
    for keys, tpl in rules:
        if isinstance(keys, str):
            keys = (keys,)
        if any(k in question for k in keys):
            return tpl.format(**ctx)
    return ""


def variants(q: str, n: int = 5) -> list[str]:
    """给一个问题补常见问法，入库后能命中更多口语表达。"""
    out = [q, f"请问{q}", f"{q}？", f"我想问下{q}", f"麻烦问一下，{q}"]
    seen, uniq = set(), []
    for v in out:
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq[:n]


def gen_skus() -> None:
    r = rng()
    cols = ["sku_id", "型号", "品类", "子类", "商品名称", "尺码规格", "颜色", "材质",
            "售价", "保修政策", "保养方式", "核心卖点", "风险提示", "产地", "上市日期"]
    rows = []
    for cat in CATEGORIES:
        lo, hi = cat["price"]
        for i, sub in enumerate(cat["subs"]):
            price = round((lo + (hi - lo) * (i + 1) / len(cat["subs"])) / 10) * 10 - 1
            color = r.choice(cat["colors"])
            rows.append({
                "sku_id": f"{cat['code']}-{i + 1:03d}",
                "型号": f"{cat['code']}-{i + 1:03d}-{color}",
                "品类": cat["name"],
                "子类": sub,
                "商品名称": f"{BRAND} {sub}",
                "尺码规格": r.choice(cat["sizes"]),
                "颜色": color,
                "材质": r.choice(cat["materials"]),
                "售价": price,
                "保修政策": cat["warranty"],
                "保养方式": r.choice(cat["care"]),
                "核心卖点": "；".join(r.sample(cat["features"], 3)),
                "风险提示": r.choice(cat["risks"]),
                "产地": r.choice(["浙江杭州", "广东深圳", "江苏南通", "福建泉州", "山东青岛"]),
                "上市日期": (date(2025, 1, 1) + timedelta(days=r.randint(0, 660))).isoformat(),
            })
    with (OUT / "sku_master.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"  sku_master.csv        {len(rows)} 行")


def gen_scenarios() -> None:
    r = rng()
    cols = ["sop_id", "大类", "场景名称", "触发条件", "处理步骤", "时限要求",
            "升级条件", "话术要点", "常见错误"]
    extra_step = {
        "售前": "确认库存与发货时效后，给出明确答复",
        "售后": "核对售后资格：签收时间、商品状态、是否已使用",
        "投诉": "判断投诉等级：普通 / 高危，高危立即升级",
        "物流": "调取物流最新轨迹节点与更新时间",
    }
    rows = []
    for kind, names in SOP_SCENARIOS.items():
        for i, name in enumerate(names):
            steps = SOP_STEP_TEMPLATES[:]
            steps.insert(3, extra_step[kind])
            rows.append({
                "sop_id": f"SOP-{kind}-{i + 1:03d}",
                "大类": kind,
                "场景名称": name,
                "触发条件": f"客户消息涉及「{name}」相关内容",
                "处理步骤": " → ".join(f"{k + 1}.{s}" for k, s in enumerate(steps)),
                "时限要求": r.choice(SOP_DEADLINE[kind]),
                "升级条件": r.choice(SOP_ESCALATION),
                "话术要点": "；".join(r.sample(SOP_TALK_POINTS[kind], 2)),
                "常见错误": "；".join(r.sample(SOP_MISTAKES[kind], 2)),
            })
    with (OUT / "scenario_master.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"  scenario_master.csv   {len(rows)} 行")


def gen_qa() -> None:
    r = rng()
    cols = ["qa_id", "分类", "问题", "常见问法", "标准答案", "适用品类", "关联政策码"]
    rows = []

    def add(kind, q, ans, catname, code):
        rows.append({
            "qa_id": f"QA-{len(rows) + 1:04d}",
            "分类": kind, "问题": q,
            "常见问法": " | ".join(variants(q)),
            "标准答案": ans, "适用品类": catname, "关联政策码": code,
        })

    # 政策 200 条：50 个基础问题 × 12 个品类，用步长取样保证品类覆盖均匀
    pool = []
    for cat in CATEGORIES:
        for q in QA_SEED_QUESTIONS["政策"]:
            ans = find_answer(q, ANSWER_RULES, cat) or (
                f"关于「{q}」：{cat['name']}类商品的售后政策为 {cat['warranty']}。"
                f"具体到你的订单，把订单号发我可以按实际签收时间判断。")
            pool.append((q, ans, cat["name"]))
    for i in range(200):
        q, ans, catname = pool[(i * 7) % len(pool)]
        add("政策", q, ans, catname, "03-退换货政策")

    # 售前 120 条
    pool = []
    for cat in CATEGORIES:
        for q in QA_SEED_QUESTIONS["售前"]:
            ans = find_answer(q, SALES_ANSWERS, cat) or (
                f"关于「{q}」：{cat['name']}类商品的规格与卖点可查看商品详情页，"
                f"或把具体商品名发我，我帮你确认。")
            pool.append((q, ans, cat["name"]))
    for i in range(120):
        q, ans, catname = pool[(i * 5) % len(pool)]
        add("售前", q, ans, catname, "")

    # 物流 120 条
    pool = []
    for q in QA_SEED_QUESTIONS["物流"]:
        ans = find_answer(q, LOGISTICS_ANSWERS, CATEGORIES[0]) or (
            "物流问题请提供订单号，我帮你查最新轨迹并给出明确处理时限。"
            "轨迹连续 48 小时未更新按异常件处理，24 小时内反馈结果。")
        pool.append((q, ans))
    for i in range(120):
        q, ans = pool[i % len(pool)]
        add("物流", q, ans, "全品类", "RULE-LOGISTICS")

    # 投诉 60 条
    pool = []
    for q in QA_SEED_QUESTIONS["投诉"]:
        ans = find_answer(q, COMPLAINT_ANSWERS, CATEGORIES[0]) or (
            "抱歉给你带来不好的体验。你反馈的问题我已记录，"
            "需要人工核实处理，现在帮你转人工主管，工单会标注你的诉求。")
        pool.append((q, ans))
    for i in range(60):
        q, ans = pool[i % len(pool)]
        add("投诉", q, ans, "全品类", "见 prompts/handoff.md")

    with (OUT / "qa_seed.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    dist = Counter(x["分类"] for x in rows)
    print(f"  qa_seed.csv           {len(rows)} 行   分布 {dict(dist)}")


if __name__ == "__main__":
    print("开始生成种子数据 ...")
    gen_skus()
    gen_scenarios()
    gen_qa()
    print("完成，文件在 kb-assets/data/ 下。")
