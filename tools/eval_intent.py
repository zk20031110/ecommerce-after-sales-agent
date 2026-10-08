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

    sets = list(SETS) if args.set == "all" else [args.set]
    results = [run_set(n, SETS[n], base, api, app_key, api_key, args.limit, args.delay)
               for n in sets]

    total_hit = sum(r["hit"] for r in results)
    total = sum(r["total"] for r in results)
    empty_total = sum(r["empty"] for r in results)
    print(f"\n总准确率：{total_hit}/{total} = {total_hit / total:.0%}")

    if empty_total:
        print(f"\n! 有 {empty_total} 条取不到意图。最可能的原因：")
        print("  Dify 里导入的 DSL 不是最新版——「记录对话」节点需要带上 intent 变量")
        print("  （值填 {{#1800000000002.class_name#}}）。没有它，意图只存在 Dify 里，")
        print("  后端拿不到，也就没法算准确率。")

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
