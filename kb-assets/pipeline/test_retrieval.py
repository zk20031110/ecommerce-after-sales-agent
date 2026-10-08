"""用 Dify 的检索 API 自动评测知识库召回质量。

    cd kb-assets
    python pipeline/test_retrieval.py              # 跑全部 30 条
    python pipeline/test_retrieval.py --limit 5    # 只跑前 5 条

读 tests/retrieval_cases.jsonl，逐条调用
POST /v1/datasets/{dataset_id}/retrieve，检查两件事：
    1. 命中的文档类型是否与期望一致（跨素材类型串味是最常见的问题）
    2. 命中内容里有没有期望的关键词（答案对不对）

产出 data/retrieval_report.md，含每条用例的命中排名与整体命中率。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load_env() -> None:
    env = ROOT.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def retrieve(base: str, key: str, dataset_id: str, query: str, top_k: int,
             threshold: float) -> list[dict]:
    payload = {
        "query": query,
        "retrieval_setting": {"top_k": top_k, "score_threshold": threshold},
    }
    req = urllib.request.Request(
        f"{base.rstrip('/')}/datasets/{dataset_id}/retrieve",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8")).get("records", [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.getenv("DIFY_BASE", "http://localhost/v1"))
    ap.add_argument("--dataset-id", default=os.getenv("DIFY_DATASET_ID", ""))
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--split", action="store_true",
                    help="按 data/kb_ids.json 分流：每条用例只查它该查的那个库")
    args = ap.parse_args()

    load_env()
    key = os.getenv("DIFY_DATASET_KEY", "").strip()
    base = args.base or os.getenv("DIFY_BASE", "http://localhost/v1")
    dataset_id = args.dataset_id or os.getenv("DIFY_DATASET_ID", "").strip()

    if not key:
        print("× 没读到 DIFY_DATASET_KEY（写在项目根目录 .env 里）")
        return 1
    # 分流模式：素材类型 → 该查哪个库
    type2kb: dict[str, tuple[str, str]] = {}
    ids_file = ROOT / "data" / "kb_ids.json"
    if args.split:
        if not ids_file.exists():
            print("× 没找到 data/kb_ids.json，先跑：python pipeline/setup_kbs.py")
            return 1
        for kb_name, v in json.loads(ids_file.read_text(encoding="utf-8")).items():
            for t in v.get("types", []):
                type2kb[t] = (kb_name, v["id"])

    if not dataset_id and not args.split:
        print("× 缺少 dataset id。先跑：python pipeline/upload_to_dify.py --list")
        return 1

    # 文档名 → 素材类型 的映射。文档名现在以 chunk_id 开头（为了全局唯一），
    # 不再以素材类型开头，所以要靠本地 chunks.jsonl 反查它属于哪类素材。
    chunks_path = ROOT / "data" / "chunks.jsonl"
    name2type: dict[str, str] = {}
    if chunks_path.exists():
        for line in chunks_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            c = json.loads(line)
            name2type[f"{c['chunk_id']}｜{c['title'][:40]}"] = c["doc_type"]

    cases = [json.loads(l) for l in
             (ROOT / "tests" / "retrieval_cases.jsonl").read_text(encoding="utf-8").splitlines()
             if l.strip()]
    if args.limit:
        cases = cases[: args.limit]

    print(f"评测 {len(cases)} 条检索用例（top_k={args.top_k}，阈值={args.threshold}）...\n")
    rows, type_hit, kw_hit = [], 0, 0
    for c in cases:
        # expect_type 可以是字符串或列表：
        # 政策类问题的答案既可能来自政策文档(03)，也可能来自 FAQ 清单(04)，
        # 两者在同一个库里，都算命中。写死成单值会误判成失败。
        exp = c["expect_type"]
        exp_set = {exp} if isinstance(exp, str) else set(exp)
        if args.split:
            kb_name, target = type2kb.get(next(iter(exp_set)), ("未配置", dataset_id))
        else:
            kb_name, target = "", dataset_id
        try:
            records = retrieve(base, key, target, c["query"], args.top_k, args.threshold)
        except urllib.error.HTTPError as e:
            print(f"  × {c['id']} HTTP {e.code}: {e.read().decode('utf-8','ignore')[:120]}")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"  × {c['id']} {e}")
            continue

        best_type_rank = 0
        best_kw_rank = 0
        for i, r in enumerate(records, 1):
            seg = r.get("segment", {})
            doc_name = (seg.get("document", {}) or {}).get("name", "")
            text = seg.get("content", "")
            if not best_type_rank and name2type.get(doc_name, "") in exp_set:
                best_type_rank = i
            if not best_kw_rank and any(k in text for k in c["expect_keywords"]):
                best_kw_rank = i

        ok_type = bool(best_type_rank)
        ok_kw = bool(best_kw_rank)
        type_hit += ok_type
        kw_hit += ok_kw
        mark = "√" if (ok_type and ok_kw) else ("△" if (ok_type or ok_kw) else "×")
        print(f"  {mark} {c['id']}  [{len(records)} 条召回]  类型排名={best_type_rank or '-'}  "
              f"关键词排名={best_kw_rank or '-'}  库={kb_name or '单库'}  {c['query'][:22]}")
        rows.append({
            "case": c, "records": records,
            "type_rank": best_type_rank, "kw_rank": best_kw_rank,
            "kb": kb_name,
            "top_doc": (records[0].get("segment", {}).get("document", {}) or {}).get("name", "")
            if records else "",
        })

    n = len(rows) or 1
    lines = [
        "# 知识库检索评测报告", "",
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}　"
        f"参数：top_k={args.top_k}，score 阈值={args.threshold}", "",
        f"- 用例数：{len(rows)}",
        f"- **素材类型命中率：{type_hit}/{len(rows)} = {type_hit / n:.0%}**"
        f"（期望类型的文档是否出现在召回里）",
        f"- **关键词命中率：{kw_hit}/{len(rows)} = {kw_hit / n:.0%}**"
        f"（召回内容是否包含正确答案的特征词）",
        "",
        "## 逐条结果", "",
        "| 用例 | 问题 | 期望类型 | 查询的库 | 类型排名 | 关键词排名 | 实际召回的首条文档 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        c = r["case"]
        lines.append(
            f"| {c['id']} | {c['query'][:22]} | {c['expect_type']} | {r['kb'] or '单库'} | "
            f"{r['type_rank'] or '未命中'} | {r['kw_rank'] or '未命中'} | {r['top_doc'][:40]} |")

    bad = [r for r in rows if not (r["type_rank"] and r["kw_rank"])]
    lines += ["", "## 未完全命中的用例", ""]
    if bad:
        for r in bad:
            c = r["case"]
            lines.append(f"- **{c['id']}**「{c['query']}」：期望 {c['expect_type']} / "
                         f"{c['expect_keywords']}，实际首条召回「{r['top_doc']}」。{c['note']}")
    else:
        lines.append("无，全部命中。")

    lines += ["", "## 怎么处理未命中的用例", "",
              "1. **类型没命中**（串味）：说明不同素材的表述太像。解决办法是拆知识库"
              "（政策库 / 手册库分开），或在检索节点开启元数据过滤。",
              "2. **关键词没命中**：说明切块把答案切散了，或该内容压根没入库。"
              "先确认文档在库里，再检查切块边界。",
              "3. **都命中了但排名靠后**：提高 top_k，或开 Rerank 重排。",
              "4. 改完重跑本脚本对比，不要把「感觉好点了」当成结论。"]

    (DATA / "retrieval_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n类型命中率 {type_hit}/{len(rows)}，关键词命中率 {kw_hit}/{len(rows)}")
    print("报告：data/retrieval_report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
