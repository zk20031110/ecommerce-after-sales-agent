"""入库对账：用知识库的真实状态校准本地台账，找出被丢弃的块。

    cd kb-assets
    python pipeline/reconcile.py            # 只对账，不动数据
    python pipeline/reconcile.py --fix      # 对账后重写台账（这样重跑上传只会补缺失的）

为什么需要它：
上传脚本按 HTTP 200 判断成功，但 Dify 的文档创建是异步的——
embedding 接口被限流（429）时，文档可能压根没落库，而脚本已经记成"成功"。
实测一次灌 1322 个块，实际只入库 713 个，台账却全是成功记录。

对账方式：文档名是可复现的（素材类型｜标题｜块序号），用它与本地 chunks.jsonl 比对。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections import Counter
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


def get(base: str, key: str, path: str) -> dict:
    req = urllib.request.Request(f"{base}{path}", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_all_docs(base: str, key: str, dataset_id: str) -> list[dict]:
    docs, page = [], 1
    while True:
        d = get(base, key, f"/datasets/{dataset_id}/documents?limit=100&page={page}")
        items = d.get("data", [])
        docs.extend(items)
        if len(items) < 100 or page > 30:
            break
        page += 1
    return docs


def chunk_name(c: dict) -> str:
    """必须与 upload_to_dify.py 里的命名规则完全一致，否则对不上。"""
    return f"{c['chunk_id']}｜{c['title'][:40]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="按实际状态重写台账")
    args = ap.parse_args()

    load_env()
    key = os.getenv("DIFY_DATASET_KEY", "").strip()
    base = os.getenv("DIFY_BASE", "http://localhost/v1")
    if not key:
        print("× 没读到 DIFY_DATASET_KEY")
        return 1

    ids_file = DATA / "kb_ids.json"
    if not ids_file.exists():
        print("× 没找到 data/kb_ids.json，先跑 pipeline/setup_kbs.py")
        return 1
    kb_map = json.loads(ids_file.read_text(encoding="utf-8"))
    type2kb = {t: (name, v["id"]) for name, v in kb_map.items() for t in v.get("types", [])}

    chunks = [json.loads(l) for l in
              (DATA / "chunks.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    expect: dict[tuple[str, str], dict] = {}
    for c in chunks:
        kb_name, _ = type2kb.get(c["doc_type"], ("未配置", ""))
        expect[(kb_name, chunk_name(c))] = c

    print("从 Dify 拉取三个库的实际文档 ...")
    actual: dict[str, dict[str, dict]] = {}
    for name, v in kb_map.items():
        docs = fetch_all_docs(base, key, v["id"])
        actual[name] = {d["name"]: d for d in docs}
        status = Counter(d.get("indexing_status") for d in docs)
        print(f"  {name}: {len(docs)} 篇   状态 {dict(status)}")

    missing, present, errored = [], [], []
    for (kb_name, dname), c in expect.items():
        doc = actual.get(kb_name, {}).get(dname)
        if not doc:
            missing.append((kb_name, c))
        elif doc.get("indexing_status") == "error":
            errored.append((kb_name, c))
        else:
            present.append((kb_name, c))

    total = len(expect)
    print(f"\n对账结果（本地应有 {total} 个块）：")
    print(f"  √ 已入库且正常：{len(present)}")
    print(f"  ! 入库但索引失败：{len(errored)}")
    print(f"  × 根本没入库：{len(missing)}")

    if errored:
        print("\n索引失败的样例（embedding 被限流导致，重传即可）：")
        for kb_name, c in errored[:3]:
            print(f"  · [{kb_name}] {c['chunk_id']}")
    if missing:
        print("\n未入库的样例：")
        by_kb = Counter(k for k, _ in missing)
        for kb_name, n in by_kb.most_common():
            print(f"  · {kb_name}：缺 {n} 个")

    if args.fix:
        backup = LEDGER.with_suffix(".jsonl.bak")
        if LEDGER.exists():
            backup.write_text(LEDGER.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"\n原台账已备份到 {backup.name}")
        keep = []
        for kb_name, c in present:
            keep.append({"chunk_id": c["chunk_id"], "kb": kb_name,
                         "name": chunk_name(c), "reconciled": True})
        with LEDGER.open("w", encoding="utf-8") as f:
            for rec in keep:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"台账已重写：{len(keep)} 条（接下来的上传只会补缺失的 {len(missing) + len(errored)} 个）")
        print("\n下一步：慢速补传（把 --sleep 调大，避免再次触发限流）")
        print("  python pipeline/upload_to_dify.py --split --sleep 1.0")
    else:
        print("\n（加 --fix 可按实际状态重写台账）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
