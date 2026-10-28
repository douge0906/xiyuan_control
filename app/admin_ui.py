"""管理后台的单页界面（无 CDN 依赖，纯 HTML/CSS/JS）。

布局：左侧固定侧边栏 + 右侧内容区；窄屏时侧边栏折成顶部横向导航。
"""

ADMIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>掌上锡院 · 控制台</title>
<style>
  :root{
    --ink:#1F2937; --sub:#6B7280; --line:#E8E8EE; --bg:#F6F7FB;
    --accent:#6C4FD9; --accent-weak:#F2EEFF; --warn:#DC2626; --warn-weak:#FEF2F2;
    --ok:#0F6E56; --ok-weak:#ECFDF5;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;font:14px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;
       background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
  button{font:inherit;cursor:pointer}

  /* ---------- 登录 ---------- */
  .login-wrap{min-height:100%;display:grid;place-items:center;padding:24px}
  .login{width:100%;max-width:380px;background:#fff;border:1px solid var(--line);
         border-radius:20px;padding:26px 24px;box-shadow:0 10px 40px rgba(24,24,40,.06)}
  .login .logo{width:40px;height:40px;border-radius:12px;background:var(--ink);
               display:grid;place-items:center;margin-bottom:14px}
  .login h2{font-size:17px;margin:0 0 4px;font-weight:650}
  .login p{margin:0 0 16px}
  .field{width:100%;padding:11px 13px;border:1px solid var(--line);border-radius:11px;
         font:inherit;background:#FAFAFC;outline:none}
  .field:focus{border-color:#C9BDF5;background:#fff;box-shadow:0 0 0 3px #F2EEFF}
  .btn{border:1px solid var(--line);background:#fff;border-radius:11px;padding:9px 14px}
  .btn:hover{background:#FAFAFC}
  .btn.primary{background:var(--ink);border-color:var(--ink);color:#fff;width:100%;margin-top:12px}
  .btn.primary:hover{opacity:.92}
  .btn.ghost{border-color:transparent;color:var(--sub)}
  .err{color:var(--warn);font-size:12px;margin-top:10px;min-height:18px}

  /* ---------- 外壳 ---------- */
  .shell{display:flex;min-height:100%}
  .side{width:236px;flex:0 0 236px;background:#fff;border-right:1px solid var(--line);
        display:flex;flex-direction:column;padding:18px 14px;gap:6px;
        position:sticky;top:0;height:100vh}
  .brand{display:flex;align-items:center;gap:10px;padding:4px 8px 16px}
  .brand .mark{width:30px;height:30px;border-radius:9px;background:var(--ink);
               display:grid;place-items:center;flex:none}
  .brand b{font-size:14px;font-weight:650;letter-spacing:.2px}
  .brand span{display:block;font-size:11px;color:var(--sub);font-weight:400}
  nav{display:flex;flex-direction:column;gap:2px}
  nav a{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:10px;
        color:var(--sub);text-decoration:none;font-size:13.5px;cursor:pointer;
        transition:background .12s,color .12s}
  nav a:hover{background:#F7F7FA;color:var(--ink)}
  nav a.on{background:var(--accent-weak);color:#5B3FC4;font-weight:600}
  nav a svg{width:16px;height:16px;flex:none;opacity:.9}
  .side-foot{margin-top:auto;display:flex;flex-direction:column;gap:8px;
             padding:12px 10px 0;border-top:1px solid var(--line)}
  .side-foot .who{font-size:11.5px;color:var(--sub);line-height:1.5}

  /* ---------- 主区 ---------- */
  .main{flex:1;min-width:0;padding:22px 26px 56px}
  .topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px;gap:12px}
  .topbar h1{font-size:19px;margin:0;font-weight:650;letter-spacing:-.2px}
  .topbar .sub{font-size:12px;color:var(--sub);margin-top:2px}
  .actions{display:flex;gap:8px}

  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:12px;margin-bottom:16px}
  .card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:14px 16px}
  .card .l{font-size:11.5px;color:var(--sub);display:flex;justify-content:space-between}
  .card .n{font-size:26px;font-weight:660;letter-spacing:-.6px;margin-top:6px;font-variant-numeric:tabular-nums}
  .card .h{font-size:11px;color:var(--sub);margin-top:2px}
  .card.accent{background:linear-gradient(180deg,#FBFAFF,#fff)}

  .panel{background:#fff;border:1px solid var(--line);border-radius:16px;padding:16px;margin-bottom:14px}
  .panel .hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
  .panel h3{font-size:12.5px;color:var(--sub);margin:0;font-weight:600;letter-spacing:.3px}
  .cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .chart{display:flex;align-items:flex-end;gap:4px;height:132px;padding-top:4px}
  .col{flex:1;display:flex;align-items:flex-end;gap:2px;height:100%}
  .bar{flex:1;background:var(--accent);opacity:.85;border-radius:4px 4px 2px 2px;min-width:5px}
  .bar.alt{background:#185FA5;opacity:.45}
  .legend{display:flex;gap:14px;font-size:11.5px;color:var(--sub);margin-top:10px}
  .dot{display:inline-block;width:8px;height:8px;border-radius:3px;margin-right:5px}
  .row{display:grid;grid-template-columns:112px 1fr 44px;gap:10px;align-items:center;margin:7px 0}
  .row .k{font-size:12px;color:var(--sub);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .track{background:#F2F1F7;height:7px;border-radius:4px;overflow:hidden}
  .track i{display:block;height:100%;background:var(--accent);opacity:.65;border-radius:4px}
  .row .v{text-align:right;font-size:12px;font-variant-numeric:tabular-nums;color:var(--sub)}

  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:9px 10px;border-bottom:1px solid #F3F3F7;white-space:nowrap}
  th{color:var(--sub);font-weight:600;font-size:11.5px;letter-spacing:.3px;
     position:sticky;top:0;background:#fff}
  tbody tr:hover{background:#FBFAFE}
  td.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:#4B5563}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;
        background:var(--accent-weak);color:#5B3FC4}
  .pill.bad{background:var(--warn-weak);color:#B91C1C}
  .pill.ok{background:var(--ok-weak);color:var(--ok)}
  .empty{color:var(--sub);font-size:12.5px;padding:18px 4px;text-align:center}
  .hide{display:none!important}

  /* ---------- 内容管理 ---------- */
  .tabs{display:flex;flex-wrap:wrap;gap:4px;padding:3px;background:#F3F2F8;
         border:1px solid var(--line);border-radius:11px;margin-bottom:14px}
  .tabs button{border:0;background:transparent;color:var(--sub);padding:7px 12px;
               border-radius:8px;font-size:12.5px}
  .tabs button.on{background:#fff;color:var(--ink);font-weight:650;
                  box-shadow:0 1px 4px rgba(24,24,40,.08)}
  .tabs button small{display:block;font-size:10px;color:var(--sub);font-weight:400}
  .form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .form-grid .full{grid-column:1/-1}
  .form-grid label,.field-label{display:block;font-size:12px;color:var(--sub);margin-bottom:5px}
  .input,.textarea{width:100%;padding:9px 11px;border:1px solid var(--line);
                   border-radius:9px;background:#FAFAFC;outline:none;font:inherit}
  .input:focus,.textarea:focus{border-color:#C9BDF5;background:#fff;
                               box-shadow:0 0 0 3px #F2EEFF}
  .textarea{min-height:96px;resize:vertical;line-height:1.65}
  .jsonarea{min-height:360px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
            font-size:12px;line-height:1.6;white-space:pre;overflow:auto}
  .editor-actions{display:flex;align-items:center;gap:8px;margin-top:12px;flex-wrap:wrap}
  .editor-actions .msg{font-size:12px;color:var(--sub);margin-right:auto}
  .editor-actions .msg.bad{color:var(--warn)}
  .editor-actions .msg.ok{color:var(--ok)}
  .btn.primary-sm{background:var(--ink);border-color:var(--ink);color:#fff}
  .btn.danger{color:#B91C1C;border-color:#F3C6C6}
  .msg-item{border:1px solid var(--line);border-radius:12px;padding:12px;
            margin-bottom:10px;background:#FCFCFE}
  .msg-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
  .msg-head b{font-size:12px;color:var(--sub);font-weight:600}
  .msg-item .form-grid{grid-template-columns:1fr 1fr}
  .feedback-content{white-space:normal;max-width:440px;line-height:1.55}
  .history-row{display:flex;align-items:center;gap:10px;padding:7px 0;
               border-bottom:1px solid #F3F3F7;font-size:12.5px}
  .history-row:last-child{border-bottom:0}
  .history-row .mono{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .history-row .muted{flex:none}
  .muted{color:var(--sub);font-size:12px}

  @media (max-width:860px){
    .shell{flex-direction:column}
    .side{width:auto;flex:none;height:auto;position:static;flex-direction:row;
          align-items:center;gap:12px;padding:10px 12px;overflow-x:auto}
    .brand{padding:0;flex:none}
    nav{flex-direction:row;gap:4px}
    nav a span{display:none}
    .side-foot{margin:0;padding:0;border:0;flex-direction:row;align-items:center}
    .side-foot .who{display:none}
    .main{padding:16px 14px 40px}
    .cols{grid-template-columns:1fr}
    .form-grid{grid-template-columns:1fr}
  }
</style></head>
<body>

<!-- 登录 -->
<div class="login-wrap" id="loginView">
  <div class="login">
    <div class="logo">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#fff"
           stroke-width="2" stroke-linecap="round"><rect x="4" y="4" width="7" height="7" rx="2"/>
        <rect x="13" y="4" width="7" height="7" rx="2"/><rect x="4" y="13" width="7" height="7" rx="2"/>
        <path d="M13 17h5m0 0-2-2m2 2-2 2"/></svg>
    </div>
    <h2>掌上锡院 · 控制台</h2>
    <p class="err" style="color:var(--sub);margin-bottom:14px;font-size:12.5px">
      输入服务器 data/.env 中的 ADMIN_KEY</p>
    <input id="key" class="field" type="password" placeholder="ADMIN_KEY" autocomplete="current-password">
    <button class="btn primary" onclick="login()">进入控制台</button>
    <p class="err" id="loginErr"></p>
  </div>
</div>

<!-- 主界面 -->
<div class="shell hide" id="app">
  <aside class="side">
    <div class="brand">
      <div class="mark">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff"
             stroke-width="2.2" stroke-linecap="round"><rect x="4" y="4" width="7" height="7" rx="2"/>
          <rect x="13" y="4" width="7" height="7" rx="2"/><rect x="4" y="13" width="7" height="7" rx="2"/>
          <path d="M13 17h5m0 0-2-2m2 2-2 2"/></svg>
      </div>
      <div><b>掌上锡院</b><span>控制台</span></div>
    </div>

    <nav id="nav">
      <a data-tab="overview" class="on" title="概览">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round"><rect x="3" y="3" width="7" height="7" rx="2"/>
          <rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/>
          <rect x="14" y="14" width="7" height="7" rx="2"/></svg><span>概览</span></a>
      <a data-tab="devices" title="设备">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round"><rect x="7" y="2.5" width="10" height="19" rx="3"/>
          <path d="M11 18.5h2"/></svg><span>设备</span></a>
      <a data-tab="users" title="用户">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round"><circle cx="12" cy="8" r="4"/><path d="M4.5 20c1.5-3.5 4.2-5 7.5-5s6 1.5 7.5 5"/></svg><span>用户</span></a>
      <a data-tab="content" title="内容">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round"><path d="M5 4h14v16H5z"/><path d="M8 8h8M8 12h8M8 16h5"/></svg><span>内容</span></a>
      <a data-tab="resources" title="资源">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round"><path d="M12 16V4"/><path d="M7 9l5-5 5 5"/><path d="M4 20h16"/></svg><span>资源</span></a>
      <a data-tab="feedback" title="反馈">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round"><path d="M4 5h16v11H9l-5 4z"/><path d="M8 9h8M8 12h5"/></svg><span>反馈</span></a>
      <a data-tab="risk" title="风控">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round"><path d="M12 3.5 21 20H3z"/><path d="M12 9v4.5"/>
          <circle cx="12" cy="17" r=".6" fill="currentColor"/></svg><span>风控</span></a>
      <a data-tab="audit" title="审计">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round"><path d="M5 4h11l3 3v13H5z"/><path d="M8 9h8M8 13h8M8 17h5"/></svg><span>审计</span></a>
    </nav>

    <div class="side-foot">
      <div class="who" id="stamp">—</div>
      <button class="btn ghost" onclick="logout()" style="text-align:left">退出登录</button>
    </div>
  </aside>

  <main class="main">
    <div class="topbar">
      <div>
        <h1 id="pageTitle">概览</h1>
        <div class="sub" id="pageSub">设备、用户与内容下发情况</div>
      </div>
      <div class="actions">
        <button class="btn" onclick="reload()">刷新</button>
      </div>
    </div>

    <!-- 概览 -->
    <section id="tab-overview">
      <div class="cards" id="cards"></div>
      <div class="panel">
        <div class="hd"><h3>近 30 天</h3></div>
        <div class="chart" id="chart"></div>
        <div class="legend">
          <span><i class="dot" style="background:#6C4FD9"></i>活跃设备</span>
          <span><i class="dot" style="background:#185FA5;opacity:.5"></i>新增设备</span>
        </div>
      </div>
      <div class="cols">
        <div class="panel"><div class="hd"><h3>版本分布</h3></div><div id="versions"></div></div>
        <div class="panel"><div class="hd"><h3>平台分布</h3></div><div id="platforms"></div></div>
      </div>
    </section>

    <!-- 设备 -->
    <section id="tab-devices" class="hide">
      <div class="panel">
        <div class="hd">
          <h3>设备列表</h3>
          <input id="q" class="field" style="width:240px;padding:8px 12px"
                 placeholder="搜索序列号 / 安装标识" oninput="loadDevices()">
        </div>
        <div style="overflow:auto;max-height:60vh"><table id="devTable"></table></div>
      </div>
    </section>

    <!-- 用户 -->
    <section id="tab-users" class="hide">
      <div class="panel">
        <div class="hd"><h3>用户列表</h3></div>
        <div style="overflow:auto;max-height:60vh"><table id="userTable"></table></div>
        <p class="who" style="padding:6px 2px 0">只保存学号哈希与固定伪名，不存学号明文、不存姓名。</p>
      </div>
    </section>

    <!-- 内容管理 -->
    <section id="tab-content" class="hide">
      <div class="panel">
        <div class="hd">
          <h3>内容管理</h3>
          <span class="muted" id="contentMeta">—</span>
        </div>
        <div class="tabs" id="contentTabs">
          <button data-content="app_release" class="on">版本发布<small id="cv-app_release">—</small></button>
          <button data-content="messages">公告<small id="cv-messages">—</small></button>
          <button data-content="school_calendar">校历<small id="cv-school_calendar">—</small></button>
          <button data-content="exam_papers">试卷库<small id="cv-exam_papers">—</small></button>
        </div>
        <div id="contentEditor"></div>
        <div id="contentHistory"></div>
      </div>
    </section>

    <!-- 资源审核 -->
    <section id="tab-resources" class="hide">
      <div class="panel">
        <div class="hd">
          <h3>资源审核</h3>
          <select id="resFilter" class="input" style="width:160px;padding:7px 10px" onchange="loadResources()">
            <option value="pending">待审核</option>
            <option value="approved">已通过</option>
            <option value="rejected">已拒绝</option>
            <option value="">全部</option>
          </select>
        </div>
        <div style="overflow:auto;max-height:60vh"><table id="resTable"></table></div>
        <p class="muted" style="padding:6px 2px 0">通过后立即出现在客户端资源中心；拒绝仅保留审核记录，客户端不可见。</p>
      </div>
    </section>

    <!-- 反馈 -->
    <section id="tab-feedback" class="hide">
      <div class="panel">
        <div class="hd"><h3>用户反馈</h3>
          <select id="fbFilter" class="input" style="width:160px;padding:7px 10px" onchange="loadFeedback()">
            <option value="">全部</option><option value="new">未读</option>
            <option value="read">已读</option><option value="done">已处理</option>
          </select>
        </div>
        <div style="overflow:auto;max-height:60vh"><table id="fbTable"></table></div>
        <p class="muted" style="padding:6px 2px 0">反馈可匿名提交；带设备令牌时显示设备序列号，不采集学号明文。</p>
      </div>
    </section>

    <!-- 风控 -->
    <section id="tab-risk" class="hide">
      <div class="panel">
        <div class="hd"><h3>风控事件</h3>
          <button class="btn" onclick="clearRisk()">全部标记已处理</button></div>
        <div style="overflow:auto;max-height:60vh"><table id="riskTable"></table></div>
      </div>
    </section>

    <!-- 审计 -->
    <section id="tab-audit" class="hide">
      <div class="panel">
        <div class="hd"><h3>审计日志</h3></div>
        <div style="overflow:auto;max-height:60vh"><table id="auditTable"></table></div>
      </div>
    </section>
  </main>
</div>

<script>
let KEY = sessionStorage.getItem('adminKey') || '';
let CURRENT = 'overview';
const $ = (id) => document.getElementById(id);
const TITLES = {
  overview: ['概览', '设备、用户与内容下发情况'],
  devices: ['设备', '按最后活跃排序，可封禁 / 恢复'],
  users: ['用户', '学号哈希与固定伪名，可停用'],
  content: ['内容', '版本、公告、校历与试卷库，保存后自动生效'],
  resources: ['资源审核', '用户上传的教材 PDF / 试卷与答案，审核后客户端可见'],
  feedback: ['反馈', '匿名用户反馈与处理状态'],
  risk: ['风控', '登录频次异常与多设备绑定'],
  audit: ['审计', '管理端写操作记录'],
};

const auth = () => ({'Authorization': 'Bearer ' + KEY});

async function api(path, opts={}){
  const res = await fetch(path, {...opts, headers: {...(opts.headers||{}), ...auth()}});
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (res.status === 401) throw new Error('密钥无效或已失效');
  if (!res.ok) throw new Error((data && data.error && data.error.message) || ('HTTP ' + res.status));
  return data;
}

const fmt = (n) => (n ?? 0).toLocaleString('zh-CN');
const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function login(){
  KEY = $('key').value.trim();
  if(!KEY) return;
  sessionStorage.setItem('adminKey', KEY);
  boot();
}
function logout(){
  KEY=''; sessionStorage.removeItem('adminKey');
  $('app').classList.add('hide'); $('loginView').classList.remove('hide');
}
$('key').addEventListener('keydown', e => { if(e.key === 'Enter') login(); });

async function boot(){
  try{
    const ov = await api('/admin/metrics/overview');
    $('loginView').classList.add('hide');
    $('app').classList.remove('hide');
    $('stamp').textContent = '更新于 ' + ov.generatedAt.slice(11, 19);
    renderCards(ov);
    renderBars('versions', ov.versions);
    renderBars('platforms', ov.platforms);
    const s = await api('/admin/metrics/series?days=30');
    renderChart(s.items);
  }catch(e){
    $('loginErr').textContent = '密钥不对，或服务未启动';
    $('app').classList.add('hide'); $('loginView').classList.remove('hide');
  }
}

function renderCards(o){
  const items = [
    ['用户总数', fmt(o.users.total), '停用 ' + o.users.banned, false],
    ['设备总数', fmt(o.devices.total), '今日新增 ' + o.devices.newToday, true],
    ['DAU', fmt(o.active.dau), '今日活跃设备', true],
    ['WAU', fmt(o.active.wau), '近 7 天', false],
    ['MAU', fmt(o.active.mau), '近 30 天', false],
    ['今日事件', fmt(o.events.today), '累计 ' + o.events.total, false],
    ['待处理风控', fmt(o.risk.open), '风险事件', false],
  ];
  $('cards').innerHTML = items.map(([l,n,h,accent]) =>
    `<div class="card${accent?' accent':''}"><div class="l"><span>${l}</span></div>
     <div class="n">${n}</div><div class="h">${h}</div></div>`).join('');
}

function renderBars(id, items){
  if(!items || !items.length){ $(id).innerHTML = '<div class="empty">暂无数据</div>'; return; }
  const top = Math.max(...items.map(i => i.value)) || 1;
  $(id).innerHTML = items.map(i =>
    `<div class="row"><span class="k">${esc(i.name)}</span>
      <span class="track"><i style="width:${Math.round(i.value/top*100)}%"></i></span>
      <span class="v">${i.value}</span></div>`).join('');
}

function renderChart(items){
  const max = Math.max(1, ...items.map(i => i.activeDevices), ...items.map(i => i.newDevices));
  $('chart').innerHTML = items.map(i => {
    const a = Math.max(2, Math.round(i.activeDevices / max * 100));
    const b = Math.max(1, Math.round(i.newDevices / max * 100));
    return `<div class="col" title="${i.day} · 活跃 ${i.activeDevices} / 新增 ${i.newDevices}">
      <div class="bar" style="height:${a}%"></div>
      <div class="bar alt" style="height:${b}%"></div></div>`;
  }).join('');
}

async function loadDevices(){
  const q = ($('q')?.value || '').trim();
  const d = await api('/admin/devices?query=' + encodeURIComponent(q));
  if(!d.items.length){ $('devTable').innerHTML = '<tr><td class="empty">暂无设备</td></tr>'; return; }
  $('devTable').innerHTML =
    '<thead><tr><th>序列号</th><th>平台</th><th>版本</th><th>状态</th><th>会话</th>'
    + '<th>最后活跃</th><th></th></tr></thead><tbody>'
    + d.items.map(x => `<tr>
      <td class="mono">${esc(x.device_serial)}</td><td>${esc(x.platform||'-')}</td>
      <td>${esc(x.app_version||'-')}</td>
      <td>${x.status==='active'?'<span class="pill ok">正常</span>':'<span class="pill bad">已封禁</span>'}</td>
      <td>${x.sessions}</td><td class="mono">${esc(x.last_seen)}</td>
      <td><button class="btn" style="padding:4px 10px;font-size:12px"
        onclick="toggleDevice(${x.id},'${x.status==='active'?'banned':'active'}')">
        ${x.status==='active'?'封禁':'恢复'}</button></td></tr>`).join('')
    + '</tbody>';
}

async function toggleDevice(id, status){
  await api(`/admin/devices/${id}/status`, {method:'PATCH',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({status})});
  loadDevices();
}

async function loadUsers(){
  const d = await api('/admin/users');
  if(!d.items.length){ $('userTable').innerHTML = '<tr><td class="empty">暂无用户</td></tr>'; return; }
  $('userTable').innerHTML =
    '<thead><tr><th>伪名</th><th>显示名</th><th>设备</th><th>状态</th><th>最后登录</th></tr></thead><tbody>'
    + d.items.map(x => `<tr>
      <td class="mono">${esc(x.student_alias.slice(0,14))}…</td>
      <td>${esc(x.display_name||'-')}</td><td>${x.devices}</td>
      <td>${x.status==='active'?'<span class="pill ok">正常</span>':'<span class="pill bad">停用</span>'}</td>
      <td class="mono">${esc(x.last_login_at||'-')}</td></tr>`).join('') + '</tbody>';
}

async function loadRisk(){
  const d = await api('/admin/risk-events');
  if(!d.items.length){ $('riskTable').innerHTML = '<tr><td class="empty">暂无风控事件</td></tr>'; return; }
  $('riskTable').innerHTML =
    '<thead><tr><th>类型</th><th>设备</th><th>用户</th><th>详情</th><th>时间</th><th>状态</th></tr></thead><tbody>'
    + d.items.map(x => `<tr>
      <td>${esc(x.kind)}</td><td class="mono">${esc(x.device_serial||'-')}</td>
      <td class="mono">${esc((x.student_alias||'-').slice(0,10))}</td>
      <td>${esc(x.detail)}</td><td class="mono">${esc(x.created_at)}</td>
      <td>${x.cleared_at?'<span class="pill">已处理</span>':'<span class="pill bad">待处理</span>'}</td>
      </tr>`).join('') + '</tbody>';
}

async function loadAudit(){
  const d = await api('/admin/audit');
  if(!d.items.length){ $('auditTable').innerHTML = '<tr><td class="empty">暂无记录</td></tr>'; return; }
  $('auditTable').innerHTML =
    '<thead><tr><th>动作</th><th>操作者</th><th>详情</th><th>时间</th></tr></thead><tbody>'
    + d.items.map(x => `<tr><td>${esc(x.action)}</td><td>${esc(x.actor)}</td>
      <td class="mono">${esc(x.detail)}</td><td class="mono">${esc(x.created_at)}</td></tr>`).join('')
    + '</tbody>';
}

// ---------------------------------------------------------------- 内容管理

const CONTENT_LABELS = {
  app_release: '版本发布', messages: '公告',
  school_calendar: '校历', exam_papers: '试卷库',
};
let CONTENT_NAME = 'app_release';
let MESSAGES = [];

async function loadContent(){
  try{
    const d = await api('/admin/content');
    d.items.forEach(x => {
      const el = $('cv-' + x.name);
      if (el) el.textContent = x.version === '0' ? '未配置' : x.version;
    });
    $('contentMeta').textContent = '修改后点击保存，版本号自动变化';
    await openContent(CONTENT_NAME);
  }catch(e){
    $('contentEditor').innerHTML = '<div class="empty">' + esc(e.message) + '</div>';
  }
}

async function openContent(name){
  CONTENT_NAME = name;
  document.querySelectorAll('#contentTabs button').forEach(b =>
    b.classList.toggle('on', b.dataset.content === name));
  $('contentHistory').innerHTML = '';
  try{
    const d = await api('/admin/content/' + name);
    if (name === 'app_release') renderRelease(d.data || {});
    else if (name === 'messages') renderMessages(Array.isArray(d.data) ? d.data : []);
    else renderJson(name, d.data);
    await loadContentHistory(name);
  }catch(e){
    $('contentEditor').innerHTML = '<div class="empty">' + esc(e.message) + '</div>';
  }
}

async function loadContentHistory(name){
  try{
    const d = await api('/admin/content/' + name + '/history');
    if (!d.items.length){ $('contentHistory').innerHTML = ''; return; }
    $('contentHistory').innerHTML = `
      <div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--line)">
        <div class="field-label" style="margin-bottom:4px">历史备份 · 最近 ${d.items.length} 份</div>
        ${d.items.map(x => `<div class="history-row">
          <span class="mono">${esc(x.file)}</span>
          <span class="muted">${esc(x.savedAt)}</span>
          <button class="btn" style="padding:3px 9px;font-size:12px"
            onclick="restoreContent('${name}','${esc(x.file)}')">恢复</button>
        </div>`).join('')}
      </div>`;
  }catch(_){
    $('contentHistory').innerHTML = '';
  }
}

async function restoreContent(name, file){
  if (!confirm('恢复会覆盖当前内容，确定继续？')) return;
  try{
    await api('/admin/content/' + name + '/restore', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({file})});
    await openContent(name);
  }catch(e){
    alert('恢复失败：' + e.message);
  }
}

function renderRelease(d){
  $('contentEditor').innerHTML = `
    <div class="form-grid">
      <div><label class="field-label">版本号</label>
        <input id="crVersion" class="input" value="${esc(d.latestVersion || '')}" placeholder="例如 1.0.8"></div>
      <div><label class="field-label">下载地址</label>
        <input id="crUrl" class="input" value="${esc(d.downloadUrl || '')}" placeholder="https://…"></div>
      <div class="full"><label class="field-label">更新说明</label>
        <textarea id="crNotes" class="textarea">${esc(d.notes || '')}</textarea></div>
    </div>
    <div class="editor-actions"><span id="contentMsg" class="msg"></span>
      <button class="btn primary-sm" onclick="saveRelease()">保存发布信息</button></div>`;
}

async function saveRelease(){
  await saveContent('app_release', {
    latestVersion: $('crVersion').value.trim(),
    downloadUrl: $('crUrl').value.trim(),
    notes: $('crNotes').value.trim(),
  });
}

function renderMessages(items){
  MESSAGES = items.map(x => ({...x}));
  $('contentEditor').innerHTML = `
    <div class="editor-actions" style="margin:0 0 12px 0">
      <span class="muted">共 ${MESSAGES.length} 条，前端按文件顺序展示</span>
      <button class="btn" onclick="addMessage()">新增公告</button>
    </div>
    <div id="messageList">${MESSAGES.map(messageCard).join('') || '<div class="empty">暂无公告</div>'}</div>
    <div class="editor-actions"><span id="contentMsg" class="msg"></span>
      <button class="btn primary-sm" onclick="saveMessages()">保存全部公告</button></div>`;
}

function messageCard(m, i){
  return `<div class="msg-item" data-index="${i}">
    <div class="msg-head"><b>公告 ${i + 1}</b>
      <button class="btn danger" style="padding:4px 10px;font-size:12px" onclick="removeMessage(${i})">删除</button></div>
    <div class="form-grid">
      <div><label class="field-label">ID</label>
        <input class="input" data-f="id" value="${esc(m.id || '')}" placeholder="唯一标识"></div>
      <div><label class="field-label">日期</label>
        <input class="input" data-f="date" value="${esc(m.date || '')}" placeholder="2026-10-28"></div>
      <div class="full"><label class="field-label">标题</label>
        <input class="input" data-f="title" value="${esc(m.title || '')}"></div>
      <div class="full"><label class="field-label">正文</label>
        <textarea class="textarea" data-f="body">${esc(m.body || '')}</textarea></div>
      <div class="full"><label class="muted">
        <input type="checkbox" data-f="pinned" ${m.pinned ? 'checked' : ''}> 置顶</label></div>
    </div></div>`;
}

function collectMessages(){
  return [...document.querySelectorAll('#messageList .msg-item')].map(el => {
    const val = f => el.querySelector('[data-f="' + f + '"]');
    return {
      id: val('id').value.trim(),
      title: val('title').value.trim(),
      body: val('body').value.trim(),
      date: val('date').value.trim(),
      pinned: val('pinned').checked,
    };
  });
}

function addMessage(){
  MESSAGES = collectMessages();
  MESSAGES.push({id: 'msg-' + Date.now(), title: '', body: '', date: new Date().toISOString().slice(0, 10), pinned: false});
  renderMessages(MESSAGES);
}

function removeMessage(index){
  MESSAGES = collectMessages();
  MESSAGES.splice(index, 1);
  renderMessages(MESSAGES);
}

async function saveMessages(){
  await saveContent('messages', collectMessages());
}

function renderJson(name, data){
  $('contentEditor').innerHTML = `
    <label class="field-label">${esc(CONTENT_LABELS[name] || name)} · JSON</label>
    <textarea id="jsonEditor" class="textarea jsonarea" spellcheck="false">${esc(JSON.stringify(data, null, 2))}</textarea>
    <div class="editor-actions"><span id="contentMsg" class="msg"></span>
      <button class="btn" onclick="validateJson()">仅校验</button>
      <button class="btn primary-sm" onclick="saveJson('${name}')">保存</button></div>`;
}

function readJson(){
  const msg = $('contentMsg');
  try{
    return JSON.parse($('jsonEditor').value);
  }catch(e){
    msg.className = 'msg bad';
    msg.textContent = 'JSON 格式错误：' + e.message;
    return null;
  }
}

function validateJson(){
  if (readJson() !== null){
    const msg = $('contentMsg'); msg.className = 'msg ok'; msg.textContent = 'JSON 格式正确';
  }
}

async function saveJson(name){
  const data = readJson();
  if (data !== null) await saveContent(name, data);
}

async function saveContent(name, data){
  const msg = $('contentMsg');
  if (msg){ msg.className = 'msg'; msg.textContent = '保存中…'; }
  try{
    const res = await api('/admin/content/' + name, {method:'PUT',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({data})});
    if (msg){ msg.className = 'msg ok'; msg.textContent = '已保存 · 版本 ' + res.version; }
    const el = $('cv-' + name);
    if (el) el.textContent = res.version;
  }catch(e){
    if (msg){ msg.className = 'msg bad'; msg.textContent = '保存失败：' + e.message; }
  }
}

$('contentTabs').addEventListener('click', e => {
  const b = e.target.closest('button[data-content]');
  if (b) openContent(b.dataset.content);
});

// ---------------------------------------------------------------- 反馈

const RES_AREA = {
  textbook: '教材 PDF',
  exam_paper: '试卷',
  answer: '答案',
};

function resStatus(x){
  if (x.status === 'approved') return '<span class="pill ok">已通过</span>';
  if (x.status === 'rejected') return '<span class="pill">已拒绝</span>';
  return '<span class="pill bad">待审核</span>';
}

async function loadResources(){
  const status = ($('resFilter')?.value || '').trim();
  const d = await api('/admin/resources' + (status ? '?status=' + status : ''));
  if(!d.items.length){
    $('resTable').innerHTML = '<tr><td class="empty">暂无资源上传</td></tr>';
    return;
  }
  $('resTable').innerHTML =
    '<thead><tr><th>课程 / 教材</th><th>区域</th><th>备注</th><th>文件</th><th>状态</th><th>时间</th><th>操作</th></tr></thead><tbody>'
    + d.items.map(x => `<tr>
      <td><b>${esc(x.courseName)}</b><div class="who">${esc(x.bookName || '未填教材')}</div></td>
      <td>${esc(RES_AREA[x.area] || x.area)}</td>
      <td><div class="feedback-content">${esc(x.note || '-')}</div></td>
      <td><div class="feedback-content">${x.files.map(f => esc(f.name)).join('<br>')}</div></td>
      <td>${resStatus(x)}</td>
      <td class="mono">${esc(x.createdAt)}${x.reviewedAt ? '<br>' + esc(x.reviewedAt) : ''}</td>
      <td><span class="table-actions">
        ${x.status !== 'approved' ? `<button class="btn" style="padding:4px 9px;font-size:12px" onclick="reviewResource(${x.id},'approved')">通过</button>` : ''}
        ${x.status !== 'rejected' ? `<button class="btn" style="padding:4px 9px;font-size:12px" onclick="reviewResource(${x.id},'rejected')">拒绝</button>` : ''}
      </span></td></tr>`).join('') + '</tbody>';
}

async function reviewResource(id, status){
  const ok = confirm(status === 'approved' ? '确认通过这条资源？' : '确认拒绝这条资源？');
  if(!ok) return;
  const reviewNote = status === 'rejected' ? (prompt('拒绝原因（可选）') || '') : '';
  await api(`/admin/resources/${id}/status`, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({status, reviewNote}),
  });
  loadResources();
}

async function loadFeedback(){
  const status = ($('fbFilter')?.value || '').trim();
  const d = await api('/admin/feedback' + (status ? '?status=' + status : ''));
  if(!d.items.length){ $('fbTable').innerHTML = '<tr><td class="empty">暂无反馈</td></tr>'; return; }
  $('fbTable').innerHTML =
    '<thead><tr><th>时间</th><th>内容</th><th>联系方式</th><th>版本</th><th>状态</th><th>操作</th></tr></thead><tbody>'
    + d.items.map(x => `<tr>
      <td class="mono">${esc(x.created_at)}</td>
      <td><div class="feedback-content">${esc(x.content)}</div></td>
      <td>${esc(x.contact || '-')}</td>
      <td class="mono">${esc(x.app_version || '-')}</td>
      <td>${fbStatus(x)}</td>
      <td><span class="table-actions">
        ${x.status === 'new' ? `<button class="btn" style="padding:4px 9px;font-size:12px" onclick="setFeedback(${x.id},'read')">标为已读</button>` : ''}
        ${x.status !== 'done' ? `<button class="btn" style="padding:4px 9px;font-size:12px" onclick="setFeedback(${x.id},'done')">已处理</button>` : ''}
        ${x.status === 'done' ? `<button class="btn" style="padding:4px 9px;font-size:12px" onclick="setFeedback(${x.id},'read')">重新打开</button>` : ''}
      </span></td></tr>`).join('') + '</tbody>';
}

function fbStatus(x){
  if (x.status === 'done') return '<span class="pill ok">已处理</span>';
  if (x.status === 'read') return '<span class="pill">已读</span>';
  return '<span class="pill bad">未读</span>';
}

async function setFeedback(id, status){
  await api(`/admin/feedback/${id}/status`, {method:'PATCH',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({status})});
  loadFeedback();
}

async function clearRisk(){
  await api('/admin/risk-events', {method:'DELETE'});
  loadRisk();
}

function switchTab(name){
  CURRENT = name;
  document.querySelectorAll('nav a').forEach(a => a.classList.toggle('on', a.dataset.tab === name));
  ['overview','devices','users','content','resources','feedback','risk','audit'].forEach(k =>
    $('tab-' + k).classList.toggle('hide', k !== name));
  const [title, sub] = TITLES[name] || ['', ''];
  $('pageTitle').textContent = title;
  $('pageSub').textContent = sub;
  reload();
}

function reload(){
  const loaders = {
    devices: loadDevices, users: loadUsers, content: loadContent,
    resources: loadResources,
    feedback: loadFeedback, risk: loadRisk, audit: loadAudit,
  };
  const fn = loaders[CURRENT];
  if (fn) fn();
  else boot();
}

$('nav').addEventListener('click', e => {
  const a = e.target.closest('a[data-tab]');
  if (a) switchTab(a.dataset.tab);
});

if(KEY) boot();
</script>
</body></html>"""
