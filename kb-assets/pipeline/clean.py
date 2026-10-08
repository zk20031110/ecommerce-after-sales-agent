"""清洗：把 515 份 DOCX / 15 份 Markdown / 2 个 Excel 统一抽成纯文本。

    python pipeline/clean.py

产出 data/cleaned.jsonl，每行一条文档：
    {"doc_id","doc_type","category","source_file","title","text"}

清洗动作：
    1. 去掉页脚水印与页码
    2. 合并多余空行、统一全角空格
    3. 统一术语（"七天无理由"→"7 天无理由"、"三十天"→"30 天"）
    4. 剔除生成模板留下的空占位

术语统一这一步很关键：知识库里同一个意思有两种写法，检索时会分散命中，
召回率会明显下降。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "gen"))

from docx import Document  # noqa: E402
from docx_util import FOOTER_TEXT  # noqa: E402

ROOT = HERE.parent
OUTPUT_DIR = ROOT / "output"
DATA = ROOT / "data"

# 术语统一表：左边写得越全越好，避免误替换
NORMALIZE = [
    ("七天无理由", "7 天无理由"),
    ("七日无理由", "7 天无理由"),
    ("三十天", "30 天"),
    ("十五天", "15 天"),
    ("四十八小时", "48 小时"),
    ("二十四小时", "24 小时"),
    ("一到七天", "1-7 天"),
    ("一至七个工作日", "1-7 个工作日"),
    ("（", "("), ("）", ")"),
    ("：", ":"), ("，", ","),
]

NOISE_PATTERNS = [
    re.compile(r"^第\s*\d+\s*页.*$", re.M),
    re.compile(r"^\s*[-—=·]{3,}\s*$", re.M),
]


def normalize(text: str) -> str:
    for a, b in NORMALIZE:
        text = text.replace(a, b)
    return text


def clean_text(text: str) -> str:
    text = text.replace(FOOTER_TEXT, "")
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    for pat in NOISE_PATTERNS:
        text = pat.sub("", text)
    lines = [ln.rstrip() for ln in text.splitlines()]
    out, blank = [], 0
    for ln in lines:
        if not ln.strip():
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(ln)
    text = "\n".join(out).strip()
    return normalize(text)


def docx_to_text(path: Path) -> tuple[str, str]:
    doc = Document(str(path))
    lines = [p.text for p in doc.paragraphs if p.text.strip()]
    for t in doc.tables:
        for row in t.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                lines.append(" | ".join(cells))
    title = lines[0] if lines else path.stem
    return title, "\n".join(lines)


def md_to_text(path: Path) -> tuple[str, str]:
    raw = path.read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    title = lines[0].lstrip("# ").strip() if lines else path.stem
    return title, "\n".join(lines)


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    records = []

    for path in sorted(OUTPUT_DIR.rglob("*.docx")):
        rel = path.relative_to(OUTPUT_DIR)
        doc_type = rel.parts[0] if rel.parts else "未知"
        # 目录结构是「素材类型/品类/文件.docx」，因此品类取文件名上一级目录
        category = rel.parts[-2] if len(rel.parts) >= 3 else "通用"
        title, text = docx_to_text(path)
        records.append({
            "doc_id": path.stem,
            "doc_type": doc_type,
            "category": category,
            "source_file": str(rel).replace("\\", "/"),
            "title": title,
            "text": clean_text(text),
        })

    for path in sorted(OUTPUT_DIR.rglob("*.md")):
        rel = path.relative_to(OUTPUT_DIR)
        title, text = md_to_text(path)
        records.append({
            "doc_id": path.stem,
            "doc_type": rel.parts[0] if rel.parts else "未知",
            "category": "通用",
            "source_file": str(rel).replace("\\", "/"),
            "title": title,
            "text": clean_text(text),
        })

    for path in sorted(OUTPUT_DIR.rglob("*.xlsx")):
        rel = path.relative_to(OUTPUT_DIR)
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(values_only=True))
        header = [str(c) if c is not None else "" for c in rows[0]]
        # 逐行成文档：一条 QA / 一条话术 = 一个块。
        # 如果整个表格当成一份文档按长度切，一条 QA 会被切到两个块里，答案就串了。
        for n, r in enumerate(rows[1:], 1):
            if not any(r):
                continue
            pairs = [(header[i], str(v)) for i, v in enumerate(r)
                     if i < len(header) and v is not None and str(v).strip() and header[i]]
            if not pairs:
                continue
            body = "\n".join(f"{k}: {v}" for k, v in pairs)
            title = ""
            for k, v in pairs:
                if k in ("标准问题", "问题", "话术内容"):
                    title = v
                    break
            id_field = ""
            for k, v in pairs:
                if k in ("QA编号", "话术编号"):
                    id_field = v
                    break
            records.append({
                "doc_id": f"{id_field or (path.stem + '-' + str(n))}",
                "doc_type": rel.parts[0] if rel.parts else "未知",
                "category": pairs[0][1] if pairs and pairs[0][0] in ("分类", "大类") else "表格",
                "source_file": f"{str(rel).replace(chr(92), '/')}#行{n + 1}",
                "title": (title or path.stem)[:60],
                "text": clean_text(body),
            })

    out = DATA / "cleaned.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    total_chars = sum(len(r["text"]) for r in records)
    print(f"清洗完成：{len(records)} 份文档 → {out.name}")
    print(f"  总字符数 {total_chars:,}，平均 {total_chars // max(len(records), 1):,} 字/份")
    print(f"  最短 {min(len(r['text']) for r in records):,} 字，最长 {max(len(r['text']) for r in records):,} 字")
    empty = [r["doc_id"] for r in records if len(r["text"]) < 100]
    if empty:
        print(f"  ! 有 {len(empty)} 份内容过短，建议检查：{empty[:5]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
