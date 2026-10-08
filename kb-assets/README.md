# 知识库素材工厂（Knowledge Base Assets）

为售后客服 Agent 生产知识库素材，覆盖「生成 → 清洗 → 切块 → 向量化 → 入库 → 抽检」全链路。

> ⚠️ 全部素材为**合成数据**，用于验证检索与引用链路，不可作为对外承诺发布。

## 产出规模

| 素材 | 数量 | 格式 | 是否入库 |
|---|---|---|---|
| 产品手册 | 300（一 SKU 一份） | DOCX | ✅ |
| SOP 标准流程 | 200（售前/售后/投诉/物流 各 50） | DOCX | ✅ |
| 退换货政策 | 15（12 品类 + 3 通用） | DOCX + Markdown | ✅ |
| FAQ 清单 | 500 条 | XLSX | ✅ |
| 客服话术库 | 300 条 | XLSX | ✅ |
| 历史工单 | 150,000 条 | SQLite | ❌ 只挖掘 |
| 订单/物流实时数据 | — | API | ❌ 实时调用 |

实测：1330 份文档 → 1330 个块 → 约 25.4 万 tokens（FAQ 与话术库逐条成块后的结果）。

## 快速开始

```bash
cd kb-assets

# 1. 生成种子数据（300 SKU / 200 SOP 场景 / 500 FAQ）
python gen/make_seed_data.py

# 2. 生成文档
python gen/product_manuals.py        # 300 份产品手册，约 20 秒
python gen/sop_docs.py               # 200 份 SOP
python gen/policy_docs.py            # 15 份政策（DOCX + MD）
python gen/faq_excel.py              # FAQ Excel
python gen/scripts_library.py        # 话术库 Excel
python gen/tickets_db.py             # 15 万条工单

# 3. 清洗与切块
python pipeline/clean.py
python pipeline/chunk.py

# 4. 工单挖掘（不向量化，只挖候选）
python pipeline/mine_tickets.py

# 5. 入库 Dify
python pipeline/upload_to_dify.py --list                # 先看知识库 id
python pipeline/upload_to_dify.py --dataset-id <ID> --limit 30   # 小批量验证
python pipeline/upload_to_dify.py --dataset-id <ID>              # 全量

# 6. 审计报告
python pipeline/audit_report.py
```

## 目录结构

```
kb-assets/
├── gen/                    生成器
│   ├── templates.py        品类字典、SOP 场景、FAQ 种子（唯一内容源）
│   ├── answers.py          答案模板库
│   ├── docx_util.py        Word 公共封装（中文字体双属性设置）
│   ├── make_seed_data.py   三张主表
│   ├── product_manuals.py  产品手册
│   ├── sop_docs.py         SOP
│   ├── policy_docs.py      政策（DOCX + MD）
│   ├── faq_excel.py        FAQ Excel
│   ├── scripts_library.py  话术库 Excel
│   └── tickets_db.py       工单库
├── pipeline/               流水线
│   ├── clean.py            抽文本 + 术语统一
│   ├── chunk.py            语义切块 + 重叠
│   ├── mine_tickets.py     工单挖掘
│   ├── upload_to_dify.py   批量入库（断点续传 / 重试 / 限流）
│   └── audit_report.py     审计报告
├── data/                   中间产物（CSV / JSONL / SQLite）
├── output/                 最终素材（515 DOCX + 15 MD + 2 XLSX）
└── tests/
    └── retrieval_cases.jsonl   30 条检索抽检用例
```

## 三个关键设计（面试可讲）

**1. 单一内容源，跨文档口径一致。**
300 份手册、200 份 SOP、500 条 FAQ 全部从 `data/sku_master.csv` 派生。
同一个商品的材质、尺码、保修期在各文档里是同一个值。
如果各生成器各写各的，知识库就会出现自相矛盾的条目，
模型检索到哪条都"有依据"，这是合成数据最隐蔽的坑。

**2. 切块策略按素材类型区分。**
FAQ 和话术库**逐条成块**（一条 QA 一个块），因为按长度切会把一条 QA 切成两半，答案就串了；
长文档按语义块合并 + 10% 重叠。切块报告见 `data/chunk_stats.md`。

**3. 边界清楚：什么进知识库，什么不进。**

| | 进知识库 | 原因 |
|---|---|---|
| 产品手册 / SOP / 政策 / FAQ / 话术 | ✅ | 内容相对稳定，需要被检索引用 |
| 历史工单 | ❌ | 含客户隐私和临时状态，向量化后会被当成政策依据 |
| 订单 / 物流 | ❌ | 实时变化，必须走 API 现查，否则 AI 会拿旧数据回答 |

工单的正确用法是 `pipeline/mine_tickets.py`：挖掘高频问题 → 人工确认 → 补进 FAQ。
实测 15 万条工单挖出 1561 个不同问题，其中 1400 个知识库未覆盖。

## 入库前必读

1. **先小批量验证**：`--limit 30` 跑通再全量，避免反复重建索引重复计费。
2. **断点续传**：已上传的块记在 `data/uploaded.jsonl`，重跑自动跳过。
3. **知识库 id 要填对**：用 `--list` 查真实 id。填错不会报错，只是检索不到东西，
   这是最难排查的一类问题。
4. **Embedding 模型**：知识库的向量化模型在 Dify 知识库设置里配置，
   建议用 qwen 系列的 embedding，与你的模型供应商保持一致。
