# minimax-token-plan-monitor

监控 [MiniMax Token Plan](https://platform.minimax.cn/token-plan) 配额用量,定期快照入库,每周自动出一份"5h 窗口用满率"周报。

> 本项目为社区第三方工具,**不隶属 MiniMax 官方**。依赖的是控制台内部接口,非官方公开 API,可能随 MiniMax 调整而变动(见[已知限制](#已知限制))。

## 它解决什么问题

MiniMax 官方控制台只能看**当前剩余百分比**和**近 7 天 token 总数**,没有按周/按窗口的历史明细,你无法回答:

- 我的 5 小时额度到底用没用满?是不是白买了更高档位?
- 每周哪些时段在浪费配额、哪些时段在顶格?

本项目定时把快照落库,再按"每个 5h 窗口的最低剩余%"判断**用满率**,据此建议升档还是降档。

## 30 秒 Quick Start

```bash
# 前置:已有 PostgreSQL,以及一个 MiniMax Token Plan 的 Key(见下)
psql -U postgres -d postgres -c "CREATE DATABASE minimax_token_plan;"
psql -U postgres -d minimax_token_plan -f schema.sql

python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 写入 Key(三选一,见下文 Key 管理)
mkdir -p ~/.config/minimax && chmod 700 ~/.config/minimax
printf '%s' 'YOUR_KEY' > ~/.config/minimax/secret && chmod 600 ~/.config/minimax/secret

# 拉一次快照 + 立即出一份上周周报
python fetch_token_plan.py
python report_token_plan.py --stdout-only
```

## Prerequisites

- **Python 3.11+**
- **PostgreSQL 13+**(本地或服务器均可)
- 一个 **MiniMax Token Plan 订阅 Key**:在 [MiniMax 开放平台](https://platform.minimax.cn/) 的 Token Plan 页面获取(控制台「订阅/付费」处,形如一串 API key,调用 `/v1/token_plan/remains` 用)。
- 纯 Python 标准库 + `psycopg[binary]`,无 Redis、无消息队列、无前端。

## 用到的接口

| 项 | 值 |
|---|---|
| Method | `GET` |
| URL | `https://www.minimax.cn/v1/token_plan/remains` |
| Header | `Authorization: Bearer <YOUR_KEY>` |
| 返回 | `base_resp.status_code == 0` 为成功;`model_remains[]` 内每个模型的 5h / 周窗口剩余百分比与用量计数 |

> 注意:这是控制台内部接口,字段名(`current_interval_remaining_percent` 等)以实际返回为准。

## 架构

```
┌──────────────────────────────┐
│ macOS (开发机)                 │
│  项目源码 / venv              │
│  Key 可放 macOS Keychain      │
└──────────────┬───────────────┘
               │ ssh / git pull
               ▼
┌──────────────────────────────┐
│ 一台常开的 Linux 服务器(可选) │
│  systemd: token-plan-daemon   │
│    → fetch_token_plan_daemon  │
│    → 动态算 5h 重置前 2 分钟   │
│    → curl MiniMax API → PG     │
│  cron(周一 10:00):            │
│    token-plan-weekly.sh       │
│    → fetch + report           │
│  PostgreSQL:                  │
│    db minimax_token_plan      │
│    snapshots / model_usage    │
└──────────────────────────────┘
```

> 只想在自己 Mac 上跑也行:直接 `python fetch_token_plan.py` 配 launchd,或手工跑;不强制上服务器。

## 项目结构

```
.
├── README.md
├── LICENSE                    # MIT
├── requirements.txt          # psycopg[binary]
├── schema.sql                 # PostgreSQL DDL
├── fetch_token_plan.py        # 一次性拉取(cron/手动用)
├── fetch_token_plan_daemon.py # 长跑 daemon(systemd 用)
├── report_token_plan.py       # 周报生成
├── token-plan-weekly.sh       # 每周 wrapper(fetch + report)
├── examples/
│   └── report_sample.md       # 报告样例(脱敏)
└── deploy/
    ├── aliyun_setup.sh        # 一键装到 Linux(PG+venv+systemd+cron)
    ├── token-plan-daemon.service
    └── pg_hba_trust.conf
```

## 安装

### 1. 建库 + 表

```bash
sudo -u postgres psql -c "CREATE DATABASE minimax_token_plan;"
psql -h 127.0.0.1 -U postgres -d minimax_token_plan -f schema.sql
```

### 2. Python 依赖

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. 配置 Key(四选一,优先级从高到低)

1. `--key <KEY>` CLI 参数(仅本地测试);
2. **macOS Keychain**(开发机):
   ```bash
   security add-generic-password -s minimax-token-plan-key -a minimax-token-plan -w '<KEY>'
   ```
3. **secret 文件**(推荐,Linux/服务器):
   ```bash
   mkdir -p ~/.config/minimax && chmod 700 ~/.config/minimax
   printf '%s' '<KEY>' > ~/.config/minimax/secret && chmod 600 ~/.config/minimax/secret
   ```
4. 环境变量 `MINIMAX_TOKEN_PLAN_KEY`(临时调试)。

Key 只在内存中使用,**不写日志、不入库、不进错误信息**。

### 4.(可选)DSN 覆盖

默认 `host=127.0.0.1 port=5432 dbname=minimax_token_plan user=postgres`。自定义:

```bash
export MINIMAX_TOKEN_PLAN_DSN="host=127.0.0.1 port=5432 dbname=minimax_token_plan user=myuser password=mypass"
```

### 5.(可选)一键部署到 Linux 服务器

```bash
git clone <your-repo> /opt/minimax-token-plan-monitor
bash /opt/minimax-token-plan-monitor/deploy/aliyun_setup.sh   # 需 root
```

脚本会:装 PG + Python3.11 → 建库建表 → 建 venv → 拷脚本到 `~/bin/` → 提示输入 Key → 注册 systemd + 每周一 10:00 cron → 跑一次验证。

## 验证

```bash
# 拉一次快照
.venv/bin/python fetch_token_plan.py
# OK  models=2  general:91%week/14%5h, video:100%week/100%5h
#    snapshot_id=2

# 立即出上周周报(打印到终端)
.venv/bin/python report_token_plan.py --stdout-only
```

周报默认写到 `~/Documents/MiniMax/token-plan/reports/report_YYYY-Www.md`;
可用环境变量 `TOKEN_PLAN_REPORT_DIR` 覆盖。

报告样例见 [`examples/report_sample.md`](examples/report_sample.md)。

## 报告怎么读

- **判定**:每个 5h 窗口内 `current_interval_remaining_percent` 的最小值 ≤ 5% → 记一次"用满"。
- **目标**:用满率 ≥ 80% = 配额刚好够;< 50% = 大量浪费,可降档。
- 报告按天拆分"用满窗口 / 总窗口 / 用满率"。

## 关键设计决策

### 为什么用 daemon 而不是固定 cron

5h 窗口的 anchor 是 rolling 的(从你首次使用时刻起每 5h 一个区间),固定 cron 很难对齐。daemon 每次拉取后读返回的 `remains_time`,动态算"下一次重置前 2 分钟"再睡;同时保底每 30 分钟抓一次,防止 rolling 漂移漏窗口。

### 为什么用 PostgreSQL 而不是 SQLite

快照 + JSONB 原始响应 + 聚合查询,PG 更顺;你大概率已有 PG,不引入新依赖。

### 为什么不内置推送(飞书/邮件/webhook)

监控是监控,推送是你的个人集成。保持本仓库单一职责,需要推送自行在 `token-plan-weekly.sh` 末尾加几行(见下)。

```bash
# 邮件
mail -s "Token Plan 周报 $(date +%Y-W%V)" you@example.com < "$LATEST"
# webhook
curl -X POST https://hooks.example.com/notify -H 'Content-Type: text/markdown' --data-binary "@$LATEST"
```

## 已知限制

1. **该接口不返回 token 总数 / cache 命中率**。控制台"近 7 天 N M Token"拿不到,需要另外抓控制台其他接口(如 `/v1/usage/*`)。
2. **周限额视套餐而定**。无限档(如 Plus)周使用量不会被限制,所以报告以 5h 窗口为主维度,不做周 diff。
3. **5h 百分比是按模型分类独立计时的**。控制台那个重置倒计时是某个模型的视图,与 API 单模型百分比是不同维度,不能直接对等。
4. 接口为控制台内部实现,MiniMax 若改字段或加鉴权,本工具需跟进。

## 安全说明

- 默认把 PG 配成 `127.0.0.1 trust`(仅本机),不要对公网开放 PG;
- Key 文件 `chmod 600`、目录 `chmod 700`;
- 本工具不记录任何对话内容,只存配额快照。

## License

MIT
