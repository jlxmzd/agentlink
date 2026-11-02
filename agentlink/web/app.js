'use strict';

const TOKEN = document.querySelector('meta[name="agentlink-token"]').content;
const AGENTS = {
  codex: { label: 'Codex', color: 'var(--codex)', duty: '总控 · 设计与主要功能' },
  claude: { label: 'Claude Code', color: 'var(--claude)', duty: '编码专家 · 疑难代码与整改' },
  deepseek: { label: 'DeepSeek（dsh）', color: 'var(--deepseek)', duty: '主力执行 · 可调度子智能体' },
};
const STATE_LABEL = {
  posted: '待领取', claimed: '进行中', delivered: '待验收', rework: '返工', blocked: '受阻',
  ai_review: 'AI 审查中', accepted: '已验收', cancelled: '已取消',
};
const AI_STATUS = {
  pending: '待投递', sending: '投递中', sent: '审查中', accepted: '审查通过', rejected: '审查驳回',
  failed: '未送达', uncertain: '投递不确定',
};
const $ = sel => document.querySelector(sel);
const state = { projects: [], current: null, sessions: null, picks: {}, reviewPick: null, reviewEnabled: false, search: {}, open: {}, tasks: null, hosts: null, tab: 'bind' };

// -- helpers -------------------------------------------------------------------
async function api(path, body) {
  const init = { headers: { 'X-AgentLink-Token': TOKEN } };
  if (body !== undefined) {
    init.method = 'POST';
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({ error: res.statusText }));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

// Build DOM without innerHTML so session titles and task text can never inject markup.
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'style') node.style.cssText = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, '');
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c !== undefined && c !== null && c !== false) node.append(c instanceof Node ? c : String(c));
  return node;
}

let toastTimer;
function toast(text, isError = false) {
  const t = $('#toast');
  t.textContent = text;
  t.className = 'toast' + (isError ? ' err' : '');
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, isError ? 6000 : 3000);
}

const when = iso => {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
};
const params = obj => '?' + new URLSearchParams(obj).toString();

// -- projects ------------------------------------------------------------------
async function loadProjects() {
  state.projects = await api('/api/projects');
  renderProjects();
}

function renderProjects() {
  const list = $('#projects');
  list.replaceChildren(...state.projects.map(p => el('li', {},
    el('button', {
      type: 'button', 'aria-current': state.current && state.current.root === p.root ? 'true' : 'false',
      onclick: () => openProject(p.root),
    },
      p.name,
      el('span', { class: 'trio', title: '已绑定的参与方' },
        Object.keys(AGENTS).map(a => el('i', { class: p.bindings[a] ? a : '' }))),
      el('span', { class: 'sub mono' }, p.root)))));
  if (!state.projects.length) list.append(el('li', { class: 'muted' }, '还没有项目'));
}

async function openProject(root) {
  state.current = await api('/api/project' + params({ root }));
  state.picks = {};
  for (const a of Object.keys(AGENTS)) {
    const b = state.current.bindings[a];
    state.picks[a] = b ? { id: b.id, title: b.title } : null;
  }
  const review = state.current.options.aiReviewSession;
  state.reviewPick = review && review.id ? { id: review.id, title: review.title || '' } : null;
  state.reviewEnabled = !!state.current.options.aiReviewEnabled;
  state.roleEdits = {};                                        // switching projects must not carry drafts over
  state.search = {};                                           // ... nor search terms or expanded pickers
  state.open = {};
  state.sessions = null;
  state.tasks = null;
  $('#empty').hidden = true;
  $('#project').hidden = false;
  $('#pname').textContent = state.current.name;
  $('#proot').textContent = state.current.root;
  const o = state.current.options;
  $('#opt-name').value = state.current.name;
  $('#opt-integrator').value = o.integrator;
  $('#opt-lead').value = o.lead;
  for (const k of ['agentsMd', 'claudeMd', 'gitignore', 'autoEscalate']) $('#opt-' + k).checked = !!o[k];
  $('#apply-result').textContent = state.current.confirmed ? '上次确认：' + when(state.current.confirmed) : '尚未确认';
  renderProjects();
  renderAgents();
  loadSessions();
  if (state.tab === 'tasks') loadTasks();
  if (state.tab === 'hosts') loadHosts();
  if (state.tab === 'settings') loadSettings();
}

$('#add-project').addEventListener('submit', async ev => {
  ev.preventDefault();
  const root = $('#add-root').value.trim();
  try {
    const p = await api('/api/project/add', { root });
    $('#add-root').value = '';
    await loadProjects();
    await openProject(p.root);
  } catch (e) { toast(e.message, true); }
});

// -- binding -------------------------------------------------------------------
async function loadSessions() {
  if (!state.current) return;
  const root = state.current.root;
  state.sessions = null;
  renderAgents();
  try {
    const found = await api('/api/sessions' + params({ root, all: $('#show-all').checked ? '1' : '0' }));
    if (state.current && state.current.root === root) {
      state.sessions = found;
      // Refresh labels by ID without changing the user's selected conversations.
      const refreshTitle = (pick, source) => {
        const live = pick && source && !source.error && source.items.find(i => i.id === pick.id);
        if (live && typeof live.title === 'string') pick.title = live.title;
      };
      for (const agent of Object.keys(AGENTS)) refreshTitle(state.picks[agent], found[agent]);
      refreshTitle(state.reviewPick, found.deepseek);
      renderAgents();
    }
  } catch (e) { toast(e.message, true); }
}

function renderAgents() {
  const box = $('#agents');
  box.replaceChildren(...Object.entries(AGENTS).map(([agent, info]) => agentCard(agent, info)), reviewCard());
}

const shortId = id => (id && id.length > 13 ? id.slice(0, 13) + '…' : id || '');
const matchesTerm = (item, term) => !term || ((item.title || '') + ' ' + item.id).toLowerCase().includes(term.toLowerCase());

// One collapsed picker per role: a search box and a scrollable list keep the four cards short.
function pickerBlock(agent, label, items, found, pick, chooseFn) {
  const list = el('ul', { class: 'list', role: 'radiogroup', 'aria-label': label });
  const body = [];
  if (!found) body.push(el('p', { class: 'note' }, '读取对话中…'));
  else {
    body.push(el('input', {
      class: 'search mono', type: 'search', value: state.search[agent] || '', placeholder: '搜索标题或 ID',
      'aria-label': label + '：搜索', oninput: ev => { state.search[agent] = ev.target.value; draw(); },
    }));
    body.push(list);
  }
  if (found && found.error) body.push(el('p', { class: 'note err' }, '无法读取：' + found.error));
  const draw = () => {
    const term = state.search[agent] || '';
    const shown = (items || []).filter(i => i.missing || matchesTerm(i, term));
    const rows = [el('li', { class: !pick ? 'picked' : '' }, el('label', {},
      el('input', { type: 'radio', name: 'pick-' + agent, checked: !pick, onchange: () => chooseFn(null) }),
      el('span', {}, '不绑定'), el('span', { class: 'meta' }, '该角色不参与本项目')) )];
    if (!shown.length) rows.push(el('li', { class: 'empty' }, el('span', { class: 'meta' }, '没有匹配的对话')));
    shown.forEach(i => {
      rows.push(el('li', { class: pick && pick.id === i.id ? 'picked' : '' },
        el('label', {},
          el('input', { type: 'radio', name: 'pick-' + agent, checked: !!(pick && pick.id === i.id), onchange: () => chooseFn(i) }),
          el('span', {}, i.title || '（无标题）', i.running ? el('span', { class: 'running' }, ' · 运行中') : ''),
          el('span', { class: 'meta mono', title: i.cwd || '' },
            i.missing ? '未在当前列表中找到 · ' + i.id : [when(i.updated), shortId(i.id)].filter(Boolean).join(' · ')))));
    });
    list.replaceChildren(...rows);
  };
  draw();
  return el('details', { class: 'picker', open: state.open[agent] || undefined,
    ontoggle: ev => { state.open[agent] = ev.target.open; } },
    el('summary', {}, pick ? '更换对话' : '选择对话'), ...body);
}

function advancedBlock(agent, pick, roleText, chooseFn) {
  const manualId = 'manual-' + agent;
  const kids = [
    pick ? el('div', { class: 'full mono' }, pick.id) : null,
    el('label', { class: 'manual', for: manualId }, '手动填写会话 ID',
      el('input', {
        id: manualId, class: 'mono', placeholder: '会话 / 线程 ID',
        onchange: ev => { const v = ev.target.value.trim(); if (v) chooseFn({ id: v, title: '' }); },
      })),
  ];
  if (roleText !== undefined) kids.push(el('label', {}, '分工说明', el('textarea', {
    rows: 2, 'data-role': agent,
    oninput: ev => { state.roleEdits = Object.assign(state.roleEdits || {}, { [agent]: ev.target.value }); },
  }, roleText)));
  return el('details', { class: 'advanced' },
    el('summary', {}, roleText === undefined ? '高级：完整 ID / 手动填写' : '高级：完整 ID / 手动填写 / 分工'), ...kids);
}

function agentCard(agent, info) {
  const pick = state.picks[agent];
  const found = state.sessions ? state.sessions[agent] : null;
  const items = found ? found.items.slice() : [];
  // Keep the current binding visible even when it no longer shows up in discovery; it stays marked as missing.
  if (pick && !items.some(i => i.id === pick.id)) items.unshift({ id: pick.id, title: pick.title || '（当前绑定）', missing: true });
  return el('article', { class: 'agent', style: '--c:' + info.color },
    el('h3', {}, info.label, el('span', { class: 'state' }, pick ? '已选择' : '未绑定')),
    el('div', { class: 'picked mono', title: pick ? pick.id : '' }, pick ? (pick.title || pick.id) : '未选择会话'),
    pick ? el('div', { class: 'sid mono' }, shortId(pick.id)) : null,
    el('div', { class: 'duty' }, info.duty),
    pickerBlock(agent, info.label + ' 的对话', items, found, pick, item => choose(agent, item)),
    advancedBlock(agent, pick, (state.roleEdits && state.roleEdits[agent]) ?? state.current.roles[agent], item => choose(agent, item)));
}

// The DeepSeek review conversation is a display card only: it is not an executor, so it stays out of AGENTS,
// out of the dispatch targets and out of the host installer.
function reviewCard() {
  const pick = state.reviewPick;
  const exec = state.picks.deepseek;
  const found = state.sessions ? state.sessions.deepseek : null;
  // Selecting the same conversation for execution and review would make the reviewer review itself.
  const items = found ? found.items.filter(i => !exec || i.id !== exec.id).slice() : [];
  if (pick && !items.some(i => i.id === pick.id)) items.unshift({ id: pick.id, title: pick.title || '（当前选择）', missing: true });
  return el('article', { class: 'agent review', style: '--c:' + (AGENTS.deepseek ? AGENTS.deepseek.color : 'var(--accent)') },
    el('h3', {}, 'DeepSeek 验收', el('span', { class: 'state' }, pick ? (state.reviewEnabled ? '已启用' : '已选择 · 未启用') : '未绑定')),
    el('div', { class: 'picked mono', title: pick ? pick.id : '' }, pick ? (pick.title || pick.id) : '未选择对话'),
    pick ? el('div', { class: 'sid mono' }, shortId(pick.id)) : null,
    el('div', { class: 'duty' }, '独立 AI 审查 · 只审不改，通过后交发起方验收'),
    el('label', { class: 'check' }, el('input', {
      type: 'checkbox', id: 'review-enabled', checked: !!state.reviewEnabled, disabled: !pick,
      onchange: ev => { state.reviewEnabled = ev.target.checked; renderAgents(); },
    }), '启用 AI 验收'),
    pickerBlock('review', 'DeepSeek 验收对话', items, found, pick, chooseReview),
    exec ? el('p', { class: 'note fine' }, '执行对话 ' + (exec.title || shortId(exec.id)) + ' 已从列表中排除') : null,
    advancedBlock('review', pick, undefined, chooseReview));
}

function chooseReview(item) {
  if (item && state.picks.deepseek && item.id === state.picks.deepseek.id) {
    return toast('验收对话不能与 DeepSeek 执行对话相同', true);
  }
  state.reviewPick = item ? { id: item.id, title: item.title || '' } : null;
  state.reviewEnabled = !!item;
  state.open.review = false; state.search.review = '';
  renderAgents();
}

function choose(agent, item) {
  state.picks[agent] = item ? { id: item.id, title: item.title || '' } : null;
  if (agent === 'deepseek' && item && state.reviewPick && state.reviewPick.id === item.id) {
    state.reviewPick = null;
    state.reviewEnabled = false;
    toast('DeepSeek 验收对话不能与执行对话相同，已清空验收选择', true);
  }
  state.open[agent] = false; state.search[agent] = '';
  renderAgents();
}

$('#show-all').addEventListener('change', loadSessions);
$('#refresh-sessions').addEventListener('click', loadSessions);

$('#apply').addEventListener('click', async () => {
  const bound = Object.values(state.picks).filter(Boolean).length;
  if (bound < 2 && !confirm('只绑定了 ' + bound + ' 方，协作至少需要两方。仍然确认吗？')) return;
  const button = $('#apply');
  button.disabled = true;
  try {
    const roles = {};
    document.querySelectorAll('textarea[data-role]').forEach(t => { roles[t.dataset.role] = t.value; });
    const out = await api('/api/project/apply', {
      root: state.current.root,
      name: $('#opt-name').value.trim(),
      bindings: state.picks,
      roles,
      options: {
        integrator: $('#opt-integrator').value, lead: $('#opt-lead').value, autoEscalate: $('#opt-autoEscalate').checked,
        agentsMd: $('#opt-agentsMd').checked, claudeMd: $('#opt-claudeMd').checked, gitignore: $('#opt-gitignore').checked,
        aiReviewEnabled: !!(state.reviewPick && state.reviewEnabled), aiReviewSession: state.reviewPick,
      },
    });
    const root = state.current.root;
    state.roleEdits = {};
    $('#apply-result').textContent = '已生成：' + out.files.join('、');
    toast('绑定已确认，协作文件已写入项目目录');
    await loadProjects();
    await openProject(root);                       // re-read what was saved, so the cards match the config
  } catch (e) { toast(e.message, true); }
  finally { button.disabled = false; }
});

// -- tasks ---------------------------------------------------------------------
async function loadTasks() {
  if (!state.current) return;
  try {
    state.tasks = await api('/api/tasks' + params({ root: state.current.root }));
    renderTasks();
  } catch (e) { toast(e.message, true); }
}

function renderTasks() {
  const t = state.tasks;
  if (!t) return;
  const showDone = $('#show-done').checked;
  const rows = t.tasks.filter(x => showDone || !['accepted', 'cancelled'].includes(x.state)).reverse();
  $('#board').replaceChildren(...(rows.length ? rows.map(taskCard) : [el('p', { class: 'muted' }, state.current.confirmed ? '没有进行中的任务' : '先在「绑定」页确认绑定')]));
  $('#events').replaceChildren(...t.events.slice().reverse().map(e =>
    el('li', {}, `${when(e.t)}  ${e.task}  ${e.by} ${e.action}${e.note ? '：' + e.note : ''}`)));
  const w = t.wake;
  if (w) {
    $('#wake').checked = w.enabled;
    const beat = w.watcher && w.watcher.t ? 'Harness 心跳 ' + when(w.watcher.t) : 'Harness 未运行（DeepSeek 投递在钩子里兜底）';
    const seen = w.claudeSeen ? '上次检查 ' + when(w.claudeSeen) : '从未检查';
    const claude = (w.claudeOnline ? 'Claude 在线（' : 'Claude 离线（') + seen + '）';
    $('#wake-info').textContent = [beat, claude, w.codexLastWake ? 'Codex 上次唤醒 ' + when(w.codexLastWake) : '',
      w.claudeLastWake ? 'Claude 上次唤醒 ' + when(w.claudeLastWake) : ''].filter(Boolean).join(' · ');
  }
}

function taskCard(t) {
  const isMine = t.from === 'user';
  const note = el('input', { placeholder: '备注 / 返工意见', 'aria-label': '备注' });
  const act = (action, extra = {}) => async () => {
    if (action === 'reject' && !note.value.trim()) { note.focus(); return toast('驳回需要写清具体问题', true); }
    try {
      const out = await api('/api/task/act', { root: state.current.root, id: t.id, action, note: note.value, ...extra });
      toast(`${t.id}：${STATE_LABEL[out.state] || out.state}${out.escalated ? '，已转交 ' + label(out.escalated) + ' 整改' : ''}${out.advice ? '。' + out.advice : ''}${out.dispatch ? '（投递 ' + out.dispatch + '）' : ''}`);
      loadTasks();
    } catch (e) { toast(e.message, true); }
  };
  const acts = [];
  const kind = el('select', { 'aria-label': '问题类型' }, el('option', { value: 'code' }, '代码问题'), el('option', { value: 'design' }, '设计问题'));
  if (isMine && t.state === 'delivered') {
    acts.push(note, el('button', { class: 'primary', onclick: act('accept') }, '验收'));
    if (t.to === 'deepseek') acts.push(kind);
    acts.push(el('button', { onclick: () => act('reject', { kind: kind.value })() }, '驳回返工'));
  }
  if (isMine && ['posted', 'claimed', 'rework', 'blocked', 'delivered', 'ai_review'].includes(t.state)) {
    if (!acts.length) acts.push(note);
    const target = el('select', { 'aria-label': '转交给' }, Object.entries(AGENTS).filter(([a]) => a !== t.to).map(([a, i]) => el('option', { value: a }, i.label)));
    acts.push(target, el('button', { onclick: () => act('reassign', { to: target.value })() }, '转交'), el('button', { class: 'ghost', onclick: act('cancel') }, '取消任务'));
  }
  const d = t.dispatch;
  if (t.to === 'deepseek' && d && ['held', 'pending', 'uncertain'].includes(d.state)) {
    acts.push(el('button', {
      onclick: () => {
        if (d.state === 'uncertain' && !confirm('上次投递结果不确定。确认 DeepSeek 没有收到这条任务后再重发，否则会重复执行。继续？')) return;
        act('dispatch')();
      },
    }, d.state === 'held' ? '投递给 DeepSeek' : '重试投递'));
  }
  const ai = t.aiReview;
  if (t.state === 'ai_review' && ai && ['failed', 'uncertain', 'sending'].includes(ai.status)) {
    acts.push(el('button', { onclick: act('dispatch') }, '重试 AI 审查'));
  }
  const r = t.receipt;
  return el('details', { class: 'task', style: '--c:' + (AGENTS[t.to] ? AGENTS[t.to].color : 'var(--line)') },
    el('summary', {},
      el('span', { class: 'id mono' }, t.id),
      el('span', { class: 'badge ' + t.state }, STATE_LABEL[t.state] || t.state),
      el('span', { class: 'muted' }, `${label(t.from)} → ${label(t.to)} · ${t.level}`),
      el('span', {}, t.title),
      d ? el('span', { class: 'muted' }, '投递：' + d.state + (d.reason ? '（' + d.reason + '）' : '') + (d.stalled ? ' · 疑似停滞' : '')) : null,
      ai ? el('span', { class: 'muted' }, 'AI 验收：' + (AI_STATUS[ai.status] || ai.status)) : null),
    el('div', { class: 'muted mono' }, '路径：' + (t.paths.join(', ') || '（未限定）') + (t.build ? ' · 构建锁' : '') + ' · 基线 ' + (t.base || '-')),
    t.escalation ? el('p', { class: 'escalated' }, `整改：DeepSeek 返工 ${t.escalation.rejects} 次未解决（${t.escalation.kind === 'design' ? '设计' : '代码'}问题），已转交 ${label(t.escalation.to)}`) : null,
    el('pre', {}, t.spec || ''),
    t.guide ? el('pre', { class: 'guide' }, '实现指引：\n' + t.guide) : null,
    r ? el('pre', {}, `回执：${r.summary}\n检查：${r.checks || '-'}\n证据：${(r.evidence || []).join(', ') || '-'}\n边界：${r.limits || '-'}`) : null,
    t.gate ? el('pre', { class: 'gate' }, gateText(t)) : null,
    ai ? el('pre', { class: 'review' }, aiReviewText(t)) : null,
    t.review && t.review.note ? el('p', { class: 'muted' }, '审查意见：' + t.review.note) : null,
    acts.length ? el('div', { class: 'acts' }, acts) : null);
}

function gateText(t) {
  const g = t.gate, out = [];
  if (g.checks) out.push('程序验收：' + (g.checks.ok ? `通过（${g.checks.seconds}s）` : `未通过：${g.checks.failed}（退出码 ${g.checks.exit}）`));
  else if (!(t.checks || []).length) out.push('程序验收：未设验收命令');
  if (g.outside && g.outside.length) out.push('允许路径外改动：' + g.outside.join(', '));
  if (g.stat && g.stat.length) out.push('改动：\n  ' + g.stat.join('\n  '));
  else if (g.files) out.push('改动：允许路径内无变化');
  return out.join('\n');
}

function aiReviewText(t) {
  const ai = t.aiReview || {}, session = ai.session || {};
  const out = ['AI 验收：' + (AI_STATUS[ai.status] || ai.status || '-')];
  if (session.id) out.push('审查对话：' + (session.title || session.id));
  if (ai.note) out.push('审查意见：' + ai.note);
  if (ai.error) out.push('错误：' + ai.error);
  if (t.state === 'ai_review' && ['failed', 'uncertain'].includes(ai.status)) {
    out.push('处理：在「设置 / 迁移」页确认 AI 验收对话后点「重试 AI 审查」，或直接人工验收。');
  }
  return out.join('\n');
}

const label = a => (AGENTS[a] ? AGENTS[a].label : a === 'user' ? '你' : a);

$('#post').addEventListener('submit', async ev => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  try {
    const out = await api('/api/task/post', {
      root: state.current.root, to: f.get('to'), level: f.get('level'), title: f.get('title'), spec: f.get('spec'), guide: f.get('guide'),
      checks: String(f.get('checks') || '').split('\n').map(s => s.trim()).filter(Boolean),
      paths: String(f.get('paths') || '').split(/\s+/).filter(Boolean), build: f.get('build') === 'on', hold: f.get('hold') === 'on',
    });
    $('#post-result').textContent = `已发出 ${out.id}${out.dispatch ? '，投递 ' + out.dispatch : ''}`;
    ev.target.reset();
    loadTasks();
  } catch (e) { toast(e.message, true); }
});

$('#wake').addEventListener('change', async ev => {
  try { await api('/api/wake', { root: state.current.root, enabled: ev.target.checked }); loadTasks(); }
  catch (e) { toast(e.message, true); }
});
$('#show-done').addEventListener('change', renderTasks);

// -- hosts ---------------------------------------------------------------------
async function loadHosts() {
  try { state.hosts = await api('/api/hosts'); renderHosts(); } catch (e) { toast(e.message, true); }
}

function renderHosts() {
  const h = state.hosts;
  const card = (key, title, lines) => el('article', { class: 'host', style: '--c:' + AGENTS[key].color },
    el('h3', {}, el('label', { class: 'check' }, el('input', { type: 'checkbox', 'data-host': key }), title)),
    lines.filter(Boolean).map(l => el('p', {}, l)));
  const dot = (on, text) => el('span', {}, el('span', { class: 'dot ' + (on ? 'on' : 'off') }), text);
  $('#hosts').replaceChildren(
    card('claude', 'Claude Code', [dot(h.claude.installed, h.claude.installed ? '已安装' : '未安装'),
      '写入 ~/.claude/settings.json 的 CLAUDE_CODE_PLUGIN_DIRS；提供 /agentlink 命令和自动唤醒。',
      h.claude.legacy ? '检测到 MLUQOMS 旧插件 tri-collab，两者会同时运行；迁移后可在 MLUQOMS 用 install.py --uninstall --claude 移除。' : '']),
    card('codex', 'Codex', [dot(h.codex.installed, h.codex.installed ? '已安装' : (h.codex.found ? '未安装' : '未找到 ~/.codex/config.toml')),
      '添加本地市场 agentlink；装完运行 codex plugin add agentlink@agentlink，并在 Codex 中信任钩子。',
      h.codex.legacy ? '检测到旧市场 mluqoms-tri。' : '']),
    card('deepseek', 'DeepSeek Harness', [dot(h.harness.installed, h.harness.installed ? '已安装' : (h.harness.found ? '未安装' : '未找到 Harness 桌面配置')),
      dot(h.harness.connected, h.harness.connected ? '已连接（可列出会话、投递任务）' : '未连接'),
      '在 cordis.patch.yml 插入 agentlink-bridge；发布本机回环地址，每 15 秒投递任务、唤醒 Codex。',
      h.harness.legacy ? '检测到旧插件 tri-collaboration-bridge：它和本插件都会唤醒 Codex，迁移后请移除旧插件。' : '']));
  $('#hostbar').replaceChildren(
    ...[['claude', h.claude.installed], ['codex', h.codex.installed], ['deepseek', h.harness.installed && h.harness.connected]]
      .map(([a, on]) => el('span', {}, el('span', { class: 'dot ' + (on ? 'on' : 'off') }), AGENTS[a].label)));
}

async function install(uninstall) {
  const parts = {};
  document.querySelectorAll('input[data-host]').forEach(i => { parts[i.dataset.host === 'deepseek' ? 'harness' : i.dataset.host] = i.checked; });
  if (!Object.values(parts).some(Boolean)) return toast('先勾选宿主', true);
  if (!confirm((uninstall ? '卸载' : '安装') + '会修改所选宿主的用户配置（改动前自动备份）。继续？')) return;
  try {
    const out = await api('/api/install', { ...parts, uninstall });
    $('#install-report').textContent = out.report.join('\n');
    state.hosts = out.status;
    renderHosts();
  } catch (e) { toast(e.message, true); }
}
$('#install').addEventListener('click', () => install(false));
$('#uninstall').addEventListener('click', () => install(true));

// -- settings and migration ----------------------------------------------------
// The AI review conversation lives on the binding page card; this tab only holds the auto-accept policy and
// migration, so saving it never overwrites a binding-page selection.
async function loadSettings() {
  if (!state.current) return;
  const root = state.current.root;
  if (!state.sessions) await loadSessions();
  if (!state.current || state.current.root !== root) return;      // the project changed while sessions loaded
  renderSettings();
}

function renderSettings() {
  if (!state.current) return;
  const accept = state.current.options.autoAccept || [];
  $('#opt-autoAccept-L0').checked = accept.includes('L0');
  $('#opt-autoAccept-L1').checked = accept.includes('L1');
  $('#settings-result').textContent = '程序自动验收：' + (accept.length ? accept.join('、') : '无');
}

$('#save-settings').addEventListener('click', async () => {
  const root = state.current.root;
  const autoAccept = ['L0', 'L1'].filter(level => $('#opt-autoAccept-' + level).checked);
  try {
    const saved = await api('/api/project/settings', { root, options: { autoAccept } });
    if (state.current && state.current.root === root) { state.current = saved; renderSettings(); }
    toast('设置已保存');
  } catch (e) { toast(e.message, true); }
});

$('#export-migration').addEventListener('click', async () => {
  try {
    const data = await api('/api/migration/export' + params({
      root: state.current.root, sessions: $('#migrate-sessions').checked ? '1' : '0',
    }));
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = el('a', { href: url, download: (state.current.name || 'agentlink') + '.agentlink.json' });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    $('#migration-result').textContent = '已导出迁移包';
  } catch (e) { toast(e.message, true); }
});

$('#import-migration').addEventListener('change', async ev => {
  const file = ev.target.files && ev.target.files[0];
  ev.target.value = '';
  if (!file) return;
  try {
    const text = await file.text();
    let pkg;
    try { pkg = JSON.parse(text); } catch (err) { throw new Error('迁移包不是有效的 JSON：' + err.message); }
    const out = await api('/api/migration/import', { root: state.current.root, package: pkg });
    const lines = ['已应用：' + ((out.applied || []).join('、') || '（无）')];
    const pending = Object.keys(out.pending || {});
    if (pending.length) lines.push('待重新绑定：' + pending.map(a => label(a) + '（' + out.pending[a] + '）').join('、'));
    if (out.aiReviewSession) lines.push('AI 验收对话：' + out.aiReviewSession);
    (out.warnings || []).forEach(w => lines.push('注意：' + w));
    lines.push('写入：' + ((out.files || []).join('、') || '（无）'));
    $('#migration-report').textContent = lines.join('\n');
    $('#migration-result').textContent = '导入完成';
    await loadProjects();
    await openProject(state.current.root);
  } catch (e) { toast(e.message, true); }
});

// -- tabs and refresh ----------------------------------------------------------
document.querySelectorAll('[data-tab]').forEach(b => b.addEventListener('click', () => {
  state.tab = b.dataset.tab;
  document.querySelectorAll('[data-tab]').forEach(x => x.setAttribute('aria-selected', String(x === b)));
  for (const name of ['bind', 'tasks', 'hosts', 'settings']) $('#tab-' + name).hidden = name !== state.tab;
  if (state.tab === 'tasks') loadTasks();
  if (state.tab === 'hosts') loadHosts();
  if (state.tab === 'settings') loadSettings();
}));

setInterval(() => { if (state.tab === 'tasks' && !document.hidden) loadTasks(); }, 5000);

(async () => {
  try {
    await loadProjects();
    loadHosts();
    if (state.projects.length) await openProject(state.projects[state.projects.length - 1].root);
  } catch (e) { toast(e.message, true); }
})();
