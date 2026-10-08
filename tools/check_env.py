"""环境自检：确认跑通 Agent 需要的三样东西都在。

    python tools/check_env.py

检查项：
    1. mock 售后后端是否在跑，五个业务场景接口是否正常
    2. Dify 是否可访问、知识库 API Key 是否有效
    3. Dify 容器能否访问到 mock 后端（给出验证命令，这一步只能在容器里验）
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def get(url: str, headers: dict | None = None, timeout: int = 8):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "ignore")


def main() -> int:
    load_env()
    ok = True

    print("[1] mock 售后后端")
    base = os.getenv("MOCK_API_BASE", "http://127.0.0.1:8000").rstrip("/")
    key = os.getenv("MOCK_API_KEY", "dev-local-key")
    try:
        status, body = get(f"{base}/health")
        data = json.loads(body)
        print(f"    √ {base}/health  HTTP {status}  订单数 {data.get('orders')}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"    × 连不上：{e}")
        print("      → 在另一个窗口跑：cd mock-api && python -m uvicorn main:app --host 0.0.0.0 --port 8000")
        print()

    if ok:
        print("[2] 五个业务场景")
        cases = [
            ("查订单", "GET", f"{base}/api/orders/2024091288765", None,
             lambda d: d["status"] == "已签收"),
            ("物流异常", "GET", f"{base}/api/orders/2024092004455", None,
             lambda d: d["logistics_abnormal"] is True),
            ("退款资格", "POST", f"{base}/api/after-sales/eligibility",
             {"order_no": "2024091288765", "request_type": "refund", "reason": "不合适"},
             lambda d: d["eligible"] is True and d["refund_amount"] == 268.0),
            ("超期转人工", "POST", f"{base}/api/after-sales/eligibility",
             {"order_no": "2024090511233", "request_type": "refund", "reason": "想退"},
             lambda d: d["requires_human"] is True),
            ("建工单", "POST", f"{base}/api/tickets",
             {"order_no": "2024090511233", "intent": "complaint", "summary": "不满", "sentiment": "negative"},
             lambda d: str(d.get("ticket_id", "")).startswith("TK")),
        ]
        for name, method, url, payload, check in cases:
            try:
                if method == "GET":
                    status, body = get(url, {"X-API-Key": key})
                else:
                    req = urllib.request.Request(
                        url, data=json.dumps(payload).encode(), method="POST",
                        headers={"X-API-Key": key, "Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        body = resp.read().decode("utf-8", "ignore")
                data = json.loads(body)
                good = check(data)
                print(f"    {'√' if good else '×'} {name}")
                ok = ok and good
            except Exception as e:  # noqa: BLE001
                print(f"    × {name}：{e}")
                ok = False

    print("[3] Dify 与知识库 API")
    dify = os.getenv("DIFY_BASE", "http://localhost/v1").rstrip("/")
    dkey = os.getenv("DIFY_DATASET_KEY", "").strip()
    try:
        status, _ = get(dify.replace("/v1", "/"))
        print(f"    √ Dify 可访问（HTTP {status}）")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"    × Dify 连不上：{e}")
    if not dkey:
        print("    ! 没配置 DIFY_DATASET_KEY，素材上传会失败")
    else:
        try:
            status, body = get(f"{dify}/datasets?limit=10",
                               {"Authorization": f"Bearer {dkey}"})
            data = json.loads(body)
            kbs = data.get("data", [])
            print(f"    √ 知识库 API 可用，共 {len(kbs)} 个知识库：")
            for kb in kbs:
                print(f"        · {kb.get('name')}  id={kb.get('id')}  文档数={kb.get('document_count')}")
            if not kbs:
                print("        （空的，先在 Dify 界面建一个知识库）")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"    × 知识库 API 调用失败：{e}")

    print("[4] 容器网络（只能用下面这条命令验，宿主机上解析不了 host.docker.internal 是正常的）")
    print("    docker exec -it docker-api-1 curl -s http://host.docker.internal:8000/health")
    print("    返回 {\"ok\":true,...} 就说明 HTTP 节点能通。")

    print()
    print("√ 环境自检通过，可以去 Dify 里导入了。" if ok else "× 有检查项未通过，见上面的 × 提示。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
