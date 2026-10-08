"""意图识别评测：跑真实工作流，把"识别准不准"变成一个能报的数字。

    python tools/eval_intent.py                 # 两套用例都跑（46 + 44 条）
    python tools/eval_intent.py --set standard  # 只跑标准问法
    python tools/eval_intent.py --set colloquial --limit 10

为什么不能拿提示词单独跑一遍就完事：
那样测的是"提示词在本地模型上的表现"，不是**线上工作流的真实表现**。
这个脚本每条用例都真调一次已部署的 Dify 应用，再从后端库里把分类器
判出来的意图取回来比对——测的是同一条链路，结果才能写进简历。

链路：
    脚本 → Dify /chat-messages（真实工作流）
         → 工作流内「记录对话」节点把 intent 一起报给后端
         → 脚本查 /api/conversation/{会话} 取回 intent
         → 与用例的 expect_class 比对

前置条件：
    1. mock 后端在跑：cd mock-api && python -m uvicorn main:app --port 8000
    2. .env 里有 DIFY_APP_KEY、DIFY_API_BASE
    3. Dify 里导入的是**带 intent 字段的最新版 DSL**（记录对话节点）
       如果没导入，脚本会提示"取不到 intent"，不会给出假数字。

耗时与成本：每条用例一次模型调用，90 条约 6~10 分钟，成本约 ¥0.13。
报告写到 tests/reports/intent_report.md（该目录已 gitignore）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "tests" / "reports"

SETS = {
    "standard": ROOT / "tests" / "cases.jsonl",
    "colloquial": ROOT / "tests" / "cases_colloquial.jsonl",
}

PROBE = "订单 2024091288765 我要退货"


def load_env() -> None:
    """把项目根目录的 .env 读进环境变量（已存在的环境变量优先）。"""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def post_json(url: str, payload: dict, headers: dict, timeout: int = 90) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(url: str, headers: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ask_workflow(base: str, app_key: str, text: str, user: str) -> str:
    """调一次真实工作流，返回 Dify 的 conversation_id。"""
    out = post_json(
        f"{base}/chat-messages",
        {"inputs": {}, "query": text, "response_mode": "blocking",
         "conversation_id": "", "user": user},
        {"Authorization": f"Bearer {app_key}"})
    return out.get("conversation_id", "")


def read_intent(api: str, api_key: str, conversation_id: str) -> str:
    """从后端取回这轮对话上记录的意图。

    会话里可能有多条客户消息（客户端记一条、工作流再记一条），
    取**最后一条带 intent 的客户消息**——那才是分类器判出来的。
    """
    data = get_json(f"{api}/api/conversation/{conversation_id}",
                    {"X-API-Key": api_key})
    for m in reversed(data.get("messages", [])):
        if m.get("role") == "customer" and (m.get("intent") or "").strip():
            return m["intent"].strip()
    return ""


def run_set(name: str, path: Path, base: str, api: str, app_key: str, api_key: str,
            limit: int, delay: float) -> dict:
    cases = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if limit:
        cases = cases[:limit]
    print(f"\n[{name}] {len(cases)} 条用例（{path.name}）")

    rows, empty = [], 0
    for i, c in enumerate(cases, 1):
        session = f"eval-{name}-{c['id']}"
        try:
            conv = ask_workflow(base, app_key, c["input"], session)
            time.sleep(delay)          # 给后端的「记录对话」节点留出落库时间
            got = read_intent(api, api_key, conv)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:160]
            print(f"  × {c['id']} HTTP {e.code}: {body}")
            rows.append({"case": c, "got": "<请求失败>", "ok": False})
            continue
        except Exception as e:  # noqa: BLE001
            print(f"  × {c['id']} {e}")
            rows.append({"case": c, "got": "<请求失败>", "ok": False})
            continue

        if not got:
            empty += 1
        ok = got == c["expect_class"]
        mark = "√" if ok else "×"
        print(f"  {mark} {c['id']}  期望={c['expect_class']}  实际={got or '(空)'}  {c['input'][:24]}")
        rows.append({"case": c, "got": got, "ok": ok})

    hit = sum(1 for r in rows if r["ok"])
    return {"name": name, "rows": rows, "hit": hit, "total": len(rows), "empty": empty}


def post_form(url: str, fields: dict, headers: dict, timeout: int = 30) -> dict:
    import urllib.parse
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def show_conversation(api: str, api_key: str, conv: str) -> list[dict]:
    data = get_json(f"{api}/api/conversation/{conv}", {"X-API-Key": api_key})
    msgs = data.get("messages", [])
    if not msgs:
        print("    （后端库里没有这个会话的任何消息）")
    for m in msgs:
        print(f"    [{m.get('role')}] intent={m.get('intent') or '(空)'}  "
              f"{str(m.get('content'))[:34]}")
    return msgs


def selfcheck(base: str, api: str, app_key: str, api_key: str) -> int:
    """一次自检：把"意图取不到"这件事拆成后端 / Dify 两半，定位到底哪半边断的。"""
    print("=" * 60)
    print("自检：意图上报链路")
    print("=" * 60)
    print(f"后端地址        : {api}")
    print(f"Dify 地址       : {base}")
    print(f"Dify 应用密钥   : {'已配置 ' + app_key[:8] + '…' if app_key else '【缺失】'}")

    # 1) 后端活着吗
    try:
        get_json(f"{api}/api/dashboard", {"X-API-Key": api_key})
        print("后端连通        : √")
    except Exception as e:  # noqa: BLE001
        print(f"后端连通        : × {e}")
        print("\n先把后端起起来：cd mock-api && python -m uvicorn main:app --port 8000")
        return 2

    # 2) 先判断 Dify 里跑的是不是新版 DSL：
    #    新版把"降价"接到了价保接口，旧版会走知识库并答"不支持退差价"。
    try:
        probe_out = post_json(
            f"{base}/chat-messages",
            {"inputs": {}, "query": "刚买就降价了能退差价吗", "response_mode": "blocking",
             "conversation_id": "", "user": f"probe-dsl-{int(time.time())}"},
            {"Authorization": f"Bearer {app_key}"})
        answer = str(probe_out.get("answer", ""))
        print(f"\n新版 DSL 探测（价保问题）：")
        print(f"    {answer[:120].replace(chr(10), ' ')}")
        if "知识库" in answer or "不支持" in answer:
            print("    → 这是**旧版**行为：降价被丢给了知识库。")
            print("    先重新导入 dify/after-sales-agent.yml 再跑评测，否则结果没意义。")
            return 1
        print("    → 走了价保接口，说明新版 DSL 已生效。")
    except Exception as e:  # noqa: BLE001
        print(f"\n新版 DSL 探测失败：{e}")

    # 3) 真跑一次工作流
    conv = f"eval-check-{int(time.time())}"
    try:
        cid = ask_workflow(base, app_key, PROBE, conv)
    except urllib.error.HTTPError as e:
        print(f"Dify 调用       : × HTTP {e.code} "
              f"{e.read().decode('utf-8', 'ignore')[:200]}")
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"Dify 调用       : × {e}")
        return 2
    print(f"Dify 调用       : √ 会话 {cid}")
    print(f"\n工作流跑完后，后端库里记到的是：")
    time.sleep(1.5)
    msgs = show_conversation(api, api_key, cid)

    # 4) 后端自己写一次意图，验证写库这半边
    print("\n再手动让后端写一次意图（模拟工作流的上报节点）：")
    try:
        r = post_form(f"{api}/api/intent",
                      {"session_id": cid, "intent": "退款退货"},
                      {"X-API-Key": api_key})
        print(f"    后端返回 {r}")
    except Exception as e:  # noqa: BLE001
        print(f"    × {e}")
        return 2
    time.sleep(0.5)
    show_conversation(api, api_key, cid)

    # 5) 给结论
    print("\n" + "=" * 60)
    if not msgs:
        print("结论：工作流没把消息记到后端。")
        print("  → Dify 里的「记录对话」节点访问不到后端（先看后端控制台有没有 POST /api/conversation/turn）。")
    elif all(not (m.get("intent") or "") for m in msgs):
        print("结论：消息能记上，但**意图没上报**——Dify 里跑的不是最新版 DSL。")
        print("  → 把 dify/after-sales-agent.yml 重新导入一次；")
        print("    导入后确认画布上有一个叫「上报意图」的节点，")
        print("    它的 intent 变量取的是 「意图识别」的 class_name。")
    else:
        print("结论：链路是通的（上面最后一条消息已经有 intent 了），可以直接跑全量。")
    print("=" * 60)
    return 0


def confusion(rows: list[dict]) -> list[str]:
    """混淆矩阵：把"错到哪儿去了"列出来，这是迭代提示词的直接输入。"""
    pairs: dict[tuple[str, str], list[str]] = {}
    for r in rows:
        exp, got = r["case"]["expect_class"], r["got"] or "(空)"
        if exp != got:
            pairs.setdefault((exp, got), []).append(r["case"]["id"])
    if not pairs:
        return ["无错例。"]
    lines = ["| 应该判成 | 实际判成 | 条数 | 用例 |", "|---|---|---|---|"]
    for (exp, got), ids in sorted(pairs.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"| {exp} | {got} | {len(ids)} | {'、'.join(ids[:6])} |")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=["all", *SETS], default="all")
    ap.add_argument("--limit", type=int, default=0, help="每套只跑前 N 条，调试用")
    ap.add_argument("--check", action="store_true",
                    help="自检：只跑一条，把「取不到意图」拆成后端 / Dify 两半定位")
    ap.add_argument("--delay", type=float, default=1.2,
                    help="每条用例之间的间隔秒数（默认 1.2，防止打爆后端）")
    args = ap.parse_args()

    load_env()
    app_key = os.getenv("DIFY_APP_KEY", "").strip()
    base = os.getenv("DIFY_API_BASE", "http://localhost/v1").rstrip("/")
    api = os.getenv("MOCK_API_BASE", "http://127.0.0.1:8000").rstrip("/")
    api_key = os.getenv("MOCK_API_KEY", "dev-local-key")

    if not app_key:
        print("× .env 里没有 DIFY_APP_KEY，没法调工作流。")
        print("  Dify → 打开应用 → 左侧「访问 API」→ 创建密钥，填进 .env 后重跑。")
        return 2

    if args.check:
        return selfcheck(base, api, app_key, api_key)

    sets = list(SETS) if args.set == "all" else [args.set]
    results = [run_set(n, SETS[n], base, api, app_key, api_key, args.limit, args.delay)
               for n in sets]

    total_hit = sum(r["hit"] for r in results)
    total = sum(r["total"] for r in results)
    empty_total = sum(r["empty"] for r in results)
    print(f"\n总准确率：{total_hit}/{total} = {total_hit / total:.0%}")

    if empty_total:
        print(f"\n! 有 {empty_total} 条取不到意图，这次的结果不能用。")
        print("  先跑自检定位断点：python tools/eval_intent.py --check")
        print("  最常见的两个原因：")
        print("  1) Dify 里还是旧版 DSL —— 需要重新导入 dify/after-sales-agent.yml")
        print("     导入后画布上应有一个「上报意图」节点（8 个类别各连一条边）")
        print("  2) 「上报意图」节点的变量取错了 —— 应该取「意图识别」的 class_name")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 意图识别评测报告", "",
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}　"
        f"方式：调真实 Dify 工作流，从后端取回分类结果", "",
        f"- 用例总数：{total}",
        f"- **准确率：{total_hit}/{total} = {total_hit / total:.0%}**",
    ]
    for r in results:
        lines += ["", f"## {r['name']}（{r['hit']}/{r['total']}）", "",
                  "| 用例 | 输入 | 期望 | 实际 | 结果 |", "|---|---|---|---|---|"]
        for row in r["rows"]:
            c = row["case"]
            lines.append(f"| {c['id']} | {c['input'][:30]} | {c['expect_class']} | "
                         f"{row['got'] or '(空)'} | {'√' if row['ok'] else '×'} |")
        lines += ["", "### 错例分布", "", *confusion(r["rows"])]
    lines += ["", "## 怎么用这份报告", "",
              "1. 错例分布里**同一对错法反复出现**的，说明提示词里的判定规则写得不清楚，"
              "补一条带例子的规则比换个模型有效。",
              "2. 口语集准确率明显低于标准集，说明规则依赖关键词、不够口语化。",
              "3. 每次改完分类器指令重跑一次，把准确率记下来——"
              "**能报出数字，和只能说'挺准的'，是两个层次。**"]
    out = REPORT_DIR / "intent_report.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
