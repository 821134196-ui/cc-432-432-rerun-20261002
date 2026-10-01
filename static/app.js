/* 布卷疵点复检与退修系统 —— 前端逻辑（原生 JS） */
'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const api = async (path, opts = {}) => {
  const headers = { 'Content-Type': 'application/json' };
  const user = $('#currentUser').value.trim();
  if (user) headers['X-User'] = user;
  if (opts.body) opts.body = JSON.stringify(opts.body);
  const res = await fetch(path, { ...opts, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 ${res.status}`);
  return data;
};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const badge = (s) => `<span class="badge ${esc(s)}">${esc(s)}</span>`;
const toast = (msg, kind = '') => {
  const t = $('#toast');
  t.textContent = msg;
  t.className = `toast ${kind}`;
  setTimeout(() => t.classList.add('hidden'), 3200);
};

let META = { severity: {}, release_rules: {}, defect_handling: {}, defect_types: [] };
let entryState = { roll: null, detail: null };

/* ---------- 标签页 ---------- */
$$('.tab').forEach((tab) => tab.addEventListener('click', () => {
  $$('.tab').forEach((t) => t.classList.remove('active'));
  $$('.view').forEach((v) => v.classList.remove('active'));
  tab.classList.add('active');
  $(`#view-${tab.dataset.view}`).classList.add('active');
  const loaders = { ledger: loadLedger, entry: loadEntry, map: loadMap,
    queue: loadQueue, rework: loadRework, release: loadRelease };
  loaders[tab.dataset.view]?.();
}));

$('#currentUser').addEventListener('change', () => {
  localStorage.setItem('qc_user', $('#currentUser').value);
  if ($('#view-map').classList.contains('active')) loadMap();
});
if (localStorage.getItem('qc_user')) $('#currentUser').value = localStorage.getItem('qc_user');

/* ---------- 台账 ---------- */
async function loadLedger() {
  const rolls = await api('/api/rolls');
  const tb = $('#rollTable tbody');
  tb.innerHTML = rolls.map((r) => `
    <tr>
      <td><b>${esc(r.roll_no)}</b></td>
      <td>${r.length_m.toFixed(1)}</td>
      <td>${r.width_m.toFixed(2)}</td>
      <td>${esc(r.color)}<div class="muted">${esc(r.color_batch_code)} / ${esc(r.dye_lot)}</div></td>
      <td>${esc(r.loom_code)}<div class="muted">${esc(r.workshop)}</div></td>
      <td>v${r.current_version}</td>
      <td>${badge(r.status)}</td>
      <td>${r.open_defects}</td>
      <td><button class="btn small ghost" onclick="showDetail(${r.id})">详情</button></td>
    </tr>`).join('');
}
$('#refreshLedger').addEventListener('click', loadLedger);
$('#closeDetail').addEventListener('click', () => $('#rollDetailPanel').hidden = true);

window.showDetail = async function (rid) {
  const d = await api(`/api/rolls/${rid}`);
  $('#detailTitle').textContent = `布卷详情 · ${d.roll_no}`;
  const versions = d.versions.map((v) => `
    <div class="version-node ${v.version_no === d.current_version ? 'current' : ''}">
      <b>v${v.version_no}</b> ${v.kind === 'initial' ? '初检' : '退修复检'}<br>
      <span class="muted">${esc(v.inspector)} · ${esc(v.created_at)}</span>
    </div>`).join('<div class="version-arrow">→</div>');
  const defects = d.defects.map((x) => `
    <tr><td>${x.id}</td><td>v${x.version_no}</td>
      <td>${x.pos_m.toFixed(1)}m</td>
      <td><span class="pill ${x.grade}">${x.grade}</span> ${esc(x.type_name)}</td>
      <td>${esc(x.status)}</td>
      <td>${x.photo_file
        ? `<img class="photo-thumb" data-photo="${esc(x.photo_file)}" data-rid="${d.id}" alt="照片">`
        : '<span class="muted">照片缺失</span>'}</td>
      <td class="muted">${esc(x.note || '')}</td></tr>`).join('');
  $('#detailBody').innerHTML = `
    <div class="kv card"><div class="kv">
      <b>长度/幅宽</b><span>${d.length_m}m × ${d.width_m}m（订单最小 ${d.min_length_m}m）</span>
      <b>颜色/机台</b><span>${esc(d.color)} ${esc(d.color_batch_code)} · ${esc(d.loom_code)}</span>
      <b>状态</b><span>${badge(d.status)}</span>
    </div></div>
    <h3>检验版本链</h3>
    <div class="version-tree">${versions || '<span class="muted">尚无检验版本</span>'}</div>
    <h3>检验段（复检仅可引用初检段）</h3>
    <p>${d.segments.map((s) => `[${s.start_m}m, ${s.end_m}m) ${esc(s.inspector)}`).join(' ；') || '<span class="muted">无</span>'}</p>
    <h3>疵点（含各版本快照）</h3>
    <div class="table-wrap"><table class="grid">
      <thead><tr><th>#</th><th>版本</th><th>位置</th><th>类型</th><th>状态</th><th>照片</th><th>备注</th></tr></thead>
      <tbody>${defects}</tbody></table></div>
    <div class="actions">
      ${d.status === '待放行'
        ? `<button class="btn rust" onclick="doRelease(${d.id})">执行放行</button>`
        : '<span class="hint">仅“待放行”状态可执行放行；已放行布卷不能直接改回待检。</span>'}
    </div>`;
  $$('#detailBody .photo-thumb').forEach((img) => loadPhotoInto(img));
  $('#rollDetailPanel').hidden = false;
};

window.doRelease = async function (rid) {
  const reviewer = $('#currentUser').value.trim();
  if (!reviewer) return toast('请先在右上角填写当前检验人员（不得与初检人相同）', 'err');
  try {
    const r = await api(`/api/rolls/${rid}/release`, { method: 'POST', body: { reviewer } });
    toast(`已放行：${r.basis}`, 'ok');
    await showDetail(rid);
    loadLedger();
  } catch (e) { toast(e.message, 'err'); }
};

/* ---------- 检验录入 ---------- */
async function loadEntry() {
  const rolls = await api('/api/rolls');
  const candidates = rolls.filter((r) => ['待检', '检验中'].includes(r.status));
  $('#entryRoll').innerHTML = '<option value="">— 选择待检/检验中布卷 —</option>' +
    candidates.map((r) => `<option value="${r.id}">${r.roll_no}（${r.status}，${r.length_m}m）</option>`).join('');
  $('#defectType').innerHTML = META.defect_types
    .map((t) => `<option value="${t.code}">${t.name}（${t.category}/${t.severity}）</option>`).join('');
}
$('#loadEntryRoll').addEventListener('click', loadEntryRoll);
$('#entryRoll').addEventListener('change', loadEntryRoll);

async function loadEntryRoll() {
  const rid = Number($('#entryRoll').value);
  if (!rid) return;
  let d = await api(`/api/rolls/${rid}`);
  const hint = $('#entryHint');
  if (d.status === '待检') {
    const inspector = $('#currentUser').value.trim();
    if (!inspector) return hint.textContent = '请先在右上角填写初检人';
    await api('/api/inspections/begin', { method: 'POST', body: { roll_id: rid, inspector } });
    d = await api(`/api/rolls/${rid}`);
    toast('已开始初检（v1）', 'ok');
  }
  entryState = { roll: d, detail: d };
  const v1 = d.versions.find((v) => v.version_no === 1);
  $('#currentMeter').textContent = (d.segments.at(-1)?.end_m ?? 0).toFixed(1);
  $('#totalMeter').textContent = d.length_m.toFixed(1);
  $('#widthMeter').textContent = d.width_m.toFixed(2);
  $('#entryInspector').textContent = v1?.inspector || '—';
  $('#segStart').value = (d.segments.at(-1)?.end_m ?? 0).toFixed(1);
  $('#segEnd').value = '';
  renderEntryDefects(d);
}

function renderEntryDefects(d) {
  $('#segList').innerHTML = d.segments
    .map((s) => `<li>段 #${s.id}：[${s.start_m}m, ${s.end_m}m) · ${esc(s.inspector)}</li>`).join('');
  $('#defectSeg').innerHTML = d.segments
    .map((s) => `<option value="${s.id}">#${s.id} [${s.start_m}, ${s.end_m})m</option>`).join('');
  $('#entryDefects').innerHTML = d.defects.length
    ? `<div class="table-wrap"><table class="grid">
        <thead><tr><th>位置</th><th>等级</th><th>类型</th><th>照片</th></tr></thead>
        <tbody>${d.defects.map((x) => `<tr>
          <td>${x.pos_m}m</td><td><span class="pill ${x.grade}">${x.grade}</span></td>
          <td>${esc(x.type_name)}</td>
          <td>${x.photo_file ? `<img class="photo-thumb" data-photo="${esc(x.photo_file)}" data-rid="${d.id}">`
            : '<span class="muted">缺失</span>'}</td></tr>`).join('')}
        </tbody></table></div>`
    : '<p class="muted">暂无疵点</p>';
  $$('#entryDefects .photo-thumb').forEach((img) => loadPhotoInto(img));
}

$('#addSegment').addEventListener('click', async () => {
  try {
    const d = entryState.roll;
    const inspector = $('#currentUser').value.trim();
    const v1 = d.versions.find((v) => v.version_no === 1);
    const sid = await api('/api/segments', { method: 'POST', body: {
      version_id: v1.id, start_m: Number($('#segStart').value),
      end_m: Number($('#segEnd').value), inspector } });
    toast(`检验段 #${sid.segment_id} 已添加`, 'ok');
    await loadEntryRoll();
  } catch (e) { toast(e.message, 'err'); }
});

function readFileBase64(file) {
  return new Promise((resolve, reject) => {
    if (!file) return resolve(null);
    const fr = new FileReader();
    fr.onload = () => resolve({ b64: fr.result.split(',')[1], name: file.name });
    fr.onerror = reject;
    fr.readAsDataURL(file);
  });
}

$('#addDefect').addEventListener('click', async () => {
  try {
    const d = entryState.roll;
    const inspector = $('#currentUser').value.trim();
    const file = $('#defectPhoto').files[0];
    const photo = file ? await readFileBase64(file) : null;
    const payload = {
      segment_id: Number($('#defectSeg').value),
      type_code: $('#defectType').value,
      pos_m: Number($('#defectPos').value),
      length_m: Number($('#defectLen').value || 0),
      grade: $('#defectGrade').value,
      inspector, note: $('#defectNote').value,
    };
    if (photo) {
      payload.photo_data_base64 = photo.b64;
      payload.photo_filename = photo.name;
    }
    const r = await api('/api/defects', { method: 'POST', body: payload });
    toast(`疵点 #${r.defect_id} 已登记${r.photo_file ? '（含照片）' : '（无照片：复检须补拍）'}`, 'ok');
    $('#defectPos').value = ''; $('#defectNote').value = ''; $('#defectPhoto').value = '';
    await loadEntryRoll();
  } catch (e) { toast(e.message, 'err'); }
});

$('#completeInitial').addEventListener('click', async () => {
  try {
    const d = entryState.roll;
    await api('/api/inspections/complete', { method: 'POST', body: { roll_id: d.id } });
    toast('初检结束；已按疵点情况进入复检队列或待放行', 'ok');
    await loadEntry();
    entryState = { roll: null, detail: null };
  } catch (e) { toast(e.message, 'err'); }
});

/* ---------- 疵点地图 ---------- */
async function loadMap() {
  const [rolls, defects] = await Promise.all([api('/api/rolls'), api('/api/map')]);
  $('#mapLegend').innerHTML =
    `<span><i style="background:var(--rust)"></i>A 严重</span>
     <span><i style="background:#c07f38"></i>B 中等</span>
     <span><i style="background:var(--slate-mid)"></i>C 轻微</span>`;
  const byRoll = {};
  defects.forEach((x) => (byRoll[x.roll_id] ??= []).push(x));
  $('#mapBody').innerHTML = rolls.map((r) => {
    const ds = byRoll[r.id] || [];
    const dots = ds.map((x) => `
      <div class="defect-dot ${x.grade}" style="left:${Math.min(100, x.pos_m / r.length_m * 100)}%"
           data-photo="${esc(x.photo_file || '')}" data-rid="${r.id}"
           data-label="${esc(x.type_name)} ${x.pos_m}m（${x.grade}级）">
        <span class="tip">${esc(x.type_name)} ${x.pos_m}m · ${esc(x.status)}${x.photo_file ? '' : ' · 照片缺失'}</span>
      </div>`).join('');
    return `<div class="roll-strip">
      <div class="strip-head"><span class="no">${esc(r.roll_no)}</span>
        <span class="muted">${r.length_m}m × ${r.width_m}m · ${esc(r.color)} · ${badge(r.status)}</span></div>
      <div class="strip-track">${dots}</div>
      <div class="ruler"><span>0m</span><span>${(r.length_m / 2).toFixed(1)}m</span><span>${r.length_m}m</span></div>
    </div>`;
  }).join('');
  $$('.defect-dot').forEach((dot) => dot.addEventListener('click', () => {
    if (!dot.dataset.photo) return openPhotoDenied(dot.dataset.label + '：照片缺失，复检补拍后方可查看');
    openPhoto(dot.dataset.photo, dot.dataset.rid, dot.dataset.label);
  }));
}

async function loadPhotoInto(imgEl) {
  const user = $('#currentUser').value.trim();
  if (!user) { imgEl.replaceWith(Object.assign(document.createElement('span'),
    { className: 'muted', textContent: '需身份' })); return; }
  try {
    const res = await fetch(`/api/photos/${imgEl.dataset.photo}?roll_id=${imgEl.dataset.rid}&user=${encodeURIComponent(user)}`);
    if (!res.ok) throw new Error((await res.json()).error);
    imgEl.src = URL.createObjectURL(await res.blob());
  } catch (e) {
    imgEl.replaceWith(Object.assign(document.createElement('span'),
      { className: 'muted', textContent: '无权查看' }));
  }
}

async function openPhoto(file, rid, label) {
  const user = $('#currentUser').value.trim();
  $('#photoTitle').textContent = `疵点照片 · ${label}`;
  if (!user) return openPhotoDenied('未填写检验人员身份，照片不可见');
  $('#photoBody').innerHTML = '<p class="muted">加载中…</p>';
  $('#photoModal').classList.remove('hidden');
  try {
    const res = await fetch(`/api/photos/${file}?roll_id=${rid}&user=${encodeURIComponent(user)}`);
    if (!res.ok) throw new Error((await res.json()).error);
    const url = URL.createObjectURL(await res.blob());
    $('#photoBody').innerHTML = `<img src="${url}" alt="疵点照片">`;
  } catch (e) { openPhotoDenied(e.message); }
}
function openPhotoDenied(msg) {
  $('#photoTitle').textContent = '疵点照片';
  $('#photoBody').innerHTML = `<div class="denied">⛔ ${esc(msg)}</div>`;
  $('#photoModal').classList.remove('hidden');
}
$('#photoClose').addEventListener('click', () => $('#photoModal').classList.add('hidden'));
$('#photoModal').addEventListener('click', (e) => {
  if (e.target.id === 'photoModal') $('#photoModal').classList.add('hidden');
});

/* ---------- 复检队列 ---------- */
async function loadQueue() {
  const [tasks, rolls] = await Promise.all([api('/api/queue'), api('/api/rolls')]);
  const detailMap = {};
  for (const r of rolls) detailMap[r.id] = await api(`/api/rolls/${r.id}`);
  $('#queueBody').innerHTML = tasks.map((t) => {
    const d = detailMap[t.roll_id];
    const initialDefects = d.defects.filter((x) => x.version_no === 1 && x.status === '待复检');
    const urgent = t.status === '待接收' ? 'urgent' : t.status === '超时' ? 'overdue' : '';
    let body = '';
    if (t.status === '超时') {
      body = `<p class="hint error">已超过复检时限 ${esc(t.due_at)}，任务自动超时，可重新下发改派。</p>
        <button class="btn rust small" onclick="recreateTask(${t.roll_id})">重新下发复检任务</button>`;
    } else if (t.status === '待接收') {
      body = `
        <div class="form-row">
          <label>产线<input class="mini-input" id="line-${t.id}" value="复检一线"></label>
          <label>复检人<input class="mini-input" id="rev-${t.id}" value="${esc($('#currentUser').value || '')}"
            placeholder="不得为 ${esc(t.initial_inspector)}"></label>
          <button class="btn small rust" onclick="claim(${t.id})">接收任务</button>
        </div>
        <p class="hint">初检人：${esc(t.initial_inspector)}（复检人不能相同）· 时限至 ${esc(t.due_at)}</p>`;
    } else if (t.status === '进行中') {
      const rows = initialDefects.map((x) => `
        <li><span class="pill ${x.grade}">${x.grade}</span> ${esc(x.type_name)} ${x.pos_m}m
          <select id="act-${t.id}-${x.id}">
            <option value="确认">复检确认${x.photo_file ? '' : '（须补拍照片）'}</option>
            <option value="误判">判为误判</option>
            <option value="转退修">转退修${x.grade === 'A' || x.category === '断经' ? '（强制）' : ''}</option>
          </select>
          ${x.photo_file ? '' : `<label class="mini-input">补拍<input type="file" id="ph-${t.id}-${x.id}" accept="image/*,.svg"></label>`}
        </li>`).join('');
      body = `<ul class="decision-list">${rows}</ul>
        <label>处置方式 <select id="disp-${t.id}">
          <option value="直接放行">全部确认 → 直接放行</option>
          <option value="误判关闭">全部误判 → 关闭</option>
          <option value="转退修">存在退修项 → 转退修</option></select></label>
        <div class="actions"><button class="btn rust small" onclick="submitReview(${t.id})">提交复检</button></div>`;
    } else {
      body = `<p class="hint">处置：${esc(t.disposition || '—')} · 提交于 ${esc(t.submitted_at || '')}；${badge(t.status)}</p>`;
    }
    return `<div class="card ${urgent}">
      <h4>${esc(t.roll_no)} <span>${badge(t.status)}</span></h4>
      <div class="kv">
        <b>长度×幅宽</b><span>${t.length_m}m × ${t.width_m}m</span>
        <b>初检人</b><span>${esc(t.initial_inspector)}</span>
        <b>待复检</b><span>${t.pending} 项</span>
        <b>接收产线</b><span>${esc(t.line || '—')} / ${esc(t.reviewer || '—')}</span>
      </div>${body}</div>`;
  }).join('') || '<p class="muted">暂无复检任务</p>';
}
$('#refreshQueue').addEventListener('click', loadQueue);

window.claim = async function (tid) {
  try {
    const r = await api(`/api/tasks/${tid}/claim`, { method: 'POST', body: {
      line: $(`#line-${tid}`).value, reviewer: $(`#rev-${tid}`).value } });
    toast(`产线 ${r.line} 接收成功（复检人 ${r.reviewer}）`, 'ok');
    loadQueue();
  } catch (e) { toast(e.message, 'err'); }
};

window.recreateTask = async function (rid) {
  try {
    const r = await api('/api/tasks/recreate', { method: 'POST', body: { roll_id: rid } });
    toast(`已重新下发任务 #${r.task_id}`, 'ok');
    loadQueue();
  } catch (e) { toast(e.message, 'err'); }
};

window.submitReview = async function (tid) {
  try {
    const rolls = await api('/api/rolls');
    // 从当前卡片收集决定
    const selects = $$(`[id^="act-${tid}-"]`);
    const decisions = [];
    for (const sel of selects) {
      const did = Number(sel.id.split('-').pop());
      const item = { defect_id: did, action: sel.value };
      const ph = $(`#ph-${tid}-${did}`);
      if (ph && ph.files[0]) {
        const photo = await readFileBase64(ph.files[0]);
        item.photo_data_base64 = photo.b64;
        item.photo_filename = photo.name;
      }
      decisions.push(item);
    }
    const r = await api(`/api/tasks/${tid}/submit`, { method: 'POST', body: {
      decisions, disposition: $(`#disp-${tid}`).value } });
    toast(`复检已提交：${r.disposition}${r.rework_order_id ? `，退修工单 #${r.rework_order_id}` : ''}`, 'ok');
    loadQueue();
  } catch (e) { toast(e.message, 'err'); }
};

/* ---------- 退修进度 ---------- */
async function loadRework() {
  const [orders, rolls] = await Promise.all([api('/api/rework'), api('/api/rolls')]);
  const detailMap = Object.fromEntries(rolls.map((r) => [r.id, null]));
  for (const r of rolls) detailMap[r.id] = await api(`/api/rolls/${r.id}`);
  $('#reworkBody').innerHTML = orders.map((o) => {
    const d = detailMap[o.roll_id];
    const defectIds = JSON.parse(o.defects);
    const targets = d.defects.filter((x) => defectIds.includes(x.id));
    let body = '';
    if (o.status === '待退修') {
      body = `<div class="form-row">
        <label>修布工<input class="mini-input" id="asg-${o.id}" value="钱修布"></label>
        <button class="btn small" onclick="startRework(${o.id})">开工</button></div>`;
    } else if (o.status === '退修中') {
      body = `<ul class="decision-list">${targets.map((x) => `
        <li><span class="pill ${x.grade}">${x.grade}</span> ${esc(x.type_name)} ${x.pos_m}m
          <select id="rw-${o.id}-${x.id}">
            <option value="修复">已修复（接头/清洗）</option>
            ${x.category === '长度不足' ? '<option value="核减长度">核减长度</option>' : ''}
          </select>
          <label class="mini-input">修复照片<input type="file" id="rwp-${o.id}-${x.id}" accept="image/*,.svg"></label>
          ${x.category === '长度不足' ? '<input class="mini-input" type="number" step="0.1" placeholder="核减后长度m" id="rwn-' + o.id + '-' + x.id + '">' : ''}
        </li>`).join('')}</ul>
        <div class="form-row">
          <label>退修复检人<input class="mini-input" id="rwi-${o.id}" value="${esc($('#currentUser').value || '')}"></label>
          <button class="btn rust small" onclick="completeRework(${o.id})">完工并生成新版本</button>
        </div>`;
    } else {
      body = `<p class="hint">v${o.from_version} → v${o.to_version} · ${esc(o.assignee)} · 完成于 ${esc(o.completed_at)}</p>`;
    }
    return `<div class="card">
      <h4>退修工单 #${o.id} · ${esc(o.roll_no)} ${badge(o.status)}</h4>
      <div class="kv"><b>疵点</b><span>${targets.map((x) => `#${x.id} ${esc(x.type_name)}`).join('；')}</span></div>
      ${body}</div>`;
  }).join('') || '<p class="muted">暂无退修工单</p>';
}
$('#refreshRework').addEventListener('click', loadRework);

window.startRework = async function (oid) {
  try {
    await api(`/api/rework/${oid}/start`, { method: 'POST',
      body: { assignee: $(`#asg-${oid}`).value } });
    toast('退修已开工', 'ok'); loadRework();
  } catch (e) { toast(e.message, 'err'); }
};

window.completeRework = async function (oid) {
  try {
    const selects = $$(`[id^="rw-${oid}-"]`).filter((el) => el.tagName === 'SELECT');
    const results = [];
    for (const sel of selects) {
      const did = Number(sel.id.split('-').pop());
      const ph = $(`#rwp-${oid}-${did}`);
      if (!ph.files[0]) throw new Error(`疵点 #${did} 缺少修复照片（照片缺失不得放行）`);
      const photo = await readFileBase64(ph.files[0]);
      const item = { defect_id: did, action: sel.value,
        photo_data_base64: photo.b64, photo_filename: photo.name };
      const nl = $(`#rwn-${oid}-${did}`);
      if (nl && nl.value !== '') item.new_length_m = Number(nl.value);
      results.push(item);
    }
    const r = await api(`/api/rework/${oid}/complete`, { method: 'POST', body: {
      results, inspector: $(`#rwi-${oid}`).value } });
    toast(`退修完成，已生成新版本 v${r.new_version}`, 'ok'); loadRework();
  } catch (e) { toast(e.message, 'err'); }
};

/* ---------- 放行记录 ---------- */
async function loadRelease() {
  const rows = await api('/api/releases');
  $('#releaseTable tbody').innerHTML = rows.map((x) => `
    <tr><td>${esc(x.created_at)}</td><td><b>${esc(x.roll_no)}</b></td>
      <td>v${x.version_no}</td><td>${esc(x.reviewer)}</td>
      <td><span class="badge 已放行">${esc(x.decision)}</span></td>
      <td>${esc(x.basis)}</td></tr>`).join('');
  $('#releaseRules').innerHTML =
    Object.entries(META.release_rules).map(([g, txt]) =>
      `<div class="rule"><b>${g} 级疵点</b>：${esc(txt)}</div>`).join('') +
    Object.entries(META.defect_handling).map(([k, txt]) =>
      `<div class="rule"><b>${esc(k)}</b>：${esc(txt)}</div>`).join('');
}
$('#refreshRelease').addEventListener('click', loadRelease);

/* ---------- 复检超时提醒轮询 ---------- */
async function pollReminders() {
  try {
    const list = await api('/api/reminders');
    const bell = $('#reminderBell');
    if (list.length) {
      bell.classList.remove('hidden');
      $('#reminderCount').textContent = list.length;
      bell.title = list.map((m) => m.message).join('\n');
    } else bell.classList.add('hidden');
  } catch { /* 忽略 */ }
}

/* ---------- 初始化 ---------- */
(async function init() {
  META = await api('/api/meta');
  await loadLedger();
  pollReminders();
  setInterval(pollReminders, 8000);
})();
