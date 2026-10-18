/* ============================================================================
   DSH 多版本启动器 —— Web 前端
   ----------------------------------------------------------------------------
   三条设计规矩（都是为了帧数与"有过渡"）：

   ① **原地改，不重建。** lane 卡片按 lane 名、profile 行按 profile 名做 key，
      节点缓存起来只改文字与 class —— 所以刷新时不会闪，滚动位置和展开状态不受影响。
      （tkinter 版每次刷新都把画布上的卡片删掉重画。）

   ② **先攒后渲染。** SSE 事件只写进模型 + 置脏标记，真正的 DOM 改动统一放在
      requestAnimationFrame 里做一次 —— 同一帧来 200 行日志也只渲染一次。

   ③ **动画只碰 transform / opacity**（外加展开收起的 grid-template-rows），
      交给合成器线程，主线程不被拖住。

   和后端的契约（事件名 / API 名）在 dsh_lanes_web.py 里，两边要一起改。
   ========================================================================= */

(() => {
'use strict';

/* ── 小工具 ─────────────────────────────────────────────────────────────── */

const $ = (sel, root) => (root || document).querySelector(sel);

/** 建节点。第三个参数起都是子节点（字符串按文本处理）——
    比"建完再 append 一串"短得多，卡片与对话框里到处在用。 */
function el(tag, cls, ...children) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  for (const child of children) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function button(cls, text, act, data) {
  const node = el('button', cls, text);
  node.type = 'button';
  if (act) node.dataset.act = act;
  if (data) Object.assign(node.dataset, data);
  return node;
}

/** 把 nodes 按给定顺序排进 parent，尽量少动 DOM。

    注意那个 `node.parentNode !== parent` 判断：新建的节点还没挂上去时
    `nextElementSibling` 也是 null，光比 nextElementSibling 会得出"位置已经对了"的
    结论，于是节点根本没进 DOM、后面的 insertBefore 还会抛
    "The node before which the new node is to be inserted is not a child of this node"。
*/
function orderNodes(parent, nodes) {
  let ref = null;
  for (let i = nodes.length - 1; i >= 0; i -= 1) {
    const node = nodes[i];
    if (node.parentNode !== parent || node.nextElementSibling !== ref) {
      parent.insertBefore(node, ref);
    }
    ref = node;
  }
}

/** 只在真的变了才写 —— 无谓的写会让浏览器白做一次样式/布局 */
function setText(node, text) {
  const value = text == null ? '' : String(text);
  if (node.textContent !== value) node.textContent = value;
}

function setClass(node, name, on) {
  if (node.classList.contains(name) !== !!on) node.classList.toggle(name, on);
}

function setHidden(node, hidden) {
  if (node.hidden !== !!hidden) node.hidden = !!hidden;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

/** 卡片上只显示缩略 URL（完整地址点一下就复制），避免长 token 把布局挤变形 */
function shortUrl(url, keep = 14) {
  const m = /^(.*\?token=)(.+)$/.exec(url || '');
  if (!m) return url || '';
  const token = m[2];
  if (token.length <= keep + 8) return url;
  return m[1] + token.slice(0, keep) + '…' + token.slice(-4);
}

const THEME_LABEL = { light: '浅色', dark: '深色' };

/* ── 模型 ───────────────────────────────────────────────────────────────── */

const LOG_MAX = 2000;          // 日志面板保留的行数上限（超出从头裁）

const S = {
  ready: false,
  theme: 'light',
  root: '',
  rootWritable: { ok: true, detail: '' },
  lanes: [],
  laneMap: new Map(),
  foreign: [],
  installs: [],
  counts: { lanes: 0, profiles: 0, running: 0, foreign: 0 },
  busy: false,
  busyTitle: '',
  remote: { text: '正在查询 npm 上的最新版本…', ok: true, pending: true },
  registry: { choice: 'auto', value: '' },   // 新建 / 升级对话框里「下载源」的默认选中项
  expand: new Map(),           // lane 名 → 是否展开（用户点过才写）
  follow: true,
  connected: false,
};

/* ── 与后端通信 ─────────────────────────────────────────────────────────── */

async function api(action, body) {
  let resp;
  try {
    resp = await fetch('/api/' + action, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  } catch (err) {
    throw Object.assign(new Error('与启动器服务的连接断了：' + err.message), { net: true });
  }
  let data = null;
  try { data = await resp.json(); } catch (_) { /* 没有 JSON 体也要能报错 */ }
  if (!resp.ok) {
    const msg = (data && data.error) || ('HTTP ' + resp.status);
    if (resp.status === 409 && data && data.error === 'busy') {
      throw new Error(`上一个任务还在进行中：${data.title || ''}（请等它结束）`);
    }
    throw new Error(msg);
  }
  return data;
}

/** 动作的统一出口：错误只在这里变成吐司 + 日志，业务代码里不必层层 try */
async function act(label, action, body) {
  try {
    return await api(action, body);
  } catch (err) {
    toast(`${label}失败：${err.message}`, 'err', 4200);
    pushLocal(`[XX] ${label}失败：${err.message}`, 'err');
    return null;
  }
}

/* ── 吐司 / 模态 ────────────────────────────────────────────────────────── */

function toast(message, kind, ms) {
  const box = $('#toasts');
  raiseToasts();
  const node = el('div', 'toast' + (kind ? ' ' + kind : ''), message);
  box.append(node);
  setTimeout(() => {
    node.classList.add('out');
    setTimeout(() => node.remove(), 240);
  }, ms || 2600);
}

/**
 * 把吐司层顶到 top layer 的最上面。
 *
 * 为什么需要：#toasts 是 popover="manual"，它和 <dialog> 都在 top layer。
 * 后进的在上 —— 所以对话框一开，之前就在的吐司就被它的 ::backdrop 压住了。
 * 每次显示吐司、每次开窗口都重新举一次，窗口里报的错才不会"说了等于没说"。
 */
function raiseToasts() {
  const box = $('#toasts');
  if (!box || !box.showPopover) return;
  try {
    if (box.matches(':popover-open')) box.hidePopover();
    box.showPopover();
  } catch (_) { /* 不支持就算了：吐司只是提示，不影响操作 */ }
}

const modalEl = $('#modal');
let modalResolve = null;

/**
 * 通用确认框。返回 Promise<boolean>。
 * body 给字符串就是一段说明；也可以给 build(bodyEl) 自己往里塞结构（kv 表之类）。
 */
function confirmBox(opts) {
  const head = $('#modal-head');
  const body = $('#modal-body');
  const okBtn = $('#modal-ok');
  setText(head, opts.head || '确认');
  clear(body);
  if (opts.text) body.append(el('p', null, opts.text));
  if (opts.build) opts.build(body);
  setText(okBtn, opts.ok || '确定');
  okBtn.className = opts.danger ? 'btn danger' : 'btn primary';
  return new Promise((resolve) => {
    modalResolve = resolve;
    modalEl.showModal();
  });
}

function settleModal(value) {
  const resolve = modalResolve;
  modalResolve = null;
  if (modalEl.open) modalEl.close();
  if (resolve) resolve(value);
}

$('#modal-ok').addEventListener('click', () => settleModal(true));
$('#modal-cancel').addEventListener('click', () => settleModal(false));
modalEl.addEventListener('cancel', (event) => { event.preventDefault(); settleModal(false); });
modalEl.addEventListener('close', () => settleModal(false));

/* ── 日志面板 ───────────────────────────────────────────────────────────── */

const logEl = $('#log');
let logLines = 0;

function lineNode(line) {
  const node = el('span', 'ln' + (line.tag ? ' t-' + line.tag : ''));
  // 空行也要占一行：pre 里空文本节点会被当成"什么都没有"
  node.textContent = line.text === '' ? '\u200b' : line.text;
  return node;
}

function atLogBottom() {
  return logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight <= 32;
}

function scrollLogToEnd() {
  logEl.scrollTop = logEl.scrollHeight;
}

function setJump(show) {
  setHidden($('#btn-log-jump'), !show);
}

function appendLog(lines) {
  if (!lines || !lines.length) return;
  const frag = document.createDocumentFragment();
  for (const line of lines) frag.append(lineNode(line));
  logEl.append(frag);
  logLines += lines.length;
  // 超出上限就从头裁：文本控件只增不减的话，跑几次安装连滚动都会变卡
  while (logLines > LOG_MAX && logEl.firstElementChild) {
    logEl.firstElementChild.remove();
    logLines -= 1;
  }
  if (S.follow) scrollLogToEnd();
}

function resetLog(lines) {
  clear(logEl);
  logLines = 0;
  if (lines && lines.length) {
    const frag = document.createDocumentFragment();
    for (const line of lines) frag.append(lineNode(line));
    logEl.append(frag);
    logLines = lines.length;
  }
  if (S.follow) scrollLogToEnd();
}

/** 界面自己的提示（不进服务器日志缓冲，刷新页面就没了） */
function pushLocal(text, tag) {
  appendLog([{ text, tag: tag || '' }]);
  if (S.follow) scrollLogToEnd();
}

logEl.addEventListener('scroll', () => {
  if (atLogBottom()) {
    setJump(false);
    return;
  }
  // 用户往上翻了：自动跟随让位，右下角给一个"回到最新"
  if (S.follow) {
    S.follow = false;
    $('#follow').checked = false;
  }
  setJump(true);
}, { passive: true });

$('#follow').addEventListener('change', (event) => {
  S.follow = event.target.checked;
  if (S.follow) { scrollLogToEnd(); setJump(false); }
});

$('#btn-log-clear').addEventListener('click', async () => {
  resetLog([]);
  await api('log_clear', {}).catch(() => {});
  pushLocal('日志已清空。', 'muted');
});

$('#btn-log-copy').addEventListener('click', async () => {
  const text = logEl.innerText.replace(/\u200b/g, '').trim();
  if (!text) { toast('日志是空的', 'err'); return; }
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制到剪贴板', 'ok');
  } catch (err) {
    toast('复制失败：' + err.message, 'err');
  }
});

$('#btn-log-jump').addEventListener('click', () => {
  S.follow = true;
  $('#follow').checked = true;
  scrollLogToEnd();
  setJump(false);
});

/* ── 顶部 / 底部 ────────────────────────────────────────────────────────── */

function setTheme(theme, persist) {
  const next = theme === 'dark' ? 'dark' : 'light';
  S.theme = next;
  document.documentElement.dataset.theme = next;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', next === 'dark' ? '#0a0d16' : '#e8ecf6');
  setText($('#btn-theme'), `${THEME_LABEL[next === 'dark' ? 'light' : 'dark']}模式`);
  if (persist) act('切换主题', 'theme', { theme: next });
}

$('#btn-theme').addEventListener('click', () => {
  setTheme(S.theme === 'dark' ? 'light' : 'dark', true);
});

function renderHeader() {
  const remote = $('#remote');
  setText($('#remote-text'), S.remote.text + '　');
  setClass(remote, 'pending', !!S.remote.pending);
  setClass(remote, 'fail', !S.remote.pending && !S.remote.ok);
}

function renderStatus() {
  const counts = S.counts;
  setText($('#counts'),
    `lane ${counts.lanes} 条 / profile ${counts.profiles} 个（运行 ${counts.running}）`
    + `｜未登记实例 ${counts.foreign} 个｜根目录 ${S.root}`);
  setText($('#busy'), S.busy ? `● ${S.busyTitle} 进行中…` : '');
  const bar = $('.status');
  let warn = $('#root-warn');
  if (!S.rootWritable.ok) {
    if (!warn) {
      warn = el('span', 'pill warn', '');
      warn.id = 'root-warn';
      bar.insertBefore(warn, $('#counts'));
    }
    // 全文放 title：状态栏只留一行，长了就省略号
    const text = `根目录不可写：${S.rootWritable.detail}`;
    setText(warn, text);
    warn.title = text;
  } else if (warn) {
    warn.remove();
  }
}

/* ── lane 卡片：keyed 渲染 ──────────────────────────────────────────────── */

const laneCards = new Map();     // lane 名 → article
const profRows = new Map();      // lane 名 → Map(profile 名 → row)
let skeletonCleared = false;

function isEditing(node) {
  return document.activeElement === node;
}

function buildCard(lane) {
  const card = el('article', 'card');
  card.dataset.lane = lane.name;

  const head = el('div', 'card-head');
  const title = el('div', 'card-title');
  const name = el('h3', null, lane.name);
  const ver = el('span', 'ver');
  const kind = el('span', 'pill tag');
  title.append(name, ver, kind);
  head.append(title);

  // 右侧按钮先放：窗口窄的时候宁可挤左边的标题，也不能把按钮压没
  const ops = el('div', 'card-ops');
  const badge = el('span', 'pill');
  const openBtn = button('btn primary', '打开', 'open-lane');
  const stopBtn = button('btn danger', '全部停止', 'stop-lane');
  ops.append(badge, openBtn, stopBtn);
  head.append(ops);
  card.append(head);

  card.append(el('div', 'sep'));

  const profHead = el('div', 'prof-head');
  const summary = el('span', 'summary');
  const addBtn = button('link', '＋ 添加 Profile…', 'add-profile');
  const toggle = button('link muted toggle', '', 'toggle');
  const toggleText = el('span', null, '收起');
  toggle.append(toggleText, el('i', 'chev', '▾'));
  profHead.append(summary, addBtn, toggle);
  card.append(profHead);

  const body = el('div', 'prof-body');
  const inner = el('div', 'prof-inner');
  const note = el('p', 'prof-note',
    '同一套会话与凭据；各自独立的插件组合＝多环境。端口留空＝自动挑空闲。');
  inner.append(note);
  body.append(inner);
  card.append(body);

  card.append(el('div', 'sep'));

  const foot = el('div', 'card-foot');
  const row1 = el('div', 'row');
  row1.append(
    button('link', '凭据…', 'dlg-credentials'),
    button('link', '服务商…', 'dlg-providers'),
    button('link', '插件…', 'dlg-plugins'),
    button('link', '插件市场…', 'dlg-market'),
    button('link', '配置文件…', 'dlg-files'),
    button('link', '备份对话…', 'dlg-chat'),
    button('link', '配置备份…', 'dlg-cfg'),
  );
  const verify = button('link right', '核对隔离', 'verify');
  verify.hidden = true;
  row1.append(verify);

  const row2 = el('div', 'row');
  const cloneBtn = button('link', '复制…', 'dlg-clone');
  const upgradeBtn = button('link', '升级版本…', 'dlg-upgrade');
  const delBtn = button('link danger right', '删除这套 DSH', 'dlg-delete');
  row2.append(cloneBtn, upgradeBtn,
    el('span', 'note', '端口留空＝自动挑空闲；插件…＝读取 / 迁移插件'), delBtn);
  foot.append(row1, row2);
  card.append(foot);

  card._u = { ver, kind, badge, openBtn, stopBtn, summary, toggle, toggleText, inner, note, verify };
  return card;
}

function updateCard(card, lane) {
  const u = card._u;
  setText(u.ver, `  ${lane.version}`);
  const kindText = lane.kind === 'adopted' ? '接管'
    : (lane.kind === 'clone' ? `副本（源 ${lane.clonedFrom || '?'}）` : '');
  setText(u.kind, kindText);
  setHidden(u.kind, !kindText);

  setText(u.badge, lane.running ? `● ${lane.running}/${lane.total} 运行中` : '○ 已停止');
  setClass(u.badge, 'ok', lane.running > 0);

  let expanded = S.expand.get(lane.name);
  if (expanded === undefined) expanded = lane.running > 0;   // 没手动动过：有在跑的就展开
  card.dataset.expanded = expanded ? '1' : '0';
  setText(u.toggleText, expanded ? '收起' : '展开');

  setText(u.summary, `PROFILES　${lane.total} 个环境`
    + (lane.running ? ` · ${lane.running} 个运行中` : ''));
  setClass(u.summary, 'has-run', lane.running > 0);
  setHidden(u.verify, lane.kind !== 'clone');

  syncProfRows(card, lane);
}

function buildProfRow(laneName, prof) {
  const row = el('div', 'prof-row');
  row.dataset.prof = prof.name;

  const info = el('div', 'prof-info');
  const dot = el('span', 'dot');
  const name = el('b', 'name', prof.name);
  const meta = el('span', 'meta');
  const tags = el('span', 'tags');
  const url = el('span', 'url');
  url.dataset.act = 'copy-url';           // 长 URL 点一下就复制完整地址
  info.append(dot, name, meta, tags, url);

  // 端口框：留空＝自动挑空闲。用真的 placeholder，不用 tkinter 版那套"塞灰字再清掉"
  const ops = el('div', 'prof-ops');
  const port = el('input', 'input port');
  port.type = 'text';
  port.inputMode = 'numeric';
  port.placeholder = '自动';
  port.dataset.act = 'port';
  port.dataset.lane = laneName;
  port.dataset.prof = prof.name;
  port.size = 6;
  const openBtn = button('btn', '打开', 'open-prof', { lane: laneName, prof: prof.name });
  const stopBtn = button('btn', '停止', 'stop-prof', { lane: laneName, prof: prof.name });
  const logBtn = button('link muted', '日志', 'tail-log', { lane: laneName, prof: prof.name });
  const rmBtn = button('link danger', '移除', 'rm-prof', { lane: laneName, prof: prof.name });
  ops.append(port, openBtn, stopBtn, logBtn, rmBtn);
  row.append(info, ops);

  row._u = { dot, name, meta, tags, url, port, openBtn, stopBtn, rmBtn };
  return row;
}

function updateProfRow(row, laneName, prof) {
  const u = row._u;
  setClass(u.dot, 'ok', prof.running);
  setClass(u.dot, 'off', !prof.running);
  setText(u.dot, prof.running ? '●' : '○');

  const bits = [];
  if (prof.running) {
    bits.push(`PID ${prof.pid}`, `端口 ${prof.livePort}`);
  } else if (prof.prefPort) {
    bits.push(`端口 偏好 ${prof.prefPort}（被占自动改口）`);
  } else {
    bits.push('端口 自动挑空闲');
  }
  setText(u.meta, '　' + bits.join(' · '));

  const tags = [];
  if (prof.isDefault) tags.push('默认');
  if (!prof.onDisk) tags.push('首次启动时自动初始化');
  else if (!prof.registered) tags.push('磁盘上有');
  setText(u.tags, tags.length ? `　（${tags.join('；')}）` : '');

  setText(u.url, prof.url ? shortUrl(prof.url) : '');
  setHidden(u.url, !prof.url);
  u.url.dataset.url = prof.url || '';
  u.url.title = prof.url ? '点击复制完整地址' : '';

  u.openBtn.className = prof.running ? 'btn' : 'btn primary';
  u.stopBtn.className = prof.running ? 'btn danger' : 'btn';
  setHidden(u.rmBtn, !!prof.isDefault);

  // 千万别在用户正打字的时候回写（tkinter 版是靠重建卡片，这里必须显式避开）
  if (!isEditing(u.port)) {
    const value = prof.running && prof.livePort ? String(prof.livePort)
      : (prof.prefPort ? String(prof.prefPort) : '');
    if (u.port.value !== value) u.port.value = value;
  }
}

function syncProfRows(card, lane) {
  let map = profRows.get(lane.name);
  if (!map) { map = new Map(); profRows.set(lane.name, map); }
  const inner = card._u.inner;

  for (const prof of lane.profiles) {
    let row = map.get(prof.name);
    if (!row) {
      row = buildProfRow(lane.name, prof);
      map.set(prof.name, row);
    }
    updateProfRow(row, lane.name, prof);
  }
  // 消失的 profile 先把节点摘掉，再统一排顺序
  const live = new Set(lane.profiles.map((prof) => prof.name));
  for (const [name, row] of [...map]) {
    if (!live.has(name)) {
      row.remove();
      map.delete(name);
    }
  }

  // 尾注常驻在最后（收起时被 0fr 压掉，展开动画里它跟着一起"滑出来"）
  const wanted = lane.profiles.map((prof) => map.get(prof.name));
  if (card._u.note) wanted.push(card._u.note);
  orderNodes(inner, wanted);
}

function renderLanes() {
  const box = $('#lane-cards');
  // 骨架屏只留给第一次：真状态一到就把它们清掉（否则卡片会被排到骨架后面）
  if (!skeletonCleared) {
    skeletonCleared = true;
    box.querySelectorAll('.skel').forEach((node) => node.remove());
  }
  if (!S.lanes.length) {
    let empty = box.querySelector('.empty');
    if (!empty) {
      clear(box);
      empty = el('div', 'empty');
      empty.append(el('h3', null, '还没有任何 lane'));
      empty.append(el('p', null,
        '点右上角「＋ 新建版本」，选一个版本装进来（例如 next / 0.1.7-rc.2）。\n'
        + '装好之后这里会出现卡片：卡片上是这条 lane 的 profiles（环境）列表，\n'
        + '每个 profile 可以单独打开/停止，端口留空＝每次启动自动挑空闲端口。\n\n'
        + '同一条 lane 里可以「＋ 添加 Profile」开出多个环境（各自装各自的插件），\n'
        + '不必为每个环境复制一整套 dsh；插件可以用「插件库」从别的环境搬过来。'));
      empty.classList.add('card');
      laneCards.clear();
      profRows.clear();
    }
    return;
  }
  const empty = box.querySelector('.empty');
  if (empty) empty.remove();

  const nodes = [];
  for (const lane of S.lanes) {
    let card = laneCards.get(lane.name);
    if (!card) {
      card = buildCard(lane);
      laneCards.set(lane.name, card);
    }
    updateCard(card, lane);
    nodes.push(card);
  }
  const live = new Set(S.lanes.map((lane) => lane.name));
  for (const [name, card] of [...laneCards]) {
    if (!live.has(name)) {
      card.remove();
      laneCards.delete(name);
      profRows.delete(name);
    }
  }
  orderNodes(box, nodes);
}

/* ── 未登记实例 / 本机各套安装 ──────────────────────────────────────────── */

function renderExtra() {
  const box = $('#extra');
  // 这一块小、且不常变，按内容签名整体重画就够了（一样的内容连 DOM 都不碰）
  const signature = JSON.stringify([S.foreign, S.installs]);
  if (box.dataset.sig === signature) return;
  box.dataset.sig = signature;
  clear(box);
  if (!S.foreign.length && !S.installs.length) return;

  box.append(el('div', 'list-section', '未登记的实例（不是本启动器启动的）'));

  if (!S.foreign.length) {
    const none = el('p', 'prof-note', '（当前没有别的 DSH 实例在跑）');
    none.classList.add('list-section');
    box.append(none);
  }

  for (const inst of S.foreign) {
    const card = el('article', 'card');
    const head = el('div', 'card-head');
    const title = el('div', 'card-title');
    title.append(el('h3', null, `端口 ${inst.port}`));
    const flag = el('span', 'pill' + (inst.dshLike ? ' warn' : ''), inst.dshLike ? '● 像是 DSH' : '○ 占用中');
    head.append(title, flag);

    const ops = el('div', 'card-ops');
    const openBtn = button('btn primary', '打开页面', 'open-page', { url: inst.url });
    const killBtn = button('btn danger', '结束进程', 'kill', { pid: String(inst.pid), port: String(inst.port), exe: inst.exe });
    ops.append(openBtn, killBtn);
    head.append(ops);
    card.append(head);

    const info = el('div', 'prof-info');
    info.append(el('span', 'meta', `PID ${inst.pid}　进程 ${inst.exe}`));
    const url = el('span', 'url', inst.url + '   ⧉ 点击复制');
    url.dataset.act = 'copy-url';
    url.dataset.url = inst.url;
    info.append(url);
    card.append(info);

    const note = el('p', 'prof-note', `启动于 ${inst.started || '?'}　·　非本启动器管理`);
    card.append(note);
    box.append(card);
  }

  if (S.installs.length) {
    const card = el('article', 'card');
    const head = el('div', 'card-head');
    const title = el('div', 'card-title');
    title.append(el('h3', null, '本机各套安装（你平时在用的那份在这里）'));
    const ops = el('div', 'card-ops');
    ops.append(button('btn', '打开根目录', 'open-root'));
    head.append(title, ops);
    card.append(head);

    for (const inst of S.installs) {
      const row = el('div', 'install-row');
      const who = el('div', 'who');
      who.append(el('div', 'line1', `${inst.source}　·　${inst.version}　·　写入 ${inst.installedAt || '?'}`));
      who.append(el('div', 'line2', inst.path));
      const add = button('btn', '加入 lane 列表', 'adopt', { path: inst.path });
      row.append(who, add);
      card.append(row);
    }
    card.append(el('p', 'prof-note',
      '「加入 lane 列表」＝用它现有的 DSH_HOME（~/.dsh）注册成一条 lane，之后就能在这里打开/停止它。'));
    box.append(card);
  }
}

/* ── 渲染调度 ───────────────────────────────────────────────────────────── */

let dirty = false;

function markDirty() {
  if (dirty) return;
  dirty = true;
  requestAnimationFrame(() => {
    dirty = false;
    if (!S.ready) return;
    renderHeader();
    renderLanes();
    renderExtra();
    renderStatus();
  });
}

function applyState(state) {
  if (!state || !state.ready) return;      // 快照还没算完：继续显示骨架屏
  S.ready = true;
  S.root = state.root || '';
  S.rootWritable = state.rootWritable || { ok: true, detail: '' };
  S.lanes = state.lanes || [];
  S.laneMap = new Map(S.lanes.map((lane) => [lane.name, lane]));
  S.foreign = state.foreign || [];
  S.installs = state.installs || [];
  S.counts = state.counts || S.counts;
  S.busy = !!state.busy;
  S.busyTitle = state.busyTitle || '';
  if (state.registry) S.registry = state.registry;
  if (state.remote && state.remote.text) S.remote = state.remote;
  if (state.theme) setTheme(state.theme, false);
  if (state.ui) lastGeometry = state.ui;
  hideBoot();
  // 给自动化验收一个"首屏已经渲染完"的标记（也方便人工在控制台里确认）
  document.documentElement.dataset.ready = '1';
  markDirty();
}

function applyRemote(remote) {
  S.remote = { text: remote.text || '', ok: !!remote.ok, pending: !!remote.pending };
  renderHeader();
}

/* ── 交互（事件委托：卡片重建也不会丢监听） ─────────────────────────────── */

function laneOf(node) {
  const card = node.closest('.card');
  const name = card && card.dataset.lane;
  return name ? S.laneMap.get(name) : null;
}

function boundPort(node) {
  const row = node.closest('.prof-row');
  const input = row && row.querySelector('.port');
  return input ? input.value : '';
}

$('#lane-scroll').addEventListener('click', (event) => {
  const target = event.target.closest('[data-act]');
  if (!target) return;
  const kind = target.dataset.act;
  const laneName = target.dataset.lane;

  if (kind === 'port') return;                // 端口框的点击不做事

  // 卡片上的那批入口（第二批迁过来的）：先取到 lane，再开对应的窗口
  const laneDialogs = {
    'add-profile': dlgAddProfile,
    'dlg-credentials': dlgCredentials,
    'dlg-providers': dlgProviders,
    'dlg-files': dlgFiles,
    'dlg-plugins': dlgPlugins,
    'dlg-market': dlgMarket,
    'dlg-chat': dlgChat,
    'dlg-cfg': dlgCfg,
    'dlg-clone': dlgClone,
    'dlg-upgrade': dlgUpgrade,
    'dlg-delete': dlgDelete,
  };
  if (kind in laneDialogs) {
    const lane = laneOf(target);
    if (!lane) {
      toast('找不到这条 lane 了，点「刷新」重看', 'err');
      return;
    }
    laneDialogs[kind](lane.name);
    return;
  }
  if (kind === 'verify') {
    const lane = laneOf(target);
    if (lane) act(`核对隔离：${lane.name}`, 'verify', { lane: lane.name });
    return;
  }

  if (kind === 'open-lane') {
    const lane = laneOf(target);
    if (lane) act(`打开 ${lane.name}/${lane.defaultProfile}`, 'open',
      { lane: lane.name, profile: lane.defaultProfile });
    return;
  }
  if (kind === 'stop-lane') {
    if (laneName) act(`停止 ${laneName}`, 'stop', { lane: laneName });
    return;
  }
  if (kind === 'toggle') {
    const card = target.closest('.card');
    const lane = laneOf(target);
    if (!card || !lane) return;
    // 展开/收起只改一个属性：行节点一直都在，由 CSS 的 grid-template-rows 过渡高度。
    // （tkinter 版得手工生成 12 个互不相同的高度、每帧强制重排版才勉强算"动画"。）
    const now = card.dataset.expanded !== '1';
    S.expand.set(lane.name, now);
    card.dataset.expanded = now ? '1' : '0';
    setText(card._u.toggleText, now ? '收起' : '展开');
    return;
  }
  if (kind === 'open-prof') {
    act(`打开 ${laneName}/${target.dataset.prof}`, 'open',
      { lane: laneName, profile: target.dataset.prof, port: boundPort(target) });
    return;
  }
  if (kind === 'stop-prof') {
    act(`停止 ${laneName}/${target.dataset.prof}`, 'stop',
      { lane: laneName, profile: target.dataset.prof });
    return;
  }
  if (kind === 'tail-log') {
    act('读取日志', 'tail_log', { lane: laneName, profile: target.dataset.prof });
    return;
  }
  if (kind === 'rm-prof') {
    const prof = target.dataset.prof;
    confirmBox({
      head: '移除 Profile',
      text: `移除 profile「${laneName}/${prof}」？\n\n`
        + '· 会删掉它在磁盘上的目录（HOME\\profiles\\<名>），里面装的插件无法找回\n'
        + '· 会话与凭据在 HOME 级，不受影响\n'
        + '· 正在运行的话这一步会被拒绝（先停止它）',
      ok: '移除',
      danger: true,
    }).then((yes) => {
      if (yes) act(`移除 ${laneName}/${prof}`, 'profile_rm', { lane: laneName, name: prof });
    });
    return;
  }
  if (kind === 'copy-url') {
    if (target.dataset.url) copyText(target.dataset.url);
    return;
  }
  if (kind === 'open-page') {
    act('打开页面', 'open_url', { url: target.dataset.url });
    return;
  }
  if (kind === 'open-root') {
    act('打开根目录', 'open_path', { which: 'root' });
    return;
  }
  if (kind === 'kill') {
    const pid = target.dataset.pid;
    const port = target.dataset.port;
    const exe = target.dataset.exe || '?';
    confirmBox({
      head: '结束进程',
      text: `确定结束端口 ${port} 上的进程吗？\n\nPID ${pid}　进程 ${exe}\n`
        + '这不是本启动器启动的实例——如果它正是你此刻在用的 DSH，当前页面会立刻断开。',
      ok: '结束进程',
      danger: true,
    }).then((yes) => { if (yes) act(`结束 PID ${pid}`, 'kill', { pid: Number(pid), port: Number(port) }); });
    return;
  }
  if (kind === 'adopt') {
    adoptFlow(target.dataset.path);
  }
});

// 双击卡片空白处＝打开默认 profile（和 tkinter 版一致）
$('#lane-scroll').addEventListener('dblclick', (event) => {
  if (event.target.closest('button, input, a, .url')) return;
  const lane = laneOf(event.target);
  if (!lane) return;
  act(`打开 ${lane.name}/${lane.defaultProfile}`, 'open',
    { lane: lane.name, profile: lane.defaultProfile });
});

// 端口框里按回车＝打开（tkinter 版没有这个，纯属顺手）
$('#lane-scroll').addEventListener('keydown', (event) => {
  if (event.key !== 'Enter') return;
  const input = event.target.closest('.port');
  if (!input) return;
  const row = input.closest('.prof-row');
  const laneName = input.dataset.lane;
  const prof = input.dataset.prof;
  if (row && laneName && prof) {
    act(`打开 ${laneName}/${prof}`, 'open', { lane: laneName, profile: prof, port: input.value });
  }
});

function copyText(text) {
  if (!text) return;
  navigator.clipboard.writeText(text).then(
    () => toast('已复制到剪贴板', 'ok'),
    (err) => toast('复制失败：' + err.message, 'err'),
  );
}

async function adoptFlow(path) {
  const plan = await act('读取安装信息', 'adopt_plan', { path });
  if (!plan) return;
  if (plan.alreadyRegistered) {
    toast(`这份安装已经注册为 lane「${plan.alreadyRegistered}」了`, 'err', 4000);
    pushLocal(`[!!] 这份安装已经注册为 lane「${plan.alreadyRegistered}」了`, 'warn');
    return;
  }
  const yes = await confirmBox({
    head: '加入 lane 列表',
    text: '把这份安装注册成一条 lane？',
    ok: '加入',
    build: (body) => {
      const dl = el('dl', 'kv');
      const rows = [
        ['来源', plan.source],
        ['版本', plan.version],
        ['安装目录', plan.path],
        ['lane 名', plan.lane],
        ['端口', 'web profile 偏好 3080（被占用会自动改口）'],
        ['DSH_HOME', plan.home],
      ];
      for (const [key, value] of rows) {
        dl.append(el('dt', null, key), el('dd', null, value));
      }
      body.append(dl);
      body.append(el('p', 'warn', `（${plan.note}）`));
      body.append(el('p', null,
        '注册后就能在本窗口「打开 / 停止」它，和你原来的启动方式是同一套环境；'
        + 'HOME 里现成的 profiles（web / desktop / …）会显示在卡片上，可分别启动。'
        + (plan.extra ? '\n\n' + plan.extra : '')));
    },
  });
  if (yes) act(`加入 lane：${plan.lane}`, 'adopt', { path });
}

/* ══════════════════════════════════════════════════════════════════════════
   第二批：新建版本 / 添加 Profile / 插件库 / 凭据 / 插件市场 / 备份对话 /
           复制 / 升级 / 删除 （外加卡片上的「核对隔离」）
   ----------------------------------------------------------------------------
   每个窗口都照 dsh_lanes_gui.py 里同名对话框的规矩来：**先把"会发生什么"算出来摆给
   用户看（只读接口），确认了才动手（后端任务，进度进右边的实时日志）**。
   差别只在画法：那边是 Toplevel + tk 控件 + messagebox，这里是 <dialog> + 原生表单。
   ═════════════════════════════════════════════════════════════════════════ */

const LANE_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$/;
const PROFILE_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const REGISTRY_OPTIONS = [
  { value: 'auto', label: '自动（先官方，缺东西再换镜像）' },
  { value: 'official', label: '官方源 npmjs.org' },
  { value: 'mirror', label: '镜像源 npmmirror.com（国内快）' },
];
const MODE_LABEL = { install: '重新安装（点名到同版本）', copy: '离线直拷' };

let seqCounter = 0;
const uid = (prefix) => `${prefix}-${++seqCounter}`;

/* ── 窗口底座 ───────────────────────────────────────────────────────────── */

const openDialogs = new Set();

function spinnerRow(text) {
  const box = el('div', 'dlg-load');
  box.append(el('span', 'spinner small'), el('span', null, text || '读取中…'));
  return box;
}

/**
 * 开一个表单窗口，返回控制器。
 *
 * 为什么不复用 index.html 里的 #modal：那个是"确认 / 取消"两按钮专用。
 * 第二批的窗口要异步加载、要装表单与清单、底部按钮还得变，而且「删除」窗口里
 * 还要再弹一次确认 —— 每次新建一个 <dialog>，靠浏览器自己的 top layer 天然可堆叠。
 */
function openDialog(opts) {
  const node = el('dialog', 'modal dlg');
  node.dataset.size = opts.size || 'md';
  const head = el('div', 'modal-head', opts.head || '');
  const body = el('div', 'modal-body');
  const foot = el('div', 'modal-foot');
  node.append(head, body, foot);
  document.body.append(node);

  const ctl = {
    node, head, body, foot,
    busy: false,
    closed: false,
    jobDone: null,
    setHead(text) { setText(head, text); },
    setBusy(on) { ctl.busy = !!on; ctl.refreshFoot(); },
    setFoot(items) {
      clear(foot);
      for (const item of items || []) {
        const cls = item.kind === 'primary' ? 'btn primary'
          : item.kind === 'danger' ? 'btn danger'
            : item.kind === 'link' ? 'link' : 'btn';
        const btn = button(cls, item.text);
        btn._rule = item;
        btn.addEventListener('click', () => {
          if (btn.disabled) return;
          item.act(ctl, btn);
        });
        foot.append(btn);
      }
      ctl.refreshFoot();
    },
    /** 底部按钮的可用性每帧重算：忙碌中一律按不动（后端也会回 409） */
    refreshFoot() {
      for (const btn of foot.children) {
        const rule = btn._rule;
        if (!rule) continue;
        btn.disabled = (rule.enabled ? !rule.enabled(ctl) : false) || !!ctl.busy;
      }
    },
    close() { if (!ctl.closed) node.close(); },
  };

  node.addEventListener('close', () => {
    ctl.closed = true;
    ctl.jobDone = null;             // 窗口都关了，任务结束就别再往这儿回写
    openDialogs.delete(ctl);
    // 等关闭过渡走完再摘节点：立刻 remove 会把那个淡出动画切掉
    setTimeout(() => node.remove(), 300);
    if (opts.onClose) opts.onClose(ctl);
  });

  ctl.setFoot(opts.foot);
  openDialogs.add(ctl);
  node.showModal();
  raiseToasts();            // 窗口进来了，把吐司层重新举到它上面
  if (opts.build) opts.build(ctl);
  return ctl;
}

/** 任务结束时（SSE job_done）挨个问一遍开着的窗口："是不是你的任务？"
 *  若没有任何窗口认领（任务是在对话框关闭后于后台跑的，比如备份/恢复），
 *  就在主页面弹一个全局结果提示。 */
function notifyJobDone(data) {
  let handled = false;
  for (const ctl of [...openDialogs]) {
    if (ctl.jobDone) {
      ctl.jobDone(data);
      handled = true;
    }
  }
  if (!handled) {
    toast(`${data.title}${data.ok ? ' 已完成' : ' 失败'}`, data.ok ? 'ok' : 'err', 4600);
    if (!data.ok) pushLocal(`[XX] ${data.title}失败，详情看右侧实时日志`, 'err');
  }
}

/** 「读数据 → 建内容」的套路。读失败就在窗口里说清楚，而不是把窗口关掉。 */
async function loadDialog(ctl, label, action, payload, builder) {
  try {
    const data = await api(action, payload);
    if (ctl.closed) return null;
    clear(ctl.body);
    builder(data);
    return data;
  } catch (err) {
    if (ctl.closed) return null;
    clear(ctl.body);
    ctl.body.append(el('p', 'field-note err', `${label}失败：${err.message}`));
    pushLocal(`[XX] ${label}失败：${err.message}`, 'err');
    return null;
  }
}

/**
 * 起一个后端任务。
 * · 默认：任务起起来了就关窗（进度去右边的日志面板看）
 * · opts.reload：窗口留着，等 job_done 到了自己重读一遍（备份对话 / 插件库 这种要刷清单的）
 * · 起不来（忙 / 参数错）：窗口留着，让人改完再试
 */
async function startJob(ctl, label, action, payload, opts) {
  if (ctl && ctl.busy) return false;
  if (ctl) ctl.setBusy(true);
  const res = await act(label, action, payload);
  if (!res) {
    if (ctl) ctl.setBusy(false);
    return false;
  }
  toast(`${label}已开始，进度看右边的实时日志`, 'ok', 3600);
  if (ctl && opts && opts.reload) {
    ctl.jobDone = (data) => {
      ctl.jobDone = null;
      ctl.setBusy(false);
      if (!data.ok) toast(`${label}失败了，看日志`, 'err', 4200);
      opts.reload(ctl);
    };
    return true;
  }
  if (ctl) ctl.close();
  return true;
}

/* ── 表单小件 ───────────────────────────────────────────────────────────── */

function field(label, node, note) {
  const box = el('div', 'field');
  box.append(el('div', 'field-label', label), node);
  if (note) box.append(el('p', 'field-note', note));
  return box;
}

function textInput(value, placeholder) {
  const node = el('input', 'text-input');
  node.type = 'text';
  node.value = value || '';
  if (placeholder) node.placeholder = placeholder;
  return node;
}

/** 下拉框。options 可以是字符串数组，也可以是 {value,label}。 */
function selectInput(options, value) {
  const node = el('select', 'select');
  optionList(node, options, value);
  return node;
}

function optionList(node, options, value) {
  clear(node);
  for (const opt of options || []) {
    const item = el('option', null, typeof opt === 'string' ? opt : opt.label);
    item.value = typeof opt === 'string' ? opt : opt.value;
    node.append(item);
  }
  const values = [...node.children].map((o) => o.value);
  node.value = value != null && values.includes(String(value))
    ? String(value) : (values[0] || '');
}

/** 可手输的下拉（HTML 里是 input + datalist）：profile 名要允许现场起一个新的。
    返回的是**包一层的外壳**，取值用 `wrap.input.value` —— datalist 必须和 input 待在
    同一个容器里才会生效（`input.after()` 在节点还没进 DOM 时是空操作）。 */
function comboInput(options, value, placeholder) {
  const wrap = el('div', 'combo');
  const node = textInput(value || '', placeholder);
  const list = el('datalist');
  const id = uid('dl');
  list.id = id;
  node.setAttribute('list', id);
  wrap.append(node, list);
  wrap.input = node;
  setComboOptions(wrap, options);
  return wrap;
}

function setComboOptions(wrap, options) {
  const list = wrap.querySelector('datalist');
  if (!list) return;
  clear(list);
  for (const opt of options || []) {
    const item = el('option');
    item.value = typeof opt === 'string' ? opt : opt.value;
    list.append(item);
  }
}

function radioGroup(name, options, value, onchange) {
  const node = el('div', 'radio-group');
  const inputs = new Map();
  const group = uid(name);
  for (const opt of options) {
    const lab = el('label', 'radio-row');
    const input = el('input');
    input.type = 'radio';
    input.name = group;
    input.value = opt.value;
    lab.append(input);
    const main = el('div', 'pick-main');
    main.append(el('div', null, opt.label));
    if (opt.note) main.append(el('div', 'field-note', opt.note));
    lab.append(main);
    inputs.set(opt.value, input);
    node.append(lab);
  }
  const api = {
    node,
    get() {
      for (const [key, input] of inputs) if (input.checked) return key;
      return value;
    },
    set(next) { const input = inputs.get(next); if (input) input.checked = true; },
    disable(key) { const input = inputs.get(key); if (input) input.disabled = true; },
    onChange(fn) { for (const input of inputs.values()) input.addEventListener('change', fn); },
  };
  api.set(value != null ? value : (options[0] && options[0].value));
  if (onchange) api.onChange(onchange);
  return api;
}

function checkInput(label, checked, cls) {
  const lab = el('label', 'check' + (cls ? ' ' + cls : ''));
  const input = el('input');
  input.type = 'checkbox';
  input.checked = !!checked;
  lab.append(input, el('span', null, label));
  return {
    node: lab, input,
    get: () => input.checked,
    set: (on) => { input.checked = !!on; },
    onChange: (fn) => input.addEventListener('change', fn),
  };
}

function registryRow(choice) {
  const box = el('div', 'field');
  box.append(el('div', 'field-label', '下载源'));
  const group = radioGroup('registry', REGISTRY_OPTIONS, choice || 'auto');
  box.append(group.node);
  const remember = checkInput('记住这个选择（写进 lanes.json，以后新建 / 升级默认用它）', false, 'tiny');
  box.append(remember.node);
  const raw = String((S.registry && S.registry.value) || '');
  if (raw && !['auto', 'official', 'mirror'].includes(raw.toLowerCase())) {
    box.append(el('p', 'field-note warn',
      `（lanes.json 里现在写的是自定义地址 ${raw}；不想换掉就别勾「记住」）`));
  }
  return { node: box, get: () => group.get(), remember: () => remember.get() };
}

function noteLine(text, cls) {
  return el('p', 'dlg-note' + (cls ? ' ' + cls : ''), text);
}

/* ── ① 新建版本 ─────────────────────────────────────────────────────────── */

/** 版本号 → 默认 lane 名（照经典界面的 base_name） */
const NAME_TAGS = { '0.1.7-rc.2': 'next', '0.1.5-rc.3': 'stable' };
function baseName(ver) {
  return NAME_TAGS[ver]
    || ('lane-' + ver.replace(/[^A-Za-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 12));
}

function dlgCreate() {
  const reg = registryRow(S.registry && S.registry.choice);
  const nameBox = textInput('', '例如 next / lane-017rc2');
  const verBox = textInput('', '在上面挑一版，或手打 0.1.7-rc.2 / latest');
  const hint = el('p', 'field-note', '正在查询 npm 上可用的版本…');
  const listBox = el('div', 'opt-list');

  const ctl = openDialog({
    head: '新建一条 lane',
    size: 'lg',
    build: (c) => {
      c.body.append(el('p', 'dlg-lead',
        '会把该版本的 DSH 装进独立目录（不覆盖你正在用的版本），并分配独立 DSH_HOME。'
        + '端口不固定：每次打开时实时挑空闲端口。'));
      c.body.append(noteLine(
        'API key：新 HOME 是空的。建好之后在卡片上点「凭据…」，从已有版本按条目勾选复制'
        + '（也可以启动后直接在 DSH 里填）。', 'info'));
      c.body.append(reg.node);
      const form = el('div', 'form');
      form.append(field('名称', nameBox), field('版本', verBox));
      c.body.append(form);
      c.body.append(el('div', 'field-label', 'npm 上的版本（点一下填进上面的「版本」）'));
      c.body.append(listBox, hint);
    },
    foot: [
      { text: '重新查询版本', act: () => load() },
      { text: '取消', act: (c) => c.close() },
      { text: '开始安装', kind: 'primary', act: (c) => doCreate(c) },
    ],
  });

  function renderList(data) {
    clear(listBox);
    const rows = data.items || [];
    if (!rows.length) {
      listBox.append(el('div', 'opt-empty', '这个源上没有查到可用版本。'));
      return;
    }
    for (const item of rows) {
      const row = el('div', 'opt-row');
      row.append(el('span', null, item.v));
      if (item.tags.length) row.append(el('span', 'tag', `(${item.tags.join(',')})`));
      if (item.marks.length) row.append(el('span', 'mark', '← ' + item.marks.join('、')));
      row.addEventListener('click', () => {
        verBox.value = item.v;
        if (!nameBox.value.trim()) nameBox.value = baseName(item.v);
        for (const other of listBox.children) other.removeAttribute('aria-selected');
        row.setAttribute('aria-selected', 'true');
      });
      listBox.append(row);
    }
    // 默认落在最新一版上（还没填过名字就顺手填一个）
    const first = listBox.firstElementChild;
    if (first) first.setAttribute('aria-selected', 'true');
    if (!verBox.value.trim() && rows[0]) {
      verBox.value = rows[0].v;
      if (!nameBox.value.trim()) nameBox.value = baseName(rows[0].v);
    }
    const tags = Object.entries(data.tags || {}).sort()
      .map(([key, val]) => `${key}=${val}`).join('　');
    hint.className = 'field-note';
    setText(hint, `来源 ${data.label}（${data.reg}）｜共 ${data.total} 个版本${tags ? '｜' + tags : ''}`);
  }

  async function load() {
    hint.className = 'field-note';
    setText(hint, '正在查询 npm 上可用的版本…');
    clear(listBox);
    listBox.append(el('div', 'opt-empty', '读取中…'));
    try {
      const data = await api('registry_versions', { registry: reg.get() });
      if (ctl.closed) return;
      if (!data.ok) throw new Error(data.error || '查询失败');
      renderList(data);
    } catch (err) {
      if (ctl.closed) return;
      clear(listBox);
      listBox.append(el('div', 'opt-empty', '没读到版本 —— 可以直接手输版本号。'));
      hint.className = 'field-note warn';
      setText(hint, `查询失败：${err.message}`);
    }
  }

  async function doCreate(c) {
    const lane = nameBox.value.trim();
    const version = (verBox.value.trim().split(/\s+/)[0] || '');
    if (!LANE_NAME_RE.test(lane)) {
      toast('名称只能用字母/数字开头，含字母 数字 . _ -（最长 32 字符）', 'err', 4200);
      nameBox.focus();
      return;
    }
    if (!version) {
      toast('请选择或输入一个版本号（如 0.1.7-rc.2 或 next）', 'err', 4200);
      verBox.focus();
      return;
    }
    if (S.laneMap.has(lane)) {
      const yes = await confirmBox({
        head: 'lane 已存在',
        text: `lane「${lane}」已存在，要覆盖它的版本吗？`,
        ok: '覆盖',
        danger: true,
      });
      if (!yes || c.closed) return;
    }
    startJob(c, `新建 ${lane}（${version}）`, 'create',
      { lane, version, registry: reg.get(), remember: reg.remember() });
  }

  load();
  return ctl;
}

/* ── ② 添加 Profile ─────────────────────────────────────────────────────── */

function dlgAddProfile(lane) {
  const ctl = openDialog({
    head: `给 lane「${lane}」添加一个环境`,
    size: 'md',
    build: (c) => {
      c.body.append(el('p', 'dlg-lead',
        'profile＝同一套 dsh（同版本、同 HOME）里的一个环境：会话 / 凭据 / 设置共享，'
        + '插件组合与补丁层各自独立 —— 一个版本就能开出多环境，不必复制多套 dsh。'));
      c.body.append(spinnerRow('正在读取现有 profile…'));
    },
    foot: [{ text: '取消', act: (c) => c.close() }],
  });
  loadDialog(ctl, '读取 profile 列表', 'lane_profiles', { lane },
    (data) => buildAddProfile(ctl, lane, data));
  return ctl;
}

function buildAddProfile(ctl, lane, data) {
  const onDisk = data.profiles.filter((p) => p.onDisk).map((p) => p.name);
  const srcOptions = onDisk.length ? onDisk : [data.default];
  const nameBox = textInput('', '例如 test');
  const hint = el('p', 'field-note');
  const mode = radioGroup('pmode', [
    { value: 'copy', label: '复制现有 profile（连同已装插件、补丁层一起带走）' },
    { value: 'clean', label: '干净环境（只有官方 web 组合，插件之后自己装）' },
  ], onDisk.length ? 'copy' : 'clean');
  const srcSel = selectInput(srcOptions,
    srcOptions.includes(data.default) ? data.default : srcOptions[0]);
  const srcRow = field('复制自', srcSel);
  if (!onDisk.length) {
    // 磁盘上还没有任何已初始化的 profile：复制无从谈起，直接把那条选项按住
    mode.disable('copy');
    srcRow.append(el('p', 'field-note', '（现在磁盘上还没有已初始化的 profile，只能建干净环境）'));
  }

  const form = el('div', 'form');
  form.append(field('名称', nameBox), mode.node, srcRow);
  ctl.body.append(form, hint);

  function update() {
    const name = nameBox.value.trim();
    srcRow.style.display = mode.get() === 'copy' ? '' : 'none';
    if (!name) {
      hint.className = 'field-note';
      setText(hint, '名称只能用字母/数字开头，含字母 数字 . _ -');
      return;
    }
    const bad = !PROFILE_NAME_RE.test(name);
    hint.className = bad ? 'field-note err' : 'field-note';
    setText(hint, bad
      ? 'profile 名不合法：要字母/数字开头，只含字母 数字 . _ -'
      : `将以 ${lane}/${name} 启动：　py dsh_lanes.py open ${lane}/${name}`);
  }
  nameBox.addEventListener('input', update);
  mode.onChange(update);
  update();

  ctl.setFoot([
    { text: '取消', act: (c) => c.close() },
    {
      text: '添加',
      kind: 'primary',
      act: (c) => {
        const name = nameBox.value.trim();
        if (!PROFILE_NAME_RE.test(name)) {
          toast('profile 名不合法：要字母/数字开头，只含字母 数字 . _ -', 'err', 4200);
          nameBox.focus();
          return;
        }
        const from = mode.get() === 'copy' ? srcSel.value : '';
        if (from && from === name) {
          toast('源 profile 和新名字不能相同', 'err', 4200);
          return;
        }
        startJob(c, `添加 profile ${lane}/${name}`, 'profile_add',
          { lane, name, fromProfile: from });
      },
    },
  ]);
  nameBox.focus();
}

/* ── ③ 凭据（按条目复制 API key） ───────────────────────────────────────── */

function dlgCredentials(lane) {
  const ctl = openDialog({
    head: '凭据 · 按条目复制 API key',
    size: 'lg',
    build: (c) => { c.body.append(spinnerRow('正在读取凭据条目…')); },
    foot: [{ text: '关闭', act: (c) => c.close() }],
  });
  loadDialog(ctl, '读取凭据', 'credentials', { lane }, (data) => buildCredentials(ctl, lane, data));
  return ctl;
}

function buildCredentials(ctl, lane, data) {
  const lanes = data.lanes && data.lanes.length ? data.lanes : [lane];
  const laneSel = selectInput(lanes, lane);
  const info = el('p', 'field-note');
  const listBox = el('div', 'pick-list');
  const state = { items: [] };

  ctl.body.append(el('p', 'dlg-lead',
    '每个 HOME 的 .credentials.yaml 里有一个 refs 段：一条 = 一个提供商 / 插件的 key 引用。'
    + '这里能看每条是谁在用（dsh 里设的显示名）、点了才取明文值、直接改 / 删 / 新增，'
    + '也能从别的 lane 重新导入（修坏掉的文件就走这条）。'));
  ctl.body.append(noteLine(
    '改之前会先备份成 .credentials.yaml.bak；写后用 DSH 自己的解析器校验，不过会自动回滚。', 'info'));
  ctl.body.append(el('div', 'plug-head', el('span', 'field-label', 'LANE'), laneSel));
  ctl.body.append(el('div', 'field-label', '条目'), listBox, info);

  function render(items) {
    clear(listBox);
    state.items = items || [];
    setText(info, `共 ${state.items.length} 条`);
    if (!state.items.length) {
      listBox.append(el('p', 'field-note',
        '这个 HOME 还没有凭据条目。可以在下面新增，或从别的 lane 导入。'));
      return;
    }
    for (const item of state.items) {
      const row = el('div', 'pick-row');
      const main = el('div', 'pick-main');
      const title = el('div', 'pick-title');
      title.append(el('b', null, item.display || item.name));
      title.append(el('span', 'ver', item.name));
      const providers = item.providers || [];
      if (providers.length) title.append(el('span', 'pill tag', providers.join('、')));
      main.append(title);
      const holder = el('div', 'pick-sub');
      main.append(holder);
      const editBtn = button('btn small', '显示 / 编辑');
      editBtn.addEventListener('click', () => toggleEdit(holder, item));
      const delBtn = button('btn small danger', '删除');
      delBtn.addEventListener('click', async () => {
        const yes = await confirmBox({
          head: '删除凭据条目',
          text: `删掉「${item.name}」？\n\n引用它的插件 / 服务商会失去密钥，可能要重新填。`,
          ok: '删除',
        });
        if (!yes || ctl.closed) return;
        try {
          const res = await api('credential_del', { lane: laneSel.value, names: [item.name] });
          if (!res.ok) { toast(res.error || '删除失败', 'err', 5000); return; }
          toast(`已删除 ${item.name}`, 'ok', 3000);
          await load();
        } catch (err) { toast('删除失败：' + err.message, 'err', 5000); }
      });
      const ops = el('div', 'pick-ops');
      ops.append(editBtn, delBtn);
      row.append(main, ops);
      listBox.append(row);
    }
  }

  async function toggleEdit(holder, item) {
    if (holder.dataset.open === '1') { holder.dataset.open = '0'; clear(holder); return; }
    clear(holder);
    holder.dataset.open = '1';
    holder.append(spinnerRow('正在取明文值…'));
    let value = '';
    try {
      const res = await api('credential_get', { lane: laneSel.value, name: item.name });
      if (ctl.closed) return;
      if (!res.ok) {
        clear(holder);
        holder.append(el('div', 'field-note err', res.error || '取不到值'));
        return;
      }
      value = res.value || '';
    } catch (err) {
      clear(holder);
      holder.append(el('div', 'field-note err', '取不到值：' + err.message));
      return;
    }
    clear(holder);
    const input = textInput(value, '（空）');
    input.classList.add('grow');
    const save = button('btn small primary', '保存');
    save.addEventListener('click', async () => {
      try {
        const res = await api('credential_set',
          { lane: laneSel.value, name: item.name, value: input.value });
        if (!res.ok) { toast(res.error || '保存失败', 'err', 6000); return; }
        toast(`已保存 ${item.name}`, 'ok', 3000);
        holder.dataset.open = '0';
        clear(holder);
      } catch (err) { toast('保存失败：' + err.message, 'err', 6000); }
    });
    const cancel = button('btn small', '收起');
    cancel.addEventListener('click', () => { holder.dataset.open = '0'; clear(holder); });
    const ops = el('div', 'pick-ops');
    ops.append(save, cancel);
    holder.append(input, ops);
  }

  async function load() {
    clear(listBox);
    listBox.append(spinnerRow('正在读取…'));
    updateCopySources(lanes, laneSel.value);
    try {
      const res = await api('credentials', { lane: laneSel.value });
      if (ctl.closed) return;
      render(res.items || (res.keys || []).map((k) => ({ name: k })));
    } catch (err) {
      if (ctl.closed) return;
      clear(listBox);
      listBox.append(el('p', 'field-note err', '读取失败：' + err.message));
      setText(info, '');
    }
  }

  laneSel.addEventListener('change', load);

  const newName = textInput('', 'MY_API_KEY');
  const newValue = textInput('', 'sk-...');
  const addBtn = button('btn small primary', '新增 / 覆盖');
  addBtn.addEventListener('click', async () => {
    const name = (newName.value || '').trim();
    if (!name) { toast('先填条目名', 'err', 3000); return; }
    try {
      const res = await api('credential_set',
        { lane: laneSel.value, name, value: newValue.value || '' });
      if (!res.ok) { toast(res.error || '写入失败', 'err', 6000); return; }
      toast(`已写入 ${name}`, 'ok', 3000);
      newName.value = '';
      newValue.value = '';
      await load();
    } catch (err) { toast('写入失败：' + err.message, 'err', 6000); }
  });
  const addRow = el('div', 'two-col');
  addRow.append(el('span', 'k', '条目名'), newName, el('span', 'k', '值'), newValue);
  ctl.body.append(el('div', 'field-label', '新增 / 覆盖一个条目'), addRow,
    el('div', 'pick-bar', addBtn));

  const srcSel = selectInput(lanes, laneSel.value);
  const impBtn = button('btn small', '从这条 lane 导入');
  impBtn.addEventListener('click', async () => {
    if (srcSel.value === laneSel.value) { toast('源和目标不能是同一条 lane', 'err', 3600); return; }
    const yes = await confirmBox({
      head: '导入凭据',
      text: `把「${srcSel.value}」的全部凭据条目合并进「${laneSel.value}」？\n\n`
        + '同名的覆盖、其余保留；改之前先备份。',
      ok: '导入',
    });
    if (!yes || ctl.closed) return;
    try {
      const res = await api('credential_import', { lane: laneSel.value, source: srcSel.value });
      if (!res.ok) { toast(res.error || '导入失败', 'err', 7000); return; }
      const kept = (res.kept || []).length;
      toast(`导入了 ${(res.imported || []).length} 条`
        + (res.rebuilt ? '（refs 段整段重铺）' : '')
        + (kept ? `，保留目标里原有的 ${kept} 条` : ''), 'ok', 4200);
      await load();
    } catch (err) { toast('导入失败：' + err.message, 'err', 7000); }
  });
  ctl.body.append(el('div', 'field-label', '修复 / 迁移（从别的 lane 导入）'),
    el('div', 'plug-head', srcSel, impBtn),
    noteLine('修坏掉的凭据文件就走这条：从一份好的 HOME 里重新导入。', 'muted'));

  const copyInfo = el('p', 'field-note');
  const copySrc = selectInput(lanes, lane === lanes[0] && lanes[1] ? lanes[1] : lane);
  const copyDst = selectInput(lanes, lane);
  const copyBtn = button('btn small primary', '复制全部条目');
  copyBtn.addEventListener('click', async () => {
    const from = copySrc.value;
    const to = copyDst.value;
    if (from === to) { toast('源和目标是同一条 lane', 'err', 3600); return; }
    const yes = await confirmBox({
      head: '复制凭据',
      text: `把「${from}」的全部凭据条目复制给「${to}」？\n\n同名的覆盖、其余保留。`,
      ok: '复制',
    });
    if (!yes || ctl.closed) return;
    startJob(ctl, `复制凭据 ${from} → ${to}`, 'copy_key',
      { source: from, dest: to, only: '*' });
  });
  function updateCopySources(_lanes, current) {
    // 源下拉默认挑一条 ≠ 当前的 lane
    if (copySrc.value === current) {
      const other = lanes.find((n) => n !== current);
      if (other) copySrc.value = other;
    }
    setText(copyInfo, `当前管理：${current}`);
  }
  const copyRow = el('div', 'two-col');
  copyRow.append(el('span', 'k', '源 lane'), copySrc, el('span', 'k', '目标 lane'), copyDst);
  ctl.body.append(el('div', 'field-label', '整份复制到别的 lane'), copyRow,
    el('div', 'pick-bar', copyBtn, copyInfo));

  ctl.setFoot([{ text: '关闭', act: (c) => c.close() }]);
  load();
}

/* ── ④b 自定义服务商（写 profile 补丁层 → llm-pi-ai） ───────────────────── */

function dlgProviders(lane) {
  const ctl = openDialog({
    head: `自定义服务商 · lane「${lane}」`,
    size: 'lg',
    build: (c) => { c.body.append(spinnerRow('正在读取服务商…')); },
    foot: [{ text: '关闭', act: (c) => c.close() }],
  });
  loadDialog(ctl, '读取自定义服务商', 'provider_list', { lane },
    (data) => buildProviders(ctl, lane, data));
  return ctl;
}

function buildProviders(ctl, lane, data) {
  const profiles = (data.profiles || []).map((p) => ({
    value: p.name, label: p.name + (p.onDisk ? '' : '（未建）'),
  }));
  const profSel = selectInput(profiles.length ? profiles : [core_default_profile(lane)],
    data.profile);
  const apis = data.apis && data.apis.length ? data.apis
    : ['openai-completions', 'openai-responses', 'anthropic-messages'];
  const listBox = el('div', 'pick-list');
  const info = el('p', 'field-note');
  const state = { providers: [] };

  ctl.body.append(el('p', 'dlg-lead',
    '这里列的是这个 profile 补丁层（cordis.patch.yml）里手写的服务商 —— 就是 dsh「设置 → 模型」里'
    + '显示的那些。新增一条会写进 llm-pi-ai 的 providers，并把 API key 存进 .credentials.yaml。'));
  ctl.body.append(noteLine(
    'route 只能小写字母 / 数字 / 中划线；keyRef 默认是 <ROUTE>_API_KEY（和 dsh 自己的规则一致）。',
    'info'));
  ctl.body.append(el('div', 'plug-head', el('span', 'field-label', 'PROFILE'), profSel));
  ctl.body.append(el('div', 'field-label', '已有服务商'), listBox, info);

  function render(providers, error) {
    clear(listBox);
    state.providers = providers || [];
    setText(info, error ? '' : `共 ${state.providers.length} 个`);
    if (error) {
      listBox.append(el('p', 'field-note err', '读取失败：' + error));
      return;
    }
    if (!state.providers.length) {
      listBox.append(el('p', 'field-note', '这个 profile 还没有手写服务商。'));
      return;
    }
    for (const p of state.providers) {
      const row = el('div', 'pick-row');
      const main = el('div', 'pick-main');
      const title = el('div', 'pick-title');
      title.append(el('b', null, p.displayName || p.route));
      title.append(el('span', 'ver', p.route));
      if (p.api) title.append(el('span', 'pill tag', p.api));
      main.append(title);
      const sub = el('div', 'pick-sub');
      const ref = p.apiKeyEnv || defaultKeyRef(p.route);
      sub.append(el('div', null, 'keyRef：' + ref));
      if (p.baseURL) sub.append(el('div', null, 'baseURL：' + p.baseURL));
      if ((p.models || []).length) sub.append(el('div', null, 'models：' + p.models.join('、')));
      main.append(sub);
      row.append(main);
      listBox.append(row);
    }
  }

  async function load() {
    clear(listBox);
    listBox.append(spinnerRow('正在读取…'));
    try {
      const res = await api('provider_list', { lane, profile: profSel.value });
      if (ctl.closed) return;
      render(res.providers, res.error);
    } catch (err) {
      if (!ctl.closed) render([], err.message);
    }
  }
  profSel.addEventListener('change', load);

  const routeIn = textInput('', 'my-gateway');
  const nameIn = textInput('', '（默认跟 route 一样）');
  const apiSel = selectInput(apis, apis[0]);
  const urlIn = textInput('', 'https://…/v1');
  const keyIn = textInput('', 'sk-...（可留空）');
  const modelsIn = textInput('', 'model-a, model-b（逗号分隔）');
  const addGrid = el('div', 'two-col');
  addGrid.append(
    el('span', 'k', 'route'), routeIn,
    el('span', 'k', '显示名'), nameIn,
    el('span', 'k', '协议'), apiSel,
    el('span', 'k', 'baseURL'), urlIn,
    el('span', 'k', 'API key'), keyIn,
    el('span', 'k', '模型'), modelsIn,
  );
  const addBtn = button('btn small primary', '写入服务商');
  const overwriteBox = checkInput('允许覆盖已存在的同名 route', false);
  addBtn.addEventListener('click', async () => {
    const route = (routeIn.value || '').trim();
    if (!route) { toast('先填 route（小写字母 / 数字 / 中划线）', 'err', 4200); return; }
    const models = (modelsIn.value || '').split(',').map((s) => s.trim()).filter(Boolean);
    const yes = await confirmBox({
      head: '写入自定义服务商',
      text: `把「${route}」写进 ${lane}/${profSel.value} 的补丁层？\n\n`
        + '会改 cordis.patch.yml（先备份 .bak），key 会存进 .credentials.yaml 的 '
        + `${defaultKeyRef(route)}。`,
      ok: '写入',
    });
    if (!yes || ctl.closed) return;
    try {
      const res = await api('provider_add', {
        lane, profile: profSel.value, route,
        displayName: (nameIn.value || '').trim(), api: apiSel.value,
        baseURL: (urlIn.value || '').trim(), apiKey: keyIn.value || '', models,
        overwrite: overwriteBox.get(),
      });
      if (!res.ok) { toast(res.error || '写入失败', 'err', 7000); return; }
      const what = { added: '新增', replaced: '覆盖更新', none: '文件没变', refused: '被拒绝' };
      toast(`${route}：${what[res.action] || res.action || '写完'}`,
        res.action === 'none' ? 'warn' : 'ok', 3600);
      if (res.action === 'added' || res.action === 'replaced') {
        routeIn.value = '';
        nameIn.value = '';
        urlIn.value = '';
        keyIn.value = '';
        modelsIn.value = '';
      }
      await load();
    } catch (err) { toast('写入失败：' + err.message, 'err', 7000); }
  });
  ctl.body.append(el('div', 'field-label', '新增 / 更新一个服务商'), addGrid,
    el('div', 'pick-bar', addBtn, overwriteBox));

  load();
}

/** dsh 自己的 keyRef 规则：route → UPPER_SNAKE + _API_KEY */
function defaultKeyRef(route) {
  return String(route || '').toUpperCase().replace(/[^A-Z0-9]+/g, '_') + '_API_KEY';
}

/* ── ④c 配置文件查看（需求③：打开 / 看内容 / 定位） ─────────────────────── */

const CONFIG_KINDS = [
  ['patch', 'cordis.patch.yml', '插件补丁层（开关 / 服务商 / 路由都在这）'],
  ['credentials', '.credentials.yaml', '凭据（refs 段）'],
  ['package', 'package.json', '这个 profile 的依赖与 dsh.profile.bundles'],
  ['lock', 'pnpm-lock.yaml', '实际装上的版本账本'],
  ['workspace', 'pnpm-workspace.yaml', 'pnpm 工作区设置'],
  ['modules', 'node_modules/.modules.yaml', 'node_modules 布局（软链 / 隔离）'],
];

function dlgFiles(lane) {
  const ctl = openDialog({
    head: `配置文件 · lane「${lane}」`,
    size: 'lg',
    build: (c) => { c.body.append(spinnerRow('正在准备…')); },
    foot: [{ text: '关闭', act: (c) => c.close() }],
  });
  api('lane_profiles', { lane }).then((data) => {
    if (ctl.closed) return;
    clear(ctl.body);
    buildFiles(ctl, lane, data);
  }).catch((err) => {
    if (ctl.closed) return;
    clear(ctl.body);
    buildFiles(ctl, lane, { profiles: [], error: err.message });
  });
  return ctl;
}

function buildFiles(ctl, lane, data) {
  const profiles = (data.profiles || []).map((p) => p.name);
  const profSel = selectInput(profiles.length ? profiles : [core_default_profile(lane)],
    core_default_profile(lane));
  const cur = { which: 'patch' };
  const pathLine = el('p', 'field-note');
  const pre = el('pre', 'preview file-pre');
  const openBtn = button('btn small', '在资源管理器里定位');
  openBtn.addEventListener('click', () => openWhich(ctl, lane, profSel.value, cur.which));
  const reloadBtn = button('btn small', '重新读取');

  ctl.body.append(el('p', 'dlg-lead',
    '直接看 dsh 自己的配置文件：改之前先看一眼、或者点「在资源管理器里定位」用记事本 / VSCode 改。'
    + '本工具写这些文件时会先留 .bak，所以看花眼了也能还原。'));
  ctl.body.append(el('div', 'plug-head', el('span', 'field-label', 'PROFILE'), profSel,
    reloadBtn, openBtn));

  const kinds = el('div', 'pick-bar');
  const kindBtns = new Map();
  for (const [key, label, note] of CONFIG_KINDS) {
    const btn = button('btn small', label);
    btn.title = note;
    btn.addEventListener('click', () => { cur.which = key; markKind(); load(); });
    kindBtns.set(key, btn);
    kinds.append(btn);
  }
  function markKind() {
    for (const [key, btn] of kindBtns) setClass(btn, 'primary', key === cur.which);
  }
  ctl.body.append(kinds, pathLine, pre);
  reloadBtn.addEventListener('click', load);
  profSel.addEventListener('change', load);

  async function load() {
    markKind();
    clear(pre);
    pathLine.classList.remove('err');
    setText(pathLine, '正在读取…');
    try {
      const res = await api('file_view', { lane, profile: profSel.value, which: cur.which });
      if (ctl.closed) return;
      if (!res.ok) {
        setText(pathLine, res.error || '读取失败');
        pathLine.classList.add('err');
        return;
      }
      setText(pathLine, `${res.path}　（${res.bytes} 字节`
        + (res.clipped ? '，已截断显示' : '') + '）');
      pre.textContent = res.text || '（空文件）';
    } catch (err) {
      setText(pathLine, '读取失败：' + err.message);
      pathLine.classList.add('err');
    }
  }

  load();
}

async function openWhich(ctl, lane, profile, which) {
  try {
    const res = await api('open_path', { which: 'file', key: which, lane, profile });
    if (res && res.error) toast(res.error, 'warn', 4200);
    else toast('已定位到文件', 'ok', 2400);
  } catch (err) {
    toast('打不开：' + err.message, 'err', 4200);
  }
}

/* ── ④ 插件库（读取已装插件 · 跨 profile / 跨版本搬运） ─────────────────── */

function dlgPlugins(lane, profile) {
  const lanes = S.lanes.map((l) => l.name);
  if (!lanes.length) {
    confirmBox({
      head: '插件库',
      text: '还没有任何 lane。先在右上角点「＋ 新建版本」。',
      ok: '知道了',
    });
    return null;
  }
  const srcLane = lanes.includes(lane) ? lane : lanes[0];
  const ctl = openDialog({
    head: '插件库 · 读取与迁移',
    size: 'xl',
    build: (c) => {
      c.body.append(el('p', 'dlg-lead',
        '读的是磁盘真值（不跑 --dump-config），所以是即时的：包名、实际装上的版本、'
        + '行 id、开关状态。左边挑要搬的，右边选搬到哪、怎么搬。'));
      c.body.append(spinnerRow('正在读取插件清单…'));
    },
    foot: [{ text: '关闭', act: (c) => c.close() }],
  });
  buildPluginUI(ctl, srcLane, profile);
  return ctl;
}

function buildPluginUI(ctl, srcLane, srcProfile) {
  const lanes = S.lanes.map((l) => l.name);
  const state = { plugins: [], vars: new Map(), plan: null, srcLane, srcProfile: '' };

  const srcLaneSel = selectInput(lanes, srcLane);
  let srcProfBox = comboInput([], srcProfile || '', 'web');
  const dstLaneSel = selectInput(lanes, lanes[0]);
  let dstProfBox = comboInput([], core_default_profile(srcLane), 'web');
  const mode = radioGroup('ptype', [
    { value: 'install', label: '重新安装（点名到同版本）',
      note: '走 dsh 自己的 plugin 命令，pnpm 账本干净；跨版本对不上会明确报错。需要能连上源。' },
    { value: 'copy', label: '离线直拷（不联网 / 不用 node）',
      note: '把插件连同依赖整棵复制过去，链接按目标路径重建；pnpm 账本不会同步。' },
  ], 'install');
  const status = el('span', 'field-note');
  const pickList = el('div', 'pick-list');
  const preview = el('pre', 'preview');
  const previewHost = el('div', 'plug-col');
  const bar = el('div', 'pick-bar');

  function setPreview(lines) {
    clear(preview);
    for (const [text, tag] of lines) {
      const node = el('span', tag || '', (text || '') + '\n');
      preview.append(node);
    }
  }

  function picked() {
    return [...state.vars.entries()].filter(([, box]) => box.get()).map(([pkg]) => pkg);
  }

  function renderPlugins(plugins, error) {
    clear(pickList);
    state.plugins = plugins || [];
    state.vars.clear();
    if (error) {
      pickList.append(el('p', 'field-note err', error));
      setText(status, '读取失败');
      return;
    }
    if (!plugins.length) {
      pickList.append(el('p', 'field-note', '这个环境没有第三方插件（只有官方组合包）。'));
    }
    for (const item of plugins) {
      const row = el('div', 'pick-row');
      const box = checkInput('', false);
      const main = el('div', 'pick-main');
      const title = el('div', 'pick-title');
      title.append(el('b', null, item.pkg));
      title.append(el('span', 'ver', item.version || '?'));
      const disabledRows = item.disabled_rows || [];
      const rows = item.rows || [];
      if (rows.length && disabledRows.length === rows.length) {
        title.append(el('span', 'pill ok', '已禁用'));
      } else if (disabledRows.length) {
        title.append(el('span', 'pill warn', `部分禁用 ${disabledRows.length}/${rows.length}`));
      } else {
        title.append(el('span', 'pill', '启用中'));
      }
      if (item.kind === 'client-only') title.append(el('span', 'pill tag', '纯客户端'));
      if (!item.in_bundles) title.append(el('span', 'pill tag', '未登记为组合包'));
      main.append(title);
      main.append(el('div', 'pick-sub', '行 id：' + (rows.join('、') || '（无）')));
      row.append(box.node, main);

      // 就地开关 / 卸载（需求②：启动器页面直接管插件，不用去 dsh 里点）
      const ops = el('div', 'pick-ops');
      const fullyDisabled = rows.length > 0 && disabledRows.length === rows.length;
      if (rows.length && !item.protected) {
        const tg = button('btn small', fullyDisabled ? '启用' : '禁用');
        tg.addEventListener('click', () => setPluginState(item, !fullyDisabled));
        ops.append(tg);
      }
      if (!item.protected && !item.official && item.pkg !== 'dshmarket') {
        const rm = button('btn small danger', '卸载');
        rm.addEventListener('click', () => removePkg(item));
        ops.append(rm);
      }
      if (ops.children.length) row.append(ops);
      state.vars.set(item.pkg, box);
      pickList.append(row);
    }
    setText(status, `共 ${state.plugins.length} 个第三方插件`);
    ctl.refreshFoot();
  }

  /** 打开/关掉一个插件：后端写补丁层 + 宿主校验，不过会自动回滚 */
  async function setPluginState(item, on) {
    try {
      const res = await api('plugin_state',
        { lane: state.srcLane, profile: state.srcProfile, pkg: item.pkg, on });
      if (!res.ok) { toast(res.error || '写入失败', 'err', 6000); return; }
      if (res.rollback) {
        toast(`${item.pkg} 的改动没过校验，已自动回滚（文件没变）`, 'warn', 6000);
      } else {
        toast(`${item.pkg} 已${on ? '启用' : '禁用'}`, 'ok', 3000);
      }
      await loadSource();
    } catch (err) { toast('操作失败：' + err.message, 'err', 6000); }
  }

  async function removePkg(item) {
    const yes = await confirmBox({
      head: '卸载插件',
      text: `从 ${state.srcLane}/${state.srcProfile} 卸载「${item.pkg}」？\n\n`
        + '会删依赖、摘掉 dsh.profile.bundles 里的登记，并清掉补丁层里它的行。',
      ok: '卸载',
    });
    if (!yes || ctl.closed) return;
    startJob(ctl, `卸载插件 ${item.pkg}`, 'plugin_remove',
      { lane: state.srcLane, profile: state.srcProfile, pkg: item.pkg, cleanRows: true },
      { reload: () => loadSource() });
  }

  async function loadSource() {
    const laneName = srcLaneSel.value;
    const prof = (srcProfBox.input.value || '').trim() || core_default_profile(laneName);
    state.srcLane = laneName;
    state.srcProfile = prof;
    setText(status, '正在读取插件…');
    clear(pickList);
    pickList.append(spinnerRow('正在读取插件…'));
    setPreview([['正在读取插件清单…', 'muted']]);
    try {
      const data = await api('plugin_list', { lane: laneName, profile: prof });
      if (ctl.closed) return;
      setComboOptions(srcProfBox, data.profiles.map((p) => p.name));
      renderPlugins(data.plugins, data.error);
    } catch (err) {
      if (ctl.closed) return;
      clear(pickList);
      pickList.append(el('p', 'field-note err', `读取失败：${err.message}`));
      setText(status, '读取失败');
    }
  }

  async function loadTargetProfiles() {
    const laneName = dstLaneSel.value;
    try {
      const data = await api('lane_profiles', { lane: laneName });
      if (ctl.closed) return;
      setComboOptions(dstProfBox, data.profiles.map((p) => p.name));
    } catch (_) { /* 目标 lane 的 profile 列不出来也不挡搬运 */ }
  }

  async function buildPlan(showErrors) {
    const pkgs = picked();
    if (!pkgs) return null;
    if (!pkgs.length) {
      setPreview([['先在左边勾选要搬的插件。', 'muted']]);
      if (showErrors) toast('先在左边勾选要搬的插件', 'err', 3600);
      return null;
    }
    const dstProf = (dstProfBox.input.value || '').trim() || core_default_profile(dstLaneSel.value);
    setPreview([['正在算计划…', 'muted']]);
    let plan;
    try {
      plan = await api('plugin_plan', {
        srcLane: state.srcLane, srcProfile: state.srcProfile,
        dstLane: dstLaneSel.value, dstProfile: dstProf,
        pkgs, mode: mode.get(),
      });
    } catch (err) {
      setPreview([[`算计划失败：${err.message}`, 'err']]);
      if (showErrors) toast('算计划失败：' + err.message, 'err', 4200);
      return null;
    }
    if (ctl.closed) return null;
    const lines = [];
    if (!plan.ok) {
      lines.push([`不能搬：${plan.why}`, 'err']);
      setPreview(lines);
      state.plan = null;
      return null;
    }
    state.plan = plan;
    lines.push([`源   ${plan.srcLane}/${plan.srcProfile}`, 'muted']);
    lines.push([`目标 ${plan.dstLane}/${plan.dstProfile}`
      + (plan.dst && plan.dst.onDisk ? '' : '（磁盘上还没有，会先建一个干净环境）'), 'muted']);
    lines.push([`方式 ${MODE_LABEL[mode.get()]}`, 'muted']);
    lines.push(['', null]);
    for (const item of plan.items) {
      let text = `· ${item.pkg}  ${item.version || '?'} → ${item.want}`;
      if (item.why) text += `   （${item.why}）`;
      lines.push([text, item.action === 'skip' ? 'muted' : (item.version ? 'ok' : null)]);
    }
    const skipped = plan.items.filter((it) => it.action === 'skip');
    if (skipped.length) {
      lines.push(['', null]);
      lines.push([`已自动过滤 ${skipped.length} 个：两边同版本 / 源里没真装上，不用搬。`,
        'muted']);
    }
    for (const line of plan.warnings || []) lines.push([`注意  ${line}`, 'warn']);
    setPreview(lines);
    return plan;
  }

  // ── 版面
  const left = el('div', 'plug-col');
  left.append(el('div', 'plug-head',
    el('span', 'field-label', '来源环境'), srcLaneSel, srcProfBox));
  left.append(pickList);

  const specIn = textInput('', '包名或 @scope/name@1.2.3');
  specIn.classList.add('grow');
  const addBtn = button('btn small primary', '加装');
  addBtn.addEventListener('click', async () => {
    const spec = (specIn.value || '').trim();
    if (!spec) { toast('先填包名（可带版本）', 'err', 3600); return; }
    const yes = await confirmBox({
      head: '加装插件',
      text: `给 ${state.srcLane}/${state.srcProfile} 装「${spec}」？\n\n`
        + '走 dsh 自己的 plugin add（pnpm），需要能连上源；装好后会登记进 dsh.profile.bundles。',
      ok: '加装',
    });
    if (!yes || ctl.closed) return;
    specIn.value = '';
    startJob(ctl, `加装插件 ${spec}`, 'plugin_add',
      { lane: state.srcLane, profile: state.srcProfile, spec },
      { reload: () => loadSource() });
  });
  left.append(el('div', 'field-label', '加装插件（走 dsh 自己的 plugin add）'),
    el('div', 'plug-head', specIn, addBtn));

  const right = el('div', 'plug-col');
  right.append(el('div', 'plug-head',
    el('span', 'field-label', '目标环境'), dstLaneSel, dstProfBox));
  right.append(mode.node);
  previewHost.append(el('div', 'field-label', '计划预览'), preview);
  right.append(previewHost);

  bar.append(status, el('span', 'spacer'));
  const btnAll = button('btn small', '全选');
  const btnNone = button('btn small', '全不选');
  const btnOverview = button('btn small', '总览全部环境');
  const btnReload = button('btn small primary', '重新读取');
  btnAll.addEventListener('click', () => { for (const box of state.vars.values()) box.set(true); });
  btnNone.addEventListener('click', () => { for (const box of state.vars.values()) box.set(false); });
  btnOverview.addEventListener('click', () => act('插件总览', 'plugin_overview', {}));
  btnReload.addEventListener('click', () => loadSource());
  bar.prepend(btnReload, btnOverview, btnNone, btnAll);

  const grid = el('div', 'plug-grid');
  grid.append(left, right);
  clear(ctl.body);
  ctl.body.append(noteLine(
    '读的是磁盘真值（不跑 --dump-config），所以是即时的：包名、实际装上的版本、行 id、开关状态。'
    + '左边挑要搬的，右边选搬到哪、怎么搬。', 'info'));
  ctl.body.append(grid, bar);

  srcLaneSel.addEventListener('change', () => {
    setComboOptions(srcProfBox, []);
    srcProfBox.input.value = core_default_profile(srcLaneSel.value);
    loadSource();
  });
  srcProfBox.input.addEventListener('change', () => loadSource());
  dstLaneSel.addEventListener('change', () => loadTargetProfiles());
  mode.onChange(() => buildPlan(false));

  ctl.setFoot([
    { text: '关闭', act: (c) => c.close() },
    { text: '预览计划', act: () => buildPlan(true) },
    {
      text: '开始搬运',
      kind: 'primary',
      enabled: () => picked().length > 0,
      act: async (c) => {
        const plan = await buildPlan(true);
        if (!plan || c.closed) return;
        const pkgs = picked();
        const dstProf = (dstProfBox.input.value || '').trim() || core_default_profile(dstLaneSel.value);
        const yes = await confirmBox({
          head: '开始搬运',
          text: `把 ${pkgs.length} 个插件从 ${state.srcLane}/${state.srcProfile} `
            + `搬到 ${dstLaneSel.value}/${dstProf}？\n\n方式：${MODE_LABEL[mode.get()]}\n`
            + '会写目标 profile 的 node_modules / package.json / cordis.patch.yml（改动前先留 .bak）。',
          ok: '开始搬运',
        });
        if (!yes || c.closed) return;
        startJob(c, `搬运插件 → ${dstLaneSel.value}/${dstProf}`, 'plugin_transfer',
          {
            srcLane: state.srcLane, srcProfile: state.srcProfile,
            dstLane: dstLaneSel.value, dstProfile: dstProf,
            pkgs, mode: mode.get(),
          },
          { reload: () => loadSource() });
      },
    },
  ]);

  loadSource();
  loadTargetProfiles();
}

/** 后端的 default_profile_of：web 优先，否则按名字排第一个 */
function core_default_profile(laneName) {
  const lane = S.laneMap.get(laneName);
  if (!lane) return 'web';
  return lane.defaultProfile || 'web';
}

/* ── ⑤ 插件市场（dshmarket）单独开关 / 更新 ─────────────────────────────── */

function dlgMarket(lane) {
  const ctl = openDialog({
    head: `插件市场（dshmarket）：lane「${lane}」`,
    size: 'lg',
    build: (c) => { c.body.append(spinnerRow('正在读状态（顺便查一次 registry）…')); },
    foot: [{ text: '取消', act: (c) => c.close() }],
  });
  loadDialog(ctl, '读取插件市场状态', 'market_get', { lane }, (st) => buildMarket(ctl, lane, st));
  return ctl;
}

function buildMarket(ctl, lane, st) {
  let latest = st.latest || '';

  const stateLine = el('p', 'dlg-lead');
  const detail = el('p', 'field-note');
  const note = el('p', 'field-note warn');
  const disabled = st.disabled || [];
  const rows = st.rows || [];

  if (!st.spec && !st.packageOnDisk) {
    setText(stateLine, '这条 lane 没装插件市场');
    stateLine.className = 'dlg-lead warn';
  } else if (disabled.length && rows.length && disabled.length === rows.length) {
    setText(stateLine, '已关掉（插件开关块在）');
    stateLine.className = 'dlg-lead ok';
  } else if (disabled.length) {
    setText(stateLine, `部分关着：${disabled.join('、')}`);
    stateLine.className = 'dlg-lead warn';
  } else {
    setText(stateLine, '启用中');
    stateLine.className = 'dlg-lead';
  }

  const lines = [
    `已装版本  ${st.installed || '（没装）'}`
      + (st.spec ? `      profile 里写的是 ${st.spec}` : ''),
    `行 id     ${rows.join('、') || '(没有补丁行)'}`,
  ];
  if (latest) {
    lines.push(`registry  最新 ${latest}`
      + (latest === st.installed ? '（就是你现在这个）' : '  ← 有新版本'));
  } else if (st.registryError) {
    lines.push(`registry  查不到：${st.registryError}`);
  } else {
    lines.push('registry  没查（离线）');
  }
  const snaps = st.snapshots || [];
  if (snaps.length) {
    lines.push(`更新快照  ${snaps.length} 个，最新是 ${snaps[0].version || '?'}`);
  }
  setText(detail, lines.join('\n'));

  if (st.generalBlock) {
    setText(note, '注意：这条 lane 的补丁层里还挂着一个「dsh-lanes 禁用第三方插件」块'
      + '（启动器已不再维护它，要撤只能手删那几行）');
  } else if (latest && latest !== st.installed) {
    setText(note, `有新版 ${latest}：点「更新」即可（会先存快照）`);
  } else {
    setText(note, '');
    note.hidden = true;
  }

  const boot = checkInput('改动后启动一次确认还能起来（起不来就自动还原）', true);
  const anyway = checkInput('新版本被 pnpm 的 strict 按住也硬闯（--anyway，带一次性绕过）', false);

  ctl.body.append(
    el('p', 'dlg-lead',
      '关掉它不会动别的插件，也不会动官方组合包；「打开」把启动器写的那一小段删掉就回来了。'
      + '「更新」只更新插件市场自己：点名到版本地装，装完启动一次确认能起来，起不来自动回滚。'),
    stateLine, detail, note,
    el('div', 'check-group', boot.node, anyway.node),
  );

  const canUpdate = !!latest;
  ctl.setFoot([
    { text: '关掉', act: (c) => startJob(c, `关掉插件市场：${lane}`, 'market_off',
        { lane, bootCheck: boot.get() }) },
    { text: '打开', act: (c) => startJob(c, `打开插件市场：${lane}`, 'market_on', { lane }) },
    {
      text: '更新',
      kind: 'primary',
      enabled: () => canUpdate,
      act: async (c) => {
        if (!latest) {
          toast(`没查到 registry 上的版本。可以先用命令行指定版本：\n`
            + `py dsh_lanes.py market-update ${lane} --version <版本号>`, 'err', 6000);
          return;
        }
        const yes = await confirmBox({
          head: '更新插件市场',
          text: `把插件市场更新到 ${latest}？\n\n`
            + '· 会先把现在的包与清单存一份快照（失败能退回）\n'
            + '· 走 dsh plugin → pnpm，钉死这个版本号\n'
            + '· 装完启动一次确认能起来，起不来自动回滚\n'
            + '· 更新完要重启这条 lane 才生效',
          ok: '更新',
        });
        if (!yes || c.closed) return;
        startJob(c, `更新插件市场：${lane}`, 'market_update',
          { lane, version: null, anyway: anyway.get(), bootCheck: boot.get() });
      },
    },
    { text: '取消', act: (c) => c.close() },
  ]);
}

/* ── ⑥ 备份对话 ─────────────────────────────────────────────────────────── */

function dlgChat(lane) {
  const ctl = openDialog({
    head: `备份对话：lane「${lane}」`,
    size: 'xl',
    build: (c) => { c.body.append(spinnerRow('正在读取备份列表…')); },
    foot: [{ text: '关闭', act: (c) => c.close() }],
  });
  loadDialog(ctl, '读取备份列表', 'chat_list', { lane }, (data) => buildChat(ctl, lane, data));
  return ctl;
}

function buildChat(ctl, lane, data) {
  const items = data.snapshots || [];
  let selected = -1;

  const headRow = el('div', 'row-head cols-chat');
  headRow.append(el('span', null, '名称'), el('span', null, '时间'),
    el('span', null, '会话'), el('span', null, '大小'),
    el('span', 'hide-narrow', null, '备注'));
  const rows = el('div', 'rows');
  rows.append(headRow);
  const status = el('p', 'field-note');

  if (!items.length) {
    const empty = el('p', 'field-note warn');
    setText(empty, `还没有备份。当前会话文件数 ${data.current}，点「立即备份」来一份。`);
    rows.append(empty);
  }
  for (let i = 0; i < items.length; i += 1) {
    const item = items[i];
    const row = el('div', 'row-item cols-chat');
    row.append(el('span', 'mono-cell', item.name));
    row.append(el('span', 'mono-cell', String(item.created).slice(0, 19)));
    row.append(el('span', 'mono-cell', String(item.sessions)));
    row.append(el('span', 'mono-cell', item.human));
    const remark = (!item.ok ? '不可用：缺 manifest.json'
      : item.running ? '运行时备份' : (item.note || ''));
    row.append(el('span', 'mono-cell hide-narrow', remark));
    if (!item.ok) row.classList.add('bad');
    row.addEventListener('click', () => {
      selected = i;
      for (const other of rows.querySelectorAll('.row-item')) other.removeAttribute('aria-selected');
      row.setAttribute('aria-selected', 'true');
      ctl.refreshFoot();
    });
    rows.append(row);
  }

  if (items.length) {
    setText(status, `${items.length} 份备份 / ${data.totalHuman}；`
      + `这条 lane 现在的会话文件数 ${data.current}`
      + (data.running ? '　·　它正在运行' : ''));
  }

  const suffix = textInput('', '可留空，例如 before-upgrade');
  const backupRow = el('div', 'two-col');
  backupRow.append(el('span', 'k', '备份后缀'), suffix);

  ctl.body.append(
    el('p', 'dlg-lead',
      '备份的是这条 lane 的对话记录（DSH_HOME\\sessions，一个会话一个文件）。'
      + '恢复前会先校验快照本身，再把现在的记录另存一份；恢复后逐文件比对指纹。'),
    rows, status, backupRow,
  );

  ctl.setFoot([
    { text: '关闭', act: (c) => c.close() },
    {
      text: '立即备份',
      act: (c) => startJob(c, `备份对话：${lane}`, 'chat_backup',
        { lane, name: suffix.value.trim() || null },
        { reload: (self) => reload(self) }),
    },
    {
      text: '恢复选中的',
      enabled: () => selected >= 0 && items[selected] && items[selected].ok,
      act: async (c) => {
        const target = items[selected];
        if (!target) { toast('先在列表里选一份备份', 'err'); return; }
        if (!target.ok) { toast('这份快照不完整（缺 manifest.json），不能用', 'err', 4200); return; }
        const yes = await confirmBox({
          head: '备份对话',
          text: `把对话记录恢复到「${target.name}」？\n\n`
            + `· 快照里 ${target.sessions} 个会话文件 / ${target.human}\n`
            + '· 先校验快照自身，再把现在的记录另存一份（恢复错了能回来）\n'
            + '· 恢复完逐文件比对指纹'
            + (data.running ? '\n\n这条 lane 正在运行：恢复会先把它停掉，之后请手动「打开」。继续？' : ''),
          ok: '恢复',
        });
        if (!yes || c.closed) return;
        startJob(c, `恢复对话：${lane}`, 'chat_restore', { lane, name: target.name },
          { reload: (self) => reload(self) });
      },
    },
    {
      text: '删除选中的',
      kind: 'danger',
      enabled: () => selected >= 0 && !!items[selected],
      act: async (c) => {
        const target = items[selected];
        if (!target) { toast('先在列表里选一份备份', 'err'); return; }
        const yes = await confirmBox({
          head: '备份对话',
          text: `删掉快照「${target.name}」（${target.human}）？删了就没了。`,
          ok: '删除',
          danger: true,
        });
        if (!yes || c.closed) return;
        startJob(c, `删除备份：${lane}`, 'chat_rm', { lane, name: target.name },
          { reload: (self) => reload(self) });
      },
    },
  ]);

  function reload(self) {
    loadDialog(self, '读取备份列表', 'chat_list', { lane }, (next) => buildChat(self, lane, next));
  }
}

/* ── ⑥b 配置备份（轻量：插件清单 + 会话 + 设置；恢复时自动联网拉回插件）────────── */

function dlgCfg(initialLane) {
  const ctl = openDialog({
    head: initialLane ? `配置备份：lane「${initialLane}」` : '配置备份 / 迁移',
    size: 'xl',
    build: (c) => { c.body.append(spinnerRow('正在读取备份列表…')); },
    foot: [{ text: '关闭', act: (c) => c.close() }],
  });
  const st = { initialLane: initialLane || '', createLane: initialLane || '' };
  ctl.gotoList = () => renderCfgList(ctl);
  if (initialLane) {
    renderCfgCreate(ctl, st);
  } else {
    renderCfgList(ctl);
  }
  return ctl;
}

function renderCfgList(ctl) {
  ctl.setHead('配置备份 / 迁移');
  ctl.body.append(spinnerRow('正在读取备份列表…'));
  loadDialog(ctl, '读取备份列表', 'cfg_list', {}, (data) => {
    clear(ctl.body);
    const items = data.items || [];
    let selected = -1;
    const headRow = el('div', 'row-head cols-cfg');
    headRow.append(el('span', null, '备份名'), el('span', null, '来源'),
      el('span', null, '环境'), el('span', null, '插件'),
      el('span', null, '会话'), el('span', null, '大小'));
    const rows = el('div', 'rows');
    rows.append(headRow);
    if (!items.length) {
      rows.append(el('p', 'field-note warn',
        '还没有配置备份。点「新建备份」打包一份插件清单+会话+设置（不含插件本体，很快）。'));
    }
    for (let i = 0; i < items.length; i += 1) {
      const item = items[i];
      const row = el('div', item.ok ? 'row-item cols-cfg' : 'row-item cols-cfg bad');
      row.append(el('span', 'mono-cell', item.name));
      row.append(el('span', 'mono-cell', item.ok ? `${item.sourceLane}@${item.dshVersion}` : '—'));
      row.append(el('span', 'mono-cell', item.ok ? (item.profiles || []).join(',') : '—'));
      row.append(el('span', 'mono-cell', item.ok ? String(item.thirdParty) : '—'));
      row.append(el('span', 'mono-cell', item.ok ? String(item.sessions) : '—'));
      row.append(el('span', 'mono-cell', item.ok ? item.human : (item.error || '损坏')));
      row.addEventListener('click', () => {
        selected = i;
        for (const other of rows.querySelectorAll('.row-item')) {
          other.removeAttribute('aria-selected');
        }
        row.setAttribute('aria-selected', 'true');
        ctl.refreshFoot();
      });
      rows.append(row);
    }
    ctl.body.append(
      el('p', 'dlg-lead',
        '配置备份是单个 zip：插件清单（含精确版本与开关）、会话、设置；'
        + '恢复到新版本时自动联网把插件装回来。文件在根目录 backups\\config 下，可直接拷走。'),
      rows,
    );
    ctl.setFoot([
      { text: '关闭', act: (c) => c.close() },
      { text: '新建备份', kind: 'primary',
        act: (c) => renderCfgCreate(c, { initialLane: '', createLane: '' }) },
      {
        text: '查看内容 / 增删插件',
        enabled: () => selected >= 0 && items[selected] && items[selected].ok,
        act: (c) => renderCfgShow(c, items[selected].name),
      },
      {
        text: '导入到新版本…',
        kind: 'primary',
        enabled: () => selected >= 0 && items[selected] && items[selected].ok,
        act: (c) => renderCfgRestore(c, items[selected].name),
      },
      {
        text: '删除',
        kind: 'danger',
        enabled: () => selected >= 0 && !!items[selected],
        act: async (c) => {
          const item = items[selected];
          const yes = await confirmBox({
            head: '配置备份',
            text: `删掉备份「${item.name}」（${item.human}）？删了就没了。`,
            ok: '删除', danger: true,
          });
          if (!yes || c.closed) return;
          try {
            await api('cfg_rm', { name: item.name });
            toast('已删除', 'ok');
          } catch (err) { toast(err.message, 'err'); return; }
          renderCfgList(c);
        },
      },
    ]);
  });
}

function renderCfgCreate(ctl, st) {
  ctl.setHead('新建配置备份');
  clear(ctl.body);
  const laneSel = selectInput(
    (S.lanes || []).map((l) => ({ value: l.name, label: `${l.name}  (${l.version})` })),
    st.createLane || '');
  const suffix = textInput('', '可留空，例如 before-reinstall');
  const opts = {
    sessions: checkInput('会话记录（sessions）', true),
    attachments: checkInput('会话附件（attachments）', true),
    settings: checkInput('设置文件（顶层 *.json/*.yaml 小文件）', true),
    credentials: checkInput('凭据（API key 等，敏感）', false),
  };
  const dataBox = el('div', 'check-grid');
  const planLine = el('p', 'field-note', '正在计算内容…');
  const dataPicked = new Set();

  async function refreshPlan() {
    const lane = laneSel.value;
    st.createLane = lane;
    setText(planLine, '正在计算内容…');
    try {
      const data = await api('cfg_plan', {
        lane,
        sessions: opts.sessions.get(),
        attachments: opts.attachments.get(),
        settings: opts.settings.get(),
        credentials: opts.credentials.get(),
        pluginData: [...dataPicked],
      });
      if (ctl.closed) return;
      clear(dataBox);
      for (const d of data.dataDirs || []) {
        const chk = checkInput(`${d.name}  (${d.human})`, dataPicked.has(d.name));
        chk.onChange(() => {
          if (chk.get()) dataPicked.add(d.name); else dataPicked.delete(d.name);
          refreshPlan();
        });
        dataBox.append(chk.node);
      }
      const bits = [];
      if (opts.sessions.get()) bits.push(`会话 ${data.sessions.human}`);
      if (opts.attachments.get()) bits.push(`附件 ${data.attachments.human}`);
      if (opts.settings.get()) bits.push(`设置 ${data.settingsFiles.length} 个`);
      if (opts.credentials.get()) bits.push(`凭据 ${data.credentials.length} 条`);
      if (dataPicked.size) {
        bits.push(`数据目录 ${dataPicked.size} 个`);
      }
      if (data.localFiles.length) {
        bits.push(`本地插件 ${data.localFiles.map((x) => x.human).join('+')}`);
      }
      const prof = (data.profiles || []).map((p) =>
        `${p.name}(${p.thirdParty} 插件)`).join(', ');
      setText(planLine, `环境 ${prof}；打包约 ${data.totalHuman}：${bits.join(' · ') || '仅插件清单'}`);
    } catch (err) {
      if (!ctl.closed) setText(planLine, `计算失败：${err.message}`);
    }
  }
  laneSel.addEventListener('change', refreshPlan);
  Object.values(opts).forEach((c) => c.onChange(refreshPlan));

  ctl.body.append(
    el('p', 'dlg-lead',
      '只备份"装了什么、说了什么、怎么配置"，不复制 profiles 里的插件本体（几百 MB）'
      + '与 dsh 安装树。恢复时按清单逐个联网装回，版本精确到当前安装值。'),
    field('从哪条 lane 备份', laneSel),
    field('备份后缀（可留空）', suffix, '文件名形如 dshcfg-<lane>-<时间戳>-<后缀>.zip'),
    el('div', 'field',
      el('div', 'field-label', '打包内容'),
      opts.sessions.node, opts.attachments.node, opts.settings.node, opts.credentials.node),
    el('div', 'field',
      el('div', 'field-label', '插件数据目录（可选，默认不打；体积大的工作数据在这）'),
      dataBox),
    planLine,
  );
  refreshPlan();
  ctl.setFoot([
    { text: '返回列表', act: (c) => renderCfgList(c) },
    {
      text: '开始备份', kind: 'primary',
      act: (c) => startJob(c, `配置备份：${laneSel.value}`, 'cfg_backup', {
        lane: laneSel.value,
        name: suffix.value.trim() || null,
        sessions: opts.sessions.get(),
        attachments: opts.attachments.get(),
        settings: opts.settings.get(),
        credentials: opts.credentials.get(),
        pluginData: [...dataPicked],
      }),
    },
  ]);
}

function renderCfgShow(ctl, name) {
  ctl.setHead(`备份内容：${name}`);
  ctl.body.append(spinnerRow('正在读取备份内容…'));
  loadDialog(ctl, '读取备份内容', 'cfg_show', { name }, (data) => {
    clear(ctl.body);
    const picked = new Map();   // profile -> Set(pkg)
    const info1 = el('p', 'field-note',
      `来源 ${data.source.lane}（dsh ${data.source.dshVersion}） · 打包于 ${data.created} · ${data.human}`);
    const contentBits = [];
    if (data.sessions.included) contentBits.push(`会话 ${data.sessions.files} 个 / ${data.sessions.human}`);
    if (data.attachments.included) contentBits.push(`附件 ${data.attachments.files}`);
    if (data.settings.included) contentBits.push(`设置 ${data.settings.files.length} 项`);
    if (data.credentials.included) contentBits.push(`凭据 ${data.credentials.entries.length} 条`);
    if (data.localFiles.length) contentBits.push(`本地插件 ${data.localFiles.length} 个`);
    if (data.pluginData.length) contentBits.push(`数据目录 ${data.pluginData.map((x) => x.name).join(',')}`);
    const profBox = el('div', 'cfg-profs');
    for (const prof of data.profiles) {
      const box = el('div', 'cfg-prof');
      const set = new Set();
      picked.set(prof.name, set);
      const grid = el('div', 'rows cfg-plugin-rows');
      for (const plug of prof.plugins) {
        const row = el('div', 'row-item cols-cfg-plugin');
        const chk = el('input');
        chk.type = 'checkbox';
        chk.addEventListener('change', () => {
          if (chk.checked) set.add(plug.pkg); else set.delete(plug.pkg);
        });
        row.append(chk,
          el('span', 'mono-cell', plug.pkg),
          el('span', 'mono-cell', plug.version),
          el('span', 'mono-cell', plug.disabled ? '关闭' : '启用'),
          el('span', 'mono-cell hide-narrow', plug.local ? '本地依赖' : ''));
        grid.append(row);
      }
      box.append(
        el('h4', null, `profile「${prof.name}」：${prof.plugins.length} 个第三方插件`
          + (prof.officialCount ? `（官方组合包 ${prof.officialCount} 个，宿主自带不重装）` : '')),
        grid);
      profBox.append(box);
    }
    // 添加插件
    const addProfile = selectInput(data.profiles.map((p) => p.name), data.profiles[0] && data.profiles[0].name);
    const addSpec = textInput('', 'pkg@spec，例如 dsh-foo@1.2.3');
    const laneOpts = [{ value: '', label: '不采集（按规格联网装，默认启用）' }]
      .concat((S.lanes || []).map((l) => ({ value: l.name, label: `从 lane「${l.name}」采集版本/开关` })));
    const fromLane = selectInput(laneOpts, '');
    const addNote = el('p', 'field-note');
    const addBtn = button('btn small', '添加进备份');
    addBtn.addEventListener('click', async () => {
      const spec = addSpec.value.trim();
      if (!spec) { toast('填 pkg@spec', 'err'); return; }
      addBtn.disabled = true;
      try {
        const res = await api('cfg_plugin_add', {
          name, profile: addProfile.value, plugins: [spec],
          fromLane: fromLane.value || '',
        });
        toast(`已加入 ${res.added.join(',')}`, 'ok');
        renderCfgShow(ctl, name);
      } catch (err) {
        toast(err.message, 'err');
        addBtn.disabled = false;
      }
    });

    ctl.body.append(
      info1,
      el('p', 'field-note', contentBits.join(' · ') || '仅插件清单'),
      profBox,
      el('div', 'field',
        el('div', 'field-label', '往备份里添加插件'),
        el('div', 'two-col', el('span', 'k', '加到 profile'), addProfile),
        el('div', 'two-col', el('span', 'k', '插件'), addSpec),
        el('div', 'two-col', el('span', 'k', '版本/开关'), fromLane),
        el('div', 'row', addBtn),
        addNote),
      el('p', 'field-note', '勾选上面的插件后点底部「删除勾选」：会同时从清单、bundles 登记和补丁开关里移除。'),
    );
    ctl.setFoot([
      { text: '返回列表', act: (c) => renderCfgList(c) },
      {
        text: '删除勾选插件', kind: 'danger',
        enabled: () => [...picked.values()].some((set) => set.size),
        act: async (c) => {
          const total = [...picked.values()].reduce((n, set) => n + set.size, 0);
          const yes = await confirmBox({
            head: '配置备份',
            text: `从备份里删除选中的 ${total} 个插件（连同开关条目）？`,
            ok: '删除', danger: true,
          });
          if (!yes || c.closed) return;
          for (const [profName, set] of picked.entries()) {
            if (!set.size) continue;
            try {
              await api('cfg_plugin_rm', { name, profile: profName, plugins: [...set] });
            } catch (err) { toast(err.message, 'err'); return; }
          }
          toast('已删除', 'ok');
          renderCfgShow(c, name);
        },
      },
    ]);
  });
}

function renderCfgRestore(ctl, name) {
  ctl.setHead(`从备份导入：${name}`);
  ctl.body.append(spinnerRow('正在读取备份内容…'));
  loadDialog(ctl, '读取备份内容', 'cfg_show', { name }, (data) => {
    clear(ctl.body);
    const modeNew = el('input');
    modeNew.type = 'radio';
    modeNew.name = `cfgmode-${uid('m')}`;
    modeNew.checked = true;
    const modeOld = el('input');
    modeOld.type = 'radio';
    modeOld.name = modeNew.name;
    const newName = textInput('', '新 lane 名，例如 new-work');
    const version = textInput(data.source.dshVersion || '', 'dsh 版本，默认用备份里的');
    const laneSel = selectInput((S.lanes || []).map((l) => l.name), '');
    const optSessions = checkInput('会话记录（现有会话会先另存一份）', true);
    const optSettings = checkInput('设置文件（同名文件先留 .bak）', true);
    const optCred = checkInput(`凭据（${data.credentials.included ? data.credentials.entries.length + ' 条，只合并不覆盖' : '备份里没有凭据'}）`,
      data.credentials.included);
    if (!data.credentials.included) optCred.input.disabled = true;
    const optData = checkInput(`插件数据目录（${data.pluginData.length} 个）`, data.pluginData.length > 0);
    if (!data.pluginData.length) optData.input.disabled = true;
    const planLine = el('p', 'field-note', '');
    const modeRow = (radio, label, node) => {
      const lab = el('label', 'check', radio, el('span', null, label));
      return el('div', 'cfg-mode', lab, node);
    };
    setHidden(laneSel, true);
    modeNew.addEventListener('change', () => {
      setHidden(newName, false); setHidden(version, false); setHidden(laneSel, true);
      refreshPlan();
    });
    modeOld.addEventListener('change', () => {
      setHidden(newName, true); setHidden(version, true); setHidden(laneSel, false);
      refreshPlan();
    });
    [newName, version, laneSel].forEach((n) => {
      n.addEventListener('change', refreshPlan);
      n.addEventListener('input', refreshPlan);
    });
    const plugCount = data.profiles.reduce((n, p) => n + p.plugins.length, 0);
    const plugText = data.profiles.map((p) =>
      `${p.name}：${p.plugins.length} 个`).join('；');

    async function refreshPlan() {
      try {
        const payload = {
          name,
          newLane: modeNew.checked ? newName.value.trim() : '',
          target: modeNew.checked ? '' : laneSel.value,
          version: version.value.trim(),
          sessions: optSessions.get(),
          settings: optSettings.get(),
          credentials: optCred.get(),
          pluginData: optData.get(),
        };
        const plan = await api('cfg_restore_plan', payload);
        if (ctl.closed) return;
        if (plan.ok) {
          const lines = [`将在 ${plan.mode === 'new' ? `新 lane「${plan.target}」装 dsh ${plan.version}` : `已有 lane「${plan.target}」`} 上：`,
            `· 装回 ${plan.plugins.length} 个第三方插件（${plugText}）`,
            `· ${plan.sessions.files && optSessions.get() ? `恢复会话 ${plan.sessions.files} 个` : '不动会话'}`,
            `· ${optSettings.get() && plan.settingsCount ? `恢复设置 ${plan.settingsCount} 项` : '不动设置'}`,
            `· ${optCred.get() && plan.credentialsIncluded ? '合并凭据' : '不动凭据'}`,
            `· ${optData.get() && plan.pluginDataCount ? `恢复数据目录 ${plan.pluginDataCount} 个` : '不动数据目录'}`,
            '安装插件会联网，耗时几分钟；lane 正在运行会先停掉。'];
          setText(planLine, lines.join('\n'));
        } else {
          setText(planLine, '需要先解决：\n' + plan.problems.map((p) => '· ' + p).join('\n'));
        }
      } catch (err) {
        setText(planLine, '校验失败：' + err.message);
      }
    }
    [optSessions, optSettings, optCred, optData].forEach((c) => c.onChange(refreshPlan));

    ctl.body.append(
      el('p', 'dlg-lead',
        `备份来自 ${data.source.lane}（dsh ${data.source.dshVersion}），共 ${plugCount} 个第三方插件。`
        + '选一个全新版本（推荐）或导入已有 lane：'),
      modeRow(modeNew, '新建 lane 并安装 dsh', el('div', 'two-col', newName, version)),
      modeRow(modeOld, '导入到已有 lane', laneSel),
      el('div', 'field',
        el('div', 'field-label', '恢复内容'),
        optSessions.node, optSettings.node, optCred.node, optData.node),
      planLine,
    );
    newName.value = `${data.source.lane || 'lane'}-imported`;
    refreshPlan();
    ctl.setFoot([
      { text: '返回列表', act: (c) => renderCfgList(c) },
      {
        text: '开始恢复', kind: 'primary',
        act: async (c) => {
          const yes = await confirmBox({
            head: '从备份导入',
            text: modeNew.checked
              ? `新建 lane「${newName.value.trim()}」并安装 dsh、装回插件？`
              : `把配置导入已有 lane「${laneSel.value}」？它的会话会先另存、同名设置先留 .bak。`,
            ok: '开始',
          });
          if (!yes || c.closed) return;
          startJob(c, `配置恢复：${name}`, 'cfg_restore', {
            name,
            newLane: modeNew.checked ? newName.value.trim() : '',
            target: modeNew.checked ? '' : laneSel.value,
            version: version.value.trim(),
            sessions: optSessions.get(),
            settings: optSettings.get(),
            credentials: optCred.get(),
            pluginData: optData.get(),
          });
        },
      },
    ]);
  });
}

/* ── ⑦ 复制这套 DSH ─────────────────────────────────────────────────────── */

function dlgClone(lane) {
  const nameBox = textInput(`${lane}-copy`, '新 lane 名');
  const sizeLine = el('p', 'field-note', '正在估算体积…');
  const profBox = el('div', 'field');
  const profCount = el('p', 'field-note');
  const ctl = openDialog({
    head: `复制「${lane}」生成副本`,
    size: 'lg',
    build: (c) => {
      c.body.append(el('p', 'dlg-lead',
        '副本带走这套的全部状态：会话记录、设置、已装插件、API key。'
        + '副本有自己的安装树和 DSH_HOME —— 升级副本不会碰到原件，出问题删掉副本就行。'));
      c.body.append(noteLine(
        '典型用法：复制主要版本 → 升级副本 → 点「核对隔离」→ 看插件还都在不在。', 'info'));
      c.body.append(field('新 lane 名', nameBox));
      c.body.append(profBox);
      c.body.append(sizeLine);
    },
    foot: [
      { text: '取消', act: (c) => c.close() },
      { text: '开始复制', kind: 'primary', act: (c) => doClone(c) },
    ],
  });

  let timer = null;
  let planOk = true;
  let profBoxes = null;          // 名字 → checkInput；第一次拿到方案时建出来

  /** 勾了哪些 profile。null ＝"还没建出来/不挑"（后端按全部处理） */
  function selectedProfiles() {
    if (!profBoxes) return null;
    return [...profBoxes.entries()].filter(([, box]) => box.get()).map(([name]) => name);
  }

  /** 建一次勾选清单（只在第一次拿到方案时建，之后只更新，不动用户已经勾的） */
  function buildProfBox(profiles) {
    if (profBoxes) return;
    profBoxes = new Map();
    const picks = el('div', 'pick-list');
    if (!profiles.length) {
      picks.append(el('p', 'field-note',
        '（这条 lane 的 HOME 里还没有任何 profile —— 副本会是个空环境，首次打开时初始化）'));
    }
    for (const name of profiles) {
      const row = el('div', 'pick-row');
      const box = checkInput('', true);
      const main = el('div', 'pick-main');
      main.append(el('div', 'pick-title', name));
      main.append(el('div', 'pick-sub',
        'HOME\\profiles\\' + name + '（含这一套插件组合）'));
      row.append(box.node, main);
      picks.append(row);
      profBoxes.set(name, box);
      box.onChange(() => {
        const count = selectedProfiles().length;
        setText(profCount, `已选 ${count} / ${profBoxes.size} 个　·　`
          + '没选的 profile 不会进副本');
        ctl.refreshFoot();
        clearTimeout(timer);
        timer = setTimeout(loadPlan, 400);        // 体积要重算（只算选中的）
      });
    }
    const bar = el('div', 'pick-bar');
    const btnAll = button('btn small', '全选');
    const btnNone = button('btn small', '全不选');
    btnAll.addEventListener('click', () => {
      for (const box of profBoxes.values()) box.set(true);
      refreshProfCount();
    });
    btnNone.addEventListener('click', () => {
      for (const box of profBoxes.values()) box.set(false);
      refreshProfCount();
    });
    bar.append(btnAll, btnNone);
    profBox.append(el('div', 'field-label', '要一起复制过来的 profile（环境）'),
      picks, bar, profCount);
  }

  function refreshProfCount() {
    if (!profBoxes) return;
    setText(profCount, `已选 ${selectedProfiles().length} / ${profBoxes.size} 个　·　`
      + '没选的 profile 不会进副本');
    ctl.refreshFoot();
    clearTimeout(timer);
    timer = setTimeout(loadPlan, 400);
  }

  async function loadPlan() {
    const name = nameBox.value.trim() || `${lane}-copy`;
    setText(sizeLine, '正在估算体积…（要把两个目录都走一遍）');
    sizeLine.className = 'field-note';
    try {
      const plan = await api('clone_plan', {
        lane, name, profiles: selectedProfiles(),
      });
      if (ctl.closed) return;
      planOk = true;
      buildProfBox(plan.profiles || []);
      const parts = [`预计复制 ${plan.humanTotal}（安装树 ${plan.humanInstall}`
        + ` + HOME ${plan.humanHome}）`];
      if (plan.profilesSkipped && plan.profilesSkipped.length) {
        parts.push(`整份 HOME 是 ${plan.humanHomeAll}，跳过 `
          + plan.profilesSkipped.join('、'));
      }
      if (plan.free) parts.push(`目标盘剩余 ${plan.free}`);
      if (plan.problems.length) parts.push('问题：' + plan.problems.join('；'));
      if (plan.exists) { parts.push(`lane「${name}」已经存在`); planOk = false; }
      setText(sizeLine, parts.join('，'));
      sizeLine.className = plan.problems.length || !planOk ? 'field-note warn' : 'field-note';
      refreshProfCount();
    } catch (err) {
      if (ctl.closed) return;
      planOk = true;
      setText(sizeLine, `体积估算失败：${err.message}`);
      sizeLine.className = 'field-note warn';
    }
  }

  async function doClone(c) {
    const name = nameBox.value.trim();
    if (!name) { toast('先填新 lane 名', 'err'); nameBox.focus(); return; }
    if (name === lane) { toast('新 lane 名不能和源一样', 'err'); nameBox.focus(); return; }
    if (!LANE_NAME_RE.test(name)) {
      toast('名称只能用字母/数字开头，含字母 数字 . _ -（最长 32 字符）', 'err', 4200);
      nameBox.focus();
      return;
    }
    if (S.laneMap.has(name)) { toast(`lane「${name}」已经存在了`, 'err', 4200); return; }
    if (!planOk) {
      const yes = await confirmBox({
        head: '复制',
        text: '体积估算里报了问题（见下面那行）。还要继续吗？',
        ok: '继续',
        danger: true,
      });
      if (!yes || c.closed) return;
    }
    const picked = selectedProfiles();
    if (profBoxes && !picked.length) {
      toast('一个 profile 都没选 —— 至少留一个（不想挑就「全选」）', 'err', 4200);
      return;
    }
    startJob(c, `复制 ${lane} → ${name}`, 'clone',
      { lane, name, profiles: picked });
  }

  nameBox.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(loadPlan, 500);
  });
  loadPlan();
  return ctl;
}

/* ── ⑧ 升级版本 ─────────────────────────────────────────────────────────── */

function dlgUpgrade(lane) {
  const ctl = openDialog({
    head: `换 DSH 版本：lane「${lane}」`,
    size: 'xl',
    build: (c) => { c.body.append(spinnerRow('正在读取升级方式…')); },
    foot: [{ text: '关闭', act: (c) => c.close() }],
  });
  loadDialog(ctl, '读取升级方式', 'upgrade_plan', { lane }, (plan) => buildUpgrade(ctl, lane, plan));
  return ctl;
}

function buildUpgrade(ctl, lane, plan) {
  const reg = registryRow(plan.registry);
  const verBox = textInput('', '在上面挑一版，或手打 0.1.7-rc.2 / latest');
  const hint = el('p', 'field-note', '正在查询 registry 上的版本…');
  const listBox = el('div', 'opt-list');
  let rows = [];
  let current = plan.version;

  const head = el('p', 'dlg-lead');
  setText(head, `当前版本  ${plan.version}\n安装树    ${plan.dir}\n升级方式  ${plan.why}`
    + (plan.running ? `\n它现在正在运行（pid ${plan.running.pid}，端口 ${plan.running.port}）`
      + ' —— 升级会先停掉它，升完再起回来。' : ''));

  ctl.body.append(head);
  if (plan.external) {
    ctl.body.append(noteLine(
      '这条 lane 的安装树在本启动器目录之外（你日常真正在用的那一套）—— 这个窗口有意不动它。'
      + '要动它得单独一步、单独确认。', 'warn'));
  }
  ctl.body.append(reg.node);
  ctl.body.append(el('div', 'field-label', '从 registry 挑一版（点一下填进下面的「版本」，也可以自己手打）'));
  ctl.body.append(listBox, field('版本', verBox), hint);

  function renderList(data) {
    clear(listBox);
    rows = data.items || [];
    if (!rows.length) {
      listBox.append(el('div', 'opt-empty', '没查到版本 —— 可以直接手输版本号。'));
      return;
    }
    for (const item of rows) {
      const row = el('div', 'opt-row');
      row.append(el('span', null, item.v));
      if (item.tags.length) row.append(el('span', 'tag', `(${item.tags.join(',')})`));
      if (item.marks.length) row.append(el('span', 'mark', '← ' + item.marks.join('、')));
      row.addEventListener('click', () => {
        verBox.value = item.v;
        for (const other of listBox.children) other.removeAttribute('aria-selected');
        row.setAttribute('aria-selected', 'true');
      });
      listBox.append(row);
    }
    const first = listBox.firstElementChild;
    if (first) first.setAttribute('aria-selected', 'true');
    if (!verBox.value.trim() && data.newest) verBox.value = data.newest;
    hint.className = 'field-note';
    setText(hint, `来源 ${data.label}（${data.reg}）｜registry 最新 ${data.newest || '?'}`
      + (data.newest === current ? '（就是你现在这个）' : ' ← 比你现在的新'));
  }

  async function load() {
    hint.className = 'field-note';
    setText(hint, '正在查询 registry 上的版本…');
    clear(listBox);
    listBox.append(el('div', 'opt-empty', '读取中…'));
    try {
      const data = await api('registry_versions', { registry: reg.get(), current });
      if (ctl.closed) return;
      if (!data.ok) throw new Error(data.error || '查询失败');
      renderList(data);
    } catch (err) {
      if (ctl.closed) return;
      clear(listBox);
      listBox.append(el('div', 'opt-empty', '没读到版本 —— 可以直接手输版本号。'));
      hint.className = 'field-note warn';
      setText(hint, `查询失败：${err.message}（自己手打版本号也行）`);
    }
  }

  async function doUpgrade(c, rollback, dryRun) {
    const version = rollback ? null : (verBox.value.trim().split(/\s+/)[0] || '');
    if (rollback) {
      if (!plan.previousVersion) {
        toast('这条 lane 还没有升级记录，没有可退回的版本。', 'err', 4200);
        return;
      }
    } else if (!version) {
      toast('先填版本号（例如 0.1.7-rc.2 / latest）', 'err', 4200);
      verBox.focus();
      return;
    }
    if (!dryRun) {
      const text = rollback
        ? `把「${lane}」退回 ${plan.previousVersion}？\n\n会做：换回那棵树 → 核对 → 启动冒烟；起不来自动退回。`
        : `把「${lane}」升到 ${version}？\n\n`
          + `当前 ${current} → 装到 ${plan.dir}\n`
          + `下载源：${REGISTRY_OPTIONS.find((o) => o.value === reg.get()).label}\n`
          + '会做：升级 → 核对 → 启动冒烟；起不来自动退回原版本。';
      const yes = await confirmBox({
        head: rollback ? '退回上一版' : '升级',
        text,
        ok: rollback ? '退回' : '升级',
        danger: !rollback,
      });
      if (!yes || c.closed) return;
    }
    startJob(c, `升级 ${lane}${dryRun ? '（只看会做什么）' : ''}`, 'upgrade', {
      lane, version, rollback, dryRun,
      registry: reg.get(), remember: dryRun ? false : reg.remember(),
    });
  }

  reg.node.addEventListener('change', () => load());

  const foot = [{ text: '关闭', act: (c) => c.close() }];
  foot.push({ text: '先看会做什么', kind: 'link', act: (c) => doUpgrade(c, false, true) });
  if (plan.previousVersion) {
    foot.push({
      text: `退回上一版（${plan.previousVersion}）`,
      kind: 'link',
      act: (c) => doUpgrade(c, true, false),
    });
  }
  if (plan.external) {
    // 外部安装（你日常真正在用的那一套）：这个窗口有意不动它，所以没有"升级"按钮
    foot.push({ text: '外部安装：本窗口不动它', kind: 'link', enabled: () => false });
  } else {
    foot.push({ text: '升级到这一版', kind: 'primary', act: (c) => doUpgrade(c, false, false) });
  }
  ctl.setFoot(foot);

  load();
}

/* ── ⑨ 删除这套 DSH ─────────────────────────────────────────────────────── */

function dlgDelete(lane) {
  const ctl = openDialog({
    head: `删除 DSH · ${lane}`,
    size: 'lg',
    build: (c) => { c.body.append(spinnerRow('正在算清会动哪些文件…')); },
    foot: [{ text: '取消', act: (c) => c.close() }],
  });
  loadDialog(ctl, '计算删除范围', 'delete_plan', { lane }, (plan) => buildDelete(ctl, lane, plan));
  return ctl;
}

function buildDelete(ctl, lane, plan) {
  const canInstall = plan.installInside && !plan.sharedWith.length;
  const delInstall = checkInput('', canInstall);
  const keepHome = checkInput('', false);      // HOME 在根目录内：勾＝保留
  const dangerHome = checkInput('', false);    // HOME 在根目录外：勾＝一起删
  const stopRun = checkInput('', true);
  const confirmBoxEl = textInput('', `手打「${lane}」确认`);

  const list = el('div', 'rows');
  const sizeNodes = { install: null, home: null };

  function plainRow(text, note) {
    const row = el('div', 'cols-del');
    row.append(el('span', null, ''));
    const main = el('div', 'pick-main');
    main.append(el('div', null, note ? `${text}　（${note}）` : text));
    row.append(main);
    list.append(row);
    return main;
  }

  function pickRow(text, box, why, cls) {
    const row = el('div', 'cols-del');
    row.append(box ? box.node : el('span', null, ''));
    const main = el('div', 'pick-main');
    main.append(el('div', null, text));
    if (why) main.append(el('div', 'why' + (cls ? ' ' + cls : ''), why));
    row.append(main);
    list.append(row);
    return main;
  }

  const title = el('p', 'danger-zone');
  setText(title, `删除这套 DSH\n`
    + `lane「${lane}」　·　版本 ${plan.version}`
    + (plan.profiles.length ? `　·　profiles：${plan.profiles.join('、')}` : '')
    + (plan.adopted ? '　·　接管型（安装树在 npm 全局，本启动器不会删它）' : ''));

  ctl.body.append(title, el('p', 'field-label', '将删除 / 保留'), list);

  plainRow(`登记信息　lane「${lane}」`, '必删');
  for (const path of plan.runs) plainRow(`运行态　　${path.split('\\').pop()}`, '必删');
  for (const path of plan.logs) plainRow(`启动日志　${path.split('\\').pop()}`, '必删');

  if (canInstall) {
    const node = pickRow('安装树　　（计算中…）', delInstall);
    sizeNodes.install = node.firstChild;
  } else {
    const why = plan.adopted
      ? '保留 —— 接管型：删的是 npm 全局那份，要卸载请用 npm uninstall -g'
      : `保留 —— 其它 lane 还在共用这棵树：${plan.sharedWith.join('、')}`;
    pickRow(`安装树　　${plan.install}`, null, why);
  }

  if (plan.homeInside) {
    const node = pickRow('DSH_HOME　（计算中…）', keepHome,
      '勾上＝保留 DSH_HOME（只删安装树和登记，会话记录留着）');
    sizeNodes.home = node.firstChild;
  } else {
    const node = pickRow(`DSH_HOME　${plan.home}`, dangerHome);
    sizeNodes.home = node.firstChild;
    pickRow('★ 它不在 lane 根目录里（像是你现有的 HOME），里面有真实会话记录、设置和插件，'
      + '默认保留。确实要一起删才勾上面的框 —— 删了无法恢复。', null, null, 'bad');
  }

  const running = plan.running;
  if (running) {
    ctl.body.append(el('div', 'danger-zone',
      `它正在运行（PID ${running.pid}，端口 ${running.port}）—— 删除前必须先停止它。`));
    ctl.body.append(el('div', 'check-group', stopRun.node));
  }

  ctl.body.append(el('p', 'field-label', `请输入 lane 名称 ${lane} 以确认：`), confirmBoxEl);
  const hint = el('p', 'field-note');
  ctl.body.append(hint);

  function collect() {
    let keep = true;
    let homeToo = false;
    if (plan.homeInside) {
      keep = keepHome.get();
      homeToo = false;
    } else {
      keep = true;
      homeToo = dangerHome.get();
    }
    const keepInstall = canInstall ? !delInstall.get() : true;
    return { keepHome: keep, homeToo, keepInstall };
  }

  function refresh() {
    const nameOk = confirmBoxEl.value.trim() === lane;
    const runOk = !running || stopRun.get();
    const { keepHome: keep, homeToo, keepInstall } = collect();
    const bits = [keepInstall ? '保留安装树' : '删除安装树'];
    if (plan.homeInside) bits.push(keep ? '保留 DSH_HOME' : '删除 DSH_HOME');
    else if (homeToo) bits.push('连现有 DSH_HOME 一起删除');
    else bits.push('保留现有 DSH_HOME');
    if (!nameOk) {
      hint.className = 'field-note';
      setText(hint, `为避免误删，请完整输入「${lane}」`);
    } else if (!runOk) {
      hint.className = 'field-note err';
      setText(hint, '它正在运行，必须先勾上「先停止它」');
    } else {
      hint.className = 'field-note ok';
      setText(hint, '确认无误：' + bits.join('、'));
    }
    ctl.refreshFoot();
  }

  for (const box of [delInstall, keepHome, dangerHome, stopRun]) box.onChange(refresh);
  confirmBoxEl.addEventListener('input', refresh);

  ctl.setFoot([
    { text: '取消', act: (c) => c.close() },
    {
      text: '删除',
      kind: 'danger',
      enabled: () => confirmBoxEl.value.trim() === lane && (!running || stopRun.get()),
      act: (c) => {
        const { keepHome: keep, homeToo, keepInstall } = collect();
        startJob(c, `删除 ${lane}`, 'delete', {
          lane,
          stop: !!running && stopRun.get(),
          keepHome: keep,
          keepInstall,
          homeToo,
        });
      },
    },
  ]);

  // 体积是单独一步（全盘遍历几百毫秒起步），别卡住上面这份清单
  api('delete_sizes', { lane }).then((sizes) => {
    if (ctl.closed || !sizes) return;
    if (sizeNodes.install) {
      setText(sizeNodes.install, `安装树　　${plan.install}　（${sizes.install}）`);
    }
    if (sizeNodes.home) {
      setText(sizeNodes.home, plan.homeInside
        ? `DSH_HOME　${plan.home}　（${sizes.home}）`
        : `DSH_HOME　${plan.home}　（${sizes.home}）`);
    }
  }).catch(() => {
    if (sizeNodes.install) setText(sizeNodes.install, `安装树　　${plan.install}`);
    if (sizeNodes.home) setText(sizeNodes.home, `DSH_HOME　${plan.home}`);
  });

  refresh();
  confirmBoxEl.focus();
}

/* ── 顶部按钮 ───────────────────────────────────────────────────────────── */

$('#btn-new').addEventListener('click', () => dlgCreate());
$('#btn-plugins').addEventListener('click', () => dlgPlugins());
$('#btn-cfg').addEventListener('click', () => dlgCfg());
$('#btn-refresh').addEventListener('click', () => act('刷新', 'refresh', {}));
$('#btn-root').addEventListener('click', () => act('打开根目录', 'open_path', { which: 'root' }));
$('#btn-doctor').addEventListener('click', () => act('环境自检', 'doctor', {}));
$('#btn-stop-all').addEventListener('click', () => act('全部停止', 'stop_all', {}));
$('#repo-link').addEventListener('click', () => act('打开代码库', 'open_repo', {}));

document.addEventListener('keydown', (event) => {
  if (event.ctrlKey && (event.key === 'l' || event.key === 'L')) {
    event.preventDefault();
    $('#btn-log-clear').click();
  }
});

/* ── 窗口几何上报 ──────────────────────────────────────────────────────── */

let lastGeometry = null;
let geoTimer = null;

function currentGeometry() {
  return {
    w: Math.round(window.outerWidth || 0),
    h: Math.round(window.outerHeight || 0),
    x: Math.round(window.screenX || 0),
    y: Math.round(window.screenY || 0),
  };
}

function sendGeometry(beacon) {
  const geo = currentGeometry();
  if (geo.w < 400 || geo.h < 300) return;      // 最小化/异常状态不记
  const same = lastGeometry
    && lastGeometry.w === geo.w && lastGeometry.h === geo.h
    && lastGeometry.x === geo.x && lastGeometry.y === geo.y;
  if (same) return;
  lastGeometry = geo;
  if (beacon && navigator.sendBeacon) {
    // 关窗口那一刻用 sendBeacon：fetch 会被浏览器直接掐掉
    navigator.sendBeacon('/api/geometry', new Blob([JSON.stringify(geo)], { type: 'application/json' }));
    return;
  }
  api('geometry', geo).catch(() => {});
}

window.addEventListener('resize', () => {
  clearTimeout(geoTimer);
  geoTimer = setTimeout(() => sendGeometry(false), 400);
});

window.addEventListener('pagehide', () => {
  sendGeometry(true);
  if (navigator.sendBeacon) navigator.sendBeacon('/api/bye', new Blob(['{}'], { type: 'application/json' }));
});

setTimeout(() => sendGeometry(false), 1200);

/* ── 连上 / 断线 ───────────────────────────────────────────────────────── */

const bootEl = $('#boot');

function showBoot(text, hint) {
  bootEl.hidden = false;
  bootEl.classList.remove('gone');
  setText($('#boot-text'), text);
  setText($('#boot-hint'), hint || '界面通过本机回环地址与后端通信，不会联网。');
}

function hideBoot() {
  if (bootEl.hidden) return;
  bootEl.classList.add('gone');
  setTimeout(() => { bootEl.hidden = true; }, 320);
}

let eventSource = null;

function connect() {
  eventSource = new EventSource('/api/events');

  eventSource.addEventListener('hello', (event) => {
    const data = JSON.parse(event.data);
    S.connected = true;
    resetLog(data.lines || []);
    hideBoot();
  });

  eventSource.addEventListener('state', (event) => applyState(JSON.parse(event.data)));
  eventSource.addEventListener('remote', (event) => applyRemote(JSON.parse(event.data)));
  eventSource.addEventListener('log', (event) => appendLog(JSON.parse(event.data).lines || []));
  eventSource.addEventListener('log_clear', () => resetLog([]));
  // 任务结束：开着的对话框靠它决定"该重新读一遍了"（凭据 / 备份对话 / 插件库…）
  eventSource.addEventListener('job_done', (event) => notifyJobDone(JSON.parse(event.data)));
  eventSource.addEventListener('busy', (event) => {
    const data = JSON.parse(event.data);
    S.busy = !!data.busy;
    S.busyTitle = data.title || '';
    renderStatus();
  });

  eventSource.addEventListener('open', () => {
    S.connected = true;
    hideBoot();
  });

  eventSource.addEventListener('error', () => {
    if (eventSource.readyState === EventSource.CLOSED) {
      showBoot('与启动器服务的连接已关闭', '启动器进程可能已经退出。关掉这个窗口，重新运行 dsh_lanes_web.py 即可。');
      return;
    }
    showBoot('与启动器服务的连接断了，正在自动重连…');
  });
}

/* ── 帧率表（?debug=1） ────────────────────────────────────────────────── */

function startFpsMeter() {
  const box = $('#fps');
  box.hidden = false;
  const value = el('b', null, '—');
  const rest = el('span');
  box.append(value, rest, el('div', 'fps-note', '?debug=1 时显示'));

  let last = performance.now();
  let frames = 0;
  let dropped = 0;
  let since = last;
  let samples = 0;

  function tick(now) {
    const dt = now - last;
    last = now;
    frames += 1;
    if (dt > 34 && since > 0) dropped += 1;      // 预期 16.7ms，超过两帧算掉帧
    if (now - since >= 500) {
      const fps = Math.round((frames * 1000) / (now - since));
      setText(value, String(fps));
      setText(rest, ` fps · 掉帧 ${dropped}`);
      setClass(box, 'warn', fps < 50);
      frames = 0;
      dropped = 0;
      since = now;
      samples += 1;
      if (samples === 6) {
        // 报一次给后端：自动化核对"页面真的在动"用（无头下的数值不代表真实帧率）
        api('client_metric', { fps, dropped }).catch(() => {});
      }
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

/* ── 起 ─────────────────────────────────────────────────────────────────── */

if (new URLSearchParams(location.search).has('debug')) startFpsMeter();

window.addEventListener('error', (event) => {
  reportClientError(event.message, `${event.filename || ''}:${event.lineno || 0}`);
});
window.addEventListener('unhandledrejection', (event) => {
  const reason = event.reason;
  reportClientError((reason && reason.message) || String(reason), 'unhandledrejection');
});

let reportedErrors = 0;
function reportClientError(message, where) {
  if (reportedErrors > 20) return;      // 别让一个循环出错把后端刷爆
  reportedErrors += 1;
  api('client_error', { message: String(message || ''), where: String(where || '') }).catch(() => {});
}

renderHeader();

// 首屏先用一次普通 GET 把状态拿到（不等 SSE 建连）：SSE 断线重连期间也不至于只有骨架屏。
// 只在还没拿到状态时才应用，免得一条慢回来的响应把 SSE 推来的新状态盖回去。
(async () => {
  try {
    const resp = await fetch('/api/state');
    if (resp.ok && !S.ready) applyState(await resp.json());
  } catch (_) { /* SSE 那条路会接手 */ }
})();

connect();

})();
