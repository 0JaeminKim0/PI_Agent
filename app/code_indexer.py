"""
② 구조 인덱싱 — 코드(규칙 기반) 버전.
SKILL.md 의 반복 패턴을 정규식으로 잡아 page→혁신과제/세부과제 맵을 만든다.
LLM 호출 없음 → 잘림/비용/지연 0. 패턴이 규칙적이면 LLM보다 정확.

문서 패턴(예):
  [혁신과제 리스트] ...
  과제별: [과제 개요] / [To-Be] / [세부 실행 과제 리스트] / [세부 실행 과제 개요]
  번호: 혁신과제 '1','2','3' / 세부과제 '1.1','1.2'

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

# ---- 헤더 패턴 (띄어쓰기 유연) ----
RE_TASK_LIST = re.compile(r"혁신\s*과제\s*리스트")
RE_OVERVIEW = re.compile(r"과제\s*개요")
RE_SUB_LIST = re.compile(r"세부\s*실행\s*과제\s*리스트")
RE_SUB_DETAIL = re.compile(r"세부\s*실행\s*과제\s*(개요|구현)")
RE_TOBE = re.compile(r"To[\s\-]?Be", re.IGNORECASE)

# 도메인: '생산계획(PS)' 처럼 한글+영문약어(괄호)
RE_DOMAIN = re.compile(r"([가-힣A-Za-z]+\s*\(\s*[A-Za-z]{1,6}\s*\))")

# 혁신과제 번호+명: 줄 시작의 "1 ...", "1." / 세부: "1.1 ..."
# (줄을 strip 한 뒤 매칭 — layout 추출의 선행 공백 대응)
RE_SUB_ID = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})(?!\d)")
RE_TASK_ID_LINE = re.compile(r"^(\d{1,2})[\.\)]?\s+(\S.+)$")
RE_SUB_ID_LINE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?!\d)\s*[\.\)]?\s*(\S.*)?$")


def _domain(doc: Document) -> str:
    # 앞쪽 페이지에서 도메인 후보 탐색
    for p in doc.pages[:5]:
        m = RE_DOMAIN.search(p.text or "")
        if m:
            return m.group(1).replace(" ", "")
    return ""


def _owner_text(doc: Document) -> str:
    # '과제오너' 또는 '상무/전무(...)' 명단이 있는 라인 추출
    for p in doc.pages[:30]:
        for line in (p.text or "").splitlines():
            if ("과제오너" in line or "과제 오너" in line) and ("(" in line):
                return line.split(":", 1)[-1].strip()
            if re.search(r"(상무|전무|부사장|사장|이사)\s*\(", line) and line.count("(") >= 2:
                return line.strip()
    return ""


def build_code_index(doc: Document) -> dict | None:
    """규칙 기반 인덱싱. 신뢰할 만한 결과면 dict, 아니면 None(→LLM 폴백)."""
    pages = doc.pages
    n = len(pages)
    if n == 0:
        return None

    # 1) 혁신과제 경계 = "혁신과제 리스트" 또는 "과제 개요" 헤더가 나오는 페이지에서
    #    같은 페이지의 task 번호/명을 잡는다.
    tasks: dict[str, dict] = {}   # task_id -> {name, pages:set, sub:{sub_id:{name,pages:set}}}
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
        # layout 추출의 선행/후행 공백 제거한 라인 목록
        lines = [ln.strip() for ln in text.splitlines()]
        lines = [ln for ln in lines if ln]
        is_overview = bool(RE_OVERVIEW.search(text))

        # 세부과제 ID 가 등장하면 그 과제로 컨텍스트 전환
        sub_ids_here = RE_SUB_ID.findall(text)  # [(major,minor),...]
        if sub_ids_here:
            majors = [a for a, _ in sub_ids_here]
            major = max(set(majors), key=majors.count)
            current_task = major
            t = ensure_task(major)
            t["pages"].add(p.page_no)
            # 세부과제명 추출: 줄 시작이 'x.y 명칭'
            for line in lines:
                m = RE_SUB_ID_LINE.match(line)
                if m and m.group(1) == major:
                    sid = f"{m.group(1)}.{m.group(2)}"
                    name = (m.group(3) or "").strip()
                    name = re.split(r"\s{2,}|\t|·{2,}|\.{3,}", name)[0].strip()[:60]
                    s = ensure_sub(major, sid, name)
                    s["pages"].add(p.page_no)

        # '과제 개요' 페이지 또는 '혁신과제 리스트' 페이지: 과제번호+명 추출
        if is_overview or RE_TASK_LIST.search(text):
            for line in lines:
                # 세부과제 라인(1.1)은 건너뜀
                if RE_SUB_ID_LINE.match(line):
                    continue
                mm = RE_TASK_ID_LINE.match(line)
                if mm:
                    tid = mm.group(1)
                    name = re.split(r"\s{2,}|\t", mm.group(2))[0].strip()[:80]
                    # 너무 짧거나 헤더성 단어는 제외
                    if len(name) >= 2:
                        ensure_task(tid, name)
                        if is_overview:
                            current_task = tid
                            tasks[tid]["pages"].add(p.page_no)

        # 현재 과제가 정해져 있으면 관련 페이지로 귀속
        if current_task:
            tasks[current_task]["pages"].add(p.page_no)

    # 2) 정리 / 검증
    if not tasks:
        return None

    out_tasks = []
    for tid in sorted(order, key=lambda x: int(x) if x.isdigit() else 999):
        t = tasks[tid]
        subs = []
        for sid in sorted(t["subs"].keys(), key=_sid_key):
            s = t["subs"][sid]
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

    result = {
        "domain": _domain(doc),
        "owner_text": _owner_text(doc),
        "tasks": out_tasks,
    }

    # 신뢰도 판단: 과제 2개 미만이거나, 과제명이 대부분 비면 폴백
    if len(out_tasks) < 2:
        print(f"[code_indexer] 과제 {len(out_tasks)}개 — 신뢰 부족, LLM 폴백 권장")
        return None
    named = sum(1 for t in out_tasks if t["task_name"])
    if named < max(1, len(out_tasks) // 2):
        print(f"[code_indexer] 과제명 추출 부족({named}/{len(out_tasks)}) — LLM 폴백 권장")
        return None

    print(f"[code_indexer] 코드 인덱싱 성공: 혁신과제 {len(out_tasks)}개")
    return result


def _sid_key(sid: str):
    try:
        a, b = sid.split(".")
        return (int(a), int(b))
    except Exception:
        return (999, 999)
