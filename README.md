# 掌上锡院 · 控制台（control plane）

给客户端下发内容、做匿名设备统计的服务端。**不碰教务凭据、不存个人数据。**

- 客户端身份 = 匿名设备（`installation_id` + P-256 密钥对，登记时验签）
- 会话 = OA 登录后的学号哈希 + 固定伪名，access 15 分钟 / refresh 30 天轮换
- 服务端只做：设备登记、会话、内容下发、遥测、统计、风控、审计

设计文档见 [`fmd/system-design.md`](fmd/system-design.md)。

## 接口

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/healthz` | 无 | 健康检查（设备数 / 用户数） |
| POST | `/api/v1/devices/register` | 签名 | 设备登记，返回设备令牌（幂等） |
| POST | `/api/v1/auth/challenges` | Device | 申请一次性挑战（5 分钟） |
| POST | `/api/v1/auth/assertions` | Device + 签名 | 断言登录，返回 access/refresh |
| POST | `/api/v1/auth/refresh` | 无 | 刷新并轮换令牌 |
| POST | `/api/v1/auth/revoke` | Device | 撤销本设备全部会话 |
| GET | `/api/v1/config/versions` | 可选 | 两段式探测：只回版本号 |
| GET | `/api/v1/messages` | 可选 | 公告 |
| GET | `/api/v1/school-calendar` | 可选 | 校历 |
| GET | `/api/v1/exam-papers/folders` | 可选 | 试卷目录 |
| GET | `/api/v1/app/release` | 无 | 最新版本与下载地址 |
| POST | `/api/v1/telemetry/events` | Device | 遥测（数组，`event_id` 幂等） |
| GET | `/admin` | Bearer | 管理后台（单页） |
| GET | `/admin/metrics/*` | Bearer | 概览 / 趋势 / 分布 |
| GET/PATCH | `/admin/devices`、`/admin/devices/{id}/status` | Bearer | 设备列表、封禁/恢复 |
| GET/PATCH | `/admin/users`、`/admin/users/{id}/status` | Bearer | 用户列表、停用 |
| GET/DELETE | `/admin/risk-events` | Bearer | 风控事件、标记已处理 |
| GET | `/admin/audit` | Bearer | 审计日志 |

错误统一返回：

```json
{"error": {"code": "invalid_signature", "message": "设备签名校验未通过"},
 "request_id": "3f9c2a1b7d4e"}
```

## 内容维护

`content/` 下四个 JSON，改文件即生效（版本号 = 内容 sha1 前 8 位）：

| 文件 | 用途 |
|---|---|
| `app_release.json` | 最新版本号与下载地址 |
| `messages.json` | 公告 |
| `school_calendar.json` | 校历 |
| `exam_papers.json` | 试卷目录 |

## 运行

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8100
```

首次启动会在 `data/.env` 自动生成 `ADMIN_KEY` 与 `TOKEN_SECRET`（0600），
管理后台地址：`/admin`，用 `ADMIN_KEY` 登录。

## 部署

```bash
chmod +x deploy.sh && bash deploy.sh
```

脚本会：装依赖 → 同步代码到 `/opt/xiyuan-control`（**保留 data/**）→ venv →
写 systemd → 启动 → 打印管理密钥。

Nginx 反代见 `nginx-control.conf.example`（`con.wxxydouge.icu → 127.0.0.1:8100`）。

## 自测

```bash
# 服务起在 8124 后
python tools/smoke.py http://127.0.0.1:8124 <ADMIN_KEY>
```

覆盖 12 个场景：登记验签、时间窗、错误签名、挑战一次性、断言登录、
刷新轮换、内容下发、遥测幂等、统计、封禁→令牌失效、恢复。

## 安全边界

- 令牌在库中只存 HMAC；管理密钥只存 `data/.env`
- 只监听 `127.0.0.1`，对外由 Nginx 终止 TLS
- 封禁设备立即撤销令牌与会话；管理写操作全部进审计

## 尚未做

- 多管理员 / 权限分级（当前单管理员，够用）
- 内容在线编辑（现在直接改 JSON 文件）
- 题库与 CDK（本阶段不做内容运营）
