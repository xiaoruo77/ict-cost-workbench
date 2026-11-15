/* 资料检索与问答 —— React 18（UMD，本地 vendor）+ htm 模板
 *
 * ⚠️ 本文件**面向最终使用者**。
 *    不出现内部实现术语：文件路径、脚本名、数据库、原始资料目录结构……
 *    用户只需要知道「数字可不可信、口径是什么」，不需要知道它怎么算出来的。
 *
 * 前端无构建步骤：htm 提供 JSX 式语法但无需编译。
 */
const { useState, useEffect, useRef } = React;
const html = htm.bind(React.createElement);

/* =============================================================================
 * 登录态
 *
 * 会话由**服务端持有**，浏览器只拿一个 HttpOnly Cookie —— JS 读不到它，
 * 所以也不存在把它存进 localStorage 的问题（上一版正是栽在
 * 「localStorage 里存了脏值 → 请求头出现非 ISO-8859-1 字符」）。
 *
 * CSRF：写操作带 `X-CSRF-Token`，值从**非 HttpOnly** 的 `lw_csrf` cookie 里读。
 * 这是 double-submit 的标准做法。
 * ========================================================================== */

function getCookie(name) {
  const m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return m ? decodeURIComponent(m[1]) : "";
}

/* 写操作的请求头：JSON + CSRF。
 * ⚠️ 上传（multipart）**不能**用它——`Content-Type` 要让浏览器自己带 boundary。 */
function writeHeaders(extra) {
  return Object.assign({
    "Content-Type": "application/json; charset=utf-8",
    "X-CSRF-Token": getCookie("lw_csrf"),
  }, extra || {});
}

/* 统一的 API 调用：自动带同源 cookie + CSRF，错误归一成 `{_error}`。
 *
 * ⚠️ `credentials: "same-origin"` 一句都不能少。少了它，**登录态完全不起作用**，
 *    而症状极具迷惑性：接口看着"能通"，服务端却始终认为你没登录。 */
async function api(path, { method = "GET", body } = {}) {
  method = method.toUpperCase();
  const opts = { method, credentials: "same-origin" };
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    opts.headers = { "X-CSRF-Token": getCookie("lw_csrf") };
  }
  if (body !== undefined) {
    opts.headers = writeHeaders();
    opts.body = JSON.stringify(body);
  }
  let r;
  try { r = await fetch(path, opts); }
  catch (e) { return { _error: String(e) }; }
  let d = null;
  try { d = await r.json(); } catch (e) { d = null; }
  if (!r.ok) {
    const detail = d && (d.detail || d.message || d.error);
    return { _error: typeof detail === "string" ? detail : (detail ? JSON.stringify(detail) : r.statusText), _status: r.status };
  }
  return d || {};
}

/* ---------------- 数据获取 ---------------- */
function useApi(path) {
  const [s, set] = useState({ loading: true, data: null, error: null });
  useEffect(() => {
    let alive = true;
    set({ loading: true, data: null, error: null });
    fetch(path, { credentials: "same-origin", headers: { "X-CSRF-Token": getCookie("lw_csrf") } })
      .then(async r => {
        const data = await r.json();
        if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || r.statusText));
        return data;
      })
      .then(d => alive && set({ loading: false, data: d, error: null }))
      .catch(e => alive && set({ loading: false, data: null, error: String(e) }));
    return () => { alive = false; };
  }, [path]);
  return s;
}

/* ---------------- 工具 ---------------- */
const fmt = (v, nd = 2) =>
  (v === null || v === undefined || Number.isNaN(v)) ? "—" : Number(v).toFixed(nd);
const cls = v => v > 0 ? "up" : (v < 0 ? "down" : "flat");
const ARROW = v => v > 0 ? "▲" : (v < 0 ? "▼" : "—");

/* 默认隐藏 / 加载占位 */
const Loading = () => html`<div class="skel skel-card"></div>`;
const ErrorBox = ({ msg }) => html`<div class="msg err">数据加载失败：${msg}</div>`;

/* ---------------- 滚动渐入 ----------------
 *
 * ⚠️ **可见性绝不能依赖这段 JS。**
 *    这的写法是：元素在 HTML 里**本来就是可见的**（`.reveal{opacity:1}`），
 *    这段代码只是**额外**给它加上 `.js-reveal` 这个类，动画才开始生效。
 *    所以 JS 没跑、浏览器不支持 IO、或脚本报错 —— 内容照样看得见，
 *    只是没有淡入效果。**反过来写（默认隐藏、JS 负责显示）就会开天窗。**
 */
function useReveal() {
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    el.classList.add("js-reveal");
    const io = new IntersectionObserver((es) => {
      es.forEach(e => {
        if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.04 });
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return ref;
}

/* 包一层：滚动到视口时淡入 */
function Reveal({ children, className = "" }) {
  const ref = useReveal();
  return html`<div ref=${ref} class=${"reveal " + className}>${children}</div>`;
}

/* =============================================================================
 * 注册 / 登录
 *
 * 为什么是**就地一张卡片**而不是整页跳转：
 *   首页的图表、图谱、数据说明都是**公开数据**，一上来就拦到登录页
 *   会把这些全挡掉——看不到东西的人不会想注册。
 *   所以未登录照常展示，只在**需要个人身份的地方**（问答/上传/导出/整改）
 *   就地展开这张卡片。
 * ========================================================================== */
function AuthPanel({ onDone, hint = "" }) {
  const [mode, setMode] = useState("login");     // login | register
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [p2, setP2] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const submit = async (e) => {
    e.preventDefault();
    setErr("");
    if (!u.trim() || !p) { setErr("请填写用户名和密码"); return; }
    if (mode === "register") {
      if (p.length < 8) { setErr("密码至少 8 位"); return; }
      if (p !== p2) { setErr("两次输入的密码不一致"); return; }
    }
    setBusy(true);
    const r = await api("/api/auth/" + (mode === "register" ? "register" : "login"),
                        { method: "POST", body: { username: u.trim(), password: p } });
    setBusy(false);
    if (r._error) { setErr(r._error); return; }
    setP(""); setP2("");
    if (onDone) onDone(r);
  };

  return html`
    <div class="authcard">
      <div class="auth-tabs">
        <button class=${"auth-tab" + (mode === "login" ? " on" : "")} type="button"
                onClick=${() => { setMode("login"); setErr(""); }}>登录</button>
        <button class=${"auth-tab" + (mode === "register" ? " on" : "")} type="button"
                onClick=${() => { setMode("register"); setErr(""); }}>注册</button>
      </div>

      ${hint ? html`<p class="auth-hint">${hint}</p>` : null}

      <form class="authform" onSubmit=${submit}>
        <label>
          <span>用户名</span>
          <input value=${u} autoComplete="username" disabled=${busy}
                 placeholder="3–32 个字符，中文 / 字母 / 数字 / 下划线"
                 onInput=${e => setU(e.target.value)} />
        </label>
        <label>
          <span>密码</span>
          <input type="password" value=${p} disabled=${busy}
                 autoComplete=${mode === "register" ? "new-password" : "current-password"}
                 placeholder=${mode === "register" ? "至少 8 位" : "输入密码"}
                 onInput=${e => setP(e.target.value)} />
        </label>
        ${mode === "register" ? html`
          <label>
            <span>确认密码</span>
            <input type="password" value=${p2} disabled=${busy} autoComplete="new-password"
                   placeholder="再输一遍" onInput=${e => setP2(e.target.value)} />
          </label>` : null}

        ${err ? html`<div class="alert">⚠️ ${err}</div>` : null}

        <button class="authsubmit" type="submit" disabled=${busy}>
          ${busy ? "处理中…" : (mode === "register" ? "注册并登录" : "登录")}
        </button>
      </form>

      <p class="auth-foot">
        ${mode === "register"
          ? "注册后你的资料与对话存在独立的数据区，其他成员看不到。"
          : "没有账号？点上方「注册」。"}
      </p>
    </div>`;
}


/* 受限功能的统一提示 —— 未登录时的各处入口都用它。
 *
 * ⚠️ 为什么统一：以前每个组件各自内嵌一张登录卡，四处维护、文案还各不相同。
 *    现在登录只在登录页做，这里只负责**说明原因 + 把人送过去**。 */
function LoginGate({ what, onGoLogin }) {
  return html`
    <div class="logingate">
      <div class="lg-icon">🔒</div>
      <div class="lg-text">${what}需要登录后使用</div>
      <button class="chip" onClick=${onGoLogin}>去登录</button>
    </div>`;
}


/* =============================================================================
 * 登录页（独立整屏）
 *
 * 它是**入口**：未登录时落在这一页。但**不强制**——
 * 「以访客身份浏览」可以进主页面看图表/图谱/数据说明（那些是公司公开数据）。
 * 拦死的话，第一次来的人看不到任何东西，不会想注册。
 * ========================================================================== */
function LoginPage({ onAuthed, onGuest, notice = "" }) {
  return html`
    <div class="loginpage">
      <div class="loginbox">
        <div class="login-brand">
          <div class="mark">析</div>
          <div>
            <h1>通用成本智能分析平台</h1>
            <div class="sub">多行业成本核算 · 数据与结果可追溯</div>
          </div>
        </div>

        ${notice ? html`<div class="alert" style=${{ marginBottom: 16 }}>${notice}</div>` : null}

        <p class="login-lead">登录后接入成本数据、运行核算，保存并追溯每份报告。</p>

        <${AuthPanel} onDone=${onAuthed} />

        <button class="linkbtn login-guest" onClick=${onGuest}>
          以访客身份查看平台
        </button>
      </div>
    </div>`;
}


/* =============================================================================
 * 个人中心
 *
 * 一屏三块：账号信息 / 我的历史对话 / 我的知识库。
 * 数据走 `/api/profile` **一次取回**（避免开页三个请求，任一慢就半残）。
 * ========================================================================== */
function Profile({ me, onAuthed, onLoggedOut }) {
  /* 用 path 上挂一个自增 key 来触发重新拉取——`useApi` 的 useEffect
   * 依赖 `path`，换个字符串就会重跑，不必给它加 refresh API。 */
  const [tick, setTick] = useState(0);
  const [actionError, setActionError] = useState("");
  const { data, loading, error } = useApi(`/api/profile?t=${tick}`);

  /* 打开某个会话：跳回首页并选中它。
   * ⚠️ 不改「首页」组件的内部 state（那是 HeroAsk 的私有状态），
   *    而是把目标会话 id 写进 hash query，由 HeroAsk 读进来。
   *    这样两个组件不必互相持有对方的 setter。 */
  const openConv = (id) => {
    location.hash = `home?conv=${id}`;
  };

  const delConv = async (id) => {
    if (!window.confirm("删除这个对话？该对话的所有消息会一并删除，不可恢复。")) return;
    const r = await api(`/api/conversations/${id}`, { method: "DELETE" });
    if (r._error) { setActionError("删除失败：" + r._error); return; }
    setActionError("");
    setTick(t => t + 1);     // 重新拉 profile，刷新列表
  };

  if (!me) {
    return html`<div class="card">
      <h2>个人中心</h2>
      <p class="card-sub">请先登录。</p>
      <${AuthPanel} onDone=${onAuthed} />
    </div>`;
  }
  if (loading) return html`<${Loading} />`;
  if (error) return html`<${ErrorBox} msg=${error} />`;

  const ws = data.workspace || { raw: [], wiki: [], counts: {} };
  const convs = data.conversations || [];

  const logout = async () => {
    await api("/api/auth/logout", { method: "POST", body: {} });
    if (onLoggedOut) onLoggedOut();
  };

  return html`
    <div class="card">
      <h2>个人中心</h2>
      <p class="card-sub">
        账号信息、历史对话与个人知识库。资料与对话存在你自己的数据区，其他成员看不到。
      </p>
      <table class="tbl">
        <tbody>
          <tr><td class="strong" style=${{ width: 140 }}>用户名</td><td>${data.external_id}</td></tr>
          <tr><td class="strong">显示名</td><td>${data.name || data.external_id}</td></tr>
          <tr><td class="strong">角色</td><td>${data.role || "user"}</td></tr>
          <tr><td class="strong">个人数据区</td><td>${data.user_db}</td></tr>
        </tbody>
      </table>
      <div class="toolbar" style=${{ marginTop: 20 }}>
        <button class="chip" onClick=${logout}>退出登录</button>
        <button class="chip" onClick=${() => onLoggedOut && onLoggedOut(true)}>切换账号</button>
      </div>
    </div>

    <div class="card">
      <h2>我的历史对话
        <span class="hint">${convs.length} 个会话</span>
      </h2>
      ${actionError ? html`<div class="alert">${actionError}</div>` : null}
      ${data.conversations_error
        ? html`<div class="alert">⚠️ ${data.conversations_error}</div>`
        : convs.length ? html`
          <table class="tbl">
            <thead><tr><th>会话</th><th>建立时间</th></tr></thead>
            <tbody>${convs.map(c => html`<tr key=${c.id}>
              <td>${c.title || "未命名"}</td>
              <td class="nowrap">${String(c.created_at).slice(0, 19)}</td>
              <td class="nowrap">${c.n_msg || 0} 条</td>
              <td class="nowrap">
                <button class="chip" type="button"
                        onClick=${() => openConv(c.id)}
                        title="在首页打开这个对话">打开</button>
                <button class="chip" type="button"
                        onClick=${() => delConv(c.id)}
                        title="删除这个对话">删除</button>
              </td>
            </tr>`)}</tbody>
          </table>`
        : html`<div class="empty">
            <div class="big">还没有对话</div>
            <div>在首页「智能文档」里问一句，对话会出现在这里</div>
          </div>`}
    </div>

    <div class="card">
      <h2>我的知识库
        <span class="hint">资料 ${ws.counts.raw || 0} 份 · 词条 ${ws.counts.wiki || 0} 篇${
          ws.counts.inbox ? html` · 待编译 ${ws.counts.inbox} 份` : null}</span>
      </h2>
      ${data.workspace_error
        ? html`<div class="alert">⚠️ ${data.workspace_error}</div>`
        : (!((ws.inbox || []).length + ws.raw.length + ws.wiki.length)) ? html`
          <div class="empty">
            <div class="big">还没有个人资料</div>
            <div>在首页往下滑，把文件拖进来即可</div>
          </div>`
        : html`
          ${(ws.inbox || []).length ? html`
            <h3 class="ws-h">待编译 <span class="hint">刚上传、还没跑摄入</span></h3>
            <table class="tbl"><tbody>${ws.inbox.map(f => html`<tr key=${f.path}>
              <td>${f.path}</td><td class="num">${(f.size / 1024).toFixed(1)} KB</td>
            </tr>`)}</tbody></table>` : null}
          ${ws.raw.length ? html`
            <h3 class="ws-h">资料</h3>
            <table class="tbl"><tbody>${ws.raw.map(f => html`<tr key=${f.path}>
              <td>${f.path}</td><td class="num">${(f.size / 1024).toFixed(1)} KB</td>
            </tr>`)}</tbody></table>` : null}
          ${ws.wiki.length ? html`
            <h3 class="ws-h">词条</h3>
            <table class="tbl"><tbody>${ws.wiki.map(f => html`<tr key=${f.path}>
              <td>${f.path}</td><td class="num">${(f.size / 1024).toFixed(1)} KB</td>
            </tr>`)}</tbody></table>` : null}`}
    </div>`;
}








const CHART_FONT = '"Inter","Segoe UI","Microsoft YaHei",system-ui,sans-serif';
const C_INK = "#1C1917", C_MUTED = "#78716C", C_FAINT = "#A8A29E", C_LINE = "#E7E5E4";

/* 统一的提示框样式（白色圆角卡片 + 柔影）*/
const TOOLTIP = {
  trigger: "axis",
  backgroundColor: "#fff",
  borderColor: C_LINE,
  borderWidth: 1,
  padding: [10, 14],
  textStyle: { color: C_INK, fontSize: 13, fontFamily: CHART_FONT },
  extraCssText: "border-radius:12px;box-shadow:0 4px 20px rgba(28,25,23,.10);",
};

function EChart({ option, height = 300, deps = [], title = "" }) {
  const ref = useRef(null);
  const inst = useRef(null);
  const [hover, setHover] = useState(false);

  useEffect(() => {
    if (!ref.current || !window.echarts) return;
    inst.current = window.echarts.init(ref.current, null, { renderer: "canvas" });
    const onResize = () => inst.current && inst.current.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      if (inst.current) { inst.current.dispose(); inst.current = null; }
    };
  }, []);

  // 数据变化时只 setOption，不重建实例（避免闪烁）
  useEffect(() => {
    if (inst.current && option) inst.current.setOption(option, true);
  }, deps);

  /* ---- 导出 PNG ----
   * ECharts 用 canvas 渲染，`getDataURL()` 直接拿到图。
   * ⚠️ 必须显式传 `backgroundColor`——否则导出的 PNG 背景透明，
   *    贴进 Word/PPT 后深色文字压在深色底上会看不清。
   */
  const save = () => {
    if (!inst.current) return;
    const url = inst.current.getDataURL({
      type: "png", pixelRatio: 2, backgroundColor: "#FFFFFF",
    });
    const a = document.createElement("a");
    a.href = url;
    a.download = (title || "图表").replace(/[\\/:*?"<>|]/g, "_") + ".png";
    document.body.appendChild(a); a.click(); a.remove();
  };

  return html`
    <div class="chartwrap" onMouseEnter=${() => setHover(true)}
         onMouseLeave=${() => setHover(false)}>
      <div ref=${ref} style=${{ width: "100%", height: height + "px" }}></div>
      ${title ? html`
        <button class="chart-dl" title="导出为图片" onClick=${save}
                style=${{ opacity: hover ? 1 : 0 }}>⬇ 图片</button>` : null}
    </div>`;
}

/* ---- 折线图：逐月成本走势 ---- */
function LineChart({ series, colors, height = 320, title = "" }) {
  if (!series.length) return html`<div class="empty">该时间段暂无数据</div>`;
  const months = series.map(s => s.月份.slice(5) + "月");

  const option = {
    animationDuration: 420,
    // ⚠️ legend 放 bottom:0 会与 X 轴标签重叠（实测"单位成本"压住了"03月"）。
    //    改为 legend 置顶 + grid 留出上边距，两者各占一行。
    grid: { left: 8, right: 16, top: 42, bottom: 8, containLabel: true },
    tooltip: {
      ...TOOLTIP,
      valueFormatter: v => (v == null ? "—" : Number(v).toFixed(2) + " 元/盒"),
    },
    legend: {
      top: 0, left: 0, icon: "roundRect", itemWidth: 10, itemHeight: 10, itemGap: 20,
      textStyle: { color: C_MUTED, fontSize: 13, fontFamily: CHART_FONT },
    },
    xAxis: {
      type: "category", data: months, boundaryGap: false,
      axisLine: { lineStyle: { color: C_LINE } },
      axisTick: { show: false },
      axisLabel: { color: C_FAINT, fontSize: 12, fontFamily: CHART_FONT },
    },
    yAxis: {
      type: "value", scale: true,
      splitLine: { lineStyle: { color: "#F0EFED", type: "dashed" } },
      axisLabel: { color: C_FAINT, fontSize: 12, fontFamily: CHART_FONT },
    },
    series: colors.map(c => ({
      name: c.label,
      type: "line",
      smooth: 0.28,
      symbol: "circle",
      symbolSize: 7,
      lineStyle: { width: 2.5, color: c.color },
      itemStyle: { color: "#fff", borderColor: c.color, borderWidth: 2.5 },
      emphasis: { focus: "series" },
      data: series.map(s => s[c.key]),
    })),
  };

  return html`<${EChart} option=${option} height=${height}
              deps=${[series, colors]} title=${title} />`;
}

/* ---- 柱状图：三产品单位成本对比 ---- */
function BarChart({ items, height = 300, title = "" }) {
  if (!items.length) return html`<div class="empty">该月份暂无数据</div>`;

  const option = {
    animationDuration: 420,
    grid: { left: 8, right: 16, top: 34, bottom: 8, containLabel: true },
    tooltip: {
      ...TOOLTIP,
      trigger: "item",
      valueFormatter: v => (v == null ? "—" : Number(v).toFixed(2) + " 元/盒"),
    },
    xAxis: {
      type: "category",
      data: items.map(i => i.label),
      axisLine: { lineStyle: { color: C_LINE } },
      axisTick: { show: false },
      axisLabel: { color: C_MUTED, fontSize: 13.5, fontFamily: CHART_FONT },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: "#F0EFED", type: "dashed" } },
      axisLabel: { color: C_FAINT, fontSize: 12, fontFamily: CHART_FONT },
    },
    series: [{
      type: "bar",
      barMaxWidth: 78,
      itemStyle: {
        borderRadius: [10, 10, 0, 0],
        color: p => items[p.dataIndex] && items[p.dataIndex].color,
      },
      label: {
        show: true, position: "top", distance: 10,
        formatter: p => Number(p.value).toFixed(2),
        color: C_INK, fontWeight: 650, fontSize: 14, fontFamily: CHART_FONT,
      },
      data: items.map(i => i.value),
    }],
  };

  return html`<${EChart} option=${option} height=${height} deps=${[items]} title=${title} />`;
}














function WaterfallChart({ rows, height = 330, title = "" }) {
  if (!rows.length) return html`<div class="empty">该月份无对比数据</div>`;

  const cats = rows.map(r => r.name);
  const last = rows.length - 1;

  // 三组柱子：不可见垫高 / 上升 / 下降。
  // 「合计」那根也走 rise/fall，但高度是总变动，单独着色区分。
  const base = [], rise = [], fall = [], isTotal = [];

  rows.forEach((r, i) => {
    isTotal.push(i === last);
    if (i === 0) {                       // 起点：0，不画柱
      base.push(0); rise.push(0); fall.push(0);
      return;
    }
    const stackFrom = i === last ? 0 : rows[i - 1].cum;
    if (r.amount >= 0) {
      base.push(stackFrom); rise.push(r.amount); fall.push(0);
    } else {
      base.push(stackFrom + r.amount);
      rise.push(0); fall.push(-r.amount);
    }
  });

  const mk = (color, sign, totalColor) => ({
    type: "bar", stack: "wf", stackStrategy: "all", barMaxWidth: 56,
    itemStyle: {
      color: p => (isTotal[p.dataIndex] ? totalColor : color),
      borderRadius: sign < 0 ? [0, 0, 6, 6] : [6, 6, 0, 0],
    },
    label: {
      show: true, position: sign < 0 ? "bottom" : "top", distance: 7,
      fontWeight: 640, fontSize: 12.5, fontFamily: CHART_FONT,
      color: p => (isTotal[p.dataIndex] ? C_INK : color),
      formatter: p => {
        if (!p.value) return "";
        const s = isTotal[p.dataIndex] ? (sign < 0 ? "−" : "") : (sign < 0 ? "−" : "+");
        return s + Number(p.value).toFixed(2);
      },
    },
  });

  const option = {
    animationDuration: 420,
    grid: { left: 8, right: 16, top: 32, bottom: 8, containLabel: true },
    tooltip: {
      ...TOOLTIP, trigger: "axis", axisPointer: { type: "shadow" },
      formatter: ps => {
        const i = ps[0].dataIndex, r = rows[i];
        if (i === 0) return `${r.name}`;
        if (i === last)
          return `<b>${r.name}</b><br/>${r.amount >= 0 ? "+" : ""}${r.amount.toFixed(2)} 元/盒`;
        return `${r.name}<br/>${r.amount >= 0 ? "+" : ""}${r.amount.toFixed(2)} 元/盒<br/>`
             + `对总变动的贡献 <b>${r.share == null ? "—" : r.share.toFixed(1) + "%"}</b>`;
      },
    },
    xAxis: {
      type: "category", data: cats,
      axisLine: { lineStyle: { color: C_LINE } },
      axisTick: { show: false },
      axisLabel: {
        color: p => (p === cats[last] ? C_INK : C_MUTED),
        fontWeight: 560, fontSize: 12.5, fontFamily: CHART_FONT,
      },
    },
    yAxis: {
      type: "value", scale: true,
      splitLine: { lineStyle: { color: "#F0EFED", type: "dashed" } },
      axisLabel: { color: C_FAINT, fontSize: 12, fontFamily: CHART_FONT },
    },
    series: [
      { name: "base", type: "bar", stack: "wf", stackStrategy: "all", silent: true,
        itemStyle: { color: "transparent" }, emphasis: { disabled: true },
        data: base },
      { name: "上升", ...mk("#C2410C", 1, "#0F766E"), data: rise },
      { name: "下降", ...mk("#15803D", -1, "#0F766E"), data: fall },
    ],
  };
  return html`<${EChart} option=${option} height=${height} deps=${[rows]} title=${title} />`;
}





function DonutChart({ items, total, unit, height = 300, title = "" }) {
  const sum = items.reduce((a, b) => a + (b.value || 0), 0);
  if (!sum) return html`<div class="empty">该月份无结构数据</div>`;

  const option = {
    animationDuration: 420,
    tooltip: {
      ...TOOLTIP, trigger: "item",
      formatter: p => `${p.name}<br/><b>${Number(p.value).toFixed(2)}</b> 元/盒`
                     + `<br/>占 ${p.percent}%`,
    },
    legend: {
      bottom: 0, icon: "roundRect", itemWidth: 10, itemHeight: 10, itemGap: 18,
      textStyle: { color: C_MUTED, fontSize: 13, fontFamily: CHART_FONT },
    },
    series: [{
      type: "pie",
      // 半径收小一点、中心下移，给外圈标签留出空间——
      // 实测 80% 外径时左侧 "13.65%" 会被容器裁成 "13.6..."
      radius: ["52%", "72%"],
      center: ["50%", "46%"],
      avoidLabelOverlap: true,
      itemStyle: { borderColor: "#fff", borderWidth: 3, borderRadius: 6 },
      label: {
        show: true,
        formatter: "{d}%",
        color: C_INK, fontSize: 12.5, fontWeight: 620, fontFamily: CHART_FONT,
      },
      labelLine: { length: 8, length2: 10, lineStyle: { color: C_LINE } },
      data: items.map(i => ({
        name: i.name, value: i.value,
        itemStyle: { color: i.color },
      })),
    }],
    graphic: total == null ? [] : [{
      type: "text", left: "center", top: "39%",
      style: {
        text: String(total), textAlign: "center",
        fill: C_INK, fontSize: 26, fontWeight: 680, fontFamily: CHART_FONT,
      },
    }, {
      type: "text", left: "center", top: "50%",
      style: {
        text: unit || "", textAlign: "center",
        fill: C_FAINT, fontSize: 12, fontFamily: CHART_FONT,
      },
    }],
  };
  return html`<${EChart} option=${option} height=${height} deps=${[items, total]} title=${title} />`;
}














const GRAPH_COLORS = {
  product:   "#0F766E",   // 主色：产品是入口
  material:  "#C2410C",
  process:   "#6366F1",
  equipment: "#0891B2",
  incident:  "#B45309",
  anomaly:   "#BE123C",
  peer:      "#7C3AED",
};

function GraphChart({ data, height = 460, selected, onSelect, fitKey }) {
  const inst = useRef(null);
  const boxRef = useRef(null);

  useEffect(() => {
    if (!boxRef.current || !window.echarts || !data || !data.nodes) return;
    inst.current = window.echarts.init(boxRef.current, null, { renderer: "canvas" });

    const deg = {};
    data.edges.forEach(e => {
      deg[e.src] = (deg[e.src] || 0) + 1;
      deg[e.dst] = (deg[e.dst] || 0) + 1;
    });

    inst.current.setOption({
      animationDuration: 600,
      tooltip: {
        ...TOOLTIP, trigger: "item", confine: true,
        formatter: p => {
          if (p.dataType === "edge") return p.data.relation;
          const n = p.data.raw || {};
          const lines = [`<b>${n.label}</b>`,
                         `<span style="color:#78716C">${n.typeZh || ""}</span>`];
          if (n.attrs) {
            Object.entries(n.attrs).slice(0, 4).forEach(([k, v]) => {
              if (v) lines.push(`${k}：${v}`);
            });
          }
          return lines.join("<br/>");
        },
      },
      series: [{
        type: "graph",
        layout: "force",
        /* ---- 交互灵敏度：三处调低，都是实测过的误操作来源 ----
         *
         * ① `roam: "move"` 而不是 `true`
         *    用 `true` 时**滚轮会缩放图**。图占了整屏宽，用户往下滚页面
         *    时鼠标常停在图上 → 页面不动、图被缩放，非常恼人。
         *    改为只允许**拖拽平移**，滚轮归还给页面。
         *
         * ② `draggable: false`（原来是 true）
         *    节点可拖时，"想平移但起点落在节点上"会**拖走那个节点**，
         *    图就散了。关掉之后拖哪都是平移，行为唯一。
         *
         * ③ 点击 = **只选中**，不再重新居中（见下面的 click 处理）
         *    原来点一下节点就 onPick → 重新取数 + 重新力导向布局，
         *    落点每次不同 → 一失手就找不回原来的位置。
         *    "选中"和"重新布局"是两件事，必须分开。
         */
        roam: "move",
        draggable: false,
        // ⚠️ 不能用圆形布局：本图是**星形/枢纽形**（银黄口服液为中心，
        //    一圈原料/工序/设备），圆形会把边全挤到中心打结。
        //    力导向才对。调参目标：让外围节点摊开、别挤成一坨。
        //    friction 提高 + gravity 略增 → 收敛更快，减少"点不准"的漂移。
        force: { repulsion: 520, edgeLength: [90, 170], gravity: 0.03,
                 layoutAnimation: true, friction: 0.2 },
        // 标签只给"重要的"（度数高的）显示；全显示会糊成一片。
        // 阈值不能太高——实测 deg>=3 时只剩 3~4 个标签，等于没标。
        label: {
          show: true,
          position: "right",
          fontSize: 11.5,
          fontFamily: CHART_FONT,
          color: C_INK,
          formatter: p => (deg[p.data.id] >= 2 ? p.data.name : ""),
        },
        // 选中的节点加一圈描边（用 ECharts 内建 select，不重设 data，避免重跑布局）
        select: {
          itemStyle: { borderColor: "#0F766E", borderWidth: 3,
                       shadowBlur: 12, shadowColor: "rgba(15,118,110,.35)" },
          label: { fontWeight: 700 },
        },
        selectedMode: "single",
        lineStyle: { color: "#D6D3D1", width: 1.2, curveness: 0.06 },
        emphasis: {
          focus: "adjacency",
          label: { show: true, fontWeight: 700 },
          lineStyle: { width: 2.4, color: "#0F766E" },
        },
        data: data.nodes.map(n => ({
          id: n.id, name: n.label,
          raw: { ...n, typeZh: (data.type_zh || {})[n.type] || n.type },
          symbolSize: Math.min(42, 12 + (deg[n.id] || 0) * 3.2),
          itemStyle: { color: GRAPH_COLORS[n.type] || "#78716C" },
        })),
        edges: data.edges.map(e => ({
          source: e.src, target: e.dst, relation: e.relation,
        })),
      }],
    });

    /* 点击**只选中**，不改焦点、不重新取数。
     * 点空白处则取消选中（符合直觉，也是"反悔"的出口）。 */
    inst.current.on("click", p => {
      const id = p.dataType === "node" ? p.data.id : null;
      if (id) inst.current.dispatchAction({ type: "select", seriesIndex: 0, dataIndex: p.dataIndex });
      else inst.current.dispatchAction({ type: "unselect", seriesIndex: 0 });
      if (onSelect) onSelect(id);
    });

    const onResize = () => inst.current && inst.current.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      if (inst.current) { inst.current.dispose(); inst.current = null; }
    };
  }, [data]);

  /* "重置视图"不走这里——由父组件换 `key` 让本组件**整个重新挂载**。
   * 试过在当前实例上复位 roam 状态：ECharts 的 graph 没有干净的
   * "复位平移/缩放"公开接口，猜内部状态不可靠。重挂载最省事也最确定。 */

  if (!data || !data.nodes || !data.nodes.length)
    return html`<div class="empty">该范围内没有可展示的关系</div>`;
  return html`<div ref=${boxRef} style=${{ width: "100%", height: height + "px" }}></div>`;
}

/* 图例：告诉用户颜色代表什么 */
function GraphLegend({ typeZh }) {
  return html`
    <div class="glegend">
      ${Object.entries(GRAPH_COLORS).map(([k, c]) => html`
        <span key=${k}><i style=${{ background: c }}></i>${(typeZh || {})[k] || k}</span>`)}
    </div>`;
}













function HeatmapChart({ data, element, height = 300, title = "" }) {
  if (!data || !data.products || !data.products.length)
    return html`<div class="empty">暂无热力图数据</div>`;

  const months = data.products[0].months || [];
  // ECharts heatmap 的 data 是 [xIndex, yIndex, value]
  const cells = [];
  data.products.forEach((p, yi) => {
    p.cells.forEach(([el, m, v]) => {
      if (el !== element) return;
      const xi = months.indexOf(m);
      if (xi >= 0) cells.push([xi, yi, v]);
    });
  });
  if (!cells.length) return html`<div class="empty">该要素暂无可比数据</div>`;

  const bound = data.bound || 1;
  const option = {
    animationDuration: 400,
    grid: { left: 8, right: 16, top: 12, bottom: 46, containLabel: true },
    tooltip: {
      ...TOOLTIP, trigger: "item",
      formatter: p => {
        const v = p.value[2];
        const sign = v > 0 ? "+" : "";
        return `${data.products[p.value[1]].product}<br/>`
             + `${months[p.value[0]]} · ${element}<br/>`
             + `<b>${sign}${v.toFixed(2)}%</b>`;
      },
    },
    // 连续色阶：绿(跌) → 白(平) → 红(涨)，以 0 为中心
    visualMap: {
      min: -bound, max: bound, calculable: true, orient: "horizontal",
      left: "center", bottom: 0,
      itemWidth: 12, itemHeight: 90,
      textStyle: { color: C_MUTED, fontSize: 12, fontFamily: CHART_FONT },
      inRange: { color: ["#15803D", "#F0FDF4", "#FFFBEB", "#FFF7ED", "#C2410C"] },
    },
    xAxis: {
      type: "category", data: months.map(m => m.slice(5) + "月"),
      splitArea: { show: true },
      axisLine: { lineStyle: { color: C_LINE } }, axisTick: { show: false },
      axisLabel: { color: C_MUTED, fontSize: 12.5, fontFamily: CHART_FONT },
    },
    yAxis: {
      type: "category", data: data.products.map(p => p.product),
      splitArea: { show: true },
      axisLine: { lineStyle: { color: C_LINE } }, axisTick: { show: false },
      axisLabel: { color: C_MUTED, fontSize: 12.5, fontFamily: CHART_FONT },
    },
    series: [{
      type: "heatmap",
      data: cells,
      label: {
        show: true, fontSize: 11, fontFamily: CHART_FONT, color: C_INK,
        formatter: p => {
          const v = p.value[2];
          return (v > 0 ? "+" : "") + v.toFixed(1);
        },
      },
      itemStyle: { borderColor: "#fff", borderWidth: 2, borderRadius: 4 },
      emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(28,25,23,.25)" } },
    }],
  };
  return html`<${EChart} option=${option} height=${height}
              deps=${[data, element]} title=${title} />`;
}


function DecideCard() {
  const { data, loading, error } = useApi("/api/decide");

  if (loading) return html`<div class="skel skel-card"></div>`;
  if (error) return html`<${ErrorBox} msg=${error} />`;

  const gen = data.action === "generate_report";
  return html`
    <div class=${"decidecard " + (gen ? "gen" : "dash")}>
      <div class="dc-head">
        <span class="dc-mark">${gen ? "◆" : "◇"}</span>
        <div>
          <div class="dc-title">系统判断：${data.label}</div>
          <div class="dc-sub">
            ${gen
              ? "知识库有实质变化，已有报告可能已过时——需要重新生成。"
              : "知识库自上次分析以来没有实质变化，刷新看板即可，无需重跑分析。"}
          </div>
        </div>
      </div>
      <ul class="dc-signals">
        ${(data.reasons || []).map((s, i) => html`
          <li key=${i} class=${s.fired ? "on" : ""}>
            <span class="dc-dot"></span>
            <b>${s.name}</b>
            <span class="dc-detail">${s.detail}</span>
          </li>`)}
      </ul>
    </div>`;
}

/* =============================================================================
 * 页签 2：成本趋势
 * ========================================================================== */
function Trend() {
  const { data: prod } = useApi("/api/products");
  const [product, setProduct] = useState("银黄口服液");
  const { data, loading, error } =
    useApi(`/api/trend?product=${encodeURIComponent(product)}`);

  useEffect(() => {
    const l = prod && prod.products;
    if (l && l.length && !l.includes(product)) setProduct(l[0]);
  }, [prod]);

  const LINES = [
    { key: "单位成本",     label: "单位成本", color: "#0F766E" },
    { key: "单位材料",     label: "直接材料", color: "#0891B2" },
    { key: "单位人工",     label: "直接人工", color: "#F59E0B" },
    { key: "单位制造费用", label: "制造费用", color: "#6366F1" },
  ];

  const toolbar = html`
    <div class="toolbar">
      <label>产品</label>
      <select value=${product} onChange=${e => setProduct(e.target.value)}>
        ${((prod && prod.products) || []).map(p => html`<option key=${p} value=${p}>${p}</option>`)}
      </select>
      <span class="pill">单位：元 / 盒</span>
    </div>`;

  if (loading) return html`<div>${toolbar}<${Loading} /></div>`;
  if (error) return html`<div>${toolbar}<${ErrorBox} msg=${error} /></div>`;

  const s = data.series || [];
  return html`
    ${toolbar}
    <div class="card">
      <h2>逐月成本走势 <span class="hint">${product}</span></h2>
      <p class="card-sub">三条成本要素线之和等于单位成本线，可用来核对数据一致性。</p>
      <${LineChart} series=${s} colors=${LINES} />
    </div>

    <div class="card">
      <h2>分月明细</h2>
      <table class="tbl">
        <thead><tr>
          <th>月份</th><th style=${{ textAlign: "right" }}>产量（盒）</th>
          <th style=${{ textAlign: "right" }}>直接材料</th>
          <th style=${{ textAlign: "right" }}>直接人工</th>
          <th style=${{ textAlign: "right" }}>制造费用</th>
          <th style=${{ textAlign: "right" }}>单位成本</th>
          <th style=${{ textAlign: "right" }}>环比</th>
        </tr></thead>
        <tbody>
          ${s.map(r => {
            const dd = parseFloat(r.环比);
            return html`<tr key=${r.月份}>
              <td>${r.月份.replace("-", " 年 ")} 月</td>
              <td class="num">${r.产量 == null ? "—" : r.产量.toLocaleString()}</td>
              <td class="num">${fmt(r.单位材料)}</td>
              <td class="num">${fmt(r.单位人工)}</td>
              <td class="num">${fmt(r.单位制造费用)}</td>
              <td class="num strong">${fmt(r.单位成本)}</td>
              <td class=${"num " + cls(dd)}>${r.环比}</td>
            </tr>`;
          })}
        </tbody>
      </table>
    </div>`;
}

/* =============================================================================
 * 页签 3：数据说明（原「冲突登记」）
 *
 * 对用户讲的不是"我们代码里有 26 个 Status 块"，
 * 而是"哪些数字存在口径差异、分析时采信了哪一个、对结论影响多大"。
 * ========================================================================== */
function Notes() {
  const { data, loading, error } = useApi("/api/conflicts");
  const [open, setOpen] = useState({});

  if (loading) return html`<${Loading} />`;
  if (error) return html`<${ErrorBox} msg=${error} />`;

  const LABEL = { Disputed: "口径存疑", Outdated: "已更新", Update: "已修订" };
  const CLS = { Disputed: "disputed", Outdated: "outdated", Update: "updated" };

  /* 分级展示（服务端已去重 + 标注 severity）：
   *   A = 影响**数字可信度** → 默认展开，用户必须看到
   *   B = **资料自身前后不一致** → 默认折叠，供了解
   *
   * ⚠️ 这是这次改版的核心：原来 26 条平铺，把"GMP 摘要章节号对不上"
   *    和"金银花涨幅算不出来"混在一起，看起来像**整个系统都不可靠**。
   *    分开之后用户能看到真实比例——真正影响结论的只有个位数。
   */
  const sevA = (data.items || []).filter(x => x.severity === "A");
  const sevB = (data.items || []).filter(x => x.severity !== "A");

  const Card = ({ it }) => {
    const id = it.article + ":" + it.line;
    const isOpen = !!open[id];
    return html`
      <div class="note-item" key=${id}>
        <div class="note-hd" onClick=${() => setOpen({ ...open, [id]: !isOpen })}>
          <span class=${"badge " + (CLS[it.kind] || "outdated")}>${LABEL[it.kind] || it.kind}</span>
          <div class="grow">
            <div class="hl">${it.headline || "详见下方说明"}
              ${it.merged_count ? html`<span class="merged-tag">合并 ${it.merged_count + 1} 处</span>` : null}
            </div>
            <div class="src">出自：${prettySource(it.article)}${it.section ? " · " + it.section : ""}</div>
          </div>
          <span class=${"caret" + (isOpen ? " open" : "")}>▶</span>
        </div>
        ${isOpen ? html`
          <div class="note-bd">
            <ul>${it.body.map((b, i) => html`<li key=${i}>${inline(b)}</li>`)}</ul>
          </div>` : null}
      </div>`;
  };

  return html`
    <div class="card">
      <h2>数据说明</h2>
      <p class="card-sub">
        分析所依据的原始资料中，本系统逐条记录了 <strong>${data.count} 处</strong>需要注意的地方
        （原始登记 ${data.raw_count} 处，已合并重复）。其中
        <strong>${sevA.length} 处影响数字可信度</strong>——已说明分析时采用了哪种口径；
        其余 <strong>${sevB.length} 处</strong>是资料自身的前后不一致，
        不影响分析结论，供你了解。未列入的事项，均为已核对一致。
      </p>
    </div>

    ${/* ---- A 类：影响结论可信度，默认展开 ---- */ ""}
    ${sevA.length ? html`
      <div class="sev-head">
        <span class="sev-mark sev-a"></span>
        <div>
          <div class="sev-title">影响数字可信度</div>
          <div class="sev-sub">
            这些地方的口径不一致会改变结论，报告里已用<strong>可复现的口径</strong>并注明基准。
          </div>
        </div>
        <span class="sev-n">${sevA.length}</span>
      </div>
      ${sevA.map(it => html`<${Card} it=${it} />`)}` : null}

    ${/* ---- B 类：资料自身不一致，默认折叠 ---- */ ""}
    ${sevB.length ? html`
      <details class="sev-fold">
        <summary>
          <span class="sev-mark sev-b"></span>
          <div>
            <div class="sev-title">资料自身的不一致</div>
            <div class="sev-sub">不影响分析结论（如文档示例与实现不符、章节编号对不上）</div>
          </div>
          <span class="sev-n">${sevB.length}</span>
          <span class="caret">▾</span>
        </summary>
        <div class="sev-body">${sevB.map(it => html`<${Card} it=${it} />`)}</div>
      </details>` : null}`;
}

/* 内部路径 → 用户能懂的中文来源名 */
function prettySource(article) {
  const base = String(article).split("/").pop().replace(/\.md$/, "");
  const MAP = {
    "成本异动登记册": "成本变动分析",
    "三产品成本基线": "产品成本数据",
    "成本数据口径与结构": "成本口径说明",
    "一厂vs二厂对标分析": "两厂对比分析",
    "药材行情与行业基准": "药材行情与行业基准",
    "设备台账与维修历史": "设备与维修记录",
    "GMP质量约束与成本关联": "GMP 质量要求",
    "报告模板契约": "报告格式要求",
    "RPA接口契约": "工单系统接口",
  };
  return MAP[base] || base;
}

/* 行内标记：只渲染 `代码` 与 **粗体**（够用，不引 markdown 库） */
function inline(s) {
  const out = [];
  String(s).split(/(`[^`]+`|\*\*[^*]+\*\*)/g).forEach((p, i) => {
    if (p.startsWith("`") && p.endsWith("`") && p.length > 2) {
      out.push(html`<code key=${i} style=${{
        background: "#fff", border: "1px solid #E7E5E4", borderRadius: 6,
        padding: "1px 6px", fontSize: 13,
      }}>${p.slice(1, -1)}</code>`);
    } else if (p.startsWith("**") && p.endsWith("**") && p.length > 4) {
      out.push(html`<strong key=${i}>${p.slice(2, -2)}</strong>`);
    } else if (p) out.push(p);
  });
  return out;
}

/* =============================================================================
 * 对话里的 markdown 渲染
 *
 * 为什么需要：大模型的回答**本来就是 markdown**（标题、表格、列表、代码、粗体），
 * 但原先消息体是 `${m.content}` 直出 + CSS `white-space: pre-wrap`——
 * 于是用户看到的是**带 `|` 和 `##` 的源码**，而不是排好版的文档。
 * 我们的回答动辄带三张对齐的表格，纯文本根本没法读。
 *
 * 设计取舍
 * --------
 * ① **不引第三方 markdown 库**。本项目前端是 vendor 版、无构建步骤，
 *    引一个 md 库要么加文件、要么走 CDN（而我们刻意不依赖 CDN）。表格 +
 *    标题 + 列表 + 粗体/代码 已覆盖本场景 99%，自己写反而可控。
 * ② **安全**：输出走 `dangerouslySetInnerHTML`，所以**必须**先转义 HTML。
 *    转义放在最前，之后只由本函数插入受控标签——模型输出里的 `<script>`
 *    进不来。链接另外只允许 http/https，挡掉 `javascript:`。
 * ③ **导出**：长回答要能存成本地 .md。所以额外提供
 *    `markdownToPlain()` 与下载按钮（见 Chat 里）——**导出的仍是原始
 *    markdown 源码**，不是渲染后的 HTML（.md 文件就该是源码）。
 * ========================================================================== */

function _escHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/* 行内：转义后按 `代码` / **粗体** / *斜体* / [文字](链接) 处理 */
function _mdInline(s) {
  return _escHtml(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_m, text, url) =>
      /^https?:\/\//i.test(url)
        ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${text}</a>`
        : text);
}

/*
 * markdown → 受控 HTML 字符串。
 * 支持：`#`~`####` 标题、`|` 表格、`-`/`*`/数字 列表、``` 代码块、
 *       `>` 引用、`---` 分隔线、段落与单换行。
 */
function markdownToHtml(src) {
  const lines = String(src == null ? "" : src).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;

  const cellsOf = (line) => line.replace(/^\s*\|/, "").replace(/\|\s*$/, "")
                               .split("|").map(c => c.trim());
  const isSep = (line) => /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(line) && line.includes("-");

  while (i < lines.length) {
    const line = lines[i];

    // --- 代码块 ---
    if (/^\s*```/.test(line)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      i++; // 收尾的 ```
      out.push(`<pre><code>${_escHtml(buf.join("\n"))}</code></pre>`);
      continue;
    }

    // --- 表格：当前行含 |，下一行是分隔行 ---
    if (line.includes("|") && i + 1 < lines.length && isSep(lines[i + 1])) {
      const head = cellsOf(line);
      i += 2;
      const body = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        body.push(cellsOf(lines[i++]));
      }
      const th = head.map(c => `<th>${_mdInline(c)}</th>`).join("");
      const tr = body.map(r =>
        `<tr>${r.map(c => `<td>${_mdInline(c)}</td>`).join("")}</tr>`).join("");
      out.push(`<div class="md-table-wrap"><table><thead><tr>${th}</tr></thead>` +
               `<tbody>${tr}</tbody></table></div>`);
      continue;
    }

    // --- 标题 ---
    let m = line.match(/^(#{1,4})\s+(.*)$/);
    if (m) {
      const lv = m[1].length + 2;          // # → h3（h1/h2 留给页面本身）
      out.push(`<h${lv}>${_mdInline(m[2])}</h${lv}>`);
      i++;
      continue;
    }

    // --- 分隔线 ---
    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { out.push("<hr/>"); i++; continue; }

    // --- 引用 ---
    if (/^\s*>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ""));
        i++;
      }
      out.push(`<blockquote>${buf.map(_mdInline).join("<br/>")}</blockquote>`);
      continue;
    }

    // --- 列表（无序 / 有序）---
    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\.\s+/.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
        items.push(_mdInline(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, "")));
        i++;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>${items.map(t => `<li>${t}</li>`).join("")}</${tag}>`);
      continue;
    }

    // --- 空行 ---
    if (!line.trim()) { i++; continue; }

    // --- 段落：连续非空、非结构行合成一段；单换行转 <br/> ---
    const buf = [];
    while (i < lines.length && lines[i].trim()
           && !/^\s*(#{1,4}\s|>|```|([-*+]|\d+\.)\s)/.test(lines[i])
           && !(lines[i].includes("|") && i + 1 < lines.length && isSep(lines[i + 1]))) {
      buf.push(lines[i]);
      i++;
    }
    out.push(`<p>${buf.map(_mdInline).join("<br/>")}</p>`);
  }
  return out.join("");
}

/* 长回答才给导出按钮：这条阈值决定"多长算长"。
   太短的回答导出成文件没意义（一句话一个 .md 很滑稽）。 */
const MD_EXPORT_MIN_CHARS = 400;

/* 触发浏览器下载：把原始 markdown 存成本地 .md */
function downloadMarkdown(content, idx) {
  const ts = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const name = `对话回答_${ts.getFullYear()}${pad(ts.getMonth() + 1)}${pad(ts.getDate())}`
             + `_${pad(ts.getHours())}${pad(ts.getMinutes())}${pad(ts.getSeconds())}`
             + (idx ? `_${idx}` : "") + ".md";
  const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}







function Benchmark({ me, onNeedLogin }) {
  const { data: prod } = useApi("/api/products");
  const [product, setProduct] = useState("银黄口服液");
  const [month, setMonth] = useState("2026-05");
  const [llm, setLlm] = useState(false);
  const [busy, setBusy] = useState(false);

  const q = `/api/benchmark?product=${encodeURIComponent(product)}&month=${month}&llm=${llm ? 1 : 0}`;
  const { data, loading, error } = useApi(q);

  useEffect(() => {
    const l = prod && prod.products;
    if (l && l.length && !l.includes(product)) setProduct(l[0]);
  }, [prod]);

  // llm=1 时请求会慢，显示"生成中"
  useEffect(() => { setBusy(loading && llm); }, [loading, llm]);

  const MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"];

  const toolbar = html`
    <div class="toolbar">
      <label>产品</label>
      <select value=${product} onChange=${e => setProduct(e.target.value)}>
        ${((prod && prod.products) || []).map(p => html`<option key=${p} value=${p}>${p}</option>`)}
      </select>
      <label>月份</label>
      <select value=${month} onChange=${e => setMonth(e.target.value)}>
        ${MONTHS.map(m => html`<option key=${m} value=${m}>${m.replace("-", " 年 ")} 月</option>`)}
      </select>
      <span class="pill">与中药二厂同期对比</span>
    </div>`;

  if (loading && !data) {
    return html`<div>${toolbar}<${Loading} />
      ${busy ? html`<p class="card-sub" style=${{ textAlign: "center", marginTop: 12 }}>
        正在生成归因分析…</p>` : null}</div>`;
  }
  if (error) return html`<div>${toolbar}<${ErrorBox} msg=${error} /></div>`;

  const { step1, step2, step3 } = data;

  return html`
    ${toolbar}

    ${/* ---- 三步指示器 ---- */ ""}
    <div class="card">
      <h2>对标分析三步法</h2>
      <p class="card-sub">
        对比本单位与中药二厂同产品同期的成本差异，逐层拆解到成本要素，
        再由分析模型给出归因与改进建议。
      </p>
      <div class="steps">
        ${[["1", "找差异", "对比两厂同期成本"],
           ["2", "拆结构", "按要素逐层拆解"],
           ["3", "拆原因", "归因与改进建议"]].map(([n, t, d], i) => html`
          <div class="step-item" key=${n}>
            <div class="step-num">${n}</div>
            <div class="step-txt">
              <div class="step-title">${t}</div>
              <div class="step-desc">${d}</div>
            </div>
            ${i < 2 ? html`<div class="step-arrow">→</div>` : null}
          </div>`)}
      </div>
    </div>

    ${/* ---- 第一步 ---- */ ""}
    <div class="card">
      <h2>第一步 · 找差异 <span class="hint">差异总览表</span></h2>
      <p class="card-sub">
        差异金额 = 二厂 − 一厂，<strong>正数表示二厂更高</strong>；
        差异率以本单位（一厂）为基准。
      </p>
      <table class="tbl">
        <thead><tr>
          <th>成本要素</th>
          <th style=${{ textAlign: "right" }}>一厂（元/盒）</th>
          <th style=${{ textAlign: "right" }}>二厂（元/盒）</th>
          <th style=${{ textAlign: "right" }}>差异金额</th>
          <th style=${{ textAlign: "right" }}>差异率</th>
          <th>方向</th>
        </tr></thead>
        <tbody>
          ${step1.rows.map(r => html`<tr key=${r.成本要素}>
            <td class=${r.成本要素 === "单位成本" ? "strong" : ""}>${r.成本要素}</td>
            <td class="num">${fmt(r.一厂)}</td>
            <td class="num">${fmt(r.二厂)}</td>
            <td class=${"num " + cls(r.差异金额)}>${r.差异金额 > 0 ? "+" : ""}${fmt(r.差异金额)}</td>
            <td class=${"num " + cls(r.差异率)}>${r.差异率 == null ? "—" : `${r.差异率 > 0 ? "+" : ""}${fmt(r.差异率)}%`}</td>
            <td>${r.方向}</td>
          </tr>`)}
        </tbody>
      </table>
    </div>

    ${/* ---- 第二步 ---- */ ""}
    ${step2.tree ? html`
      <div class="card">
        <h2>第二步 · 拆结构 <span class="hint">差异结构树</span></h2>
        <p class="card-sub">${step2.tree.根}</p>

        <div class="tree">
          ${step2.tree.要素层.map((n, i) => html`
            <div class="tree-node" key=${n.要素}>
              <span class="tree-branch">${i === step2.tree.要素层.length - 1 ? "└─" : "├─"}</span>
              <span class="tree-name">${n.要素}</span>
              <span class=${"tree-amt " + cls(n.差异金额)}>
                ${n.差异金额 > 0 ? "+" : ""}${fmt(n.差异金额)}
              </span>
              <span class="tree-pct">占差异 ${fmt(n.占差异比, 1)}%</span>
              <span class=${"badge " + (n.结论 === "主因" ? "disputed" : "outdated")}>${n.结论}</span>
              <span class="tree-bar"><i style=${{ width: Math.min(100, n.占差异比) + "%" }}></i></span>
            </div>`)}
        </div>

        ${step2.tree.下钻层.明细.length ? html`
          <details style=${{ marginTop: 18 }}>
            <summary style=${{ cursor: "pointer", fontSize: 13.5, color: "var(--muted)" }}>
              下钻：一厂材料构成（${step2.tree.下钻层.明细.length} 项）
            </summary>
            <div class="note" style=${{ marginTop: 12, borderLeftColor: "var(--warn-line)",
                                        background: "var(--warn-soft)", color: "var(--warn)",
                                        fontSize: 13 }}>
              ${step2.tree.下钻层.说明}
            </div>
            <table class="tbl" style=${{ marginTop: 12 }}>
              <thead><tr>
                <th>原材料</th>
                <th style=${{ textAlign: "right" }}>一厂单耗（元/盒）</th>
                <th style=${{ textAlign: "right" }}>占材料成本</th>
                <th style=${{ textAlign: "right" }}>环比</th>
              </tr></thead>
              <tbody>
                ${step2.tree.下钻层.明细.map(m => html`<tr key=${m.原材料名称}>
                  <td>${m.原材料名称}</td>
                  <td class="num">${m.一厂单耗}</td>
                  <td class="num">${m.占比}</td>
                  <td class="num">${m.环比}</td>
                </tr>`)}
              </tbody>
            </table>
          </details>` : null}
      </div>` : null}

    ${/* ---- 第三步 ---- */ ""}
    <div class="card">
      <h2>第三步 · 拆原因 <span class="hint">归因与改进建议</span></h2>
      ${step3.text && step3.text !== "（未调用 LLM）" ? html`
        <div class="prose">${inline(step3.text)}</div>
      ` : html`
        <p class="card-sub">
          归因由分析模型结合成本资料生成本段分析，会明确区分「有数据支撑的结论」
          与「无法判定、需补充数据」的部分。
        </p>
        <button class="ask-btn" disabled=${busy}
                onClick=${() => me ? setLlm(true) : onNeedLogin()}>
          ${busy ? "生成中…" : "生成归因分析"}
        </button>`}

      ${step3.advice && step3.advice.length ? html`
        <h2 style=${{ marginTop: 26 }}>改进建议
          <span class="hint">可转化为整改任务下发</span>
        </h2>
        <table class="tbl">
          <thead><tr>
            <th>序号</th><th>建议事项</th><th>责任部门</th>
            <th>优先级</th><th>预期效果</th><th>建议完成时间</th>
          </tr></thead>
          <tbody>
            ${step3.advice.map((a, i) => html`<tr key=${i}>
              <td>${i + 1}</td>
              <td>${a.建议事项}</td>
              <td>${a.责任部门}</td>
              <td><span class=${"badge " + (a.优先级 === "高" ? "disputed" : "outdated")}>${a.优先级}</span></td>
              <td>${a.预期效果}</td>
              <td>${a.建议完成时间}</td>
            </tr>`)}
          </tbody>
        </table>` : null}
    </div>`;
}

/* =============================================================================
 * 页签 4：数据更新记录（原「摄入时间线」）
 * ========================================================================== */
function History() {
  const { data, loading, error } = useApi("/api/timeline");
  if (loading) return html`<${Loading} />`;
  if (error) return html`<${ErrorBox} msg=${error} />`;

  // 内部操作类型 → 用户语言
  const KIND = {
    ingest: "纳入新资料", skill: "规则调整", lint: "数据校核",
    verify: "效果验证", fix: "问题修复", feat: "功能上线",
    test: "测试", deploy: "系统部署",
  };

  // 只展示与"数据/分析能力"相关的记录，过滤纯开发事件
  const SHOW = new Set(["ingest", "lint", "fix", "skill", "feat", "deploy"]);
  const items = data.items.filter(e => SHOW.has(e.kind)).reverse();

  return html`
    <div class="card">
      <h2>数据更新记录</h2>
      <p class="card-sub">
        本系统持续纳入新的原始资料（成本表、行情、法规、设备清单等），
        每次纳入都会重新核对既有结论。以下是完整记录——
        <strong>包括被后续数据修正过的结论</strong>，不做隐藏。
      </p>
    </div>

    <div class="card">
      <div class="timeline">
        ${items.map((e, i) => html`
          <div class=${"tl-item t-" + e.kind} key=${i}>
            <span class="tl-date">${e.date}</span>
            <span class="tl-kind">${KIND[e.kind] || e.kind}</span>
            <div class="tl-title">${cleanTitle(e.title)}</div>
            ${e.excerpt ? html`<div class="tl-desc">${e.excerpt}</div>` : null}
          </div>`)}
      </div>
    </div>`;
}

/* 标题里去掉内部术语 */
function cleanTitle(t) {
  return String(t)
    .replace(/（骨架 [A-G]'?）/g, "")
    .replace(/^no material:\s*/i, "未纳入（无新信息）：")
    .replace(/raw\/[\w/一-龥.\-（）]+/g, "某原始资料")
    .trim();
}







function Rectify({ me, onNeedLogin, onAuthed }) {
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [data, setData] = useState(null);
  const [reports, setReports] = useState([]);
  const [pick, setPick] = useState("");
  const [last, setLast] = useState(null);      // 上次派发结果
  /* 展开的任务详情：{task_id: {loading|task|error}}。
   * 按需拉取——列表给的是"当前状态"，详情才有**状态流转时间线**，
   * 而一次拉全部任务的详情是浪费（列表 N 条 = N 个请求）。 */
  const [detail, setDetail] = useState({});

  const load = () => {
    if (!me) return;
    api("/api/rpa/tasks").then(d => {
      if (d._error) { setErr(d._error); setData(null); return; }
      setData(d); setErr("");
    });
    api("/api/control/exports").then(d => {
      const rs = ((d && d.reports) || []).filter(r => r.rpa_supported);
      setReports(rs);
      if (!pick && rs.length) setPick(rs[0].name);
    });
  };
  useEffect(load, [me]);

  const toggleDetail = (tid) => {
    setDetail(prev => {
      // 已展开 → 收起
      if (prev[tid]) {
        const c = { ...prev }; delete c[tid]; return c;
      }
      // 未展开 → 先占位再拉
      return { ...prev, [tid]: { loading: true } };
    });
    if (detail[tid]) return;                 // 上面那个分支是收起，不用拉
    api("/api/rpa/task/" + encodeURIComponent(tid)).then(d => {
      setDetail(prev => ({
        ...prev,
        [tid]: d._error ? { error: d._error }
                        : (d.ok ? { task: d.task } : { error: d.error || "查询失败" }),
      }));
    });
  };

  const dispatch = async () => {
    if (!pick) return;
    const rep = reports.find(x => x.name === pick);
    setBusy(true); setLast(null); setErr("");
    const r = await api("/api/rpa/dispatch", {
      method: "POST",
      body: { report: pick },
    });
    setBusy(false);
    if (r._error) { setErr("派发失败：" + r._error); return; }
    setLast(r); load();
  };

  if (!me) return html`
    <div class="card">
      <h2>整改闭环</h2>
      <p class="card-sub">把分析结论转为整改任务并下发到责任人，跟踪执行状态。</p>
      <${LoginGate} what="下发与追踪整改任务" onGoLogin=${onNeedLogin} />
    </div>`;

  const c = (data && data.counts) || { generated: 0, delivered: 0, confirmed: 0 };
  const zh = (data && data.status_zh) || {};

  return html`
    <div class="card">
      <h2>整改闭环（模拟工单）
        <span class="hint">${data && data.ok ? "下游工单系统已连接" : "下游工单系统未连接"}</span>
      </h2>
      <p class="card-sub">
        报告第六章的「整改任务清单」进入模拟工单系统，通知和状态推进均为模拟。
        下发前需人工确认——这是分析结论进入执行环节的最后一道闸门。
      </p>

      ${err ? html`<div class="alert" style=${{ marginBottom: 14 }}>⚠️ ${err}</div>` : null}
      ${data && data.pending && data.pending.length ? html`<div class="alert">
        ${data.pending.length} 条任务尚未送达，已保存在当前账户。
        <button class="chip" disabled=${busy} onClick=${async () => {
          setBusy(true); setErr("");
          const retried = await api("/api/rpa/retry", { method: "POST" });
          if (retried._error) setErr(retried._error); else setLast(retried);
          setBusy(false); load();
        }}>重试未送达任务</button>
      </div>` : null}

      <div class="toolbar" style=${{ marginBottom: 8 }}>
        <label>选择报告</label>
        <select value=${pick} onChange=${e => setPick(e.target.value)}>
          ${reports.map(r => html`<option key=${r.name} value=${r.name}>${r.product} · ${r.period} · ${r.theme} · ${r.scope === "personal" ? "我的报告" : "共享样例"}</option>`)}
        </select>
        <button class="ask-btn" disabled=${busy || !pick} onClick=${dispatch}>
          ${busy ? "下发中…" : "下发整改任务"}
        </button>
      </div>
      ${!reports.length ? html`<div class="note">暂无可下发的报告。</div>` : null}

      ${last ? html`
        <div class=${"alert " + (last.failed || last.degraded ? "warn" : "")}
             style=${{ marginTop: 16, background: last.failed || last.degraded ? "var(--warn-soft)" : "var(--down-soft)",
                       color: last.failed || last.degraded ? "var(--warn)" : "var(--down)",
                       borderColor: last.failed || last.degraded ? "var(--warn-line)" : "#BBF7D0" }}>
          ${last.summary}
        </div>
        <table class="tbl" style=${{ marginTop: 12 }}>
          <thead><tr><th>任务编号</th><th>结果</th></tr></thead>
          <tbody>${last.results.map(x => html`<tr key=${x.task_id}>
            <td>${x.task_id}</td>
            <td>${x.ok ? html`<span class="down">✓ ${x.notify || "已下发"}</span>`
                       : html`<span class="up">✗ ${x.error || "失败"}</span>`}</td>
          </tr>`)}</tbody>
        </table>` : null}
    </div>

    <div class="card">
      <h2>任务追踪
        <button class="linkbtn" style=${{ marginLeft: "auto" }} onClick=${load}>刷新</button>
      </h2>
      <div class="grid3" style=${{ marginBottom: 18 }}>
        ${[["已生成", c.generated, "var(--ink)"],
           ["已送达", c.delivered, "var(--info)"],
           ["已确认", c.confirmed, "var(--down)"]].map(([lab, v, col]) => html`
          <div class="kpi" key=${lab} style=${{ padding: "18px 20px" }}>
            <div class="kpi-name">${lab}</div>
            <div class="kpi-value" style=${{ color: col, fontSize: 32 }}>${v}</div>
          </div>`)}
      </div>

      ${data && data.tasks && data.tasks.length ? html`
        <table class="tbl">
          <thead><tr>
            <th>任务编号</th><th>任务标题</th><th>责任人</th>
            <th>优先级</th><th>状态</th><th>截止</th>
          </tr></thead>
          <tbody>${data.tasks.map(t => {
            const open = !!detail[t.task_id];
            const d = detail[t.task_id];
            return html`<${React.Fragment} key=${t.task_id}>
              <tr class="clickable" onClick=${() => toggleDetail(t.task_id)}>
                <td class="nowrap"><span class=${"caret-inline" + (open ? " open" : "")}>▶</span>${t.task_id}</td>
                <td>${String(t.task_title || "").slice(0, 40)}${String(t.task_title || "").length > 40 ? "…" : ""}</td>
                <td>${(t.assignee && t.assignee.name) || "—"}</td>
                <td class="nowrap"><span class=${"badge " + (t.priority === "high" ? "disputed" : "outdated")}>${t.priority}</span></td>
                <td class="nowrap">${zh[t.status] || t.status}</td>
                <td class="nowrap">${t.deadline || "—"}</td>
              </tr>
              ${open ? html`<tr><td colspan="6" class="detailcell">
                ${d && d.loading ? html`<span class="muted">载入中…</span>`
                  : d && d.error ? html`<span class="up">⚠️ ${d.error}</span>`
                  : html`
                    ${d && d.task && d.task.progress ? html`
                      <div class="tl-progress">进展：${d.task.progress}</div>` : null}
                    <div class="tl-title">状态流转</div>
                    <ol class="tl">
                      ${((d && d.task && d.task.status_history) || []).map((h, j) => html`
                        <li key=${j}>
                          <span class="tl-dot"></span>
                          <b>${zh[h.status] || h.status}</b>
                          <span class="tl-time">${String(h.time || "").replace("T", " ").slice(0, 19)}</span>
                        </li>`)}
                      ${!(d && d.task && (d.task.status_history || []).length)
                        ? html`<li class="muted">暂无流转记录</li>` : null}
                    </ol>`}
              </td></tr>` : null}
            </${React.Fragment}>`;
          })}</tbody>
        </table>` : html`
        <div class="empty">
          <div class="big">还没有下发的任务</div>
          <div>选一份报告，点「下发整改任务」</div>
        </div>`}
    </div>`;
}

/* =============================================================================
 * 首页第 ① 段：智能文档（对话）
 *
 * 为什么它不是独立页签：**问答是这个系统的入口动作**——
 * 用户想知道什么，第一反应是问，而不是先选页签再找图表。
 * 独立页签等于把入口藏在导航里。
 *
 * ⚠️ 未登录时**直接给登录卡片**，不显示输入框。
 *    这里和「上传区」的处理不同：上传区是滑到才看见的次要功能，
 *    而问答区在首屏——摆一个点了没反应的输入框比给张登录卡更让人困惑。
 * ========================================================================== */
function HeroAsk({ me, onNeedLogin, onAuthed }) {
  const [msgs, setMsgs] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [meta, setMeta] = useState(null);
  const [showTrace, setShowTrace] = useState({});
  const boxRef = useRef(null);
  /* 多会话：当前会话 id 与列表。
   * ⚠️ `convId === null` 表示**尚未落库的新对话**——用户点了"新对话"但还没提问。
   *    这时不建会话（否则点开看看又关掉会攒一堆空的），
   *    首次提问时带 `new: true` 让后端建。 */
  const [convId, setConvId] = useState(null);
  const [convs, setConvs] = useState([]);
  const [actionError, setActionError] = useState("");

  /* 从 URL 里取「要打开哪个会话」——个人中心点「打开」时写进来的。
   * 读后即清，避免刷新页面时又跳一次。 */
  const takePendingConv = () => {
    const m = (location.hash || "").match(/[?&]conv=(\d+)/);
    if (!m) return null;
    history.replaceState(null, "", location.pathname + "#docs");
    return Number(m[1]);
  };

  /* 登录态变化时拉自己的历史。
   * ⚠️ `me` 由 Home 统一持有，本组件**不自己去读凭证**——
   *    上一版三个组件各自读 localStorage，结果是"三个真相源"：
   *    在对话区登录了，上传区还不知道。 */
  useEffect(() => {
    if (!me) { setMsgs([]); setMeta(null); setConvs([]); setConvId(null); return; }
    let alive = true;

    const apply = (d) => {
      setMeta({ summary: d.summary, compactions: d.compactions });
      setMsgs((d.messages || [])
        .filter(m => m.role === "user" || m.role === "assistant")
        .map(m => ({ role: m.role === "user" ? "me" : "ai",
                     content: m.content, steps: [] })));
    };

    // 个人中心指定了要打开某个会话 → 打开它；否则续最近一个
    const wanted = takePendingConv();
    const p = wanted != null
      ? api(`/api/conversations/${wanted}`)
      : api("/api/chat/history");

    p.then(d => {
      if (!alive || d._error) {
        // 指定的会话打不开（被删了？）→ 退回"续最近"
        if (wanted != null) {
          api("/api/chat/history").then(h => {
            if (!alive || h._error) return;
            apply(h); setConvId(h.conversation_id || null);
            setConvs(h.conversations || []);
          });
        }
        return;
      }
      apply(d);
      setConvId(wanted != null ? wanted : (d.conversation_id || null));
      // 列表仍从统一的端点取（指定会话时响应里没有列表）
      api("/api/conversations").then(c => {
        if (alive && !c._error) setConvs(c.conversations || []);
      });
    });
    return () => { alive = false; };
  }, [me]);

  useEffect(() => {
    const b = boxRef.current;
    if (b) b.scrollTop = b.scrollHeight;
  }, [msgs]);

  /* 切到另一个会话：整体替换消息与摘要。
   * ⚠️ 必须**整体替换**而不是追加——追加会把上一个会话的内容混进来，
   *    那正是"上下文不隔离"的表现。同时复位 steps/typing 等瞬态。 */
  const switchConv = async (id) => {
    if (busy || id === convId) return;
    const d = await api(`/api/conversations/${id}`);
    if (d._error) return;
    setConvId(id);
    setMeta({ summary: d.summary, compactions: d.compactions });
    setShowTrace({});
    setMsgs((d.messages || [])
      .filter(m => m.role === "user" || m.role === "assistant")
      .map(m => ({ role: m.role === "user" ? "me" : "ai",
                   content: m.content, steps: [] })));
  };

  /* 开一个新对话：只清空界面，**不落后端**（见 convId 的说明）。 */
  const newConv = () => {
    if (busy) return;
    setConvId(null);
    setMsgs([]);
    setMeta(null);
    setShowTrace({});
    setInput("");
  };

  const deleteConv = async (id, e) => {
    e.stopPropagation();
    if (busy) return;
    if (!window.confirm("删除这个对话？该对话的所有消息会一并删除，不可恢复。")) return;
    const r = await api(`/api/conversations/${id}`, { method: "DELETE" });
    if (r._error) { setActionError("删除失败：" + r._error); return; }
    setActionError("");
    const rest = convs.filter(c => c.id !== id);
    setConvs(rest);
    if (id === convId) newConv();     // 删的是当前会话 → 复位成"新对话"
  };

  const ask = async (q) => {
    q = (q || "").trim();
    if (!q || busy) return;
    if (!me) { onNeedLogin && onNeedLogin(); return; }
    setInput(""); setBusy(true);
    setMsgs(m => [...m, { role: "me", content: q, steps: [] },
                        { role: "ai", content: "", steps: [], err: "", compact: null }]);

    const patch = fn => setMsgs(m => {
      const c = m.slice(), i = c.length - 1;
      c[i] = Object.assign({}, c[i]); fn(c[i]); return c;
    });

    try {
      /* SSE 流式：不能用 api()（那个会把响应读成 JSON）。 */
      const res = await fetch("/api/chat", {
        method: "POST",
        credentials: "same-origin",
        headers: writeHeaders(),
        /* 带上当前会话：
         *   convId 有值 → 在这个会话里继续
         *   convId 为 null（刚点过"新对话"）→ new:true，让后端建一个
         * ⚠️ 仍然**不带 user 字段**：身份只能由服务端从凭证判定。 */
        body: JSON.stringify(convId != null
          ? { question: q, conversation_id: convId }
          : { question: q, new: true }),
      });
      if (res.status === 401) {
        patch(t => { t.err = "登录已过期，请重新登录。"; });
        onNeedLogin && onNeedLogin(); setBusy(false); return;
      }
      if (!res.ok) {
        const failure = await res.json();
        throw new Error(failure.detail || res.statusText);
      }
      const rd = res.body.getReader(), dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await rd.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const chunks = buf.split("\n\n"); buf = chunks.pop();
        for (const line of chunks) {
          if (!line.startsWith("data: ")) continue;
          const raw = line.slice(6);
          if (raw === "[DONE]") continue;
          let ev; try { ev = JSON.parse(raw); } catch { continue; }
          if (ev.type === "tool_call") patch(t => t.steps.push({ state: "run" }));
          else if (ev.type === "tool_result")
            patch(t => {
              const last = [...t.steps].reverse().find(x => x.state === "run");
              if (last) last.state = ev.ok ? "ok" : "bad";
            });
          else if (ev.type === "token") patch(t => { t.content += ev.text; });
          else if (ev.type === "compact") patch(t => { t.compact = ev; });
          else if (ev.type === "error") patch(t => { t.err = ev.message; });
          else if (ev.type === "done") {
            patch(t => {
              if (ev.answer && !t.content) t.content = ev.answer;
              if (ev.error) t.err = ev.error;
              /* ⚠️ 必须把**残留的 "run" 步骤收尾**。
               *    steps 只在 `tool_result` 到达时才由 "run" → "ok"。
               *    若最后一步工具调用没等到结果（中断 / 报错 / 迭代上限），
               *    那个 "run" 会永远留着 → `running` 恒为真 →
               *    **回答早已结束，却一直按"正在查阅"的纯文本样式显示**：
               *    用户看到的是一堆 `##`、`|`、`**` 原文，而不是排好的 markdown。
               *    收到 done 就说明本轮结束了，无论步骤是否都拿到结果。 */
              t.steps.forEach(s => { if (s.state === "run") s.state = "ok"; });
            });
            if (ev.guard) setMeta(x => ({ ...(x || {}), lastGuard: ev.guard }));
          }
        }
      }
    } catch (e) { patch(t => { t.err = String(e); }); }
    /* 兜底：万一 `done` 事件没送到（连接中途断掉），这里也要把残留的
     * "run" 收尾——否则那条消息会**永远**显示成"正在查阅"，正文永远
     * 停在纯文本样式。宁可显示成已结束，也不要卡在假的"进行中"。 */
    patch(t => t.steps.forEach(s => { if (s.state === "run") s.state = "bad"; }));
    setBusy(false);
    /* 本轮结束后刷新会话列表：
     * "新对话"的首轮提问会让后端**新建**一个会话（并对它起标题），
     * 这里把新 id 与列表同步过来——否则下拉里看不到刚建的会话，
     * 下次提问又会带 new:true 再造一个。 */
    api("/api/conversations").then(d => {
      if (d._error) return;
      setConvs(d.conversations || []);
      if (convId == null && d.conversation_id != null) setConvId(d.conversation_id);
    });
  };

  const SUGGEST = [
    "资料中有哪些成本口径说明？",
    "哪些费用需要分摊，依据是什么？",
    "当前资料有哪些缺失和边界？",
  ];

  if (!me) {
    return html`
      <section class="hero">
        <h1 class="hero-title">资料检索与问答</h1>
        <p class="hero-sub">问一句，每个数字都带你回到它的出处。</p>
        <${LoginGate} what="智能问答（查的是你自己的数据区）"
                      onGoLogin=${onNeedLogin} />
      </section>`;
  }

  return html`
    <section class="hero">
      <h1 class="hero-title">资料检索与问答</h1>
      <p class="hero-sub">问一句，每个数字都带你回到它的出处。</p>

      <div class="conv-bar">
        <span class="conv-label">会话</span>
        <select class="conv-select" value=${convId == null ? "" : String(convId)}
                disabled=${busy}
                onChange=${e => { const v = e.target.value; if (v) switchConv(Number(v)); }}>
          <option value="">新对话（尚未提问）</option>
          ${convs.map(c => html`<option key=${c.id} value=${String(c.id)}>
            ${(c.title || "未命名")}${c.n_msg ? `（${c.n_msg}）` : ""}
          </option>`)}
        </select>
        <button class="conv-new" type="button" disabled=${busy} onClick=${newConv}
                title="开一个新对话。不同对话的上下文互相隔离。">+ 新对话</button>
        ${convId != null ? html`
          <button class="conv-del" type="button" disabled=${busy}
                  title="删除当前会话"
                  onClick=${e => deleteConv(convId, e)}>删除</button>` : null}
      </div>
      ${actionError ? html`<div class="alert">${actionError}</div>` : null}

      <form class="askbar" onSubmit=${e => { e.preventDefault(); ask(input); }}>
        <span class="askbar-icon">💬</span>
        <input value=${input} disabled=${busy}
               placeholder="检索资料中的口径、说明或引用依据"
               onInput=${e => setInput(e.target.value)} />
        <button type="submit" disabled=${busy || !input.trim()}>
          ${busy ? "…" : "提问"}
        </button>
      </form>

      ${!msgs.length ? html`
        <div class="suggest">
          ${SUGGEST.map((s, i) => html`
            <button key=${i} class="chip" onClick=${() => ask(s)}>${s}</button>`)}
        </div>` : null}

      ${msgs.length ? html`
        <div class="chat" ref=${boxRef}>
          ${meta && meta.summary ? html`
            <details class="chat-summary">
              <summary>此前对话的摘要（点击展开）</summary>
              <pre>${meta.summary}</pre>
            </details>` : null}

          ${msgs.map((m, i) => {
            if (m.role === "me") return html`
              <div class="bubble me" key=${i}><div class="txt">${m.content}</div></div>`;
            const running = m.steps.some(s => s.state === "run");
            // 长回答才给导出。阈值见 MD_EXPORT_MIN_CHARS 的说明。
            const canExport = !running && (m.content || "").length >= MD_EXPORT_MIN_CHARS;
            return html`
              <div class="bubble ai" key=${i}>
                ${m.steps.length ? html`
                  <div class="trace">
                    <button class="trace-toggle"
                            onClick=${() => setShowTrace(x => ({ ...x, [i]: !x[i] }))}>
                      <span>${showTrace[i] ? "▾" : "▸"}</span>
                      ${running ? "正在查阅资料…" : `已查阅 ${m.steps.length} 处资料`}
                    </button>
                    ${showTrace[i] ? html`
                      <div class="trace-list">
                        ${m.steps.map((s, j) => html`
                          <div class="trace-row" key=${j}>
                            <span class=${"dot " + s.state}></span>
                            ${s.state === "run" ? "查阅中…" : "已查阅"}
                          </div>`)}
                      </div>` : null}
                  </div>` : null}
                ${m.compact ? html`<div class="compact-note">
                  为保持对话流畅，此前部分内容已归纳为摘要</div>` : null}
                ${running
                  ? html`<div class="txt typing">
                      ${m.content || "正在查阅资料，请稍候…"}</div>`
                  : html`<div class="txt md"
                           dangerouslySetInnerHTML=${{ __html: markdownToHtml(m.content) }} />`}
                ${canExport ? html`
                  <div class="md-actions">
                    <button class="md-export" title="把这条回答按原始 Markdown 存成本地 .md 文件"
                            onClick=${() => downloadMarkdown(m.content, i)}>
                      导出 .md
                    </button>
                  </div>` : null}
                ${m.err ? html`<div class="alert" style=${{ marginTop: 10 }}>${m.err}</div>` : null}
              </div>`;
          })}
        </div>` : null}
    </section>`;
}


/* =============================================================================
 * 首页第 ② 段：把资料加进来（拖拽上传）
 *
 * ⚠️ 上传的资料只进**你自己的个人工作区**，不影响其他人和共享基线。
 *    这一点必须写清楚——否则用户会以为自己在改全公司的库。
 * ========================================================================== */
function UploadZone({ me, onNeedLogin, onChanged }) {
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState(false);
  const [logs, setLogs] = useState([]);
  const [note, setNote] = useState("");
  const [err, setErr] = useState("");
  const dirRef = useRef(null);
  const fileRef = useRef(null);
  const logRef = useRef(null);

  useEffect(() => {
    const b = logRef.current;
    if (b) b.scrollTop = b.scrollHeight;
  }, [logs]);

  const upload = async (fileList) => {
    const files = Array.from(fileList || []).filter(f => f.size > 0);
    if (!files.length) return;
    if (!me) { onNeedLogin && onNeedLogin(); return; }
    setBusy(true); setNote(""); setErr("");
    try {
      const fd = new FormData();
      files.forEach(f => fd.append("files", f, f.name));
      fd.append("paths", JSON.stringify(files.map(f => f.webkitRelativePath || "")));
      /* ⚠️ multipart 请求**不能**用 writeHeaders()：
       *    `Content-Type` 必须让浏览器自己带 boundary，写死了服务端解析不了。
       *    只加 CSRF 头。 */
      const r = await fetch("/api/control/upload", {
        method: "POST", credentials: "same-origin",
        headers: { "X-CSRF-Token": getCookie("lw_csrf") }, body: fd,
      });
      if (r.status === 401 || r.status === 403)
        throw new Error("登录已过期，请重新登录");
      const d = await r.json();
      const okN = (d.saved || []).length, badN = (d.rejected || []).length;
      setNote(`已接收 ${okN} 个文件` + (badN ? `，拒绝 ${badN} 个` : "") +
        (badN ? `：${d.rejected.map(x => x.file + "（" + x.reason + "）").join("；")}` : ""));
      onChanged && onChanged();
      if (okN) setTimeout(() => runIngest(), 300);   // 上传完自动开编译
    } catch (e) { setErr(String(e.message || e)); }
    setBusy(false);
  };

  /* 摄入（SSE）：LLM 编译通常 30–120 秒，故流式推进度 */
  const runIngest = async () => {
    setBusy(true); setLogs([{ k: "start", t: "开始处理你上传的资料…" }]);
    try {
      const res = await fetch("/api/control/ingest", {
        method: "POST", credentials: "same-origin",
        headers: { "X-CSRF-Token": getCookie("lw_csrf") },
      });
      if (res.status === 401 || res.status === 403)
        throw new Error("登录已过期，请重新登录");
      const rd = res.body.getReader(), dec = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await rd.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n\n"); buf = parts.pop();
        for (const line of parts) {
          if (!line.startsWith("data: ")) continue;
          const raw = line.slice(6);
          if (raw === "[DONE]") continue;
          let ev; try { ev = JSON.parse(raw); } catch { continue; }
          if (ev.type === "log") setLogs(L => [...L, { k: "log", t: ev.line }]);
          else if (ev.type === "done")
            setLogs(L => [...L, { k: ev.ok ? "ok" : "bad", t: ev.message }]);
          else if (ev.type === "error")
            setLogs(L => [...L, { k: "bad", t: ev.message }]);
        }
      }
      onChanged && onChanged();
    } catch (e) {
      setLogs(L => [...L, { k: "bad", t: String(e.message || e) }]);
    }
    setBusy(false);
  };

  return html`
    <section class="home-sec">
      <h2 class="sec-title">补充成本分析资料
        <span class="sec-hint">仅你可见</span>
      </h2>
      <p class="sec-sub">
        拖入你手头的资料（报表、工艺说明、检查记录…），会被编译进
        <strong>你自己的</strong>知识库，检索与问答都能用到。
        不影响其他成员，也不改动公司共享基线。
      </p>

      <div class=${"drop" + (drag ? " on" : "")}
           onDragOver=${e => { e.preventDefault(); setDrag(true); }}
           onDragLeave=${() => setDrag(false)}
           onDrop=${e => { e.preventDefault(); setDrag(false); upload(e.dataTransfer.files); }}>
        <div class="drop-icon">⬆</div>
        <div class="drop-title">把文件拖到这里</div>
        <div class="drop-sub">
          支持 .md .txt .csv .xlsx .docx .pdf .json ｜ 上传后自动编译
        </div>
        <div class="drop-btns">
          <button type="button" class="chip" disabled=${busy}
                  onClick=${() => fileRef.current && fileRef.current.click()}>选择文件</button>
          <button type="button" class="chip" disabled=${busy}
                  onClick=${() => dirRef.current && dirRef.current.click()}>选择文件夹</button>
        </div>
        <input ref=${fileRef} type="file" multiple hidden
               accept=".md,.txt,.csv,.xlsx,.xls,.docx,.pdf,.json"
               onChange=${e => { upload(e.target.files); e.target.value = ""; }} />
        <input ref=${dirRef} type="file" webkitdirectory multiple hidden
               onChange=${e => { upload(e.target.files); e.target.value = ""; }} />
      </div>

      ${note ? html`<div class="note" style=${{ marginTop: 12 }}>${note}</div>` : null}
      ${err ? html`<div class="alert" style=${{ marginTop: 12 }}>⚠️ ${err}</div>` : null}
      ${busy ? html`<div class="note" style=${{ marginTop: 12 }}>
        处理中…（编译通常 30–120 秒）</div>` : null}

      ${logs.length ? html`
        <div class="ingest-log" ref=${logRef}>
          ${logs.map((l, i) => html`
            <div class=${"ing-line " + l.k} key=${i}>
              <span class="ing-dot"></span>${l.t}
            </div>`)}
        </div>` : null}
    </section>`;
}


/* 我的工作区（清单 + 删除）—— 折叠在次要位置 */
function WorkspaceList({ me, refreshKey }) {
  const [ws, setWs] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const generation = useRef(0);
  const isDocumentPath = value => {
    if (typeof value !== "string" || !value) return false;
    const parts = value.split(String.fromCharCode(92)).join("/").split("/").filter(part => part && part !== ".");
    if (!parts.length || parts.includes("..")) return false;
    const first = parts[0].normalize("NFKC").toLowerCase();
    return !["costing_datasets", "costing_snapshots", "reports"].includes(first);
  };
  const load = async () => {
    const request = ++generation.current;
    if (!me) { setWs(null); setLoading(false); return; }
    setLoading(true);
    const data = await api("/api/control/workspace?documents_only=true");
    if (request !== generation.current) return;
    if (data._error) { setErr(data._error); setWs(null); }
    else {
      setWs({
        raw: (data.raw || []).filter(file => isDocumentPath(file.path)),
        wiki: (data.wiki || []).filter(file => isDocumentPath(file.path))
      });
      setErr("");
    }
    setLoading(false);
  };
  useEffect(() => {
    setWs(null); setErr("");
    load();
    return () => { ++generation.current; };
  }, [me, refreshKey]);
  const del = async path => {
    if (!isDocumentPath(path)) { setErr("\u6838\u7b97\u6570\u636e\u4e0e\u62a5\u544a\u8bf7\u901a\u8fc7\u6570\u636e\u63a5\u5165\u53ca\u62a5\u544a\u5165\u53e3\u7ba1\u7406\u3002"); return; }
    if (!confirm("\u786e\u5b9a\u5220\u9664\u300c" + path + "\u300d\uff1f\u6b64\u64cd\u4f5c\u4ec5\u4f5c\u7528\u4e8e\u5f53\u524d\u8d26\u6237\u7684\u8865\u5145\u8d44\u6599\u3002")) return;
    const request = generation.current;
    const response = await api("/api/control/workspace?path=" + encodeURIComponent(path), { method: "DELETE" });
    if (request !== generation.current) return;
    if (response._error) { setErr("\u5220\u9664\u5931\u8d25\uff1a" + response._error); return; }
    load();
  };
  if (!me) return html`<${LoginGate} what="\u4e2a\u4eba\u8d44\u6599" onGoLogin=${() => (location.hash = "login")} />`;
  if (loading || (!ws && !err)) return html`<p class="muted">\u6b63\u5728\u8bfb\u53d6\u4e2a\u4eba\u8d44\u6599\u2026</p>`;
  const raw = ws?.raw || [], wiki = ws?.wiki || [];
  const table = (items, title) => html`<h3 class="ws-h">${title}</h3><table class="tbl">
    <thead><tr><th>\u6587\u4ef6</th><th class="num">\u5927\u5c0f</th><th></th></tr></thead><tbody>
      ${items.map(file => html`<tr key=${file.path}><td>${file.path}</td>
        <td class="num">${Number.isFinite(file.size) ? (file.size / 1024).toFixed(1) + " KB" : "\u672a\u63d0\u4f9b"}</td>
        <td><button class="linkbtn" onClick=${() => del(file.path)}>\u5220\u9664</button></td></tr>`)}
    </tbody></table>`;
  return html`<details class="wsbox" open=${raw.length + wiki.length > 0}><summary>
    \u6211\u7684\u8865\u5145\u8d44\u6599 <span class="hint">\u8d44\u6599 ${raw.length} \u4efd \u00b7 \u8bcd\u6761 ${wiki.length} \u7bc7</span></summary>
    ${err ? html`<p class="alert" role="alert">${err}</p>` : null}
    ${!raw.length && !wiki.length ? html`<div class="empty">\u8fd8\u6ca1\u6709\u8865\u5145\u8d44\u6599\u3002\u4e0a\u4f20\u4e1a\u52a1\u8bf4\u660e\u540e\u53ef\u5728\u8fd9\u91cc\u67e5\u770b\u3002\u6838\u7b97\u539f\u59cb\u6587\u4ef6\u4e0e\u62a5\u544a\u901a\u8fc7\u5bf9\u5e94\u5de5\u4f5c\u53f0\u5165\u53e3\u6838\u5bf9\u3002</div>` : null}
    ${raw.length ? table(raw, "\u8d44\u6599\u6587\u4ef6") : null}
    ${wiki.length ? table(wiki, "\u8d44\u6599\u8bcd\u6761") : null}
  </details>`;
}

/* 导出文档 —— 视觉上降为次要，放在上传区之后 */











function ReportGen({ me, onGenerated }) {
  const { data: prod } = useApi("/api/products");
  const { data: th } = useApi("/api/themes");
  const [product, setProduct] = useState("银黄口服液");
  const [month, setMonth] = useState("2026-06");
  const [theme, setTheme] = useState("monthly");
  const [period, setPeriod] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [useLlm, setUseLlm] = useState(false);

  const MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"];
  const t = ((th && th.themes) || []).find(x => x.id === theme) || {};
  const opts = t.period_options || [];

  useEffect(() => {
    const l = prod && prod.products;
    if (l && l.length && !l.includes(product)) setProduct(l[0]);
  }, [prod]);
  // 切主题时重置期间为该项的第一个可选项
  useEffect(() => {
    setPeriod(opts.length ? opts[0] : "");
    // eslint-disable-next-line
  }, [theme, th]);

  const gen = async () => {
    setBusy(true); setMsg(""); setErr("");
    try {
      const r = await api("/api/control/generate", {
        method: "POST",
        body: { product, month, theme, period, use_llm: useLlm },
      });
      if (r._error) throw new Error(r._error);
      setMsg(`已生成 ${r.file}（${r.stats.deterministic_fields} 个确定性值）`);
      onGenerated && onGenerated();
    } catch (e) { setErr("生成失败：" + String(e.message || e)); }
    setBusy(false);
  };

  if (!me) return null;      // 生成要花 token，未登录不显示入口

  return html`
    <details class="wsbox" open>
      <summary>药企共享样例报告 <span class="hint">已验证主题以选项为准</span></summary>
      <p class="sec-sub" style=${{ marginTop: 12 }}>
        当前入口使用药企共享样例；个人CSV、餐饮与制造业请在「通用成本分析」保存当前结果。
        选择分析主题、期间与产品，可离线生成完整核算报告；勾选后可使用Flash补充受证据约束的叙述。
        生成后在下方导出Word / PDF，图表和来源随该次结果保存。
      </p>
      <div class="toolbar" style=${{ flexWrap: "wrap", gap: 10 }}>
        <label>分析主题</label>
        <select value=${theme} onChange=${e => setTheme(e.target.value)}>
          ${((th && th.themes) || []).map(x => html`
            <option key=${x.id} value=${x.id}>${x.name}</option>`)}
        </select>

        ${theme === "monthly" || theme === "quarterly" || theme === "topic" ? html`
          <label>${theme === "monthly" ? "分析月份" : "资料截止月份"}</label>
          <select value=${month} onChange=${e => setMonth(e.target.value)}>
            ${MONTHS.map(m => html`<option key=${m} value=${m}>${m}</option>`)}
          </select>` : null}

        ${opts.length ? html`
          <label>${t.period_label || "期间"}</label>
          <select value=${period} onChange=${e => setPeriod(e.target.value)}>
            ${opts.map(o => html`<option key=${o} value=${o}>${o}</option>`)}
          </select>` : null}

        <label>产品</label>
        <select value=${product} onChange=${e => setProduct(e.target.value)}>
          ${((prod && prod.products) || []).map(p => html`
            <option key=${p} value=${p}>${p}</option>`)}
        </select>

        <button class="chip" type="button" disabled=${busy} onClick=${gen}>
          ${busy ? "正在生成并冻结报告与来源…" : "生成报告"}
        </button>
        <label><input type="checkbox" checked=${useLlm} disabled=${busy} onChange=${e => setUseLlm(e.target.checked)} /> 使用Flash补充叙述</label>
      </div>
      ${msg ? html`<div class="alert ok" style=${{ marginTop: 10 }}>✅ ${msg}</div>` : null}
      ${err ? html`<div class="alert" style=${{ marginTop: 10 }}>${err}</div>` : null}
    </details>`;
}


function ExportBar({ me }) {
  const [docs, setDocs] = useState(null);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [reportSources, setReportSources] = useState(null);
  const [sourceDetail, setSourceDetail] = useState(null);

  useEffect(() => {
    if (!me) { setDocs(null); return; }
    api("/api/control/exports").then(d => setDocs(d._error ? null : d));
  }, [me]);

  /* POST 拿文件流，用 blob 下载——不能直接 <a href>，导出要带会话 cookie。
   * ⚠️ 下载流不能用 api()（那个按 JSON 解析），得走原生 fetch。 */
  const doExport = async (kind, md, fmt, charts) => {
    const key = kind + fmt + (charts ? "c" : "");
    setBusy(key); setErr("");
    try {
      const r = await fetch("/api/control/export", {
        method: "POST", credentials: "same-origin",
        headers: writeHeaders(),
        // charts=True 时服务端重绘三张图嵌进文档（赛题 5.1.3「图表嵌入」）
        body: JSON.stringify({ kind, md, fmt, charts: !!charts }),
      });
      if (r.status === 401 || r.status === 403)
        throw new Error("登录已过期，请重新登录");
      if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
      const blob = await r.blob();
      const cd = r.headers.get("content-disposition") || "";
      const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/.exec(cd);
      const name = m ? decodeURIComponent(m[1]) : `${md || kind}.${fmt}`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
    } catch (e) { setErr("导出失败：" + String(e.message || e)); }
    setBusy("");
  };
  const inspectReportSource = async (report, evidenceId = "") => {
    setBusy("source"); setErr("");
    const data = await api("/api/control/report-evidence?" + new URLSearchParams({ report, evidence_id: evidenceId }));
    if (data._error) setErr(data._error);
    else if (evidenceId) setSourceDetail(data.evidence);
    else { setReportSources(data); setSourceDetail(null); }
    setBusy("");
  };

  if (!me) return html`<${LoginGate} what="文档导出"
                               onGoLogin=${() => (location.hash = "login")} />`;

  return html`
    <${ReportGen} me=${me} onGenerated=${() =>
      api("/api/control/exports").then(d => { if (!d._error) setDocs(d); })} />

    <details class="wsbox">
      <summary>导出文档 <span class="hint">Word / PDF</span></summary>
      <p class="sec-sub" style=${{ marginTop: 12 }}>
        导出的文档保留完整排版：页眉页脚、表格样式。
      </p>
      ${err ? html`<div class="alert" style=${{ marginBottom: 12 }}>⚠️ ${err}</div>` : null}
      ${docs && docs.reports && docs.reports.length ? html`
        <h3 class="ws-h">已保存分析报告</h3>
        ${docs.reports.map(r => html`
          <div class="exp-row" key=${r.name}>
            <span class="exp-name">${r.stem.replace("_", " · ")}</span>
            ${["docx", "pdf"].map(f => html`
              <button key=${f} class="chip" disabled=${busy}
                      onClick=${() => doExport("report", r.name, f, true)}>
                ${busy === "report" + f + "c" ? "导出中…" : f.toUpperCase() + "（含图）"}
              </button>`)}
            ${r.analysis_id ? html`<a class="chip" href=${"#costing?analysis=" + r.analysis_id}>打开原分析</a>` : html`<button class="chip" disabled=${!!busy} onClick=${() => inspectReportSource(r.name)}>预览与核对来源</button>`}
          </div>`)}` : html`
        <div class="empty" style=${{ padding: "18px 0" }}>暂无可导出的报告</div>`}
      ${reportSources ? html`<div class="costing-evidence"><h4>${reportSources.report} · 来源记录</h4><p class="costing-version">来源版本 ${reportSources.context.source_revision || "历史文件未冻结来源"}</p>
        <details class="costing-gap"><summary>查看该版本报告正文</summary><div class="md-render costing-report" dangerouslySetInnerHTML=${{ __html: markdownToHtml(reportSources.markdown || "") }}></div></details>
        ${reportSources.evidence.length ? html`<div class="costing-table-wrap"><table class="costing-table"><thead><tr><th>来源</th><th>字段 / 条件</th><th></th></tr></thead><tbody>${reportSources.evidence.map(item => html`<tr key=${item.evidence_id}><td>${item.source_uri}</td><td>${item.field} / ${item.selector}</td><td><button class="linkbtn" disabled=${!!busy} onClick=${() => inspectReportSource(reportSources.report, item.evidence_id)}>查看原始行列</button></td></tr>`)}</tbody></table></div>` : html`<p>这份历史文件没有冻结来源，请重新生成已保存版本以核对原始行列。</p>`}
        ${sourceDetail ? html`<h5>${sourceDetail.source_uri}</h5><p>${sourceDetail.description}</p><p class="costing-version">${sourceDetail.sha256 || ""}</p><pre class="costing-audit">${JSON.stringify(sourceDetail.rows || sourceDetail.text || sourceDetail, null, 2)}</pre>` : null}
      </div>` : null}
    </details>`;
}


/* =============================================================================
 * 首页第 ③ 段：分析（图表 + 知识图谱）
 * ========================================================================== */
function HomeCharts({ month, setMonth }) {
  const { data, loading, error } = useApi(`/api/overview?month=${month}`);
  const MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"];
  const MIX = [
    { key: "材料",     color: "#0F766E" },
    { key: "人工",     color: "#F59E0B" },
    { key: "制造费用", color: "#6366F1" },
  ];

  if (loading) return html`<section class="home-sec"><${Loading} /></section>`;
  if (error) return html`<section class="home-sec"><${ErrorBox} msg=${error} /></section>`;

  const bars = data.items
    .filter(it => it.facts["本月单位成本"] != null)
    .map((it, i) => ({
      label: it.product.replace(/(口服液|颗粒|胶囊)$/, ""),
      value: it.facts["本月单位成本"],
      color: ["#0F766E", "#0891B2", "#6366F1"][i % 3],
    }));

  const first = data.items.find(it => it.facts["本月单位成本"] != null);
  const waterfallRows = (() => {
    if (!first) return [];
    const f = first.facts;
    const prev = f["上月单位成本"], cur = f["本月单位成本"];
    if (prev == null || cur == null) return [];
    const items = [
      { name: "起点", amount: 0, share: null },
      { name: "直接材料", amount: (f["本月材料成本"] ?? 0) - (f["上月材料成本"] ?? 0),
        share: f["材料贡献度"] },
      { name: "直接人工", amount: (f["本月人工成本"] ?? 0) - (f["上月人工成本"] ?? 0),
        share: f["人工贡献度"] },
      { name: "制造费用", amount: (f["本月制造成本"] ?? 0) - (f["上月制造成本"] ?? 0),
        share: f["制造费用贡献度"] },
      { name: "单位成本变动", amount: +(cur - prev).toFixed(2), share: null },
    ];
    let cum = 0;
    items.forEach((it, i) => {
      if (i === 0) { it.cum = 0; return; }
      if (i === items.length - 1) { it.cum = it.amount; return; }
      cum += it.amount; it.cum = cum;
    });
    return items;
  })();

  const donutItems = first ? [
    { name: "直接材料", value: first.facts["本月材料成本"] || 0, color: "#0F766E" },
    { name: "直接人工", value: first.facts["本月人工成本"] || 0, color: "#F59E0B" },
    { name: "制造费用", value: first.facts["本月制造成本"] || 0, color: "#6366F1" },
  ] : [];

  const mLabel = month.replace("-", " 年 ") + " 月";

  return html`
    <section class="home-sec">
      <div class="sec-head">
        <h2 class="sec-title">本月成本分析</h2>
        <div class="toolbar">
          <select value=${month} onChange=${e => setMonth(e.target.value)}>
            ${MONTHS.map(m => html`<option key=${m} value=${m}>${m.replace("-", " 年 ")} 月</option>`)}
          </select>
          <span class="pill">单位：元 / 盒</span>
        </div>
      </div>

      <div class="card">
        <h3>三产品单位成本对比</h3>
        <p class="card-sub">单位成本指每盒成品的综合生产成本，含直接材料、直接人工与制造费用。</p>
        <${BarChart} items=${bars} title=${`三产品成本对比-${month}`} />
      </div>

      <div class="chart2">
        <div class="card">
          <h3>成本变动分解</h3>
          <p class="card-sub">各成本要素对本月单位成本变动的贡献——柱高即该项变动额，最后一根是合计变动。</p>
          <${WaterfallChart} rows=${waterfallRows} title=${`成本变动分解-${month}`} />
        </div>
        <div class="card">
          <h3>本月成本构成</h3>
          <p class="card-sub">直接材料、直接人工、制造费用三项占单位成本的比重。</p>
          <${DonutChart} items=${donutItems} total=${first ? first.facts["本月单位成本"] : null}
                         unit="元/盒" title=${`成本构成-${month}`} />
        </div>
      </div>

      <div class="grid3">
        ${data.items.map(it => {
          const f = it.facts, d = it.display || {};
          const mix = MIX.map(m => ({
            ...m,
            share: f[m.key === "材料" ? "材料贡献度" : m.key === "人工" ? "人工贡献度" : "制造费用贡献度"] || 0,
            amount: f[`本月${m.key === "材料" ? "材料" : m.key === "人工" ? "人工" : "制造"}成本`],
          }));
          const hasMix = mix.some(m => m.share > 0);
          return html`
            <div class="kpi" key=${it.product}>
              <div class="kpi-name">${it.product}</div>
              <div class="kpi-value">
                ${d["本月单位成本"] || "—"}<span class="unit">元/盒</span>
              </div>
              <div class="kpi-deltas">
                <div>
                  <span class="lab">环比</span>
                  <span class=${"val " + cls(f["单位成本环比"])}>
                    <span class="arrow">${ARROW(f["单位成本环比"])}</span>
                    ${(d["单位成本环比"] || "—").replace("+", "")}
                  </span>
                </div>
                <div>
                  <span class="lab">同比</span>
                  <span class=${"val " + cls(f["单位成本同比"])}>
                    <span class="arrow">${ARROW(f["单位成本同比"])}</span>
                    ${(d["单位成本同比"] || "—").replace("+", "")}
                  </span>
                </div>
                <div>
                  <span class="lab">预算偏差</span>
                  <span class=${"val " + cls(f["单位成本预算偏差"])}>
                    ${(d["单位成本预算偏差"] || "—").replace("+", "")}
                  </span>
                </div>
              </div>
              ${hasMix ? html`
                <div class="mix">
                  <div class="mix-title">本月成本变动的来源</div>
                  <div class="mix-bar">
                    ${mix.map(m => html`<i key=${m.key}
                      style=${{ width: m.share + "%", background: m.color }}></i>`)}
                  </div>
                  <div class="mix-legend">
                    ${mix.map(m => html`<span key=${m.key}>
                      <i style=${{ background: m.color }}></i>
                      ${m.key} ${fmt(m.amount)} · ${fmt(m.share, 1)}%
                    </span>`)}
                  </div>
                </div>` : null}
            </div>`;
        })}
      </div>
      <p class="sec-foot">分析月份：${mLabel}</p>
    </section>`;
}












function HomeForecast() {
  const { data: prod } = useApi("/api/products");
  const [product, setProduct] = useState("");
  useEffect(() => {
    const l = prod && prod.products;
    if (l && l.length && !product) setProduct(l[0]);
  }, [prod, product]);

  const q = product
    ? `/api/forecast?product=${encodeURIComponent(product)}&horizon=2` : "";
  const { data, loading, error } = useApi(q || "/api/products");

  if (!product || loading) return html`<section class="home-sec"><${Loading} /></section>`;
  if (error) return html`<section class="home-sec"><${ErrorBox} msg=${error} /></section>`;
  if (!data || !data.predictions || !data.predictions.length) return null;

  const last = data.history[data.history.length - 1];
  const p1 = data.predictions[0];
  const chg = last ? (p1.预测值 - last.单位成本) : 0;

  return html`
    <section class="home-sec">
      <div class="card fc-card">
        <h2>成本趋势预测
          <span class="fc-tag">外推</span>
          <span class="hint">${data.method}　·　样本 ${data.n_samples} 个点</span>
        </h2>
        <p class="card-sub">
          基于历史单位成本外推，<strong>不是已发生的数</strong>——
          预测值没有原始坐标，与看板其余数字的性质不同，请连同区间一起读。
        </p>

        <div class="toolbar" style=${{ marginBottom: 16 }}>
          <label>产品</label>
          <select value=${product} onChange=${e => setProduct(e.target.value)}>
            ${((prod && prod.products) || []).map(p => html`
              <option key=${p} value=${p}>${p}</option>`)}
          </select>
        </div>

        <table class="tbl">
          <thead><tr>
            <th>月份</th><th class="num">预测值（元/盒）</th>
            <th class="num">参考区间</th><th>相对上月实际</th>
          </tr></thead>
          <tbody>
            <tr>
              <td>${last.月份}<span class="hint">（实际）</span></td>
              <td class="num">${fmt(last.单位成本)}</td>
              <td class="num">—</td><td>—</td>
            </tr>
            ${data.predictions.map(p => html`
              <tr key=${p.月份}>
                <td>${p.月份}</td>
                <td class="num strong">${fmt(p.预测值)}</td>
                <td class="num">${fmt(p.下限)} ~ ${fmt(p.上限)}</td>
                <td class=${cls(p.步长 === 1 ? chg : (p.预测值 - last.单位成本))}>
                  ${p.步长 === 1 && last
                    ? `${ARROW(chg)} ${(chg / last.单位成本 * 100).toFixed(2)}%`
                    : "—"}
                </td>
              </tr>`)}
          </tbody>
        </table>

        ${(data.warnings || []).length ? html`
          <div class="fc-warn">
            ${data.warnings.map((w, i) => html`<div key=${i}>⚠️ ${w}</div>`)}
          </div>` : null}
      </div>
    </section>`;
}


function HomeHeatmap() {
  const { data, loading, error } = useApi("/api/heatmap");
  const [el, setEl] = useState("单位材料");
  const els = (data && data.elements) || [];

  /* ⚠️ 这个 hook **必须在任何提前 return 之前**。
   *    放在 return 之后会触发 React #310
   *    （"Rendered more hooks than during the previous render"）——
   *    hooks 数量在两次渲染间必须一致，提前 return 会让后面的 hook 被跳过。
   *    这是本文件踩过的坑，**加载态出现时整页白屏**，日志里只有一串 minified 错误号。 */
  useEffect(() => {
    if (els.length && !els.includes(el)) setEl(els[0]);
  }, [data]);

  if (loading) return html`<section class="home-sec"><${Loading} /></section>`;
  if (error) return html`<section class="home-sec"><${ErrorBox} msg=${error} /></section>`;

  return html`
    <section class="home-sec">
      <div class="sec-head">
        <h2 class="sec-title">异动热力图
          <span class="sec-hint">产品 × 月份 × 成本要素</span>
        </h2>
        <div class="toolbar">
          <select value=${el} onChange=${e => setEl(e.target.value)}>
            ${els.map(k => html`<option key=${k} value=${k}>${k}</option>`)}
          </select>
        </div>
      </div>
      <p class="sec-sub">
        颜色是<strong>逐月环比变动率</strong>，不是绝对金额——
        三个产品单价量级差两倍多，比绝对值只会得出"六味最深"这种废话。
        色阶以 0 为中心对称：<strong>红涨绿跌</strong>，越深变动越大。
      </p>
      <div class="card">
        <${HeatmapChart} data=${data} element=${el}
                         title=${`异动热力图-${el}`} />
      </div>
    </section>`;
}



function HomeGraph() {
  const [focus, setFocus] = useState("银黄口服液");
  const [hops, setHops] = useState(2);
  /* key：换中心 / 换跳数 / 点"重置视图"时 +1，让图**整个重新挂载**。
   * 图谱的力导向布局每次都会重算，所以重挂载后落点也变——
   * 这是布局本身的性质，不是 bug；用户要的是"能主动重置"。 */
  const [viewKey, setViewKey] = useState(0);
  const [sel, setSel] = useState(null);        // 当前选中的节点（与"中心"区分）
  const q = `focus=${encodeURIComponent(focus)}&hops=${hops}`;
  const { data, loading, error } = useApi(`/api/graph?${q}`);
  const PRODS = ["银黄口服液", "板蓝根颗粒", "六味地黄胶囊"];

  const reCenter = () => {
    if (!sel || sel === focus) return;
    setFocus(sel); setSel(null); setViewKey(k => k + 1);
  };

  const selNode = sel && data && data.nodes.find(n => n.id === sel);

  return html`
    <section class="home-sec">
      <div class="sec-head">
        <h2 class="sec-title">知识图谱
          <span class="sec-hint">配方 · 工艺 · 设备 · 成本异动</span>
        </h2>
        <div class="toolbar">
          <select value=${focus} onChange=${e => { setFocus(e.target.value); setSel(null); }}>
            ${PRODS.map(p => html`<option key=${p} value=${p}>${p}</option>`)}
          </select>
          <select value=${String(hops)} onChange=${e => { setHops(+e.target.value); setSel(null); }}>
            <option value="1">1 跳</option>
            <option value="2">2 跳</option>
            <option value="3">3 跳</option>
          </select>
          <button class="chip" onClick=${() => setViewKey(k => k + 1)}
                  title="把拖走的图恢复到初始位置">重置视图</button>
        </div>
      </div>
      <p class="sec-sub">
        从产品出发穿透到它的原料、工序与设备。
        <strong>检索给你一段话，图谱给你一条路径</strong>——
        所以它能回答「成本上涨经过了哪些环节」，而不只是「哪段文字提到了它」。
      </p>

      <div class="card">
        ${loading ? html`<${Loading} />`
          : error ? html`<${ErrorBox} msg=${error} />`
          : data && !data.ok ? html`<div class="msg err">${data.error}</div>`
          : html`
            <${GraphChart} key=${viewKey} data=${data}
                           selected=${sel} onSelect=${setSel} />
            <${GraphLegend} typeZh=${(data && data.type_zh) || {}} />

            ${/* 选中后的操作条 —— "查看"与"以它为中心"分成两步，
                 不会因为点错一下就把整个图重新布局。 */ ""}
            <div class="gbar">
              ${selNode ? html`
                <span class="gbar-sel">
                  <i style=${{ background: GRAPH_COLORS[selNode.type] || "#78716C" }}></i>
                  已选：<b>${selNode.label}</b>
                  <span class="gbar-type">${(data.type_zh || {})[selNode.type] || selNode.type}</span>
                </span>
                ${sel !== focus ? html`
                  <button class="chip" onClick=${reCenter}>以它为中心展开</button>` : null}
                <button class="linkbtn" onClick=${() => setSel(null)}>取消选择</button>`
              : html`
                <span class="gbar-hint">
                  点击节点查看，拖动可平移；当前 ${data.stats.nodes} 个节点、
                  ${data.stats.edges} 条关系
                </span>`}
            </div>`}
      </div>
    </section>`;
}


/* =============================================================================
 * 首页：对话 → 上传 → 分析
 *
 * 顺序是刻意的：**先问，再补资料，最后看分析**。
 * 用户的心流是「我想知道 X → 资料不够我补 → 数字都在这里」。
 * ========================================================================== */
function LegacyHome({ me, onNeedLogin, onAuthed }) {
  const [month, setMonth] = useState("2026-05");
  const [wsKey, setWsKey] = useState(0);
  const topRef = useRef(null);

  return html`
    <div ref=${topRef}></div>
    <div class="msg">此页用于个人文档检索与药企共享样例。个人成本CSV的核算、图表、问答和报告请进入<a href="#costing">通用成本分析</a>，选择同一份数据。</div>
    <${HeroAsk} me=${me} onNeedLogin=${onNeedLogin} onAuthed=${onAuthed} />
    <${Reveal}><${UploadZone} me=${me} onNeedLogin=${onNeedLogin}
                   onChanged=${() => setWsKey(k => k + 1)} /><//>
    <${Reveal}><${WorkspaceList} me=${me} refreshKey=${wsKey} /><//>
    <${Reveal}><${ExportBar} me=${me} /><//>
    <${Reveal}><${DecideCard} /><//>
    <${Reveal}><${HomeCharts} month=${month} setMonth=${setMonth} /><//>
    <${Reveal}><${HomeHeatmap} /><//>
    <${Reveal}><${HomeForecast} /><//>
    <${Reveal}><${HomeGraph} /><//>`;
}
/* =============================================================================
 * 外壳
 * ========================================================================== */
const costNumber = (value, digits = 2) => value == null ? "未知" : fmt(value, digits);
const COST_COLUMN_LABELS = [
  ["object_id", "对象编号（必填）"], ["object_name", "对象名称"],
  ["quantity", "产量 / 服务量"], ["quantity_unit", "产量单位"],
  ["cost_element", "成本类别（必填）"], ["amount", "金额（必填）"],
  ["currency", "币种（必填）"], ["behavior", "成本性态"],
  ["selling_price", "销售单价（可缺）"], ["period", "期间"]
];
const COST_SAMPLE_CSV = "对象编号,对象名称,产量,产量单位,币种,成本要素,金额,成本性态,销售单价,期间\nP1,体验产品A,10,件,CNY,材料,100,variable,20,2026-05\nP1,体验产品A,10,件,CNY,人工,50,fixed,20,2026-05\n";
const COST_SAMPLE_SCHEMA = {
  label: "我的成本数据", industry: "自定义", currency: "CNY", period: "2026-05", scenario: "actual",
  columns: { object_id: "对象编号", object_name: "对象名称", quantity: "产量", quantity_unit: "产量单位",
    currency: "币种", cost_element: "成本要素", amount: "金额", behavior: "成本性态", selling_price: "销售单价", period: "期间" },
  allocation_rules: []
};

function downloadCostFile(text, name, mime) {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const link = document.createElement("a");
  link.href = url; link.download = name;
  document.body.appendChild(link); link.click(); link.remove();
  URL.revokeObjectURL(url);
}

function CostSnapshotCharts({ result }) {
  const payload = result.result;
  const chart = result.charts || { objects: payload.objects, cost_structure: payload.totals.cost_structure || {} };
  const objects = (chart.objects || []).filter(row => row.total_cost != null);
  const categories = Object.entries(chart.cost_structure || {});
  const costOption = {
    tooltip: { trigger: "axis", formatter: rows => rows.map(row => `${_escHtml(row.axisValue)}：${costNumber(row.value)} ${_escHtml(payload.currency)}`).join("<br/>") },
    grid: { left: 80, right: 20, top: 15, bottom: 65, containLabel: true },
    xAxis: { type: "category", data: objects.map(row => row.name || row.object_id), axisLabel: { interval: 0, rotate: 15 } },
    yAxis: { type: "value", name: payload.currency },
    series: [{ name: "总成本", type: "bar", barMaxWidth: 48, itemStyle: { color: "#0f766e" }, data: objects.map(row => Number(row.total_cost)) }]
  };
  const structureOption = {
    tooltip: { trigger: "axis", formatter: rows => rows.map(row => `${_escHtml(row.axisValue)}：${costNumber(row.value)} ${_escHtml(payload.currency)}`).join("<br/>") },
    grid: { left: 80, right: 20, top: 15, bottom: 65, containLabel: true },
    xAxis: { type: "category", data: categories.map(([name]) => name), axisLabel: { interval: 0, rotate: 15 } },
    yAxis: { type: "value", name: payload.currency },
    series: [{ name: "成本构成", type: "bar", barMaxWidth: 48, itemStyle: { color: "#6366f1" }, data: categories.map(([, value]) => Number(value)) }]
  };
  return html`<div class="costing-chart-grid">
    <section class="costing-panel"><h3 class="costing-panel-title">对象总成本</h3><${EChart} option=${costOption} height=${300} deps=${[result]} title="当前分析对象成本" /></section>
    <section class="costing-panel"><h3 class="costing-panel-title">成本类别构成</h3><${EChart} option=${structureOption} height=${300} deps=${[result]} title="当前分析成本类别" /></section>
  </div>`;
}

function CostCategoryChecklist({ payload }) {
  if (payload.scope?.type !== "dataset") return null;
  const review = payload.recognition?.category_checklist;
  if (review?.version !== 1 || review.basis !== "readonly_final_components_declared_categories" ||
      !Array.isArray(review.groups)) return null;
  const groups = Array.isArray(payload.recognition?.object_rollups?.groups)
    ? payload.recognition.object_rollups.groups : [];
  const groupLabel = new Map(groups.map(group => [group.id, group.label || group.id]));
  const unreviewed = Array.isArray(review.unreviewed_group_ids) ? review.unreviewed_group_ids : [];
  const decimal = value => typeof value === "string" && /^-?\d+(\.\d+)?$/.test(value);
  const amount = value => decimal(value) ? value + " " + payload.currency : "未提供";
  const stateLabel = row => {
    switch (row.state) {
      case "observed": return decimal(row.amount) && /^-?0+(\.0+)?$/.test(row.amount)
        ? "有入账记录，净额为零" : "有入账记录";
      case "missing": return "缺少入账记录";
      case "declared_zero": return "申报为零（未核实）";
      case "not_applicable": return "申报不适用（未核实）";
      case "conflict": return row.declared_state === "declared_zero" ? "冲突：申报为零但有入账记录"
        : row.declared_state === "not_applicable" ? "冲突：申报不适用但有入账记录" : "申报与入账冲突";
      default: return "未知状态";
    }
  };
  const rowAmount = row => {
    if (row.state === "missing") return "未知（不按零处理）";
    if (row.state === "not_applicable") return "不计入";
    if (["observed", "declared_zero", "conflict"].includes(row.state)) return amount(row.amount);
    return "未提供";
  };
  return html`
    <section class="costing-panel">
      <h3 class="costing-panel-title">插件成本项目核对</h3>
      <p>按插件申报项目核对分摊后的入账记录。仅核对已配置的归集组和项目，不代表成本资料完整，也未独立核实申报。</p>
      ${review.groups.map(group => {
        const categories = Array.isArray(group.categories) ? group.categories : [];
        const remaining = Array.isArray(group.uncategorized_components) ? group.uncategorized_components : [];
        return html`<section key=${group.group_id}>
          <h4>${groupLabel.get(group.group_id) || group.group_id} · ${group.state === "needs_review"
            ? "待复核" : group.state === "satisfied_for_declared_categories_only" ? "本组所列项目已核对" : "核对状态未知"}</h4>
          <p>本组实际 ${amount(group.group_total)} · 已分类实际 ${amount(group.categorized_observed_amount)}
            · 未分类实际 ${amount(group.uncategorized_amount)}</p>
          <div class="costing-table-wrap"><table class="costing-table">
            <thead><tr><th>成本项目</th><th>核对状态</th><th>金额</th></tr></thead>
            <tbody>${categories.map(row => html`<tr key=${row.id}><td>${row.name || row.id}</td><td>${stateLabel(row)}</td><td>${rowAmount(row)}</td></tr>`)}</tbody>
          </table></div>
          ${remaining.length ? html`<details><summary>未分类成本（${remaining.length} 类）</summary>
            <ul>${remaining.map(row => html`<li key=${JSON.stringify([row.input_name, row.element_id])}>
              ${row.input_name} / ${row.element_id}：${amount(row.amount)}</li>`)}</ul>
          </details>` : null}
        </section>`;
      })}
      ${unreviewed.length ? html`<p>未核对的归集组：${unreviewed.map(id => groupLabel.get(id) || id).join("、")}</p>` : null}
      <p>未归入任何组的台账金额（不在核对范围）：${amount(review.ungrouped_ledger_amount)}</p>
    </section>`;
}

function CostRollupPanels({ payload }) {
  if (payload.scope?.type !== "dataset") return null;
  const rollup = payload.recognition?.object_rollups;
  if (!Array.isArray(rollup?.groups)) return null;
  const budget = payload.recognition?.object_budgets;
  const rows = Array.isArray(budget?.rows) ? budget.rows : [];
  const ungrouped = Array.isArray(rollup.ungrouped) ? rollup.ungrouped : [];
  // Preserve backend decimal strings rather than rounding them again in JS.
  const amount = value => typeof value === "string" && /^-?\d+(\.\d+)?$/.test(value)
    ? value + " " + payload.currency : "未提供";
  const pct = value => typeof value === "string" && /^-?\d+(\.\d+)?$/.test(value)
    ? value + "%" : "不计算";
  const summary = budget?.summary;
  return html`
    <section class="costing-panel">
      <h3 class="costing-panel-title">跨分支成本归集</h3>
      <p>按已配置的分摊后口径归集。不重复加总，不合并不同单位的用量。分组状态不代表原始资料完整。</p>
      <p>已归集 ${amount(rollup.grouped_total)} · 未归集 ${amount(rollup.ungrouped_total)} · 台账合计 ${amount(rollup.ledger_total)}</p>
      <div class="costing-table-wrap"><table class="costing-table"><thead><tr><th>归集组</th><th>总成本</th><th>组成（分支 / 成本对象）</th></tr></thead>
        <tbody>${rollup.groups.map(group => html`<tr key=${group.id}><td>${group.label || group.id}</td><td>${amount(group.total_cost)}</td>
          <td>${group.members.map(member => html`<div key=${JSON.stringify([member.input_name, member.object_id])}>${member.input_name} / ${member.object_id}：${amount(member.total_cost)}</div>`)}</td></tr>`)}</tbody></table></div>
      ${ungrouped.length ? html`<details><summary>未归集的对象（${ungrouped.length} 项）</summary>
        <ul class="costing-list">${ungrouped.map(row => html`<li key=${row.object_id}>${row.object_id}：${amount(row.total_cost)}（${row.reason === "pool_owner" ? "成本池保留余额" : "未指定归集组"}）</li>`)}</ul></details>` : null}
    </section>
    <section class="costing-panel"><h3 class="costing-panel-title">归集组预算比较</h3>
      ${!summary ? html`<p>未提供归集组预算，不按零处理。</p>` : html`
        <p>已配对 ${summary.matched_group_count} / ${summary.total_group_count} 组。预算为申报值，未独立核实；只比较已配对归集组。</p>
        <p>可比实际 ${amount(summary.comparable_actual)} · 可比预算 ${amount(summary.comparable_budget)} · 差额（实际减预算）${amount(summary.comparable_variance)} · 差异率 ${pct(summary.comparable_variance_pct)}</p>
        <p>缺预算的组实际成本 ${amount(summary.unmatched_group_actual)} · 未归集实际成本 ${amount(summary.ungrouped_ledger_actual)}</p>
        <div class="costing-table-wrap"><table class="costing-table"><thead><tr><th>归集组</th><th>实际成本</th><th>预算</th><th>差额</th><th>差异率</th></tr></thead>
          <tbody>${rows.map(row => html`<tr key=${row.group_id}><td>${row.group_label || row.group_id}</td><td>${amount(row.actual_cost)}</td>
            <td>${row.budget_status === "missing" ? "未提供，不按零" : row.budget_status === "evidenced_zero" ? amount(row.budget_cost) + "（有依据的零预算）" : amount(row.budget_cost)}</td>
            <td>${amount(row.variance)}</td><td>${pct(row.variance_pct)}</td></tr>`)}</tbody></table></div>`}
    </section>`;
}

function CostObjectPrivateSummary({ analysisId, revision, objects, busy }) {
  const [selected, setSelected] = useState(objects[0]?.object_id || "");
  const [state, setState] = useState(null);
  const generation = useRef(0);
  useEffect(() => () => { ++generation.current; }, []);
  const requestSummary = async () => {
    if (busy || !objects.some(row => row.object_id === selected)) return;
    const seq = ++generation.current;
    setState({ objectId: selected, loading: true });
    try {
      const query = new URLSearchParams({ revision, object_id: selected });
      const data = await api("/api/platform/costing-snapshots/" + encodeURIComponent(analysisId) + "/object-summary?" + query.toString());
      if (seq !== generation.current) return;
      if (data?._error || data?.schema_version !== "costing-object-only-summary/1" ||
          data.summary_scope !== "object_only_v1" || data.object?.object_id !== selected ||
          !/^[0-9a-f]{64}$/.test(data.revision || "")) throw new Error("unexpected summary receipt");
      const metric = value => typeof value === "string" && /^-?\d+(\.\d+)?$/.test(value) ? value : null;
      setState({ objectId: selected, summary: {
        name: typeof data.object.name === "string" ? data.object.name : selected,
        currency: /^[A-Z]{3}$/.test(data.currency || "") ? data.currency : "",
        quantity: metric(data.object.quantity),
        unit: typeof data.object.quantity_unit === "string" ? data.object.quantity_unit : "",
        cost: metric(data.object.total_cost), unitCost: metric(data.object.unit_cost),
        revenue: data.object.revenue_status === "complete" ? metric(data.object.revenue) : null,
        notApplicable: data.object.revenue_status === "not_applicable",
        margin: data.object.revenue_status === "complete" ? metric(data.object.operating_margin) : null,
        receipt: data.revision
      }});
    } catch {
      if (seq === generation.current) setState({ objectId: selected, error: true });
    }
  };
  const visible = state?.objectId === selected ? state : null;
  const s = visible?.summary;
  return html`<section class="costing-panel"><h3 class="costing-panel-title">仅此对象的私有摘要</h3>
    <p>基于当前保存版本，只显示所选对象的最终指标，不包含其他对象、原始凭证或分摊明细。不会生成分享链接。</p>
    <div class="costing-controls"><label>摘要对象<select value=${selected} onChange=${e => {
      ++generation.current; setState(null); setSelected(e.target.value);
    }}>${objects.map(row => html`<option key=${row.object_id} value=${row.object_id}>${row.name || row.object_id}</option>`)}</select></label>
    <button type="button" class="btn" disabled=${!!busy || !!visible?.loading || !selected} onClick=${requestSummary}>${visible?.loading ? "读取当前版本…" : "查看仅此对象摘要"}</button></div>
    ${visible?.error ? html`<p class="msg err" role="alert">暂时无法读取这份摘要。请重新打开保存版本后重试。</p>` : null}
    ${s ? html`<div role="status"><p><b>${s.name}</b> · ${s.currency}</p>
      <p>总成本 ${s.cost ?? "未提供"} · 用量 ${s.quantity ?? "未提供"} ${s.unit} · 单位成本 ${s.unitCost ?? "未提供"}</p>
      <p>收入 ${s.revenue ?? (s.notApplicable ? "不适用" : "未提供")} · 经营毛差 ${s.margin ?? (s.notApplicable ? "不适用" : "未提供")}</p>
      <p class="costing-version">摘要校验值 ${s.receipt}</p></div>` : null}
  </section>`;
}

function CostSourceLinks({ refs = [], onReference, busy }) {
  const [page, setPage] = useState(0);
  const keys = ["dataset_id", "source_uri", "field", "selector", "kind", "formula"];
  const unique = [...new Map(refs.map(ref => [JSON.stringify(keys.map(k => String(ref[k] ?? ""))), ref])).values()];
  if (!unique.length) return html`<span>未提供来源</span>`;
  const pages = Math.ceil(unique.length / 12), current = Math.min(page, pages - 1);
  return html`<details><summary>原始依据（${unique.length}）</summary>
    <ul class="costing-list">${unique.slice(current * 12, current * 12 + 12).map((ref, index) => html`<li key=${index}>
      <button class="linkbtn" disabled=${!!busy || !onReference} onClick=${() => onReference(ref)}>${ref.source_uri} · ${ref.selector}</button>
    </li>`)}</ul>
    ${pages > 1 ? html`<div class="costing-controls"><button class="chip" disabled=${current === 0} onClick=${() => setPage(current - 1)}>上一组依据</button><span>${current + 1} / ${pages}</span><button class="chip" disabled=${current + 1 >= pages} onClick=${() => setPage(current + 1)}>下一组依据</button></div>` : null}
  </details>`;
}

function CostAuxTable({ headers, rows }) {
  return html`<div class="costing-table-wrap"><table class="costing-table"><thead><tr>${headers.map((name, i) => html`<th key=${i}>${name}</th>`)}</tr></thead>
    <tbody>${rows.map((cells, i) => html`<tr key=${i}>${cells.map((value, j) => html`<td key=${j}>${value}</td>`)}</tr>`)}</tbody></table></div>`;
}

function CostOperationalInputs({ data, objectName, onReference, busy }) {
  if (!data) return null;
  if (data.status === "requires_book_scope") {
    return `<section class="costing-panel"><h3 class="costing-panel-title">运营计量与单位依据</h3><p>查看全部对象可核对整套运营计量。当前对象的原始依据仍可从来源证据查看。</p></section>`;
  }
  const status = { provided: "已提供", explicit_zero: "来源显式为零", missing: "缺资料", not_applicable: "不适用" };
  const rows = (data.object_bindings || []).map(row => [objectName(row.object_id), row.metric_id,
    costNumber(row.quantity, 6), row.unit || row.quantity_unit, status[row.status] || row.status,
    html`<${CostSourceLinks} refs=${row.source_refs || []} onReference=${onReference} busy=${busy} />`]);
  return html`<section class="costing-panel"><h3 class="costing-panel-title">运营计量与单位依据</h3>
    <p>由本修订原始事件、单位转换和期间规则复算，不把吨公里、托盘日、工时等混为一种业务量。${data.policy_source}</p>
    <${CostAuxTable} headers=${["对象", "计量规则", "数量", "单位", "资料状态", "原始依据"]} rows=${rows} />
    <p class="muted">${data.boundary || "单位核对仅涵盖上传原始行和声明契约，不代替真实系统与审批真实性核验。"}</p>
  </section>`;
}

function CostFinancialLedgers({ data, currency, onReference, busy }) {
  if (!data) return null;
  if (data.status === "requires_book_scope") return html`<section class="costing-panel"><h3>结算与资产辅助账</h3><p>辅助账需在全部对象范围查看，本对象报告不展示全账余额。</p></section>`;
  const refs = row => html`<${CostSourceLinks} refs=${row.source_refs || []} onReference=${onReference} busy=${busy} />`;
  const money = value => costNumber(value);
  const settlement = data.settlement, assets = data.assets, custody = data.custody, bridge = data.cost_bridge;
  const kinds = { receivable: "应收", payable: "应付", agency: "代收代付" };
  return html`<section class="costing-panel"><h3 class="costing-panel-title">结算与资产辅助账</h3>
    <p>核算口径：${data.policy_source}。以下结算现金与资产期末存量不再次计入成本。金额单位 ${currency}。</p>
    ${settlement ? html`<h4>应收应付与代收代付</h4><p>净开票、确认收入、收付款分别核对。代收款不计收入或成本，未批准的收入确认保持未知。</p>
      <${CostAuxTable} headers=${["账户", "类型", "期初", "本期记账", "贷项", "期末余额", "原始依据"]}
        rows=${(settlement.rows || []).map(row => [row.account_id, kinds[row.kind] || row.kind, ...["opening", "charges", "credits", "closing"].map(k => money(row[k])), refs(row)])} />
      <${CostAuxTable} headers=${["账户", "收款", "付款", "净开票", "声明确认收入", "资料状态"]}
        rows=${(settlement.rows || []).map(row => [row.account_id, ...["cash_in", "cash_out", "net_billed", "net_revenue"].map(k => money(row[k])), row.status])} />
      <p>应收余额 ${money(settlement.totals?.receivable_closing)}；应付余额 ${money(settlement.totals?.payable_closing)}；代收待付余额 ${money(settlement.totals?.agency_liability_closing)}。</p>
      ${(settlement.cost_reconciliation || []).map((row, i) => html`<p key=${i}>成本勾稽 ${row.cost_element}：辅助账 ${money(row.expected_recognition)}，成本引擎 ${money(row.engine_cost)}，${row.status}。</p>`)}
    ` : null}
    ${assets ? html`<h4>自有资产与跨期履约成本</h4><p>这是声明的资产辅助账，期末余额不与本期成本相加；购置、预付、资本化新增与当期耗用、折旧、摊销分开。</p>
      <${CostAuxTable} headers=${["资产", "期初账面", "本期新增", "本期确认成本", "期末账面", "资料状态", "原始依据"]}
        rows=${(assets.rows || []).map(row => [row.asset_id, ...["opening_value", "additions", "recognized_cost", "closing_value"].map(k => money(row[k])), row.status, refs(row)])} />
      <${CostAuxTable} headers=${["资产", "类型", "期初数量", "期末数量", "单位"]}
        rows=${(assets.rows || []).map(row => [row.asset_id, row.kind, costNumber(row.opening_quantity, 6), costNumber(row.closing_quantity, 6), row.unit])} />
      ${(assets.cost_reconciliation || []).map((row, i) => html`<p key=${i}>资产成本勾稽 ${row.cost_element}：辅助账 ${money(row.expected_recognition)}，成本引擎 ${money(row.engine_cost)}，${row.status}。</p>`)}
      <p class="muted">仅涵盖本次声明资产及政策，不是完整资产负债表，也不代替资本化判断。</p>
    ` : null}
    ${bridge ? html`<h4>成本与资产活动勾稽</h4><${CostAuxTable} headers=${["指标", "金额"]} rows=${[
      ["成本引擎归集额", money(bridge.engine_cost_total)], ["其中资产本期确认已含成本", money(bridge.asset_recognition_in_engine)],
      ["其他成本", money(bridge.engine_other_costs)], ["独立资产期末余额", money(bridge.separate_asset_closing)],
      ["批准本期履约资本化", money(bridge.approved_service_capitalization)], ["原履约资产转本期成本", money(bridge.prior_service_asset_release)]
    ]} />` : null}
    ${custody ? html`<h4>客户保管货物数量备查</h4><p>客户货物不计作本服务商自有存货、货值成本或资产。</p>
      <${CostAuxTable} headers=${["货物", "期初数量", "期末数量", "单位", "状态", "原始依据"]}
        rows=${(custody.rows || []).map(row => [row.goods_id, costNumber(row.opening_quantity, 6), costNumber(row.closing_quantity, 6), row.unit, row.status, refs(row)])} />
    ` : null}
  </section>`;
}

function CostBusinessViews({ view, currency, objectName }) {
  if (!view) return null;
  const lifecycle = view.lifecycle || [];
  const settlement = view.settlement;
  return html`<section class="costing-panel"><h3 class="costing-panel-title">成本、期间费用与资产桥接</h3>
    <p>核算依据：${view.policy_source}。以下金额来自本修订原始数据和已声明规则，结算款单独核对。</p>
    <div class="costing-summary">${[["本期发生额（含资产）", view.incurred_total], ["本期发生额留存的生产资产", view.asset_total], ["本期损益成本费用", view.pnl_cost], ["期间费用", view.period_expense], ["服务成本", view.service_cost]].map(([name, value]) => html`<div key=${name}><span>${name}</span><strong>${costNumber(value)}</strong><small>${currency}</small></div>`)}</div>
    ${(view.separate_inventory_assets || []).map(item => html`<p key=${item.input_name}>独立库存子账 ${item.input_name} 的期末余额 ${costNumber(item.closing_value)} ${currency}</p>`)}
    ${view.required_costs?.status === "incomplete" && html`<div role="status"><p>资料不完整。发生额和资产桥接只涵盖已提供资料。</p><ul>${view.required_costs.missing.map(row => html`<li key=${row.object_id + "/" + row.element_id}>${row.object_id} 缺少 ${row.element_id}</li>`)}</ul></div>`}
    ${lifecycle.length ? html`<div class="costing-table-wrap"><table class="costing-table"><thead><tr><th>对象</th><th>发生额</th><th>在制</th><th>完工库存</th><th>销售成本（退回后）</th><th>报废损失</th><th>合格完工量</th><th>合格单位成本</th></tr></thead><tbody>${lifecycle.map(row => html`<tr key=${row.object_id}><td>${objectName(row.object_id)}</td><td>${costNumber(row.incurred_cost)}</td><td>${costNumber(row.wip)}</td><td>${costNumber(row.finished_inventory)}</td><td>${costNumber(row.cost_of_sales)}</td><td>${costNumber(row.scrap_loss)}</td><td>${costNumber(row.completed_quantity)} ${row.unit}</td><td>${row.qualified_unit_cost == null ? "分母缺失或为零，不报单位成本" : costNumber(row.qualified_unit_cost, 4)}</td></tr>`)}</tbody></table></div>` : null}
    ${lifecycle.length ? html`<details><summary>数量与退回桥接</summary><div class="costing-table-wrap"><table class="costing-table"><thead><tr><th>对象</th><th>投入量</th><th>在制量</th><th>库存量</th><th>销售量</th><th>关联退回量</th><th>报废量</th></tr></thead><tbody>${lifecycle.map(row => html`<tr key=${row.object_id}><td>${objectName(row.object_id)}</td><td>${row.started_quantity}</td><td>${row.wip_quantity}</td><td>${row.finished_quantity}</td><td>${row.sales_quantity}</td><td>${row.returned_quantity}</td><td>${row.scrap_quantity}</td></tr>`)}</tbody></table></div></details>` : null}
    ${settlement ? html`<h4>营业额、净收入与实结回款</h4><p>商家优惠按声明口径抵减收入；平台承担并补偿的优惠单列；佣金、支付费与成本台账逐项核对，只计一次。</p><div class="costing-table-wrap"><table class="costing-table"><thead><tr><th>对象</th><th>营业额</th><th>商家优惠</th><th>平台优惠</th><th>退款</th><th>净收入</th><th>佣金</th><th>支付费</th><th>应结回款</th><th>实结回款</th><th>结算差额</th></tr></thead><tbody>${(settlement.rows || []).map(row => html`<tr key=${row.object_id}><td>${objectName(row.object_id)}</td>${["gross", "merchant_discount", "platform_discount", "refund", "net_revenue", "commission", "payment_fee", "expected_receipt", "receipt", "receipt_difference"].map(key => html`<td key=${key}>${costNumber(row[key])}</td>`)}</tr>`)}</tbody></table></div>` : null}
    ${(view.notices || []).length ? html`<ul class="costing-list">${view.notices.map((notice, index) => html`<li key=${index}>${notice.message}</li>`)}</ul>` : null}
  </section>`;
}

function revealCostRegion(selector) {
  requestAnimationFrame(() => requestAnimationFrame(() => {
    document.querySelector(selector)?.scrollIntoView({ block: "start" });
  }));
}

function CostingPlatform({ me, onNeedLogin, workspaceMode = "analysis" }) {
  const [industryList, setIndustryList] = useState([]);
  const [industryCode, setIndustryCode] = useState("");
  const [industryConfig, setIndustryConfig] = useState(null);
  const [industryPeriod, setIndustryPeriod] = useState("");
  const [industryRows, setIndustryRows] = useState("");
  const [industryFile, setIndustryFile] = useState(null);
  const [sharedAmount, setSharedAmount] = useState("");
  const [poolAmounts, setPoolAmounts] = useState({});
  const [datasets, setDatasets] = useState([]);
  const [datasetId, setDatasetId] = useState("");
  const [period, setPeriod] = useState("");
  const [scenario, setScenario] = useState("actual");
  const [costObject, setCostObject] = useState("");
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [diagnostics, setDiagnostics] = useState([]);
  const [savedReports, setSavedReports] = useState([]);
  const [file, setFile] = useState(null);
  const [sourceFiles, setSourceFiles] = useState([]);
  const [label, setLabel] = useState("");
  const [schemaText, setSchemaText] = useState(JSON.stringify({ ...COST_SAMPLE_SCHEMA, label: "", period: "" }, null, 2));
  const [mappingName, setMappingName] = useState("");
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState(null);
  const [evidence, setEvidence] = useState(null);
  const [mappingLoading, setMappingLoading] = useState(false);
  const mappingRequest = useRef(0);
  const reportRequest = useRef(0);
  const datasetCache = useRef(null);
  const importPanel = useRef(null);
  const activeCosting = useRef(false);
  const [evidencePage, setEvidencePage] = useState(0);
  const [reportOpened, setReportOpened] = useState(false);
  const [auditOpened, setAuditOpened] = useState(false);


  const selectDataset = (id, list = datasets) => {
    const item = list.find(entry => entry.dataset_id === id) || {};
    setDatasetId(id); setPeriod((item.periods || [])[0] || "");
    setScenario((item.scenarios || ["actual"])[0]); setCostObject("");
    setResult(null); setAnswer(null); setEvidence(null); setNotice("");
  };
  const refreshDatasets = async (preferred = "") => {
    const data = await api("/api/platform/costing-industries");
    if (data._error) { setError(data._error); return []; }
    const list = data.datasets || [];
    datasetCache.current = list;
    setDatasets(list); setDiagnostics(data.diagnostics || []);
    const parameters = new URLSearchParams(location.hash.split("?")[1] || "");
    const explicitSelection = parameters.has("analysis") || parameters.has("dataset");
    if (!preferred && !datasetId && !explicitSelection && reportRequest.current === 0) {
      const personal = list.find(item => item.dataset_id.startsWith("csv-"));
      if (personal) selectDataset(personal.dataset_id, list);
    }
    return list;
  };
  const refreshReports = async () => {
    if (!me) { setSavedReports([]); return; }
    const data = await api("/api/platform/costing-workspace");
    if (!data._error) setSavedReports((data.reports || []).filter(report => report.analysis_id));
  };
  useEffect(() => {
    setResult(null); setAnswer(null); setEvidence(null);
    setSavedReports([]); setDatasets([]); datasetCache.current = null;
    let active = true;
    activeCosting.current = true;
    let lastLink = "";
    const followReportLink = () => {
      if (!active) return;
      const [tab, query] = location.hash.slice(1).split("?", 2);
      if (tab !== "costing") return;
      const params = new URLSearchParams(query || "");
      const id = params.get("analysis");
      const revision = params.get("revision") || "";
      if (!me) {
        if (id) onNeedLogin();
        return;
      }
      const linkedDataset = params.get("dataset");
      if (params.get("import") === "1" && importPanel.current) {
        importPanel.current.open = true;
        importPanel.current.scrollIntoView({ block: "start" });
      }
      if (linkedDataset && !Array.isArray(datasetCache.current)) return;
      const link = id ? JSON.stringify([id, revision]) : linkedDataset ? JSON.stringify(["dataset", linkedDataset]) : "";
      if (link === lastLink) return;
      lastLink = link;
      if (id) openReport(id, revision);
      else if (linkedDataset) {
        if (datasetCache.current.some(item => item.dataset_id === linkedDataset)) {
          selectDataset(linkedDataset, datasetCache.current);
        } else setError("当前账户没有这个数据集，或数据集已不可用。");
      } else {
        ++reportRequest.current;
        setResult(null); setAnswer(null); setEvidence(null);
        setBusy(""); setError(""); setNotice("");
      }
    };
    refreshDatasets().then(followReportLink);
    window.addEventListener("hashchange", followReportLink);
    refreshReports();
    return () => {
      active = false;
      activeCosting.current = false;
      ++reportRequest.current;
      window.removeEventListener("hashchange", followReportLink);
    };
  }, [me]);

  const acceptResult = (data) => {
    if (!activeCosting.current) return;
    if (workspaceMode !== "import" && me && location.hash.split("?")[0] === "#costing" &&
        /^[0-9a-f]{32}$/.test(data.analysis_id || "") && /^[0-9a-f]{64}$/.test(data.revision || "")) {
      window.history.replaceState(null, "", location.pathname + location.search +
        "#costing?analysis=" + encodeURIComponent(data.analysis_id) +
        "&revision=" + encodeURIComponent(data.revision));
    }
    setResult(data); setAnswer(null); setEvidence(null); setEvidencePage(0);
    setQuestion(""); setReportOpened(false); setAuditOpened(false);
    const scope = data.context || data.result;
    setDatasetId(scope.dataset_id); setPeriod(scope.period); setScenario(scope.scenario); setCostObject(scope.cost_object || "");
    revealCostRegion(".costing-result-head");
  };
  const loadIndustryCatalog = async () => {
    const data = await api("/api/platform/industry-catalog");
    if (data._error) setError(data._error);
    else setIndustryList((data.industries || []).filter(x => x.level === 3));
  };
  const pickIndustry = async (code) => {
    setIndustryCode(code); setIndustryConfig(null); setPoolAmounts({}); setSharedAmount("");
    setResult(null); setAnswer(null); setEvidence(null); setNotice("");
    if (!code) return;
    const data = await api("/api/platform/industry-contract/" + encodeURIComponent(code));
    if (data._error) setError(data._error); else setIndustryConfig(data);
  };
  const submitIndustry = async () => {
    if (!me) { onNeedLogin(); return; }
    if (!industryConfig || industryConfig.industry.code !== industryCode || busy) return;
    try {
      setBusy("industry"); setError("");
      const payload = {version: industryConfig.version, industry_code:industryCode, period:industryPeriod};
      if (sharedAmount !== "") payload.shared_amount = sharedAmount;
      const pools = Object.fromEntries(Object.entries(poolAmounts).filter(([,v]) => v !== ""));
      let data;
      if (industryFile) {
        const response = await fetch("/api/platform/industry-analysis?" + new URLSearchParams({...payload,shared_pools:JSON.stringify(pools)}),
          {method:"POST", credentials:"same-origin",
           headers:{"Content-Type":"text/csv", "X-CSRF-Token":getCookie("lw_csrf")},body:industryFile});
        data = await response.json();
        if (!response.ok) throw new Error(data.detail || response.statusText);
      } else {
        payload.rows = JSON.parse(industryRows); payload.shared_pools = pools;
        data = await api("/api/platform/industry-analysis",{method:"POST",body:payload});
      }
      if (data._error) setError(data._error); else acceptResult(data);
    } catch (e) { setError(e.message || String(e)); }
    finally { setBusy(""); }
  };
  const runAnalysis = async () => {
    if (!datasetId || !period || busy) return;
    setBusy("analysis"); setError(""); setNotice(""); setResult(null); setAnswer(null); setEvidence(null);
    const query = new URLSearchParams({ dataset_id: datasetId, period, scenario, cost_object: costObject });
    const data = await api("/api/platform/costing-analysis?" + query.toString());
    if (data._error) setError(data._error); else acceptResult(data);
    setBusy("");
  };
  const loadMapping = async (event) => {
    const source = event.target.files && event.target.files[0];
    event.target.value = "";
    if (!source) return;
    const request = ++mappingRequest.current;
    setMappingLoading(true);
    try {
      if (source.size > 256 * 1024) throw new Error("规则文件超过256KB");
      const value = JSON.parse(await source.text());
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("规则文件必须是JSON对象");
      if (request === mappingRequest.current) {setSchemaText(JSON.stringify(value)); setMappingName(source.name); setError(""); setResult(null); setAnswer(null); setEvidence(null); setNotice("");}
    } catch (e) {
      if (request === mappingRequest.current) setError("未替换原规则：" + (e.message || String(e)));
    } finally {
      if (request === mappingRequest.current) setMappingLoading(false);
    }
  };
  const importCsv = async () => {
    if (!me) { onNeedLogin(); return; }
    if (mappingLoading) return;
    if (!file) { setError("请先选择CSV文件"); return; }
    let schema;
    try { schema = JSON.parse(schemaText); }
    catch (e) { setError("字段映射JSON格式错误：" + e.message); return; }
    const bundle = schema.raw_composite && schema.raw_composite.version === 4;
    if (!bundle && sourceFiles.length > 1) { setError("多文件须选择配套的多源规则JSON"); return; }
    const form = new FormData();
    if (bundle) sourceFiles.forEach(source => form.append("files", source));
    else form.append("file", file);
    form.append("schema_json", JSON.stringify(schema));
    form.append("label", label || schema.label || file.name);
    const request = ++reportRequest.current;
    setBusy("import"); setError(""); setNotice("");
    try {
      const response = await fetch(bundle ? "/api/platform/costing-bundle-import" : "/api/platform/costing-import", { method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": getCookie("lw_csrf") }, body: form });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || response.statusText));
      if (request !== reportRequest.current) return;
      await refreshDatasets(data.dataset.dataset_id);
      if (request !== reportRequest.current) return;
      acceptResult(data.analysis);
      if (workspaceMode === "import") {
        location.hash = "costing?analysis=" + encodeURIComponent(data.analysis.analysis_id) +
          "&revision=" + encodeURIComponent(data.analysis.revision);
      }
      setNotice("导入已完成。下方图表、问答与导出均对应本次导入的同一分析版本。");
    } catch (e) { setError(e.message || String(e)); }
    setBusy("");
  };
  const saveReport = async () => {
    if (!me || !result.analysis_id) { onNeedLogin(); return; }
    setBusy("save"); setError("");
    const data = await api(`/api/platform/costing-snapshots/${result.analysis_id}/report?revision=${encodeURIComponent(result.revision)}`, { method: "POST" });
    if (data._error) setError(data._error);
    else { setNotice("报告已保存，版本与当前计算一致，可从「已保存报告」重新打开。"); await refreshReports(); }
    setBusy("");
  };
  const exportReport = async (format) => {
    if (!me || !result.analysis_id) { onNeedLogin(); return; }
    setBusy("export"); setError("");
    try {
      const response = await fetch(`/api/platform/costing-snapshots/${result.analysis_id}/export?format=${format}&revision=${encodeURIComponent(result.revision)}`, { credentials: "same-origin" });
      if (!response.ok) { const failure = await response.json(); throw new Error(failure.detail || failure.message || response.statusText); }
      const blob = await response.blob(), url = URL.createObjectURL(blob);
      const link = document.createElement("a"); link.href = url;
      link.download = `成本报告_${result.result.period}_${result.analysis_id.slice(0, 8)}.${format}`;
      document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
      setNotice(`${format.toUpperCase()}已导出：与当前分析版本一致，包含来源核对附录。`);
    } catch (e) { setError("导出失败：" + (e.message || String(e))); }
    setBusy("");
  };
  const openReport = async (id, expectedRevision = "") => {
    if (!id) return;
    const request = ++reportRequest.current;
    setBusy("open"); setError(""); setNotice("");
    setResult(null); setAnswer(null); setEvidence(null);
    const saved = savedReports.find(report => report.analysis_id === id);
    const pin = expectedRevision || (saved && saved.metadata && saved.metadata.revision);
    const data = await api(`/api/platform/costing-snapshots/${encodeURIComponent(id)}${pin ? "?revision=" + encodeURIComponent(pin) : ""}`);
    if (request !== reportRequest.current) return;
    if (data._error) setError(data._error); else acceptResult(data);
    setBusy("");
  };
  const inspectEvidence = async (id) => {
    if (!result.analysis_id) { onNeedLogin(); return; }
    const request = reportRequest.current;
    setError(""); setBusy("source");
    const data = await api(`/api/platform/costing-snapshots/${result.analysis_id}/evidence/${encodeURIComponent(id)}?revision=${encodeURIComponent(result.revision)}`);
    if (request !== reportRequest.current) return;
    if (data._error) setError(data._error);
    else { setEvidence(data); revealCostRegion(".costing-evidence"); }
    setBusy("");
  };
  const askCurrent = async (text = question) => {
    if (!me || !result.analysis_id) { onNeedLogin(); return; }
    if (!text.trim()) return;
    setBusy("question"); setError(""); setQuestion(text);
    const data = await api(`/api/platform/costing-snapshots/${result.analysis_id}/question?revision=${encodeURIComponent(result.revision)}`, { method: "POST", body: { question: text } });
    if (data._error) setError(data._error); else setAnswer(data);
    setBusy("");
  };
  let mapping = {};
  try { const value = JSON.parse(schemaText); if (value && typeof value === "object") mapping = value; } catch (e) {}
  const changeMapping = (key, value, column = false) => {
    const next = { ...mapping };
    if (column) next.columns = { ...(mapping.columns || {}), [key]: value };
    else next[key] = value;
    setSchemaText(JSON.stringify(next, null, 2));
  };
  const selectedDataset = datasets.find(item => item.dataset_id === datasetId) || {};
  const periods = selectedDataset.periods || [];
  const scenarios = selectedDataset.scenarios || ["actual", "budget", "standard"];
  const payload = result && result.result;
  const objects = payload ? Object.values(payload.objects || {}) : [];
  const objectName = id => (objects.find(object => object.object_id === id) || {}).name || id;
  const quality = payload && payload.quality || [];
  const allocations = payload && payload.allocations || [];
  const totals = payload && payload.totals || {};
  const scope = result && (result.context || payload) || {};
  const sources = result && result.evidence || [];
  const inspectReference = ref => {
    const fields = ["dataset_id", "source_uri", "field", "selector", "kind", "formula"];
    const index = sources.findIndex(item => fields.every(k => String(item[k] ?? "") === String(ref[k] ?? "")));
    if (index < 0) { setError("当前修订没有此来源，请重新打开同一报告后核对。"); return; }
    setEvidencePage(Math.floor(index / 40));
    inspectEvidence(sources[index].evidence_id);
  };
  const scenarioNames = { actual: "实际", budget: "预算", standard: "标准" };
  const sourceStatus = { captured: "原始记录已核对", captured_mapping: "映射已保存", synthetic: "可重复模拟数据", missing: "原文件缺失", selector_not_found: "未匹配到原始行", reference_only: "待补原始材料", page_not_specified: "待补页码" };
  return html`<section class=${"costing-page" + (workspaceMode === "import" ? " wb-import-view" : "")}>
    ${workspaceMode !== "import" ? html`<header class="costing-intro wb-page-heading"><div><h2>成本核算</h2><p>选择数据集与核算范围，查看同一修订的结果、分摊和原始依据。</p></div><a class="btn" href="#data">管理数据集</a></header>
    <div class="wb-stepper" aria-label="核算流程"><a href="#data" class=${datasetId ? "is-done" : "is-active"}>选择或接入数据</a><span class=${result ? "is-done" : "is-active"}>运行成本核算</span><span class=${result ? "is-active" : ""}>保存报告与追溯</span></div> 
    <details class="costing-panel" onToggle=${e => { if(e.currentTarget.open && !industryList.length) loadIndustryCatalog(); }}>
      <summary>行业模板与高级配置</summary>
      <p class="muted">此入口按行业分类收集申报字段，成本口径和适用范围以具体配置及已提供资料为准。</p>
      <label>行业 <select value=${industryCode} onChange=${e=>pickIndustry(e.target.value)}>
        <option value="">选择行业小类</option>
        ${industryList.map(x=>html`<option key=${x.code} value=${x.code}>${x.code} ${x.name}</option>`)}
      </select></label>
      ${industryConfig ? html`<div>
        <p>${industryConfig.coverage==="configured-template" ? "已配置行业模板" : "通用起步模板，不代表行业完整核验"}</p>
        <p>成本模块：${industryConfig.modules.map(x=>x.id+" "+x.label).join("；")}</p>
        <p>输入字段：${industryConfig.fields.join("、")}</p>
        ${(industryConfig.custom_fields || []).map(f=>html`<p key=${f.id}>${f.id}：${f.label}（${f.type}${f.required ? "，必填" : "，可选"}）</p>`)}
        <label>期间 <input type="month" value=${industryPeriod} onInput=${e=>setIndustryPeriod(e.target.value)} /></label>
        ${industryConfig.allocation ? html`<label>共享成本 <input type="text" value=${sharedAmount} onInput=${e=>setSharedAmount(e.target.value)} /></label>` : null}
        ${Object.entries(industryConfig.pools || {}).map(([id,spec])=>html`<label key=${id}>${id}（按 ${spec.driver} 分摊） <input type="text" value=${poolAmounts[id] || ""} onInput=${e=>setPoolAmounts(old=>({...old,[id]:e.target.value}))} /></label>`)}
        <label>成本数据 CSV <input type="file" accept=".csv,text/csv" onChange=${e=>setIndustryFile(e.target.files[0] || null)} /></label>
        <label>不上传时使用成本数据 JSON <textarea value=${industryRows} onInput=${e=>setIndustryRows(e.target.value)} /></label>
        <button type="button" class="chip" disabled=${!!busy} onClick=${submitIndustry}>分析</button>
      </div>` : null}
    </details>
    <section class="costing-panel"><h3 class="costing-panel-title">选择核算范围</h3>
      <div class="costing-controls">
        <label>数据集<select value=${datasetId} disabled=${!!busy} onChange=${e => selectDataset(e.target.value)}><option value="">请选择数据集</option>${datasets.map(item => html`<option value=${item.dataset_id} key=${item.dataset_id}>${item.label} · ${item.dataset_id.startsWith("csv-") ? "我的数据" : "共享样例"}</option>`)}</select></label>
        <label>数据中可用期间<select value=${period} disabled=${!!busy} onChange=${e => { setPeriod(e.target.value); setResult(null); }}>${!periods.length ? html`<option value="">暂无可用期间</option>` : periods.map(item => html`<option key=${item} value=${item}>${item}</option>`)}</select></label>
        <label>情景<select value=${scenario} disabled=${!!busy} onChange=${e => { setScenario(e.target.value); setResult(null); }}>${scenarios.map(item => html`<option key=${item} value=${item}>${scenarioNames[item] || item}</option>`)}</select></label>
        <label>成本对象<select value=${costObject} disabled=${!!busy} onChange=${e => { setCostObject(e.target.value); setResult(null); }}><option value="">全部对象</option>${(selectedDataset.cost_objects || []).map(item => html`<option key=${item.object_id || item.id} value=${item.object_id || item.id}>${item.name}</option>`)}</select></label>
        <button class="btn primary" disabled=${!!busy || !datasetId || !period} onClick=${runAnalysis}>${busy === "analysis" ? "核算中…" : "运行成本分析"}</button>
      </div>
      <p class="muted costing-gap">${selectedDataset.availability_reason || selectedDataset.description || "请选择数据集"}</p>
      ${savedReports.length ? html`<label class="costing-saved">已保存报告<select value="" disabled=${!!busy} onChange=${e => openReport(e.target.value)}><option value="">选择并打开原分析版本</option>${savedReports.map(report => html`<option key=${report.analysis_id} value=${report.analysis_id}>${report.metadata.dataset_label || report.metadata.dataset_id} · ${report.period} · ${report.analysis_id.slice(0, 8)}</option>`)}</select></label>` : null}
      ${diagnostics.map(item => html`<p class="msg err" key=${item.dataset_id}>${item.dataset_id}：${item.message}。该数据集暂不可用，其他数据集不受影响。</p>`)}
    </section>
    ` : null}
    <details class="costing-panel wb-stage" ref=${importPanel} open=${workspaceMode === "import"}><summary class="costing-panel-title">${workspaceMode === "import" ? "导入原始成本文件" : "导入自己的成本数据"}</summary>
      <p class="muted">选择原始CSV（多源数据须一次选择全部配套CSV），再选择配套规则JSON，最后点「校验并导入数据集」。原文件、来源行、映射和规则版本会随同一分析保存。</p>
      <p class="muted">CSV每行记录一笔成本，同一对象的产量和名称须一致。收入缺失时保持未知；variable表示变动成本，fixed表示固定成本，semi_variable表示尚未拆分的混合成本。</p>
      <div class="costing-controls costing-gap">
        <button class="btn" onClick=${() => downloadCostFile("\ufeff" + COST_SAMPLE_CSV, "成本导入示例.csv", "text/csv;charset=utf-8")}>下载CSV示例</button>
        <button class="btn" onClick=${() => {setSchemaText(JSON.stringify(COST_SAMPLE_SCHEMA, null, 2)); setMappingName("");}}>使用示例字段映射</button>
        <label>数据集名称<input value=${label} onInput=${e => setLabel(e.target.value)} placeholder="例如：11月运营成本" /></label>
        <label>原始CSV文件（可多选）<input type="file" multiple accept=".csv,text/csv" onChange=${e => {const selected = Array.from(e.target.files || []); setSourceFiles(selected); setFile(selected[0] || null); setResult(null); setAnswer(null); setEvidence(null); setNotice("");}} /></label>
        ${sourceFiles.length ? html`<p role="status">已选择 ${sourceFiles.length} 份：${sourceFiles.map(source => source.name).join("、")}</p>` : null}
        <label>字段映射 JSON（可选：选配套规则文件）<input type="file" accept=".json,application/json" onChange=${loadMapping} /></label>
        ${mappingName ? html`<p role="status">正在使用规则：${mappingName}</p>` : null}
        <label>本次导入期间<input type="month" value=${mapping.period || ""} onChange=${e => changeMapping("period", e.target.value)} /></label>
        <label>本次导入情景<select value=${mapping.scenario || "actual"} onChange=${e => changeMapping("scenario", e.target.value)}>${["actual", "budget", "standard"].map(value => html`<option key=${value} value=${value}>${scenarioNames[value]}</option>`)}</select></label>
      </div>
      ${mapping.raw_composite ? html`<p class="msg">已载入配套多源规则，字段、单位和分摊按该规则校验。请一次选择同一套配套CSV。</p>` : html`      <div class="costing-mapping">${COST_COLUMN_LABELS.map(([key, name]) => html`<label key=${key}>${name}<input value=${mapping.columns && mapping.columns[key] || ""} onInput=${e => changeMapping(key, e.target.value, true)} placeholder="填写CSV表头" /></label>`)}</div>
      <p class="muted">示例两行成本合计150元，产量10件，销售单价20元；核算应得到单位成本15元、收入200元。映射填写的是表头名称，不是某一行的数值。</p>
      `}
      <details class="costing-gap"><summary>高级映射与分摊规则</summary><label class="costing-schema-label">字段映射JSON<textarea rows="14" value=${schemaText} onInput=${e => setSchemaText(e.target.value)} spellCheck="false"></textarea></label></details>
      <button class="btn primary costing-gap" disabled=${!!busy || !file || mappingLoading} onClick=${importCsv}>${busy === "import" ? "校验、试算并导入…" : "校验并导入数据集"}</button>
      ${!me ? html`<p class="muted costing-gap">个人导入、保存、问答与导出需要登录。<button class="linkbtn" onClick=${onNeedLogin}>去登录</button></p>` : null}
    </details>
    ${error ? html`<div class="msg err" role="alert">${error}</div>` : null}
    ${notice ? html`<div class="msg" role="status">${notice}</div>` : null}
    ${workspaceMode !== "import" && payload ? html`
      <div class="costing-result-head"><div><h3>${scope.dataset_label || scope.dataset_id}</h3><p>${typeof scope.industry === "string" ? scope.industry : scope.industry && (scope.industry.name || scope.industry.code)} · ${scope.period} · ${scenarioNames[scope.scenario] || scope.scenario} · ${payload.currency} · ${scope.cost_object ? objectName(scope.cost_object) : "全部对象"}</p></div><span class="costing-chip">${result.analysis_id ? "已固定分析版本" : "共享样例预览"}</span></div>
      ${result.revision ? html`<p class="costing-version">分析编号 ${result.analysis_id} · 版本 ${result.revision}</p>` : html`<p class="muted">登录后重新运行，可保存版本、核对原始记录并导出。</p>`}
      ${scope.configuration_sha256 ? html`<p class="costing-version">规则版本 ${scope.plugin_version} · 配置 ${scope.configuration_sha256}</p>` : null}
      ${payload.coverage ? html`<p class=${payload.coverage.status==="complete_for_declared_scope" ? "msg" : "msg err"} role="status">
        台账平衡：${payload.ledger_reconciled ? "通过" : "未通过"}；资料覆盖：${payload.coverage.status==="complete_for_declared_scope" ? "声明范围已核对" : "不完整（当前金额不能代表完整成本）"}
      </p>` : null}
      <${CostBusinessViews} key=${result.analysis_id + ":" + result.revision} view=${payload.business_views} currency=${payload.currency} objectName=${objectName} />
      <${CostOperationalInputs} key=${"operations:" + result.analysis_id + ":" + result.revision} data=${payload.recognition?.operational_inputs} objectName=${objectName} onReference=${result.analysis_id ? inspectReference : null} busy=${busy} />
      <${CostFinancialLedgers} key=${"financial:" + result.analysis_id + ":" + result.revision} data=${payload.business_views?.financial_ledgers} currency=${payload.currency} onReference=${result.analysis_id ? inspectReference : null} busy=${busy} />
      ${!payload.business_views ? html`<div class="costing-summary">${[["总成本", totals.total_cost], ["总收入", totals.total_revenue], ["经营毛差", totals.total_operating_margin], ["未分配成本", totals.unallocated_cost]].map(([name, value]) => html`<div key=${name}><span>${name}</span><strong>${costNumber(value)}</strong><small>${payload.currency}</small></div>`)}</div>` : null}
      ${totals.total_revenue == null && !(payload.business_views && payload.business_views.settlement) ? html`<div class="msg">收入资料不完整，当前不能判断整体盈利或亏损。未知值没有按零参与结论。</div>` : null}
      <${CostSnapshotCharts} result=${result} />
      ${(me && /^[0-9a-f]{32}$/.test(result.analysis_id || "") && /^[0-9a-f]{64}$/.test(result.revision || "") && objects.length)
        ? html`<${CostObjectPrivateSummary} key=${JSON.stringify([result.analysis_id, result.revision, me.external_id || me.id || null])}
            analysisId=${result.analysis_id} revision=${result.revision} objects=${objects} busy=${busy} />` : null}
      <${CostRollupPanels} payload=${payload} />
      <${CostCategoryChecklist} payload=${payload} />
      <div class="costing-object-grid">${objects.map(obj => html`<article class="costing-object" key=${obj.object_id}><h4>${obj.name || obj.object_id}</h4><p class="muted">${obj.kind === "cost_center" ? "成本中心" : "核算对象"} · ${obj.object_id}</p><div class="costing-total">${costNumber(obj.total_cost)}</div><div class="muted">${payload.currency} · 总成本</div><dl>
        <div><dt>产量 / 服务量</dt><dd>${costNumber(obj.quantity)} ${obj.quantity_unit || ""}</dd></div>
        ${obj.quantity != null && obj.kind === "service" && !payload.recognition?.operational_inputs ? `<div><dt>服务量依据</dt><dd>配置声明，尚无运营原行核验</dd></div>` : null}
        <div><dt>直接入账</dt><dd>${costNumber(obj.direct_cost)}</dd></div><div><dt>分摊转入 / 转出</dt><dd>${costNumber(obj.allocated_in)} / ${costNumber(obj.allocated_out)}</dd></div>
        <div><dt>成本性质：直接 / 间接</dt><dd>${costNumber(obj.cost_type_structure?.direct)} / ${costNumber(obj.cost_type_structure?.indirect)}</dd></div>
        <div><dt>单位成本</dt><dd>${costNumber(obj.unit_cost, 4)} / ${obj.quantity_unit || "单位"}</dd></div>
        <div><dt>固定 / 变动成本</dt><dd>${costNumber(obj.fixed_cost)} / ${costNumber(obj.variable_cost)}</dd></div>
        <div><dt>未拆分混合成本</dt><dd>${costNumber(obj.mixed_cost)}</dd></div>
        <div><dt>收入</dt><dd>${obj.revenue_status === "not_applicable" ? "不适用" : costNumber(obj.revenue)}</dd></div>
        <div><dt>经营毛差</dt><dd>${obj.kind === "cost_center" ? "不适用" : costNumber(obj.operating_margin)}</dd></div>
        <div><dt>成本性态资料</dt><dd>${obj.cost_behavior_status === "complete" ? "完整" : "不完整，保留未知"}</dd></div>
      </dl></article>`)}</div>
      ${payload.coverage ? html`<section class="costing-panel">
        <h3 class="costing-panel-title">成本完整性核对</h3>
        <p>${payload.coverage.status==="complete_for_declared_scope" ? "插件声明的本月范围已核对" : "资料不完整，不应将本次金额视为完整成本"}</p>
        <p>全来源台账 ${costNumber(payload.coverage.ledger_total)} · 独立控制额 ${costNumber(payload.coverage.controls_total)} · 未映射 ${costNumber(payload.coverage.unmapped_total)} · 未分摊 ${costNumber(payload.coverage.unallocated_total)}</p>
        <details><summary>按费用和责任范围查看（${payload.coverage.cells.length} 项）</summary>
          <div class="costing-table-wrap"><table class="costing-table"><thead><tr><th>责任范围</th><th>费用</th><th>状态</th><th>金额</th><th>依据</th></tr></thead>
          <tbody>${payload.coverage.cells.map(x => html`<tr key=${x.scope + ":" + x.category}><td>${x.scope}</td><td>${x.label}</td><td>${({observed:"已入账",verified_zero:"经证实为零",not_applicable:"不适用",missing:"缺失",conflict:"冲突"})[x.state] || x.state}</td><td>${x.amount===null ? "—" : costNumber(x.amount)}</td><td>${x.reason || "见来源与报告"}</td></tr>`)}</tbody>
          </table></div></details>
        ${payload.coverage.issues.length ? html`<p>异常：${[...new Set(payload.coverage.issues.map(x=>x.code))].join("；")}</p>` : null}
        </section>` : null}
      ${(payload.benchmarks || []).length ? html`<section class="costing-panel"><h3 class="costing-panel-title">预算 / 基准差异</h3><div class="costing-table-wrap"><table class="costing-table"><thead><tr><th>对象</th><th>基准情景</th><th>本次成本</th><th>基准成本</th><th>差异</th></tr></thead><tbody>${payload.benchmarks.map((row, i) => html`<tr key=${i}><td>${objectName(row.object_id)}</td><td>${scenarioNames[row.scenario] || row.scenario}</td><td>${costNumber(row.actual_cost)}</td><td>${costNumber(row.benchmark_cost)}</td><td>${costNumber(row.variance)}</td></tr>`)}</tbody></table></div></section>` : null}
      ${(payload.standard_variances || []).length ? html`<section class="costing-panel"><h3 class="costing-panel-title">标准成本差异</h3><div class="costing-table-wrap"><table class="costing-table"><thead><tr><th>对象 / 类别</th><th>实际用量</th><th>标准用量</th><th>价格差异</th><th>耗用差异</th><th>合计差异</th></tr></thead><tbody>${payload.standard_variances.map((row, i) => html`<tr key=${i}><td>${objectName(row.object_id)} / ${row.element_id}</td><td>${costNumber(row.actual_quantity, 4)}</td><td>${costNumber(row.standard_quantity, 4)}</td><td>${costNumber(row.price_variance)}</td><td>${costNumber(row.usage_variance)}</td><td>${costNumber(row.total_variance)}</td></tr>`)}</tbody></table></div></section>` : null}
      ${payload.forecast ? html`<details class="costing-panel"><summary class="costing-panel-title">显式假设下的情景估算</summary><p>这是给定假设的算术推演，不代表实际未来业绩。</p><pre class="costing-audit">${JSON.stringify(payload.forecast, null, 2)}</pre></details>` : null}
      <section class="costing-panel"><h3 class="costing-panel-title">间接成本分摊（${allocations.length}条）</h3>${allocations.length ? html`<div class="costing-table-wrap"><table class="costing-table"><thead><tr><th>阶段</th><th>成本池</th><th>对象</th><th>动因</th><th>权重</th><th>金额</th><th>规则</th></tr></thead><tbody>${allocations.map((row, i) => html`<tr key=${i}><td>${row.stage}</td><td>${row.pool_name || row.pool_id}</td><td>${objectName(row.to_object)}</td><td>${row.driver_id || row.method} / ${row.driver_value}</td><td>${costNumber(row.share_pct)}%</td><td>${costNumber(row.amount)} ${row.currency}</td><td>${row.formula}</td></tr>`)}</tbody></table></div>` : html`<p class="muted">本次没有间接成本分摊。</p>`}</section>
      <section class="costing-panel"><h3 class="costing-panel-title">质量检查（${quality.length}项）</h3>${quality.length ? html`<ul class="costing-list">${quality.map((issue, i) => html`<li key=${i}><b>${objectName(issue.object_id || "")}</b> ${issue.message || issue.code}</li>`)}</ul>` : html`<p class="costing-ok">已执行的核算检查未发现异常；原始业务凭证真实性仍需业务复核。</p>`}</section>
      <section class="costing-panel"><h3 class="costing-panel-title">只问当前数据与结果</h3><p class="muted">回答绑定当前数据集、期间与版本。可查成本、收入、构成、差异和缺失资料；没有证据的原因不会被补写。</p><div class="costing-controls costing-gap"><label class="costing-question">你的问题<input maxLength="2000" value=${question} onInput=${e => setQuestion(e.target.value)} placeholder="例如：各对象的成本和利润分别是多少？" onKeyDown=${e => { if (e.key === "Enter" && !busy) askCurrent(); }} /></label><button class="btn primary" disabled=${!!busy} onClick=${() => askCurrent()}>${busy === "question" ? "核对结果中…" : "基于当前结果回答"}</button></div>
        <div class="costing-controls costing-gap">${["成本结构是什么？", "收入缺失时能判断利润吗？", "有哪些质量问题？"].map(text => html`<button key=${text} class="chip" disabled=${!!busy} onClick=${() => askCurrent(text)}>${text}</button>`)}</div>
        ${answer ? html`<div class="costing-answer"><div class="md-render" dangerouslySetInnerHTML=${{ __html: markdownToHtml(answer.answer || "") }}></div><div class="costing-controls">${(answer.evidence_ids || []).map(id => html`<button key=${id} class="linkbtn" disabled=${!!busy} onClick=${() => inspectEvidence(id)}>查看来源 ${id.slice(-8)}</button>`)}</div></div>` : null}
      </section>
      <section class="costing-panel"><h3 class="costing-panel-title">保存与导出当前报告</h3><p class="muted">报告包含当前范围的完整核算、质量边界、图表与来源附录。改变筛选后需重新分析，不会套用其他行业报告。</p><div class="costing-controls costing-gap"><button class="btn primary" disabled=${!!busy} onClick=${saveReport}>${busy === "save" ? "保存中…" : "保存当前报告"}</button>${["docx", "pdf"].map(format => html`<button key=${format} class="btn" disabled=${!!busy} onClick=${() => exportReport(format)}>${busy === "export" ? "导出中…" : `导出${format === "docx" ? "Word" : "PDF"}`}</button>`)}<button class="chip" disabled=${!!busy} onClick=${() => exportReport("json")}>下载精确结果JSON</button><button class="chip" disabled=${!!busy} onClick=${() => exportReport("md")}>下载Markdown报告</button></div><details class="costing-gap" onToggle=${e => setReportOpened(e.currentTarget.open)}><summary>查看完整报告正文</summary>${reportOpened ? html`<div class="md-render costing-report" dangerouslySetInnerHTML=${{ __html: markdownToHtml(result.report && result.report.markdown || payload.report && payload.report.markdown || "报告不可用") }}></div>` : null}</details></section>
      <section class="costing-panel"><h3 class="costing-panel-title">来源证据（${sources.length}项）</h3>${sources.length ? html`<div class="costing-table-wrap"><table class="costing-table"><thead><tr><th>来源</th><th>字段</th><th>原始行 / 页 / 条件</th><th>状态</th><th></th></tr></thead><tbody>${sources.slice(evidencePage * 40, evidencePage * 40 + 40).map(item => html`<tr key=${item.evidence_id}><td>${item.source_uri}</td><td>${item.field}</td><td>${item.selector}</td><td>${sourceStatus[item.status] || item.status}</td><td><button class="linkbtn" disabled=${!!busy} onClick=${() => inspectEvidence(item.evidence_id)}>核对原始记录</button></td></tr>`)}</tbody></table></div>` : html`<p class="muted">登录并运行分析后可打开当前结果的证据记录。</p>`}
        ${sources.length > 40 ? html`<div class="costing-controls costing-gap">
          <button class="chip" disabled=${!evidencePage} onClick=${() => setEvidencePage(x=>x-1)}>上页证据</button>
          <span>第 ${evidencePage+1} / ${Math.ceil(sources.length/40)} 页</span>
          <button class="chip" disabled=${(evidencePage+1)*40>=sources.length} onClick=${() => setEvidencePage(x=>x+1)}>下页证据</button></div>` : null}
        ${evidence ? html`<div class="costing-evidence"><h4>${evidence.source_uri}</h4><p>${evidence.description}</p><p class="muted">字段：${evidence.field} · 条件：${evidence.selector}</p>${evidence.current_source_matches === false ? html`<p class="msg err">当前原文件已变化；下面保留的是计算时的证据版本。</p>` : null}${evidence.sha256 ? html`<p class="costing-version">原文件SHA-256 ${evidence.sha256}</p>` : null}${(evidence.rows || []).map((row, i) => html`<div key=${i}><h5>CSV记录 ${row.record}</h5><div class="costing-table-wrap"><table class="costing-table"><thead><tr><th>列</th><th>原始值</th></tr></thead><tbody>${Object.entries(row.values || {}).map(([key, value]) => html`<tr key=${key}><td>${key}</td><td>${value}</td></tr>`)}</tbody></table></div></div>`)}${evidence.text ? html`<pre class="costing-audit">第${evidence.page}页：${evidence.text}</pre>` : null}${evidence.mapping ? html`<pre class="costing-audit">${JSON.stringify(evidence.mapping, null, 2)}</pre>` : null}</div>` : null}
      </section>
      <details class="costing-panel" onToggle=${e => setAuditOpened(e.currentTarget.open)}><summary class="costing-panel-title">核算审计轨迹（${(payload.audit || []).length}条）</summary>${auditOpened ? html`<pre class="costing-audit">${JSON.stringify(payload.audit || [], null, 2)}</pre>` : null}</details>
    ` : workspaceMode !== "import" ? html`<div class="costing-empty">选择已有数据集后运行核算，或从数据接入创建个人数据集。</div>` : null}
  </section>`;
}


/* Cost workbench navigation and workspace views. */
const WORKBENCH_NAV = [
  { id: "home", label: "工作概览", short: "概览" },
  { id: "data", label: "数据接入", short: "数据" },
  { id: "costing", label: "成本核算", short: "核算" },
  { id: "reports", label: "报告与追溯", short: "报告" },
  { id: "docs", label: "资料中心", short: "资料" }
];

function workspaceIdentity(me) {
  return me ? String(me.external_id ?? me.id ?? "") : "";
}

function useCostWorkspace(me) {
  const identity = workspaceIdentity(me);
  const [refreshKey, setRefreshKey] = useState(0);
  const [state, setState] = useState({ owner: "", loading: false, data: null, error: "" });
  useEffect(() => {
    let active = true;
    if (!identity) {
      setState({ owner: "", loading: false, data: null, error: "" });
      return () => { active = false; };
    }
    setState({ owner: identity, loading: true, data: null, error: "" });
    api("/api/platform/costing-workspace").then(data => {
      if (!active) return;
      setState(data._error
        ? { owner: identity, loading: false, data: null, error: String(data._error) }
        : { owner: identity, loading: false, data, error: "" });
    }).catch(error => {
      if (active) setState({ owner: identity, loading: false, data: null, error: String(error.message || error) });
    });
    return () => { active = false; };
  }, [identity, refreshKey]);
  const current = state.owner === identity ? state : { loading: !!identity, data: null, error: "" };
  return { ...current, refresh: () => setRefreshKey(value => value + 1) };
}

function workbenchDatasetLink(dataset) {
  return "#costing?dataset=" + encodeURIComponent(dataset.dataset_id);
}

function workbenchReportLink(report) {
  const id = report.analysis_id;
  const revision = report.revision || report.metadata?.revision;
  if (!/^[0-9a-f]{32}$/.test(id || "") || !/^[0-9a-f]{64}$/.test(revision || "")) return "";
  return "#costing?analysis=" + encodeURIComponent(id) + "&revision=" + encodeURIComponent(revision);
}

function workbenchIndustry(value) {
  if (typeof value === "string") return value;
  return value && (value.name || value.code) || "未声明";
}

function WorkbenchPageHeading({ title, description, children }) {
  return html`<div class="wb-page-heading"><div><h1>${title}</h1>${description ? html`<p>${description}</p>` : null}</div>
    ${children ? html`<div class="wb-actions">${children}</div>` : null}</div>`;
}

function WorkbenchEmpty({ title, description, href, action }) {
  return html`<div class="wb-empty"><h3>${title}</h3><p>${description}</p>
    ${href ? html`<a class="btn primary" href=${href}>${action}</a>` : null}</div>`;
}

function WorkbenchDatasetTable({ rows = [], compact = false }) {
  return html`<div class="costing-table-wrap"><table class=${"costing-table wb-table" + (compact ? " wb-dataset-compact" : "")}>
    <thead><tr><th>数据集</th>${compact ? null : html`<th>行业</th><th>可用期间</th><th>来源范围</th>`}<th></th></tr></thead>
    <tbody>${rows.map(row => html`<tr key=${row.dataset_id}><td class="wb-dataset-title"><strong>${row.label || row.dataset_id}</strong>
      ${compact ? html`<small class="wb-dataset-meta">${workbenchIndustry(row.industry)} · ${(row.periods || []).length > 2 ? row.periods[0] + " 等" + row.periods.length + "个期间" : (row.periods || []).join("、") || "期间未提供"}</small>` :
        row.description ? html`<small>${row.description}</small>` : null}</td>
      ${compact ? null : html`<td>${workbenchIndustry(row.industry)}</td><td>${(row.periods || []).join("、") || "未提供"}</td>
        <td><span class="wb-badge">${row.ownership === "personal" ? "个人工作区" : "共享合成样例"}</span></td>`}
      <td><a class="wb-card-link" href=${workbenchDatasetLink(row)}>开始核算 →</a></td></tr>`)}</tbody>
  </table></div>`;
}

function WorkbenchReportTable({ rows = [], compact = false }) {
  return html`<div class="costing-table-wrap"><table class=${"costing-table wb-table" + (compact ? " wb-report-compact" : "")}>
    <thead><tr><th>报告</th>${compact ? null : html`<th>期间</th>`}<th>成本归集额</th>${compact ? null : html`<th>行业 / 对象数</th><th>固定修订</th>`}<th></th></tr></thead>
    <tbody>${rows.map(row => {
      const metadata = row.metadata || {};
      const link = workbenchReportLink(row);
      const revision = row.revision || metadata.revision || "";
      const total = row.cost_total ?? metadata.cost_total;
      const currency = row.currency || metadata.currency || "";
      const count = row.object_count ?? metadata.object_count;
      const period = row.period || metadata.period || "期间未提供";
      return html`<tr key=${row.analysis_id}><td class="wb-report-title"><strong>${metadata.dataset_label || row.dataset_label || metadata.dataset_id || row.analysis_id}</strong>
        <small class="wb-report-meta">${compact ? period + " · " : ""}分析 ${row.analysis_id?.slice(0, 8)}</small></td>
        ${compact ? null : html`<td>${period}</td>`}
        <td class="wb-money">${total == null ? "未提供" : costNumber(total) + " " + currency}</td>
        ${compact ? null : html`<td>${workbenchIndustry(metadata.industry || row.industry)}<small>${Number.isInteger(count) ? count + " 个对象" : "对象数未提供"}</small></td><td title=${revision}><code>${revision ? revision.slice(0, 12) + "…" : "版本信息缺失"}</code></td>`}
        <td>${link ? html`<a class="wb-card-link" href=${link}>重开与追溯 →</a>` : html`<span class="muted">版本信息缺失</span>`}</td></tr>`;
    })}</tbody>
  </table></div>`;
}

function WorkbenchExamples({ me, onNeedLogin, compact = false, onCount }) {
  const { data, loading, error } = useApi("/api/platform/costing-examples");
  const [busy, setBusy] = useState("");
  const [failure, setFailure] = useState("");
  const generation = useRef(0);
  const identity = workspaceIdentity(me);
  useEffect(() => {
    ++generation.current;
    setBusy(""); setFailure("");
    return () => { ++generation.current; };
  }, [identity]);
  const launch = async example => {
    if (!me) { onNeedLogin(); return; }
    if (busy) return;
    const request = ++generation.current;
    setBusy(example.id); setFailure("");
    try {
      const response = await api("/api/platform/costing-examples/" + encodeURIComponent(example.id) + "/import",
        { method: "POST", body: {} });
      if (request !== generation.current) return;
      if (response._error) throw new Error(response._error);
      const snapshot = response.analysis;
      if (!/^[0-9a-f]{32}$/.test(snapshot?.analysis_id || "") || !/^[0-9a-f]{64}$/.test(snapshot?.revision || "")) {
        throw new Error("示例未返回完整分析版本，请重试。");
      }
      location.hash = "costing?analysis=" + encodeURIComponent(snapshot.analysis_id) + "&revision=" + encodeURIComponent(snapshot.revision);
    } catch (error) {
      if (request === generation.current) setFailure(String(error.message || error));
    } finally {
      if (request === generation.current) setBusy("");
    }
  };
  const examples = data?.examples || [];
  useEffect(() => {
    if (data && Array.isArray(data.examples) && onCount) onCount(data.examples.length);
  }, [data, onCount]);
  return html`<section class="wb-card wb-examples"><div class="wb-card-head"><div><h3>行业合成示例</h3>
    <p>用配套原始文件和规则体验完整核算流程。</p></div><span class="wb-badge">合成验证数据</span></div>
    ${loading ? html`<p class="muted">正在读取配套示例…</p>` : null}
    ${error ? html`<p class="msg err" role="alert">${error}</p>` : null}
    ${failure ? html`<p class="msg err" role="alert">${failure}</p>` : null}
    <div class="wb-industry-grid">${examples.map(example => {
      const files = example.source_files || [];
      const rowCount = files.every(file => Number.isInteger(file.row_count)) ? files.reduce((sum, file) => sum + file.row_count, 0) : null;
      return html`<article class="wb-industry-card" key=${example.id}><div class="wb-industry-card-head">
        <h4>${example.name}</h4><span class="wb-badge">${example.period}</span></div>
        <p>${example.description}</p>
        <dl><div><dt>原始来源</dt><dd>${files.length} 份文件${rowCount == null ? "" : " · " + rowCount + " 行"}</dd></div>
          <div><dt>预期成本合计</dt><dd>${costNumber(example.expected_total)} ${example.currency}</dd></div></dl>
        ${compact ? null : html`<small>配置版本 ${example.configuration_version}</small>`}
        <button type="button" class="btn" disabled=${!!busy} onClick=${() => launch(example)}>
          ${busy === example.id ? "正在导入并核算…" : me ? "载入示例并核算" : "登录后体验"}</button>
      </article>`;
    })}</div>
    ${!loading && !error && !examples.length ? html`<p class="muted">当前没有可用的配套示例。</p>` : null}
  </section>`;
}

function WorkbenchOverview({ me, onNeedLogin }) {
  const [exampleCount, setExampleCount] = useState(null);
  const workspace = useCostWorkspace(me);
  const data = workspace.data;
  const datasets = (data?.datasets || []).filter(row => row.ownership === "personal");
  const reports = data?.reports || [];
  const stats = data?.statistics;
  const count = key => stats && Number.isInteger(stats[key]) ? stats[key] : "—";
  return html`<div class="wb-overview">
    <${WorkbenchPageHeading} title="工作概览" description="选择数据集开展核算，保存每次结果并核对原始依据。">
      <a class="btn" href="#reports">查看报告</a><a class="btn primary" href="#data">导入数据</a>
    <//>
    ${!me ? html`<div class="wb-card wb-context"><div><h3>创建你的成本工作区</h3><p>登录后导入数据、保存报告，并保留对应的规则和来源版本。</p></div>
      <button class="btn primary" onClick=${onNeedLogin}>登录或注册</button></div>` : null}
    ${workspace.error ? html`<div class="msg err" role="alert">${workspace.error}<button class="linkbtn" onClick=${workspace.refresh}>重新读取</button></div>` : null}
    <div class="wb-kpi-grid">
      ${[["个人数据集", me ? count("personal_datasets") : "登录查看", "当前账户可用的数据"],
         ["已保存报告", me ? count("saved_reports") : "登录查看", "可按原修订重新打开"],
         ["行业合成示例", data && Number.isInteger(data.examples_count) ? data.examples_count : exampleCount == null ? "—" : exampleCount, "配套原始文件与规则"],
         ["待处理接入问题", me ? count("unavailable_items") : "登录查看", "待核对的接入与版本资料"]].map(([name,value,detail]) =>
        html`<article class="wb-kpi" key=${name}><span>${name}</span><strong>${value}</strong><small>${workspace.loading ? "正在读取当前账户…" : detail}</small></article>`)}
    </div>
    <div class="wb-grid">
      <section class="wb-card"><div class="wb-card-head"><div><h3>我的数据集</h3><p>从同一份数据继续核算。</p></div><a href="#data" class="wb-card-link">管理数据 →</a></div>
        ${workspace.loading ? html`<p class="muted">正在读取数据集…</p>` : datasets.length ? html`<${WorkbenchDatasetTable} rows=${datasets.slice(0, 5)} compact=${true} />` :
        html`<${WorkbenchEmpty} title=${me ? "还没有个人成本数据" : "登录后查看个人数据"} description="导入配套CSV和规则，或选择下方合成示例开始。" href="#data" action="接入数据" />`}
      </section>
      <section class="wb-card"><div class="wb-card-head"><div><h3>最近保存的报告</h3><p>按分析编号和修订重开。</p></div><a href="#reports" class="wb-card-link">全部报告 →</a></div>
        ${workspace.loading ? html`<p class="muted">正在读取报告…</p>` : reports.length ? html`<${WorkbenchReportTable} rows=${reports.slice(0, 5)} compact=${true} />` :
        html`<${WorkbenchEmpty} title="尚无已保存报告" description="完成核算后保存报告，即可从这里继续查看与导出。" href="#costing" action="进入核算" />`}
      </section>
    </div>
    <${WorkbenchExamples} me=${me} onNeedLogin=${onNeedLogin} compact=${true} onCount=${setExampleCount} />
  </div>`;
}

function WorkbenchData({ me, onNeedLogin }) {
  const workspace = useCostWorkspace(me);
  const personal = (workspace.data?.datasets || []).filter(row => row.ownership === "personal");
  const shared = (workspace.data?.datasets || []).filter(row => row.ownership === "shared_sample");
  return html`<div class="wb-data">
    <${WorkbenchPageHeading} title="数据接入" description="原始文件、字段映射和规则与每次分析一同保留。">
      <button class="btn" onClick=${workspace.refresh} disabled=${workspace.loading}>刷新列表</button>
    <//>
    <section class="wb-card"><div class="wb-card-head"><div><h3>个人数据集</h3><p>选择已有数据集核算，或在下方导入新文件。</p></div></div>
      ${workspace.error ? html`<p class="msg err" role="alert">${workspace.error}</p>` : null}
      ${workspace.loading ? html`<p class="muted">正在读取…</p>` : personal.length ? html`<${WorkbenchDatasetTable} rows=${personal} />` :
        html`<p class="muted">${me ? "当前账户还没有个人成本数据集。" : "登录后查看个人数据集并导入文件。"}</p>`}
      ${(workspace.data?.diagnostics || []).length ? html`<details class="wb-data-diagnostics"><summary>待处理资料（${workspace.data.diagnostics.length}）</summary><ul class="costing-list">
        ${workspace.data.diagnostics.map((item, index) => html`<li key=${index}><strong>${item.label || item.dataset_label || item.dataset_id || "读取提示"}</strong> ${item.message || "资料或版本暂不可用，请检查后重试。"}</li>`)}
      </ul></details>` : null}
      ${shared.length ? html`<details class="wb-shared-samples"><summary>历史共享样例（${shared.length}）</summary><${WorkbenchDatasetTable} rows=${shared} /></details>` : null}
    </section>
    <${CostingPlatform} me=${me} onNeedLogin=${onNeedLogin} workspaceMode="import" />
    <${WorkbenchExamples} me=${me} onNeedLogin=${onNeedLogin} />
  </div>`;
}

function WorkbenchReports({ me, onNeedLogin }) {
  const workspace = useCostWorkspace(me);
  const [search, setSearch] = useState("");
  const all = workspace.data?.reports || [];
  const query = search.trim().toLowerCase();
  const reports = all.filter(row => !query || JSON.stringify([
    row.metadata?.dataset_label, row.metadata?.dataset_id, row.analysis_id, row.period, row.metadata?.industry
  ]).toLowerCase().includes(query));
  return html`<div class="wb-reports">
    <${WorkbenchPageHeading} title="报告与追溯" description="打开已保存的固定修订，查看金额、分摊过程与原始凭证。">
      <button class="btn" disabled=${workspace.loading} onClick=${workspace.refresh}>刷新报告</button><a class="btn primary" href="#costing">新建分析</a>
    <//>
    ${!me ? html`<${WorkbenchEmpty} title="登录后查看个人报告" description="报告和原始依据按当前账户保留。" href="#login" action="登录" />` :
      html`<section class="wb-card"><div class="wb-card-head"><h3>已保存报告（${all.length}）</h3>
        <label class="wb-search">查找报告<input type="search" value=${search} onInput=${e => setSearch(e.target.value)} placeholder="数据集、期间或分析编号" /></label></div>
        ${workspace.error ? html`<p class="msg err" role="alert">${workspace.error}</p>` : null}
        ${workspace.loading ? html`<p class="muted">正在读取当前账户的报告…</p>` : reports.length ? html`<${WorkbenchReportTable} rows=${reports} />` :
          html`<${WorkbenchEmpty} title=${query ? "没有匹配的报告" : "还没有已保存报告"} description=${query ? "试试其他数据集名称或期间。" : "在成本核算中运行分析，再点击保存当前报告。"} href=${query ? null : "#costing"} action="进入核算" />`}
      </section>`}
  </div>`;
}

function WorkbenchDocuments({ me, onNeedLogin, onAuthed }) {
  const pendingConversation = new URLSearchParams((location.hash.split("?")[1] || "")).has("conv");
  const [section, setSection] = useState(pendingConversation ? "questions" : "files");
  const [refreshKey, setRefreshKey] = useState(0);
  return html`<div class="wb-documents">
    <${WorkbenchPageHeading} title="资料中心" description="保存成本口径、合同和业务说明，检索其中的依据。">
      <a class="btn" href="#data">接入成本数据</a>
    <//>
    <div class="wb-page-tabs" role="tablist" aria-label="资料中心">
      ${[["files","资料文件"],["questions","资料问答"],["legacy","历史示例"]].map(([id,label]) =>
        html`<button role="tab" aria-selected=${section === id} class=${section === id ? "is-active" : ""} onClick=${() => setSection(id)} key=${id}>${label}</button>`)}
    </div>
    ${section === "files" ? html`<section class="wb-card"><p>成本CSV与配套规则通过数据接入创建数据集。本页用于整理说明材料和文档引用。</p>
      <${UploadZone} me=${me} onNeedLogin=${onNeedLogin} onChanged=${() => setRefreshKey(value => value + 1)} />
      <${WorkspaceList} me=${me} refreshKey=${refreshKey} />
    </section>` : null}
    ${section === "questions" ? html`<section class="wb-card"><p>资料问答依据文档内容。需要核对当前成本结果时，可在成本核算中打开同一份分析报告。</p>
      <${HeroAsk} me=${me} onNeedLogin=${onNeedLogin} onAuthed=${onAuthed} />
    </section>` : null}
    ${section === "legacy" ? html`<section class="wb-card"><div class="wb-card-head"><div><h3>历史共享示例</h3>
      <p>保留原示例的数据范围、期间和说明，便于回看既有演示。</p></div></div>
      <div class="wb-actions"><a class="btn" href="#legacy">历史资料工作台</a><a class="btn" href="#trend">历史趋势示例</a><a class="btn" href="#benchmark">历史对标示例</a><a class="btn" href="#notes">数据说明</a><a class="btn" href="#history">更新记录</a><a class="btn" href="#rectify">问题与整改记录</a></div>
    </section>` : null}
  </div>`;
}

const TABS = [
  ...WORKBENCH_NAV,
  { id: "legacy", label: "历史资料工作台" },
  { id: "trend", label: "历史趋势示例" },
  { id: "benchmark", label: "历史对标示例" },
  { id: "notes", label: "数据说明" },
  { id: "history", label: "更新记录" },
  { id: "rectify", label: "问题与整改记录" },
  { id: "profile", label: "账户设置" },
  { id: "login", label: "登录" }
];

const TAB_ALIAS = { overview: "home", chat: "docs", panel: "home", documents: "docs" };

function useHashTab() {
  const read = () => {
    const [route, query] = (location.hash || "").replace(/^#/, "").split("?", 2);
    let id = TAB_ALIAS[route] || route || "home";
    if (id === "home" && new URLSearchParams(query || "").has("conv")) id = "docs";
    return TABS.some(tab => tab.id === id) ? id : "home";
  };
  const [tab, setTab] = useState(read);
  useEffect(() => {
    const onHashChange = () => setTab(read());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);
  return [tab, id => { location.hash = id; setTab(read()); }];
}

function App() {
  const [tab, go] = useHashTab();
  const [me, setMe] = useState(null);
  const [authReady, setAuthReady] = useState(false);
  const [guest, setGuest] = useState(false);
  const [notice, setNotice] = useState("");
  const returnRoute = useRef("home");
  const authRequest = useRef(0);
  const refreshMe = async () => {
    const request = ++authRequest.current;
    const response = await api("/api/auth/me");
    if (request !== authRequest.current) return null;
    setMe(response && response.logged_in ? response : null);
    setAuthReady(true);
    return response;
  };
  useEffect(() => { refreshMe(); }, []);
  useEffect(() => {
    if (authReady && !me && !guest && !location.hash) go("login");
  }, [authReady, me, guest]);
  useEffect(() => {
    if (authReady && me && tab === "login") go(returnRoute.current || "home");
  }, [authReady, me, tab]);
  const requestedReport = () => {
    const [route, query] = location.hash.replace(/^#/, "").split("?", 2);
    const params = new URLSearchParams(query || "");
    const id = params.get("analysis") || "";
    const revision = params.get("revision") || "";
    if (route !== "costing" || !/^[0-9a-f]{32}$/.test(id) || !/^[0-9a-f]{64}$/.test(revision)) return "";
    return "costing?analysis=" + id + "&revision=" + revision;
  };
  const needLogin = route => {
    returnRoute.current = WORKBENCH_NAV.some(item => item.id === route) ? route : "home";
    if (route === "costing") returnRoute.current = requestedReport() || returnRoute.current;
    go("login");
  };
  const onAuthed = async () => {
    returnRoute.current = requestedReport() || returnRoute.current;
    setGuest(false); setNotice("");
    const current = await refreshMe();
    if (current?.logged_in) go(returnRoute.current || "home");
  };
  const onLoggedOut = switchAccount => {
    ++authRequest.current;
    setMe(null); setGuest(!switchAccount);
    setNotice(switchAccount ? "已退出，请用另一个账号登录" : "");
    returnRoute.current = requestedReport() || "home";
    go("login");
  };
  if (!authReady) return html`<div class="loginpage"><div class="loginbox"><div class="login-brand"><div class="mark">析</div><h1>通用成本智能分析平台</h1></div><p>正在读取工作区…</p></div></div>`;
  const pendingReport = requestedReport();
  if (!me && pendingReport) {
    returnRoute.current = pendingReport;
    return html`<${LoginPage} onAuthed=${onAuthed} notice=${notice}
      onGuest=${() => { setGuest(true); go("home"); }} />`;
  }
  if (!me && tab === "login") return html`<${LoginPage} onAuthed=${onAuthed} notice=${notice}
    onGuest=${() => { setGuest(true); go("home"); }} />`;
  const identity = workspaceIdentity(me) || "guest";
  const legacy = ["legacy", "trend", "benchmark", "notes", "history", "rectify"].includes(tab);
  const primaryTab = legacy ? "docs" : tab;
  const account = me ? html`<button class="who" title="进入个人中心" onClick=${() => go("profile")}>
      <span class="who-name">${me.name || me.external_id}</span><span class="who-go">账户设置</span>
    </button>` : html`<button class="who" title="去登录" onClick=${() => needLogin(primaryTab)}><span class="who-anon">访客</span><span class="who-go">登录 / 注册</span></button>`;
  return html`<div class="wb-app">
    <aside class="wb-sidebar"><a class="wb-brand" href="#home"><span class="wb-brand-mark">析</span><div><strong>通用成本分析</strong><small>数据与核算工作台</small></div></a>
      <nav class="wb-nav" aria-label="主导航">${WORKBENCH_NAV.map(item => html`<a key=${item.id} href=${"#" + item.id}
        class=${"wb-nav-item" + (primaryTab === item.id ? " is-active" : "")} aria-current=${primaryTab === item.id ? "page" : null}>${item.label}</a>`)}</nav>
      <div class="wb-sidebar-footer"><strong>${me ? "个人工作区" : "访客浏览"}</strong><p>${me ? "数据集、报告与原始依据随账户保留。" : "登录后创建个人数据集并保存报告。"}</p>
        <a href="#docs">查看资料与使用入口 →</a></div>
    </aside>
    <main class="wb-main"><header class="wb-topbar"><div class="wb-topbar-title"><strong>成本工作台</strong></div>
      <div class="wb-context"><span class="wb-badge">${me ? "个人工作区" : "访客"}</span>${account}</div></header>
      <div class="wb-content">
        ${legacy ? html`<div class="wb-card wb-context"><div><strong>历史共享示例</strong><p>当前页面保留原示例的行业、期间和资料范围。</p></div><a class="btn" href="#home">返回工作概览</a></div>` : null}
        ${tab === "home" || (me && tab === "login") ? html`<${WorkbenchOverview} key=${"home:" + identity} me=${me} onNeedLogin=${() => needLogin("home")} />` : null}
        ${tab === "data" ? html`<${WorkbenchData} key=${"data:" + identity} me=${me} onNeedLogin=${() => needLogin("data")} />` : null}
        ${tab === "costing" ? html`<${CostingPlatform} key=${"costing:" + identity} me=${me} onNeedLogin=${() => needLogin("costing")} />` : null}
        ${tab === "reports" ? html`<${WorkbenchReports} key=${"reports:" + identity} me=${me} onNeedLogin=${() => needLogin("reports")} />` : null}
        ${tab === "docs" ? html`<${WorkbenchDocuments} key=${"docs:" + identity} me=${me} onNeedLogin=${() => needLogin("docs")} onAuthed=${refreshMe} />` : null}
        ${tab === "legacy" ? html`<${LegacyHome} key=${"legacy:" + identity} me=${me} onNeedLogin=${() => needLogin("docs")} onAuthed=${refreshMe} />` : null}
        ${tab === "trend" ? html`<${Trend} />` : null}
        ${tab === "benchmark" ? html`<${Benchmark} key=${"benchmark:" + identity} me=${me} onNeedLogin=${() => needLogin("docs")} />` : null}
        ${tab === "notes" ? html`<${Notes} />` : null}
        ${tab === "history" ? html`<${History} />` : null}
        ${tab === "rectify" ? html`<${Rectify} key=${"rectify:" + identity} me=${me} onNeedLogin=${() => needLogin("docs")} onAuthed=${refreshMe} />` : null}
        ${tab === "profile" ? html`<${Profile} key=${"profile:" + identity} me=${me} onAuthed=${refreshMe} onLoggedOut=${onLoggedOut} />` : null}
        <footer class="wb-footer">核算结果固定数据集、期间与修订，报告与原始依据随版本保留。</footer>
      </div>
    </main>
  </div>`;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
