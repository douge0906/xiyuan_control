# 掌上锡院控制台 · 系统设计

> 参考同类项目（掌上徐工控制台）的成熟做法，按本项目需要裁剪。
> **只借鉴设计，不复制代码**（对方为 GPLv3，本项目为 GPLv3）。

## 1. 定位与边界

控制台只为「内容下发」和「匿名设备统计」而存在。

- **不碰教务凭据**：OA 登录始终在客户端完成，服务端只接收登录后的学号与固定伪名，
  不读取、不转发、不保存密码 / Cookie / Ticket
- **不影响主流程**：控制台不可用时，登录、课表、待办、成绩全部照常；
  只影响「新内容下发」与「统计」
- **不存个人数据**：只有学号哈希、伪名、设备公钥、时间戳

## 2. 架构

```
Flutter 客户端
  ├─ 直连学校系统（CAS / 正方 / 融合门户）
  └─ 提交：安装标识、设备签名、学号、伪名
          │
          ▼
FastAPI 单体
  ├─ api.py      公开接口（设备/会话/内容/遥测）
  ├─ admin.py    管理端 + 内嵌单页后台
  ├─ security.py 令牌 HMAC / ECDSA 验签 / 规范化文本
  ├─ stats.py    统计查询
  └─ db.py       SQLite（WAL）
          │
          ▼
     data/control.db
```

新增功能沿用 `api → store` 两层；不拆微服务、不引入 Redis 或消息队列。

## 3. 身份与会话

### 3.1 设备

客户端每次安装生成 `installation_id` 与 P-256（secp256r1）密钥对，登记时签名：

```
xiyuan-control-device
installation_id
public_key
platform
app_version
created_at
```

服务端校验签名与 **±5 分钟时间窗**，生成 `device_serial` 与随机 `device_token`。
数据库只保存**令牌的 HMAC** 与公钥，不保存私钥；同一安装标识换密钥会被拒绝（409）。

### 3.2 登录

客户端先完成 OA 登录，再申请一次性 challenge（5 分钟有效），签名：

```
xiyuan-control-login
challenge_id
challenge
device_serial
student_id
student_alias
display_name
asserted_at
```

服务端验签 + 校验 challenge 未用过 + 校验伪名 = `SHA-256("xiyuan-control|student|" + 学号)`，
然后按**学号哈希**查找或创建用户，绑定设备并建会话。

- 同一学号可多设备登录
- access token 15 分钟、refresh token 30 天，**刷新时轮换**
- 令牌在库中只存 HMAC

## 4. 内容下发

`content/*.json` 四个文件，版本号 = 文件内容 sha1 前 8 位：

| 文件 | 用途 |
|---|---|
| `app_release.json` | 最新版本与下载地址 |
| `messages.json` | 公告 |
| `school_calendar.json` | 校历 |
| `exam_papers.json` | 试卷目录 |

客户端先 `GET /api/v1/config/versions` 拿版本号，与本地缓存比对，**变了才下载**。
改 JSON 即自动变版本，不需要手工维护。

## 5. 统计、风控与审计

- 任何鉴权请求都会写 `device_days(device_id, day)`，DAU/WAU/MAU 按日历日去重
- 遥测事件 `event_id` 幂等，原始事件保留 90 天
- 风控两类：10 分钟内同设备挑战 > 5 次（`login_burst`）、
  近 30 天同账号设备 ≥ 3（`too_many_devices`）
- 封禁设备会**立即撤销其令牌与全部会话**；`audit_logs` 记录管理写操作

## 6. 管理端

- 单管理员：`data/.env` 的 `ADMIN_KEY`，请求头 `Authorization: Bearer <key>`
- 后台页面 `/admin` 本身不含密钥，使用时输入一次，存 `sessionStorage`
- 指标：用户/设备总量、新增、DAU/WAU/MAU、事件量、30 天趋势、版本/平台分布
- 列表：设备（可封禁/恢复）、用户（可停用）、风控事件（可清理）、审计日志

## 7. 配置与数据

只读运行目录 `data/.env`，首次启动自动生成 `ADMIN_KEY` 与 `TOKEN_SECRET`，权限 0600。

| 项 | 默认 | 说明 |
|---|---|---|
| 监听 | `127.0.0.1:8100` | 对外由 Nginx 终止 TLS |
| 数据库 | `data/control.db` | SQLite WAL |
| 内容目录 | `content/` | 四个 JSON |
| 日志 | journald | `journalctl -u xiyuan-control` |

## 8. 验收标准

1. 未登录客户端：**零请求**（门禁）
2. 错误签名 / 超时签名：400，且错误码明确
3. challenge 复用：400 `challenge_used`
4. 刷新令牌轮换后旧 token：401
5. 封禁设备：令牌立即失效，重新登记返回 403
6. 控制台宕机：客户端功能不受影响
