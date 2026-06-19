"""
④ 검증 · 정규화 레이어 (코드, LLM 아님)
LLM 출력(③의 task별 결과 + ②인덱스)을 기계적으로 점검·정규화한다.
- 세부과제 ID 연속성 (1.1 → 1.2 → 1.3)
- 인덱스가 명시한 세부과제 개수 ↔ 실제 추출 개수 일치
- 솔루션 화이트리스트(Hi-APS/OASIS/MAPS/NexFrame/APS)만 허용
- 빈칸 / 저신뢰(confidence) 항목 플래그
결과: 검토 게이트(⑤)에서 쓸 flags 목록과, 신뢰도/출처가 보존된 enriched 구조.
"""
from __future__ import annotations

import re

SOLUTION_WHITELIST = ["Hi-APS", "OASIS", "MAPS", "NexFrame", "APS"]
LOW_CONF = 0.6

BU_KEYS = ["조선", "해양", "특수", "미포", "HD한조", "HHIP", "HVS", "삼호"]
# 오너 직책 사업부 → 컬럼 매핑 (SKILL 규칙)
OWNER_BU_RULES = [
    (r"조선", "조선"), (r"해양", "해양"), (r"특수", "특수"),
    (r"미포", "미포"), (r"삼호", "삼호"),
    (r"한조|한국조선", "HD한조"), (r"HHIP", "HHIP"), (r"HVS", "HVS"),
]


def _fv(field):
    """필드(dict{value,...} 또는 raw str)에서 value 추출."""
    if isinstance(field, dict):
        return (field.get("value") or "").strip()
    return (field or "").strip()


def _fc(field):
    if isinstance(field, dict):
        return float(field.get("confidence", 1.0) or 0.0)
    return 1.0


def _fp(field):
    if isinstance(field, dict):
        return field.get("source_pages", []) or []
    return []


def normalize_solution(raw: str) -> tuple[str, list[str]]:
    """화이트리스트 외 토큰 제거. (정규화값, 제거된토큰들) 반환."""
    if not raw:
        return "", []
    tokens = [t.strip() for t in re.split(r"[,/·;]+", raw) if t.strip()]
    kept, dropped = [], []
    for t in tokens:
        match = next((w for w in SOLUTION_WHITELIST if w.lower() == t.lower()), None)
        if match:
            if match not in kept:
                kept.append(match)
        else:
            dropped.append(t)
    return ", ".join(kept), dropped


def infer_owner_bu_map(owner_text: str) -> dict:
    m = {k: "" for k in BU_KEYS}
    for pat, col in OWNER_BU_RULES:
        if re.search(pat, owner_text or ""):
            m[col] = "O"
    return m


def validate(index: dict, extracted_tasks: list[dict]) -> dict:
    """
    index: ②결과, extracted_tasks: ③결과 리스트(task별).
    반환: {data, enriched, flags}
      - data: fill_template.py 가 먹는 pi_data.json 스키마 (값만)
      - enriched: 필드별 value/source_pages/confidence 보존 (⑤ UI용)
      - flags: 검토 필요 항목 리스트
    """
    flags: list[dict] = []
    domain = index.get("domain", "")
    owner_text = index.get("owner_text", "")
    owner_bu_map = infer_owner_bu_map(owner_text)
    if not any(v == "O" for v in owner_bu_map.values()):
        flags.append({"level": "warn", "scope": "owner",
                      "msg": "오너→사업부 매핑이 비었습니다. 수동 확인 필요."})

    # 인덱스의 세부과제 개수 맵
    idx_counts = {t.get("task_id"): len(t.get("subtasks", [])) for t in index.get("tasks", [])}

    data_tasks = []
    enriched_tasks = []

    for t in extracted_tasks:
        tid = str(t.get("task_id", ""))
        subs = t.get("subtasks", []) or []

        # 개수 대조
        expected = idx_counts.get(tid)
        if expected is not None and expected != len(subs):
            flags.append({"level": "warn", "scope": f"task {tid}",
                          "msg": f"세부과제 개수 불일치: 인덱스 {expected} ≠ 추출 {len(subs)}"})

        # ID 연속성
        sub_ids = [str(s.get("sub_id", "")) for s in subs]
        for n, sid in enumerate(sub_ids, start=1):
            expect = f"{tid}.{n}"
            if sid != expect:
                flags.append({"level": "info", "scope": f"task {tid}",
                              "msg": f"세부과제 ID 비연속: '{sid}' (예상 '{expect}')"})

        # 필드 검증 + 정규화
        e_subs = []
        d_subs = []
        for s in subs:
            sid = str(s.get("sub_id", ""))
            sol_raw = _fv(s.get("solution"))
            sol_norm, dropped = normalize_solution(sol_raw)
            if dropped:
                flags.append({"level": "warn", "scope": f"{sid} 솔루션",
                              "msg": f"화이트리스트 외 제거: {', '.join(dropped)}"})
            for fname, label in [("sub_name", "세부과제명"), ("definition", "과제정의")]:
                if not _fv(s.get(fname)):
                    flags.append({"level": "warn", "scope": f"{sid} {label}", "msg": "값 비어있음"})
                elif _fc(s.get(fname)) < LOW_CONF:
                    flags.append({"level": "info", "scope": f"{sid} {label}",
                                  "msg": f"신뢰도 낮음({_fc(s.get(fname)):.2f}) — 출처 페이지 확인 권장"})

            e_subs.append({
                "sub_id": sid,
                "sub_name": {"value": _fv(s.get("sub_name")), "source_pages": _fp(s.get("sub_name")), "confidence": _fc(s.get("sub_name"))},
                "definition": {"value": _fv(s.get("definition")), "source_pages": _fp(s.get("definition")), "confidence": _fc(s.get("definition"))},
                "solution": {"value": sol_norm, "raw": sol_raw, "source_pages": _fp(s.get("solution")), "confidence": _fc(s.get("solution"))},
            })
            d_subs.append({
                "sub_id": sid,
                "sub_name": _fv(s.get("sub_name")),
                "definition": _fv(s.get("definition")),
                "solution": sol_norm,
            })

        # 과제 레벨 필드
        for fname, label in [("task_name", "과제명"), ("task_overview", "과제개요"), ("effect_q", "기대효과-정성")]:
            if not _fv(t.get(fname)):
                flags.append({"level": "info", "scope": f"task {tid} {label}", "msg": "값 비어있음"})

        enriched_tasks.append({
            "task_id": tid,
            "task_name": {"value": _fv(t.get("task_name")), "source_pages": _fp(t.get("task_name")), "confidence": _fc(t.get("task_name"))},
            "task_overview": {"value": _fv(t.get("task_overview")), "source_pages": _fp(t.get("task_overview")), "confidence": _fc(t.get("task_overview"))},
            "effect_q": {"value": _fv(t.get("effect_q")), "source_pages": _fp(t.get("effect_q")), "confidence": _fc(t.get("effect_q"))},
            "effect_n": {"value": _fv(t.get("effect_n")), "source_pages": _fp(t.get("effect_n")), "confidence": _fc(t.get("effect_n"))},
            "subtasks": e_subs,
        })
        data_tasks.append({
            "task_id": tid,
            "task_name": _fv(t.get("task_name")),
            "task_overview": _fv(t.get("task_overview")),
            "effect_q": _fv(t.get("effect_q")),
            "effect_n": _fv(t.get("effect_n")),
            "subtasks": d_subs,
        })

    data = {
        "domain": domain,
        "owner_bu_map": owner_bu_map,
        "owner_text": owner_text,
        "tasks": data_tasks,
    }
    enriched = {
        "domain": domain,
        "owner_text": owner_text,
        "owner_bu_map": owner_bu_map,
        "tasks": enriched_tasks,
    }
    return {"data": data, "enriched": enriched, "flags": flags}
