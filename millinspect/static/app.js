/* 布卷疵点复检与退修系统 —— 前端逻辑（零依赖） */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  meta: null,
  view: "ledger",
  identity: localStorage.getItem("mi_identity") || "王检",
  grants: JSON.parse(localStorage.getItem("mi_grants") || "{}"), // defectId -> token
};

// ------------------------------------------------------------------ 工具

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* 二进制接口不会走这里 */ }
  if (!res.ok) throw new Error((data && data.error) || `请求失败 ${res.status}`);
  return data;
}

let toastTimer = null;
function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3200);
}

function statusBadge(status, names) {
  const text = (names && names[status]) || state.meta.status_names[status] || status;
  return `<span class="badge ${esc(status)}">${esc(text)}</span>`;
}

function sevChip(sev) {
  return `<span class="sev ${sev}">${sev} 级</span>`;
}

function fmtTime(isoStr) {
  if (!isoStr) return "—";
  const d = new Date(isoStr);
  const p = n => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function pct(pos, len) { return `${Math.max(0, Math.min(100, (pos / len) * 100))}%`; }

// 布卷横向长度条：检验段分带 + 疵点按米数落点
function boltBar(bolt, version, opts = {}) {
  const len = bolt.length_m;
  const segs = version ? version.segments : [];
  const bands = segs.map(s =>
    `<div class="seg-band" style="left:${pct(s.start_m, len)};width:${pct(s.end_m - s.start_m, len)}"
         title="段 ${s.seq}：${s.start_m}–${s.end_m}m"></div>`).join("");
  let dots = "";
  for (const s of segs) {
    for (const d of s.defects) {
      const missing = d.type_code === "missing_photo" && !d.resolved_at;
      const cls = missing ? "defect-dot missing" : `defect-dot sev-${d.severity}`;
      dots += `<div class="${cls}" data-defect='${esc(JSON.stringify({ ...d, seg_seq: s.seq }))}'
        style="left:${pct(d.position_m, len)}" title="${d.position_m}m ${esc(d.type_name)}"></div>`;
    }
  }
  const ticks = [0, Math.round(len / 2), Math.round(len)]
    .map(m => `<span class="meter-tick" style="left:${pct(m, len)}">${m}m</span>`).join("");
  const head = opts.currentMeter != null
    ? `<div class="hud-head" style="left:${pct(opts.currentMeter, len)}"></div>` : "";
  return `<div class="bolt-bar-wrap"><div class="bolt-bar">${bands}${dots}${head}${ticks}</div></div>`;
}

// -------------------------------------------------------------- 身份切换

function identityBar() {
  const staff = state.meta.staff;
  const all = [
    ...staff.initial_inspectors.map(n => [n, "初检员"]),
    ...staff.reinspectors.map(n => [n, "复检员"]),
    ...staff.repairers.map(n => [n, "退修工"]),
    ...staff.releasers.map(n => [n, "放行主管"]),
  ];
  return `<label class="field" style="flex-direction:row;align-items:center;gap:6px;color:var(--linen-deep);font-size:12px">
    当前身份
    <select id="identity" style="padding:4px 6px;font-size:12px">
      ${all.map(([n, role]) => `<option value="${esc(n)}" ${n === state.identity ? "selected" : ""}>${esc(n)}（${role}）</option>`).join("")}
    </select></label>`;
}

function bindIdentity(root) {
  $("#identity", root).addEventListener("change", e => {
    state.identity = e.target.value;
    localStorage.setItem("mi_identity", state.identity);
    render();
  });
}

// ================================================================== 台账

async function renderLedger(viewEl) {
  const [bolts] = await Promise.all([api("/api/bolts")]);
  viewEl.innerHTML = `
    <h2 class="view-title">布卷台账</h2>
    <div class="card">
      <h3>布卷入检登记</h3>
      <div class="form-grid">
        <label class="field">布卷编号<input id="f-code" placeholder="如 B-240908"></label>
        <label class="field">长度（米）<input id="f-len" type="number" step="0.1" value="120"></label>
        <label class="field">标称长度（米）<input id="f-nlen" type="number" step="0.1" value="120"></label>
        <label class="field">幅宽（米）<input id="f-width" type="number" step="0.01" value="1.50"></label>
        <label class="field">颜色批次<select id="f-color">${state.meta.color_batches.map(c =>
          `<option value="${esc(c.code)}">${esc(c.code)} ${esc(c.color_name)}</option>`).join("")}</select></label>
        <label class="field">来源机台<select id="f-loom">${state.meta.looms.map(l =>
          `<option value="${esc(l.code)}">${esc(l.code)} ${esc(l.name)}</option>`).join("")}</select></label>
        <button class="btn rust" id="f-create">入检登记</button>
      </div>
    </div>
    <div class="card">
      <h3>在册布卷（${bolts.length} 卷）</h3>
      <table>
        <thead><tr><th>布卷编号</th><th>长度/幅宽</th><th>颜色批次</th><th>来源机台</th><th>入检时间</th>
        <th>当前版本疵点</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${bolts.map(b => `
          <tr>
            <td><strong>${esc(b.code)}</strong></td>
            <td>${b.length_m}m × ${b.width_m}m</td>
            <td>${esc(b.color_batch)}<br><span class="muted">${esc(b.color_name)}</span></td>
            <td>${esc(b.loom_code)}<br><span class="muted">${esc(b.loom_name)}</span></td>
            <td class="muted">${fmtTime(b.received_at)}</td>
            <td>${b.defect_count} 处${b.released_grade ? `　<span class="grade-chip grade-${b.released_grade}">${b.released_grade} 级放行</span>` : ""}</td>
            <td>${statusBadge(b.status)}</td>
            <td>
              <button class="btn sm ghost" data-go="entry" data-bolt="${esc(b.code)}">检验</button>
              <button class="btn sm ghost" data-go="map" data-bolt="${esc(b.code)}">疵点地图</button>
            </td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>`;

  $("#f-create", viewEl).addEventListener("click", async () => {
    try {
      await api("/api/bolts", { method: "POST", body: {
        code: $("#f-code", viewEl).value.trim(),
        length_m: parseFloat($("#f-len", viewEl).value),
        nominal_length_m: parseFloat($("#f-nlen", viewEl).value),
        width_m: parseFloat($("#f-width", viewEl).value),
        color_batch: $("#f-color", viewEl).value,
        loom_code: $("#f-loom", viewEl).value,
      }});
      toast("布卷已入检登记", "ok");
      renderLedger(viewEl);
    } catch (e) { toast(e.message, "err"); }
  });
  $$("[data-go]", viewEl).forEach(btn => btn.addEventListener("click", () => {
    switchView(btn.dataset.go, btn.dataset.bolt);
  }));
}

// ================================================================== 录入

async function renderEntry(viewEl, boltCode) {
  viewEl.innerHTML = `<h2 class="view-title">检验录入</h2><div id="entry-body" class="muted">加载中…</div>`;
  const body = $("#entry-body", viewEl);
  const bolts = await api("/api/bolts");
  const selectable = bolts.filter(b => ["pending", "inspecting", "reinspecting"].includes(b.status));
  if (!selectable.length) {
    body.innerHTML = `<div class="card">暂无可录入的布卷。</div>`;
    return;
  }
  const code = boltCode && bolts.some(b => b.code === boltCode) ? boltCode : selectable[0].code;
  await drawEntry(body, code, selectable);
}

async function drawEntry(root, code, selectable) {
  const bolt = await api(`/api/bolts/${code}`);
  const cur = bolt.versions.find(v => v.id === bolt.current_version) || null;
  const segs = cur ? cur.segments : [];
  // 当前米数 = 已登记检验段的最大结束米数；幅宽固定显示
  const currentMeter = segs.reduce((mx, s) => Math.max(mx, s.end_m), 0);
  const nextSeq = segs.length + 1;

  root.innerHTML = `
    <div class="form-grid" style="margin-bottom:14px">
      <label class="field">选择布卷
        <select id="e-bolt">${selectable.map(b =>
          `<option value="${esc(b.code)}" ${b.code === code ? "selected" : ""}>${esc(b.code)}（${esc(b.color_name)} ${b.length_m}m）</option>`).join("")}
        </select>
      </label>
      ${bolt.status === "pending" ? `<button class="btn rust" id="e-start">开始初检（${esc(state.identity)}）</button>` : ""}
    </div>
    <div class="entry-layout">
      <div class="sticky-hud">
        <div class="hud">
          <div class="hud-label">当前米数（检验进度）</div>
          <div class="hud-value">${currentMeter.toFixed(1)}<small> / ${bolt.length_m} m</small></div>
          <div class="hud-bar"><div class="hud-fill" style="width:${pct(currentMeter, bolt.length_m)}"></div>
            <div class="hud-head" style="left:${pct(currentMeter, bolt.length_m)}"></div></div>
          <div class="hud-row"><span>布卷幅宽（固定）</span><strong>${bolt.width_m} m</strong></div>
          <div class="hud-row"><span>布卷长度</span><strong>${bolt.length_m} m</strong></div>
          <div class="hud-row"><span>检验版本</span><strong>${cur ? `V${cur.version_no}${cur.kind === "post_repair" ? "（退修后）" : ""}` : "未开始"}</strong></div>
          <div class="hud-row"><span>布卷状态</span><strong>${esc(bolt.status_name)}</strong></div>
        </div>
        ${cur ? `
        <div class="card" style="margin-top:14px">
          <h3>登记检验段（区间不得重叠/越界）</h3>
          <div class="form-grid" style="grid-template-columns:1fr 1fr">
            <label class="field">起始米数<input id="e-startm" type="number" step="0.1" value="${currentMeter}"></label>
            <label class="field">结束米数<input id="e-endm" type="number" step="0.1" value="${Math.min(bolt.length_m, currentMeter + 40)}"></label>
          </div>
          <button class="btn" id="e-addseg" style="margin-top:10px">登记第 ${nextSeq} 段（${esc(state.identity)}）</button>
        </div>` : ""}
      </div>

      <div>
        ${cur ? `
        <div class="card">
          <h3>疵点录入（按米数定位，附照片）</h3>
          <div class="form-grid">
            <label class="field">所在检验段<select id="e-seg">${segs.map(s =>
              `<option value="${s.id}">段 ${s.seq}：${s.start_m}–${s.end_m}m</option>`).join("")}</select></label>
            <label class="field">疵点类型<select id="e-type">${state.meta.defect_types.map(t =>
              `<option value="${esc(t.code)}">${esc(t.name)}（${t.severity} 级）</option>`).join("")}</select></label>
            <label class="field">疵点位置（米）<input id="e-pos" type="number" step="0.1" min="0"></label>
            <label class="field">疵点照片<input id="e-photo" type="file" accept="image/*"></label>
          </div>
          <label class="field" style="margin-top:10px">描述<textarea id="e-desc" rows="2"></textarea></label>
          <div class="muted" id="e-photonote" style="margin-top:6px">普通疵点必须附照片。</div>
          <button class="btn rust" id="e-adddefect" style="margin-top:10px">登记疵点</button>
        </div>
        <div class="card">
          <h3>V${cur.version_no} 检验段与疵点</h3>
          ${segs.length ? segs.map(s => `
            <div style="margin-bottom:14px">
              <div><strong>段 ${s.seq}</strong>　${s.start_m}–${s.end_m}m　<span class="muted">初检人 ${esc(s.inspector)} · ${fmtTime(s.checked_at)}</span></div>
              ${boltBar(bolt, { ...cur, segments: [s] })}
              ${s.defects.length ? `<table><tbody>${s.defects.map(d => `
                <tr><td style="width:64px">${d.type_code === "missing_photo" && !d.resolved_at
                    ? `<div class="photo-missing">照片<br>缺失</div>`
                    : `<img class="photo-thumb" src="/api/photo?token=${state.grants[d.id] || ""}" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='grid'"><div class="photo-missing" style="display:none">需授权</div>`}
                    </td>
                  <td>${sevChip(d.severity)} <strong>${esc(d.type_name)}</strong>　${d.position_m}m
                    <div class="muted">${esc(d.description)}</div></td>
                  <td style="width:110px">${d.type_code === "missing_photo" && !d.resolved_at
                    ? `<button class="btn sm" data-rephoto="${d.id}">补照</button>`
                    : `<button class="btn sm ghost" data-grant='{"defect":${d.id},"seg":${s.id}}'>申请查看</button>`}</td>
                </tr>`).join("")}</tbody></table>`
                : `<div class="muted">该段暂无疵点</div>`}
            </div>`).join("") : `<div class="muted">尚未登记检验段</div>`}
        </div>` : `<div class="card">${esc(bolt.status_name)}状态，点击左上方“开始初检”创建 V1。</div>`}
      </div>
    </div>`;

  $("#e-bolt", root).addEventListener("change", e => drawEntry(root, e.target.value, selectable));
  const startBtn = $("#e-start", root);
  if (startBtn) startBtn.addEventListener("click", async () => {
    try { await api(`/api/bolts/${code}/inspection`, { method: "POST", body: { inspector: state.identity }});
      toast("V1 初检已开始", "ok"); drawEntry(root, code, selectable);
    } catch (e) { toast(e.message, "err"); }
  });
  const addSeg = $("#e-addseg", root);
  if (addSeg) addSeg.addEventListener("click", async () => {
    try {
      await api(`/api/versions/${cur.id}/segments`, { method: "POST", body: {
        seq: nextSeq,
        start_m: parseFloat($("#e-startm", root).value),
        end_m: parseFloat($("#e-endm", root).value),
        inspector: state.identity,
      }});
      toast("检验段已登记", "ok"); drawEntry(root, code, selectable);
    } catch (e) { toast(e.message, "err"); }
  });
  const typeSel = $("#e-type", root);
  const photoInput = $("#e-photo", root);
  const photoNote = $("#e-photonote", root);
  if (typeSel) typeSel.addEventListener("change", () => {
    const missing = typeSel.value === "missing_photo";
    photoInput.disabled = missing;
    photoNote.textContent = missing
      ? "“照片缺失”登记时不带照片，事后在下方补照；未补照前禁止放行。"
      : "普通疵点必须附照片。";
  });
  const addDef = $("#e-adddefect", root);
  if (addDef) addDef.addEventListener("click", async () => {
    const file = photoInput.files[0];
    const readFile = f => new Promise((res, rej) => {
      if (!f) return res(null);
      const r = new FileReader();
      r.onload = () => res({ data: r.result.split(",")[1], mime: f.type || "image/png" });
      r.onerror = rej; r.readAsDataURL(f);
    });
    try {
      const photo = await readFile(file);
      await api(`/api/segments/${$("#e-seg", root).value}/defects`, { method: "POST", body: {
        type_code: typeSel.value,
        position_m: parseFloat($("#e-pos", root).value),
        description: $("#e-desc", root).value,
        photo_data: photo ? photo.data : null,
        photo_mime: photo ? photo.mime : "image/png",
      }});
      toast("疵点已定位登记", "ok"); drawEntry(root, code, selectable);
    } catch (e) { toast(e.message, "err"); }
  });
  $$("[data-rephoto]", root).forEach(btn => btn.addEventListener("click", () => rephoto(btn.dataset.rephoto, root, code, selectable)));
  $$("[data-grant]", root).forEach(btn => btn.addEventListener("click", async () => {
    const info = JSON.parse(btn.dataset.grant);
    await requestGrant(info.defect, code);
    drawEntry(root, code, selectable);
  }));
}

async function rephoto(defectId, root, code, selectable) {
  const inp = document.createElement("input");
  inp.type = "file"; inp.accept = "image/*";
  inp.addEventListener("change", () => {
    const f = inp.files[0];
    const r = new FileReader();
    r.onload = async () => {
      try {
        await api(`/api/defects/${defectId}/photo`, { method: "POST", body: {
          photo_data: r.result.split(",")[1], photo_mime: f.type || "image/png",
        }});
        toast("补照完成，照片缺失已消除", "ok"); drawEntry(root, code, selectable);
      } catch (e) { toast(e.message, "err"); }
    };
    r.readAsDataURL(f);
  });
  inp.click();
}

async function requestGrant(defectId, boltCode) {
  // 找到该疵点所在版本上、当前身份已接收的任务
  const queue = await api("/api/queue");
  const task = queue.find(t => t.bolt_code === boltCode && t.status === "claimed" && t.assignee === state.identity);
  if (!task) { toast("需先以复检员身份在复检队列接收该布卷的任务，才能查看照片", "err"); return false; }
  try {
    const r = await api(`/api/tasks/${task.id}/photo-grant`, { method: "POST", body: {
      defect_id: defectId, assignee: state.identity,
    }});
    state.grants[defectId] = r.token;
    localStorage.setItem("mi_grants", JSON.stringify(state.grants));
    toast("照片访问令牌已签发（任务完成后自动失效）", "ok");
    return true;
  } catch (e) { toast(e.message, "err"); return false; }
}

// ================================================================== 地图

async function renderMap(viewEl, boltCode) {
  viewEl.innerHTML = `<h2 class="view-title">疵点地图</h2><div id="map-body" class="muted">加载中…</div>`;
  const body = $("#map-body", viewEl);
  const bolts = await api("/api/bolts");
  if (!bolts.length) { body.innerHTML = `<div class="card">暂无布卷。</div>`; return; }
  const code = boltCode && bolts.some(b => b.code === boltCode) ? boltCode : bolts[0].code;

  body.innerHTML = `
    <div class="form-grid" style="margin-bottom:14px">
      <label class="field">布卷<select id="m-bolt">${bolts.map(b =>
        `<option value="${esc(b.code)}" ${b.code === code ? "selected" : ""}>${esc(b.code)}（${b.length_m}m）</option>`).join("")}</select></label>
      <label class="field">检验版本<select id="m-ver"></select></label>
    </div>
    <div class="card">
      <div class="legend">
        <span><i class="dot" style="background:var(--rust)"></i>A 级（断经）</span>
        <span><i class="dot" style="background:var(--warn)"></i>B 级（色差/污渍/长度不足）</span>
        <span><i class="dot" style="background:var(--slate-pale)"></i>C 级</span>
        <span><i class="dot" style="background:repeating-linear-gradient(45deg,#9a3b2e 0 3px,#fff 3px 6px)"></i>照片缺失</span>
        <span>虚线分隔 = 检验段区间</span>
      </div>
      <div id="m-bar" style="margin-top:18px"></div>
      <div id="m-detail" style="margin-top:10px"></div>
    </div>
    <div class="card">
      <h3>版本链与退修关联</h3>
      <div id="m-chain"></div>
    </div>`;
  $("#m-bolt", body).addEventListener("change", e => renderMap(viewEl, e.target.value));

  const bolt = await api(`/api/bolts/${code}`);
  const verSel = $("#m-ver", body);
  verSel.innerHTML = bolt.versions.map(v =>
    `<option value="${v.id}">V${v.version_no} ${v.kind === "post_repair" ? "退修后版本" : "初检版本"}（${esc(v.inspector)}）</option>`).join("");
  verSel.value = String(bolt.current_version);
  verSel.addEventListener("change", () => drawBar(body, bolt, parseInt(verSel.value, 10)));
  drawBar(body, bolt, bolt.current_version);

  $("#m-chain", body).innerHTML = bolt.versions.length ? `
    <table><thead><tr><th>版本</th><th>类型</th><th>检验人</th><th>创建时间</th><th>取代版本</th><th>退修工单</th></tr></thead>
    <tbody>${bolt.versions.map(v => `<tr>
      <td><strong>V${v.version_no}</strong></td>
      <td><span class="version-tag ${v.kind}">${v.kind === "post_repair" ? "退修后" : "初检"}</span></td>
      <td>${esc(v.inspector)}</td><td>${fmtTime(v.created_at)}</td>
      <td>${v.supersedes ? `V${(bolt.versions.find(x => x.id === v.supersedes) || {}).version_no || "?"}` : "—"}</td>
      <td>${v.repair_order_id ? `#${v.repair_order_id}` : "—"}</td>
    </tr>`).join("")}</tbody></table>` : `<div class="muted">尚无检验版本</div>`;
}

function drawBar(root, bolt, versionId) {
  const version = bolt.versions.find(v => v.id === versionId);
  $("#m-bar", root).innerHTML = boltBar(bolt, version);
  const detail = $("#m-detail", root);
  const showDefect = async d => {
    const missing = d.type_code === "missing_photo" && !d.resolved_at;
    detail.innerHTML = `
      <div class="card" style="margin:0">
        <h3>${esc(d.type_name)} ${sevChip(d.severity)}</h3>
        <div class="kv">
          <dt>位置</dt><dd>${d.position_m} m（检验段 ${d.seg_seq}）</dd>
          <dt>描述</dt><dd>${esc(d.description) || "—"}</dd>
          <dt>登记时间</dt><dd>${fmtTime(d.recorded_at)}</dd>
          <dt>照片</dt><dd id="m-photo">加载中…</dd>
        </div>
      </div>`;
    if (missing) {
      $("#m-photo", detail).innerHTML = `<span class="deny">照片缺失，须补照后才能放行</span>`;
      return;
    }
    let token = state.grants[d.id];
    if (!token) {
      $("#m-photo", detail).innerHTML =
        `<span class="deny">照片受权限保护：仅该布卷复检任务的接收人可申请查看。</span>
         <button class="btn sm rust" id="m-grant">以「${esc(state.identity)}」申请查看</button>`;
      $("#m-grant", detail).addEventListener("click", async () => {
        if (await requestGrant(d.id, bolt.code)) showDefect(d);
      });
      return;
    }
    $("#m-photo", detail).innerHTML =
      `<img src="/api/photo?token=${token}" style="max-width:280px;border-radius:8px;border:1px solid var(--line)"
        onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'deny',textContent:'令牌失效或权限已收回（任务完成后自动失效）'}))">
       <div class="muted">凭授权令牌加载；任务完成/令牌过期后自动 403。</div>`;
  };
  $$(".defect-dot", root).forEach(dot => dot.addEventListener("click", () => {
    $$(".defect-dot", root).forEach(d => d.style.outline = "");
    dot.style.outline = "2px solid var(--slate-deep)";
    showDefect(JSON.parse(dot.dataset.defect));
  }));
  detail.innerHTML = `<div class="muted">点击长度条上的疵点落点查看详情。</div>`;
}

// ================================================================== 复检队列

async function renderQueue(viewEl) {
  viewEl.innerHTML = `<h2 class="view-title">复检队列</h2>
    <div class="card"><h3>派发复检任务（复检只能引用原检验段）</h3><div id="q-new" class="muted">加载中…</div></div>
    <div class="card"><h3>任务列表</h3><div id="q-list" class="muted">加载中…</div></div>`;
  const bolts = await api("/api/bolts");
  const detailable = bolts.filter(b => b.current_version && b.status !== "released");
  const newBox = $("#q-new", viewEl);
  newBox.classList.remove("muted");
  newBox.innerHTML = `
    <div class="form-grid">
      <label class="field">布卷<select id="q-bolt">${detailable.map(b =>
        `<option value="${esc(b.code)}">${esc(b.code)}（${esc(b.status_name)}）</option>`).join("")}</select></label>
      <label class="field">原检验段<select id="q-seg"></select></label>
      <label class="field">触发疵点<select id="q-trigger"></select></label>
      <label class="field">超时（小时）<input id="q-hours" type="number" value="24" min="0.1" step="0.1"></label>
      <button class="btn rust" id="q-enqueue">派发</button>
    </div>
    <p class="muted" style="margin-bottom:0">规则：复检人不得与初检人相同；同一布卷同时只允许一条任务被接收；超过时限自动转“已超时”，需重新派发。</p>`;

  async function fillSegs() {
    const code = $("#q-bolt", newBox).value;
    if (!code) return;
    const bolt = await api(`/api/bolts/${code}`);
    const cur = bolt.versions.find(v => v.id === bolt.current_version);
    const opts = cur ? cur.segments.map(s => ({ s, defects: s.defects })) : [];
    $("#q-seg", newBox).innerHTML = opts.length ? opts.map(o =>
      `<option value="${o.s.id}">段 ${o.s.seq}：${o.s.start_m}–${o.s.end_m}m（初检 ${esc(o.s.inspector)}）</option>`).join("")
      : `<option value="">当前版本暂无检验段</option>`;
    const refreshTriggers = () => {
      const seg = opts.find(o => String(o.s.id) === $("#q-seg", newBox).value);
      const types = seg && seg.defects.length ? seg.defects : [];
      $("#q-trigger", newBox).innerHTML = types.length
        ? types.map(d => `<option value="${esc(d.type_code)}">${esc(d.type_name)}（${d.severity} 级）@${d.position_m}m</option>`).join("")
        : state.meta.defect_types.map(t => `<option value="${esc(t.code)}">${esc(t.name)}</option>`).join("");
    };
    $("#q-seg", newBox).onchange = refreshTriggers;
    refreshTriggers();
  }
  $("#q-bolt", newBox).onchange = fillSegs;
  if (detailable.length) await fillSegs();

  $("#q-enqueue", newBox).addEventListener("click", async () => {
    try {
      await api("/api/queue", { method: "POST", body: {
        bolt_code: $("#q-bolt", newBox).value,
        segment_id: parseInt($("#q-seg", newBox).value, 10),
        trigger_code: $("#q-trigger", newBox).value,
        timeout_seconds: parseFloat($("#q-hours", newBox).value) * 3600,
      }});
      toast("复检任务已派发，等待复检线接收", "ok"); renderQueue(viewEl);
    } catch (e) { toast(e.message, "err"); }
  });

  const listBox = $("#q-list", viewEl);
  const tasks = await api("/api/queue");
  listBox.classList.remove("muted");
  listBox.innerHTML = tasks.length ? `
    <table><thead><tr><th>#</th><th>布卷/版本</th><th>原检验段</th><th>触发原因</th>
    <th>状态</th><th>初检/复检人</th><th>截止时间</th><th>操作</th></tr></thead>
    <tbody>${tasks.map(t => `
      <tr class="${t.overdue ? "flash-row" : ""}">
        <td>${t.id}</td>
        <td><strong>${esc(t.bolt_code)}</strong><br><span class="muted">V${t.version_no}</span></td>
        <td>段 @ ${t.start_m}–${t.end_m}m</td>
        <td>${sevChip(t.severity)} ${esc(t.trigger_name)}</td>
        <td>${statusBadge(t.status)}${t.overdue ? `<div class="deny">已超过截止时间</div>` : ""}
            ${t.result ? `<div class="muted">${t.result === "cleared" ? "不影响使用" : "问题确认"}</div>` : ""}</td>
        <td>${esc(t.initial_inspector)} → <strong>${esc(t.assignee || "—")}</strong></td>
        <td class="muted">${fmtTime(t.due_at)}</td>
        <td>${taskActions(t)}</td>
      </tr>`).join("")}</tbody></table>`
    : `暂无复检任务`;

  $$("[data-claim]", listBox).forEach(btn => btn.addEventListener("click", async () => {
    try {
      await api(`/api/tasks/${btn.dataset.claim}/claim`, { method: "POST", body: { assignee: state.identity }});
      toast(`任务已由 ${state.identity} 接收`, "ok"); renderQueue(viewEl);
    } catch (e) { toast(e.message, "err"); }
  }));
  $$("[data-complete]", listBox).forEach(btn => btn.addEventListener("click", async () => {
    const result = btn.dataset.result;
    const note = prompt(result === "cleared" ? "复检确认意见（不影响使用）：" : "复检确认意见（问题确认，需退修/折价）：", "");
    if (note === null) return;
    try {
      await api(`/api/tasks/${btn.dataset.complete}/complete`, { method: "POST", body: {
        assignee: state.identity, result, note,
      }});
      toast("复检任务已完成", "ok"); renderQueue(viewEl);
    } catch (e) { toast(e.message, "err"); }
  }));
}

function taskActions(t) {
  if (t.status === "queued") {
    return `<button class="btn sm rust" data-claim="${t.id}">以「${esc(state.identity)}」接收</button>`;
  }
  if (t.status === "claimed") {
    const mine = t.assignee === state.identity;
    return mine
      ? `<button class="btn sm" data-complete="${t.id}" data-result="cleared">确认无碍</button>
         <button class="btn sm rust" data-complete="${t.id}" data-result="confirmed">确认问题</button>`
      : `<span class="muted">${esc(t.assignee)} 复检中（不可重复接收）</span>`;
  }
  if (t.status === "timeout") return `<span class="deny">已超时，请重新派发</span>`;
  if (t.status === "completed") return `<span class="muted">${esc(t.note)}</span>`;
  return "—";
}

// ================================================================== 退修

async function renderRepair(viewEl) {
  viewEl.innerHTML = `<h2 class="view-title">退修进度</h2>
    <div class="card"><h3>开具退修工单</h3><div id="r-new"></div></div>
    <div class="card"><h3>工单进度与版本</h3><div id="r-list" class="muted">加载中…</div></div>`;
  const bolts = await api("/api/bolts");
  const repairable = bolts.filter(b => b.current_version && !["released"].includes(b.status));
  $("#r-new", viewEl).innerHTML = `
    <div class="form-grid">
      <label class="field">布卷<select id="r-bolt">${repairable.map(b =>
        `<option value="${esc(b.code)}">${esc(b.code)}（${esc(b.status_name)}）</option>`).join("")}</select></label>
      <label class="field" style="grid-column:span 2">退修原因<input id="r-reason" placeholder="如：断经穿经修复 / 左幅色差复修"></label>
      <button class="btn rust" id="r-open">开具工单</button>
    </div>
    <p class="muted" style="margin-bottom:0">退修完成后将生成新的检验版本（V2、V3…）并关联旧版本；已放行布卷不能直接改回待检或退修。</p>`;
  $("#r-open", viewEl).addEventListener("click", async () => {
    try {
      await api("/api/repairs", { method: "POST", body: {
        bolt_code: $("#r-bolt", viewEl).value, reason: $("#r-reason", viewEl).value || "—",
      }});
      toast("退修工单已开具，布卷进入退修中", "ok"); renderRepair(viewEl);
    } catch (e) { toast(e.message, "err"); }
  });

  const orders = await api("/api/repairs");
  const box = $("#r-list", viewEl);
  box.classList.remove("muted");
  box.innerHTML = orders.length ? `
    <table><thead><tr><th>#</th><th>布卷</th><th>原因</th><th>版本链</th><th>状态</th>
    <th>创建/完成</th><th>操作</th></tr></thead>
    <tbody>${orders.map(o => `
      <tr>
        <td>${o.id}</td>
        <td><strong>${esc(o.bolt_code)}</strong><br><span class="muted">${esc(o.color_name)} ${o.length_m}m</span></td>
        <td>${esc(o.reason)}</td>
        <td>V${o.from_version_no} → ${o.new_version_no ? `<strong>V${o.new_version_no}</strong>` : "…"}</td>
        <td>${statusBadge(o.status)}</td>
        <td class="muted">${fmtTime(o.created_at)}<br>${fmtTime(o.finished_at)}</td>
        <td>${o.status === "open" ? `<button class="btn sm" data-rstart="${o.id}">开工</button>` : ""}
            ${["open", "in_progress"].includes(o.status)
              ? `<button class="btn sm rust" data-rfinish="${o.id}">退修完成（新版本，检验人 ${esc(state.identity)}）</button>` : ""}
            ${o.status === "done" ? `<button class="btn sm ghost" data-go-queue>去复检新版本</button>` : ""}</td>
      </tr>`).join("")}</tbody></table>`
    : `暂无退修工单`;
  $$("[data-rstart]", box).forEach(b => b.addEventListener("click", async () => {
    try { await api(`/api/repairs/${b.dataset.rstart}/start`, { method: "POST" });
      toast("工单已开工", "ok"); renderRepair(viewEl);
    } catch (e) { toast(e.message, "err"); }
  }));
  $$("[data-rfinish]", box).forEach(b => b.addEventListener("click", async () => {
    try { const r = await api(`/api/repairs/${b.dataset.rfinish}/finish`, { method: "POST", body: { inspector: state.identity }});
      toast(`退修完成，已生成新版本 #${r.new_version_id}，请登记检验段并派发退修后复检`, "ok");
      renderRepair(viewEl);
    } catch (e) { toast(e.message, "err"); }
  }));
  $$("[data-go-queue]", box).forEach(b => b.addEventListener("click", () => switchView("queue")));
}

// ================================================================== 放行

async function renderRelease(viewEl) {
  viewEl.innerHTML = `<h2 class="view-title">放行记录</h2>
    <div class="detail-grid">
      <div class="card"><h3>放行条件校验</h3>
        <div class="form-grid">
          <label class="field">布卷<select id="x-bolt"></select></label>
          <button class="btn rust" id="x-check">校验放行条件</button>
        </div>
        <div id="x-result" style="margin-top:12px"></div>
      </div>
      <div class="card">
        <h3>分级放行条件</h3>
        <ul class="rule-list">
          <li><span class="sev A">A</span> ${esc(state.meta.release_rules.A)}</li>
          <li><span class="sev B">B</span> ${esc(state.meta.release_rules.B)}</li>
          <li><span class="sev C">C</span> ${esc(state.meta.release_rules.C)}</li>
        </ul>
      </div>
    </div>
    <div class="card"><h3>放行记录</h3><div id="x-list" class="muted">加载中…</div></div>`;
  const bolts = await api("/api/bolts");
  $("#x-bolt", viewEl).innerHTML = bolts.map(b =>
    `<option value="${esc(b.code)}">${esc(b.code)}（${esc(b.status_name)}）</option>`).join("");

  async function check() {
    const code = $("#x-bolt", viewEl).value;
    const bolt = await api(`/api/bolts/${code}`);
    const d = await api(`/api/release-check?bolt=${encodeURIComponent(code)}`);
    const cur = bolt.versions.find(v => v.id === bolt.current_version);
    $("#x-result", viewEl).innerHTML = `
      <div class="kv">
        <dt>布卷</dt><dd><strong>${esc(code)}</strong> ${statusBadge(bolt.status)}</dd>
        <dt>当前版本</dt><dd>${cur ? `V${cur.version_no}${cur.kind === "post_repair" ? "（退修后）" : ""}` : "—"}</dd>
        <dt>拟定级</dt><dd><span class="grade-chip grade-${d.grade}">${d.grade} 级</span></dd>
      </div>
      ${d.allowed
        ? `<p class="ok-text">✔ 满足 ${d.grade} 级放行条件。</p>
           <button class="btn rust" id="x-release">以「${esc(state.identity)}」放行</button>`
        : `<div class="deny" style="margin-top:8px">不满足放行条件：</div>
           <ul class="rule-list">${d.reasons.map(r => `<li class="deny">${esc(r)}</li>`).join("")}</ul>`}`;
    const btn = $("#x-release", viewEl);
    if (btn) btn.addEventListener("click", async () => {
      const note = prompt("放行备注（可空）：", "") ?? "";
      try {
        await api("/api/releases", { method: "POST", body: { bolt_code: code, released_by: state.identity, note }});
        toast("布卷已放行并写入放行记录", "ok"); renderRelease(viewEl);
      } catch (e) { toast(e.message, "err"); }
    });
  }
  $("#x-check", viewEl).addEventListener("click", () => check().catch(e => toast(e.message, "err")));

  const releases = await api("/api/releases");
  const box = $("#x-list", viewEl);
  box.classList.remove("muted");
  box.innerHTML = releases.length ? `
    <table><thead><tr><th>#</th><th>布卷</th><th>规格</th><th>颜色/机台</th><th>版本</th>
    <th>等级</th><th>放行人</th><th>时间</th><th>备注</th></tr></thead>
    <tbody>${releases.map(r => `
      <tr>
        <td>${r.id}</td><td><strong>${esc(r.bolt_code)}</strong></td>
        <td>${r.length_m}m × ${r.width_m}m</td>
        <td>${esc(r.color_name)}<br><span class="muted">${esc(r.loom_code)}</span></td>
        <td>V${r.version_no}</td>
        <td><span class="grade-chip grade-${r.grade}">${r.grade} 级</span></td>
        <td>${esc(r.released_by)}</td><td class="muted">${fmtTime(r.released_at)}</td>
        <td class="muted">${esc(r.note)}</td>
      </tr>`).join("")}</tbody></table>`
    : `暂无放行记录`;
  await check();
}

// ------------------------------------------------------------------ 路由

const VIEWS = {
  ledger: renderLedger, entry: renderEntry, map: renderMap,
  queue: renderQueue, repair: renderRepair, release: renderRelease,
};

function switchView(view, boltCode) {
  state.view = view;
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.view === view));
  render(boltCode);
}

async function render(boltCode) {
  const viewEl = $("#view");
  viewEl.innerHTML = "";
  // 顶栏注入身份切换
  if (!$("#identity")) {
    const bar = document.createElement("div");
    bar.style.cssText = "position:absolute;right:22px;top:16px";
    bar.innerHTML = identityBar();
    $(".topbar").style.position = "relative";
    $(".topbar").appendChild(bar);
    bindIdentity(bar);
  }
  try {
    await VIEWS[state.view](viewEl, boltCode);
  } catch (e) {
    viewEl.innerHTML = `<div class="card deny">加载失败：${esc(e.message)}</div>`;
  }
}

$("#tabs").addEventListener("click", e => {
  const btn = e.target.closest(".tab");
  if (btn) switchView(btn.dataset.view);
});

(async function init() {
  state.meta = await api("/api/meta");
  await render();
  // 进程内超时提醒：每 8 秒刷新队列页/角标
  setInterval(async () => {
    try {
      const tasks = await api("/api/queue");
      const attention = tasks.filter(t => t.overdue || t.status === "timeout").length;
      const tab = $$(".tab").find(t => t.dataset.view === "queue");
      tab.textContent = attention ? `复检队列（${attention} 超时）` : "复检队列";
      if (state.view === "queue") render();
    } catch { /* 离线静默 */ }
  }, 8000);
})();
