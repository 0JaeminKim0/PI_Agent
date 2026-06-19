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

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=4000,
        system=SYSTEM,
        tools=[INDEX_TOOL],
        tool_choice={"type": "tool", "name": "build_index"},
        messages=[{
            "role": "user",
            "content": f"다음은 페이지별 텍스트 인덱스다. 페이지 지도를 만들어라.\n\n{text_index[:150000]}",
        }],
    )
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use" and block.name == "build_index":
            return block.input
    raise ValueError("인덱싱 결과(tool_use)를 받지 못했습니다.")
