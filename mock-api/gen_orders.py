"""生成覆盖全部分支的订单数据。

    cd mock-api
    python gen_orders.py

产出：
    data/orders.json                后端启动时读取的订单数据
    ../docs/04-订单测试数据.md        给人看的对照表

三个关键设计：

1. **时间存相对偏移，不存绝对日期。**
   存 "签收于 2026-10-01" 的话，过两周这条订单就自动变成"签收 17 天"，
   「7 天内可退」的演示场景就废了。存 signed_days_ago: 3，每次启动现算，
   演示永远新鲜。

2. **商品必须引用 sku_master.csv 里真实存在的 SKU。**
   订单里的商品名、价格、保修政策要和 300 份产品手册完全一致，
   否则客户问"我这单的外套保修多久"，知识库手册和订单接口会给出两个答案。

3. **手机号后四位 = 订单号后四位。**
   真实系统里它是独立字段，这里为了演示方便直接复用订单号后四位，
   客户核验时输入订单号最后四位即可。所有订单都是这个规则。
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
SKU_CSV = HERE.parent / "kb-assets" / "data" / "sku_master.csv"


def load_skus() -> dict[str, dict]:
    if not SKU_CSV.exists():
        print(f"× 找不到 {SKU_CSV}，先跑 kb-assets/gen/make_seed_data.py")
        sys.exit(1)
    with SKU_CSV.open(encoding="utf-8-sig") as f:
        return {r["sku_id"]: r for r in csv.DictReader(f)}


# 每个订单 = 一个要验证的业务场景
#   items  商品列表 [(SKU, 数量)]，多商品订单用于验证「部分退款」
#   flags  直接驱动后端的资格判定规则
SCENARIOS = [
    # ---------------------------- 物流查询分支 ----------------------------
    dict(order_no="2024091288765", scenario="签收 3 天，7 天无理由期内（物流查询 + 可退双场景）",
         status="已签收", items=[("FS-006", 1)], paid_days_ago=10, shipped_days_ago=8,
         signed_days_ago=3, flags={}, company="顺丰速运", tracking="SF4433221188",
         logistics_state="已签收", last_update_hours_ago=72,
         traces=[(72, "快件已签收，签收人：本人"), (120, "快件已到达杭州西湖区网点，正在派送"),
                 (192, "快件已发出，下一站杭州转运中心")]),
    dict(order_no="2024093012001", scenario="已发货在途，物流正常更新（对照组）",
         status="运输中", items=[("XX-010", 1)], paid_days_ago=2, shipped_days_ago=1,
         signed_days_ago=None, flags={}, company="中通快递", tracking="ZT2200113344",
         logistics_state="运输中", last_update_hours_ago=5,
         traces=[(5, "快件已到达上海转运中心"), (28, "快件已发出，下一站上海转运中心")]),
    dict(order_no="2024092004455", scenario="在途但物流停滞 52 小时（异常件）",
         status="运输中", items=[("XX-010", 1)], paid_days_ago=4, shipped_days_ago=3,
         signed_days_ago=None, flags={}, company="圆通速递", tracking="YT5566778899",
         logistics_state="运输中", last_update_hours_ago=52,
         traces=[(52, "快件已到达郑州转运中心"), (72, "快件已发出，下一站郑州转运中心")]),
    dict(order_no="2024093012002", scenario="刚下单未发货（催发货场景）",
         status="待发货", items=[("JJ-001", 2)], paid_days_ago=0, shipped_days_ago=None,
         signed_days_ago=None, flags={}, company="", tracking="",
         logistics_state="待发货", last_update_hours_ago=2, traces=[]),
    dict(order_no="2024093012003", scenario="物流丢件（快递公司已确认）",
         status="运输中", items=[("SM-002", 1)], paid_days_ago=12, shipped_days_ago=10,
         signed_days_ago=None, flags={}, company="韵达快递", tracking="YD9900112233",
         logistics_state="异常-疑似丢件", last_update_hours_ago=168,
         traces=[(168, "快件在郑州转运中心滞留，疑似丢件，已发起查询")]),
    dict(order_no="2024093012016", scenario="三件商品分批发货，只到了两件（多商品 + 部分签收）",
         status="部分签收", items=[("JJ-008", 1), ("JJ-009", 1), ("JJ-010", 1)],
         paid_days_ago=7, shipped_days_ago=5, signed_days_ago=2, flags={},
         company="申通快递", tracking="ST5151515151",
         logistics_state="部分签收", last_update_hours_ago=30,
         traces=[(30, "包裹 1/3 已签收，其余在途"), (54, "包裹已从杭州仓发出，共 3 个包裹")]),
    dict(order_no="2024093012017", scenario="显示已签收但客户未收到（冒名签收）",
         status="已签收", items=[("XX-005", 1)], paid_days_ago=6, shipped_days_ago=4,
         signed_days_ago=1, flags={}, company="韵达快递", tracking="YD6161616161",
         logistics_state="已签收", last_update_hours_ago=20,
         traces=[(20, "快件已签收，签收人：门卫代收"), (28, "快件正在派送")]),

    # ---------------------------- 退款退货分支 ----------------------------
    dict(order_no="2024093012004", scenario="签收 7 天整（无理由期限边界，应可退）",
         status="已签收", items=[("FS-003", 1)], paid_days_ago=12, shipped_days_ago=10,
         signed_days_ago=7, flags={}, company="顺丰速运", tracking="SF1122334455",
         logistics_state="已签收", last_update_hours_ago=168,
         traces=[(168, "快件已签收，签收人：本人")]),
    dict(order_no="2024093012005", scenario="签收 8 天（刚过无理由期限，应转人工）",
         status="已签收", items=[("FS-005", 1)], paid_days_ago=14, shipped_days_ago=11,
         signed_days_ago=8, flags={}, company="中通快递", tracking="ZT3344556677",
         logistics_state="已签收", last_update_hours_ago=192,
         traces=[(192, "快件已签收，签收人：本人")]),
    dict(order_no="2024090511233", scenario="签收 40 天（严重超期，必须转人工）",
         status="已签收", items=[("CJ-002", 1)], paid_days_ago=46, shipped_days_ago=44,
         signed_days_ago=40, flags={}, company="中通快递", tracking="ZT7788990011",
         logistics_state="已签收", last_update_hours_ago=960,
         traces=[(960, "快件已签收，签收人：前台代收")]),
    dict(order_no="2024092107788", scenario="签收 2 天但商品已使用（不支持无理由）",
         status="已签收", items=[("CJ-004", 1)], paid_days_ago=6, shipped_days_ago=5,
         signed_days_ago=2, flags={"used": True}, company="韵达快递", tracking="YD1122334455",
         logistics_state="已签收", last_update_hours_ago=48,
         traces=[(48, "快件已签收，签收人：本人")]),
    dict(order_no="2024093012006", scenario="贴身用品（内衣），签收后不支持无理由",
         status="已签收", items=[("MZ-001", 2)], paid_days_ago=5, shipped_days_ago=4,
         signed_days_ago=1, flags={"intimate": True}, company="圆通速递", tracking="YT6677889900",
         logistics_state="已签收", last_update_hours_ago=24,
         traces=[(24, "快件已签收，签收人：快递柜")]),
    dict(order_no="2024093012007", scenario="定制商品（刻字），不支持无理由",
         status="已签收", items=[("JJ-011", 1)], paid_days_ago=9, shipped_days_ago=7,
         signed_days_ago=4, flags={"custom_made": True}, company="顺丰速运", tracking="SF5566778899",
         logistics_state="已签收", last_update_hours_ago=96,
         traces=[(96, "快件已签收，签收人：本人")]),
    dict(order_no="2024093012008", scenario="食品类，签收 3 天（食品安全不支持无理由）",
         status="已签收", items=[("SP-001", 1)], paid_days_ago=6, shipped_days_ago=5,
         signed_days_ago=3, flags={"food": True}, company="中通快递", tracking="ZT5566778899",
         logistics_state="已签收", last_update_hours_ago=72,
         traces=[(72, "快件已签收，签收人：本人")]),
    dict(order_no="2024093012009", scenario="数码产品已激活，不支持无理由",
         status="已签收", items=[("SM-001", 1)], paid_days_ago=5, shipped_days_ago=4,
         signed_days_ago=2, flags={"activated": True}, company="顺丰速运", tracking="SF7788990011",
         logistics_state="已签收", last_update_hours_ago=48,
         traces=[(48, "快件已签收，签收人：本人")]),
    dict(order_no="2024093012010", scenario="质量问题，签收 15 天（走质保通道，不受 7 天限制）",
         status="已签收", items=[("XD-006", 1)], paid_days_ago=18, shipped_days_ago=16,
         signed_days_ago=15, flags={"quality_issue": True}, company="圆通速递", tracking="YT2233445566",
         logistics_state="已签收", last_update_hours_ago=360,
         traces=[(360, "快件已签收，签收人：本人")]),
    dict(order_no="2024093012011", scenario="大额多商品订单（两件合计超 500，需主管审核）",
         status="已签收", items=[("XD-010", 1), ("SM-001", 1)],
         paid_days_ago=20, shipped_days_ago=18, signed_days_ago=16, flags={},
         company="顺丰速运", tracking="SF9900112233",
         logistics_state="已签收", last_update_hours_ago=384,
         traces=[(384, "快件已签收，签收人：本人")]),
    dict(order_no="2024093012012", scenario="已拒收退回中（等同退货申请）",
         status="退回中", items=[("XB-001", 1)], paid_days_ago=8, shipped_days_ago=6,
         signed_days_ago=None, flags={}, company="中通快递", tracking="ZT1122334455",
         logistics_state="退回中", last_update_hours_ago=20,
         traces=[(20, "快件已按客户要求拒收，正在退回发货地")]),
    dict(order_no="2024093012018", scenario="部分退款场景：两件商品，只想退其中一件",
         status="已签收", items=[("FS-007", 1), ("CJ-003", 1)],
         paid_days_ago=7, shipped_days_ago=5, signed_days_ago=3, flags={},
         company="顺丰速运", tracking="SF4242424242",
         logistics_state="已签收", last_update_hours_ago=72,
         traces=[(72, "快件已签收，签收人：本人")]),

    # ---------------------------- 换货分支 ----------------------------
    dict(order_no="2024093012013", scenario="换货：签收 4 天，同款有库存（可换）",
         status="已签收", items=[("FS-002", 1)], paid_days_ago=8, shipped_days_ago=6,
         signed_days_ago=4, flags={}, company="顺丰速运", tracking="SF1010101010",
         logistics_state="已签收", last_update_hours_ago=96,
         traces=[(96, "快件已签收，签收人：本人")]),
    dict(order_no="2024093012014", scenario="换货：已经换过一次（限一次，应拒绝）",
         status="已签收", items=[("XX-003", 1)], paid_days_ago=15, shipped_days_ago=13,
         signed_days_ago=5, flags={"exchange_count": 1}, company="圆通速递", tracking="YT3131313131",
         logistics_state="已签收", last_update_hours_ago=120,
         traces=[(120, "快件已签收，签收人：本人")]),

    # ---------------------------- 价保 ----------------------------
    dict(order_no="2024091922331", scenario="签收 5 天，同款降价 50 元（价保可退差价）",
         status="已签收", items=[("SM-009", 1)], paid_days_ago=9, shipped_days_ago=7,
         signed_days_ago=5, flags={}, company="顺丰速运", tracking="SF9988776655",
         logistics_state="已签收", last_update_hours_ago=120,
         traces=[(120, "快件已签收，签收人：本人")],
         price_drop=(299.00, 50.00, 1, "official")),
    dict(order_no="2024093012015", scenario="签收 20 天，虽降价但已过 15 天价保期",
         status="已签收", items=[("XD-003", 1)], paid_days_ago=24, shipped_days_ago=22,
         signed_days_ago=20, flags={}, company="中通快递", tracking="ZT1010101010",
         logistics_state="已签收", last_update_hours_ago=480,
         traces=[(480, "快件已签收，签收人：本人")],
         price_drop=(199.00, 60.00, 2, "official")),
    dict(order_no="2024093012019", scenario="签收 3 天，但降价来自限时秒杀（不参与价保）",
         status="已签收", items=[("YD-001", 1)], paid_days_ago=7, shipped_days_ago=5,
         signed_days_ago=3, flags={}, company="顺丰速运", tracking="SF1212121212",
         logistics_state="已签收", last_update_hours_ago=72,
         traces=[(72, "快件已签收，签收人：本人")],
         price_drop=(159.00, 40.00, 1, "flash_sale")),
]


def build(s: dict, skus: dict[str, dict]) -> dict | None:
    items, total = [], 0.0
    for sku_id, qty in s["items"]:
        sku = skus.get(sku_id)
        if not sku:
            print(f"  ! SKU {sku_id} 不存在，跳过订单 {s['order_no']}")
            return None
        unit = float(sku["售价"])
        total += unit * qty
        flags = s.get("flags", {})
        items.append({
            "sku": sku["sku_id"],
            "name": f"{sku['商品名称']}（{sku['颜色']}）",
            "qty": qty,
            "amount": round(unit * qty, 2),
            # 只有单商品订单的 flag 才作用到商品上；多商品订单按商品名区分场景
            "category": sku["品类"],
            "warranty": sku["保修政策"],
            "used": bool(flags.get("used")) if len(s["items"]) == 1 else False,
            "intimate": bool(flags.get("intimate")) if len(s["items"]) == 1 else False,
            "custom_made": bool(flags.get("custom_made")) if len(s["items"]) == 1 else False,
            "activated": bool(flags.get("activated")) if len(s["items"]) == 1 else False,
            "food": bool(flags.get("food")) if len(s["items"]) == 1 else False,
            "quality_issue": bool(flags.get("quality_issue")) if len(s["items"]) == 1 else False,
        })
    flags = s.get("flags", {})
    return {
        "order_no": s["order_no"],
        "scenario": s["scenario"],
        "status": s["status"],
        "buyer_masked": "138****" + s["order_no"][-4:],
        "phone_tail": s["order_no"][-4:],
        "paid_amount": round(total, 2),
        "paid_days_ago": s["paid_days_ago"],
        "shipped_days_ago": s["shipped_days_ago"],
        "signed_days_ago": s["signed_days_ago"],
        "items": items,
        "exchange_count": int(flags.get("exchange_count", 0)),
        "logistics": {
            "company": s["company"],
            "tracking_no": s["tracking"],
            "state": s["logistics_state"],
            "last_update_hours_ago": s["last_update_hours_ago"],
            "traces": [{"hours_ago": h, "text": t} for h, t in s["traces"]],
        },
        "price_drop": None if not s.get("price_drop") else {
            "current_price": s["price_drop"][0],
            "diff": s["price_drop"][1],
            "changed_days_ago": s["price_drop"][2],
            # 降价原因决定了能不能价保：官方调价可以，优惠券/秒杀/直播专享价不参与。
            # 这是价保规则里最容易被忽略、也最容易赔钱的一条——只看"降了多少"会算错。
            "source": s["price_drop"][3] if len(s["price_drop"]) > 3 else "official",
        },
    }


def main() -> int:
    skus = load_skus()
    orders = {}
    for s in SCENARIOS:
        o = build(s, skus)
        if o:
            orders[o["order_no"]] = o

    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "orders.json").write_text(
        json.dumps(orders, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"生成 {len(orders)} 个订单 → data/orders.json")
    print()
    for o in orders.values():
        names = "、".join(i["name"].split("（")[0].replace("轻岚优选 ", "") for i in o["items"])
        print(f"  {o['order_no']}  {o['status']:<6} ￥{o['paid_amount']:<8.0f} "
              f"尾号{o['phone_tail']}  {names}")
    write_doc(orders)
    return 0


def write_doc(orders: dict) -> None:
    lines = [
        "# 订单测试数据对照表",
        "",
        "> 由 `mock-api/gen_orders.py` 自动生成，改数据后重跑脚本即可同步。",
        "> 时间存的是相对偏移（如「签收 3 天」），所以这份数据**永远不过期**。",
        "> **手机号后四位 = 订单号后四位**，核验时输入订单号最后四位即可。",
        "",
        "## 全部订单",
        "",
        "| 订单号 | 状态 | 实付 | 手机尾号 | 商品 | 场景 |",
        "|---|---|---|---|---|---|",
    ]
    for o in orders.values():
        names = "、".join(i["name"].split("（")[0].replace("轻岚优选 ", "") for i in o["items"])
        lines.append(f"| `{o['order_no']}` | {o['status']} | ￥{o['paid_amount']:.0f} "
                     f"| {o['phone_tail']} | {names} | {o['scenario']} |")

    lines += [
        "",
        "## 按分支怎么挑订单",
        "",
        "### 物流查询（GET /api/order-query）",
        "",
        "| 订单号 | 预期 |",
        "|---|---|",
        "| `2024093012001` | 在途正常更新，报出真实轨迹节点 |",
        "| `2024092004455` | 停滞 52 小时，识别为**异常件**，给催件方案 |",
        "| `2024093012002` | 待发货，应回答发货时效而不是物流轨迹 |",
        "| `2024093012003` | 疑似丢件，走赔付流程 |",
        "| `2024093012016` | 三件商品只到了两件（多包裹 + 部分签收） |",
        "| `2024093012017` | 显示签收但客户未收到（冒名签收） |",
        "",
        "### 退款退货（POST /api/after-sales/eligibility）",
        "",
        "| 订单号 | 预期 |",
        "|---|---|",
        "| `2024091288765` | 签收 3 天，**可退 269 元**，问是否提交 |",
        "| `2024093012004` | 签收 7 天整，**边界内应可退** |",
        "| `2024093012005` | 签收 8 天，刚过界，**应转人工** |",
        "| `2024090511233` | 签收 40 天，严重超期，转人工 |",
        "| `2024092107788` | 商品已使用，不支持无理由 |",
        "| `2024093012006` | 贴身用品，不支持无理由 |",
        "| `2024093012007` | 定制商品，不支持无理由 |",
        "| `2024093012008` | 食品类，不支持无理由 |",
        "| `2024093012009` | 数码已激活，不支持无理由 |",
        "| `2024093012010` | 质量问题签收 15 天，**走质保通道**（不受 7 天限制） |",
        "| `2024093012011` | 两件商品合计 1039 元，超期需主管审核 |",
        "| `2024093012012` | 已拒收退回中，等同退货申请 |",
        "| `2024093012018` | **部分退款**：两件商品，只想退其中一件（提交时指定 sku） |",
        "",
        "### 换货（POST /api/after-sales/eligibility，request_type=exchange）",
        "",
        "| 订单号 | 预期 |",
        "|---|---|",
        "| `2024093012013` | 签收 4 天，**可换**，话术讲换货地址不讲退款 |",
        "| `2024093012014` | 已经换过一次，**应拒绝**（换货限一次） |",
        "",
        "### 价保",
        "",
        "| 订单号 | 预期 |",
        "|---|---|",
        "| `2024091922331` | 签收 5 天降价 50 元，**可退差价 50** |",
        "| `2024093012015` | 签收 20 天，超过 15 天价保期，转人工 |",
        "| `2024093012019` | 签收 3 天降价 40 元，但降价来自**限时秒杀**，按规则不参与价保 |",
        "",
        "价保只看**降价原因**，不看降了多少：官方调价可以补差价，"
        "优惠券、红包、限时秒杀、直播专享价、清仓价都不参与。"
        "订单数据里 `price_drop.source` 就是这个字段，"
        "只看金额不看来源会把不该赔的钱赔出去。",
        "",
        "### 提交售后单（POST /api/after-sales/refunds）",
        "",
        "提交接口会**重新校验资格**——不信任调用方传来的金额，防止伪造。",
        "必须传 `request_id`（用会话 ID）做幂等，重复提交只退一次。",
        "",
        "| 场景 | 预期 |",
        "|---|---|",
        "| 先查资格再提交 | 返回退款单号 + 到账时效 |",
        "| 同一个 request_id 提交两次 | 第二次返回「重放」，不重复退款 |",
        "| 不符合资格却直接提交 | 被拒绝，返回拒绝原因 |",
        "| 部分退款（指定 sku） | 只退该商品金额 |",
        "",
        "### 投诉 / 转人工（POST /api/tickets）",
        "",
        "任意订单号都可以。返回真实工单号 + 预计等待时长",
        "（负面情绪标为高优先级等 2 分钟，普通等 8 分钟）。",
        "",
        "## 在 Dify 里怎么用",
        "",
        "```",
        "订单 2024091288765 我要退货       → 可退 269 元，问是否提交",
        "好                                → 提交退款，返回退款单号",
        "订单 2024093012004 我的货到哪了   → 报出物流轨迹",
        "订单 2024093012010 收到是坏的     → 走质保，不受 7 天限制",
        "```",
        "",
        "## 边界值说明",
        "",
        "`2024093012004`（签收 7 天整）和 `2024093012005`（签收 8 天）是专门设计的一对：",
        "用来验证**期限判定按自然日算**，而不是按 24 小时算。",
        "后端用「签收日 + 7 天的 23:59:59」作为截止时刻，所以第 7 天当天来退货仍然可退——",
        "这是真实客服的执行方式，用浮点天数比较会在这里出错。",
    ]
    doc = HERE.parent / "docs" / "04-订单测试数据.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n对照表已输出 → {doc}")


if __name__ == "__main__":
    sys.exit(main())
