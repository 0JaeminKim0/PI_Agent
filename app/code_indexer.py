"""
② 구조 인덱싱 — 코드(규칙 기반) 버전.
SKILL.md 의 반복 패턴을 정규식으로 잡아 page→혁신과제/세부과제 맵을 만든다.
LLM 호출 없음 → 잘림/비용/지연 0. 패턴이 규칙적이면 LLM보다 정확.

핵심 신호(실문서 PS모듈_Test.pdf 검증 기준):
  1) "과제 개요 N. {과제명}"            ← 혁신과제 ID + 과제명 (가장 신뢰 높음)
  2) "세부 실행 과제 개요 N.M {세부과제명}"  ← 세부과제 ID + 명
  3) "세부 실행 과제 리스트" 페이지의 "N.M ..." 라인 ← 세부과제 보강
  4) "혁신과제 리스트" 페이지            ← 과제↔페이지 귀속 보강

주의: pdfplumber layout=True 추출은 단어 사이에 다중 공백을 넣는다.
      → 매 라인을 'collapse(공백 1개)' 한 뒤 정규식 매칭한다.

반환 스키마는 indexer.build_index 와 동일:
{
  "domain": str, "owner_text": str,
  "tasks": [{"task_id","task_name","pages":[...],
             "subtasks":[{"sub_id","sub_name","pages":[...]}]}]
}
실패(패턴 불충분) 시 None 을 반환 → 호출측에서 LLM 폴백.
"""
from __future__ import annotations

import re

from extract import Document

# ---- 헤더 패턴 (collapse 후 매칭, 띄어쓰기 1개 기준) ----
RE_TASK_LIST = re.compile(r"혁신\s?과제\s?리스트")
RE_SUB_LIST = re.compile(r"세부\s?실행\s?과제\s?리스트")

# "과제 개요 1. 계획시스템 고도화"  (세부 개요와 충돌 방지: '세부 실행' 선행 제외)
RE_TASK_OVERVIEW = re.compile(r"(?<!행\s)(?<!행)과제\s?개요\s+(\d{1,2})[\.\)]?\s+(.+)")
# "세부 실행 과제 개요 2.2 중일정 기준 ..."
RE_SUB_OVERVIEW = re.compile(r"세부\s?실행\s?과제\s?개요\s+(\d{1,2})\.(\d{1,2})\s+(.+)")
# 리스트/본문에 등장하는 "N.M  명칭"
RE_SUB_LINE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?!\d)\s+(.+)$")

# 도메인: '생산계획(PS)' 처럼 한글+영문약어(괄호)
RE_DOMAIN = re.compile(r"([가-힣A-Za-z]+\s?\(\s?[A-Za-z]{1,6}\s?\))")

# 오너/사업부 표 (두 줄 짝짓기)
RE_BU_HEADER = re.compile(r"적용\s?사업부\s+(.+)")
RE_OWNER_ROW = re.compile(r"(?:및\s?)?과제\s?오너\s+(.+)")
RE_TITLE = re.compile(r"(상무|전무|부사장|사장|이사|부장|상무보|전무보)")

# 문서 사업부 표기 → fill_template/검증 컬럼 키 매핑
BU_HEADER_MAP = {
    "조선": "조선", "해양": "해양", "특수선": "특수", "특수": "특수",
    "미포": "미포", "삼호": "삼호",
    "한조": "HD한조", "한국조선해양": "HD한조", "KSOE": "HD한조",
    "HHIP": "HHIP", "HVS": "HVS",
}


def _collapse(s: str) -> str:
    """다중 공백/탭을 공백 1개로. layout 추출 보정."""
    return re.sub(r"\s+", " ", s).strip()


def _clean_name(name: str) -> str:
    """과제/세부과제명 꼬리의 군더더기(솔루션명, 표 잔여물) 제거."""
    name = name.strip()
    # 솔루션/유형 토큰이 뒤에 붙는 경우 컷
    name = re.split(r"\s(?:Hi-APS|OASIS|MAPS|NexFrame|APS|P\s?D\s?S\s?O)\b", name)[0]
    # 불릿/대시 이후 설명 컷
    name = re.split(r"\s[▪•\-]\s", name)[0]
    return name.strip()[:80]


# 도메인 후보에서 제외할 일반 용어(프로젝트 명칭 등)
DOMAIN_STOP = {"혁신(PI)", "프로세스혁신(PI)", "프로젝트(PI)"}


def _domain(doc: Document) -> str:
    """표지/앞쪽에서 도메인 추출. '혁신(PI)' 같은 프로젝트 명칭은 제외.
    단독 라인(예: '생산계획(PS)')을 최우선으로 채택."""
    fallback = ""
    for p in doc.pages[:6]:
        for ln in (p.text or "").splitlines():
            line = _collapse(ln)
            m = RE_DOMAIN.search(line)
            if not m:
                continue
            cand = m.group(1).replace(" ", "")
            if cand in DOMAIN_STOP:
                continue
            # 단독 라인(짧고 괄호 도메인만) → 즉시 채택
            if len(line) <= len(cand) + 2:
                return cand
            if not fallback:
                fallback = cand
    return fallback


def _group_owner_tokens(headers: list[str], owner_raw: str) -> dict[str, str]:
    """'N/A N/A 박상훈 상무 최해주 상무 ...' 를 사업부 헤더 순서대로 1:1 매핑.

    그룹핑 규칙:
      - 'N/A' / '-' → 한 칸(빈 오너)
      - '이름 + 직책(상무/전무/...)' → 2토큰이 한 칸
      - 직책 없는 단독 토큰도 한 칸으로 처리(방어)
    반환: {컬럼키('조선'..): '박상훈 상무'}
    """
    toks = owner_raw.split()
    cells: list[str] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.upper() in ("N/A", "NA", "-", "·", "—"):
            cells.append("")
            i += 1
            continue
        # 이름 + 직책 묶기
        if i + 1 < len(toks) and RE_TITLE.fullmatch(toks[i + 1]):
            cells.append(f"{t} {toks[i + 1]}")
            i += 2
        else:
            cells.append(t)
            i += 1

    mapping: dict[str, str] = {}
    for h, cell in zip(headers, cells):
        col = BU_HEADER_MAP.get(h)
        if col and cell:
            mapping[col] = cell
    return mapping


def _owner_bu(doc: Document) -> tuple[dict[str, str], str]:
    """과제 개요 페이지의 '적용 사업부' + '과제 오너' 두 줄을 짝지어
    사업부별 오너명을 코드로 정확히 추출.
    반환: (owner_by_bu{'조선':'박상훈 상무',...}, owner_text(원문 한 줄)).
    """
    for p in doc.pages[:160]:
        lines = [_collapse(l) for l in (p.text or "").splitlines()]
        headers = None
        owner_raw = None
        for l in lines:
            mh = RE_BU_HEADER.match(l)
            if mh:
                headers = mh.group(1).split()
            mo = RE_OWNER_ROW.match(l)
            if mo:
                owner_raw = mo.group(1).strip()
            if headers and owner_raw:
                mapping = _group_owner_tokens(headers, owner_raw)
                if mapping:
                    return mapping, owner_raw
    return {}, ""


def _sid_key(sid: str):
    try:
        a, b = sid.split(".")
        return (int(a), int(b))
    except Exception:
        return (999, 999)


def build_code_index(doc: Document) -> dict | None:
    """규칙 기반 인덱싱. 신뢰할 만한 결과면 dict, 아니면 None(→LLM 폴백)."""
    pages = doc.pages
    if not pages:
        return None

    tasks: dict[str, dict] = {}   # tid -> {name, pages:set, subs:{sid:{name,pages:set}}}
    order: list[str] = []

    def ensure_task(tid: str, name: str = "") -> dict:
        if tid not in tasks:
            tasks[tid] = {"name": name, "pages": set(), "subs": {}}
            order.append(tid)
        elif name and not tasks[tid]["name"]:
            tasks[tid]["name"] = name
        return tasks[tid]

    def ensure_sub(tid: str, sid: str, name: str = "") -> dict:
        t = ensure_task(tid)
        if sid not in t["subs"]:
            t["subs"][sid] = {"name": name, "pages": set()}
        elif name and not t["subs"][sid]["name"]:
            t["subs"][sid]["name"] = name
        return t["subs"][sid]

    current_task: str | None = None

    for p in pages:
        text = p.text or ""
        lines = [_collapse(ln) for ln in text.splitlines()]
        lines = [ln for ln in lines if ln]

        page_is_task_list = any(RE_TASK_LIST.search(ln) for ln in lines)
        page_is_sub_list = any(RE_SUB_LIST.search(ln) for ln in lines)

        for line in lines:
            # 1) 혁신과제 개요 헤더 → 가장 신뢰 높은 과제 ID/명
            m = RE_TASK_OVERVIEW.match(line)
            if m:
                tid = m.group(1)
                name = _clean_name(m.group(2))
                if len(name) >= 2:
                    ensure_task(tid, name)["pages"].add(p.page_no)
                    current_task = tid
                continue

            # 2) 세부 실행 과제 개요 헤더 → 세부 ID/명
            ms = RE_SUB_OVERVIEW.match(line)
            if ms:
                tid, minor = ms.group(1), ms.group(2)
                sid = f"{tid}.{minor}"
                ensure_sub(tid, sid, _clean_name(ms.group(3)))["pages"].add(p.page_no)
                tasks[tid]["pages"].add(p.page_no)
                current_task = tid
                continue

            # 3) 리스트/본문의 "N.M 명칭" → 세부 보강 (리스트 페이지에서만 이름 신뢰)
            msl = RE_SUB_LINE.match(line)
            if msl:
                tid, minor = msl.group(1), msl.group(2)
                sid = f"{tid}.{minor}"
                nm = _clean_name(msl.group(3)) if (page_is_sub_list or page_is_task_list) else ""
                ensure_sub(tid, sid, nm)["pages"].add(p.page_no)
                # 리스트 페이지면 과제 자체에도 페이지 귀속
                if page_is_sub_list or page_is_task_list:
                    tasks[tid]["pages"].add(p.page_no)

        # 현재 과제 컨텍스트 페이지 귀속(개요 이후 상세 페이지들)
        if current_task and current_task in tasks:
            tasks[current_task]["pages"].add(p.page_no)

    if not tasks:
        return None

    # 신뢰 과제 = '개요 헤더'로 잡힌 (이름 있는) 과제만 남긴다 → 타임라인 숫자 노이즈 제거
    out_tasks = []
    for tid in sorted(order, key=lambda x: int(x) if x.isdigit() else 999):
        t = tasks[tid]
        if not t["name"]:
            continue  # 개요로 확정되지 않은 ID는 폐기(노이즈)
        subs = []
        for sid in sorted(t["subs"].keys(), key=_sid_key):
            s = t["subs"][sid]
            # 이름 없이 본문 한 페이지에만 스친 항목은 노이즈로 폐기(예: 잘못 잡힌 1.5)
            if not s["name"] and len(s["pages"]) <= 1:
                continue
            subs.append({
                "sub_id": sid,
                "sub_name": s["name"],
                "pages": sorted(s["pages"]),
            })
        out_tasks.append({
            "task_id": tid,
            "task_name": t["name"],
            "pages": sorted(t["pages"]),
            "subtasks": subs,
        })

    owner_by_bu, owner_text = _owner_bu(doc)
    result = {
        "domain": _domain(doc),
        "owner_text": owner_text,
        "owner_by_bu": owner_by_bu,   # {'조선':'박상훈 상무', ...} — 코드로 정확 추출
        "tasks": out_tasks,
    }

    # 신뢰도 판단
    if len(out_tasks) < 2:
        print(f"[code_indexer] 과제 {len(out_tasks)}개 — 신뢰 부족, LLM 폴백 권장")
        return None
    with_subs = sum(1 for t in out_tasks if t["subtasks"])
    if with_subs < max(1, len(out_tasks) // 2):
        print(f"[code_indexer] 세부과제 매핑 부족({with_subs}/{len(out_tasks)}) — LLM 폴백 권장")
        return None

    total_subs = sum(len(t["subtasks"]) for t in out_tasks)
    print(f"[code_indexer] 코드 인덱싱 성공: 혁신과제 {len(out_tasks)}개 / 세부과제 {total_subs}개")
    return result
