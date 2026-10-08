"""把切好的块批量上传到 Dify 知识库。

先看你的知识库列表（确认 dataset id）：
    python pipeline/upload_to_dify.py --list

全量上传：
    python pipeline/upload_to_dify.py --dataset-id <ID> --limit 30   # 先小批量验证
    python pipeline/upload_to_dify.py --dataset-id <ID>              # 确认后跑全量

特性：
    --dry-run   只打印将要上传的内容，不发请求
    --limit N   只传前 N 份
    --type      只传某一类素材，例如 01-产品手册
    断点续传：已上传的记在 data/uploaded.jsonl，重跑自动跳过
    失败重试：429/5xx 自动退避重试 3 次

API Key 从环境变量 DIFY_DATASET_KEY 读，避免写进命令行历史。
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
DATA = ROOT / "data"
LEDGER = DATA / "uploaded.jsonl"


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


def api(base: str, key: str, path: str, payload: dict | None = None,
        method: str = "GET", retries: int = 3) -> dict:
    url = f"{base.rstrip('/')}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    for attempt in range(retries):
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:300]
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                wait = 2 ** attempt
                print(f"    HTTP {e.code}，{wait}s 后重试 ...")
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code}: {body}") from None
    raise RuntimeError("重试次数用尽")


def load_ledger() -> set[tuple[str, str]]:
    """返回 {(知识库名, chunk_id)}。

    必须带上知识库名：同一个块传到旧库和传到新拆的库是两回事，
    只用 chunk_id 去重会导致"换了库但被判定为已传"，一个都传不上去。
    早期记录没有 kb 字段，按空字符串处理。
    """
    if not LEDGER.exists():
        return set()
    done: set[tuple[str, str]] = set()
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                r = json.loads(line)
                done.add((r.get("kb", ""), r["chunk_id"]))
            except Exception:
                pass
    return done


def append_ledger(chunk_id: str, doc_name: str, res: dict, kb_name: str = "") -> None:
    rec = {"chunk_id": chunk_id, "name": doc_name, "ts": datetime.now().isoformat(timespec="seconds"),
           "kb": kb_name, "document_id": (res.get("document") or {}).get("id")}
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.getenv("DIFY_BASE", "http://localhost/v1"))
    ap.add_argument("--dataset-id", default=os.getenv("DIFY_DATASET_ID", ""))
    ap.add_argument("--list", action="store_true", help="列出知识库，用来找 dataset id")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--type", default="", help="只上传某一类，如 01-产品手册")
    ap.add_argument("--split", action="store_true",
                    help="按 data/kb_ids.json 把不同素材传到对应知识库（推荐）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.35, help="每次请求间隔秒数，避免触发限流")
    args = ap.parse_args()

    load_env()
    key = os.getenv("DIFY_DATASET_KEY", "").strip()
    if not key:
        print("× 没读到 DIFY_DATASET_KEY。请先设置环境变量，或写进项目根目录的 .env：")
        print("    DIFY_DATASET_KEY=dataset-xxxxxxxx")
        return 1

    if args.list:
        print("知识库列表：")
        try:
            res = api(args.base, key, "/datasets?limit=100")
        except Exception as e:  # noqa: BLE001
            print("  查询失败:", e)
            return 1
        for d in res.get("data", []):
            print(f"  · {d.get('name')}   id={d.get('id')}   文档数={d.get('document_count')}")
        if not res.get("data"):
            print("  （没有知识库，可先在 Dify 界面建一个）")
        return 0

    # 分流映射：素材类型 → (知识库名, 知识库 id)
    type2kb: dict[str, tuple[str, str]] = {}
    if args.split:
        ids_file = DATA / "kb_ids.json"
        if not ids_file.exists():
            print("× 没找到 data/kb_ids.json，先跑：python pipeline/setup_kbs.py")
            return 1
        kb_map = json.loads(ids_file.read_text(encoding="utf-8"))
        for kb_name, v in kb_map.items():
            for t in v.get("types", []):
                type2kb[t] = (kb_name, v["id"])
        print("分流方案：")
        for kb_name, v in kb_map.items():
            print(f"  {kb_name}  ←  {'、'.join(v.get('types', []))}")
    elif not args.dataset_id:
        print("× 缺少 --dataset-id（或改用 --split 走分流模式）。先跑 --list 看 id。")
        return 1

    chunks_path = DATA / "chunks.jsonl"
    if not chunks_path.exists():
        print("× 没找到 data/chunks.jsonl，先跑 pipeline/clean.py 和 pipeline/chunk.py")
        return 1
    chunks = [json.loads(ln) for ln in chunks_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if args.type:
        chunks = [c for c in chunks if c["doc_type"].startswith(args.type)]
    if args.split:
        unknown = {c["doc_type"] for c in chunks} - set(type2kb)
        if unknown:
            print(f"× 这些素材类型没在 kb_ids.json 里配置：{sorted(unknown)}")
            return 1
    if args.limit:
        if args.split:
            # 分流模式下按库均匀取样，保证三个库都能验证到
            buckets: dict[str, list[dict]] = {}
            for c in chunks:
                buckets.setdefault(type2kb[c["doc_type"]][0], []).append(c)
            per = max(1, args.limit // max(len(buckets), 1))
            picked: list[dict] = []
            for items in buckets.values():
                picked.extend(items[:per])
            chunks = picked
            print(f"  小批量验证：每个库各取 {per} 个块")
        else:
            chunks = chunks[: args.limit]

    done = load_ledger()
    if args.split:
        todo = [c for c in chunks if (type2kb[c["doc_type"]][0], c["chunk_id"]) not in done]
    else:
        todo = [c for c in chunks if ("", c["chunk_id"]) not in done]
    print(f"待上传 {len(todo)} 个块（台账已有 {len(done)} 条 (库,块) 记录）")
    if args.dry_run:
        for c in todo[:10]:
            print(f"  [dry-run] {c['chunk_id']}  {c['chars']} 字  {c['text'][:60]}...")
        print(f"  ... 共 {len(todo)} 个，未发送任何请求")
        return 0

    ok = fail = 0
    for i, c in enumerate(todo, 1):
        # 文档名必须全局唯一，所以带上 chunk_id。
        # 踩过的坑：早期用「素材类型｜标题｜块序号」命名，而 FAQ 里同一个问题
        # 在 12 个品类下重复出现，标题完全相同 → 1330 个块只有 715 个唯一名字，
        # Dify 按名字去重，615 个块压根没入库。
        name = f"{c['chunk_id']}｜{c['title'][:40]}"
        if args.split:
            kb_name, target_id = type2kb[c["doc_type"]]
        else:
            kb_name, target_id = "", args.dataset_id
        try:
            res = api(args.base, key, f"/datasets/{target_id}/document/create-by-text",
                      payload={
                          "name": name,
                          "text": c["text"],
                          "indexing_technique": "high_quality",
                          "process_rule": {"mode": "automatic"},
                      }, method="POST")
            append_ledger(c["chunk_id"], name, res, kb_name)
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  × {c['chunk_id']} 失败：{e}")
            if fail > 20:
                print("  失败过多，中止。请检查 dataset id 与 API Key。")
                break
        if i % 20 == 0:
            print(f"  进度 {i}/{len(todo)}  成功 {ok} 失败 {fail}")
        time.sleep(args.sleep)

    print(f"\n完成：成功 {ok}，失败 {fail}，记录在 {LEDGER.name}")
    print("去 Dify 知识库页面确认文档数与索引状态。")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
