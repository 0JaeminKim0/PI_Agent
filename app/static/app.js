// PI Agent Dashboard — frontend logic
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

let SESSION = null;     // {session_id, kind, page_count}
let ENRICHED = null;    // 검토용 구조 (필드별 value/source_pages/confidence)
let BASE_DATA = null;   // owner_bu_map 등 보존
let SOLUTIONS = ["Hi-APS", "OASIS", "MAPS", "NexFrame", "APS"];

// ---------- toast ----------
function toast(msg, type = "info") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "fixed bottom-6 right-6 px-5 py-3 rounded-lg shadow-lg text-white text-sm z-50 " +
    (type === "error" ? "bg-red-600" : type === "success" ? "bg-accent" : "bg-hd-700");
  t.classList.remove("hidden");
  clearTimeout(t._t);
  t._t = setTimeout(() => t.classList.add("hidden"), 3500);
}

function setStep(n) {
  $$(".stepper").forEach((el) => {
    const s = +el.dataset.step;
    el.classList.toggle("active", s === n);
    el.classList.toggle("done", s < n);
  });
}

// ---------- health ----------
async function checkHealth() {
  try {
    const r = await fetch("/api/health");
    const j = await r.json();
    if (j.solution_whitelist) SOLUTIONS = j.solution_whitelist;
    const badge = $("#apiBadge");
    if (j.has_api_key) {
      badge.innerHTML = '<i class="fas fa-circle-check text-green-300"></i> Claude 연결됨';
    } else {
      badge.innerHTML = '<i class="fas fa-triangle-exclamation text-amber-300"></i> API 키 미설정 (데모만 가능)';
    }
  } catch {
    $("#apiBadge").innerHTML = '<i class="fas fa-circle-xmark text-red-300"></i> 서버 오류';
  }
}

// ---------- upload ----------
let CURRENT_FILE = null;
function bindUpload() {
  const dz = $("#dropzone"), fi = $("#fileInput");
  dz.addEventListener("click", () => fi.click());
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("dragover"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("dragover"));
  dz.addEventListener("drop", (e) => {
    e.preventDefault(); dz.classList.remove("dragover");
    if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]);
  });
  fi.addEventListener("change", () => { if (fi.files[0]) setFile(fi.files[0]); });
  $("#clearFile").addEventListener("click", () => setFile(null));
}
function setFile(f) {
  CURRENT_FILE = f;
  if (!f) {
    $("#fileInfo").classList.add("hidden");
    $("#processBtn").disabled = true;
    $("#fileInput").value = "";
    return;
  }
  const ok = /\.(pdf|pptx|ppt)$/i.test(f.name);
  if (!ok) { toast("PDF 또는 PPTX만 가능합니다.", "error"); return setFile(null); }
  $("#fileName").textContent = f.name;
  $("#fileSize").textContent = (f.size / 1024 / 1024).toFixed(2) + " MB";
  $("#fileInfo").classList.remove("hidden");
  $("#processBtn").disabled = false;
}

// ---------- process pipeline ----------
function fakeProgress() {
  let p = 5; $("#procBar").style.width = "5%";
  const steps = [
    [20, "① 문서 정규화 (텍스트·페이지 이미지)"],
    [40, "② 구조 인덱싱 (Haiku)"],
    [75, "③ 세부과제 추출 (Opus · 비전)"],
    [92, "④ 검증 (코드)"],
  ];
  let i = 0;
  const timer = setInterval(() => {
    if (i < steps.length) {
      p = steps[i][0]; $("#procBar").style.width = p + "%";
      $("#procStatusText").textContent = steps[i][1]; i++;
    } else clearInterval(timer);
  }, 1400);
  return () => { clearInterval(timer); $("#procBar").style.width = "100%"; };
}

async function processFile() {
  if (!CURRENT_FILE) return;
  $("#processBtn").disabled = true;
  $("#procStatus").classList.remove("hidden");
  setStep(2);
  const done = fakeProgress();
  try {
    const fd = new FormData();
    fd.append("file", CURRENT_FILE);
    const r = await fetch("/api/process", { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok || !j.ok) throw new Error(j.detail || "처리 실패");
    done();
    SESSION = { session_id: j.session_id, kind: j.kind, page_count: j.page_count };
    ENRICHED = j.enriched;
    BASE_DATA = j.data;
    setStep(5);
    renderReview(j.flags);
    toast("분석 완료 — 검토 후 다운로드하세요.", "success");
  } catch (e) {
    toast("오류: " + e.message, "error");
    showErrorBanner(e.message);
    setStep(1);
  } finally {
    $("#procStatus").classList.add("hidden");
    $("#processBtn").disabled = false;
  }
}

function showErrorBanner(msg) {
  let box = document.getElementById("errBanner");
  if (!box) {
    box = document.createElement("div");
    box.id = "errBanner";
    box.className = "mt-4 bg-red-50 border border-red-200 text-red-800 rounded-lg px-4 py-3 text-sm";
    $("#procStatus").after(box);
  }
  box.innerHTML = `<i class="fas fa-circle-exclamation mr-1"></i>${escapeHtml(msg)}`;
  box.classList.remove("hidden");
  setTimeout(() => box && box.classList.add("hidden"), 12000);
}

// ---------- review gate ----------
function confPill(c) {
  if (c == null) return "";
  const cls = c < 0.6 ? "conf-low" : c < 0.85 ? "conf-mid" : "conf-high";
  return `<span class="conf-pill ${cls}">${(c * 100).toFixed(0)}%</span>`;
}
function srcBtn(pages) {
  if (!pages || !pages.length) return "";
  return `<span class="src-btn" data-pages="${pages.join(',')}"><i class="fas fa-up-right-from-square"></i> 출처 p.${pages.join(',')}</span>`;
}

function fieldRow(label, path, field, multiline = false) {
  const v = (field && field.value) || "";
  const c = field ? field.confidence : null;
  const pages = field ? field.source_pages : [];
  const input = multiline
    ? `<textarea class="field-input" data-path="${path}" rows="2">${escapeHtml(v)}</textarea>`
    : `<input class="field-input" data-path="${path}" value="${escapeAttr(v)}" />`;
  return `<div class="field-row">
    <div class="field-label">${label} ${confPill(c)} ${srcBtn(pages)}</div>
    ${input}
  </div>`;
}

function renderReview(flags) {
  $("#upload-section").classList.add("hidden");
  $("#review-section").classList.remove("hidden");

  // summary
  const taskN = ENRICHED.tasks.length;
  const subN = ENRICHED.tasks.reduce((a, t) => a + t.subtasks.length, 0);
  $("#summary").innerHTML = `
    <span class="px-2 py-1 rounded bg-hd-100 text-hd-700"><i class="fas fa-layer-group mr-1"></i>도메인 ${escapeHtml(ENRICHED.domain || '-')}</span>
    <span class="px-2 py-1 rounded bg-hd-100 text-hd-700"><i class="fas fa-diagram-project mr-1"></i>혁신과제 ${taskN}</span>
    <span class="px-2 py-1 rounded bg-hd-100 text-hd-700"><i class="fas fa-list-check mr-1"></i>세부과제 ${subN}</span>
    <span class="px-2 py-1 rounded bg-hd-100 text-hd-700"><i class="fas fa-file mr-1"></i>${SESSION.page_count}p · ${SESSION.kind.toUpperCase()}</span>`;

  // flags
  if (flags && flags.length) {
    $("#flagsBox").classList.remove("hidden");
    $("#flagsList").innerHTML = flags.map((f) => {
      const icon = f.level === "warn" ? "fa-triangle-exclamation text-amber-600" : "fa-circle-info text-blue-500";
      return `<li><i class="fas ${icon} mr-1"></i><b>${escapeHtml(f.scope)}</b> — ${escapeHtml(f.msg)}</li>`;
    }).join("");
  } else {
    $("#flagsBox").classList.add("hidden");
  }

  // owner text + 사업부 매핑 표시 (편집 가능)
  let html = `<div class="task-card">
    <div class="task-head"><i class="fas fa-user-tie mr-1"></i>과제오너 / 사업부 (Q열 · I~P열)</div>
    <div class="sub-card">
      ${fieldRow("과제오너 명단 (Q)", "owner_text", {value: ENRICHED.owner_text, confidence: null, source_pages: []}, true)}
      <div class="field-label mt-2">사업부 매핑 (오너 기준 자동, 클릭하여 토글)</div>
      <div class="flex flex-wrap gap-1 mt-1" id="buMap"></div>
    </div>
  </div>`;

  // tasks
  ENRICHED.tasks.forEach((t, ti) => {
    html += `<div class="task-card">
      <div class="task-head"><i class="fas fa-diagram-project mr-1"></i>혁신과제 ${escapeHtml(t.task_id)}</div>
      <div class="sub-card">
        ${fieldRow("과제명 (C)", `tasks.${ti}.task_name`, t.task_name)}
        ${fieldRow("과제개요 (D)", `tasks.${ti}.task_overview`, t.task_overview, true)}
        ${fieldRow("기대효과 정성 (S)", `tasks.${ti}.effect_q`, t.effect_q, true)}
        ${fieldRow("기대효과 정량 (T)", `tasks.${ti}.effect_n`, t.effect_n)}
      </div>`;
    t.subtasks.forEach((s, si) => {
      html += `<div class="sub-card bg-slate-50/60">
        <div class="text-xs font-bold text-accent mb-1">세부과제 ${escapeHtml(s.sub_id)}</div>
        ${fieldRow("세부 실행과제명 (F)", `tasks.${ti}.subtasks.${si}.sub_name`, s.sub_name)}
        ${fieldRow("과제정의 (G)", `tasks.${ti}.subtasks.${si}.definition`, s.definition, true)}
        ${fieldRow("시스템/솔루션 (R)", `tasks.${ti}.subtasks.${si}.solution`, s.solution)}
      </div>`;
    });
    html += `</div>`;
  });
  $("#fieldsWrap").innerHTML = html;

  renderBuMap();
  bindSrcButtons();
}

function renderBuMap() {
  const keys = ["조선","해양","특수","미포","HD한조","HHIP","HVS","삼호"];
  const m = BASE_DATA.owner_bu_map || {};
  $("#buMap").innerHTML = keys.map((k) => {
    const on = m[k] === "O";
    return `<button class="bu-toggle px-2 py-1 rounded text-xs border ${on ? 'bg-accent text-white border-accent' : 'bg-white text-slate-500 border-slate-300'}" data-bu="${k}">${k} ${on ? '✓' : ''}</button>`;
  }).join("");
  $$(".bu-toggle").forEach((b) => b.addEventListener("click", () => {
    const k = b.dataset.bu;
    m[k] = m[k] === "O" ? "" : "O";
    renderBuMap();
  }));
}

function bindSrcButtons() {
  $$(".src-btn").forEach((b) => b.addEventListener("click", () => {
    const pages = b.dataset.pages.split(",").map(Number).filter(Boolean);
    showSource(pages);
  }));
}

async function showSource(pages) {
  $("#sourcePlaceholder").classList.add("hidden");
  const wrap = $("#sourceImages");
  $("#srcPageLabel").textContent = "p." + pages.join(", ");
  wrap.innerHTML = '<div class="text-center text-slate-400 py-6"><i class="fas fa-circle-notch fa-spin"></i> 불러오는 중...</div>';
  const out = [];
  for (const p of pages) {
    if (SESSION.kind === "pdf") {
      out.push(`<div><div class="text-xs font-semibold text-slate-500 mb-1">PAGE ${p}</div><img src="/api/session/${SESSION.session_id}/page/${p}" loading="lazy" /></div>`);
    } else {
      const r = await fetch(`/api/session/${SESSION.session_id}/page/${p}/text`);
      const j = await r.json();
      out.push(`<div><div class="text-xs font-semibold text-slate-500 mb-1">SLIDE ${p}</div><div class="src-text">${escapeHtml(j.text || '(빈 페이지)')}</div></div>`);
    }
  }
  wrap.innerHTML = out.join("");
}

// ---------- collect edits -> pi_data.json ----------
function collectData() {
  // 시작점: BASE_DATA 복제 (owner_bu_map 포함)
  const data = JSON.parse(JSON.stringify(BASE_DATA));
  // 입력값 반영
  $$("#fieldsWrap [data-path]").forEach((el) => {
    setByPath(data, el.dataset.path, el.value);
  });
  return data;
}
function setByPath(obj, path, val) {
  const parts = path.split(".");
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    let k = parts[i];
    if (/^\d+$/.test(k)) k = +k;
    cur = cur[k];
    if (cur == null) return;
  }
  let last = parts[parts.length - 1];
  if (/^\d+$/.test(last)) last = +last;
  cur[last] = val;
}

// ---------- generate excel ----------
async function generate(data) {
  $("#generateBtn").disabled = true;
  const old = $("#generateBtn").innerHTML;
  $("#generateBtn").innerHTML = '<i class="fas fa-circle-notch fa-spin mr-1"></i>생성 중';
  try {
    const fd = new FormData();
    fd.append("data_json", JSON.stringify(data));
    const r = await fetch("/api/generate", { method: "POST", body: fd });
    if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.detail || "생성 실패"); }
    const blob = await r.blob();
    const cd = r.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename\*?=(?:UTF-8'')?["']?([^"';]+)/i);
    const name = m ? decodeURIComponent(m[1]) : "PI_검토결과서.xlsx";
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a"); a.href = url; a.download = name; a.click();
    URL.revokeObjectURL(url);
    toast("Excel 다운로드 완료!", "success");
  } catch (e) {
    toast("오류: " + e.message, "error");
  } finally {
    $("#generateBtn").disabled = false;
    $("#generateBtn").innerHTML = old;
  }
}

// ---------- demo ----------
async function runDemo() {
  try {
    const r = await fetch("/api/demo");
    const j = await r.json();
    await generate(j.data);
  } catch (e) { toast("데모 오류: " + e.message, "error"); }
}

// ---------- json modal ----------
function bindJsonModal() {
  $("#jsonToggle").addEventListener("click", () => {
    $("#jsonEditor").value = JSON.stringify(collectData(), null, 2);
    $("#jsonModal").classList.remove("hidden");
  });
  $("#jsonClose").addEventListener("click", () => $("#jsonModal").classList.add("hidden"));
  $("#jsonApply").addEventListener("click", () => {
    try {
      const d = JSON.parse($("#jsonEditor").value);
      BASE_DATA = d;
      // enriched 갱신 (값만 반영, 출처/신뢰도 유지 어려우므로 재구성)
      syncEnrichedFromData(d);
      renderReview([]);
      $("#jsonModal").classList.add("hidden");
      toast("JSON 적용됨", "success");
    } catch (e) { toast("JSON 파싱 오류: " + e.message, "error"); }
  });
}
function syncEnrichedFromData(d) {
  const wrap = (v) => ({ value: v || "", confidence: null, source_pages: [] });
  ENRICHED = {
    domain: d.domain, owner_text: d.owner_text, owner_bu_map: d.owner_bu_map,
    tasks: (d.tasks || []).map((t) => ({
      task_id: t.task_id,
      task_name: wrap(t.task_name), task_overview: wrap(t.task_overview),
      effect_q: wrap(t.effect_q), effect_n: wrap(t.effect_n),
      subtasks: (t.subtasks || []).map((s) => ({
        sub_id: s.sub_id, sub_name: wrap(s.sub_name),
        definition: wrap(s.definition), solution: wrap(s.solution),
      })),
    })),
  };
}

// ---------- utils ----------
function escapeHtml(s){return (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
function escapeAttr(s){return (s||"").replace(/"/g,"&quot;");}

function restart() {
  SESSION = ENRICHED = BASE_DATA = null;
  $("#review-section").classList.add("hidden");
  $("#upload-section").classList.remove("hidden");
  setFile(null); setStep(1);
}

// ---------- init ----------
document.addEventListener("DOMContentLoaded", () => {
  checkHealth();
  bindUpload();
  bindJsonModal();
  setStep(1);
  $("#processBtn").addEventListener("click", processFile);
  $("#demoBtn").addEventListener("click", runDemo);
  $("#generateBtn").addEventListener("click", () => generate(collectData()));
  $("#restartBtn").addEventListener("click", restart);
});
