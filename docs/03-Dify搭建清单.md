# Dify 搭建与排错清单

> 主路径是**导入 DSL**，一条命令的事。手工搭建作为备选，只有在导入失败时才需要。

## 第 0 步：起后端 + 环境自检

```bash
# 窗口 1：起 mock 后端（这个窗口之后会被占住）
cd mock-api
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8000

# 窗口 2：环境自检
python tools/check_env.py
```

自检会告诉你三件事：后端五个场景接口通不通、Dify 能不能访问、知识库 API Key 有没有效。
**全绿再往下走**，否则导入成功了也跑不通。

## 第 1 步：确认模型供应商

你的 Dify 用的是通义千问，配置必须一字不差：

| 项 | 值 |
|---|---|
| 供应商 provider | `langgenius/tongyi/tongyi` |
| 模型名 | `qwen-plus` |
| 插件 | 从 Marketplace 安装 `langgenius/tongyi`（DSL 里已声明依赖，导入时若未安装会提示） |

**模型名和 provider 都严格区分大小写**，一个字母不对就是两个东西。
踩过的真实例子：`DeepSeek-V4-Flash`（首字母大写）和 `deepseek-v4-flash`（全小写）
在接口看来完全不同，前者会被拒绝。

坑的地方在于：接口明明返回的是"模型名不支持"这种很明确的错误，
但 Dify 会把它包装成 `Internal Server Error, please contact support`，
界面上完全看不出真正原因。**遇到 500 先核对拼写，再去翻容器日志。**

## 第 2 步：导入 DSL

浏览器打开 `http://localhost` 登录 → **工作室** → 右上角**创建应用**旁的下拉 →
**导入 DSL 文件** → 把 `dify/after-sales-agent.yml` 拖进去。

导入成功的话，画布上是 **24 个节点**（按数据流顺序）：

| 编号 | 节点 | 类型 | 作用 |
|---|---|---|---|
| 1 | 开始 | start | 对话入口 |
| 2 | 订单号抽取 | code | 抽订单号 + 手机号后四位，抽不到返回空串 |
| 3 | 记录对话 | http-request | 上报本轮消息，后端统计连续负面情绪 |
| 4 | 是否需要升级 | if-else | 连续负面 ≥2 → 直接转人工，**情绪优先于意图** |
| 5 | 意图识别 | question-classifier | 七分类，温度 0 |
| 6 | 查订单与物流 | http-request | GET `/api/order-query` |
| 7 | 生成物流回复 | llm | 把接口数据说成人话 |
| 8 | 回复：物流查询 | answer | 输出 |
| 9 | 退款资格判定 | http-request | POST `/after-sales/eligibility` (refund) |
| 10 | 生成退款方案 | llm | 转述判定结论 |
| 11 | 回复：退款退货 | answer | 输出 |
| 12 | 换货资格判定 | http-request | POST `/after-sales/eligibility` (exchange) |
| 13 | 生成换货方案 | llm | 转述判定结论 |
| 14 | 回复：换货 | answer | 输出 |
| 15 | 知识库检索 | knowledge-retrieval | 向量检索 1330 块（三个库） |
| 16 | 生成咨询回答 | llm | 依据知识库回答 |
| 17 | 回复：通用咨询 | answer | 输出 |
| 18 | 转人工建工单 | http-request | POST `/api/tickets`，自动带坐席交接摘要 |
| 19 | 生成安抚回复 | llm | 共情 + 工单号 + 等待时长 |
| 20 | 清洗回复 | code | 把模型偶尔混进来的英文词替换成中文 |
| 21 | 回复：投诉与转人工 | answer | 输出 |
| 22 | 提交售后单 | http-request | POST `/after-sales/refunds`，**真正花钱的那一步** |
| 23 | 生成提交结果回复 | llm | 退款单号 / 核验提示 / 拒绝原因 |
| 24 | 回复：提交结果 | answer | 输出 |

## 第 3 步：导入后必查三处

DSL 是死的，环境是活的，这三处最容易对不上：

**① 模型是否可选中。** 点开「意图识别」节点，看模型下拉里 `qwen-plus` 有没有橙色
「不兼容」徽章。有的话说明插件没装好或没配 Key，去设置 → 模型供应商里修。

**② 知识库是否选对。** 点开「知识库检索」节点，勾选这三个库（不是一个大库）：

| 知识库 | 装什么 | 文档数 |
|---|---|---|
| 01-政策与FAQ库 | 15 份政策 + 500 条 FAQ | 530 |
| 02-产品手册库 | 300 份产品手册 | 300 |
| 03-流程与话术库 | 200 份 SOP + 300 条话术 | 500 |

**为什么必须拆库**：混在一起时，客户问「退货运费谁承担」，
检索会召回一份砂锅的产品手册——字面太像了。实测类型命中率只有 43%，拆开后 93%。

用这条命令查知识库的真实 id：

```bash
python kb-assets/pipeline/upload_to_dify.py --list
```

**填错 id 不会报错，只是永远检索不到东西**，这是最难排查的一类问题。

**③ 向量模型和检索方式对不对。** 这两项**建库时就要选对，建完改不了**（改要重建库）：

| 项 | 值 | 为什么 |
|---|---|---|
| embedding 模型 | `text-embedding-v3` | **千万别用默认的 `multimodal-embedding-v1`**，那是多模态模型，中文纯文本区分度差。实测用它时「退款多久能到账」的前 4 名全是「价保怎么申请」，分数还挤在 0.810~0.812 |
| 检索方式 | 混合检索 hybrid_search | FAQ 块里原样包含客户问法，关键词一路能直接命中 |
| Rerank | 开启，`qwen3-rerank` | 重排显著改善排序 |
| top_k | 4 | 3 容易漏，5 以上噪声变多 |

**④ 分类器的类别名有没有被动过。** 分类器的**七个**类别名必须与连线一一对应，
改名字会让分支断开。指令原文见 `prompts/classifier_instruction.txt`。

## 第 4 步：跑通五个场景

| 输入 | 预期 |
|---|---|
| 订单 2024091288765 我要退货 | 可退，269 元，问你要不要提交 |
| 订单 2024090511233 我要退货 | 超期，转人工，**不承诺结果** |
| 订单 2024092004455 三天没动了 | 报出真实物流节点和时间 |
| 订单 2024091922331 刚买就降价 50 | 走价保，不按退货处理 |
| 太差了我要投诉 | 真实工单号 + 预计等待 2 分钟 |

五个都对了，再去跑 `tests/cases.jsonl` 的 40 条做批量评测。

## 第 5 步：报错怎么查

| 现象 | 原因 | 处理 |
|---|---|---|
| HTTP 节点连接被拒绝 | 容器里用了 localhost | 换成 `host.docker.internal` |
| 报 SSRF / 内网地址被拦截 | 自部署版有 SSRF 防护代理，默认拒绝一切内网地址 | 见下面「SSRF 拦截怎么修」一节 |
| 模型下拉显示「不兼容」 | 插件未安装或未配 Key | 设置 → 模型供应商，装插件 + 填 Key + 点测试要绿勾 |
| 运行报 500 | 模型名拼写错（最常见） | 核对 provider 与模型名大小写，再翻容器日志 |
| LLM 节点输出空字符串 | 输出额度被思考过程吃光 | 最大输出提到 1200 |
| 知识库检索结果为空 | dataset id 填错 | `--list` 查真实 id |
| 回复里出现字段名 | 提示词没约束住 | 在 SYSTEM 里再强调一次，并把这条记进迭代日志 |
| 中文回复里混进英文单词 | 提示词只说了"共情/礼貌"，没说"用什么语言"，多语言模型会漂移 | 在 SYSTEM 末尾加语言规则，**并点出具体要避免的英文词**（如 frustration、sorry），同时给订单号/工单号开例外 |

**取容器日志**（界面上看不到真正原因时，这里一定有）：

```bash
docker ps --format "{{.Names}}"
docker logs --tail 80 docker-api-1
```

## SSRF 拦截怎么修（自部署必踩）

报错长这样：

```
Access to 'http://host.docker.internal:8000/api/...' was blocked by SSRF protection
(e.g. SSRF_PROXY_ALLOW_PRIVATE_IPS=172.21.0.0/16 to allow 172.21.0.0/16).
```

Dify 默认**拒绝 HTTP 节点访问任何内网地址**——这是防 SSRF 攻击的设计，不是 bug。
而 `host.docker.internal` 解析出来是私有地址，所以必然被拦。

### ⚠️ 报错里那个网段是假的，别照抄

`172.21.0.0/16` 是 Dify 源码里**硬编码的示例文本**
（`api/core/helper/ssrf_proxy.py` 的注释原文写着 `from the bug report`），
跟你机器的实际网段没有任何关系。照抄必然失败——本项目实测踩过这个坑，
真实地址是 `192.168.65.254`（Docker Desktop 的网关），跟示例差得很远。

### 正确步骤：先查真实 IP，再精确放行

**第一步，查出容器里 `host.docker.internal` 解析成什么：**

```bash
cd /d <你的路径>\dify\docker
docker compose exec api python -c "import socket; print(socket.gethostbyname('host.docker.internal'))"
```

**第二步，把那个 IP 写进 `.env`**（注意是 `/32`，只放行这一个地址）：

```
SSRF_PROXY_ALLOW_PRIVATE_IPS=192.168.65.254/32
```

多个网段用**逗号**分隔。不要图省事写 `172.16.0.0/12,10.0.0.0/8,192.168.0.0/16`——
那等于把整个防 SSRF 的墙拆掉，内网所有设备都对 Dify 敞开了，是真实的安全风险。

**第三步，强制重建 ssrf_proxy 容器**（普通 `up -d` 检测不到 `.env` 变化，会显示 `Running`）：

```bash
docker compose up -d --force-recreate ssrf_proxy
```

**第四步，验证 ACL 真的写进去了：**

```bash
docker compose exec ssrf_proxy sh -c "cat /etc/squid/dify_allow_private.conf"
```

应该看到你刚填的那个 IP：

```
acl dify_allowed_private_networks dst 192.168.65.254/32
http_access allow client_localnet dify_allowed_private_networks
```

看到旧网段或空内容，说明容器没重建成功或改错了文件。

### 三个容易卡的细节

1. **改完必须 `--force-recreate`。** 环境变量在容器创建时固化，`docker compose up -d`
   检测不到 `.env` 的变化，输出全是 `Running` 就等于没生效。
2. **变量用逗号分隔**，不是空格或分号——squid 启动脚本会把逗号替换成空格再逐项写入 ACL。
3. **改完要确认容器真的拿到了变量**：
   `docker compose exec ssrf_proxy sh -c "echo $SSRF_PROXY_ALLOW_PRIVATE_IPS"`

## 第 6 步：手工搭建（备选）

只有导入失败时才需要。节点类型和连接关系见本文件第 2 步的表格，
提示词从 `prompts/classifier_instruction.txt` 和 `prompts/reply_prompts.md` 复制。

两个容易出错的细节：

1. **代码节点必须声明 `code_language` 和 `outputs`**，缺一个就导入失败或运行报错。
2. **`{{#节点id.字段#}}` 里的节点 id**：手动加节点时 id 是随机生成的，
   提示词里要替换成你自己的 id（点节点上的变量引用按钮最稳，别手打）。

## 第 7 步：发布

右上角「发布」→ 更新。之后可以：

- 用「探索」页直接对话，录 demo 视频
- 用「嵌入网站」拿 iframe 代码，放进作品集页面
- 用「访问 API」拿 App Key，写脚本批量跑评测集
