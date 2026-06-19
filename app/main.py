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

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse

from extract import normalize, Document
from indexer import build_index
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

app = FastAPI(title="PI Agent Dashboard")

# 세션 저장소 (메모리). Railway 단일 인스턴스 기준.
SESSIONS: dict[str, dict] = {}


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
        doc: Document = normalize(up_path, img_dir)
        if doc.page_count == 0:
            raise HTTPException(400, "문서에서 페이지를 읽지 못했습니다.")

        # ② 인덱싱 (Haiku)
        index = build_index(doc.text_index())
        if not index.get("tasks"):
            raise HTTPException(422, "구조 인덱싱에서 혁신과제를 찾지 못했습니다.")

        # ③ 추출 (Opus, 과제 단위 청크)
        extracted = []
        for task_meta in index["tasks"]:
            extracted.append(extract_task(doc, task_meta))

        # ④ 검증 (코드)
        result = validate(index, extracted)

        SESSIONS[sid] = {
            "doc": doc, "index": index, "extracted": extracted,
            "data": result["data"], "enriched": result["enriched"],
            "flags": result["flags"], "kind": doc.kind,
            "page_count": doc.page_count,
        }
        return JSONResponse({
            "ok": True, "session_id": sid, "kind": doc.kind,
            "page_count": doc.page_count,
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
