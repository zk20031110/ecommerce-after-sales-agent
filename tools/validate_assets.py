"""资产自检：评测集是否规范、DSL 里的 Python 代码能不能编译。

改完提示词或 DSL 之后跑一次，避免低级错误带进去：
    python tools/validate_assets.py

YAML 校验需要 pyyaml（Dify 本身依赖它，通常已装）。没有就只跳过 DSL 那一段。
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 与问题分类器的六个类别保持一致（改这里也要同步 prompts/classifier_instruction.txt）
CLASSES = {"物流查询", "退款退货", "换货", "通用咨询", "投诉情绪", "转人工", "确认提交"}

errors: list[str] = []
warnings: list[str] = []


def check_cases() -> None:
    print("[1] 校验意图评测集")
    all_got: set[str] = set()
    for fname, label in (("cases.jsonl", "标准问法"), ("cases_colloquial.jsonl", "口语问法")):
        path = ROOT / "tests" / fname
        if not path.exists():
            warnings.append(f"缺少 {fname}")
            continue
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        seen: set[str] = set()
        for i, line in enumerate(lines, 1):
            try:
                case = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"{fname} 第 {i} 行不是合法 JSON：{e}")
                continue
            cid = case.get("id")
            if not cid:
                errors.append(f"{fname} 第 {i} 行缺少 id")
                continue
            if cid in seen:
                errors.append(f"{fname} id 重复：{cid}")
            seen.add(cid)
            cls = case.get("expect_class")
            if cls not in CLASSES:
                errors.append(f"{cid} 的 expect_class 不在分类器枚举内：{cls}")
            if not case.get("input"):
                errors.append(f"{cid} 缺少 input")
        got = {json.loads(l).get("expect_class") for l in lines}
        all_got |= got
        print(f"    {label}（{fname}）：{len(lines)} 条，覆盖 {len(got & CLASSES)}/{len(CLASSES)} 类")
    missing = CLASSES - all_got
    if missing:
        warnings.append(f"评测集没覆盖这些类别：{sorted(missing)}")


def check_one_dsl(path: Path, yaml) -> None:
    """对单个 DSL 做结构校验：连线、分类出口、代码节点、变量引用。"""
    rel = path.name
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        errors.append(f"{rel} 不是合法 YAML：{e}")
        return

    graph = doc.get("workflow", {}).get("graph", {})
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    ids = {n.get("id") for n in nodes}
    print(f"    {rel}: {len(nodes)} 节点 / {len(edges)} 连线")

    # 1) 连线两端必须存在
    for e in edges:
        if e.get("source") not in ids:
            errors.append(f"{rel} 连线 source 不存在：{e.get('source')}")
        if e.get("target") not in ids:
            errors.append(f"{rel} 连线 target 不存在：{e.get('target')}")

    # 2) 节点自身校验
    for n in nodes:
        data = n.get("data", {})
        ntype = data.get("type")
        title = data.get("title")

        if ntype == "code":
            code = data.get("code", "")
            try:
                ast.parse(code)
            except SyntaxError as e:
                errors.append(f"{rel} 代码节点「{title}」语法错误：{e}")
            if "def main(" not in code:
                errors.append(f"{rel} 代码节点「{title}」缺少 main 函数")
            if not data.get("code_language"):
                errors.append(f"{rel} 代码节点「{title}」缺少 code_language（Dify 必需）")
            if not data.get("outputs"):
                errors.append(f"{rel} 代码节点「{title}」缺少 outputs 声明（Dify 必需）")

        elif ntype == "question-classifier":
            declared = {c.get("id") for c in data.get("classes", [])}
            used = {e.get("sourceHandle") for e in edges if e.get("source") == n.get("id")}
            for miss in sorted(declared - used):
                warnings.append(f"{rel} 分类器「{title}」的类别 {miss} 没有接任何节点")
            if used - declared:
                errors.append(f"{rel} 分类器「{title}」有连到不存在类别的出口：{used - declared}")
            m = data.get("model", {})
            if not m.get("provider") or not m.get("name"):
                warnings.append(f"{rel} 分类器「{title}」模型留空，导入后需在界面里选择")

        elif ntype == "http-request":
            if not data.get("url"):
                errors.append(f"{rel} HTTP 节点「{title}」没写 URL")
            for key in ("method", "authorization", "body", "timeout"):
                if key not in data:
                    warnings.append(f"{rel} HTTP 节点「{title}」缺少字段 {key}")

        elif ntype == "llm":
            m = data.get("model", {})
            if not m.get("provider") or not m.get("name"):
                warnings.append(f"{rel} LLM 节点「{title}」模型留空，导入后需在界面里选择")
            if not data.get("prompt_template"):
                errors.append(f"{rel} LLM 节点「{title}」没有提示词")

    # 3) 变量引用必须指向存在的节点
    text = path.read_text(encoding="utf-8")
    for m in re.finditer(r"\{\{#([A-Za-z0-9_]+)\.", text):
        ref = m.group(1)
        if ref.isdigit() and ref not in ids:
            errors.append(f"{rel} 引用了不存在的节点 id：{ref}")


def check_dsl() -> None:
    print("[2] 校验 dify/ 下的 DSL 文件")
    try:
        import yaml  # type: ignore
    except ImportError:
        warnings.append("没装 pyyaml，跳过 DSL 校验（pip install pyyaml）")
        return

    for path in sorted((ROOT / "dify").glob("*.yml")):
        check_one_dsl(path, yaml)


def check_prompts() -> None:
    print("[3] 检查提示词与素材文件是否齐全")
    for rel in ("prompts/classifier_instruction.txt", "prompts/reply_prompts.md",
                "prompts/handoff.md"):
        p = ROOT / rel
        if not p.exists():
            errors.append(f"缺少 {rel}")
        elif p.stat().st_size < 500:
            errors.append(f"{rel} 内容过短，可能是空文件")
    for rel in ("kb-assets/output/03-退换货政策/通用售后总则.docx",
                "kb-assets/output/04-FAQ清单/FAQ清单_500条.xlsx"):
        if not (ROOT / rel).exists():
            warnings.append(f"缺少素材 {rel}（跑 kb-assets 里的生成脚本可重建）")


def main() -> int:
    check_cases()
    check_dsl()
    check_prompts()
    print()
    for w in warnings:
        print(f"提醒：{w}")
    if errors:
        print("\n发现问题：")
        for e in errors:
            print(f"  × {e}")
        return 1
    print("√ 全部检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
