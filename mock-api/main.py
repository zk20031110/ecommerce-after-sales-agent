"""售后客服 Mock 后端 —— 供 Dify Chatflow 的 HTTP 节点调用。

三条设计原则（也是简历上可以讲的点）：
1. 所有金额、时效、单号都由服务端给出，模型只负责转述。模型没有机会编造数字。
2. 退款提交接口幂等（request_id），避免用户重复确认导致重复退款。
3. 每个响应都带 source 字段，出问题能立刻定位"这句话是谁说的"。

启动：
    cd mock-api
    python -m pip install -r requirements.txt
    python -m uvicorn main:app --host 0.0.0.0 --port 8000

在 Docker 里跑的 Dify 要访问本机服务时，把地址写成 http://host.docker.internal:8000
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import storage

# 读取项目根目录的 .env（DIFY_APP_KEY 这类配置不放代码里）。
# uvicorn[standard] 自带 python-dotenv，没装也不影响启动。
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # noqa: BLE001
    pass

# ---------------------------------------------------------------- 常量与规则

API_KEY = os.getenv("MOCK_API_KEY", "dev-local-key")

RULE_FREE_RETURN_DAYS = 7          # 七天无理由
RULE_PRICE_PROTECTION_DAYS = 15    # 价保窗口
RULE_STALE_LOGISTICS_HOURS = 48    # 物流超过多久没更新算异常
RULE_REFUND_ETA = "1-7 个工作日"
RULE_VERIFY_THRESHOLD = 300.0   # 退款金额达到这个数才要求核验手机号后四位


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def days_since(dt_iso: str) -> float:
    dt = datetime.fromisoformat(dt_iso)
    return (now_utc() - dt).total_seconds() / 86400


def hours_since(dt_iso: str) -> float:
    dt = datetime.fromisoformat(dt_iso)
    return (now_utc() - dt).total_seconds() / 3600


def within(signed_at: str, days: int) -> bool:
    """签收后是否还在 N 天窗口内。

    按【自然日】算，不按 24 小时算——政策写的是"自签收当日 0 点起算 N 天内"。
    也就是签收日 + N 天的 23:59:59 之前都算在期限内。

    两个坑都在这里：
    1. 用 days_since() 的浮点天数去比 `<= 7`，会算出 7.0000001 而误判超期；
    2. 用 24 小时精确比较，客户在"签收满 7 天"的当天来退货会被系统拒绝——
       真实客服不会这么干，第 7 天就是第 7 天。
    """
    signed_date = datetime.fromisoformat(signed_at).date()
    deadline = datetime.combine(signed_date + timedelta(days=days),
                                time(23, 59, 59), tzinfo=timezone.utc)
    return now_utc() <= deadline


# ---------------------------------------------------------------- 模拟数据

def build_fallback_orders() -> Dict[str, Dict[str, Any]]:
    """内置的 5 个订单，仅在 data/orders.json 不存在时兜底。"""
    t = now_utc()
    return {
        # 场景一：签收 3 天，正常可退
        "2024091288765": {
            "order_no": "2024091288765",
            "status": "已签收",
            "buyer_masked": "138****6621",
            "paid_amount": 268.00,
            "paid_at": iso(t - timedelta(days=10)),
            "shipped_at": iso(t - timedelta(days=8)),
            "signed_at": iso(t - timedelta(days=3)),
            "items": [{
                "sku": "DRESS-BLK-M",
                "name": "法式方领连衣裙 黑色 M码",
                "qty": 1,
                "amount": 268.00,
                "category": "服饰",
                "used": False,
                "custom_made": False,
                "intimate": False,
            }],
            "logistics": {
                "company": "顺丰速运",
                "tracking_no": "SF4433221188",
                "state": "已签收",
                "last_update": iso(t - timedelta(days=3)),
                "traces": [
                    {"time": iso(t - timedelta(days=3)), "text": "快件已签收，签收人：本人"},
                    {"time": iso(t - timedelta(days=5)), "text": "快件已到达杭州西湖区网点，正在派送"},
                    {"time": iso(t - timedelta(days=8)), "text": "快件已发出，下一站杭州转运中心"},
                ],
            },
        },
        # 场景二：签收 40 天，超期，需人工审核
        "2024090511233": {
            "order_no": "2024090511233",
            "status": "已签收",
            "buyer_masked": "139****3308",
            "paid_amount": 89.00,
            "paid_at": iso(t - timedelta(days=46)),
            "shipped_at": iso(t - timedelta(days=44)),
            "signed_at": iso(t - timedelta(days=40)),
            "items": [{
                "sku": "MUG-WHT-350",
                "name": "陶瓷马克杯 白色 350ml",
                "qty": 1,
                "amount": 89.00,
                "category": "家居",
                "used": False,
                "custom_made": False,
                "intimate": False,
            }],
            "logistics": {
                "company": "中通快递",
                "tracking_no": "ZT7788990011",
                "state": "已签收",
                "last_update": iso(t - timedelta(days=40)),
                "traces": [
                    {"time": iso(t - timedelta(days=40)), "text": "快件已签收，签收人：前台代收"},
                ],
            },
        },
        # 场景三：在途，且物流 52 小时没更新 —— 异常件
        "2024092004455": {
            "order_no": "2024092004455",
            "status": "运输中",
            "buyer_masked": "137****9912",
            "paid_amount": 599.00,
            "paid_at": iso(t - timedelta(days=4)),
            "shipped_at": iso(t - timedelta(days=3)),
            "signed_at": None,
            "items": [{
                "sku": "SHOE-RUN-42",
                "name": "轻量跑鞋 42码",
                "qty": 1,
                "amount": 599.00,
                "category": "鞋靴",
                "used": False,
                "custom_made": False,
                "intimate": False,
            }],
            "logistics": {
                "company": "圆通速递",
                "tracking_no": "YT5566778899",
                "state": "运输中",
                "last_update": iso(t - timedelta(hours=52)),
                "traces": [
                    {"time": iso(t - timedelta(hours=52)), "text": "快件已到达郑州转运中心"},
                    {"time": iso(t - timedelta(days=2)), "text": "快件已发出，下一站郑州转运中心"},
                ],
            },
        },
        # 场景四：签收 2 天，但商品已使用 —— 不支持无理由
        "2024092107788": {
            "order_no": "2024092107788",
            "status": "已签收",
            "buyer_masked": "186****4402",
            "paid_amount": 159.00,
            "paid_at": iso(t - timedelta(days=6)),
            "shipped_at": iso(t - timedelta(days=5)),
            "signed_at": iso(t - timedelta(days=2)),
            "items": [{
                "sku": "KNIFE-SET-3",
                "name": "陶瓷刀具三件套",
                "qty": 1,
                "amount": 159.00,
                "category": "厨具",
                "used": True,
                "custom_made": False,
                "intimate": False,
            }],
            "logistics": {
                "company": "韵达快递",
                "tracking_no": "YD1122334455",
                "state": "已签收",
                "last_update": iso(t - timedelta(days=2)),
                "traces": [
                    {"time": iso(t - timedelta(days=2)), "text": "快件已签收，签收人：本人"},
                ],
            },
        },
        # 场景五：签收 5 天，同款降价 50 元 —— 价保场景
        "2024091922331": {
            "order_no": "2024091922331",
            "status": "已签收",
            "buyer_masked": "150****7788",
            "paid_amount": 349.00,
            "paid_at": iso(t - timedelta(days=9)),
            "shipped_at": iso(t - timedelta(days=7)),
            "signed_at": iso(t - timedelta(days=5)),
            "price_drop": {"current_price": 299.00, "diff": 50.00, "changed_at": iso(t - timedelta(days=1))},
            "items": [{
                "sku": "AIRPODS-CASE-A",
                "name": "耳机保护套 透明款",
                "qty": 1,
                "amount": 349.00,
                "category": "数码配件",
                "used": False,
                "custom_made": False,
                "intimate": False,
            }],
            "logistics": {
                "company": "顺丰速运",
                "tracking_no": "SF9988776655",
                "state": "已签收",
                "last_update": iso(t - timedelta(days=5)),
                "traces": [
                    {"time": iso(t - timedelta(days=5)), "text": "快件已签收，签收人：本人"},
                ],
            },
        },
    }


# ---------------------------------------------------------------- 订单数据加载

ORDERS_FILE = Path(__file__).resolve().parent / "data" / "orders.json"


def load_orders() -> Dict[str, Dict[str, Any]]:
    """优先读 data/orders.json（由 gen_orders.py 生成的 22 个场景订单）。

    文件里存的是相对时间偏移（signed_days_ago: 3），启动时换算成绝对时间——
    演示数据永远不会过期，不需要隔几天手动改日期。
    文件不存在时退回内置的 5 个订单，保证后端任何时候都能起来。
    """
    if not ORDERS_FILE.exists():
        print(f"[mock-api] 未找到 {ORDERS_FILE.name}，使用内置的 5 个订单")
        return build_fallback_orders()

    raw = json.loads(ORDERS_FILE.read_text(encoding="utf-8"))
    now = now_utc()

    def ts(days: Optional[int] = None, hours: Optional[int] = None) -> Optional[str]:
        if days is not None:
            return iso(now - timedelta(days=days))
        if hours is not None:
            return iso(now - timedelta(hours=hours))
        return None

    out: Dict[str, Dict[str, Any]] = {}
    for no, o in raw.items():
        lg = o.get("logistics") or {}
        pd = o.get("price_drop")
        out[no] = {
            "order_no": no,
            "scenario": o.get("scenario", ""),
            "status": o["status"],
            "buyer_masked": o["buyer_masked"],
            "phone_tail": o.get("phone_tail", ""),
            "paid_amount": o["paid_amount"],
            "paid_at": ts(days=o.get("paid_days_ago")),
            "shipped_at": ts(days=o.get("shipped_days_ago")),
            "signed_at": ts(days=o.get("signed_days_ago")),
            "items": o["items"],
            "exchange_count": o.get("exchange_count", 0),
            "logistics": {
                "company": lg.get("company", ""),
                "tracking_no": lg.get("tracking_no", ""),
                "state": lg.get("state", ""),
                "last_update": ts(hours=lg.get("last_update_hours_ago", 0)),
                "traces": [{"time": ts(hours=tr["hours_ago"]), "text": tr["text"]}
                           for tr in lg.get("traces", [])],
            },
            "price_drop": None if not pd else {
                "current_price": pd["current_price"],
                "diff": pd["diff"],
                "changed_at": ts(days=pd.get("changed_days_ago")),
            },
        }
    print(f"[mock-api] 已加载 {len(out)} 个场景订单")
    return out


ORDERS = load_orders()
REFUNDS: Dict[str, Dict[str, Any]] = {}   # request_id -> 退款单
TICKETS: Dict[str, Dict[str, Any]] = {}   # ticket_id  -> 工单
SESSIONS: Dict[str, Dict[str, Any]] = {}  # 会话状态：记住最近处理的订单号等上下文

# 持久化：工单与退款单落 SQLite，重启不丢
DB = storage.init()


# ---------------------------------------------------------------- 入参模型

class EligibilityIn(BaseModel):
    order_no: str = Field("", description="订单号；也可以放在 URL 查询参数里传")
    request_type: str = Field("refund", description="refund / exchange / price_protection")
    reason: str = Field("", description="客户自述的原因")
    sku: str = Field("", description="部分退款时指定商品；留空表示全单")
    session_id: str = Field("", description="会话 ID，用于记住订单号供后续轮次使用")


class RefundIn(BaseModel):
    order_no: str = ""
    reason: str = ""
    request_type: str = Field("refund", description="refund / exchange / price_protection")
    sku: str = Field("", description="部分退款时指定商品")
    item_hint: str = Field("", description="客户对商品的描述，如「那件连衣裙」，后端做模糊匹配")
    request_id: str = Field("", description="幂等键，建议用会话 ID")
    session_id: str = Field("", description="会话 ID，用于回退到会话里记住的订单号")
    phone_tail: str = Field("", description="下单手机号后四位，大额退款需核验")


class TicketIn(BaseModel):
    order_no: Optional[str] = None
    intent: str = "other"
    summary: str = ""
    sentiment: str = "neutral"
    contact: Optional[str] = None
    session_id: str = Field("", description="会话 ID，用于自动生成坐席交接摘要")


class ChatIn(BaseModel):
    """客户端聊天入参。"""
    session_id: str = Field(..., description="会话 ID，前端生成并持久化")
    message: str = Field(..., description="客户消息")


class AgentActionIn(BaseModel):
    """坐席在工作台上的操作入参。"""
    agent: str = Field("人工坐席", description="坐席名称/工号")
    text: str = Field("", description="坐席回复内容")
    resolution: str = Field("", description="处理结果：已退款 / 已换货 / 已解释 / 无解转主管")
    unresolved_reason: str = Field("", description="未解决原因，用于周度迭代统计")
    csat: Optional[int] = Field(None, description="满意度 1-5")


# ---------------------------------------------------------------- 应用

app = FastAPI(title="售后客服 Mock 后端", version="1.0.0")


def auth(x_api_key: Optional[str], authorization: Optional[str] = None):
    """支持两种鉴权写法，Dify 里配哪种都不会翻车：
    - 自定义头：X-API-Key: dev-local-key
    - Bearer：Authorization: Bearer dev-local-key
    """
    token = (x_api_key or "").strip()
    if not token and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            token = value.strip()
    if token != API_KEY:
        raise HTTPException(status_code=401, detail="鉴权失败：X-API-Key 或 Bearer token 不正确")


def get_order(order_no: str) -> Dict[str, Any]:
    order = ORDERS.get(order_no)
    if not order:
        raise HTTPException(status_code=404, detail=f"没有找到订单 {order_no}")
    return order


def order_payload(o: Dict[str, Any]) -> Dict[str, Any]:
    """订单详情的统一响应体，供两个查询入口复用。"""
    item = o["items"][0]
    stale_hours = hours_since(o["logistics"]["last_update"])
    return {
        "found": True,
        "order_no": o["order_no"],
        "status": o["status"],
        "scenario": o.get("scenario", ""),
        "buyer_masked": o["buyer_masked"],
        "paid_amount": o["paid_amount"],
        "paid_at": o["paid_at"],
        "signed_at": o["signed_at"],
        "days_since_signed": round(days_since(o["signed_at"]), 1) if o["signed_at"] else None,
        "item": item,
        "logistics": o["logistics"],
        "logistics_stale_hours": round(stale_hours, 1),
        "logistics_abnormal": bool(stale_hours >= RULE_STALE_LOGISTICS_HOURS),
        "price_drop": o.get("price_drop"),
        "source": "order-service",
    }


@app.get("/health")
def health():
    return {"ok": True, "orders": len(ORDERS), "server_time": iso(now_utc())}


@app.get("/api/orders/{order_no}")
def order_detail(order_no: str, x_api_key: Optional[str] = Header(None),
                 authorization: Optional[str] = Header(None)):
    """订单详情 + 物流。Dify 的"查订单"分支调这个。"""
    auth(x_api_key, authorization)
    o = get_order(order_no)
    return order_payload(o)


@app.get("/api/order-query")
def order_query(order_no: str = Query("", description="订单号"),
                session_id: str = Query("", description="会话 ID"),
                x_api_key: Optional[str] = Header(None),
                authorization: Optional[str] = Header(None)):
    """按订单号查订单 —— 给 Dify 用的版本。

    和 /api/orders/{no} 的区别：订单号为空或查不到时返回 200 + 友好提示，
    而不是 404。因为 Dify 的 HTTP 节点遇到 4xx 会直接报错、整条流程断掉，
    而"客户没给订单号"恰恰是最常见的开场情况。
    """
    auth(x_api_key, authorization)
    # 客户没给订单号时，回退到本会话上一次查过的订单——这就是"多轮对话不必重复报单号"
    no = (order_no or "").strip() or SESSIONS.get(session_id, {}).get("last_order_no", "")
    if not no:
        return {"found": False, "need_order_no": True,
                "message": "还没拿到订单号。请礼貌地请客户提供订单号，不要编造订单状态。",
                "source": "order-service"}
    o = ORDERS.get(no)
    if not o:
        return {"found": False, "need_order_no": False,
                "message": f"没有查询到订单 {no}，请客户核对订单号是否输入正确。",
                "source": "order-service"}
    if session_id:
        SESSIONS.setdefault(session_id, {})["last_order_no"] = no
        storage.touch_session(DB, session_id, order_no=no)
    return order_payload(o)


@app.get("/api/orders/{order_no}/logistics")
def logistics(order_no: str, x_api_key: Optional[str] = Header(None),
              authorization: Optional[str] = Header(None)):
    auth(x_api_key, authorization)
    o = get_order(order_no)
    stale = hours_since(o["logistics"]["last_update"])
    return {
        **o["logistics"],
        "order_no": order_no,
        "stale_hours": round(stale, 1),
        "abnormal": bool(stale >= RULE_STALE_LOGISTICS_HOURS),
        "source": "logistics-service",
    }


@app.post("/api/after-sales/eligibility")
def eligibility(body: EligibilityIn,
                order_no: str = Query("", description="订单号（也可放 body，两种都收）"),
                session_id: str = Query("", description="会话 ID（也可放 body）"),
                request_type: str = Query("", description="refund / exchange / price_protection"),
                x_api_key: Optional[str] = Header(None),
                authorization: Optional[str] = Header(None)):
    """售后资格判定。规则全部在服务端，模型只负责把结论翻译成人话。

    订单号和会话 ID 既可以从 body 传，也可以从 URL 查询参数传——
    因为 Dify 的 JSON body 里带变量时会被拆成多段，导致校验失败，
    所以工作流里改成把变量放在 URL 上，body 只放常量。
    """
    if order_no and not body.order_no:
        body.order_no = order_no
    if session_id and not body.session_id:
        body.session_id = session_id
    if request_type:
        body.request_type = request_type
    auth(x_api_key, authorization)
    if not (body.order_no or "").strip():
        return {
            "eligible": False, "decision": "need_order_no",
            "rule_code": "RULE-NEED-ORDER-NO",
            "rule_text": "还没有订单号，无法判断售后资格。",
            "next_step": "请客户提供订单号，拿到后我立刻帮你查签收时间和可退金额。",
            "requires_human": False, "source": "after-sales-service",
        }
    o = get_order(body.order_no)
    # 记住这个会话正在处理哪一单：客户下一句说"好，退吧"时就不用再报订单号了。
    # 内存和数据库都写：内存给同进程的后续请求用，数据库保证后端重启后还能找回。
    if body.session_id:
        SESSIONS.setdefault(body.session_id, {})["last_order_no"] = o["order_no"]
        storage.touch_session(DB, body.session_id, order_no=o["order_no"])
    # 支持部分退款：指定 sku 时只针对那一件商品判定，金额也只算那一件
    picked = None
    if body.sku:
        picked = next((i for i in o["items"] if i["sku"] == body.sku), None)
        if picked is None:
            skus = "、".join(i["sku"] for i in o["items"])
            return {
                "eligible": False, "decision": "sku_not_found",
                "rule_code": "RULE-SKU-NOT-FOUND",
                "rule_text": f"订单 {o['order_no']} 里没有商品 {body.sku}。该订单包含：{skus}。",
                "next_step": "请客户确认要退的是哪一件商品。",
                "requires_human": False, "source": "after-sales-service",
            }
    item = picked or o["items"][0]
    t = body.request_type or "refund"
    # 指定 sku = 部分退款，只退那一件；不指定 = 全单退款
    refund_amount = item["amount"] if picked else o["paid_amount"]

    if not o["signed_at"]:
        return {
            "eligible": False,
            "decision": "not_signed",
            "rule_code": "RULE-NOT-SIGNED",
            "rule_text": "订单还在运输中，签收后才能申请售后。",
            "next_step": "我可以帮你催一下物流，或者你先等货到再发起。",
            "requires_human": False,
            "source": "after-sales-service",
        }

    d = days_since(o["signed_at"])

    # 价保单独判定
    if t == "price_protection":
        pd = o.get("price_drop")
        if not pd:
            return {
                "eligible": False, "decision": "no_price_drop",
                "rule_code": "RULE-PP-NO-DROP",
                "rule_text": f"未查询到该商品在签收后 {RULE_PRICE_PROTECTION_DAYS} 天内有降价记录。",
                "next_step": "如果你看到了更低价格，把截图发我，我帮你转人工核实。",
                "requires_human": False, "source": "after-sales-service",
            }
        if within(o["signed_at"], RULE_PRICE_PROTECTION_DAYS):
            return {
                "eligible": True, "decision": "price_protection_ok",
                "rule_code": "RULE-PP-OK",
                "rule_text": f"签收后 {RULE_PRICE_PROTECTION_DAYS} 天内同款商品降价，可退差价。",
                "refund_amount": pd["diff"],
                "next_step": "确认后我帮你提交退差价，1-7 个工作日原路退回。",
                "requires_human": False, "source": "after-sales-service",
            }
        return {
            "eligible": False, "decision": "price_protection_expired",
            "rule_code": "RULE-PP-EXPIRED",
            "rule_text": f"已签收 {round(d, 1)} 天，超过 {RULE_PRICE_PROTECTION_DAYS} 天价保期。",
            "next_step": "这种情况需要人工审核，我可以帮你转接。",
            "requires_human": True, "source": "after-sales-service",
        }

    # 质量问题：不受 7 天限制，30 天内可退换修。
    # 必须放在"已使用"判断之前——用过的商品有质量问题照样该退。
    if item.get("quality_issue"):
        return {
            "eligible": True, "decision": "quality_ok",
            "rule_code": "RULE-QUALITY-OK",
            "rule_text": f"质量问题签收后 30 天内可退换修，当前已签收 {round(d, 1)} 天，运费由我方承担。",
            "refund_amount": refund_amount,
            "need_return_goods": True,
            "shipping_fee_bearer": "质量问题往返运费由商家承担",
            "next_step": "请提供问题部位的照片或视频，核实后安排退货、换货或维修。",
            "requires_human": False, "source": "after-sales-service",
        }

    # 无理由退货
    if item["used"]:
        return {
            "eligible": False, "decision": "used_item",
            "rule_code": "RULE-USED",
            "rule_text": "商品已使用，不支持七天无理由退货。",
            "next_step": "如果是质量问题，我可以帮你申请质保通道，需要你提供照片。",
            "requires_human": False, "source": "after-sales-service",
        }
    if item["intimate"] or item["custom_made"]:
        return {
            "eligible": False, "decision": "excluded_category",
            "rule_code": "RULE-EXCLUDED",
            "rule_text": "该品类（贴身用品/定制商品）不支持七天无理由退货。",
            "next_step": "质量问题可以走质保，需要你提供照片。",
            "requires_human": False, "source": "after-sales-service",
        }
    if item.get("activated"):
        return {
            "eligible": False, "decision": "activated",
            "rule_code": "RULE-ACTIVATED",
            "rule_text": "数码产品已激活，不支持七天无理由退货。",
            "next_step": "如果是质量问题，可以走质保通道，需要你提供问题描述和照片。",
            "requires_human": False, "source": "after-sales-service",
        }
    if item.get("food"):
        return {
            "eligible": False, "decision": "food",
            "rule_code": "RULE-FOOD",
            "rule_text": "食品类商品出于食品安全考虑不支持无理由退货。",
            "next_step": "如果出现变质、破损或临期，请在 7 天内提供照片，我们安排退换。",
            "requires_human": False, "source": "after-sales-service",
        }

    # 换货：走自己的规则和话术，不能套退货的下一步，否则话术会自相矛盾
    if t == "exchange":
        if o.get("exchange_count", 0) >= 1:
            return {
                "eligible": False, "decision": "exchange_limit",
                "rule_code": "RULE-EXCHANGE-LIMIT",
                "rule_text": "换货限一次，该订单已经换过一次。",
                "next_step": "如果换来的商品仍不满意，可以申请退货退款；质量问题可以走质保。",
                "requires_human": False, "source": "after-sales-service",
            }
        if within(o["signed_at"], RULE_FREE_RETURN_DAYS):
            return {
                "eligible": True, "decision": "exchange_ok",
                "rule_code": "RULE-EXCHANGE-OK",
                "rule_text": f"签收后 {RULE_FREE_RETURN_DAYS} 天内可申请换货，限一次，当前已签收 {round(d, 1)} 天。",
                "refund_amount": 0,
                "need_return_goods": True,
                "shipping_fee_bearer": "非质量问题的换货往返运费由买家承担；质量问题由我方承担",
                "next_step": "请告诉我需要换的尺码或颜色，我确认库存后给你换货地址，收到并验收当天发出新品。",
                "requires_human": False, "source": "after-sales-service",
            }
        return {
            "eligible": False, "decision": "exchange_expired",
            "rule_code": "RULE-EXCHANGE-EXPIRED",
            "rule_text": f"已签收 {round(d, 1)} 天，超过 {RULE_FREE_RETURN_DAYS} 天换货期。",
            "next_step": "超过换货期限的申请需要人工审核，我可以帮你转人工。",
            "requires_human": True, "source": "after-sales-service",
        }

    if within(o["signed_at"], RULE_FREE_RETURN_DAYS):
        return {
            "eligible": True, "decision": "free_return_ok",
            "rule_code": "RULE-7D-OK",
            "rule_text": f"签收后 {RULE_FREE_RETURN_DAYS} 天内可无理由退货，当前已签收 {round(d, 1)} 天。",
            "refund_amount": refund_amount,
            "need_return_goods": True,
            "shipping_fee_bearer": "非质量问题的退货运费由买家承担；有运费险的按保单赔付",
            "deadline": iso(datetime.fromisoformat(o["signed_at"]) + timedelta(days=RULE_FREE_RETURN_DAYS)),
            "next_step": "确认后我给你退货地址，寄回商品签收后 1-7 个工作日退款。",
            "requires_human": False, "source": "after-sales-service",
        }
    if within(o["signed_at"], RULE_PRICE_PROTECTION_DAYS):
        return {
            "eligible": False, "decision": "over_7d_manual",
            "rule_code": "RULE-OVER-7D",
            "rule_text": f"已签收 {round(d, 1)} 天，超过 {RULE_FREE_RETURN_DAYS} 天无理由期限。",
            "next_step": "超过无理由期限的退货需要人工审核，我帮你转人工。",
            "requires_human": True, "source": "after-sales-service",
        }
    return {
        "eligible": False, "decision": "expired",
        "rule_code": "RULE-EXPIRED",
        "rule_text": f"已签收 {round(d, 1)} 天，超出所有自助售后期限。",
        "next_step": "需要人工审核，我帮你转人工。",
        "requires_human": True, "source": "after-sales-service",
    }


def match_item(items: List[Dict[str, Any]], hint: str):
    """按客户的描述模糊匹配订单里的商品。

    中文没有空格，"我只想退那件半身裙"整串不是任何商品名的子串，
    所以方向要反过来：**从商品名切出 2~6 字的片段，看哪些出现在客户描述里**，
    命中越长说明越具体。只有最高分明显领先才认，
    命中多件并列或没命中都返回 None——宁可退回整单判定，也不能猜错商品退错钱。
    """
    detail = (hint or "").strip()
    if not detail:
        return None
    scored: list[tuple[int, Dict[str, Any]]] = []
    for it in items:
        best = 0
        for tok in re.findall(r"[A-Za-z0-9\-]{2,}|[\u4e00-\u9fa5]{2,}", it["name"]):
            for n in range(min(6, len(tok)), 1, -1):     # 片段最长 6 字
                if any(tok[i:i + n] in detail for i in range(len(tok) - n + 1)):
                    best = max(best, n)
                    break
        if best >= 2:
            scored.append((best, it))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None      # 并列 → 分不清是哪件，不猜
    return scored[0][1]


@app.post("/api/after-sales/refunds")
def create_refund(body: RefundIn,
                  x_api_key: Optional[str] = Header(None),
                  order_no: str = Query("", description="订单号（也可放 body）"),
                  session_id: str = Query("", description="会话 ID（也可放 body）"),
                  phone_tail: str = Query("", description="手机号后四位（也可放 body）"),
                  request_type: str = Query(""),
                  authorization: Optional[str] = Header(None)):
    """提交售后单（退款 / 退差价 / 换货）。

    三个"贴近真实"的设计，也是这个接口和演示版最大的区别：

    1. **重新校验资格**：直接复用资格判定逻辑，不信任调用方传来的订单号和金额——
       真实系统绝不会让前端决定退多少钱、退哪一单。
    2. **幂等**：按 request_id 去重，客户连点两次只退一笔。
    3. **身份核验**：金额达到阈值时要求下单手机号后四位，防止冒领。
    """
    if order_no and not body.order_no:
        body.order_no = order_no
    if session_id and not body.session_id:
        body.session_id = session_id
    if phone_tail and not body.phone_tail:
        body.phone_tail = phone_tail
    if request_type:
        body.request_type = request_type
    auth(x_api_key, authorization)

    # 定位订单：优先用传入的，其次用本会话记住的
    no = ((body.order_no or "").strip()
          or SESSIONS.get(body.session_id, {}).get("last_order_no", "")
          or storage.get_session(DB, body.session_id).get("last_order_no", ""))
    if not no:
        return {"submitted": False, "decision": "need_order_no",
                "rule_code": "RULE-NEED-ORDER-NO",
                "rule_text": "还不知道要处理哪个订单。",
                "next_step": "请客户提供订单号，我查一下再提交。",
                "requires_human": False, "source": "refund-service"}
    o = ORDERS.get(no)
    if not o:
        return {"submitted": False, "decision": "order_not_found",
                "rule_code": "RULE-ORDER-NOT-FOUND",
                "rule_text": f"没有查询到订单 {no}。",
                "next_step": "请客户核对订单号后再试。",
                "requires_human": False, "source": "refund-service"}

    # 部分退款：客户用商品名描述（"那件连衣裙"），这里做模糊匹配
    sku = body.sku
    if not sku and body.item_hint:
        hit = match_item(o["items"], body.item_hint)
        if hit:
            sku = hit["sku"]

    # 第一步：重新校验资格（复用资格判定，不信任调用方）
    verdict = eligibility(
        EligibilityIn(order_no=no, request_type=body.request_type or "refund",
                      reason=body.reason, sku=sku, session_id=body.session_id),
        # 注意：这里是在 Python 里直接调用接口函数，不是走 HTTP。
        # 不显式传这几个查询参数的话，它们会保留默认值——而默认值是 Query 对象
        # 而不是空字符串，会被当成真实数据塞进数据库（踩过这个坑）。
        order_no="", session_id="", request_type="",
        x_api_key=x_api_key, authorization=authorization)
    if not verdict.get("eligible"):
        return {**verdict, "submitted": False,
                "rule_text": f"无法提交：{verdict.get('rule_text', '')}"}

    amount = float(verdict.get("refund_amount") or 0)

    # 第二步：金额达到阈值时核验手机号后四位
    if amount >= RULE_VERIFY_THRESHOLD:
        if not (body.phone_tail or "").strip():
            return {"submitted": False, "decision": "need_verify",
                    "rule_code": "RULE-NEED-VERIFY",
                    "rule_text": f"这笔退款 {amount:.0f} 元，金额较大，需要先核验身份。",
                    "next_step": "请客户提供下单手机号后四位，核对无误我立刻提交。",
                    "requires_human": False, "source": "refund-service"}
        if body.phone_tail.strip() != o.get("phone_tail", ""):
            return {"submitted": False, "decision": "verify_failed",
                    "rule_code": "RULE-VERIFY-FAILED",
                    "rule_text": "手机号后四位与下单时不一致，不能提交。",
                    "next_step": "请客户重新核对；多次不符需要转人工核实身份。",
                    "requires_human": True, "source": "refund-service"}

    # 第三步：幂等提交
    key = body.request_id.strip() or f"{body.session_id}:{no}:{sku or 'ALL'}"
    if key in REFUNDS:
        return {**REFUNDS[key], "submitted": True, "idempotent_replay": True}

    item_name = next((i["name"] for i in o["items"] if i["sku"] == sku), "") if sku else ""
    refund = {
        "submitted": True,
        "refund_id": f"RF{now_utc().strftime('%y%m%d%H%M%S')}{len(REFUNDS) + 1:02d}",
        "order_no": o["order_no"],
        "session_id": body.session_id,
        "amount": amount,
        "item": item_name,
        "status": "已受理",
        "eta": RULE_REFUND_ETA,
        "channel": "原路退回",
        "decision": "submitted",
        "rule_code": verdict.get("rule_code", ""),
        "rule_text": f"已受理退款 {amount:.0f} 元" + (f"（{item_name}）" if item_name else "") + "。",
        "next_step": f"退款将在 {RULE_REFUND_ETA} 内原路退回，可在订单详情查看进度。",
        "reason": body.reason,
        "created_at": iso(now_utc()),
        "idempotent_replay": False,
        "source": "refund-service",
    }
    REFUNDS[key] = refund
    storage.save_refund(DB, refund, key)
    if body.session_id:
        SESSIONS.setdefault(body.session_id, {})["last_order_no"] = no
    return refund


@app.get("/api/after-sales/refunds/{refund_id}")
def get_refund(refund_id: str, x_api_key: Optional[str] = Header(None),
               authorization: Optional[str] = Header(None)):
    """查退款进度。真实场景里客户最常问的第二句话就是"退到哪一步了"。"""
    auth(x_api_key, authorization)
    for r in REFUNDS.values():
        if r["refund_id"] == refund_id:
            return {**r, "source": "refund-service"}
    # 内存里没有就查数据库——重启过的退款单也能查到
    saved = storage.get_refund_by_id(DB, refund_id)
    if saved:
        return {**saved, "from_db": True, "source": "refund-service"}
    return {"found": False, "message": f"没有查到退款单 {refund_id}。",
            "source": "refund-service"}


@app.post("/api/conversation/turn")
async def record_turn(request: Request, x_api_key: Optional[str] = Header(None),
                      authorization: Optional[str] = Header(None)):
    """记录一轮对话，并返回情绪升级判断。

    放这一层的理由：会话状态是业务系统的事。Dify 那边只负责把消息送过来，
    怎么统计、什么时候该升级主管，由后端决定——这样策略调整不用改工作流。

    情绪判定这里用关键词粗筛（投诉/差评/曝光/垃圾/气死…），
    生产环境应该接情感分析模型，或者由 Dify 的分类器把 sentiment 传过来。
    """
    auth(x_api_key, authorization)
    # 同时支持 JSON 和表单：
    # 客户的原始消息里有引号，塞进 Dify 的 JSON body 模板会把 JSON 搞崩，
    # 所以工作流里这个节点用 form-urlencoded 提交，接口这边两种都收。
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        body = await request.json()
    else:
        form = await request.form()
        body = {k: str(v) for k, v in form.items()}
    session_id = str(body.get("session_id") or "").strip()
    message = str(body.get("message") or "")
    # Dify 传的是它自己的 conversation_id，翻译成客户端会话 ID，
    # 否则情绪统计和坐席回复会落到另一个会话上
    session_id = resolve_session(session_id)
    if not session_id:
        return {"ok": False, "message": "缺少 session_id"}

    NEGATIVE_WORDS = (
        # 强烈不满：出现即可判定为负面
        "投诉", "差评", "曝光", "垃圾", "骗子", "气死", "太差", "恶心",
        "不给解决", "什么破", "废物", "态度差", "退钱",
        # 轻微不满：单次不升级，累计两次才升级
        # 这类词必须收进来，否则客户第一句"服务不太行"记录不到，
        # 第二句同样的抱怨也不会累计，情绪升级就永远不会触发。
        "不太行", "不行啊", "不满意", "失望", "体验差", "差劲", "无语",
        "服了", "服务差", "太慢", "等了很久", "一直没", "什么玩意儿",
    )
    negative = any(w in message for w in NEGATIVE_WORDS)
    sentiment = "negative" if negative else "neutral"

    storage.touch_session(DB, session_id, negative=negative)
    storage.add_message(DB, session_id, "customer", message, sentiment=sentiment)
    s = storage.get_session(DB, session_id)

    streak = s.get("negative_streak", 0)
    turn = s.get("turn_count", 0)
    should_escalate = streak >= 2 or (turn >= 6 and streak >= 1)

    return {
        "ok": True,
        "session_id": session_id,
        "turn_count": turn,
        "negative_streak": streak,
        "sentiment": sentiment,
        "should_escalate": should_escalate,
        "escalate_reason": ("客户连续表达不满，建议直接转人工主管" if should_escalate else ""),
        "last_order_no": s.get("last_order_no", ""),
        "source": "conversation-service",
    }


@app.get("/api/conversation/{session_id}")
def get_conversation(session_id: str, x_api_key: Optional[str] = Header(None),
                     authorization: Optional[str] = Header(None)):
    """查会话历史——人工坐席接手时看的就是这个。"""
    auth(x_api_key, authorization)
    session_id = resolve_session(session_id)
    s = storage.get_session(DB, session_id)
    return {
        "session_id": session_id,
        "turn_count": s.get("turn_count", 0),
        "negative_streak": s.get("negative_streak", 0),
        "last_order_no": s.get("last_order_no", ""),
        "messages": storage.list_messages(DB, session_id, limit=20),
        "handoff": storage.build_handoff(DB, session_id, ""),
        "source": "conversation-service",
    }


@app.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: str, x_api_key: Optional[str] = Header(None),
               authorization: Optional[str] = Header(None)):
    """查工单——带坐席交接摘要。"""
    auth(x_api_key, authorization)
    t = TICKETS.get(ticket_id) or storage.get_ticket(DB, ticket_id)
    if not t:
        return {"found": False, "message": f"没有查到工单 {ticket_id}。"}
    return {**t, "found": True, "source": "ticket-service"}


@app.post("/api/tickets/{ticket_id}/claim")
def claim_ticket(ticket_id: str, body: AgentActionIn,
                 x_api_key: Optional[str] = Header(None),
                 authorization: Optional[str] = Header(None)):
    """坐席接入工单：状态从「排队中」变「处理中」。

    真实系统里这一步会同时把工单从排队队列摘掉，避免两个坐席接同一单。
    """
    auth(x_api_key, authorization)
    t = storage.get_ticket(DB, ticket_id)
    if not t:
        return {"ok": False, "message": f"没有查到工单 {ticket_id}。"}
    if t.get("status") == "处理中" and t.get("agent") and t.get("agent") != body.agent:
        return {"ok": False, "conflict": True,
                "message": f"该工单已被 {t.get('agent')} 接入。"}
    updated = storage.update_ticket(DB, ticket_id, status="处理中",
                                    agent=body.agent, claimed_at=iso(now_utc()))
    if t.get("session_id"):
        storage.add_message(DB, t["session_id"], "system",
                            f"人工坐席 {body.agent} 已接入工单 {ticket_id}")
    return {"ok": True, "ticket": updated, "source": "ticket-service"}


@app.post("/api/tickets/{ticket_id}/reply")
def reply_ticket(ticket_id: str, body: AgentActionIn,
                 x_api_key: Optional[str] = Header(None),
                 authorization: Optional[str] = Header(None)):
    """坐席回复客户。回复同时写进工单和会话记录，供后续复盘。"""
    auth(x_api_key, authorization)
    t = storage.get_ticket(DB, ticket_id)
    if not t:
        return {"ok": False, "message": f"没有查到工单 {ticket_id}。"}
    if not body.text.strip():
        return {"ok": False, "message": "回复内容不能为空。"}
    updated = storage.append_ticket_reply(DB, ticket_id, body.agent, body.text.strip())
    if t.get("session_id"):
        storage.add_message(DB, t["session_id"], "agent", body.text.strip())
    return {"ok": True, "ticket": updated, "source": "ticket-service"}


@app.post("/api/tickets/{ticket_id}/escalate")
def escalate_ticket(ticket_id: str, body: AgentActionIn,
                    x_api_key: Optional[str] = Header(None),
                    authorization: Optional[str] = Header(None)):
    """坐席转主管：优先级拉高，状态回到排队等主管接。"""
    auth(x_api_key, authorization)
    t = storage.get_ticket(DB, ticket_id)
    if not t:
        return {"ok": False, "message": f"没有查到工单 {ticket_id}。"}
    updated = storage.update_ticket(DB, ticket_id, status="排队中", priority="高",
                                    agent=f"待主管接单（原坐席 {body.agent}）")
    return {"ok": True, "ticket": updated, "source": "ticket-service"}


@app.post("/api/tickets/{ticket_id}/close")
def close_ticket(ticket_id: str, body: AgentActionIn,
                 x_api_key: Optional[str] = Header(None),
                 authorization: Optional[str] = Header(None)):
    """关闭工单。

    unresolved_reason 是这里最重要的字段——每周导出按频次排序，
    就是知识库和规则的迭代输入。没有它，系统会一直重复同样的错。
    """
    auth(x_api_key, authorization)
    t = storage.get_ticket(DB, ticket_id)
    if not t:
        return {"ok": False, "message": f"没有查到工单 {ticket_id}。"}
    if not body.resolution:
        return {"ok": False,
                "message": "关闭前必须选处理结果（已退款/已换货/已解释/无解转主管）。"}
    updated = storage.update_ticket(DB, ticket_id, status="已解决",
                                    resolution=body.resolution,
                                    unresolved_reason=body.unresolved_reason or "",
                                    csat=body.csat, closed_at=iso(now_utc()))
    return {"ok": True, "ticket": updated, "source": "ticket-service"}


@app.get("/api/console/summary")
def console_summary(x_api_key: Optional[str] = Header(None),
                    authorization: Optional[str] = Header(None)):
    """坐席工作台顶部看板 + 未解决原因排行（周度迭代的输入）。"""
    auth(x_api_key, authorization)
    return {
        "tickets": storage.ticket_stats(DB),
        "unresolved_ranking": storage.unresolved_ranking(DB),
        "conversations": storage.stats(DB),
        "source": "console",
    }


@app.get("/api/dashboard")
def dashboard_data(x_api_key: Optional[str] = Header(None),
                   authorization: Optional[str] = Header(None)):
    """给管理者看的数据看板。

    指标口径写死在 storage.business_metrics 里，不在这里现算——
    口径一旦可以随手改，数字就失去意义了。
    """
    auth(x_api_key, authorization)
    return {
        "business": storage.business_metrics(DB),
        "tickets": storage.ticket_stats(DB),
        "resolutions": storage.resolution_distribution(DB),
        "unresolved_ranking": storage.unresolved_ranking(DB),
        "api": {
            "total_requests": sum(METRICS["requests"].values()),
            "total_errors": sum(METRICS["errors"].values()),
            "by_path": dict(sorted(METRICS["requests"].items(), key=lambda kv: -kv[1])[:12]),
            "errors_by_path": METRICS["errors"],
        },
        "targets": {
            "转人工率": "20%~35%",
            "自助解决率": "≥60%",
            "平均轮次": "≤3.5 轮",
            "满意度": "≥4.0",
        },
        "source": "dashboard",
    }


# ---------------------------------------------------------------- 前端页面

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _page(filename: str):
    """统一从 web/ 目录提供页面。

    由后端提供而不是让用户双击 HTML：那样是 file:// 源，
    调接口会被浏览器当成跨域拦掉。同源访问没有这个问题。
    """
    p = WEB_DIR / filename
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"缺少前端文件 web/{filename}")
    return FileResponse(p)


@app.get("/", include_in_schema=False)
def home_page():
    """入口页。"""
    return _page("index.html")


@app.get("/client", include_in_schema=False)
def client_page():
    """客户端聊天页 —— 客户和 AI 对话的地方，也是转人工的起点。"""
    return _page("client.html")


@app.get("/console", include_in_schema=False)
def console_page():
    """坐席工作台 —— 人工客服的界面。"""
    return _page("console.html")


@app.get("/dashboard", include_in_schema=False)
def dashboard_page():
    """数据看板 —— 转人工率、自助解决率、未解决原因排行。"""
    return _page("dashboard.html")


# ---------------------------------------------------------------- 聊天代理

# 同一个对话有两个名字：
#   · 客户端页面自己生成的会话 ID（web-xxx）—— /api/chat 存消息用这个
#   · Dify 自己生成的 conversation_id（UUID）—— 工作流传给工单接口用这个
# 不打通的话，坐席的回复会存进另一个"桶"，客户永远看不到（踩过这个坑）。
CONV_MAP: Dict[str, str] = {}      # 客户端会话 ID -> Dify conversation_id
SESSION_ALIAS: Dict[str, str] = {}  # Dify conversation_id -> 客户端会话 ID

# 启动时把映射从库里读进内存，两个方向都要：
#   · 反向（Dify ID → 客户端 ID）：坐席回复才能存对桶
#   · 正向（客户端 ID → Dify ID）：继续对话时才能接上 Dify 那边的上下文，
#     否则后端一重启，客户再说一句话就变成"新对话"，之前聊的全丢了
_PERSISTED = storage.all_dify_mappings(DB)
SESSION_ALIAS.update(_PERSISTED)
CONV_MAP.update({sid: cid for cid, sid in _PERSISTED.items()})


def resolve_session(sid: str) -> str:
    """把 Dify 的 conversation_id 翻译回客户端会话 ID。

    翻译不了的（比如从 Dify 调试窗发起的会话）原样返回，不影响使用。
    """
    sid = sid or ""
    if not sid:
        return ""
    if sid in SESSION_ALIAS:
        return SESSION_ALIAS[sid]
    # 内存里没有就查库——后端重启后内存映射会丢，库里的还在
    found = storage.find_session_by_dify_id(DB, sid)
    if found:
        SESSION_ALIAS[sid] = found
        return found
    return sid


@app.post("/api/chat")
def chat(body: ChatIn, x_api_key: Optional[str] = Header(None),
         authorization: Optional[str] = Header(None)):
    """客户端聊天入口：转发到 Dify，同时把对话记进本地库。

    为什么中间要加一层代理，而不是让前端直接调 Dify：
    1. 前端与后端同源，不用处理跨域；
    2. 所有对话都落在我们自己的库里——Dify 的日志我们控制不了，
       复盘时"客户到底说了什么"必须自己有一份；
    3. 以后要换模型平台、加限流、加黑名单，改这一层就够了。
    """
    auth(x_api_key, authorization)
    session_id = (body.session_id or "").strip()
    text = (body.message or "").strip()
    if not session_id or not text:
        return {"ok": False, "reply": "session_id 和 message 都不能为空。"}

    storage.touch_session(DB, session_id)

    # 人工接管期间 AI 让位：消息只记下来交给人工，不再走 Dify 流程。
    # 不这么做的话，客户每说一句就新建一张工单（踩过这个坑）——
    # 人工在追新工单，客户觉得根本没人管。
    active = storage.active_ticket_for(DB, session_id)
    if active:
        storage.add_message(DB, session_id, "customer", text)
        agent = active.get("agent") or ""
        return {
            "ok": True,
            "reply": "",                 # 不要 AI 回复，等人工来答
            "handed_off": True,
            "ticket_id": active["ticket_id"],
            "agent": agent,
            "notice": (f"已转达给人工客服{' ' + agent if agent else ''}，请稍等"
                       if agent else "已转达，人工客服马上接入"),
        }

    storage.add_message(DB, session_id, "customer", text)

    app_key = os.getenv("DIFY_APP_KEY", "").strip()
    if not app_key:
        return {"ok": False, "need_key": True,
                "reply": "客户端还没配好：请在项目根目录的 .env 里填入 DIFY_APP_KEY"
                         "（Dify → 打开应用 → 访问 API → 创建密钥），然后重启后端。"}

    base = os.getenv("DIFY_API_BASE", "http://localhost/v1").rstrip("/")
    payload = {
        "inputs": {},
        "query": text,
        "response_mode": "blocking",
        "conversation_id": CONV_MAP.get(session_id, ""),
        "user": session_id,
    }
    req = urllib.request.Request(
        f"{base}/chat-messages",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {app_key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        return {"ok": False, "error": detail,
                "reply": f"客服系统暂时不可用（HTTP {e.code}）。"
                         f"如果是 401/403，检查 DIFY_APP_KEY 是否正确。"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e),
                "reply": "客服系统暂时连不上，请稍后再试。"}

    reply = data.get("answer", "")
    cid = data.get("conversation_id")
    if cid:
        CONV_MAP[session_id] = cid
        SESSION_ALIAS[cid] = session_id
        storage.set_dify_conversation(DB, session_id, cid)
    # 角色区分开：ai = 机器人回复，agent = 人工坐席回复。
    # 都存成 agent 的话，客户端没法区分"这句话是谁说的"。
    storage.add_message(DB, session_id, "ai", reply)
    return {"ok": True, "reply": reply,
            "conversation_id": data.get("conversation_id", ""),
            "message_id": data.get("message_id", "")}


@app.post("/api/tickets")
def create_ticket(body: TicketIn,
                  x_api_key: Optional[str] = Header(None),
                  order_no: str = Query("", description="订单号（也可放 body）"),
                  session_id: str = Query("", description="会话 ID（也可放 body）"),
                  authorization: Optional[str] = Header(None)):
    """转人工：生成工单并返回排队信息。"""
    if order_no and not body.order_no:
        body.order_no = order_no
    if session_id and not body.session_id:
        body.session_id = session_id
    # 工作流传过来的是 Dify 自己的 conversation_id，翻译成客户端会话 ID。
    # 不翻译的话，坐席的回复会存到另一个会话里，客户永远看不到。
    body.session_id = resolve_session(body.session_id)
    auth(x_api_key, authorization)
    tid = f"TK{now_utc().strftime('%y%m%d%H%M%S')}{len(TICKETS) + 1:02d}"
    urgent = body.sentiment == "negative" or body.intent == "complaint"
    # 自动生成交接摘要：坐席打开工单就知道客户说过什么，不用让客户重讲一遍
    handoff = storage.build_handoff(DB, body.session_id, body.order_no or "") if body.session_id else ""
    TICKETS[tid] = {
        "ticket_id": tid,
        "session_id": body.session_id,
        "order_no": body.order_no,
        "intent": body.intent,
        "summary": body.summary,
        "sentiment": body.sentiment,
        "priority": "高" if urgent else "普通",
        "handoff": handoff,
        "status": "排队中",
        "created_at": iso(now_utc()),
    }
    storage.save_ticket(DB, TICKETS[tid])
    if body.session_id:
        storage.touch_session(DB, body.session_id, order_no=body.order_no or "", escalate_reset=True)
    return {
        **TICKETS[tid],
        "queue_position": 1 if urgent else 3,
        "eta_minutes": 2 if urgent else 8,
        "working_hours": "每日 9:00-21:00",
        "source": "ticket-service",
    }


@app.get("/api/tickets")
def list_tickets(status: str = Query("", description="按状态过滤：排队中 / 处理中 / 已解决"),
                 limit: int = Query(100),
                 x_api_key: Optional[str] = Header(None),
                 authorization: Optional[str] = Header(None)):
    """坐席工作台的工单列表。高优先级排在前面——坐席应该先看到最急的。"""
    auth(x_api_key, authorization)
    items = storage.list_tickets(DB, limit=limit, status=status)
    return {"total": len(items), "items": items, "source": "ticket-service"}


@app.get("/api/after-sales/policy")
def policy_lookup(q: str = Query("", description="关键词"), x_api_key: Optional[str] = Header(None),
                  authorization: Optional[str] = Header(None)):
    """轻量政策查询。正式方案里政策走 Dify 知识库，这里只做兜底和对照。"""
    auth(x_api_key, authorization)
    rules: List[Dict[str, str]] = [
        {"code": "RULE-7D-OK", "text": "签收后 7 天内，商品完好可七天无理由退货，非质量问题运费买家承担。"},
        {"code": "RULE-USED", "text": "已使用、已洗涤、已拆封的贴身用品不支持七天无理由。"},
        {"code": "RULE-REFUND-ETA", "text": "退款审核通过后 1-7 个工作日原路退回。"},
        {"code": "RULE-EXCHANGE", "text": "换货需同款有货，来回运费按质量问题与否区分承担方。"},
        {"code": "RULE-PP-OK", "text": "签收后 15 天内同款降价可退差价，需提供截图。"},
        {"code": "RULE-INVOICE", "text": "电子发票在订单详情页自助申请，开具后发到下单邮箱。"},
    ]
    hits = [r for r in rules if not q or q in r["text"]]
    return {"query": q, "hits": hits, "source": "policy-service"}


# ---------------------------------------------------------------- 可观测性

METRICS: Dict[str, Any] = {"requests": {}, "errors": {}, "started_at": iso(now_utc())}


@app.middleware("http")
async def count_requests(request, call_next):
    """统计每个接口的调用次数与错误数。

    真实客服系统必须能回答"哪个环节掉链子"——没有这层统计，
    转人工率飙升时你只能靠猜。
    """
    path = request.url.path
    METRICS["requests"][path] = METRICS["requests"].get(path, 0) + 1
    response = await call_next(request)
    if response.status_code >= 400:
        METRICS["errors"][path] = METRICS["errors"].get(path, 0) + 1
    return response


@app.get("/metrics")
def metrics(x_api_key: Optional[str] = Header(None),
            authorization: Optional[str] = Header(None)):
    """运营视角的运行指标。真实部署会接 Prometheus，这里给个最小可用版。"""
    auth(x_api_key, authorization)
    total = sum(METRICS["requests"].values())
    errors = sum(METRICS["errors"].values())
    return {
        "started_at": METRICS["started_at"],
        "orders_loaded": len(ORDERS),
        "refunds_created": len(REFUNDS),
        "tickets_created": len(TICKETS),
        "active_sessions": len(SESSIONS),
        "persisted": storage.stats(DB),
        "total_requests": total,
        "total_errors": errors,
        "error_rate": round(errors / total, 4) if total else 0,
        "requests_by_path": dict(sorted(METRICS["requests"].items(),
                                        key=lambda kv: -kv[1])),
        "errors_by_path": METRICS["errors"],
        "source": "metrics",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
