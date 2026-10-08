"""把分类器指令从 prompts/ 同步进 DSL，避免两边漂移。

    python tools/sync_prompts.py          # 同步（写回 dify/after-sales-agent.yml）
    python tools/sync_prompts.py --check  # 只检查是否一致，不写文件

为什么需要这个脚本：同一段指令在两个地方各存一份——
`prompts/classifier_instruction.txt`（人看的，带使用说明）和
`dify/after-sales-agent.yml` 里分类器节点的 `instruction`（Dify 真正读的）。
手工同步一定会漂：改了 txt 忘了改 yml，Dify 就还在用旧规则，
**而且不报错**——这是最难查的一类问题。

规则：txt 里第一个 `---` 之后是【使用说明】，不进 Dify。

注意：本脚本只同步**分类器指令**。7 个回复节点的提示词
（prompts/reply_prompts.md）目前仍是手工同步，因为它们在 yml 里是
7 段独立的 system/user 文本，结构化提取反而更容易出错。
"""

from __future__ import annotations

import sys
from pathlib import Path

# Windows 控制台是 GBK，指令里有 ⚠️ 这类符号，直接 print 会抛 UnicodeEncodeError。
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent.parent
PROMPT = ROOT / "prompts" / "classifier_instruction.txt"
DSL = ROOT / "dify" / "after-sales-agent.yml"

INSTRUCTION_LINE = "        instruction: |"
BODY_INDENT = " " * 10


def read_prompt() -> list[str]:
    """取 `---` 之前的正文（去掉行尾空格，去掉首尾空行）。"""
    lines: list[str] = []
    for raw in PROMPT.read_text(encoding="utf-8").splitlines():
        if raw.strip() == "---":
            break
        lines.append(raw.rstrip())
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return lines


def locate_block(lines: list[str]) -> tuple[int, int]:
    """返回 DSL 里 instruction 正文的 [起始行, 结束行) —— 0-based。"""
    try:
        head = lines.index(INSTRUCTION_LINE)
    except ValueError:
        raise SystemExit(f"× 在 {DSL.name} 里找不到分类器的 `{INSTRUCTION_LINE.strip()}`，"
                         f"DSL 结构可能被改过")
    end = head + 1
    while end < len(lines) and (lines[end].startswith(BODY_INDENT) or not lines[end].strip()):
        end += 1
    # 结尾的空行不算正文
    while end > head + 1 and not lines[end - 1].strip():
        end -= 1
    return head + 1, end


def current_dsl_body(lines: list[str]) -> list[str]:
    start, end = locate_block(lines)
    out = []
    for ln in lines[start:end]:
        out.append(ln[len(BODY_INDENT):] if ln.startswith(BODY_INDENT) else "")
    return out


def main() -> int:
    check_only = "--check" in sys.argv
    if not PROMPT.exists() or not DSL.exists():
        raise SystemExit("× 找不到 prompts/classifier_instruction.txt 或 dify/after-sales-agent.yml")

    want = read_prompt()
    text = DSL.read_text(encoding="utf-8")
    lines = text.split("\n")
    have = current_dsl_body(lines)

    if have == want:
        print("√ 已经一致，不用同步")
        return 0

    if check_only:
        print(f"× 不一致：txt {len(want)} 行，yml {len(have)} 行")
        for i in range(max(len(want), len(have))):
            a = have[i] if i < len(have) else "<没有这一行>"
            b = want[i] if i < len(want) else "<没有这一行>"
            if a != b:
                print(f"  第 {i + 1} 行起就不一样了：")
                print(f"    yml: {a[:70]}")
                print(f"    txt: {b[:70]}")
                break
        print("\n跑 python tools/sync_prompts.py 同步（然后重新导入 DSL）")
        return 1

    start, end = locate_block(lines)
    body = [f"{BODY_INDENT}{ln}" if ln else "" for ln in want]
    new = lines[:start] + body + [""] + lines[end:]
    DSL.write_text("\n".join(new), encoding="utf-8", newline="\n")
    print(f"√ 已同步 {len(want)} 行指令进 {DSL.name}")
    print("  记得在 Dify 里重新导入 DSL，否则 Dify 用的还是旧指令。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
