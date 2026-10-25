# 智学矩阵 StudyMatrix

> 面向大学生的 **多 Agent 智能复习规划助手** —— 对话式输入需求，自动读取课表空闲时段，按艾宾浩斯遗忘曲线与 SM-2 动态间隔算法生成可执行复习计划，并在过程中自主调用外部工具、跨知识图谱节点出题、记住你的历史学习状态。

`Flask` · `DeepSeek (OpenAI 兼容 SDK)` · `SSE 流式推送` · `Function Calling` · `SQLite` · `原生 HTML/CSS/JS`

---

## 一、项目简介

StudyMatrix 是一个「能自己动手」的学习助理：它不被动问答，而是**把工具调用权交给大模型**，让模型自主决定"该查课表还是该出题还是该排计划"；同时通过**长期记忆（Memory）**、**知识图谱（Knowledge Graph）**与**督学反馈**，给出高度个性化、可执行、可跟踪的学习安排。

**核心能力**

| 能力 | 说明 |
|---|---|
| 🤖 多 Agent 调度 | Router 判定意图 → 由大模型通过 **Function Calling** 自主选择工具（不再写死 if-else） |
| 📅 课表联动 | 读取前端录入的「我的专属课表」，只在真实空闲时段排期；未录入课表时自动走宏观建议兜底 |
| 🧠 艾宾浩斯 + SM-2 | 第 1/2/4/7/15 天间隔；按「记忆自评」动态伸缩（≥4 分 ×1.5 延后，≤2 分 ×0.5 提前并强制重刺） |
| 🕸️ 知识图谱出题 | Tutor Agent 出题时回溯**前置知识点**，产出「跨节点综合应用题」 |
| 💾 长期记忆 Memory | 每次提问 / 打卡 / AI 反馈落库，生成时注入最近 5 条，支持「回忆式」个性化回答 |
| 📣 督学反馈 | 完成率 <50% → 严厉批评 + 25 分钟番茄钟；>80% → 表扬 + 薄弱科目加量 20% |
| 🔔 主动提醒（Proactive） | 快捷键 / 每天早晚时间点 / 任务积累未完成，主动推送提醒（无定时刷屏） |
| 📊 数据看板 | 今日进度环（只统计"今天"任务）+ 本周学习时长柱状图（真实累加）+ 手动调整 |
| 📱 移动端适配 | <1024px 左侧抽屉（75vw + 遮罩）、右侧看板改为底部抽屉，功能与 PC 一致 |

---

## 二、技术栈

**后端**
- Python 3.10+（开发环境 3.12）
- Flask（单文件 `app.py`，轻量路由）
- `openai` SDK（指向 DeepSeek：`base_url="https://api.deepseek.com"`，模型 `deepseek-chat`）
- SSE（`text/event-stream`）流式返回，前端可实时看到 Agent 决策与工具调用
- **SQLite**（Python 内置 `sqlite3`，零依赖持久化）+ `threading.local` 连接 + WAL
- flask-cors、python-dotenv

**前端（无框架、原生）**
- 原生 HTML / CSS / JavaScript（原生 DOM 操作）
- Tailwind CSS（CDN）
- `marked.js`（Markdown 渲染）
- `KaTeX`（LaTeX 数学公式渲染，bootcdn 国内源）
- `localStorage`（会话、打卡、画像、课表、看板手动值等本地状态）

**部署 / 联调**
- Render（`render.yaml`：`gunicorn app:app`）
- 内网穿透：ngrok（固定域名）/ serveo（`ssh -R 80:127.0.0.1:5000`）

---

## 三、目录结构

```
.
├── app.py                  # Flask 主入口（/api/*、系统提示词组装、Agent 编排）
├── requirements.txt
├── .env.example            # 环境变量样例（复制为 .env）
├── render.yaml             # Render 部署配置
├── start_tunnel.ps1        # ngrok 固定域名隧道启动脚本（Windows）
├── agents/                 # 多 Agent / 工具 / 提示词 / 存储
│   ├── loop.py             #   Agent Loop（Function Calling 主循环，SSE 事件）
│   ├── tools_registry.py   #   工具白名单 + JSON Schema + handler
│   ├── executor.py         #   工具执行器（校验 / 超时 / 审计 trace）
│   ├── prompts.py          #   运行时提示词资产（含 EBBINGHAUS / SM-2 / 督学反馈）
│   ├── router.py           #   意图分类（历史遗留，已由 Function Calling 取代）
│   ├── planner.py          #   规则引擎兜底（make_plan）
│   ├── kg.py               #   知识图谱查询层
│   ├── memory.py           #   长期记忆（SQLite）
│   ├── history.py          #   计划历史记忆（SQLite）
│   └── storage.py          #   DAO 层：SQLite 建表 / 查询 / 事务
├── tools/schedule.py       # 课表工具（空闲时段计算）
├── data/                   # 数据（SQLite 由代码初始化；json 为种子/演示数据）
│   ├── kg.json             #   知识图谱（节点 + 前置边）
│   ├── review_tasks.json   #   艾宾浩斯到期复习点（首次启动导入 SQLite）
│   ├── schedule.json       #   演示课表种子
│   └── agent_traces/       #   Agent 审计 trace（可回放）
├── static/
│   ├── index.html          # 单页前端（聊天 + 看板 + 弹窗 + 主动提醒）
│   └── style.css
└── scripts/                # 自检 / 测试脚本
    ├── selfcheck.py
    ├── test_tools.py  test_history.py  test_kg.py  test_sm2.py  test_tutor.py
    └── e2e_history.py  e2e_memory.py  e2e_router.py
```

---

## 四、本地运行指南

### 1) 环境要求
- Python **3.10+**（推荐 3.12）
- Windows PowerShell / macOS / Linux 均可
- 可访问 `api.deepseek.com` 的网络

### 2) 安装依赖
```bash
# 建议使用虚拟环境
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 3) 配置环境变量
```bash
# 复制样例文件
# Windows PowerShell:
Copy-Item .env.example .env
# macOS / Linux:
cp .env.example .env
```
然后编辑 `.env`，填入你的 Key（**详见下一节**）：
```
DEEPSEEK_API_KEY=sk-你的真实Key
```

### 4) 启动服务
```bash
python app.py
```
- 默认监听 `0.0.0.0:5000`
- 浏览器访问：**http://127.0.0.1:5000**
- 局域网/手机访问：`http://<本机内网IP>:5000`

### 5) 首次启动会做什么
- 自动创建 SQLite 数据库 `data/app.db` 及 4 张表（`memories` / `user_history` / `review_tasks` / `agent_traces`）
- 若 `review_tasks` 为空，从 `data/review_tasks.json` 导入种子数据
- 无需手动迁移，重复启动安全（幂等）

### 6) 自检 / 测试（可选）
```bash
python scripts/test_tools.py      # 工具注册 / 参数校验 / 白名单
python scripts/test_history.py    # 历史记忆（SQLite）
python scripts/test_kg.py         # 知识图谱 + 跨节点出题（端到端）
python scripts/test_sm2.py        # SM-2 动态间隔
python scripts/test_tutor.py      # 督学反馈（完成率）
python scripts/e2e_router.py      # Function Calling 自主调用工具（端到端）
```

---

## 五、`DEEPSEEK_API_KEY` 环境变量说明（重要）

本项目的**所有**大模型能力都依赖它，未配置时接口会直接返回错误（不启动模型调用）。

### 1) 如何获取
1. 访问 DeepSeek 开放平台：<https://platform.deepseek.com/>
2. 注册/登录 → 「API Keys」→ 创建新的 API Key（形如 `sk-xxxxxxxx...`）
3. **创建后仅显示一次**，请立即复制保存

### 2) 放置位置（三种方式，任选其一）

**方式 A：本地开发（推荐）** —— 写入项目根目录 `.env`
```
DEEPSEEK_API_KEY=sk-你的真实Key
```
> `.env` 已在 `.gitignore` 中，**绝不会被提交到仓库**。

**方式 B：临时会话变量（PowerShell）**
```powershell
$env:DEEPSEEK_API_KEY = "sk-你的真实Key"
python app.py
```

**方式 C：云平台部署** —— 在平台控制台配置环境变量（不要写进代码）
- `render.yaml` 已声明：
  ```yaml
  envVars:
    - key: DEEPSEEK_API_KEY
      sync: false        # 在 Render 控制台手动填入，不入库
  ```

### 3) 代码读取位置
```python
# app.py
load_dotenv()                                   # 加载 .env
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")  # 各 Agent 模块同样读取该变量
```

### 4) 安全须知
- ❌ **不要**把 Key 硬编码进 `app.py` 或任何前端文件
- ❌ **不要**提交 `.env`、`ngrok.yml`（含隧道 authtoken）等含密钥的文件
- ✅ 泄露后请立刻到平台「吊销并重建」Key
- ✅ 团队协作请只提交 `.env.example`（占位值）

---

## 六、API 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查，返回 `{"project":"StudyMatrix","status":"ok"}` |
| POST | `/api/chat` | 主对话接口（**SSE** 流式）：Agent 自主决策 → 工具调用 → 最终回复 |
| POST | `/api/proactive` | 主动提醒（`manual` / `slot` 早晚时段 / `event` 任务积累） |
| POST | `/api/memory/checkin` | 打卡情况写入长期记忆（前端勾选任务时上报） |
| GET | `/api/memory?user_id=&limit=` | 查看某用户最近记忆（默认 5 条） |
| GET | `/api/kg?subject=&topic=&depth=` | 查询知识图谱：当前知识点 + 前置知识点链 |

**`/api/chat` 请求体（示例）**
```json
{
  "message": "帮我制定本周复习计划",
  "user_id": "u_xxx",
  "profile": { "goal": "期末冲刺", "weak": "高数", "subjects": [{"subject":"高数","examDate":""}] },
  "schedule": "周一 08:00-09:40 高等数学",
  "schedule_rows": [{"day":"周一","start":"08:00","end":"09:40","course":"高等数学"}],
  "progress_data": "今日 3 项已完成 1 项",
  "completion": "2/10",
  "recall_score": 4
}
```

**SSE 事件序列**
```
event: router       data: {"route":"auto","agent":"...","completion":"20%","tutor_mode":"harsh"}
event: agent_step   data: {"type":"think" | "tool" | "fallback", "tool":"get_schedule", "ok":true, ...}
event: final        data: {"reply":"...","intent":"auto","agent":"...","tool":"done"}
event: error        data: {"error":"..."}
```

---

## 七、Function Calling 工具清单

| 工具 | 参数 | 作用 |
|---|---|---|
| `get_schedule` | — | 查询用户课表（前端录入优先，本地 `schedule.json` 兜底） |
| `generate_test` | `subject` `topic` `count` `difficulty` | 出题（Tutor Agent），**基于知识图谱跨节点出综合应用题** |
| `generate_plan` | `weak_subject` `time_limit` `recall_score` | 生成复习计划（艾宾浩斯 + SM-2 + 督学反馈约束） |
| `query_due_reviews` | `days` | 查询到期复习点（艾宾浩斯间隔） |
| `compute_free_slots` | `min_minutes` | 计算本周空闲时段 |
| `get_student_profile` | — | 查询学习画像 |
| `query_knowledge_graph` | `subject` `topic` `depth` | 查询知识图谱前置关联 |

> 工具由 `agents/tools_registry.py` 统一注册（白名单 + JSON Schema），`agents/executor.py` 负责参数校验、超时与审计 trace。

---

## 八、数据存储

**后端 SQLite（`data/app.db`，代码自动建表）**

| 表 | 用途 |
|---|---|
| `memories` | 长期记忆：提问 / 打卡情况 / AI 反馈（按 `user_id` 分桶，保留最近 50 条） |
| `user_history` | 计划历史：科目 / 进度 / 薄弱科目 / 计划摘要（每个用户保留最近 20 条） |
| `review_tasks` | 艾宾浩斯到期复习点（首次启动从 `review_tasks.json` 导入） |
| `agent_traces` | 每轮 Agent 运行审计（工具调用链、耗时），可回放 |

**前端 localStorage（客户端缓存）**
`studymatrix_sessions`（会话）、`studymatrix_taskcheck`（打卡）、`user_profile`（画像）、`user_schedule`（课表）、`studymatrix_user_id`（用户标识）、`studymatrix_lastcheckin`（最近打卡日期）、`studymatrix_dash_override`（看板手动值）等。

---

## 九、部署（可选）

**Render**
`render.yaml` 已就绪：
```yaml
startCommand: gunicorn app:app --host 0.0.0.0 --port $PORT --workers 2 --timeout 120
envVars:
  - key: DEEPSEEK_API_KEY
    sync: false
```
> 注意：Render 免费层磁盘为临时存储，`data/app.db` 重启会重置；生产长期保存请挂持久盘或改用托管数据库。

**本地内网穿透（Windows）**
```powershell
# ngrok 固定域名（需先在 ngrok.com 申请 authtoken）
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
$env:GODEBUG='x509ignoreCRL=1'
ngrok http 5000 --url your-domain.ngrok-free.dev

# 或使用 serveo（免注册，注意转发目标用 127.0.0.1）
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -R 80:127.0.0.1:5000 serveo.net
```

---

## 十、常见问题（FAQ）

**Q1：页面能打开但 AI 不回复？**
检查 `.env` 中的 `DEEPSEEK_API_KEY` 是否有效、额度是否充足；服务端启动时若未读到该变量，接口会返回「服务端未配置 DEEPSEEK_API_KEY」。

**Q2：Markdown 正常但数学公式不渲染？**
KaTeX 使用的是 bootcdn 源（部分网络下 jsdelivr 不可达）。若仍无法加载，可把 `static/index.html` 中 KaTeX 的 3 个 `<link>/<script>` 换成注释里给出的 npmmirror 备选源。

**Q3：端口被占用？**
改 `app.py` 末尾的 `app.run(port=...)`，或释放 5000 端口后重启。

**Q4：数据看板数字不对？**
进度环只统计「今天」的任务（按表格"日期"列与真实日期/星期比对）；本周时长只累加**已勾选**任务的预估时长（优先取时间段差值，如 `09:40–10:05` = 25 分钟，未给时长默认 1.5 小时）。若需人工校正，点看板右上角 ⚙ 手动覆盖。

**Q5：PowerShell 中文乱码？**
执行前设置 `$env:PYTHONIOENCODING='utf-8'`，或用 `chcp 65001` 切换代码页。

---

## License

本项目用于学习与课程演示用途。