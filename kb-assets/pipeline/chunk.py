"""切块：把清洗后的文档切成适合向量检索的块。

    python pipeline/chunk.py [--size 512] [--overlap 51]

产出：
    data/chunks.jsonl   每行一个块，供入库与抽检
    data/chunk_stats.md 切块统计报告

切块策略说明（这是检索质量的关键，值得写进简历）：
    1. 先按空行切成"语义块"，再贪心合并到接近目标长度——不硬切句子
    2. 保留 10% 重叠，避免答案正好被切在两块之间
    3. 每个块带上来源文档标题与品类，作为检索后的引用信息
    4. 单块超过 2 倍目标长度时按句号切分，防止出现巨型块拉低检索精度
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

SENT_END = re.compile(r"(?<=[。！？；])")


def split_blocks(text: str) -> list[str]:
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    return blocks


def split_long(block: str, size: int) -> list[str]:
    if len(block) <= size * 2:
        return [block]
    parts = [p for p in SENT_END.split(block) if p]
    out, buf = [], ""
    for p in parts:
        if len(buf) + len(p) > size:
            out.append(buf)
            buf = p
        else:
            buf += p
    if buf:
        out.append(buf)
    return out


def chunk_doc(rec: dict, size: int, overlap: int) -> list[dict]:
    blocks = []
    for b in split_blocks(rec["text"]):
        blocks.extend(split_long(b, size))

    chunks, buf = [], ""
    for b in blocks:
        if buf and len(buf) + len(b) + 1 > size:
            chunks.append(buf)
            tail = buf[-overlap:] if overlap else ""
            buf = (tail + "\n" + b) if tail else b
        else:
            buf = (buf + "\n" + b) if buf else b
    if buf:
        chunks.append(buf)

    out = []
    for i, c in enumerate(chunks):
        out.append({
            "chunk_id": f"{rec['doc_id']}#{i + 1}",
            "doc_id": rec["doc_id"],
            "doc_type": rec["doc_type"],
            "category": rec["category"],
            "source_file": rec["source_file"],
            "title": rec["title"],
            "text": c,
            "chars": len(c),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=512)
    ap.add_argument("--overlap", type=int, default=51)
    args = ap.parse_args()

    src = DATA / "cleaned.jsonl"
    if not src.exists():
        print("先跑 python pipeline/clean.py")
        return 1

    records = [json.loads(ln) for ln in src.read_text(encoding="utf-8").splitlines() if ln.strip()]
    all_chunks = []
    for rec in records:
        all_chunks.extend(chunk_doc(rec, args.size, args.overlap))

    out = DATA / "chunks.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for c in all_chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    sizes = [c["chars"] for c in all_chunks]
    by_type = Counter(c["doc_type"] for c in all_chunks)
    total_chars = sum(sizes)
    est_tokens = total_chars // 2  # 中文粗估：1 token ≈ 2 字符

    report = [
        "# 切块统计报告", "",
        f"- 输入文档：{len(records)} 份",
        f"- 输出块数：**{len(all_chunks)}**",
        f"- 参数：目标 {args.size} 字 / 重叠 {args.overlap} 字",
        f"- 平均块长：{total_chars // max(len(all_chunks), 1)} 字",
        f"- 最短 / 最长：{min(sizes)} / {max(sizes)} 字",
        f"- 估算 token 总量：约 {est_tokens:,}（按 1 token ≈ 2 中文字符粗估）",
        "",
        "## 按素材类型分布", "",
        "| 素材类型 | 块数 |", "|---|---|",
    ]
    for k, v in by_type.most_common():
        report.append(f"| {k} | {v} |")
    report += ["", "## 入库成本预估", "",
               f"按通义千问 text-embedding 的常见定价，全量入库一次约需处理 {est_tokens:,} tokens。",
               "建议先入 30 份验证召回，再全量入库，避免反复重建索引造成重复计费。"]
    (DATA / "chunk_stats.md").write_text("\n".join(report), encoding="utf-8")

    print(f"切块完成：{len(all_chunks)} 个块 → {out.name}")
    print(f"  平均 {total_chars // max(len(all_chunks), 1)} 字/块，最长 {max(sizes)} 字")
    print(f"  估算 {est_tokens:,} tokens（全量向量化成本参考）")
    print(f"  报告：data/chunk_stats.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
