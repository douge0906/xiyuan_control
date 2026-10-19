"""隐私政策页面（公开可访问，供 App 备案与用户查阅）。"""

PRIVACY_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>掌上锡院 · 隐私政策</title>
<style>
  :root{--ink:#1F2937;--sub:#6B7280;--line:#E8E8EE;--bg:#F7F7FA}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.85 system-ui,-apple-system,"Segoe UI","PingFang SC",sans-serif}
  .wrap{max-width:760px;margin:0 auto;padding:40px 22px 72px}
  .card{background:#fff;border:1px solid var(--line);border-radius:20px;padding:36px 34px}
  h1{font-size:24px;margin:0 0 6px;letter-spacing:-.3px}
  .meta{color:var(--sub);font-size:13px;margin-bottom:26px}
  h2{font-size:16px;margin:30px 0 10px;letter-spacing:-.1px}
  p,li{color:#374151}
  ul{padding-left:20px}
  li{margin:4px 0}
  table{width:100%;border-collapse:collapse;margin:12px 0;font-size:14px}
  th,td{border:1px solid var(--line);padding:9px 11px;text-align:left;vertical-align:top}
  th{background:#FAFAFC;color:var(--sub);font-weight:600}
  code{background:#F4F4F8;padding:1px 5px;border-radius:5px;font-size:13px}
  .note{background:#FBFAFF;border:1px solid #E7E1FA;border-radius:12px;padding:12px 14px;margin:14px 0}
  footer{margin-top:34px;padding-top:18px;border-top:1px solid var(--line);color:var(--sub);font-size:13px}
  a{color:#5B3FC4}
  @media(max-width:600px){.card{padding:24px 18px}}
</style></head>
<body><div class="wrap"><div class="card">

<h1>掌上锡院 · 隐私政策</h1>
<div class="meta">生效日期：2026 年 10 月 19 日　|　版本：v1.0</div>

<p>本文说明「掌上锡院」（以下简称"本应用"）如何处理你的信息。开发者：掌上锡院个人开发者。
本应用是非官方校园工具，与无锡学院无隶属关系。</p>

<div class="note">
<b>一句话总结：</b>你的账号密码、课表、成绩、待办都只保存在你自己的手机上；
服务器只记录"哪台设备、什么版本、什么时候访问过"，用来下发校历公告和统计使用情况，
<b>不保存学号明文、不保存姓名、不保存任何成绩或课表内容</b>。
</div>

<h2>一、本应用如何登录</h2>
<p>本应用使用学校统一身份认证（学号 + 密码）登录。登录请求由你的手机<b>直接</b>发往学校系统，
账号密码保存在本机的加密存储中，用于后续查询课表、成绩等。开发者无法获取你的密码。</p>

<h2>二、服务器会收到哪些信息</h2>
<p>只有在你<b>完成登录后</b>，本应用才会连接开发者自建的内容服务器（<code>con.wxxydouge.icu</code>，
位于中国境内）。未登录时不会发出任何请求。服务器收到的信息如下：</p>

<table>
  <tr><th>类别</th><th>具体字段</th><th>用途</th></tr>
  <tr>
    <td>设备标识</td>
    <td>随机生成的安装标识、设备公钥、平台（Android）、App 版本、服务端生成的设备编号</td>
    <td>识别"还是同一台设备"，用于内容下发与去重统计；不涉及手机号、IMEI、Android ID</td>
  </tr>
  <tr>
    <td>账号标识</td>
    <td>学号的 SHA-256 哈希、由学号派生的固定伪名</td>
    <td>区分不同用户、限制异常登录；<b>哈希不可逆，服务器不保存学号明文</b></td>
  </tr>
  <tr>
    <td>使用记录</td>
    <td>访问时间、请求次数、少量匿名事件（如启动、登录成功）</td>
    <td>统计日活/周活/月活，判断版本分布，发现异常访问</td>
  </tr>
  <tr>
    <td>网络日志</td>
    <td>IP 地址、访问时间、请求路径、浏览器标识（由 Web 服务器记录）</td>
    <td>安全审计与故障排查，30 天后自动清理</td>
  </tr>
</table>

<h2>三、服务器不会收到哪些信息</h2>
<ul>
  <li>你的统一认证密码、Cookie、登录票据</li>
  <li>课表、成绩、考试、教材、一卡通余额等教务数据的内容</li>
  <li>待办事项、日历内容、设置项</li>
  <li>通讯录、短信、位置、相册、麦克风、摄像头数据</li>
  <li>手机号、身份证号、学号明文、姓名</li>
</ul>

<h2>四、App 申请的权限及用途</h2>
<table>
  <tr><th>权限</th><th>用途</th></tr>
  <tr><td>访问网络 / 网络状态</td><td>连接学校系统与内容服务器</td></tr>
  <tr><td>发送通知</td><td>上课提醒、待办提醒（本地通知，由你的手机发出）</td></tr>
  <tr><td>振动</td><td>通知到达时提醒</td></tr>
  <tr><td>开机自启 / 精确闹钟 / 前台服务</td><td>保证上课提醒在重启后仍能按时触发</td></tr>
  <tr><td>悬浮通知 / 全屏通知</td><td>上课提醒在锁屏时可见</td></tr>
</table>
<p>以上权限都可拒绝，拒绝后对应功能不可用，但不影响课表、成绩等基础功能。</p>

<h2>五、信息如何保存与保留</h2>
<ul>
  <li>服务器数据存放在中国境内的云服务器，数据库为 SQLite</li>
  <li>匿名事件保留 90 天；访问日志保留 30 天；设备与用户记录在你申请删除前保留</li>
  <li>本机数据保存在手机的应用私有目录，卸载本应用即随之删除</li>
</ul>

<h2>六、是否共享给第三方</h2>
<p><b>不共享。</b>本应用未集成任何广告、统计或推送 SDK，也不会将信息出售、交换或提供给任何第三方。
服务器仅使用 Let's Encrypt 提供的 HTTPS 证书，该服务不接触你的数据。</p>

<h2>七、你的权利</h2>
<ul>
  <li><b>删除</b>：在 App 内退出登录会立即撤销服务器上的会话；如需删除设备与账号记录，
      可通过下方联系方式提出，我们会在 7 个工作日内处理</li>
  <li><b>停止收集</b>：不登录即可完全不产生任何服务器请求；卸载应用即停止一切上报</li>
  <li><b>了解</b>：本应用为开源项目，客户端代码可公开查阅，服务器行为与本政策一致</li>
</ul>

<h2>八、未成年人</h2>
<p>本应用面向高校学生。若你未满 18 周岁，请在监护人知晓并同意后使用。</p>

<h2>九、政策更新</h2>
<p>政策变更时，本页面会更新生效日期与版本号；涉及重大变更会在 App 内提示。继续使用即表示接受更新后的政策。</p>

<h2>十、联系方式</h2>
<p>隐私相关问题、数据删除请求，请通过项目仓库提交：
<a href="https://github.com/douge0906/xiyuan_pocket/issues">github.com/douge0906/xiyuan_pocket/issues</a></p>

<footer>
开发者：掌上锡院个人开发者　|　本应用为非官方工具，与无锡学院无隶属关系<br>
本政策最后更新：2026 年 10 月 19 日
</footer>

</div></div></body></html>"""
