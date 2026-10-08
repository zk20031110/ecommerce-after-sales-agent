# GitHub 上传指南（从零到推送成功）

## 文档信息

| 项 | 内容 |
|---|---|
| 适用对象 | 第一次把项目传上 GitHub 的人（本文按本项目实际情况写，命令可直接复制） |
| 当前状态 | 本地仓库已建好、已提交，**只差"建远程仓库 + 推送"** |
| 前置工具 | Git for Windows（本机已装 2.55.0） |

---

## 一、先看现状：已经做完的 vs 还没做的

| 阶段 | 状态 | 说明 |
|---|---|---|
| 安装 Git | ✅ 已完成 | `git --version` → 2.55.0.windows.5 |
| 配置提交身份 | ✅ 已完成 | 全局 `user.name=zk20031110`、`user.email=2250302399@qq.com` |
| 初始化仓库 | ✅ 已完成 | 分支 `main` |
| 检查密钥与大文件 | ✅ 已完成 | 无密钥入库，大文件被 `.gitignore` 挡住 |
| 首次提交 | ✅ 已完成 | 53 个文件、12325 行 |
| **创建 GitHub 远程仓库** | ❌ 待做 | 在网页上点几下 |
| **推送到 GitHub** | ❌ 待做 | 两条命令 |

---

## 二、剩下的三步

### 第 1 步：在 GitHub 上建一个空仓库

1. 登录 [github.com](https://github.com)，右上角 **+** → **New repository**。
2. **Repository name**：`ecommerce-after-sales-agent`
3. **Description**（建议直接复制）：
   `电商售后 AI 客服 Agent（Dify + 通义千问）：意图分流、订单物流查询、退换货资格判定、转人工闭环`
4. **可见性**：选 **Public**。
   面试项目要让面试官**点开就能看**；如果你不想公开，选 Private 也可以，
   但要在简历里注明"仓库私有，可提供访问"。
5. **三个勾一个都不要勾**：`Add a README file`、`Add .gitignore`、`Choose a license`。
   本地已经有这些文件了，勾了会在推送时报冲突，白折腾一轮。
6. 点 **Create repository**。

创建完页面会显示一段"快速开始"，你只需要里面的仓库地址，
形如 `https://github.com/你的用户名/ecommerce-after-sales-agent.git`。

### 第 2 步：把本地仓库和远程仓库关联起来

```powershell
cd C:\Users\22503\Desktop\test
git remote add origin https://github.com/<你的用户名>/ecommerce-after-sales-agent.git
git remote -v
```

`git remote -v` 应该输出两行（fetch / push），地址是你的仓库地址。
命令里的 `<你的用户名>` 记得换成你自己的，尖括号不要带。

### 第 3 步：推送

```powershell
git push -u origin main
```

- 第一次推送会**弹出浏览器窗口**要求登录 GitHub，登录一次以后就不用再登了
  （这是 Git Credential Manager，不是让你输密码）。
- 看到 `branch 'main' set up to track 'origin/main'` 就是成功了。
- 然后刷新刚才那个仓库页面，文件应该都在了。

---

## 三、上传前检查清单（我已按这个验过一遍）

复制下面这几条到 PowerShell 里跑，任何一条不符合预期就先别推。

```powershell
cd C:\Users\22503\Desktop\test

# 1. 工作区应该是干净的（所有文件都已提交）
git status --short

# 2. 提交的文件数应该是 53
git ls-files | Measure-Object -Line

# 3. 这几类东西绝对不能出现（.env、数据库、素材产物）
git ls-files | Where-Object { $_ -match '\.env$|\.db$|kb-assets/data|kb-assets/output|__pycache__' }

# 4. 有没有超过 5MB 的文件（GitHub 单文件上限 100MB，但大文件就该排查）
Get-ChildItem -Recurse -File |
  Where-Object { $_.FullName -notmatch '\\\.git\\' -and $_.Length -gt 5MB } |
  Select-Object @{n='大小MB';e={[math]::Round($_.Length/1MB,1)}}, FullName
```

预期结果：第 1 条**没有任何输出**；第 2 条输出 `Lines: 53`；第 3 条**没有任何输出**；
第 4 条会列出 `kb-assets\data\tickets.db`（52MB）等文件——
它们在 `.gitignore` 里，**不会**被推上去，这里列出来只是让你确认它们确实存在。

### 为什么 `.env` 不会泄露

`.gitignore` 里第一行就是 `.env`。但有一条铁律：

> **永远不要用 `git add -f .env` 强行加进去。**

`-f` 会忽略 `.gitignore`，一旦提交，密钥就永久留在 Git 历史里了。

---

## 四、推送成功后要做的四件事（很多人漏掉）

1. **填 About 和 Topics**：仓库首页右上角齿轮 → Description 填上面的简介；
   Topics 填 `dify`、`ai-agent`、`llm`、`customer-service`、`product-management`。
   Topics 是面试官搜索和扫一眼时的第一信息。
2. **检查 README 在网页上渲染正常**：表格、目录树、代码块都要看一遍（本地看和网页看不一样）。
3. **点开 `docs/` 里任意一个中文文件**，确认没有乱码。
4. **确认提交记录**：Commits 应该是 1，Contributors 是你自己。

---

## 五、日常改动怎么提交（三行口诀）

```powershell
git status --short          # 1. 看改了哪些文件
git add -A                  # 2. 全部暂存
git commit -m "docs: 补充成本测算"   # 3. 提交
git push                    # 4. 推上去
```

### 提交信息怎么写（面试官真的会翻 commit 历史）

| 前缀 | 用在什么时候 | 例子 |
|---|---|---|
| `feat:` | 新增功能 | `feat: 退款提交接口支持按 SKU 部分退款` |
| `fix:` | 修 bug | `fix: 坐席回复后客户端轮询不到新消息` |
| `docs:` | 改文档 | `docs: 补充上线与灰度方案` |
| `test:` | 改测试 | `test: 新增 40 条口语问法用例` |
| `refactor:` | 重构但不改行为 | `refactor: 把资格判定从提示词挪到后端接口` |

不要写 `update`、`修改一下`、`fix bug` —— 这种历史等于没有历史。

---

## 六、常见报错对照表

| 报错 / 现象 | 通俗解释 | 怎么解决 |
|---|---|---|
| `fatal: not a git repository` | 你不在项目目录里执行命令 | 先 `cd C:\Users\22503\Desktop\test` |
| `error: remote origin already exists` | 已经关联过远程地址了 | 改地址：`git remote set-url origin <新地址>` |
| `! [rejected] main -> main (fetch first)` | 远程仓库有本地没有的提交（通常是建仓库时勾了 README） | `git pull --rebase origin main` 后再 `git push` |
| `Support for password authentication was removed` | 你在用账号密码推送，GitHub 早就不允许了 | 让它走浏览器登录（Git Credential Manager），或创建 Personal Access Token 当密码 |
| `RPC failed` / `file is 105 MB; exceeds GitHub's limit` | 有大文件被提交了 | 先从暂存区移除（`git rm --cached <文件>`），补进 `.gitignore`，再重新提交 |
| `git status` 里中文文件名显示成一堆 `\346\234\200` | Git 默认把非 ASCII 文件名转义显示，只是**显示**问题 | `git config --global core.quotepath false` |
| `detected dubious ownership` | 仓库目录属于另一个 Windows 账号，Git 出于安全拒绝操作 | `git config --global --add safe.directory C:/Users/22503/Desktop/test` |

---

## 七、密钥体检（万一以后手滑提交了）

**改代码把密钥删掉是没用的**，它还在历史提交里，别人 clone 下来照样能翻到。正确处置顺序：

1. 立刻去模型 / Dify 平台**吊销那把 Key，重新生成**（这是唯一能真正止损的动作）；
2. 新 Key 只写进 `.env`（已被忽略）；
3. 再清理 Git 历史（`git filter-repo` 或 BFG 工具），最后强推。

所以最省事的做法永远是：**第一次推之前就检查清楚**。

> 本项目当前状态：仓库里没有任何明文 Key，`.env` 与两个大素材目录都在 `.gitignore` 里。

---

## 八、两个慎用命令（知道就行，别轻易执行）

| 命令 | 后果 |
|---|---|
| `Remove-Item -Recurse -Force .git` | 删掉整个版本历史。**提交记录会全部消失**，且不可恢复。只在"我想彻底重新开始"时才用 |
| `git push --force` | 用本地历史**覆盖**远程历史。多人协作时会把别人的提交冲掉；只有在自己独占的分支上才用 |

