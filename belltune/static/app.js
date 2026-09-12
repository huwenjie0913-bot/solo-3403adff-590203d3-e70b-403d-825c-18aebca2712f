/* 铸钟内壁车削调音方案系统 — 前端逻辑 */
"use strict";
const PARTIALS = ["hum", "prime", "tierce", "quint", "nominal"];
const PLABEL = { hum: "Hum 哼音", prime: "Prime 基音", tierce: "Tierce 三音",
                 quint: "Quint 五音", nominal: "Nominal 标称" };
const state = {
  bellId: null, bell: null, model: null,
  depths: [], selected: new Set(), lastSel: -1,
  planId: null, plans: [],
};

const $ = id => document.getElementById(id);
async function api(url, method = "GET", body = null) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body) opt.body = JSON.stringify(body);
  const r = await fetch(url, opt);
  const j = await r.json();
  if (!r.ok) { msg(j.error || ("HTTP " + r.status), true); throw new Error(j.error); }
  return j;
}
function msg(t, bad = false) {
  const el = $("msg"); el.textContent = t;
  el.style.color = bad ? "#ff9a9a" : "#ffd98a";
  if (t) setTimeout(() => { if (el.textContent === t) el.textContent = ""; }, 5000);
}
const fmt = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? "—" : Number(v).toFixed(d);
const centsClass = c => (c === null || c === undefined) ? "" : (Math.abs(c) <= 10 ? "ok" : "bad");

/* ---------------------------------------------------------- 钟体加载 */
async function loadBells(selectId) {
  const bells = await api("/api/bells");
  const sel = $("bellSel");
  sel.innerHTML = bells.map(b => `<option value="${b.id}">${b.name} (#${b.id})</option>`).join("");
  if (selectId) sel.value = selectId;
  else if (bells.length && !state.bellId) sel.value = bells[0].id;
  return bells;
}

async function loadBell(id) {
  state.bellId = id;
  const b = await api(`/api/bells/${id}`);
  state.bell = b;
  $("fName").value = b.name; $("fDensity").value = b.density; $("fA4").value = b.a4;
  $("fPass").value = b.pass_depth; $("fStep").value = b.lathe_step;
  $("fMinTh").value = b.min_thick;
  $("fProfile").value = b.profile.map(p => `${p.z},${p.r},${p.thick}`).join("\n");
  renderTargetTable(b.targets);
  renderMeasList(b.measurements);
  state.plans = b.plans;
  renderPlans();
  await loadModel();
}

async function loadModel() {
  try {
    state.model = await api(`/api/bells/${state.bellId}/model`);
  } catch (e) { state.model = null; return; }
  if (state.depths.length !== state.model.bands.length)
    state.depths = state.model.bands.map(() => 0);
  state.selected.clear();
  renderProfile(); renderHeat(); renderDev(); await refreshPreview();
}

/* ---------------------------------------------------------- 设置表单 */
function renderTargetTable(targets) {
  $("tblTargets").innerHTML =
    "<tr><th>分音</th><th>目标 Hz</th><th>容差(音分)</th></tr>" +
    PARTIALS.map(p => {
      const t = (targets && targets[p]) || { freq: "", tol_cents: 10 };
      return `<tr><td>${PLABEL[p]}</td>
        <td><input data-tg="${p}" value="${t.freq || ""}" style="width:80px"></td>
        <td><input data-tol="${p}" value="${t.tol_cents}" style="width:56px"></td></tr>`;
    }).join("");
}

function readTargets() {
  const t = {};
  PARTIALS.forEach(p => {
    const f = parseFloat(document.querySelector(`[data-tg="${p}"]`).value);
    const tol = parseFloat(document.querySelector(`[data-tol="${p}"]`).value) || 10;
    if (f > 0) t[p] = { freq: f, tol_cents: tol };
  });
  return t;
}

$("btnGenTargets").onclick = () => {
  const a4 = parseFloat($("fA4").value) || 440;
  const note = $("fPitch").value.trim();
  const prime = noteToFreq(note, a4);
  if (!prime) { msg("音名无效，示例：E4、C#5", true); return; }
  const ratio = { hum: 0.5, prime: 1, tierce: 1.2, quint: 1.5, nominal: 2.0 };
  PARTIALS.forEach(p => {
    document.querySelector(`[data-tg="${p}"]`).value = (prime * ratio[p]).toFixed(2);
  });
  msg(`已按 ${note} = ${prime.toFixed(2)} Hz 生成目标`);
};

function noteToFreq(name, a4) {
  const m = /^([A-Ga-g])(#|b)?(-?\d)$/.exec(name);
  if (!m) return null;
  const semis = { c: 0, d: 2, e: 4, f: 5, g: 7, a: 9, b: 11 };
  let s = semis[m[1].toLowerCase()] + (m[2] === "#" ? 1 : m[2] === "b" ? -1 : 0);
  const midi = (parseInt(m[3]) + 1) * 12 + s;
  return a4 * Math.pow(2, (midi - 69) / 12);
}

$("btnSaveSetup").onclick = async () => {
  const profile = $("fProfile").value.split("\n").map(l => l.trim()).filter(Boolean)
    .map(l => { const [z, r, thick] = l.split(/[,\s]+/).map(Number);
                return { z, r, thick }; })
    .filter(p => isFinite(p.z) && isFinite(p.r) && isFinite(p.thick));
  if (profile.length < 3) { msg("母线至少需要 3 个点", true); return; }
  await api(`/api/bells/${state.bellId}/setup`, "PUT", {
    name: $("fName").value, density: +$("fDensity").value, a4: +$("fA4").value,
    pass_depth: +$("fPass").value, lathe_step: +$("fStep").value,
    min_thick: +$("fMinTh").value, profile, targets: readTargets(),
  });
  msg("设置已保存"); await loadBell(state.bellId);
};

/* ---------------------------------------------------------- 测量导入 */
$("btnImport").onclick = async () => {
  const rows = $("mRows").value.split("\n").map(l => l.trim()).filter(Boolean)
    .map(l => { const [partial, freq, confidence] = l.split(/[,\s]+/);
                return { partial, freq: +freq, confidence: confidence === undefined ? 0.5 : +confidence }; })
    .filter(r => PARTIALS.includes(r.partial) && r.freq > 0);
  if (!rows.length) { msg("无有效测量行", true); return; }
  const r = await api(`/api/bells/${state.bellId}/measurements`, "POST",
                      { round_tag: $("mTag").value, rows });
  msg(`已导入 ${r.inserted} 条测量`); await loadBell(state.bellId);
};

function renderMeasList(ms) {
  const tags = {};
  ms.forEach(m => { (tags[m.round_tag] = tags[m.round_tag] || []).push(m); });
  $("measList").innerHTML = Object.entries(tags).map(([tag, rows]) =>
    `<div><b>${tag}</b>：${rows.map(r =>
      `${r.partial}=${fmt(r.freq)}(${fmt(r.confidence, 2)})`).join("，")}</div>`
  ).join("") || "暂无测量";
}

/* ---------------------------------------------------------- SVG 截面 */
const VB = { w: 460, h: 640 };
function profileScale() {
  const prof = state.bell.profile;
  const zmax = Math.max(...prof.map(p => p.z));
  const rmax = Math.max(...prof.map(p => p.r + p.thick));
  return { zmax, sc: Math.min((VB.h - 80) / zmax, 200 / rmax) };
}
const X = (r, sc) => 230 + r * sc;          // 中心线 x=230，只画右半
const Y = (z, sc, zmax) => 30 + (zmax - z) * sc;

function renderProfile() {
  const svg = $("svgBell");
  const { bell, model } = state;
  if (!bell || !model || !model.bands.length) { svg.innerHTML = ""; return; }
  const { zmax, sc } = profileScale();
  const prof = bell.profile;
  const minTh = bell.min_thick;
  let el = "";
  // 镜像左半轮廓（装饰）
  const innerL = prof.map(p => `${230 - p.r * sc},${Y(p.z, sc, zmax)}`).join(" L ");
  const innerR = prof.map(p => `${X(p.r, sc)},${Y(p.z, sc, zmax)}`).join(" L ");
  const outerR = prof.map(p => `${X(p.r + p.thick, sc)},${Y(p.z, sc, zmax)}`).join(" L ");
  const outerL = prof.map(p => `${230 - (p.r + p.thick) * sc},${Y(p.z, sc, zmax)}`).join(" L ");
  el += `<path d="M ${outerL}" fill="none" stroke="#c9bfa8"/>`;
  el += `<path d="M ${innerL}" fill="none" stroke="#c9bfa8"/>`;
  el += `<path d="M ${outerR}" fill="none" stroke="#8a7a5a" stroke-width="1.5"/>`;
  el += `<path d="M ${innerR}" fill="none" stroke="#333" stroke-width="2"/>`;
  el += `<line x1="230" y1="14" x2="230" y2="${VB.h - 20}" stroke="#ccc" stroke-dasharray="4 4"/>`;
  // 环带
  model.bands.forEach((b, j) => {
    const y0 = Y(b.z1, sc, zmax), y1 = Y(b.z0, sc, zmax);
    const x0 = X(b.r, sc), x1 = X(b.r + b.thick, sc);
    const d = state.depths[j] || 0;
    const forbidden = b.thick - minTh <= 0.05;
    let fill = "#e8e2d2", op = 0.55;
    if (d > 0) { fill = "#d9480f"; op = 0.25 + 0.55 * Math.min(1, d / 4); }
    else if (state.selected.has(j)) { fill = "#f0a840"; op = 0.8; }
    else if (forbidden) { fill = "#999"; op = 0.35; }
    el += `<rect class="band" data-j="${j}" x="${x0}" y="${y0}" width="${x1 - x0}" height="${y1 - y0}"
      fill="${fill}" fill-opacity="${op}" stroke="#fff" stroke-width="0.4">
      <title>z=${b.z0.toFixed(0)}–${b.z1.toFixed(0)}mm 壁厚${b.thick.toFixed(1)}mm 去料${d.toFixed(2)}mm</title></rect>`;
    if (d > 0)
      el += `<text x="${x1 + 4}" y="${(y0 + y1) / 2 + 3}" font-size="9" fill="#a33">−${d.toFixed(1)}</text>`;
  });
  // 轴向刻度
  for (let z = 0; z <= zmax; z += 100)
    el += `<text x="236" y="${Y(z, sc, zmax) + 3}" font-size="8" fill="#999">z=${z}</text>`;
  svg.innerHTML = el;
  svg.querySelectorAll(".band").forEach(rect => {
    const j = +rect.dataset.j;
    rect.addEventListener("click", ev => {
      if (ev.shiftKey && state.lastSel >= 0) {
        const [a, b] = [Math.min(state.lastSel, j), Math.max(state.lastSel, j)];
        for (let k = a; k <= b; k++) state.selected.add(k);
      } else if (state.selected.has(j) && state.selected.size === 1) {
        state.selected.delete(j);
      } else { state.selected.clear(); state.selected.add(j); }
      state.lastSel = j;
      const dv = state.depths[j] || 0;
      $("depthSlider").value = dv; $("depthVal").textContent = dv.toFixed(1);
      renderProfile();
    });
    rect.addEventListener("wheel", ev => {
      ev.preventDefault();
      const delta = ev.deltaY > 0 ? -0.1 : 0.1;
      const targets = state.selected.size ? [...state.selected] : [j];
      targets.forEach(k => {
        state.depths[k] = Math.max(0, Math.round((state.depths[k] + delta) * 10) / 10);
      });
      $("depthVal").textContent = fmt(state.depths[j], 1);
      renderProfile(); schedulePreview();
    }, { passive: false });
  });
}

$("depthSlider").addEventListener("input", () => {
  const v = +$("depthSlider").value;
  $("depthVal").textContent = v.toFixed(1);
  if (!state.selected.size) return;
  state.selected.forEach(j => state.depths[j] = v);
  renderProfile(); schedulePreview();
});
$("btnClear").onclick = () => {
  state.depths = state.depths.map(() => 0);
  state.selected.clear(); renderProfile(); refreshPreview();
};

/* ---------------------------------------------------------- 热图 */
function renderHeat() {
  const svg = $("svgHeat");
  const m = state.model;
  if (!m) { svg.innerHTML = ""; return; }
  const n = m.bands.length, cw = (VB.w - 110) / n, rh = 20;
  const S = m.S;
  let maxAbs = 1e-9;
  S.forEach(row => row.forEach(v => maxAbs = Math.max(maxAbs, Math.abs(v))));
  let el = "";
  PARTIALS.forEach((p, i) => {
    el += `<text x="4" y="${18 + i * (rh + 4) + 13}" font-size="10">${PLABEL[p].split(" ")[0]}</text>`;
    for (let j = 0; j < n; j++) {
      const v = S[i][j] / maxAbs;   // -1..1
      const color = v < 0 ? `rgba(200,40,20,${Math.abs(v)})` : `rgba(30,90,200,${Math.abs(v)})`;
      el += `<rect x="${100 + j * cw}" y="${18 + i * (rh + 4)}" width="${cw - 1}" height="${rh}"
        fill="${color}"><title>${PLABEL[p]} 环带z=${m.bands[j].z0.toFixed(0)}–${m.bands[j].z1.toFixed(0)}: ${(S[i][j] * 100).toFixed(3)}%/mm</title></rect>`;
    }
  });
  // 底部 z 轴
  for (let j = 0; j < n; j += 4)
    el += `<text x="${100 + j * cw}" y="${18 + 5 * (rh + 4) + 8}" font-size="8" fill="#888">${m.bands[j].z0.toFixed(0)}</text>`;
  svg.innerHTML = el;
}

/* ---------------------------------------------------------- 偏差与预览 */
function renderDev() {
  const m = state.model;
  if (!m) return;
  $("tblDev").innerHTML =
    "<tr><th>分音</th><th>当前Hz</th><th>目标Hz</th><th>偏差(音分)</th><th>标定α</th><th>可信度</th></tr>" +
    PARTIALS.map(p => {
      const d = m.deviations[p];
      const conf = d.calib.confidence;
      const confTxt = d.calib.n_records === 0 ? "无切削记录" :
        `${(conf * 100).toFixed(0)}%（${d.calib.n_records}条记录）`;
      return `<tr><td>${PLABEL[p]}</td><td>${fmt(d.current)}</td><td>${fmt(d.target)}</td>
        <td class="${centsClass(d.cents)}">${d.cents === null ? "—" : (d.cents > 0 ? "+" : "") + fmt(d.cents, 1)}</td>
        <td>${fmt(d.alpha, 2)}</td><td>${confTxt}</td></tr>`;
    }).join("");
}

let previewTimer = null;
function schedulePreview() { clearTimeout(previewTimer); previewTimer = setTimeout(refreshPreview, 120); }

async function refreshPreview() {
  if (!state.model) return;
  const r = await api(`/api/bells/${state.bellId}/preview`, "POST", { depths: state.depths });
  $("tblPrev").innerHTML =
    "<tr><th>分音</th><th>预测Hz</th><th>偏差(音分)</th><th>状态</th></tr>" +
    PARTIALS.map(p => {
      const c = r.partials[p].cents;
      const st = c === null ? "—" : (Math.abs(c) <= 10 ? "达标" : "越界");
      return `<tr><td>${PLABEL[p]}</td><td>${fmt(r.partials[p].freq)}</td>
        <td class="${centsClass(c)}">${c === null ? "—" : (c > 0 ? "+" : "") + fmt(c, 1)}</td>
        <td class="${st === "达标" ? "ok" : st === "越界" ? "bad" : ""}">${st}</td></tr>`;
    }).join("");
  $("alerts").innerHTML =
    r.errors.map(e => `<div class="err">⛔ ${e.msg}</div>`).join("") +
    r.warnings.map(w => `<div class="wrn">⚠ ${w.msg}</div>`).join("");
}

/* ---------------------------------------------------------- 可达性 */
$("btnReach").onclick = async () => {
  const r = await api(`/api/bells/${state.bellId}/reachability`, "POST", {});
  $("reachOut").innerHTML =
    `<div class="${r.reachable ? "ok" : "bad"}" style="font-weight:bold;margin:4px 0">
      ${r.reachable ? "✓ 仅靠去料可达全部目标" : "✗ 存在仅靠去料无法达到的目标"}</div>` +
    PARTIALS.filter(p => r.partials[p]).map(p => {
      const q = r.partials[p];
      return `<div>${PLABEL[p]}：最佳可达 ${fmt(q.best_freq)} Hz
        （${q.dev_cents > 0 ? "+" : ""}${q.dev_cents} 音分）
        <span class="${q.reachable ? "ok" : "bad"}">${q.reachable ? "可达" : "不可达"}</span>
        ${q.note ? `<br><span class="warn">${q.note}</span>` : ""}</div>`;
    }).join("");
};

/* ---------------------------------------------------------- 方案与轮次 */
$("btnSavePlan").onclick = async () => {
  const r = await api(`/api/bells/${state.bellId}/plans`, "POST",
                      { name: $("planName").value || undefined, depths: state.depths });
  msg(`已保存方案 v${r.version}`); state.planId = r.id;
  await loadBell(state.bellId);
};

function renderPlans() {
  $("planList").innerHTML = state.plans.map(p =>
    `<div class="plan ${p.id === state.planId ? "sel" : ""}" data-pid="${p.id}">
       v${p.version} · ${p.name} · ${p.status} · ${new Date(p.created * 1000).toLocaleString()}</div>`
  ).join("") || "暂无方案";
  document.querySelectorAll(".plan").forEach(el => {
    el.onclick = async () => {
      state.planId = +el.dataset.pid;
      const p = await api(`/api/plans/${state.planId}`);
      state.depths = p.depths.slice();
      while (state.depths.length < state.model.bands.length) state.depths.push(0);
      renderProfile(); await refreshPreview(); renderPlans(); renderRounds(p.rounds);
      updateExportLinks();
    };
  });
  const opts = state.plans.map(p => `<option value="${p.id}">v${p.version} ${p.name}</option>`).join("");
  $("cmpA").innerHTML = opts; $("cmpB").innerHTML = opts;
  updateExportLinks();
}

function updateExportLinks() {
  if (state.planId) {
    $("lnkSvg").href = `/api/plans/${state.planId}/export.svg`;
    $("lnkJson").href = `/api/plans/${state.planId}/export.json`;
  }
}

$("btnGenRounds").onclick = async () => {
  if (!state.planId) { msg("请先保存/选择一个方案", true); return; }
  const r = await api(`/api/plans/${state.planId}/rounds`, "POST", {});
  msg("已拆分轮次"); renderRounds(r.rounds);
};

function renderRounds(rounds) {
  if (!rounds) rounds = [];
  $("roundList").innerHTML = rounds.map(r => {
    if (r.kind === "cut") {
      const tot = r.payload.depths.reduce((a, b) => a + b, 0);
      return `<div class="round cut ${r.done ? "done" : ""}">🔧 第${r.payload.round}轮车削：
        合计 ${tot.toFixed(2)} mm·带（单刀≤${state.bell.pass_depth}mm，步进${state.bell.lathe_step}mm）</div>`;
    }
    if (r.payload.measured)
      return `<div class="round meas done">📏 第${r.payload.round}轮复测：
        ${Object.entries(r.payload.measured).map(([k, v]) => `${k}=${fmt(v)}`).join("，")} Hz</div>`;
    const inputs = PARTIALS.map(p =>
      `<input placeholder="${p}" data-m="${p}" data-seq="${r.seq}">`).join("");
    return `<div class="round meas">📏 第${r.payload.round}轮复测录入：${inputs}
      <input placeholder="置信度" data-mc="${r.seq}" value="0.7" style="width:48px">
      <button data-mbtn="${r.seq}">录入并更新预测</button>
      <span data-mout="${r.seq}"></span></div>`;
  }).join("");
  document.querySelectorAll("[data-mbtn]").forEach(btn => {
    btn.onclick = async () => {
      const seq = +btn.dataset.mbtn;
      const measured = {};
      document.querySelectorAll(`[data-m][data-seq="${seq}"]`).forEach(inp => {
        if (inp.value) measured[inp.dataset.m] = +inp.value;
      });
      const confidence = +document.querySelector(`[data-mc="${seq}"]`).value || 0.7;
      const r = await api(`/api/plans/${state.planId}/rounds/${seq}/measure`, "POST",
                          { measured, confidence });
      const out = document.querySelector(`[data-mout="${seq}"]`);
      out.innerHTML = "<br>更新后预测：" + PARTIALS.map(p =>
        `${PLABEL[p].split(" ")[0]} ${fmt(r.predicted[p])}Hz` +
        (r.cents[p] !== null ? ` (${r.cents[p] > 0 ? "+" : ""}${fmt(r.cents[p], 1)}音分)` : "")
      ).join("；") + `<br>标定α已更新：${PARTIALS.map(p => fmt(r.alphas[p], 2)).join("/")}`;
      msg("复测已录入，后续预测已更新");
      await loadModel();
    };
  });
}

/* ---------------------------------------------------------- 比较 */
$("btnCmp").onclick = async () => {
  const a = $("cmpA").value, b = $("cmpB").value;
  if (!a || !b) { msg("需要两个方案版本", true); return; }
  const r = await api(`/api/bells/${state.bellId}/compare?a=${a}&b=${b}`);
  const head = r.plans.map(p => `<th>v${p.version} ${p.name}</th>`).join("");
  let rows = `<tr><td>总切削量 mm·带</td>${r.plans.map(p => `<td>${p.total_removal}</td>`).join("")}</tr>
    <tr><td>最大深度 mm</td>${r.plans.map(p => `<td>${p.max_depth}</td>`).join("")}</tr>
    <tr><td>硬错误/警告</td>${r.plans.map(p => `<td class="${p.n_errors ? "bad" : "ok"}">${p.n_errors}/${p.n_warnings}</td>`).join("")}</tr>`;
  PARTIALS.forEach(pp => {
    rows += `<tr><td>${PLABEL[pp]} 预测Hz(音分)</td>` + r.plans.map(p =>
      `<td class="${centsClass(p.cents[pp])}">${fmt(p.predicted[pp])}
       (${p.cents[pp] === null ? "—" : (p.cents[pp] > 0 ? "+" : "") + fmt(p.cents[pp], 1)})</td>`).join("") + "</tr>";
  });
  $("cmpOut").innerHTML = `<table class="mini"><tr><th></th>${head}</tr>${rows}</table>`;
};

/* ---------------------------------------------------------- 新建/演示 */
$("btnNew").onclick = async () => {
  const name = prompt("钟体名称：", "新钟");
  if (!name) return;
  const r = await api("/api/bells", "POST", { name });
  await loadBells(r.id); await loadBell(r.id);
};
$("btnDemo").onclick = async () => {
  const r = await api("/api/demo", "POST", {});
  await loadBells(r.id); await loadBell(r.id);
  msg("演示钟已载入");
};
$("bellSel").onchange = e => loadBell(+e.target.value);

/* ---------------------------------------------------------- 启动 */
(async () => {
  renderTargetTable({});
  const bells = await loadBells();
  if (bells.length) await loadBell(+($("bellSel").value || bells[0].id));
})();
