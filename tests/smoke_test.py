"""Mock 后端的冒烟测试：把五个业务场景 + 鉴权 + 幂等全跑一遍。

用法：
    python tests/smoke_test.py

依赖：fastapi 自带的 TestClient（需要 httpx）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mock-api"))

# 测试用独立的数据库文件，绝不和正在运行的后端共用。
# 共用会导致会话轮次跨测试累加，结果不可复现。
TEST_DB = Path(tempfile.gettempdir()) / "mock-api-smoke-test.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["MOCK_DB_PATH"] = str(TEST_DB)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)
KEY = {"X-API-Key": main.API_KEY}
passed = failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  √ {name}")
    else:
        failed += 1
        print(f"  × {name} {extra}")


def main_test() -> int:
    print("[1] 健康检查与鉴权")
    r = client.get("/health")
    check("健康检查返回 200", r.status_code == 200, r.text)
    r = client.get("/api/orders/2024091288765", headers={"X-API-Key": "wrong"})
    check("错误 key 返回 401", r.status_code == 401, r.text)
    r = client.get("/api/orders/9999999", headers=KEY)
    check("不存在的订单返回 404", r.status_code == 404, r.text)
    r = client.get("/api/orders/2024091288765", headers={"Authorization": f"Bearer {main.API_KEY}"})
    check("Bearer 鉴权也能通过", r.status_code == 200, r.text)
    r = client.get("/api/orders/2024091288765", headers={"Authorization": "Bearer wrong"})
    check("错误的 Bearer 返回 401", r.status_code == 401, r.text)

    print("[2] 场景一：签收 3 天 → 可无理由退货")
    d = client.get("/api/orders/2024091288765", headers=KEY).json()
    check("订单状态为已签收", d["status"] == "已签收", str(d))
    check("带出签收天数", d["days_since_signed"] is not None)
    e = client.post(
        "/api/after-sales/eligibility",
        headers=KEY,
        json={"order_no": "2024091288765", "request_type": "refund", "reason": "不合适"},
    ).json()
    check("判定为可退", e["eligible"] is True, str(e))
    check("规则码正确", e["rule_code"] == "RULE-7D-OK", str(e))
    check("退款金额来自服务端", e["refund_amount"] == 269.00, str(e))

    print("[3] 场景二：签收 40 天 → 超期需人工")
    e = client.post(
        "/api/after-sales/eligibility",
        headers=KEY,
        json={"order_no": "2024090511233", "request_type": "refund", "reason": "想退"},
    ).json()
    check("判定为不可自助", e["eligible"] is False, str(e))
    check("要求转人工", e["requires_human"] is True, str(e))
    check("规则码为超期", e["rule_code"] == "RULE-EXPIRED", str(e))

    print("[4] 场景三：在途且物流停滞 52 小时 → 异常件")
    d = client.get("/api/orders/2024092004455", headers=KEY).json()
    check("识别为物流异常", d["logistics_abnormal"] is True, str(d))
    check("停滞时长 > 48h", d["logistics_stale_hours"] > 48, str(d))
    e = client.post(
        "/api/after-sales/eligibility",
        headers=KEY,
        json={"order_no": "2024092004455", "request_type": "refund", "reason": "还没到"},
    ).json()
    check("未签收不发退款", e["rule_code"] == "RULE-NOT-SIGNED", str(e))

    print("[5] 场景四：已使用商品 → 不支持无理由")
    e = client.post(
        "/api/after-sales/eligibility",
        headers=KEY,
        json={"order_no": "2024092107788", "request_type": "refund", "reason": "不想要了"},
    ).json()
    check("命中已使用规则", e["rule_code"] == "RULE-USED", str(e))
    check("给出质保替代方案", "照片" in e["next_step"], str(e))

    print("[5.5] 换货：走换货规则，话术不能串成退货")
    e = client.post(
        "/api/after-sales/eligibility",
        headers=KEY,
        json={"order_no": "2024091288765", "request_type": "exchange", "reason": "想换大一码"},
    ).json()
    check("换货判定通过", e["eligible"] is True, str(e))
    check("规则码是换货不是退货", e["rule_code"] == "RULE-EXCHANGE-OK", str(e))
    check("下一步讲换货不讲退款", "换货地址" in e["next_step"] and "退款" not in e["next_step"], str(e))
    e = client.post(
        "/api/after-sales/eligibility",
        headers=KEY,
        json={"order_no": "2024090511233", "request_type": "exchange", "reason": "想换颜色"},
    ).json()
    check("超期换货转人工", e["rule_code"] == "RULE-EXCHANGE-EXPIRED" and e["requires_human"], str(e))

    print("[6] 场景五：降价 50 元 → 价保退差价")
    e = client.post(
        "/api/after-sales/eligibility",
        headers=KEY,
        json={"order_no": "2024091922331", "request_type": "price_protection", "reason": "降价了"},
    ).json()
    check("价保通过", e["eligible"] is True, str(e))
    check("差价为 50", e["refund_amount"] == 50.00, str(e))

    print("[6.5] 新增场景：品类限制 / 时间边界 / 质保 / 换货限次")
    cases = [
        ("2024093012004", "refund", "RULE-7D-OK", True, "签收 7 天整，边界内应可退"),
        ("2024093012005", "refund", "RULE-OVER-7D", False, "签收 8 天，刚过界应转人工"),
        ("2024093012006", "refund", "RULE-EXCLUDED", False, "贴身用品不支持无理由"),
        ("2024093012007", "refund", "RULE-EXCLUDED", False, "定制商品不支持无理由"),
        ("2024093012008", "refund", "RULE-FOOD", False, "食品不支持无理由"),
        ("2024093012009", "refund", "RULE-ACTIVATED", False, "数码已激活不支持无理由"),
        ("2024093012010", "refund", "RULE-QUALITY-OK", True, "质量问题签收 15 天走质保"),
        ("2024093012014", "exchange", "RULE-EXCHANGE-LIMIT", False, "换货已用过一次"),
        ("2024093012013", "exchange", "RULE-EXCHANGE-OK", True, "换货在期内可换"),
    ]
    for order_no, rtype, expect_code, expect_ok, desc in cases:
        e = client.post("/api/after-sales/eligibility", headers=KEY,
                        json={"order_no": order_no, "request_type": rtype,
                              "reason": "自动化测试"}).json()
        check(f"{desc}（{expect_code}）",
              e.get("rule_code") == expect_code and e.get("eligible") is expect_ok, str(e))

    print("[6.4] 给 Dify 用的查询接口：没订单号也不能报错")
    r = client.get("/api/order-query", params={"order_no": ""}, headers=KEY)
    check("空订单号返回 200（不是 404）", r.status_code == 200, r.text)
    check("提示需要订单号", r.json().get("need_order_no") is True, r.text)
    r = client.get("/api/order-query", params={"order_no": "9999999"}, headers=KEY)
    check("查不到的订单也返回 200", r.status_code == 200, r.text)
    check("提示核对订单号", r.json().get("found") is False, r.text)
    r = client.get("/api/order-query", params={"order_no": "2024091288765"}, headers=KEY)
    d = r.json()
    check("正常订单能查到", d.get("found") is True and d.get("status") == "已签收", r.text)
    check("返回场景说明", bool(d.get("scenario")), r.text)
    e = client.post("/api/after-sales/eligibility", headers=KEY,
                    json={"order_no": "", "request_type": "refund", "reason": "我要退货"}).json()
    check("没订单号时给友好提示而不是 404",
          e.get("rule_code") == "RULE-NEED-ORDER-NO", str(e))

    print("[6.6] 未发货 / 丢件 / 冒名签收")
    d = client.get("/api/orders/2024093012002", headers=KEY).json()
    check("待发货订单状态正确", d["status"] == "待发货", str(d))
    check("待发货无签收时间", d["signed_at"] is None, str(d))
    e = client.post("/api/after-sales/eligibility", headers=KEY,
                    json={"order_no": "2024093012002", "request_type": "refund",
                          "reason": "催发货"}).json()
    check("未签收不发退款", e["rule_code"] == "RULE-NOT-SIGNED", str(e))
    d = client.get("/api/orders/2024093012003", headers=KEY).json()
    check("丢件订单识别为物流异常", d["logistics_abnormal"] is True, str(d))
    d = client.get("/api/orders/2024093012017", headers=KEY).json()
    check("冒名签收订单可查到签收记录", d["signed_at"] is not None, str(d))

    print("[7] 退款提交：幂等性")
    body = {"order_no": "2024091288765", "reason": "不合适", "request_id": "sess-001-2024091288765"}
    r1 = client.post("/api/after-sales/refunds", headers=KEY, json=body).json()
    r2 = client.post("/api/after-sales/refunds", headers=KEY, json=body).json()
    check("首次提交不是重放", r1["idempotent_replay"] is False, str(r1))
    check("第二次同 request_id 判定为重放", r2["idempotent_replay"] is True, str(r2))
    check("两次返回同一张退款单", r1["refund_id"] == r2["refund_id"], f"{r1} {r2}")
    check("退款单号形如 RF…", r1["refund_id"].startswith("RF"), str(r1))

    print("[8] 转人工工单")
    t = client.post(
        "/api/tickets",
        headers=KEY,
        json={
            "order_no": "2024090511233",
            "intent": "complaint",
            "summary": "客户对超期不给退很不满",
            "sentiment": "negative",
        },
    ).json()
    check("工单创建成功", str(t["ticket_id"]).startswith("TK"), str(t))
    check("负面情绪升为高优先级", t["priority"] == "高", str(t))
    check("给出预计等待时间", t["eta_minutes"] == 2, str(t))

    print("[9] 政策查询兜底")
    p = client.get("/api/after-sales/policy", params={"q": "运费"}, headers=KEY).json()
    check("能查到政策条目", len(p["hits"]) >= 1, str(p))

    print("[10] 多轮对话：会话记住订单号，第二句不用再报")
    S = "test-session-001"
    e = client.post("/api/after-sales/eligibility", headers=KEY,
                    json={"order_no": "2024091288765", "request_type": "refund",
                          "reason": "不想要了", "session_id": S}).json()
    check("第一轮判定为可退", e["eligible"] is True, str(e))
    r = client.post("/api/after-sales/refunds", headers=KEY,
                    json={"session_id": S, "request_type": "refund"}).json()
    check("第二轮不带订单号也能提交", r.get("submitted") is True, str(r))
    check("返回退款单号", str(r.get("refund_id", "")).startswith("RF"), str(r))
    check("返回到账时效", "工作日" in str(r.get("eta", "")), str(r))
    check("金额来自服务端重算", r.get("amount") == 269.0, str(r))
    r2 = client.post("/api/after-sales/refunds", headers=KEY,
                     json={"session_id": S, "request_type": "refund"}).json()
    check("同会话重复提交幂等", r2.get("idempotent_replay") is True, str(r2))
    check("幂等返回同一张单", r2.get("refund_id") == r.get("refund_id"), str(r2))
    d = client.get(f"/api/after-sales/refunds/{r['refund_id']}", headers=KEY).json()
    check("退款进度可查", d.get("found") is not False, str(d))

    # 会话里的订单号必须落库：后端重启后客户说"好"才找得到是哪一单
    import sqlite3
    conn = sqlite3.connect(os.environ["MOCK_DB_PATH"])
    row = conn.execute("SELECT last_order_no FROM sessions WHERE session_id = ?",
                       (S,)).fetchone()
    conn.close()
    check("会话里的订单号已落库（重启不丢）",
          bool(row) and row[0] == "2024091288765", str(row))

    print("[11] 部分退款：多商品订单只退其中一件")
    e = client.post("/api/after-sales/eligibility", headers=KEY,
                    json={"order_no": "2024093012018", "request_type": "refund",
                          "sku": "FS-007"}).json()
    check("指定 sku 只算那一件", e.get("refund_amount") == 299.0, str(e))
    e = client.post("/api/after-sales/eligibility", headers=KEY,
                    json={"order_no": "2024093012018", "request_type": "refund"}).json()
    check("不指定 sku 算全单", e.get("refund_amount") == 408.0, str(e))
    e = client.post("/api/after-sales/eligibility", headers=KEY,
                    json={"order_no": "2024093012018", "request_type": "refund",
                          "sku": "NOT-EXIST"}).json()
    check("sku 不存在时给出可选项", e.get("rule_code") == "RULE-SKU-NOT-FOUND", str(e))
    r = client.post("/api/after-sales/refunds", headers=KEY,
                    json={"order_no": "2024093012018", "request_type": "refund",
                          "sku": "FS-007", "request_id": "partial-001"}).json()
    check("部分退款提交成功", r.get("submitted") is True and r.get("amount") == 299.0, str(r))
    check("退款单标明退的是哪件", "A字" in str(r.get("item", "")), str(r))

    print("[12] 身份核验：大额退款要手机号后四位")
    e = client.post("/api/after-sales/eligibility", headers=KEY,
                    json={"order_no": "2024093012010", "request_type": "refund"}).json()
    check("质量问题可退 639 元", e.get("refund_amount") == 639.0, str(e))
    r = client.post("/api/after-sales/refunds", headers=KEY,
                    json={"order_no": "2024093012010", "request_type": "refund",
                          "request_id": "verify-001"}).json()
    check("未提供手机号时要求核验", r.get("rule_code") == "RULE-NEED-VERIFY", str(r))
    check("核验未通过不提交", r.get("submitted") is False, str(r))
    r = client.post("/api/after-sales/refunds", headers=KEY,
                    json={"order_no": "2024093012010", "request_type": "refund",
                          "request_id": "verify-001", "phone_tail": "9999"}).json()
    check("手机号不符被拒绝", r.get("rule_code") == "RULE-VERIFY-FAILED", str(r))
    r = client.post("/api/after-sales/refunds", headers=KEY,
                    json={"order_no": "2024093012010", "request_type": "refund",
                          "request_id": "verify-001", "phone_tail": "2010"}).json()
    check("手机号正确才提交", r.get("submitted") is True, str(r))
    r = client.post("/api/after-sales/refunds", headers=KEY,
                    json={"order_no": "2024090511233", "request_type": "refund",
                          "request_id": "reject-001"}).json()
    check("不符合资格直接提交被拒绝", r.get("submitted") is False, str(r))

    print("[13] 运行指标")
    m = client.get("/metrics", headers=KEY).json()
    check("指标接口可用", m.get("total_requests", 0) > 0, str(m)[:200])
    check("统计了订单数", m.get("orders_loaded") == 23, str(m)[:200])
    check("统计了活跃会话", m.get("active_sessions", 0) >= 1, str(m)[:200])
    check("统计了退款单数", m.get("refunds_created", 0) >= 3, str(m)[:200])

    print("[14] 会话上报与情绪升级")
    S2 = "test-escalate-001"
    r = client.post("/api/conversation/turn", headers=KEY,
                    json={"session_id": S2, "message": "我的订单到哪了"}).json()
    check("首轮不上报负面", r.get("sentiment") == "neutral", str(r))
    check("首轮不升级", r.get("should_escalate") is False, str(r))
    r = client.post("/api/conversation/turn", headers=KEY,
                    json={"session_id": S2, "message": "太差了，我要投诉"}).json()
    check("识别负面情绪", r.get("sentiment") == "negative", str(r))
    r = client.post("/api/conversation/turn", headers=KEY,
                    json={"session_id": S2, "message": "你们这服务真是垃圾"}).json()
    check("连续负面触发升级", r.get("should_escalate") is True, str(r))
    check("给出升级原因", bool(r.get("escalate_reason")), str(r))
    check("统计了轮次", r.get("turn_count", 0) >= 3, str(r))
    c = client.get(f"/api/conversation/{S2}", headers=KEY).json()
    check("会话历史可查", len(c.get("messages", [])) >= 3, str(c)[:150])
    check("历史保留了客户原话",
          any("垃圾" in m.get("content", "") for m in c.get("messages", [])), str(c)[:150])

    print("[15] 转人工：坐席交接摘要")
    t = client.post("/api/tickets", headers=KEY,
                    json={"session_id": S2, "order_no": "2024091288765",
                          "intent": "complaint", "summary": "客户对服务不满",
                          "sentiment": "negative"}).json()
    check("工单创建成功", str(t.get("ticket_id", "")).startswith("TK"), str(t))
    check("工单带交接摘要", bool(t.get("handoff")), str(t))
    check("摘要含订单号", "2024091288765" in str(t.get("handoff", "")), str(t))
    check("摘要含会话轮次", "轮次" in str(t.get("handoff", "")), str(t))
    d = client.get(f"/api/tickets/{t['ticket_id']}", headers=KEY).json()
    check("工单可回查", d.get("found") is True, str(d))
    check("回查也带摘要", bool(d.get("handoff")), str(d)[:200])

    print("[16] 部分退款：用商品名模糊匹配")
    r = client.post("/api/after-sales/refunds", headers=KEY,
                    json={"order_no": "2024093012018", "request_type": "refund",
                          "item_hint": "我只想退那件半身裙",
                          "request_id": "hint-001"}).json()
    check("按商品名匹配到半身裙", r.get("amount") == 299.0, str(r))
    check("退款单标明商品名", "半身裙" in str(r.get("item", "")), str(r))
    r = client.post("/api/after-sales/refunds", headers=KEY,
                    json={"order_no": "2024093012018", "request_type": "refund",
                          "item_hint": "退那个汤锅", "request_id": "hint-002"}).json()
    check("另一个商品也能匹配", r.get("amount") == 109.0, str(r))
    r = client.post("/api/after-sales/refunds", headers=KEY,
                    json={"order_no": "2024093012018", "request_type": "refund",
                          "item_hint": "随便退点啥", "request_id": "hint-003",
                          "phone_tail": "2018"}).json()
    check("匹配不到时退回整单", r.get("amount") == 408.0, str(r))
    check("退回整单时不标商品名", not r.get("item"), str(r))

    print("[17] 持久化")
    m = client.get("/metrics", headers=KEY).json()
    p = m.get("persisted", {})
    check("消息已落库", p.get("messages", 0) >= 3, str(p))
    check("工单已落库", p.get("tickets", 0) >= 1, str(p))
    check("退款单已落库", p.get("refunds", 0) >= 3, str(p))

    print("[18] 弱负面表达：第一句不升级，连续两句才升级")
    S3 = "test-mild-001"
    r = client.post("/api/conversation/turn", headers=KEY,
                    json={"session_id": S3, "message": "你们这服务不太行"}).json()
    check("弱负面也被记录为负面", r.get("sentiment") == "negative", str(r))
    check("第一句不升级（关键是别把人直接推给人工）",
          r.get("should_escalate") is False, str(r))
    r = client.post("/api/conversation/turn", headers=KEY,
                    json={"session_id": S3, "message": "真的有点失望"}).json()
    check("连续两句才升级", r.get("should_escalate") is True, str(r))
    check("升级时给出原因", bool(r.get("escalate_reason")), str(r))

    print("[19] 坐席工作台：接入 / 抢单冲突 / 回复 / 关闭 / 转主管")
    S4 = "test-agent-001"
    t = client.post("/api/tickets", headers=KEY,
                    json={"order_no": "2024091288765", "intent": "complaint",
                          "sentiment": "negative", "summary": "坐席流程测试",
                          "session_id": S4}).json()
    tid = t["ticket_id"]
    check("工单初始为排队中", t.get("status") == "排队中", str(t))
    check("工单带交接摘要", bool(t.get("handoff")), str(t))

    r = client.post(f"/api/tickets/{tid}/claim", headers=KEY,
                    json={"agent": "小陈 8801"}).json()
    check("接入后状态变处理中", r["ticket"]["status"] == "处理中", str(r))
    check("记录了坐席", r["ticket"]["agent"] == "小陈 8801", str(r))

    r2 = client.post(f"/api/tickets/{tid}/claim", headers=KEY,
                     json={"agent": "小李 8802"}).json()
    check("第二个坐席抢单被拦", r2.get("conflict") is True, str(r2))

    r = client.post(f"/api/tickets/{tid}/reply", headers=KEY,
                    json={"agent": "小陈 8801",
                          "text": "抱歉让你有这样的感受，我帮你把退款提交了"}).json()
    check("坐席回复已记录", len(r["ticket"]["replies"]) == 1, str(r))
    check("回复带坐席名与时间",
          r["ticket"]["replies"][0].get("agent") == "小陈 8801"
          and bool(r["ticket"]["replies"][0].get("at")), str(r))

    r = client.post(f"/api/tickets/{tid}/close", headers=KEY,
                    json={"agent": "小陈 8801"}).json()
    check("关闭前必须选处理结果", r.get("ok") is False, str(r))

    r = client.post(f"/api/tickets/{tid}/close", headers=KEY,
                    json={"agent": "小陈 8801", "resolution": "已退款",
                          "unresolved_reason": "客户对时效不满", "csat": 4}).json()
    check("关闭成功", r["ticket"]["status"] == "已解决", str(r))
    check("记录了处理结果", r["ticket"]["resolution"] == "已退款", str(r))
    check("记录了满意度", r["ticket"]["csat"] == 4, str(r))

    s = client.get("/api/console/summary", headers=KEY).json()
    check("看板可查", s["tickets"]["总数"] >= 1, str(s["tickets"]))
    check("未解决原因进排行（周度迭代的输入）",
          any(x["reason"] == "客户对时效不满" for x in s["unresolved_ranking"]),
          str(s["unresolved_ranking"]))
    check("已解决工单可从列表查到",
          any(x["ticket_id"] == tid for x in
              client.get("/api/tickets", headers=KEY).json()["items"]), "")

    t2 = client.post("/api/tickets", headers=KEY,
                     json={"intent": "complaint", "summary": "转主管测试"}).json()
    r = client.post(f"/api/tickets/{t2['ticket_id']}/escalate", headers=KEY,
                    json={"agent": "小陈 8801"}).json()
    check("转主管后优先级拉高", r["ticket"]["priority"] == "高", str(r))
    check("转主管后回到排队", r["ticket"]["status"] == "排队中", str(r))

    print("[20] 前端页面与数据看板")
    for path in ("/", "/client", "/console", "/dashboard"):
        r = client.get(path)
        check(f"{path} 页面可访问", r.status_code == 200 and len(r.text) > 2000,
              f"HTTP {r.status_code}")

    check("客户端会恢复历史对话", "loadHistory" in client.get("/client").text, "")
    check("坐席端会显示完整对话记录", "完整对话记录" in client.get("/console").text, "")
    check("客户端会轮询新消息", "pollNew" in client.get("/client").text, "")

    print("[21] 坐席回复要能同步到客户端会话")
    S5 = "test-agent-sync-001"
    t5 = client.post("/api/tickets", headers=KEY,
                     json={"order_no": "2024091288765", "intent": "complaint",
                           "sentiment": "negative", "summary": "同步测试",
                           "session_id": S5}).json()
    client.post(f"/api/tickets/{t5['ticket_id']}/claim", headers=KEY,
                json={"agent": "小陈 8801"})
    client.post(f"/api/tickets/{t5['ticket_id']}/reply", headers=KEY,
                json={"agent": "小陈 8801", "text": "我是人工客服小陈，帮你处理"})
    d5 = client.get(f"/api/conversation/{S5}", headers=KEY).json()
    roles = [m["role"] for m in d5.get("messages", [])]
    check("坐席回复写进了会话记录（客户端才拉得到）", "agent" in roles, str(roles))
    check("坐席接入的动作也留痕", "system" in roles, str(roles))
    check("消息带会话 ID", d5.get("session_id") == S5, str(d5.get("session_id")))

    print("[22] Dify 会话 ID 与客户端会话 ID 必须映射到同一个会话")
    # 这是"坐席回复客户看不到"的根因：同一个对话有两个名字，
    # 客户端的消息存在 web-xxx 下，Dify 传给工单的是它自己的 conversation_id。
    S6 = "web-alias-test"
    DID = "6933680a-3dee-44bf-a458-14f147911395"
    main.SESSION_ALIAS[DID] = S6          # 模拟 /api/chat 成功时建立的映射

    t6 = client.post("/api/tickets", headers=KEY,
                     json={"order_no": "2024091288765", "intent": "complaint",
                           "sentiment": "negative", "summary": "映射测试",
                           "session_id": DID}).json()
    check("工单存的是客户端会话 ID，不是 Dify 的",
          t6.get("session_id") == S6, str(t6.get("session_id")))

    client.post(f"/api/tickets/{t6['ticket_id']}/reply", headers=KEY,
                json={"agent": "小陈 8801", "text": "映射测试回复"})
    d6 = client.get(f"/api/conversation/{S6}", headers=KEY).json()
    check("客户端能看到坐席回复",
          any(m["role"] == "agent" and "映射测试" in m["content"]
              for m in d6.get("messages", [])), str([m["role"] for m in d6.get("messages", [])]))
    d7 = client.get(f"/api/conversation/{DID}", headers=KEY).json()
    check("用 Dify 的 ID 也能查到同一个会话",
          len(d7.get("messages", [])) == len(d6.get("messages", [])), "")

    print("[23] 人工接管后 AI 必须让位")
    S7 = "web-yield-test"
    t7 = client.post("/api/tickets", headers=KEY,
                     json={"order_no": "2024091288765", "intent": "complaint",
                           "sentiment": "negative", "summary": "让位测试",
                           "session_id": S7}).json()
    r = client.post("/api/chat", headers=KEY,
                    json={"session_id": S7, "message": "我说了我要投诉"}).json()
    check("人工接管后不再走 AI 流程", r.get("handed_off") is True, str(r)[:160])
    check("不返回 AI 回复（等人工来答）", not r.get("reply"), str(r)[:160])
    check("不会新建第二张工单", r.get("ticket_id") == t7["ticket_id"], str(r)[:160])
    check("给客户明确提示", bool(r.get("notice")), str(r)[:160])

    d8 = client.get(f"/api/conversation/{S7}", headers=KEY).json()
    check("客户的新消息进了会话（人工能看到）",
          any(m["role"] == "customer" and "我说了我要投诉" in m["content"]
              for m in d8.get("messages", [])), "")

    client.post(f"/api/tickets/{t7['ticket_id']}/close", headers=KEY,
                json={"agent": "小陈 8801", "resolution": "已解释"})
    r2 = client.post("/api/chat", headers=KEY,
                     json={"session_id": S7, "message": "你好"}).json()
    check("工单关闭后 AI 重新接管", r2.get("handed_off") is not True, str(r2)[:160])

    d = client.get("/api/dashboard", headers=KEY).json()
    check("看板接口可用", "business" in d, str(d)[:120])
    check("看板写了指标口径", bool(d.get("targets")), "")
    check("看板含未解决原因排行", "unresolved_ranking" in d, "")
    check("看板含接口调用统计", d["api"]["total_requests"] > 0, str(d["api"])[:120])
    b = d["business"]
    check("业务指标字段齐全",
          all(k in b for k in ("sessions", "handoff_rate", "self_service_rate",
                               "avg_turns", "refunds")), str(b))

    # 聊天代理：配了 app key 就真调 Dify，没配就给明确提示，两种都算正常。
    # 第三种情况是 Dify 能连上但上游模型报错（例如 503）——链路是通的，问题在模型侧，
    # 不算本后端的缺陷，但要显式提示，否则你会以为整条链路挂了。
    r = client.post("/api/chat", headers=KEY,
                    json={"session_id": "test-chat-001", "message": "你好"}).json()
    upstream_error = r.get("ok") is False and "Run failed" in str(r.get("error", ""))
    check("聊天接口有明确响应（真回复 / 提示配 key / 上游模型异常）",
          r.get("ok") is True or r.get("need_key") is True or upstream_error,
          str(r)[:200])
    if upstream_error:
        print("    ! Dify 连得上，但上游模型返回错误，去 Dify 里测一下模型可用性")
    r = client.post("/api/chat", headers=KEY,
                    json={"session_id": "", "message": ""}).json()
    check("缺参数时被拒绝", r.get("ok") is False, str(r))

    print(f"\n结果：通过 {passed} 项，失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_test())
