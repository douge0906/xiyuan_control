"""管理后台的单页界面（无 CDN 依赖，纯 HTML/CSS/JS）。

布局：左侧固定侧边栏 + 右侧内容区；窄屏时侧边栏折成顶部横向导航。
"""

ADMIN_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>掌上锡院 · 控制台</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%231F2937'/%3E%3Cg fill='none' stroke='%23fff' stroke-width='2.4'%3E%3Crect x='8' y='8' width='6' height='6' rx='1.5'/%3E%3Crect x='18' y='8' width='6' height='6' rx='1.5'/%3E%3Crect x='8' y='18' width='6' height='6' rx='1.5'/%3E%3Cpath d='M18 21h6m0 0-2-2m2 2-2 2'/%3E%3C/g%3E%3C/svg%3E">
<meta name="color-scheme" content="light">
<style>
  /* 设计令牌：间距 / 圆角 / 字号 / 控件高度只在这里定义一次，
     下面的组件不再各自写死 px（2026-11-04 UI 收敛）。 */
  :root{
    --ink:#1F2937; --sub:#6B7280; --line:#E8E8EE; --bg:#F6F7FB;
    --accent:#6C4FD9; --accent-weak:#F2EEFF; --warn:#DC2626; --warn-weak:#FEF2F2;
    --ok:#0F6E56; --ok-weak:#ECFDF5;
    --hover:#FAF9FF; --line-soft:#F3F3F7;

    --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:20px; --sp-6:24px;
    --r-xs:8px; --r-sm:10px; --r-md:12px; --r-lg:16px; --r-xl:20px;
    --fs-xs:11px; --fs-sm:12px; --fs-md:14px; --fs-lg:17px; --fs-xl:19px;
    --ctl:34px; --ctl-sm:28px; --ctl-xs:24px;
    --shadow-2:0 10px 34px rgba(24,24,40,.10);
    --ring:0 0 0 3px var(--accent-weak);
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
       font-size:var(--fs-md);line-height:1.6;background:var(--bg);color:var(--ink);
       -webkit-font-smoothing:antialiased}
  button{font:inherit;cursor:pointer;color:inherit}
  ::selection{background:var(--accent-weak);color:#4B32A8}
  :focus-visible{outline:2px solid var(--accent);outline-offset:2px}

  /* ---------- 登录 ---------- */
  .login-wrap{min-height:100%;display:grid;place-items:center;padding:24px}
  .login{width:100%;max-width:380px;background:#fff;border:1px solid var(--line);
         border-radius:20px;padding:26px 24px;box-shadow:0 10px 40px rgba(24,24,40,.06)}
  .login .logo{width:40px;height:40px;border-radius:12px;background:var(--ink);
               display:grid;place-items:center;margin-bottom:14px}
  .login h2{font-size:17px;margin:0 0 4px;font-weight:650}
  .login p{margin:0 0 16px}
  .field{width:100%;height:42px;padding:0 13px;border:1px solid var(--line);
         border-radius:var(--r-md);font:inherit;font-size:var(--fs-md);
         color:inherit;background:#FAFAFC;outline:none;
         transition:border-color .12s,background .12s,box-shadow .12s}
  .field:focus{border-color:#C9BDF5;background:#fff;box-shadow:var(--ring)}
  .field::placeholder{color:#9CA3AF}
  .login .btn.primary{width:100%;margin-top:var(--sp-3)}
  .login .hint{margin-bottom:var(--sp-3)}

  /* 按钮：高度 / 圆角 / 字号统一，危险操作用 .danger，二次确认为主按钮 */
  .btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;
       height:var(--ctl);padding:0 var(--sp-3);border:1px solid var(--line);
       background:#fff;border-radius:var(--r-sm);font-size:var(--fs-md);
       text-decoration:none;white-space:nowrap;
       transition:background .12s,border-color .12s,color .12s,opacity .12s}
  .btn:hover{background:var(--hover);border-color:#DAD8E6}
  .btn:active{transform:translateY(1px)}
  .btn:disabled{opacity:.5;cursor:not-allowed;transform:none}
  .btn.sm{height:var(--ctl-sm);padding:0 10px;font-size:var(--fs-sm);border-radius:var(--r-xs)}
  .btn.xs{height:var(--ctl-xs);padding:0 9px;font-size:var(--fs-xs);border-radius:6px}
  .btn.block{width:100%}
  .btn.primary,.btn.primary-sm{background:var(--ink);border-color:var(--ink);color:#fff}
  .btn.primary:hover,.btn.primary-sm:hover{background:#111827;border-color:#111827}
  .btn.ghost{border-color:transparent;background:transparent;color:var(--sub)}
  .btn.ghost:hover{background:var(--hover);color:var(--ink);border-color:transparent}
  .btn.danger{color:#B91C1C;border-color:#F3C6C6;background:#fff}
  .btn.danger:hover{background:var(--warn-weak);border-color:#EBA9A9}
  .btn.danger-solid{background:var(--warn);border-color:var(--warn);color:#fff}
  .btn.danger-solid:hover{background:#B91C1C;border-color:#B91C1C}
  .err{color:var(--warn);font-size:var(--fs-sm);margin-top:10px;min-height:18px}
  .hint{font-size:var(--fs-sm);color:var(--sub);margin:var(--sp-2) 0 0;line-height:1.6}

  /* ---------- 外壳 ---------- */
  .shell{display:flex;min-height:100%}
  .side{width:236px;flex:0 0 236px;background:#fff;border-right:1px solid var(--line);
        display:flex;flex-direction:column;padding:var(--sp-4) var(--sp-3) var(--sp-3);gap:6px;
        position:sticky;top:0;height:100vh}
  .brand{display:flex;align-items:center;gap:10px;padding:2px var(--sp-2) var(--sp-4)}
  .brand .mark{width:30px;height:30px;border-radius:9px;background:var(--ink);
               display:grid;place-items:center;flex:none}
  .brand b{font-size:var(--fs-md);font-weight:650;letter-spacing:.2px}
  .brand span{display:block;font-size:var(--fs-xs);color:var(--sub);font-weight:400}
  nav{display:flex;flex-direction:column;gap:2px}
  nav a{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:var(--r-sm);
        color:var(--sub);text-decoration:none;font-size:13.5px;cursor:pointer;
        border:1px solid transparent;transition:background .12s,color .12s,border-color .12s}
  nav a:hover{background:#F7F7FA;color:var(--ink)}
  /* 当前页：左侧 3px 主色条 + 实底 + 加粗，比只换底色明显 */
  nav a.on{background:var(--accent-weak);color:#4B32A8;font-weight:650;
           border-color:#E3DAFF;box-shadow:inset 3px 0 0 var(--accent)}
  nav a svg{width:16px;height:16px;flex:none;opacity:.9}
  nav a.on svg{opacity:1}
  .side-foot{margin-top:auto;display:flex;flex-direction:column;gap:var(--sp-2);
             padding:var(--sp-3) var(--sp-2) 0;border-top:1px solid var(--line)}
  .side-foot .who{font-size:var(--fs-sm);color:var(--sub);line-height:1.5}
  .side-foot .beian{font-size:10.5px;color:var(--sub);opacity:.85;
                    line-height:1.5;letter-spacing:.1px}
  .side-foot .beian a{color:inherit;text-decoration:none}
  .side-foot .beian a:hover{text-decoration:underline}

  /* ---------- 主区 ---------- */
  .main{flex:1;min-width:0;padding:var(--sp-5) var(--sp-6) 56px}
  .topbar{display:flex;align-items:flex-end;justify-content:space-between;
          margin-bottom:var(--sp-4);gap:var(--sp-3);flex-wrap:wrap}
  .topbar h1{font-size:var(--fs-xl);margin:0;font-weight:650;letter-spacing:-.2px}
  .topbar .sub{font-size:var(--fs-sm);color:var(--sub);margin-top:2px}
  .actions{display:flex;gap:var(--sp-2);flex-wrap:wrap}

  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));
         gap:var(--sp-3);margin-bottom:var(--sp-4)}
  .card{background:#fff;border:1px solid var(--line);border-radius:var(--r-lg);
        padding:var(--sp-3) var(--sp-4);transition:box-shadow .15s,border-color .15s}
  .card:hover{box-shadow:0 4px 16px rgba(24,24,40,.06)}
  .card .l{font-size:var(--fs-xs);color:var(--sub);display:flex;justify-content:space-between}
  .card .n{font-size:26px;font-weight:660;letter-spacing:-.6px;margin-top:6px;
           font-variant-numeric:tabular-nums;line-height:1.2}
  .card .h{font-size:var(--fs-xs);color:var(--sub);margin-top:2px}
  .card.accent{background:linear-gradient(180deg,#FBFAFF,#fff);border-color:#E3DAFF}

  .panel{background:#fff;border:1px solid var(--line);border-radius:var(--r-lg);
         padding:var(--sp-4);margin-bottom:var(--sp-3)}
  .panel .hd{display:flex;align-items:center;justify-content:space-between;
             gap:var(--sp-2);margin-bottom:var(--sp-3);flex-wrap:wrap}
  .panel h3{font-size:13px;color:var(--ink);margin:0;font-weight:650;letter-spacing:.2px}
  /* hd 里要放多个控件时套一层，保证窄屏换行后仍然对齐 */
  .hd-tools{display:flex;align-items:center;gap:var(--sp-2);flex-wrap:wrap}
  .cols{display:grid;grid-template-columns:1fr 1fr;gap:var(--sp-3)}
  .chart{display:flex;align-items:flex-end;gap:4px;height:140px;padding-top:4px}
  /* 30 根柱在窄屏也要塞得下：允许列收缩，柱子最小 2px */
  .col{flex:1;min-width:0;display:flex;align-items:flex-end;gap:2px;height:100%;
       border-radius:4px;transition:background .12s}
  .col:hover{background:#FBFAFF}
  .bar{flex:1;min-width:2px;background:var(--accent);opacity:.85;border-radius:4px 4px 2px 2px}
  .bar.alt{background:#185FA5;opacity:.45}
  .legend{display:flex;gap:var(--sp-3);font-size:var(--fs-sm);color:var(--sub);margin-top:10px}
  .dot{display:inline-block;width:8px;height:8px;border-radius:3px;margin-right:5px}
  .line-chart{width:100%;height:160px;display:block}
  .line-chart .line{fill:none;stroke:var(--accent);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
  .line-chart .area{fill:var(--accent);opacity:.10}
  .line-chart .pt{fill:var(--accent)}
  .row{display:grid;grid-template-columns:112px 1fr 44px;gap:10px;align-items:center;margin:7px 0}
  .row .k{font-size:var(--fs-sm);color:var(--sub);overflow:hidden;
          text-overflow:ellipsis;white-space:nowrap}
  .track{background:#F2F1F7;height:7px;border-radius:4px;overflow:hidden}
  .track i{display:block;height:100%;background:var(--accent);opacity:.65;border-radius:4px}
  .row .v{text-align:right;font-size:var(--fs-sm);font-variant-numeric:tabular-nums;color:var(--sub)}

  /* ---------- 表格 ---------- */
  /* 滚动容器统一用 .table-wrap，不再各处内联 overflow/max-height */
  .table-wrap{overflow:auto;max-height:60vh;border:1px solid var(--line-soft);
              border-radius:var(--r-md);margin-top:2px}
  .table-wrap.short{max-height:34vh}
  table{width:100%;border-collapse:separate;border-spacing:0;font-size:13px}
  th,td{text-align:left;padding:10px var(--sp-3);border-bottom:1px solid var(--line-soft);
        white-space:nowrap;vertical-align:middle}
  th{color:var(--sub);font-weight:600;font-size:var(--fs-xs);letter-spacing:.3px;
     position:sticky;top:0;z-index:1;background:#FCFCFE;
     box-shadow:inset 0 -1px 0 var(--line)}
  tbody tr:last-child td{border-bottom:0}
  tbody tr{transition:background .1s}
  tbody tr:hover{background:var(--hover)}
  tbody tr.is-click{cursor:pointer}
  td.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:var(--fs-sm);
          color:#4B5563}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:var(--fs-xs);
        background:var(--accent-weak);color:#5B3FC4;white-space:nowrap}
  .pill.bad{background:var(--warn-weak);color:#B91C1C}
  .pill.ok{background:var(--ok-weak);color:var(--ok)}
  .empty{color:var(--sub);font-size:12.5px;padding:28px var(--sp-4);text-align:center}
  .empty small{display:block;font-size:var(--fs-xs);opacity:.8;margin-top:4px}
  /* 加载态：表格骨架行 + 顶部细进度条，避免切 tab 时白屏无反馈 */
  .loading{display:flex;align-items:center;justify-content:center;gap:var(--sp-2);
           padding:34px var(--sp-4);color:var(--sub);font-size:12.5px}
  .spinner{width:16px;height:16px;border:2px solid var(--line);
           border-top-color:var(--accent);border-radius:50%;
           animation:spin .7s linear infinite;flex:none}
  @keyframes spin{to{transform:rotate(360deg)}}
  .skl{height:11px;border-radius:6px;
       background:linear-gradient(90deg,#F1F0F6 25%,#F8F7FC 37%,#F1F0F6 63%);
       background-size:400% 100%;animation:shimmer 1.3s ease infinite}
  @keyframes shimmer{from{background-position:100% 0}to{background-position:-100% 0}}
  .table-actions{display:inline-flex;gap:6px;align-items:center;flex-wrap:nowrap}
  .hide{display:none!important}

  /* 行内错误条：加载失败时贴在面板顶部，不静默 */
  .load-error{display:flex;align-items:center;gap:var(--sp-2);margin-top:var(--sp-2);
              padding:10px var(--sp-3);border-radius:var(--r-sm);
              background:var(--warn-weak);border:1px solid #F3C6C6;color:#B91C1C;
              font-size:12.5px}
  .load-error .btn{margin-left:auto}

  /* ---------- 内容管理 ---------- */
  .tabs{display:flex;flex-wrap:wrap;gap:4px;padding:3px;background:#F3F2F8;
         border:1px solid var(--line);border-radius:var(--r-md);margin-bottom:var(--sp-3)}
  .tabs button{border:0;background:transparent;color:var(--sub);padding:7px var(--sp-3);
               border-radius:var(--r-sm);font-size:var(--fs-sm);transition:background .12s}
  .tabs button:hover{color:var(--ink)}
  .tabs button.on{background:#fff;color:var(--ink);font-weight:650;
                  box-shadow:0 1px 4px rgba(24,24,40,.08)}
  .tabs button small{display:block;font-size:10px;color:var(--sub);font-weight:400}
  .form-grid{display:grid;grid-template-columns:1fr 1fr;gap:var(--sp-3)}
  .form-grid .full{grid-column:1/-1}
  .form-grid label,.field-label{display:block;font-size:var(--fs-sm);color:var(--sub);
                                margin-bottom:5px}
  .input,.textarea{width:100%;padding:9px 11px;border:1px solid var(--line);
                   border-radius:var(--r-xs);background:#FAFAFC;outline:none;
                   font:inherit;color:inherit;
                   transition:border-color .12s,background .12s,box-shadow .12s}
  .input{height:var(--ctl)}
  .input:focus,.textarea:focus{border-color:#C9BDF5;background:#fff;box-shadow:var(--ring)}
  .textarea{min-height:96px;resize:vertical;line-height:1.65}
  .jsonarea{min-height:360px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
            font-size:var(--fs-sm);line-height:1.6;white-space:pre;overflow:auto}
  /* 表格上方的小控件（搜索框 / 下拉筛选）统一尺寸 */
  .ctl{width:200px}
  .ctl-sm{width:150px}
  .editor-actions{display:flex;align-items:center;gap:var(--sp-2);
                  margin-top:var(--sp-3);flex-wrap:wrap}
  .editor-actions.top{margin:0 0 var(--sp-3)}
  .history-wrap{margin-top:var(--sp-4);padding-top:var(--sp-3);
                border-top:1px solid var(--line)}
  .history-wrap .field-label{margin-bottom:var(--sp-1)}
  .editor-actions .msg{font-size:var(--fs-sm);color:var(--sub);margin-right:auto}
  .editor-actions .msg.bad{color:var(--warn)}
  .editor-actions .msg.ok{color:var(--ok)}
  .msg-item{border:1px solid var(--line);border-radius:var(--r-md);padding:var(--sp-3);
            margin-bottom:10px;background:#FCFCFE}
  .msg-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:var(--sp-2)}
  .msg-head b{font-size:var(--fs-sm);color:var(--sub);font-weight:600}
  .msg-item .form-grid{grid-template-columns:1fr 1fr}
  .feedback-content{white-space:normal;max-width:440px;line-height:1.55}
  .history-row{display:flex;align-items:center;gap:10px;padding:7px 0;
               border-bottom:1px solid var(--line-soft);font-size:12.5px}
  .history-row:last-child{border-bottom:0}
  .history-row .mono{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .history-row .muted{flex:none}
  .muted{color:var(--sub);font-size:var(--fs-sm)}
  .who{font-size:var(--fs-sm);color:var(--sub);line-height:1.5}
  .panel-note{font-size:var(--fs-sm);color:var(--sub);line-height:1.6;
              margin:var(--sp-2) 0 0}

  /* ---------- 确认弹窗（替换 confirm()，危险操作二次确认） ---------- */
  .modal-mask{position:fixed;inset:0;background:rgba(24,24,40,.42);z-index:60;
              display:grid;place-items:center;padding:var(--sp-4)}
  .modal{width:100%;max-width:420px;background:#fff;border:1px solid var(--line);
         border-radius:var(--r-xl);box-shadow:var(--shadow-2);padding:var(--sp-5);
         animation:pop .14s ease-out}
  @keyframes pop{from{opacity:0;transform:translateY(6px) scale(.985)}to{opacity:1;transform:none}}
  .modal h4{margin:0 0 6px;font-size:var(--fs-lg);font-weight:650}
  .modal p{margin:0;color:var(--sub);font-size:var(--fs-sm);line-height:1.65;
           white-space:pre-wrap}
  .modal .detail{margin-top:var(--sp-3);padding:10px var(--sp-3);border-radius:var(--r-sm);
                 background:#FCFCFE;border:1px solid var(--line-soft);
                 font-size:var(--fs-sm);color:#4B5563;max-height:200px;overflow:auto;
                 white-space:pre-wrap;line-height:1.55}
  .modal .foot{display:flex;justify-content:flex-end;gap:var(--sp-2);margin-top:var(--sp-5)}
  .toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%);z-index:70;
         background:rgba(24,24,40,.92);color:#fff;font-size:13px;padding:10px var(--sp-4);
         border-radius:999px;box-shadow:var(--shadow-2);animation:pop .16s ease-out;
         max-width:calc(100vw - 32px);text-align:center}
  .toast.bad{background:var(--warn)}

  @media (max-width:860px){
    .shell{flex-direction:column}
    .side{width:auto;flex:none;height:auto;position:static;flex-direction:row;
          align-items:center;gap:var(--sp-3);padding:var(--sp-2) var(--sp-3);
          overflow:hidden;border-right:0;border-bottom:1px solid var(--line)}
    .brand{padding:0;flex:none}
    .brand span{display:none}
    /* 窄屏只留图标：nav 自动换行成两排，8 个入口一屏看全，不用横向滚 */
    nav{flex:1 1 auto;min-width:0;flex-direction:row;flex-wrap:wrap;gap:2px 4px;
        justify-content:flex-start}
    nav a{padding:7px 8px}
    nav a span{display:none}
    nav a.on{box-shadow:inset 0 -3px 0 var(--accent)}
    .side-foot{margin:0;padding:0;border:0;flex:none;flex-direction:row;align-items:center}
    .side-foot .who,.side-foot .beian{display:none}
    .main{padding:var(--sp-3) var(--sp-3) 40px}
    .topbar{margin-bottom:var(--sp-3)}
    .topbar h1{font-size:var(--fs-lg)}
    .cards{grid-template-columns:repeat(auto-fit,minmax(126px,1fr));gap:var(--sp-2)}
    .card{padding:var(--sp-3)}
    .card .n{font-size:22px}
    .cols{grid-template-columns:1fr}
    .form-grid,.msg-item .form-grid{grid-template-columns:1fr}
    .panel{padding:var(--sp-3)}
    .table-wrap{max-height:none}
    .ctl,.ctl-sm{width:100%}
    .panel .hd{gap:var(--sp-2)}
    .panel .hd .ctl,.panel .hd .ctl-sm,.panel .hd .field{flex:1 1 100%}
    .feedback-content{max-width:none;min-width:180px}
    /* 窄屏历史备份：文件名与时间换两行，别把文件名截成 "...202-" */
    .history-row{flex-wrap:wrap;row-gap:2px}
    .history-row .mono{flex:1 1 100%}
    .modal{max-width:100%;padding:var(--sp-4)}
    .modal .foot .btn{flex:1}
  }
  @media screen and (min-width:861px) and (max-width:1120px){
    .main{padding:var(--sp-4) var(--sp-4) 48px}
    .cards{grid-template-columns:repeat(auto-fit,minmax(132px,1fr))}
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
    <p class="hint">输入服务器 data/.env 中的 ADMIN_KEY</p>
    <input id="key" class="field" type="password" placeholder="ADMIN_KEY" autocomplete="current-password">
    <button class="btn primary block" id="loginBtn" onclick="login()">进入控制台</button>
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
      <a data-tab="timetables" title="课表">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round"><rect x="4" y="5" width="16" height="15" rx="2"/>
          <path d="M4 10h16M9 5v-2M15 5v-2M8 14h3M13 14h3M8 17h3"/></svg><span>课表</span></a>
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
      <button class="btn ghost" onclick="logout()">退出登录</button>
      <div class="beian"><a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener noreferrer">苏ICP备2026052960号-1</a></div>
    </div>
  </aside>

  <main class="main">
    <div class="topbar">
      <div>
        <h1 id="pageTitle">概览</h1>
        <div class="sub" id="pageSub">设备、用户与内容下发情况</div>
      </div>
      <div class="actions">
        <button class="btn" id="refreshBtn" onclick="reload()">刷新</button>
      </div>
    </div>

    <!-- 概览 -->
    <section id="tab-overview">
      <div class="cards" id="cards"></div>
      <div class="panel">
        <div class="hd"><h3>近 30 天</h3></div>
        <div class="chart" id="chart"></div>
        <div class="legend">
          <span><i class="dot" style="background:#6C4FD9"></i>活跃用户</span>
          <span><i class="dot" style="background:#185FA5;opacity:.5"></i>新增设备</span>
        </div>
      </div>
      <div class="panel">
        <div class="hd"><h3>近 30 天日活（DAU）趋势</h3></div>
        <svg id="dauLine" class="line-chart" viewBox="0 0 300 120" preserveAspectRatio="none"></svg>
        <div class="legend">
          <span><i class="dot" style="background:var(--accent)"></i>日活用户（去重）</span>
        </div>
      </div>
      <div class="cols">
        <div class="panel"><div class="hd"><h3>版本分布</h3></div><div id="versions"></div></div>
        <div class="panel"><div class="hd"><h3>平台分布</h3></div><div id="platforms"></div></div>
      </div>
      <div class="panel">
        <div class="hd"><h3>版本评分</h3><span id="ratingMeta" class="muted">—</span></div>
        <div class="table-wrap short"><table id="ratingTable"></table></div>
        <p class="panel-note">同一用户在同一版本/平台只计一次；只展示版本、平台、平均分与分布，不展示学号/姓名。</p>
      </div>
    </section>

    <!-- 设备 -->
    <section id="tab-devices" class="hide">
      <div class="panel">
        <div class="hd">
          <h3>设备列表</h3>
          <input id="q" class="field ctl"
                 placeholder="搜索序列号 / 安装标识" oninput="loadDevices()">
          <button class="btn" onclick="checkDuplicates()">检查重复设备</button>
        </div>
        <div class="table-wrap"><table id="devTable"></table></div>
        <p class="panel-note">
          归档只是标记 + 撤销令牌，不删记录（上传归属还挂着它）；若那台设备装回来会自动复活。
        </p>
      </div>
    </section>

    <!-- 用户 -->
    <section id="tab-users" class="hide">
      <div class="panel">
        <div class="hd"><h3>用户列表</h3></div>
        <div class="table-wrap"><table id="userTable"></table></div>
        <p class="panel-note">只保存学号哈希与固定伪名，不存学号明文、不存姓名。</p>
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
          <h3>发布队列与完整性</h3>
          <span id="resOpsSummary" class="muted"></span>
        </div>
        <div id="resOps" class="panel-note">正在读取队列与完整性抽样…</div>
      </div>
      <div class="panel">
        <div class="hd">
          <h3>下载流量</h3>
          <span id="resTrafficTotals" class="muted"></span>
        </div>
        <div class="table-wrap short"><table id="resTrafficTable"></table></div>
        <p class="panel-note">预览不占每日下载额度；登录后按账号跨设备累计，未登录按设备累计。基础额度 5 次/天，邀请 1 人加 10 次/天（按邀人账号累计）。流量含预览与下载。</p>
      </div>
      <div class="panel">
        <div class="hd">
          <h3>资源审核</h3>
          <select id="resFilter" class="input ctl-sm" onchange="loadResources()">
            <option value="pending">待审核</option>
            <option value="approved">已通过</option>
            <option value="rejected">已拒绝</option>
            <option value="removed">已下架</option>
            <option value="">全部</option>
          </select>
        </div>
        <div class="table-wrap"><table id="resTable"></table></div>
        <p class="panel-note">通过后立即出现在客户端资源中心；拒绝仅保留审核记录，客户端不可见。</p>
      </div>
      <div class="panel">
        <div class="hd">
          <h3>资源反馈</h3>
          <select id="resReportFilter" class="input ctl-sm" onchange="loadResourceReports()">
            <option value="pending">待处理</option>
            <option value="resolved">已处理</option>
            <option value="dismissed">已忽略</option>
            <option value="">全部</option>
          </select>
        </div>
        <div class="table-wrap"><table id="resReportTable"></table></div>
        <p class="panel-note">用户反馈文件失效、内容不符、重复或侵权；可直接下架整个上传批次。</p>
      </div>
    </section>

    <!-- 在线课表 -->
    <section id="tab-timetables" class="hide">
      <div class="panel">
        <div class="hd">
          <h3>发布队列</h3>
          <span id="ttOpsSummary" class="muted"></span>
        </div>
        <div class="table-wrap short"><table id="ttQueueTable"></table></div>
        <p class="panel-note">审核通过不等于客户端可见：通过后由后台 worker 打开可见闸门。发布失败会留在队列里，可在这里重试。</p>
      </div>
      <div class="panel">
        <div class="hd">
          <h3>课表审核</h3>
          <div class="hd-tools">
            <select id="ttFilter" class="input ctl-sm" onchange="loadTimetables()">
              <option value="pending">待审核</option>
              <option value="approved">已通过</option>
              <option value="rejected">已驳回</option>
              <option value="removed">已下架</option>
              <option value="">全部</option>
            </select>
            <input id="ttKeyword" class="input ctl-sm" placeholder="班级 / 专业 / 学院"
                   oninput="ttSearchSoon()" style="width:170px">
          </div>
        </div>
        <div class="table-wrap"><table id="ttTable"></table></div>
        <p class="panel-note">客户端只能看到「已通过且已发布」的课表。驳回必须选原因，原因会回显给上传的同学。</p>
      </div>
      <div class="panel">
        <div class="hd"><h3>班级维度</h3>
          <span class="muted">跨上传者聚合，含未发布，用来查重</span></div>
        <div class="table-wrap short"><table id="ttClassTable"></table></div>
      </div>
    </section>

    <!-- 反馈 -->
    <section id="tab-feedback" class="hide">
      <div class="panel">
        <div class="hd"><h3>用户反馈</h3>
          <select id="fbFilter" class="input ctl-sm" onchange="loadFeedback()">
            <option value="">全部</option><option value="new">未读</option>
            <option value="read">已读</option><option value="done">已处理</option>
          </select>
        </div>
        <div class="table-wrap"><table id="fbTable"></table></div>
        <p class="panel-note">反馈可匿名提交；带设备令牌时显示设备序列号，不采集学号明文。</p>
      </div>
    </section>

    <!-- 风控 -->
    <section id="tab-risk" class="hide">
      <div class="panel">
        <div class="hd"><h3>风控事件</h3>
          <button class="btn" onclick="clearRisk()">全部标记已处理</button></div>
        <div class="table-wrap"><table id="riskTable"></table></div>
      </div>
    </section>

    <!-- 审计 -->
    <section id="tab-audit" class="hide">
      <div class="panel">
        <div class="hd"><h3>审计日志</h3></div>
        <div class="table-wrap"><table id="auditTable"></table></div>
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
  timetables: ['在线课表', '学生上传的班级课表，审核通过后客户端可检索'],
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

// ------------------------------------------------------------ 平台显示名
//
// devices.platform 存的是客户端上报的原始标识（android / ios / web）。
// 控制台是给人看的，直接显示英文代号不如中文直观；这里统一映射，
// 未知值原样透出，避免把新平台误标成别的意思。
const PLATFORM_LABELS = {
  android: '安卓',
  ios: 'iOS',
  web: '网页版',
};
function platformLabel(p){
  const key = String(p ?? '').trim().toLowerCase();
  if (!key) return '-';
  return PLATFORM_LABELS[key] || key;
}

// ------------------------------------------------------------ 通用 UI 助手

/** 表格空态整行：带 colspan，避免列错位；可带一行说明 */
function emptyRow(cols, text, hint){
  return `<tr><td colspan="${cols}"><div class="empty">${esc(text)}`
    + (hint ? `<small>${esc(hint)}</small>` : '') + '</div></td></tr>';
}
function loadingRow(cols){
  return `<tr><td colspan="${cols}"><div class="loading">`
    + '<span class="spinner"></span><span>正在加载…</span></div></td></tr>';
}

// 每个表格一个自增序号：快速切换筛选时丢弃过期响应，避免旧数据覆盖新数据
const LOAD_SEQ = {};
const RETRY = {};
const stale = (key, token) => LOAD_SEQ[key] !== token;

/**
 * 表格加载包装：先出 loading 行；失败渲染带「重试」的错误行，不再静默不更新。
 * opts.silent = true 时保留旧内容只做替换（用于边打字边搜索，避免整表闪一下）。
 */
async function withLoading(key, tableId, cols, fn, opts){
  const token = (LOAD_SEQ[key] = (LOAD_SEQ[key] || 0) + 1);
  RETRY[key] = () => withLoading(key, tableId, cols, fn, opts);
  const table = $(tableId);
  if (!table) return;
  const silent = !!(opts && opts.silent);
  if (!silent || !table.querySelector('tbody')) table.innerHTML = loadingRow(cols);
  else table.style.opacity = '.55';
  try{
    await fn(token);
  }catch(e){
    if (stale(key, token)) return;
    table.innerHTML = `<tr><td colspan="${cols}"><div class="load-error">`
      + `<span>加载失败：${esc(e.message || e)}</span>`
      + `<button class="btn xs" onclick="retryLoad('${key}')">重试</button></div></td></tr>`;
  }finally{
    table.style.opacity = '';
  }
}
function retryLoad(key){ const fn = RETRY[key]; if (fn) fn(); }

/** 统一二次确认弹窗（替代原生 confirm）：返回 Promise<boolean> */
function confirmDialog(opts){
  opts = opts || {};
  const title = opts.title || '确认操作';
  const okText = opts.okText || '确定';
  const cancelText = opts.cancelText || '取消';
  return new Promise(resolve => {
    const mask = document.createElement('div');
    mask.className = 'modal-mask';
    mask.innerHTML = `<div class="modal" role="dialog" aria-modal="true">
      <h4>${esc(title)}</h4>
      ${opts.message ? `<p>${esc(opts.message)}</p>` : ''}
      ${opts.detail ? `<div class="detail">${esc(opts.detail)}</div>` : ''}
      <div class="foot">
        <button class="btn" data-act="cancel">${esc(cancelText)}</button>
        <button class="btn ${opts.danger ? 'danger-solid' : 'primary'}" data-act="ok">${esc(okText)}</button>
      </div></div>`;
    const onKey = (e) => { if(e.key === 'Escape') done(false); };
    const done = (val) => {
      document.removeEventListener('keydown', onKey);
      mask.remove();
      resolve(val);
    };
    mask.addEventListener('click', e => {
      if (e.target === mask) return done(false);
      const b = e.target.closest('[data-act]');
      if (b) done(b.dataset.act === 'ok');
    });
    document.addEventListener('keydown', onKey);
    document.body.appendChild(mask);
    const ok = mask.querySelector('[data-act="ok"]');
    if (ok) ok.focus();
  });
}

let toastTimer = null;
function toast(text, bad){
  document.querySelectorAll('.toast').forEach(t => t.remove());
  const el = document.createElement('div');
  el.className = 'toast' + (bad ? ' bad' : '');
  el.textContent = text;
  document.body.appendChild(el);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.remove(); }, 2600);
}

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

// 登录按钮的忙碌态：避免重复提交、也避免点完没反应
function setLoginBusy(busy){
  const btn = $('loginBtn');
  if(!btn) return;
  btn.disabled = busy;
  btn.textContent = busy ? '验证中…' : '进入控制台';
}

async function boot(){
  setLoginBusy(true);
  $('loginErr').textContent = '';
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
    renderDauLine(s.items);
    try {
      const r = await api('/admin/metrics/ratings');
      renderRatings(r);
    } catch (_) {
      // 评分接口不可用不应把整个控制台判成“密钥不对”。
      renderRatings({overall: {}, versions: []});
    }
  }catch(e){
    $('loginErr').textContent = '密钥不对，或服务未启动';
    $('app').classList.add('hide'); $('loginView').classList.remove('hide');
  }finally{
    setLoginBusy(false);
  }
}

function renderCards(o){
  const items = [
    ['用户总数', fmt(o.users.total), '停用 ' + o.users.banned, false],
    ['设备总数', fmt(o.devices.total), '今日新增 ' + o.devices.newToday, true],
    ['DAU', fmt(o.active.dau), '今日活跃用户', true],
    ['WAU', fmt(o.active.wau), '近 7 天活跃用户', false],
    ['MAU', fmt(o.active.mau), '近 30 天活跃用户', false],
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
    `<div class="row"><span class="k">${esc(id === 'platforms' ? platformLabel(i.name) : i.name)}</span>
      <span class="track"><i style="width:${Math.round(i.value/top*100)}%"></i></span>
      <span class="v">${i.value}</span></div>`).join('');
}

function renderChart(items){
  const max = Math.max(1, ...items.map(i => i.activeDevices), ...items.map(i => i.newDevices));
  $('chart').innerHTML = items.map(i => {
    const a = Math.max(2, Math.round(i.activeDevices / max * 100));
    const b = Math.max(1, Math.round(i.newDevices / max * 100));
    return `<div class="col" title="${i.day} · 活跃用户 ${i.activeDevices} / 新增设备 ${i.newDevices}">
      <div class="bar" style="height:${a}%"></div>
      <div class="bar alt" style="height:${b}%"></div></div>`;
  }).join('');
}

/// 近 30 天日活（DAU，去重用户）折线图：纯 SVG，不引外部图表库。
function renderDauLine(items){
  const svg = $('dauLine');
  if(!svg) return;
  if(!items || !items.length){
    svg.innerHTML = '<text x="150" y="60" text-anchor="middle" fill="#9CA3AF" font-size="12">暂无数据</text>';
    return;
  }
  const W = 300, H = 120, pad = 8;
  const vals = items.map(i => Number(i.activeDevices) || 0);
  const max = Math.max(1, ...vals);
  const n = vals.length;
  const px = i => n === 1 ? W / 2 : pad + i * (W - 2 * pad) / (n - 1);
  const py = v => H - pad - (v / max) * (H - 2 * pad);
  const pts = vals.map((v, i) => `${px(i).toFixed(1)},${py(v).toFixed(1)}`);
  const area = `M${pad},${H - pad} L` + pts.join(' L') + ` L${W - pad},${H - pad} Z`;
  const dots = vals.map((v, i) =>
    `<circle class="pt" cx="${px(i).toFixed(1)}" cy="${py(v).toFixed(1)}" r="2.5">` +
    `<title>${items[i].day} · 日活 ${v}</title></circle>`).join('');
  svg.innerHTML = `<path class="area" d="${area}"/><polyline class="line" points="${pts.join(' ')}"/>${dots}`;
}

function renderRatings(data){
  const table = $('ratingTable');
  if(!table) return;
  const o = data?.overall || {};
  $('ratingMeta').textContent = o.ratings
    ? `平均 ${Number(o.average || 0).toFixed(2)} / 5 · ${o.ratings} 条 · ${o.users} 人`
    : '暂无评分';
  const rows = data?.versions || [];
  if(!rows.length){ table.innerHTML = '<tbody><tr><td class="empty">暂无评分</td></tr></tbody>'; return; }
  const head = '<thead><tr><th>版本</th><th>平台</th><th>平均分</th><th>人数</th><th>1~5 星分布</th></tr></thead>';
  const body = rows.map(r => {
    const d = r.distribution || {};
    const dist = [1,2,3,4,5].map(k => `${k}★ ${d[k] || 0}`).join(' · ');
    return `<tr><td>${esc(r.version)}</td><td>${esc(platformLabel(r.platform))}</td>
      <td><strong>${Number(r.average || 0).toFixed(2)}</strong> / 5</td>
      <td>${r.users}</td><td class="muted">${dist}</td></tr>`;
  }).join('');
  table.innerHTML = head + '<tbody>' + body + '</tbody>';
}

async function loadDevices(){
  const q = ($('q')?.value || '').trim();
  // 边打字边搜索：静默模式保留旧内容只降低透明度，避免整表反复闪 loading
  await withLoading('devices', 'devTable', 7, async (token) => {
    const d = await api('/admin/devices?query=' + encodeURIComponent(q));
    if (stale('devices', token)) return;
    if(!d.items.length){
      $('devTable').innerHTML = emptyRow(7, q ? '没有匹配的设备' : '暂无设备',
        q ? '换个序列号或安装标识再试' : '客户端注册后会出现在这里');
      return;
    }
    $('devTable').innerHTML =
      '<thead><tr><th>序列号</th><th>平台</th><th>版本</th><th>状态</th><th>会话</th>'
      + '<th>最后活跃</th><th></th></tr></thead><tbody>'
      + d.items.map(x => `<tr>
        <td class="mono">${esc(x.device_serial)}</td><td>${esc(platformLabel(x.platform))}</td>
        <td>${esc(x.app_version||'-')}</td>
        <td>${x.status==='active'?'<span class="pill ok">正常</span>'
          :x.status==='removed'?'<span class="pill">已归档</span>'
          :'<span class="pill bad">已封禁</span>'}</td>
        <td>${x.sessions}</td><td class="mono">${esc(x.last_seen)}</td>
        <td><span class="table-actions">${x.status==='removed'
          ? '<span class="muted">已归档</span>'
          : `<button class="btn sm${x.status==='active'?' danger':''}"
          onclick="toggleDevice(${x.id},'${x.status==='active'?'banned':'active'}')">
          ${x.status==='active'?'封禁':'恢复'}</button>`}</span></td></tr>`).join('')
      + '</tbody>';
  }, {silent: true});
}

async function toggleDevice(id, status){
  if (status === 'banned'){
    const ok = await confirmDialog({
      title: '封禁这台设备？',
      message: '封禁后该设备的令牌与会话立即失效，客户端无法再访问接口。',
      detail: '原状态：正常 · 操作可在设备列表里随时恢复。',
      okText: '封禁',
      danger: true,
    });
    if (!ok) return;
  }
  try{
    await api(`/admin/devices/${id}/status`, {method:'PATCH',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({status})});
    toast(status === 'banned' ? '已封禁' : '已恢复');
    loadDevices();
  }catch(e){
    toast('操作失败：' + e.message, true);
  }
}

/**
 * 检查同一用户下的重复设备。
 *
 * 成因：客户端 installation_id 以前是纯随机值、只存在本机安全存储，卸载即失效，
 * 所以用户每重装一次这里就多一台「新设备」。2026-11-01 起改为 ANDROID_ID 派生，
 * 重装不再产生新设备；历史遗留的这些需要手工归档一次。
 * 每组保留最近活跃的一台，其余归档（撤销令牌与会话，不删记录）。
 */
async function checkDuplicates(){
  let d;
  try{
    d = await api('/admin/devices/duplicates');
  }catch(e){
    toast('检查失败：' + e.message, true);
    return;
  }
  if(!d.count){
    toast('没有发现重复设备');
    return;
  }
  const lines = d.groups.map(g => {
    const keep = `${g.keep.serial}（${g.keep.lastSeen}）`;
    const extra = g.extra.map(x => `${x.serial}（${x.lastSeen}）`).join('、');
    return `用户 ${(g.alias||'').slice(0,10)}…\n  保留：${keep}\n  归档：${extra}`;
  });
  const ok = await confirmDialog({
    title: `发现 ${d.count} 台重复设备`,
    message: '只撤销令牌与会话，不删记录；归档设备装回来会自动复活。',
    detail: lines.join('\n\n'),
    okText: '归档重复设备',
    danger: true,
  });
  if(!ok) return;
  let total = 0;
  try{
    for(const g of d.groups){
      const r = await api('/admin/devices/archive', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({deviceIds: g.extra.map(x => x.id), keepId: g.keep.id})});
      total += (r.archived || []).length;
    }
    toast('已归档 ' + total + ' 台重复设备');
    loadDevices();
  }catch(e){
    toast('归档失败：' + e.message, true);
  }
}

async function loadUsers(){
  await withLoading('users', 'userTable', 5, async (token) => {
    const d = await api('/admin/users');
    if (stale('users', token)) return;
    if(!d.items.length){
      $('userTable').innerHTML = emptyRow(5, '暂无用户', '用户在客户端登录后才会建档');
      return;
    }
    $('userTable').innerHTML =
      '<thead><tr><th>伪名</th><th>显示名</th><th>设备</th><th>状态</th><th>最后登录</th></tr></thead><tbody>'
      + d.items.map(x => `<tr>
        <td class="mono">${esc((x.student_alias || '').slice(0,14))}…</td>
        <td>${esc(x.display_name||'-')}</td><td>${x.devices}</td>
        <td>${x.status==='active'?'<span class="pill ok">正常</span>':'<span class="pill bad">停用</span>'}</td>
        <td class="mono">${esc(x.last_login_at||'-')}</td></tr>`).join('') + '</tbody>';
  });
}

async function loadRisk(){
  await withLoading('risk', 'riskTable', 6, async (token) => {
    const d = await api('/admin/risk-events');
    if (stale('risk', token)) return;
    if(!d.items.length){
      $('riskTable').innerHTML = emptyRow(6, '暂无风控事件', '没有异常登录或超额设备绑定');
      return;
    }
    $('riskTable').innerHTML =
      '<thead><tr><th>类型</th><th>设备</th><th>用户</th><th>详情</th><th>时间</th><th>状态</th></tr></thead><tbody>'
      + d.items.map(x => `<tr>
        <td>${esc(x.kind)}</td><td class="mono">${esc(x.device_serial||'-')}</td>
        <td class="mono">${esc((x.student_alias||'-').slice(0,10))}</td>
        <td>${esc(x.detail)}</td><td class="mono">${esc(x.created_at)}</td>
        <td>${x.cleared_at?'<span class="pill">已处理</span>':'<span class="pill bad">待处理</span>'}</td>
        </tr>`).join('') + '</tbody>';
  });
}

async function loadAudit(){
  await withLoading('audit', 'auditTable', 4, async (token) => {
    const d = await api('/admin/audit');
    if (stale('audit', token)) return;
    if(!d.items.length){
      $('auditTable').innerHTML = emptyRow(4, '暂无记录', '管理端的写操作会自动记录在这里');
      return;
    }
    $('auditTable').innerHTML =
      '<thead><tr><th>动作</th><th>操作者</th><th>详情</th><th>时间</th></tr></thead><tbody>'
      + d.items.map(x => `<tr><td>${esc(x.action)}</td><td>${esc(x.actor)}</td>
        <td class="mono">${esc(x.detail)}</td><td class="mono">${esc(x.created_at)}</td></tr>`).join('')
      + '</tbody>';
  });
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
      <div class="history-wrap">
        <div class="field-label">历史备份 · 最近 ${d.items.length} 份</div>
        ${d.items.map(x => `<div class="history-row">
          <span class="mono">${esc(x.file)}</span>
          <span class="muted">${esc(x.savedAt)}</span>
          <button class="btn sm"
            onclick="restoreContent('${name}','${esc(x.file)}')">恢复</button>
        </div>`).join('')}
      </div>`;
  }catch(_){
    $('contentHistory').innerHTML = '';
  }
}

async function restoreContent(name, file){
  const ok = await confirmDialog({
    title: '恢复历史版本？',
    message: '恢复会覆盖当前内容，保存后立即对客户端生效。',
    detail: file,
    okText: '恢复',
    danger: true,
  });
  if (!ok) return;
  try{
    await api('/admin/content/' + name + '/restore', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({file})});
    toast('已恢复到该版本');
    await openContent(name);
  }catch(e){
    toast('恢复失败：' + e.message, true);
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
    <div class="editor-actions top"><span class="muted">共 ${MESSAGES.length} 条，前端按文件顺序展示</span>
      <button class="btn" onclick="addMessage()">新增公告</button>
    </div>
    <div id="messageList">${MESSAGES.map(messageCard).join('') || '<div class="empty">暂无公告</div>'}</div>
    <div class="editor-actions"><span id="contentMsg" class="msg"></span>
      <button class="btn primary-sm" onclick="saveMessages()">保存全部公告</button></div>`;
}

function messageCard(m, i){
  return `<div class="msg-item" data-index="${i}">
    <div class="msg-head"><b>公告 ${i + 1}</b>
      <button class="btn danger sm" onclick="removeMessage(${i})">删除</button></div>
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
  exam: '试卷 + 答案',
  exam_paper: '试卷',
  answer: '答案',
};
const RES_KIND = { textbook: '教材', exam_paper: '试卷', answer: '答案' };

function fmtBytes(n){
  n = Number(n) || 0;
  if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(0) + ' KB';
  return n + ' B';
}

function resStatus(x){
  if (x.status === 'approved') return '<span class="pill ok">已通过</span>';
  if (x.status === 'rejected') return '<span class="pill">已拒绝</span>';
  if (x.status === 'removed') return '<span class="pill bad">已下架</span>';
  return '<span class="pill bad">待审核</span>';
}

async function loadResources(){
  loadResourceTraffic();
  loadResourceReports();
  loadResourceOps();
  const status = ($('resFilter')?.value || '').trim();
  await withLoading('resources', 'resTable', 7, async (token) => {
    const d = await api('/admin/resources' + (status ? '?status=' + status : ''));
    if (stale('resources', token)) return;
    if(!d.items.length){
      $('resTable').innerHTML = emptyRow(7,
        status === 'pending' ? '没有待审核的资源' : '暂无资源上传',
        status === 'pending' ? '切换筛选看全部记录' : '用户在客户端上传后会出现在这里');
      return;
    }
    $('resTable').innerHTML =
      '<thead><tr><th>课程 / 教材</th><th>区域</th><th>备注</th><th>文件</th><th>状态</th><th>时间</th><th>操作</th></tr></thead><tbody>'
      + d.items.map(x => `<tr>
        <td><b>${esc(x.courseName)}</b><div class="who">${esc(x.bookName || '未填教材')}</div>`
        + (x.contributor ? `<div class="who">贡献者：${esc(x.contributor)}</div>` : '') + `</td>
        <td>${esc(RES_AREA[x.area] || x.area)}</td>
        <td><div class="feedback-content">${esc(x.note || '-')}</div></td>
        <td><div class="feedback-content">${x.files.map(f =>
          `${esc(RES_KIND[f.kind] || f.kind)} · ${esc(f.name)} · ${fmtBytes(f.size)} · `
          + (f.storage === 'cos' || f.storage === 'edge' ? '已上 CDN · ' : '本地 · ')
          + (f.name.toLowerCase().endsWith('.pdf')
              ? `<button class="btn xs" onclick="previewAdminFile(${f.id})">预览</button> `
              : '')
          + `<button class="btn xs" onclick="downloadAdminFile(${f.id})">下载</button>`
        ).join('<br>')}</div></td>
        <td>${resStatus(x)}</td>
        <td class="mono">${esc(x.createdAt)}${x.reviewedAt ? '<br>' + esc(x.reviewedAt) : ''}</td>
        <td><span class="table-actions">
          ${x.status !== 'approved' ? `<button class="btn sm" onclick="reviewResource(${x.id},'approved')">通过</button>` : ''}
          ${x.status !== 'rejected' ? `<button class="btn sm danger" onclick="reviewResource(${x.id},'rejected')">拒绝</button>` : ''}
          ${x.status === 'approved' ? `<button class="btn sm danger" onclick="removeResource(${x.id})">下架</button>` : ''}
        </span></td></tr>`).join('') + '</tbody>';
  });
}

async function loadResourceTraffic(){
  await withLoading('traffic', 'resTrafficTable', 6, async (token) => {
    const d = await api('/admin/resources/traffic');
    if (stale('traffic', token)) return;
    if(!d) return;
    const t = d.totals || {};
    if($('resTrafficTotals')){
      $('resTrafficTotals').textContent =
        `总请求 ${t.requests || 0} · 下载 ${t.downloads || 0} · 预览 ${t.previews || 0} · ${fmtBytes(t.bytes || 0)}`;
    }
    if(!d.devices || !d.devices.length){
      $('resTrafficTable').innerHTML = emptyRow(6, '暂无下载流量', '资源被预览或下载后开始统计');
      return;
    }
    $('resTrafficTable').innerHTML =
      '<thead><tr><th>设备</th><th>状态</th><th>下载</th><th>预览</th><th>流量</th><th>最近</th></tr></thead><tbody>'
      + d.devices.map(x => `<tr>
        <td class="mono">${esc(x.device_serial || ('#' + x.device_id))}</td>
        <td>${x.status === 'banned' ? '<span class="pill bad">封禁</span>' : '<span class="pill ok">正常</span>'}</td>
        <td>${x.downloads || 0}</td>
        <td>${x.previews || 0}</td>
        <td>${fmtBytes(x.bytes || 0)}</td>
        <td class="mono">${esc(x.last_at || '-')}</td>
      </tr>`).join('') + '</tbody>';
  });
}

async function loadResourceOps(){
  if(!$('resOps')) return;
  try{
    const q = await api('/admin/resources/queue');
    const m = await api('/admin/resources/metrics?days=7');
    const i = await api('/admin/resources/integrity?limit=30');
    const counts = q?.counts || {};
    const pending = (counts.pending || 0) + (counts.retry || 0) + (counts.running || 0);
    const failed = counts.failed || 0;
    if($('resOpsSummary')){
      $('resOpsSummary').textContent =
        `队列 待处理 ${pending} · 失败 ${failed} · 已完成 ${counts.done || 0}`;
    }
    const recent = (q?.recent || []).filter(x => x.status !== 'done').slice(0, 5);
    const metrics = (m?.metrics || []).map(x => `${x.day} ${x.metric}=${x.count}`).join(' · ') || '暂无指标';
    $('resOps').innerHTML =
      `<div>近 7 天指标：${esc(metrics)}</div>`
      + `<div>完整性抽样：检查 ${i?.checked || 0} · 本地缺失 ${i?.localMissing || 0} · Edge 缺失 ${i?.edgeMissing || 0} · COS 缺失 ${i?.cosMissing || 0} · SHA 不符 ${i?.sha256Mismatch || 0}</div>`
      + (recent.length ? `<div>待处理任务：${recent.map(x => `#${x.id} ${esc(x.action)} ${esc(x.status)} ${esc(x.last_error || '')}`).join('；')}</div>` : '<div>队列无待处理或失败任务。</div>');
  }catch(e){
    $('resOps').textContent = '运维数据读取失败：' + (e?.message || e);
  }
}

async function previewAdminFile(fileId){
  const win = window.open('', '_blank');
  try{
    const res = await fetch(`/admin/resources/files/${fileId}/preview`, {headers: auth()});
    if(!res.ok){ if(win) win.close(); toast('该文件不支持预览', true); return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    if(win) win.location = url; else window.open(url, '_blank');
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }catch(e){
    if(win) win.close();
    toast('预览失败：' + e.message, true);
  }
}

async function downloadAdminFile(fileId){
  try{
    const res = await fetch(`/admin/resources/files/${fileId}`, {headers: auth()});
    if(!res.ok){ toast('下载失败', true); return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'resource-' + fileId;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  }catch(e){
    toast('下载失败：' + e.message, true);
  }
}

async function reviewResource(id, status){
  const ok = await confirmDialog({
    title: status === 'approved' ? '通过这条资源？' : '拒绝这条资源？',
    message: status === 'approved'
      ? '通过后立即出现在客户端资源中心。'
      : '拒绝仅保留审核记录，客户端不可见。',
    okText: status === 'approved' ? '通过' : '拒绝',
    danger: status !== 'approved',
  });
  if(!ok) return;
  const reviewNote = status === 'rejected' ? (prompt('拒绝原因（可选）') || '') : '';
  try{
    await api(`/admin/resources/${id}/status`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status, reviewNote}),
    });
    toast(status === 'approved' ? '已通过' : '已拒绝');
    loadResources();
  }catch(e){
    toast('操作失败：' + e.message, true);
  }
}

async function removeResource(id){
  const ok = await confirmDialog({
    title: '下架这条资源？',
    message: '下架后客户端立即不可见，但保留上传与审核记录，可在“已下架”筛选里查看。',
    okText: '下架',
    danger: true,
  });
  if(!ok) return;
  const reviewNote = prompt('下架原因（可选）') || '';
  try{
    await api(`/admin/resources/${id}/status`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status: 'removed', reviewNote}),
    });
    toast('已下架');
    loadResources();
  }catch(e){
    toast('操作失败：' + e.message, true);
  }
}

const RES_REASON = {
  broken: '文件失效', mismatch: '内容不符', duplicate: '重复资源',
  copyright: '侵权/不宜', other: '其他',
};

function reportStatus(status){
  if (status === 'resolved') return '<span class="pill ok">已处理</span>';
  if (status === 'dismissed') return '<span class="pill">已忽略</span>';
  return '<span class="pill bad">待处理</span>';
}

async function loadResourceReports(){
  const status = ($('resReportFilter')?.value || '').trim();
  await withLoading('reports', 'resReportTable', 7, async (token) => {
    const d = await api('/admin/resource-reports' + (status ? '?status=' + status : ''));
    if (stale('reports', token)) return;
    if(!d.items.length){
      $('resReportTable').innerHTML = emptyRow(7,
        status === 'pending' ? '没有待处理的资源反馈' : '暂无资源反馈',
        '用户在资源卡片右侧菜单提交后会出现在这里');
      return;
    }
    $('resReportTable').innerHTML =
      '<thead><tr><th>资源</th><th>文件</th><th>类型</th><th>说明</th><th>状态</th><th>时间</th><th>操作</th></tr></thead><tbody>'
      + d.items.map(x => `<tr>
        <td><b>${esc(x.course_name || '-')}</b><div class="who">${esc(x.book_name || '未填教材')}</div></td>
        <td><div class="feedback-content">${esc(x.file_name || '整个批次')}</div></td>
        <td>${esc(RES_REASON[x.reason] || x.reason)}</td>
        <td><div class="feedback-content">${esc(x.detail || '-')}</div></td>
        <td>${reportStatus(x.status)}</td>
        <td class="mono">${esc(x.created_at)}</td>
        <td><span class="table-actions">
          ${x.status === 'pending' ? `<button class="btn sm" onclick="resolveReport(${x.id},'resolve')">已处理</button>` : ''}
          ${x.status === 'pending' ? `<button class="btn sm" onclick="resolveReport(${x.id},'dismiss')">忽略</button>` : ''}
          ${x.status === 'pending' ? `<button class="btn sm danger" onclick="resolveReport(${x.id},'remove')">下架</button>` : ''}
        </span></td></tr>`).join('') + '</tbody>';
  });
}

async function resolveReport(id, action){
  if(action === 'remove'){
    const ok = await confirmDialog({
      title: '下架并处理这条反馈？',
      message: '整个上传批次会立即从客户端资源中心下架。',
      okText: '下架',
      danger: true,
    });
    if(!ok) return;
  }
  const note = prompt(action === 'remove' ? '下架原因（可选）' : '处理备注（可选）') || '';
  try{
    await api(`/admin/resource-reports/${id}/resolve`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, note}),
    });
    toast(action === 'remove' ? '已下架' : action === 'dismiss' ? '已忽略' : '已处理');
    loadResources();
  }catch(e){
    toast('操作失败：' + e.message, true);
  }
}

async function loadFeedback(){
  const status = ($('fbFilter')?.value || '').trim();
  await withLoading('feedback', 'fbTable', 6, async (token) => {
    const d = await api('/admin/feedback' + (status ? '?status=' + status : ''));
    if (stale('feedback', token)) return;
    if(!d.items.length){
      $('fbTable').innerHTML = emptyRow(6, '暂无反馈', '用户提交反馈后会出现在这里');
      return;
    }
    $('fbTable').innerHTML =
      '<thead><tr><th>时间</th><th>内容</th><th>联系方式</th><th>版本</th><th>状态</th><th>操作</th></tr></thead><tbody>'
      + d.items.map(x => `<tr>
        <td class="mono">${esc(x.created_at)}</td>
        <td><div class="feedback-content">${esc(x.content)}</div></td>
        <td>${esc(x.contact || '-')}</td>
        <td class="mono">${esc(x.app_version || '-')}</td>
        <td>${fbStatus(x)}</td>
        <td><span class="table-actions">
          ${x.status === 'new' ? `<button class="btn sm" onclick="setFeedback(${x.id},'read')">标为已读</button>` : ''}
          ${x.status !== 'done' ? `<button class="btn sm" onclick="setFeedback(${x.id},'done')">已处理</button>` : ''}
          ${x.status === 'done' ? `<button class="btn sm" onclick="setFeedback(${x.id},'read')">重新打开</button>` : ''}
        </span></td></tr>`).join('') + '</tbody>';
  });
}

function fbStatus(x){
  if (x.status === 'done') return '<span class="pill ok">已处理</span>';
  if (x.status === 'read') return '<span class="pill">已读</span>';
  return '<span class="pill bad">未读</span>';
}

async function setFeedback(id, status){
  try{
    await api(`/admin/feedback/${id}/status`, {method:'PATCH',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({status})});
    toast(status === 'done' ? '已标记为已处理' : '状态已更新');
    loadFeedback();
  }catch(e){
    toast('操作失败：' + e.message, true);
  }
}

async function clearRisk(){
  const ok = await confirmDialog({
    title: '全部标记为已处理？',
    message: '所有待处理的风控事件都会被清空，此操作不可撤销。',
    detail: '只清空待处理状态，风控事件记录本身仍然保留在库里。',
    okText: '全部标记已处理',
    danger: true,
  });
  if (!ok) return;
  try{
    await api('/admin/risk-events', {method:'DELETE'});
    toast('已全部标记为已处理');
    loadRisk();
  }catch(e){
    toast('操作失败：' + e.message, true);
  }
}

function switchTab(name){
  CURRENT = name;
  document.querySelectorAll('nav a').forEach(a => {
    const on = a.dataset.tab === name;
    a.classList.toggle('on', on);
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  ['overview','devices','users','content','resources','timetables','feedback','risk','audit'].forEach(k =>
    $('tab-' + k).classList.toggle('hide', k !== name));
  const [title, sub] = TITLES[name] || ['', ''];
  $('pageTitle').textContent = title;
  $('pageSub').textContent = sub;
  document.title = title + ' · 掌上锡院控制台';
  reload();
}

function reload(){
  const loaders = {
    devices: loadDevices, users: loadUsers, content: loadContent,
    resources: loadResources,
    timetables: loadTimetables,
    feedback: loadFeedback, risk: loadRisk, audit: loadAudit,
  };
  const fn = loaders[CURRENT];
  const btn = $('refreshBtn');
  if (!fn){ boot(); if (btn) flashBusy(btn, '刷新中…'); return; }
  if (btn){ btn.disabled = true; btn.textContent = '刷新中…'; }
  Promise.resolve(fn()).finally(() => {
    if (btn){ btn.disabled = false; btn.textContent = '刷新'; }
  });
}

// 概览走 boot()，给刷新按钮一个短暂的忙碌反馈，避免点了像没反应
async function flashBusy(btn, text){
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = text;
  await new Promise(r => setTimeout(r, 500));
  btn.disabled = false; btn.textContent = old;
}

// ---------------------------------------------------------------- 在线课表
const TT_STATUS = {
  pending: '待审核', approved: '已通过',
  rejected: '已驳回', removed: '已下架',
};
const TT_REASON = {
  mismatch_with_campus: '与校方课表不一致',
  incomplete_info: '信息不完整',
  duplicate: '重复上传',
  personal_info: '含个人信息',
  wrong_class: '班级填写有误',
  other: '其他',
};
const TT_SOURCE = { local_table: '本机课程表', local_file: '导出的文件' };
const TT_DAY = ['', '周一', '周二', '周三', '周四', '周五', '周六', '周日'];

function ttPill(x){
  let cls = 'pill';
  if (x.status === 'approved') cls = 'pill ok';
  else if (x.status === 'rejected' || x.status === 'removed') cls = 'pill bad';
  let html = `<span class="${cls}">${esc(TT_STATUS[x.status] || x.status)}</span>`;
  if (x.status === 'approved'){
    html += x.published
      ? '<div class="who"><span class="pill ok">已发布</span></div>'
      : '<div class="who"><span class="pill bad">待发布</span></div>';
  }
  return html;
}

function ttConsistency(c){
  if(!c || (!c.matched && !c.total)) return '<span class="who">未比对</span>';
  const all = c.matched === c.total;
  return `<span class="${all ? 'pill ok' : 'pill bad'}">`
    + (all ? '全一致' : `${c.matched}/${c.total}`) + '</span>';
}

let ttTimer = null;
function ttSearchSoon(){
  clearTimeout(ttTimer);
  ttTimer = setTimeout(() => loadTimetables(), 320);
}

async function loadTimetables(){
  loadTtQueue();
  loadTtClasses();
  const status = ($('ttFilter')?.value || '').trim();
  const kw = ($('ttKeyword')?.value || '').trim();
  await withLoading('timetables', 'ttTable', 7, async (token) => {
    const qs = new URLSearchParams();
    if(status) qs.set('status', status);
    if(kw) qs.set('keyword', kw);
    const q = qs.toString();
    const d = await api('/admin/timetables' + (q ? '?' + q : ''));
    if (stale('timetables', token)) return;
    const items = (d.data && d.data.items) || [];
    if(!items.length){
      $('ttTable').innerHTML = emptyRow(7,
        status === 'pending' ? '没有待审核的课表' : '暂无课表上传',
        status === 'pending' ? '切换筛选看全部记录' : '同学在客户端上传后会出现在这里');
      return;
    }
    $('ttTable').innerHTML =
      '<thead><tr><th>班级</th><th>学院 / 专业</th><th>提交者</th>'
      + '<th>课程 · 一致度</th><th>状态</th><th>时间</th><th>操作</th></tr></thead><tbody>'
      + items.map(x => `<tr>
        <td><b>${esc(x.bj)}</b><div class="who">v${x.version} · ${esc(x.xnm)}-${esc(x.xqm)} · ${esc(TT_SOURCE[x.source] || x.source)}</div></td>
        <td>${esc(x.jgmc)}<div class="who">${esc(x.zymc)}</div></td>
        <td>${esc(x.uploader_name)}${x.personal_flag ? '<div class="who"><span class="pill bad">含疑似个人信息</span></div>' : ''}</td>
        <td>${x.course_count} 门<div class="who">${ttConsistency(x.consistency)}</div></td>
        <td>${ttPill(x)}</td>
        <td class="mono">${esc(x.created_at)}${x.reviewed_at ? '<br>' + esc(x.reviewed_at) : ''}</td>
        <td><span class="table-actions">
          <button class="btn xs" onclick="ttDetail(${x.id})">详情</button>
          ${x.status !== 'approved' ? `<button class="btn xs" onclick="ttReview(${x.id},'approved')">通过</button>` : ''}
          ${x.status !== 'rejected' ? `<button class="btn xs danger" onclick="ttReject(${x.id})">驳回</button>` : ''}
          ${x.status !== 'removed' && x.status !== 'pending' ? `<button class="btn xs danger" onclick="ttReview(${x.id},'removed')">下架</button>` : ''}
        </span></td></tr>`).join('') + '</tbody>';
  });
}

async function loadTtQueue(){
  await withLoading('ttQueue', 'ttQueueTable', 7, async (token) => {
    const d = await api('/admin/timetables/queue');
    if (stale('ttQueue', token)) return;
    const q = (d && d.data) || {};
    if($('ttOpsSummary')){
      $('ttOpsSummary').textContent =
        `待处理 ${q.pending || 0} · 进行中 ${q.running || 0} · 完成 ${q.done || 0} · 失败 ${q.failed || 0}`;
    }
    const items = q.items || [];
    if(!items.length){
      $('ttQueueTable').innerHTML = emptyRow(7, '队列是空的', '审核通过或下架时才会有任务');
      return;
    }
    $('ttQueueTable').innerHTML =
      '<thead><tr><th>#</th><th>班级</th><th>动作</th><th>状态</th>'
      + '<th>尝试</th><th>上次错误</th><th>操作</th></tr></thead><tbody>'
      + items.map(j => `<tr>
        <td class="mono">${j.id}</td>
        <td>${esc(j.class || '-')}<div class="who">${esc(j.org || '')}</div></td>
        <td>${j.action === 'publish' ? '发布' : '下架'}</td>
        <td>${j.status === 'failed' ? '<span class="pill bad">失败</span>'
              : j.status === 'done' ? '<span class="pill ok">完成</span>'
              : j.status === 'running' ? '<span class="pill">进行中</span>'
              : '<span class="pill bad">等待</span>'}</td>
        <td class="mono">${j.attempts}</td>
        <td><div class="feedback-content">${esc(j.lastError || '-')}</div></td>
        <td>${j.status === 'failed'
              ? `<button class="btn xs" onclick="ttRetryJob(${j.id})">重试</button>` : '-'}</td>
        </tr>`).join('') + '</tbody>';
  });
}

async function loadTtClasses(){
  await withLoading('ttClass', 'ttClassTable', 5, async (token) => {
    const d = await api('/admin/timetables/classes');
    if (stale('ttClass', token)) return;
    const items = (d.data && d.data.classes) || [];
    if(!items.length){
      $('ttClassTable').innerHTML = emptyRow(5, '还没有任何班级上传过课表');
      return;
    }
    $('ttClassTable').innerHTML =
      '<thead><tr><th>班级</th><th>学院 / 专业</th><th>上传</th>'
      + '<th>已发布</th><th>版本</th></tr></thead><tbody>'
      + items.map(c => `<tr>
        <td><b>${esc(c.bj)}</b><div class="who">${esc(c.xnm)}-${esc(c.xqm)}</div></td>
        <td>${esc(c.jgmc)}<div class="who">${esc(c.zymc)}</div></td>
        <td class="mono">${c.upload_count}</td>
        <td class="mono">${c.published_count}</td>
        <td>${c.versions.map(v =>
          `v${v.version} ${esc(TT_STATUS[v.status] || v.status)}${v.published ? '·已发布' : ''}`
          + `<div class="who">${esc(v.uploader_name)} · ${esc(v.created_at)}</div>`
        ).join('')}</td>
        </tr>`).join('') + '</tbody>';
  });
}

function ttModal(html, maxWidth){
  const mask = document.createElement('div');
  mask.className = 'modal-mask';
  mask.innerHTML = `<div class="modal" role="dialog" aria-modal="true" `
    + `style="max-width:${maxWidth || 720}px">${html}</div>`;
  const close = () => {
    document.removeEventListener('keydown', onKey);
    mask.remove();
  };
  const onKey = (e) => { if(e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);
  mask.addEventListener('click', e => { if(e.target === mask) close(); });
  document.body.appendChild(mask);
  return { mask, close };
}

function ttCourseLine(c, mark){
  const slot = c.start_slot === c.end_slot
    ? `第${c.start_slot}节` : `第${c.start_slot}-${c.end_slot}节`;
  const bits = [TT_DAY[c.weekday] || `周${c.weekday}`, slot, c.name];
  if(c.teacher) bits.push(c.teacher);
  if(c.classroom) bits.push('@' + c.classroom);
  if(c.weeks) bits.push(c.weeks);
  const color = mark === 'diff' ? '#B45309'
    : mark === 'extra' ? '#047857'
    : mark === 'missing' ? '#B91C1C' : 'inherit';
  const tag = mark === 'diff' ? ' [有出入]'
    : mark === 'extra' ? ' [多出来的]'
    : mark === 'missing' ? ' [校方有、这份没有]' : '';
  return `<div style="color:${color}">${esc(bits.join(' · '))}${tag}</div>`;
}

async function ttDetail(id){
  let d;
  try{
    d = await api('/admin/timetables/' + id);
  }catch(e){
    toast('读取失败：' + e.message, true);
    return;
  }
  const x = (d.data && d.data.upload) || {};
  const courses = (d.data && d.data.courses) || [];
  const diff = (d.data && d.data.diff) || { items: [] };
  const marks = {};
  (diff.items || []).forEach(i => {
    if(i.status !== 'same' && i.uploaded) marks[i.uploaded.name || i.name] = i.status;
  });
  const diffBlock = (diff.items && diff.items.length)
    ? `<p style="margin-top:12px"><b>与校方基线比对</b>：一致 ${diff.matched} / 共 ${diff.total}</p>`
      + '<div class="detail">'
      + (diff.items || []).map(i => ttCourseLine(
          i.uploaded || i.baseline || {}, i.status)).join('')
      + '</div>'
    : '<p style="margin-top:12px" class="who">这条上传没有带校方基线，无法比对一致度。</p>';
  const html = `
    <h4>${esc(x.bj)} · v${esc(x.version)}</h4>
    <p>${esc(x.jgmc)} · ${esc(x.zymc)}　${esc(x.xnm)}-${esc(x.xqm)}</p>
    <p class="who">提交者 ${esc(x.uploader_name)} · 来源 ${esc(TT_SOURCE[x.source] || x.source)}
      · ${x.course_count} 门 · ${esc(x.created_at)}</p>
    ${x.note ? `<div class="detail">${esc(x.note)}</div>` : ''}
    ${x.review_note ? `<p style="margin-top:10px" class="who">审核备注：${esc(x.review_note)}</p>` : ''}
    ${diffBlock}
    <p style="margin-top:12px"><b>提交的课程（${courses.length} 门）</b></p>
    <div class="detail" style="max-height:260px">${courses.map(c => ttCourseLine(c, marks[c.name])).join('')}</div>
    <div class="foot"><button class="btn" data-act="close">关闭</button></div>`;
  const m = ttModal(html);
  m.mask.querySelector('[data-act="close"]').addEventListener('click', m.close);
}

async function ttReview(id, status){
  const approved = status === 'approved';
  const ok = await confirmDialog({
    title: approved ? '通过这份课表？' : '下架这份课表？',
    message: approved
      ? '通过后由后台任务发布到客户端，同学就能搜到了。'
      : '下架后客户端立即不可见，但保留上传与审核记录。',
    okText: approved ? '通过' : '下架',
    danger: !approved,
  });
  if(!ok) return;
  try{
    const d = await api(`/admin/timetables/${id}/status`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(approved ? {status} : {status, reviewNote: ''}),
    });
    const queued = d.data && d.data.queued;
    toast(approved ? (queued ? '已通过，正在发布' : '已通过') : '已下架');
    loadTimetables();
  }catch(e){
    toast('操作失败：' + e.message, true);
  }
}

function ttReject(id){
  const opts = Object.keys(TT_REASON).map(k =>
    `<option value="${k}">${esc(TT_REASON[k])}</option>`).join('');
  const html = `
    <h4>驳回这份课表</h4>
    <p>原因会回显给上传的同学，他改完可以重新提交。</p>
    <div style="margin-top:14px">
      <label class="who">驳回原因</label>
      <select id="ttReason" class="input" style="width:100%;margin-top:6px">${opts}</select>
    </div>
    <div style="margin-top:10px">
      <label class="who">补充说明（选填，最多 500 字）</label>
      <textarea id="ttNote" class="input" rows="3" maxlength="500"
        style="width:100%;margin-top:6px;resize:vertical"
        placeholder="例如：第 7-8 节的教室填错了"></textarea>
    </div>
    <div class="foot">
      <button class="btn" data-act="cancel">取消</button>
      <button class="btn danger-solid" data-act="ok">确认驳回</button>
    </div>`;
  const m = ttModal(html, 460);
  m.mask.querySelector('[data-act="cancel"]').addEventListener('click', m.close);
  m.mask.querySelector('[data-act="ok"]').addEventListener('click', async () => {
    const reason = m.mask.querySelector('#ttReason').value;
    const note = (m.mask.querySelector('#ttNote').value || '').trim();
    m.close();
    try{
      await api(`/admin/timetables/${id}/status`, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: 'rejected', reason, reviewNote: note}),
      });
      toast('已驳回');
      loadTimetables();
    }catch(e){
      toast('操作失败：' + e.message, true);
    }
  });
}

async function ttRetryJob(id){
  try{
    await api(`/admin/timetables/queue/${id}/retry`, {method: 'POST'});
    toast('已重新排队');
    loadTimetables();
  }catch(e){
    toast('重试失败：' + e.message, true);
  }
}

$('nav').addEventListener('click', e => {
  const a = e.target.closest('a[data-tab]');
  if (a) switchTab(a.dataset.tab);
});

if(KEY) boot();
</script>
</body></html>"""
