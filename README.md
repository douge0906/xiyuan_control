# 掌上锡院 · 控制台（control plane）

GPLv3 · FastAPI · SQLite

给客户端下发内容、提供账号断言与设备登记的服务端。**Android 客户端链路不接触教务密码，不保存学号明文，也不保存课表/成绩/一卡通内容。** Web/PWA 测试版另有有状态代理登录：浏览器会把学号与密码提交给控制端，控制端仅在内存持有 TGT/Cookie（见下文“Web/PWA 测试版代理”）。

- 客户端身份 = 设备（`installation_id` + P-256 密钥对，登记时验签）；`installation_id` 由 `ANDROID_ID` 单向派生
- 会话 = 客户端本地计算的学号伪名；服务端保存 `HMAC-SHA256(ACCOUNT_KEY_SECRET, 伪名)` 和 8 位展示标签，access 15 分钟 / refresh 30 天轮换
- 兼容 1.0.9 及更早客户端：v1 断言会附带原始学号，仅用于服务端重算校验与签名验证，**不写入数据库、不写入日志**；使用 v2 断言的新版客户端只发伪名
- 服务端对 Android 客户端只做：设备登记、会话、内容下发、遥测、统计、风控、审计；另为 Web/PWA 测试版提供有状态代理登录（见下节）

设计文档见 [`fmd/system-design.md`](fmd/system-design.md)。

## 接口

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/healthz` | 无 | 健康检查（设备数 / 用户数） |
| POST | `/api/v1/devices/register` | 签名 | 设备登记，返回设备令牌（幂等） |
| POST | `/api/v1/auth/challenges` | Device | 申请一次性挑战（5 分钟） |
| POST | `/api/v1/auth/assertions` | Device + 签名 | 断言登录，返回 access/refresh；`assertion_version=1` 兼容旧客户端（带学号），`2` 只带伪名 |
| POST | `/api/v1/auth/refresh` | 无 | 刷新并轮换令牌 |
| POST | `/api/v1/auth/revoke` | Device | 撤销本设备全部会话 |
| GET | `/api/v1/config/versions` | 可选 | 两段式探测：只回版本号 |
| GET | `/api/v1/messages` | 可选 | 公告 |
| GET | `/api/v1/school-calendar` | 可选 | 校历 |
| GET | `/api/v1/exam-papers/folders` | 可选 | 试卷目录 |
| POST | `/api/v1/resource-uploads` | Device | 上传教材 PDF / 试卷 + 答案，进入待审队列 |
| GET | `/api/v1/resource-uploads/mine` | Device | 我的上传记录、贡献数与下载量 |
| GET | `/api/v1/resources` | Device | 审核通过的资源，按课程 + 教材分组 |
| GET | `/api/v1/resources/quota` | Device | 当前账号/设备今日下载额度（limit/baseLimit/bonusLimit/bonusPerInvite/invitedCount/used/remaining/scope） |
| GET | `/api/v1/resources/files/{id}/text` | Device | DOCX 正文文本预览（不占下载额度；`.doc` 不支持） |
| POST | `/api/v1/resources/reports` | Device | 反馈文件失效/内容不符/重复/侵权 |
| GET | `/api/v1/resources/files/{id}` | Device | 下载资源；`mode=download` 登录后按账号跨设备累计，未登录按设备累计；基础额度 5 次/天，邀请 1 人加 10 次/天（`limit=baseLimit+bonusLimit`），`mode=preview` 不占额度；已上 EdgeOne/COS 时 307 跳短时签名 URL，≥8 MiB 优先 COS |
| GET | `/api/v1/app/release` | 无 | 最新版本与下载地址（下载地址指向控制端签名入口） |
| GET | `/api/v1/app/download` | 无 | 版本更新 APK 稳定入口；点击时 307 到 EdgeOne 短时签名 URL |
| POST | `/api/v1/telemetry/events` | Device | 遥测（数组，`event_id` 幂等） |
| POST | `/api/v1/feedback` | 可选 Device | 匿名反馈（正文最多 2000 字） |
| GET | `/admin` | Bearer | 管理后台（单页） |
| GET | `/admin/metrics/*` | Bearer | 概览 / 趋势 / 分布 |
| GET/PATCH | `/admin/devices`、`/admin/devices/{id}/status` | Bearer | 设备列表、封禁/恢复 |
| GET/PATCH | `/admin/users`、`/admin/users/{id}/status` | Bearer | 用户列表、停用 |
| GET | `/admin/content` | Bearer | 内容列表与当前版本 |
| GET/PUT | `/admin/content/{name}` | Bearer | 读取 / 保存内容（`app_release` / `messages` / `school_calendar` / `exam_papers`） |
| GET | `/admin/content/{name}/history` | Bearer | 最近 20 份历史备份 |
| POST | `/admin/content/{name}/restore` | Bearer | 从历史备份恢复 |
| GET/PATCH | `/admin/feedback`、`/admin/feedback/{id}/status` | Bearer | 反馈列表、标记已读/已处理 |
| GET/PATCH | `/admin/resources`、`/admin/resources/{id}/status` | Bearer | 资源审核列表、通过/拒绝/下架（`removed`） |
| GET/POST | `/admin/resource-reports`、`/admin/resource-reports/{id}/resolve` | Bearer | 资源反馈列表、处理/忽略/下架 |
| GET | `/admin/resources/traffic` | Bearer | 下载总量与按设备流量 |
| GET | `/admin/resources/files/{id}` | Bearer | 管理端下载文件 |
| GET | `/admin/resources/files/{id}/preview` | Bearer | 管理端内联预览 PDF |
| GET/DELETE | `/admin/risk-events` | Bearer | 风控事件、标记已处理 |
| GET | `/admin/audit` | Bearer | 审计日志 |

错误统一返回：

```json
{"error": {"code": "invalid_signature", "message": "设备签名校验未通过"},
 "request_id": "3f9c2a1b7d4e"}
```

## Web/PWA 测试版代理（有状态）

Android 客户端直连学校系统，不经过下列接口。Web/PWA 测试版通过控制端代理完成 CAS 登录与教务查询：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/campus/ping` | 代理探活 |
| POST | `/api/v1/campus/session/start` | 浏览器提交 `username` / `password`；控制端收到后立即用学校公钥加密，登录完成后清掉密码 |
| POST | `/api/v1/campus/session/submit` | 提交验证码并完成 CAS 登录 |
| POST | `/api/v1/campus/session/fetch` | 以会话内的 TGT/Cookie 代查教务/门户接口 |
| POST | `/api/v1/campus/session/refresh` | 刷新会话内的门户/教务 Cookie |
| POST | `/api/v1/campus/session/ping` | 会话探活 |
| POST | `/api/v1/campus/session/logout` | 清理会话与内存中的 TGT/Cookie |

- 会话只存在内存，按随机 `sid` 索引；`username`、TGT、教务 Cookie、门户 Cookie 保留到登出、过期或进程重启。
- 密码、Cookie、TGT 不落盘、不写入日志；登录完成后只清密码，TGT/Cookie 仍会为后续查询保留。
- 查询内容会经过控制端再返回浏览器。

## 内容维护

两种方式，结果一致：

1. **后台页面**（推荐）：`/admin` → 内容 → 选择版本发布 / 公告 / 校历 / 试卷库 → 编辑保存。
   保存前会做结构校验，旧内容自动备份到 `content/.history/`，并写审计日志。
2. **直接改文件**：`content/` 下四个 JSON，改文件即生效（版本号 = 内容 sha1 前 8 位）。

| 文件 | 用途 |
|---|---|
| `app_release.json` | 最新版本号与下载地址 |
| `messages.json` | 公告 |
| `school_calendar.json` | 校历 |
| `exam_papers.json` | 试卷目录 |

内容版本号无需手工维护：保存后自动变化，客户端用 `/api/v1/config/versions` 比对。

## 备份

```bash
./venv/bin/python tools/backup.py            # 备份一次，保留最近 14 份
./venv/bin/python tools/backup.py --keep 7   # 自定义保留份数
```

cron 示例（每天 03:10）：

```cron
10 3 * * * cd /opt/xiyuan-control && ./venv/bin/python tools/backup.py >> data/backup.log 2>&1
```

备份写入 `data/backup/control-YYYYmmdd-HHMMSS.db`，使用 SQLite 在线备份 API，不在 WAL 写入中途复制文件。

## 运行

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8100
```

首次启动会在 `data/.env` 自动生成 `ADMIN_KEY`、`TOKEN_SECRET` 与 `ACCOUNT_KEY_SECRET`（0600），
多进程同时首次启动时通过 `.env.lock` 串行化，确保所有进程拿到同一个值。
管理后台地址：`/admin`，用 `ADMIN_KEY` 登录。

## 资源中心对象存储（COS + CDN）

默认仍用本地磁盘，**不配也能跑**。要启用对象存储，在 `data/.env` 增加：

```ini
COS_ENABLED=1
COS_SECRET_ID=<腾讯云子账号 SecretId>
COS_SECRET_KEY=<腾讯云子账号 SecretKey>
COS_BUCKET=xiyuanzyzx-1313027026
COS_REGION=ap-nanjing
COS_PREFIX=resources
# 不填则用 COS 默认域名；有自定义 CDN 域名时再覆盖
COS_PUBLIC_BASE=https://xiyuanzyzx-1313027026.cos.ap-nanjing.myqcloud.com

# 超过该字节数的文件直接走 COS 预签名，绕开 EdgeOne 大文件回源边界；
# 默认 8 MiB。设为 0 表示关闭直连，全部交给 EdgeOne。
RESOURCE_COS_DIRECT_THRESHOLD=8388608

# 现有 EdgeOne 静态缓存（与 COS 可同时开启）
EDGE_RESOURCE_BASE=https://static.wxxydouge.icu/static/resources
EDGE_PUBLIC_DIR=/opt/xiyuan-pocket/static/resources
```

行为：

- 上传仍先落本地并立即返回，审核通过后后台同时做两件事：
  1. 把文件镜像到 `EDGE_PUBLIC_DIR`，供 EdgeOne 回源；
  2. 把文件 PUT 到 COS 的 `resources/<uploadId>/<uuid>.<ext>`，写
     `Cache-Control: public, max-age=31536000, immutable`、`x-cos-acl: private`。
- 服务启动不再在 Web 进程里补传历史文件；独立 `python -m app.resource_worker` 消费 `resource_publish_jobs` outbox，
  审核通过/下架只入队，worker 负责 COS 上传、EdgeOne 镜像和失败重试。
- 下载接口先校验设备令牌与每日额度（基础 5 次 + 邀请加成；每成功邀请 1 人 +10 次/天，按邀人账号累计）；登录账号跨设备累计，未登录才按设备累计；
  成功后返回 307：**文件大小达到 `RESOURCE_COS_DIRECT_THRESHOLD` 且 COS 对象存在 → COS 预签名 URL**；
  其余情况 **EdgeOne 镜像存在 → EdgeOne 短时 Token URL**；COS 兜底仍在，最后才回退本地 `FileResponse`。
  大文件走 COS 前的 HEAD 校验与本地/Edge 缺失时的 404 判断共用同一套逻辑。
- 客户端不带 `Authorization` 直连 CDN/COS，设备令牌不会泄漏到 CDN/COS 域名；预览不占下载额度，但会写 `download_events`。
- 下架/拒绝时 worker 删除 EdgeOne 镜像与 COS 对象，并调用 EdgeOne `CreatePurgeTask` 清缓存；删除失败会重试并可在控制台查看。
- 每个文件上传时计算 SHA-256，历史文件由 worker 补齐；可用 `/admin/resources/integrity` 抽样对账。

COS 桶与对象均保持私有读，下载只通过后端签发的短时预签名 URL；
不再依赖对象级匿名读，也不再上传 `x-cos-acl: public-read`。
配置后可跑真实联通检查（会临时上传并删除一个对象）：

```bash
./venv/bin/python tools/check_cos.py --confirm
```

自定义域名请在 COS/DNSPod 用**子域**（如 `res.wxxydouge.icu`）添加 CNAME，
不要占用根域 `wxxydouge.icu`（已有 A 记录）。

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

覆盖 14 个场景：登记验签、时间窗、错误签名、挑战一次性、断言登录、
刷新轮换、内容下发、遥测幂等、统计、封禁→令牌失效、恢复、内容管理、
历史备份、反馈提交与处理。

## 安全边界

- 令牌在库中只存 HMAC；管理密钥只存 `data/.env`
- 只监听 `127.0.0.1`，对外由 Nginx 终止 TLS
- 封禁设备立即撤销令牌与会话；管理写操作全部进审计
- 内容保存有白名单、结构校验、原子替换与历史备份

## 尚未做

- 多管理员 / 权限分级（当前单管理员，够用）
- 管理登录限流与密钥轮换 UI
- 数据库迁移框架（当前用 `CREATE TABLE IF NOT EXISTS` 建表）
- CDK（本阶段不做内容运营）

## 许可证

本项目以 [GNU General Public License v3.0](LICENSE) 发布。
