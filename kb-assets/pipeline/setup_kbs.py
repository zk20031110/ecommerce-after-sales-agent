"""按素材类型创建 3 个知识库，解决检索串味。

    cd kb-assets
    python pipeline/setup_kbs.py            # 创建（已存在的会跳过）
    python pipeline/setup_kbs.py --list     # 只看现有知识库

为什么拆库：一套知识库里混装手册、政策、SOP 时，客户问"退货运费谁承担"，
向量检索可能命中一份砂锅的产品手册——因为它们字面上真的很像。
拆成三个库后，政策分支只查政策库，从源头杜绝串味，比调参数有效得多。

建完把 3 个知识库 id 写进 data/kb_ids.json，供 upload_to_dify.py --split 使用。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
IDS_FILE = DATA / "kb_ids.json"

# 拆分方案：每个库放哪些素材类型
PLAN = [
    {
        "name": "01-政策与FAQ库",
        "description": "退换货政策、时效、运费、价保，以及 500 条高频问答。政策类问题只查这个库。",
        "types": ["03-退换货政策", "04-FAQ清单"],
    },
    {
        "name": "02-产品手册库",
        "description": "300 份产品手册，覆盖全部 SKU 的规格、材质、保养、保修。商品参数类问题只查这个库。",
        "types": ["01-产品手册"],
    },
    {
        "name": "03-流程与话术库",
        "description": "200 份 SOP 标准流程与 300 条客服话术模板。流程、时限、升级条件类问题只查这个库。",
        "types": ["02-SOP标准流程", "05-客服话术库"],
    },
]

# 向量模型：必须用文本专用的 text-embedding-v3。
# 踩过的坑：Dify 默认选了工作区的 multimodal-embedding-v1（多模态模型），
# 它对纯中文文本的语义区分度很差——实测「退款多久能到账」查不到正确的那条，
# 前 4 名全是「价保怎么申请」，而且分数挤在 0.810~0.812 几乎一样。
EMBEDDING_MODEL = "text-embedding-v3"
EMBEDDING_PROVIDER = "langgenius/tongyi/tongyi"

# 检索配置：混合检索（关键词 + 向量）配 Rerank。
# 我们的 FAQ 块里原样包含客户问法，关键词一路能直接命中；
# 纯语义检索在高度同质化的模板文本上容易失效。
RETRIEVAL_MODEL = {
    "search_method": "hybrid_search",
    "reranking_enable": True,
    "reranking_mode": "reranking_model",
    "reranking_model": {
        "reranking_provider_name": EMBEDDING_PROVIDER,
        "reranking_model_name": "qwen3-rerank",
    },
    "top_k": 4,
    "score_threshold_enabled": False,
    "score_threshold": None,
}


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


def api(base: str, key: str, path: str, payload: dict | None = None, method: str = "GET") -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{base.rstrip('/')}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode("utf-8")
        # DELETE 接口返回 204 空响应，直接 json.loads 会抛 "Expecting value"
        return json.loads(body) if body.strip() else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.getenv("DIFY_BASE", "http://localhost/v1"))
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--recreate", action="store_true",
                    help="先删掉已有的这三个知识库再重建（会清空里面的文档，谨慎）")
    args = ap.parse_args()

    load_env()
    key = os.getenv("DIFY_DATASET_KEY", "").strip()
    if not key:
        print("× 没读到 DIFY_DATASET_KEY（写在项目根目录 .env 里）")
        return 1

    base = args.base or os.getenv("DIFY_BASE", "http://localhost/v1")
    existing = {d["name"]: d for d in api(base, key, "/datasets?limit=100").get("data", [])}

    if args.list:
        print("现有知识库：")
        for name, d in existing.items():
            print(f"  · {name}  id={d.get('id')}  文档数={d.get('document_count')}")
        return 0

    mapping: dict[str, dict] = {}
    if args.recreate:
        for item in PLAN:
            d = existing.get(item["name"])
            if not d:
                continue
            try:
                api(base, key, f"/datasets/{d['id']}", method="DELETE")
                print(f"  - 已删除：{item['name']}")
                existing.pop(item["name"], None)
            except Exception as e:  # noqa: BLE001
                print(f"  × 删除失败：{item['name']}  {e}")

    print("创建/核对知识库：")
    for item in PLAN:
        if item["name"] in existing:
            d = existing[item["name"]]
            print(f"  = 已存在：{item['name']}  id={d['id']}")
        else:
            try:
                payload = {
                    "name": item["name"],
                    "description": item["description"],
                    "indexing_technique": "high_quality",
                    "embedding_model": EMBEDDING_MODEL,
                    "embedding_model_provider": EMBEDDING_PROVIDER,
                    "permission": "only_me",
                    "retrieval_model": RETRIEVAL_MODEL,
                }
                try:
                    res = api(base, key, "/datasets", payload=payload, method="POST")
                except Exception:
                    # 有些版本不接受 retrieval_model，退回只带向量模型的写法
                    payload.pop("retrieval_model", None)
                    res = api(base, key, "/datasets", payload=payload, method="POST")
                d = res if "id" in res else res.get("data", {})
                print(f"  + 已创建：{item['name']}  id={d.get('id')}  "
                      f"向量模型={d.get('embedding_model')}")
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "ignore")[:200]
                print(f"  × 创建失败：{item['name']}  HTTP {e.code} {body}")
                print("    → 可以在 Dify 界面手动建，然后把 id 填进 data/kb_ids.json")
                continue
            except Exception as e:  # noqa: BLE001
                print(f"  × 创建失败：{item['name']}  {e}")
                continue
        mapping[item["name"]] = {"id": d.get("id"), "types": item["types"],
                                 "description": item["description"]}

    DATA.mkdir(parents=True, exist_ok=True)
    IDS_FILE.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n映射写入 {IDS_FILE.name}：")
    for name, v in mapping.items():
        print(f"  {name}  →  {', '.join(v['types'])}")

    print("\n下一步：把这些素材分别传进对应的库")
    print("  python pipeline/upload_to_dify.py --split --limit 30   # 先小批量验证")
    print("  python pipeline/upload_to_dify.py --split              # 全量（约 1330 个块，8 分钟）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
