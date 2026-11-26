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
  footer a{color:#5B3FC4;text-decoration:none}
  footer a:hover{text-decoration:underline}
  a{color:#5B3FC4}
  @media(max-width:600px){.card{padding:24px 18px}}
</style></head>
<body><div class="wrap"><div class="card">

<h1>掌上锡院 · 隐私政策</h1>
<div class="meta">生效日期：2026 年 11 月 20 日　|　版本：v1.1</div>

<p>本文说明「掌上锡院」（以下简称"本应用"）如何处理你的信息。开发者：掌上锡院个人开发者。
本应用是非官方校园工具，与无锡学院无隶属关系。</p>

<div class="note">
<b>一句话总结（Android 客户端）：</b>你的账号密码、课表、成绩、待办都只保存在你自己的手机上；
服务器只保存账号伪名的不可枚举 HMAC、设备安装标识和访问日志，用于登录校验、内容下发和额度统计。
<b>服务器不保存学号明文和真实姓名（只保存你自愿填写的显示名）、不保存任何成绩或课表内容</b>；使用 v2 断言的新版 Android 客户端也不再上传原始学号。
Web/PWA 测试版不走这条直连链路，另见第十一节。
</div>

<h2>一、本应用如何登录</h2>
<p><b>Android 客户端</b>使用学校统一身份认证（学号 + 密码）登录。登录请求由你的手机<b>直接</b>发往学校系统，
账号密码保存在本机的加密存储中，用于后续查询课表、成绩等。开发者无法获取你的密码。</p>
<p>登录成功后，App 会用设备私钥对一次性挑战签名，向控制端换取账号会话。断言里只包含
由学号在本地计算的 SHA-256 伪名；<b>使用 v2 断言的新版客户端不再包含原始学号</b>。使用 v1 断言的历史客户端会附带学号供服务端
重算校验，服务端只用它完成这一次校验，不写入数据库、不写入日志。</p>
<p>以上为 Android 客户端的登录链路；Web/PWA 代理登录见第十一节。</p>

<h2>二、服务器会收到哪些信息</h2>
<p>App 启动后会连接开发者自建的控制端（<code>con.wxxydouge.icu</code>，
位于中国境内）做设备登记与版本检查；在你完成学校统一认证登录后，才会发送学号伪名、
建立账号会话并使用公告、校历、资源库和额度功能。无论是否登录，控制端都会收到下表中的
设备标识、平台/版本与网络日志；<b>学号伪名和显示名只在登录后发送</b>。服务器收到的信息如下：</p>

<table>
  <tr><th>类别</th><th>具体字段</th><th>用途</th></tr>
  <tr>
    <td>设备标识</td>
    <td>由 <code>ANDROID_ID</code> 单向派生的安装标识（<code>xiyuan-</code> + SHA-256 前 16 字节）、
        设备公钥、平台、App 版本、服务端生成的设备编号</td>
    <td>识别"还是同一台设备"，用于内容下发、防滥用与去重统计；不使用手机号、IMEI、MAC 地址</td>
  </tr>
  <tr>
    <td>账号标识（登录后）</td>
    <td>客户端本地计算的学号伪名；服务端保存其 <code>HMAC-SHA256(服务端密钥, 伪名)</code>
        及 8 位展示标签</td>
    <td>区分不同用户、限制异常登录、维护额度与邀请关系；<b>数据库不保存可枚举的学号哈希，也不保存完整伪名</b></td>
  </tr>
  <tr>
    <td>显示名（登录后）</td>
    <td>你主动填写的昵称，可为空</td>
    <td>资源库等界面展示</td>
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

<div class="note">
<b>关于设备标识：</b><code>ANDROID_ID</code> 由系统按"应用签名 + 用户 + 设备"生成，
卸载重装通常不变，恢复出厂设置或更换设备后会变化。它不是 IMEI、MEID、MAC 地址或手机号。
本应用不采集上述硬件标识，也不采集通讯录、短信、精确位置、相册、麦克风或摄像头数据。
</div>

<h2>三、Android 客户端不会发送给控制端的信息</h2>
<ul>
  <li>你的统一认证密码、Cookie、登录票据</li>
  <li>课表、成绩、考试、教材、一卡通余额等教务数据的内容</li>
  <li>待办事项、日历内容、设置项</li>
  <li>通讯录、短信、位置、相册、麦克风、摄像头数据</li>
  <li>手机号、身份证号、学号明文、真实姓名（你主动填写的显示名除外）</li>
</ul>
<p><b>例外：</b>Web/PWA 测试版通过控制端有状态代理登录，会向控制端提交学号、密码并持有 Cookie/TGT，详见第十一节；上述“不会发送”仅适用于 Android 客户端。</p>

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
<p>控制端全站使用 HTTPS；账号键使用独立服务端密钥做 HMAC，数据库不保存完整伪名。
校内教务、宿舍等由学校提供的接口中，部分仍使用 HTTP，且仅能在校园网访问，这些请求不经过控制端。</p>

<h2>六、是否共享给第三方</h2>
<p>本应用未集成任何广告、统计或推送 SDK，也不会将信息出售、交换或提供给任何第三方。
基础设施仅使用以下服务，且均不接触你的账号密码或教务数据：</p>
<ul>
  <li><b>腾讯云 COS / EdgeOne</b>：资源中心的学习资料存储在中国境内的 COS，并通过 EdgeOne 加速分发；
      仅涉及文件本身和下载请求，不涉及账号伪名或设备标识。相关域名包括
      <code>con.wxxydouge.icu</code>（控制端）、<code>api.wxxydouge.icu</code>（接口与静态回源）、
      <code>static.wxxydouge.icu</code> 与 <code>down.wxxydouge.icu</code>（安装包/资源下载）。</li>
  <li><b>Let's Encrypt</b>：为控制端提供 HTTPS 证书，不接触业务数据。</li>
</ul>

<h2>七、你的权利</h2>
<ul>
  <li><b>删除</b>：在 App 内退出登录会立即撤销服务器上的会话；如需删除设备与账号记录，
      可通过下方联系方式提出，我们会在 15 个工作日内处理</li>
  <li><b>停止收集</b>：不登录不会发送学号伪名、显示名或任何教务数据；
      控制端仍会收到设备登记、版本检查所需的安装标识与网络日志。卸载应用即停止一切上报</li>
  <li><b>了解</b>：本应用为开源项目，客户端代码可公开查阅，服务器行为与本政策一致</li>
</ul>

<h2>八、未成年人</h2>
<p>本应用面向高校学生。若你未满 18 周岁，请在监护人知晓并同意后使用。</p>

<h2>九、政策更新</h2>
<p>政策变更时，本页面会更新生效日期与版本号；涉及重大变更会在 App 内提示。继续使用即表示接受更新后的政策。</p>

<h2>十、联系方式</h2>
<p>隐私相关问题、数据删除请求，请通过项目仓库提交：
<a href="https://github.com/douge0906/xiyuan_pocket/issues">github.com/douge0906/xiyuan_pocket/issues</a></p>

<h2>十一、Web/PWA 测试版说明</h2>
<p>Web/PWA 版本不属于 Android 客户端链路，也不在本页前述“密码不出设备”“控制端不接收 Cookie/查询内容”的表述范围内。该版本通过控制端有状态代理完成学校统一认证登录：</p>
<ul>
  <li>浏览器会把<b>学号与密码</b>通过 HTTPS 提交给控制端；</li>
  <li>控制端收到后立即用学校公钥加密，仅在待提交验证码阶段持有加密结果；登录完成后清掉密码；</li>
  <li>控制端在内存中持有 TGT、教务 Cookie、门户 Cookie，用于代你向学校系统发起查询；</li>
  <li>密码、Cookie、TGT 不落盘、不写入日志，登出、会话过期或控制端进程重启后失效；</li>
  <li>教务/门户查询内容会经过控制端，再由控制端返回浏览器。</li>
</ul>
<p>若 Web/PWA 对外提供，必须另行展示针对 Web 的完整隐私说明，明确上述数据处理方式；Android 客户端不经过这条代理链路。</p>

<footer>
开发者：掌上锡院个人开发者　|　本应用为非官方工具，与无锡学院无隶属关系<br>
备案号：<a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener noreferrer">苏ICP备2026052960号-1</a><br>
本政策最后更新：2026 年 11 月 20 日
</footer>

</div></div></body></html>"""
