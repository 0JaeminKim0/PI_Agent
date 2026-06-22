"""
② 구조 인덱싱 (싼 패스, 텍스트만, Haiku)
196p를 한 번에 던지지 않기 위해, 먼저 "어느 페이지가 어느 혁신과제/세부과제인지"
페이지 지도를 만든다. 이 지도가 ③ 추출의 청크 경계와 ④ 검증의 기준점이 된다.

출력 스키마(tool_use 강제):
{
  "domain": "생산계획(PS)",
  "owner_text": "...",
  "tasks": [
    {
      "task_id": "1",
      "task_name": "...",
      "pages": [10, 11, 12],              # 이 혁신과제 관련 페이지(개요/To-Be 포함)
      "subtasks": [
        {"sub_id": "1.1", "sub_name": "...", "pages": [13, 14]}
      ]
    }
  ]
}
"""
from __future__ import annotations

import os
import json

import anthropic

HAIKU_MODEL = os.environ.get("ANTHROPIC_INDEX_MODEL", "claude-3-5-haiku-20241022")

INDEX_TOOL = {
    "name": "build_index",
    "description": "혁신과제 상세정의서의 페이지 구조를 혁신과제/세부과제 단위로 매핑한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "도메인명 (예: '생산계획(PS)')"},
            "owner_text": {"type": "string", "description": "과제오너 명단(있으면)"},
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "혁신과제 번호 ('1','2'...)"},
                        "task_name": {"type": "string"},
                        "pages": {
                            "type": "array", "items": {"type": "integer"},
                            "description": "이 혁신과제(개요/To-Be 등) 관련 페이지 번호들",
                        },
                        "subtasks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "sub_id": {"type": "string", "description": "'1.1','1.2'..."},
                                    "sub_name": {"type": "string"},
                                    "pages": {
                                        "type": "array", "items": {"type": "integer"},
                                        "description": "이 세부과제 관련 페이지 번호들",
                                    },
                                },
                                "required": ["sub_id", "sub_name", "pages"],
                            },
                        },
                    },
                    "required": ["task_id", "task_name", "pages", "subtasks"],
                },
            },
        },
        "required": ["domain", "tasks"],
    },
}

SYSTEM = """너는 HD현대그룹 PI '혁신과제 상세정의서'의 목차/구조 분석기다.
문서는 보통 [혁신과제 리스트] → 과제별 [과제 개요] → [To-Be] → [세부 실행 과제 리스트]
→ [세부 실행 과제 개요/구현 내용] 순으로 반복된다.

너의 임무는 '내용 요약'이 아니라 '페이지 지도'를 만드는 것이다:
- 각 혁신과제(task_id, task_name)와 그 관련 페이지 범위
- 각 세부과제(sub_id, sub_name)와 그 관련 페이지 범위
정확한 본문 추출은 다음 단계에서 한다. 여기서는 페이지 귀속만 정확히 한다.
반드시 build_index 도구를 호출해 결과를 낸다."""


def build_index(text_index: str) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 가 설정되지 않았습니다.")

    # 입력 길이 진단 (잘림 여부 파악)
    total = len(text_index)
    INPUT_LIMIT = 400000  # 과제 누락 방지: 입력 컷오프를 크게
    if total > INPUT_LIMIT:
        print(f"[indexer] WARNING: 입력 텍스트 {total}자 > {INPUT_LIMIT}자 → 뒷부분 잘림 가능")
    payload = text_index[:INPUT_LIMIT]

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=8000,  # tool_use JSON 출력 잘림 방지 (3+ 과제 전체 페이지맵)
        system=SYSTEM,
        tools=[INDEX_TOOL],
        tool_choice={"type": "tool", "name": "build_index"},
        messages=[{
            "role": "user",
            "content": f"다음은 페이지별 텍스트 인덱스다. 문서의 '모든' 혁신과제를 빠짐없이 매핑하라.\n\n{payload}",
        }],
    )

    # 출력이 max_tokens 로 잘리면 tool_use JSON 이 불완전해져 과제가 누락됨
    if getattr(resp, "stop_reason", "") == "max_tokens":
        print("[indexer] ERROR: 출력이 max_tokens 로 잘렸습니다. 일부 과제 누락 가능.")

    for block in resp.content:
        if getattr(block, "type", "") == "tool_use" and block.name == "build_index":
            result = block.input
            n = len(result.get("tasks", []))
            print(f"[indexer] 혁신과제 {n}개 인덱싱됨 "
                  f"(stop_reason={getattr(resp, 'stop_reason', '?')}, input={total}자)")
            return result
    raise ValueError("인덱싱 결과(tool_use)를 받지 못했습니다.")


def index_document(doc, per_page: int = 1800) -> tuple[dict, str]:
    """
    ② 하이브리드 인덱싱 진입점.
    1) 코드 인덱서(규칙 기반) 우선 시도
    2) 코드가 None(과제<2 또는 과제명 부족)이면 Haiku 폴백
    반환: (index_dict, method)  method ∈ {"code","llm"}
    """
    from code_indexer import build_code_index

    # 1) 코드 우선
    try:
        code_idx = build_code_index(doc)
    except Exception as e:
        print(f"[index_document] 코드 인덱서 예외 → LLM 폴백: {e}")
        code_idx = None

    if code_idx and len(code_idx.get("tasks", [])) >= 2:
        return code_idx, "code"

    # 2) Haiku 폴백
    print("[index_document] LLM(Haiku) 폴백 실행")
    llm_idx = build_index(doc.text_index(max_chars_per_page=per_page))
    return llm_idx, "llm"
