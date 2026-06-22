"""
PI Agent Dashboard — FastAPI 백엔드 (5단계 파이프라인)
업로드 → ①정규화 → ②인덱싱(Haiku) → ③추출(Opus,비전) → ④검증(코드) → ⑤검토 게이트 → Excel

- 기존 fill_template.py 는 변경 없이 subprocess 로만 호출.
- 세션(메모리)에 Document(페이지 이미지 경로 포함)와 분석 결과를 보관.
"""
from __future__ import annotations

import os
import sys
import json
import uuid
import shutil
import tempfile
import subprocess
from datetime import datetime

try:
    from dotenv import load_dotenv
    # 프로젝트 루트와 app/ 양쪽의 .env 모두 시도
    load_dotenv()
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
except Exception:
    pass

import hmac
import hashlib

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, Response

from extract import normalize, Document, CorruptDocumentError
from indexer import build_index, index_document
from extractor import extract_task
from validate import validate, SOLUTION_WHITELIST
from analyze import load_demo_data

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_XLSX = os.path.join(HERE, "template.xlsx")
FILL_SCRIPT = os.path.join(HERE, "scripts", "fill_template.py")
STATIC_DIR = os.path.join(HERE, "static")
WORK_DIR = os.path.join(tempfile.gettempdir(), "pi_agent_work")
os.makedirs(WORK_DIR, exist_ok=True)

ALLOWED_EXT = {".pdf", ".pptx", ".ppt"}

# 미인증 시 보여줄 접속 코드 입력 화면 (단독 HTML)
_LOGIN_HTML = """<!DOCTYPE html>
<html lang="ko"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>PI Agent · 접속 인증</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@6.4.0/css/all.min.css"/>
</head>
<body class="min-h-screen flex items-center justify-center bg-gradient-to-br from-[#003561] via-[#0a4f8a] to-[#00a4a6] p-4">
  <div class="w-full max-w-sm bg-white rounded-2xl shadow-2xl p-8">
    <div class="flex flex-col items-center text-center mb-6">
      <div class="w-14 h-14 rounded-xl bg-[#003561] flex items-center justify-center mb-3">
        <i class="fas fa-lock text-[#00a4a6] text-2xl"></i>
      </div>
      <h1 class="text-xl font-bold text-slate-800">PI Agent</h1>
      <p class="text-sm text-slate-500 mt-1">검토 결과서 생성기 · 접속 인증</p>
    </div>
    <form id="loginForm" class="space-y-3">
      <label class="block text-sm font-medium text-slate-600">접속 코드</label>
      <input id="codeInput" type="password" autocomplete="off" autofocus
        class="w-full px-4 py-3 rounded-lg border border-slate-300 focus:ring-2 focus:ring-[#1a73c2] focus:border-[#1a73c2] outline-none"
        placeholder="접속 코드를 입력하세요"/>
      <p id="errMsg" class="hidden text-sm text-red-600"><i class="fas fa-circle-exclamation mr-1"></i><span></span></p>
      <button type="submit" id="loginBtn"
        class="w-full py-3 rounded-lg bg-[#003561] hover:bg-[#0a4f8a] text-white font-semibold transition">
        <i class="fas fa-arrow-right-to-bracket mr-1"></i> 접속
      </button>
    </form>
    <p class="text-[11px] text-slate-400 text-center mt-5">권한이 있는 사용자만 접근할 수 있습니다.</p>
  </div>
<script>
const form=document.getElementById('loginForm'),inp=document.getElementById('codeInput'),
  err=document.getElementById('errMsg'),btn=document.getElementById('loginBtn');
form.addEventListener('submit',async(e)=>{
  e.preventDefault();
  err.classList.add('hidden');
  btn.disabled=true; btn.innerHTML='<i class="fas fa-circle-notch fa-spin mr-1"></i> 확인 중';
  try{
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({code:inp.value})});
    if(r.ok){ location.reload(); return; }
    const d=await r.json().catch(()=>({}));
    err.querySelector('span').textContent=d.detail||'접속 코드가 올바르지 않습니다.';
    err.classList.remove('hidden'); inp.select();
  }catch(_){ err.querySelector('span').textContent='네트워크 오류. 다시 시도해 주세요.'; err.classList.remove('hidden'); }
  btn.disabled=false; btn.innerHTML='<i class="fas fa-arrow-right-to-bracket mr-1"></i> 접속';
});
</script>
</body></html>"""

app = FastAPI(title="PI Agent Dashboard")

# 세션 저장소 (메모리). Railway 단일 인스턴스 기준.
SESSIONS: dict[str, dict] = {}

# ---------- 접속 게이트 (접속 코드 → 서명 쿠키) ----------
ACCESS_CODE = os.environ.get("ACCESS_CODE", "palantir1!")
# 쿠키 서명 키 (배포 시 SECRET_KEY 지정 권장). 미지정 시 코드 기반 파생.
_SECRET = os.environ.get("SECRET_KEY", "pi-agent-" + hashlib.sha256(ACCESS_CODE.encode()).hexdigest()[:16])
AUTH_COOKIE = "pi_gate"
# 인증 없이 접근 허용하는 경로 (로그인/헬스/파비콘/정적자원)
_OPEN_PATHS = {"/api/login", "/api/health", "/favicon.ico"}


def _auth_token() -> str:
    """현재 접속 코드에 대한 결정적 서명 토큰."""
    return hmac.new(_SECRET.encode(), ACCESS_CODE.encode(), hashlib.sha256).hexdigest()


def _is_authed(request: Request) -> bool:
    return hmac.compare_digest(request.cookies.get(AUTH_COOKIE, ""), _auth_token())


@app.middleware("http")
async def gate_middleware(request: Request, call_next):
    path = request.url.path
    # 정적 자원과 공개 경로는 통과
    if path.startswith("/static/") or path in _OPEN_PATHS:
        return await call_next(request)
    if _is_authed(request):
        return await call_next(request)
    # 미인증: 루트는 로그인 화면 HTML, API는 401 JSON
    if path == "/" or not path.startswith("/api/"):
        return HTMLResponse(_LOGIN_HTML, status_code=401)
    return JSONResponse({"detail": "접속 코드가 필요합니다.", "auth_required": True}, status_code=401)


@app.post("/api/login")
async def login(request: Request):
    """접속 코드 검증 → 맞으면 서명 쿠키 발급."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    code = (body.get("code") or "").strip()
    if not hmac.compare_digest(code, ACCESS_CODE):
        raise HTTPException(401, "접속 코드가 올바르지 않습니다.")
    resp = JSONResponse({"ok": True})
    # HttpOnly 쿠키 (JS 탈취 방지). 8시간 유지.
    resp.set_cookie(
        AUTH_COOKIE, _auth_token(),
        max_age=8 * 3600, httponly=True, samesite="lax", path="/",
    )
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(AUTH_COOKIE, path="/")
    return resp


# ---------- 공통: 기존 fill_template.py 호출 (코드 변경 없음) ----------
def _run_fill_template(data: dict, out_path: str) -> str:
    data_path = os.path.join(WORK_DIR, f"data_{uuid.uuid4().hex}.json")
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    try:
        proc = subprocess.run(
            [sys.executable, FILL_SCRIPT,
             "--template", TEMPLATE_XLSX, "--data", data_path, "--out", out_path],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Excel 생성 실패: {proc.stderr or proc.stdout}")
        return proc.stdout.strip()
    finally:
        if os.path.exists(data_path):
            os.remove(data_path)


def _make_out_name(domain: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = "".join(c for c in (domain or "PI") if c.isalnum() or c in "()_-")[:20] or "PI"
    return f"{safe}_PI_검토결과서_{ts}.xlsx"


# ---------- API ----------
@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "template_exists": os.path.exists(TEMPLATE_XLSX),
        "solution_whitelist": SOLUTION_WHITELIST,
    }


@app.post("/api/process")
async def process(file: UploadFile = File(...)):
    """
    업로드 → ①정규화 → ②인덱싱 → ③추출 → ④검증 까지 수행하고
    세션ID와 검토용(enriched/flags) 결과를 반환.
    """
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"지원하지 않는 형식: {ext}. PDF 또는 PPTX만 가능합니다.")

    # API 키 사전 체크 — 분석은 Claude 필요. 키 없으면 명확히 안내(503).
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(
            503,
            "ANTHROPIC_API_KEY가 설정되지 않아 문서 분석을 할 수 없습니다. "
            "서버(Railway) 환경변수에 Claude API 키를 등록해 주세요. "
            "(키 없이도 '데모 데이터로 바로 Excel'은 사용 가능합니다.)",
        )

    sid = uuid.uuid4().hex
    sdir = os.path.join(WORK_DIR, sid)
    img_dir = os.path.join(sdir, "pages")
    os.makedirs(img_dir, exist_ok=True)
    up_path = os.path.join(sdir, f"src{ext}")
    with open(up_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        # ① 정규화
        try:
            doc: Document = normalize(up_path, img_dir)
        except CorruptDocumentError as ce:
            raise HTTPException(422, str(ce))
        if doc.page_count == 0:
            raise HTTPException(400, "문서에서 페이지를 읽지 못했습니다.")

        # ② 인덱싱 (하이브리드: 코드 우선 → 실패 시 Haiku 폴백)
        # 페이지가 많으면 LLM 폴백 시 페이지당 스니펫을 줄여 전 페이지 포함 보장.
        per_page = 1800 if doc.page_count <= 60 else (900 if doc.page_count <= 150 else 600)
        index, index_method = index_document(doc, per_page=per_page)
        if not index.get("tasks"):
            raise HTTPException(422, "구조 인덱싱에서 혁신과제를 찾지 못했습니다.")
        print(f"[process] page_count={doc.page_count}, per_page={per_page}, "
              f"method={index_method}, tasks={len(index.get('tasks', []))}")

        # ③ 추출 (Opus, 과제 단위 청크) — 한 과제 실패해도 나머지는 진행
        extracted = []
        extract_errors = []
        for task_meta in index["tasks"]:
            tid = task_meta.get("task_id", "?")
            try:
                extracted.append(extract_task(doc, task_meta))
                print(f"[process] 과제 {tid} 추출 완료")
            except Exception as ex:
                extract_errors.append({"task_id": tid, "error": str(ex)})
                print(f"[process] 과제 {tid} 추출 실패: {ex}")
        if not extracted:
            raise HTTPException(502, f"모든 혁신과제 추출에 실패했습니다: {extract_errors}")

        # 오너 보강: 코드 인덱서가 오너 표를 못 잡았으면 Opus가 뽑은 owner_by_bu로 폴백
        if not index.get("owner_by_bu"):
            merged_owner: dict = {}
            for t in extracted:
                ob = t.get("owner_by_bu") or {}
                for k, v in ob.items():
                    if v and not merged_owner.get(k):
                        merged_owner[k] = v
            if merged_owner:
                index["owner_by_bu"] = merged_owner
                print(f"[process] 오너 Opus 폴백 적용: {merged_owner}")

        # ④ 검증 (코드)
        result = validate(index, extracted)

        # 인덱스 과제 수 ↔ 실제 추출 과제 수 대조 (누락 감지)
        idx_n = len(index.get("tasks", []))
        got_n = len(extracted)
        if got_n < idx_n:
            result["flags"].insert(0, {
                "level": "warn", "scope": "추출 누락",
                "msg": f"인덱스가 찾은 혁신과제 {idx_n}개 중 {got_n}개만 추출됨.",
            })
        for er in extract_errors:
            result["flags"].insert(0, {
                "level": "warn", "scope": f"과제 {er['task_id']} 추출 실패",
                "msg": er["error"][:200],
            })

        # 인덱싱 방식 정보 플래그
        method_label = "코드(규칙 기반)" if index_method == "code" else "LLM(Haiku 폴백)"
        result["flags"].append({
            "level": "info", "scope": "인덱싱 방식",
            "msg": f"{method_label}로 {idx_n}개 혁신과제를 인덱싱했습니다.",
        })

        SESSIONS[sid] = {
            "doc": doc, "index": index, "extracted": extracted,
            "data": result["data"], "enriched": result["enriched"],
            "flags": result["flags"], "kind": doc.kind,
            "page_count": doc.page_count, "index_method": index_method,
        }
        return JSONResponse({
            "ok": True, "session_id": sid, "kind": doc.kind,
            "page_count": doc.page_count, "index_method": index_method,
            "enriched": result["enriched"], "flags": result["flags"],
            "data": result["data"],
        })
    except HTTPException:
        raise
    except Exception as e:
        msg = str(e)
        name = type(e).__name__
        # Claude API 관련 에러를 사용자 친화적으로 구분
        if "authentication" in msg.lower() or "401" in msg or "invalid x-api-key" in msg.lower():
            raise HTTPException(401, "Claude API 인증 실패: API 키가 올바른지 확인해 주세요.")
        if "not_found" in msg.lower() or "model" in msg.lower() and "404" in msg:
            raise HTTPException(
                422,
                f"Claude 모델을 찾을 수 없습니다. 환경변수 ANTHROPIC_EXTRACT_MODEL/"
                f"ANTHROPIC_INDEX_MODEL의 모델명을 확인해 주세요. ({msg})",
            )
        if "rate_limit" in msg.lower() or "429" in msg:
            raise HTTPException(429, "Claude API 호출 한도를 초과했습니다. 잠시 후 다시 시도해 주세요.")
        raise HTTPException(500, f"{name}: {msg}")


@app.get("/api/session/{sid}/page/{page_no}")
def get_page_image(sid: str, page_no: int):
    """⑤ 검토 게이트: 출처 페이지 이미지 (PDF만)."""
    sess = SESSIONS.get(sid)
    if not sess:
        raise HTTPException(404, "세션을 찾을 수 없습니다.")
    doc: Document = sess["doc"]
    if not (1 <= page_no <= doc.page_count):
        raise HTTPException(404, "페이지 범위를 벗어났습니다.")
    p = doc.pages[page_no - 1]
    if p.image_path and os.path.exists(p.image_path):
        return FileResponse(p.image_path, media_type="image/png")
    # PPTX 등 이미지 없음 → 텍스트 반환
    return JSONResponse({"ok": True, "no_image": True, "text": p.text})


@app.get("/api/session/{sid}/page/{page_no}/text")
def get_page_text(sid: str, page_no: int):
    sess = SESSIONS.get(sid)
    if not sess:
        raise HTTPException(404, "세션을 찾을 수 없습니다.")
    doc: Document = sess["doc"]
    if not (1 <= page_no <= doc.page_count):
        raise HTTPException(404, "페이지 범위를 벗어났습니다.")
    return {"ok": True, "page_no": page_no, "text": doc.pages[page_no - 1].text}


@app.post("/api/generate")
async def generate(data_json: str = Form(...)):
    """⑤에서 사람이 확정한 pi_data.json 으로 Excel 생성 → 다운로드."""
    try:
        data = json.loads(data_json)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"JSON 형식 오류: {e}")
    out_path = os.path.join(WORK_DIR, f"{uuid.uuid4().hex}.xlsx")
    fname = _make_out_name(data.get("domain", ""))
    try:
        _run_fill_template(data, out_path)
    except Exception as e:
        raise HTTPException(500, str(e))
    return FileResponse(
        out_path, filename=fname,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/demo")
def demo():
    """데모 데이터 — 업로드/분석 없이 즉시 Excel 생성 테스트."""
    return JSONResponse({"ok": True, "data": load_demo_data()})


@app.get("/favicon.ico")
def favicon():
    # 인라인 SVG 파비콘 (별도 파일 불필요)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="6" fill="#003561"/>'
        '<text x="16" y="22" font-size="16" text-anchor="middle" fill="#00a4a6" '
        'font-family="Arial" font-weight="bold">PI</text></svg>'
    )
    from fastapi.responses import Response
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
def index_page():
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        return f.read()


# 정적 파일
from fastapi.staticfiles import StaticFiles  # noqa: E402
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
