# 部署指南（遗忘刺客 Agent / StudyMatrix）

> 目标：10/28 提交前跑通「可访问演示 URL」。提供三条路径，按条件任选其一。
> 推荐优先级：**华为云 ECS + Docker**（同时覆盖「码道/华为云」叙事）> Render > 本地 + 内网穿透。

---

## 一、华为云 ECS + Docker（推荐，参赛叙事最契合）

```bash
# 0) 前置：华为云 ECS（Ubuntu 22.04，安全组放行 5000 端口）、已装 Docker
#    （ECS 控制台 → 安全组 → 入方向规则 → TCP 5000，源 0.0.0.0/0）

# 1) 上传代码（本地 PowerShell）
git archive -o sm.tar HEAD            # 或直接 scp 整个目录
scp sm.tar root@<ECS公网IP>:/opt/

# 2) 服务器上解压并配置 Key
ssh root@<ECS公网IP>
cd /opt && mkdir -p studymatrix && tar -xf sm.tar -C studymatrix && cd studymatrix
cp .env.example .env && vim .env      # 填入真实 DEEPSEEK_API_KEY

# 3) 一键构建启动（docker-compose 自动读 .env 同名变量）
docker compose up -d --build

# 4) 验证
curl http://127.0.0.1:5000/api/health   # → {"project":"StudyMatrix","status":"ok"}
# 浏览器访问 http://<ECS公网IP>:5000
```

**数据持久化**：SQLite 在 `sm_data` 卷中，容器重建不丢（knowledge_point / plan_item / quiz_log 全在库内）。

**Nginx 反代（可选，绑域名 + 80 端口）**：
```nginx
server {
    listen 80;
    server_name your-domain.com;
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header Connection "";        # SSE 必需：关闭缓冲
        proxy_buffering off;                   # SSE 必需
        proxy_read_timeout 300s;               # 长连接防断
    }
}
```

---

## 二、Render（免服务器，最快上线）

`render.yaml` 已就绪（gunicorn 2 workers + timeout 120）：
1. 推代码到 GitHub → Render 控制台 New → Web Service → 连接仓库；
2. 环境变量手动填 `DEEPSEEK_API_KEY`（`sync: false`，不入库）；
3. 免费层注意：磁盘临时存储，重启 `data/app.db` 重置——演示够用，长期数据用方案一/三。

---

## 三、本地 + 内网穿透（决赛现场备份方案）

```powershell
# Windows PowerShell（项目自带脚本参考 start_tunnel.ps1）
python app.py                                   # 本地 5000

# ngrok 固定域名（需 authtoken）
ngrok http 5000 --url your-domain.ngrok-free.dev

# 或 serveo（免注册）
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -R 80:127.0.0.1:5000 serveo.net
```

**断网预案**：现场演示前 `git clone` 本地副本 + 已缓存的 `.env`，AI 功能不可用时演示
课表录入 / 批量导入 / 看板（纯前端）+ `agent_traces` 回放（data/agent_traces/*.json）。

---

## 四、环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | ✅ | DeepSeek 开放平台 API Key（platform.deepseek.com） |
| `TZ` | 建议 | Docker 部署设 `Asia/Shanghai`（主动提醒 08:00/20:00 时段判定） |

---

## 五、部署后自检清单

```bash
curl http://<URL>/api/health                 # 健康检查
curl "http://<URL>/api/review/targets?user_id=demo"   # 今日必刺清单（SM-2 落库链路）
curl "http://<URL>/api/plan/completion?user_id=demo"  # 本周完成率（SQL 统计链路）
```

前端检查：课表批量导入（粘贴「周一 08:00-09:40 高等数学」）→ 看板 ECharts 图表渲染 → AI 对话（需 Key）。
