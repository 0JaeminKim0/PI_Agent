"""
③ 세부과제 추출 (Opus, 비전+텍스트, tool_use 스키마 강제)
②인덱스의 혁신과제 단위로 청크를 만들어, 해당 페이지의 [이미지+텍스트]를 Opus에 주고
정해진 스키마로만 추출한다. 자유서술 금지 → 드리프트 차단.
필드마다 source_pages(출처 페이지) / confidence(신뢰도) 를 함께 받는다.
"""
from __future__ import annotations

import os
import base64

import anthropic

from extract import Document

OPUS_MODEL = os.environ.get("ANTHROPIC_EXTRACT_MODEL", "claude-opus-4-20250514")
SOLUTION_WHITELIST = ["Hi-APS", "OASIS", "MAPS", "NexFrame", "APS"]

# field 값 + 출처/신뢰도를 묶는 공통 형태
_FIELD = {
    "type": "object",
    "properties": {
        "value": {"type": "string"},
        "source_pages": {"type": "array", "items": {"type": "integer"}},
        "confidence": {"type": "number", "description": "0.0~1.0"},
    },
    "required": ["value", "source_pages", "confidence"],
}

EXTRACT_TOOL = {
    "name": "extract_task",
    "description": "하나의 혁신과제와 그 세부 실행과제들을 PI 검토 결과서 스키마로 추출한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "task_name": _FIELD,
            "task_overview": _FIELD,   # D 과제개요
            "effect_q": _FIELD,        # S 기대효과-정성
            "effect_n": _FIELD,        # T 기대효과-정량 (수치 없으면 value="")
            "subtasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sub_id": {"type": "string"},
                        "sub_name": _FIELD,        # F 세부실행과제명
                        "definition": _FIELD,      # G 과제정의 (1~2문장 명사형)
                        "solution": _FIELD,        # R 시스템/솔루션 (화이트리스트)
                    },
                    "required": ["sub_id", "sub_name", "definition", "solution"],
                },
            },
        },
        "required": ["task_id", "task_name", "subtasks"],
    },
}

SYSTEM = f"""너는 HD현대그룹 PI '혁신과제 상세정의서'에서 한 혁신과제 구간을 분석해
PI 검토 결과서 스키마로 추출하는 전문가다. 반드시 extract_task 도구로만 응답한다.

[추출 규칙]
- task_overview(D): 과제정의 + 추진목적을 요약.
- definition(G): 구현방안/구현내용을 1~2문장, 명사형 종결('~한다/~함')로 요약.
- solution(R): 문서의 '검토 솔루션'. 다음 화이트리스트 표기만 사용: {", ".join(SOLUTION_WHITELIST)}.
  여러 개면 쉼표로. 문서에 없으면 value="".
- effect_n(T, 정량): 문서에 명시된 수치(%, 시간/일 단축 등)가 있을 때만. 없으면 value="".
- effect_q(S, 정성): 기대효과의 정성 항목 요약.
- 모든 필드에 source_pages(근거가 된 실제 페이지 번호)와 confidence(0~1)를 정확히 기입.
- 페이지 이미지와 텍스트가 충돌하면 이미지를 우선하되, 불확실하면 confidence를 낮춘다.
- 추측으로 칸을 채우지 말 것. 근거가 약하면 confidence를 낮추고 값을 비워도 된다."""


def _img_block(image_path: str):
    with open(image_path, "rb") as f:
        b64 = base64.standard_b64encode(f.read()).decode()
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": b64},
    }


def extract_task(doc: Document, task_meta: dict, max_images: int = 8) -> dict:
    """task_meta: indexer 결과의 task 한 개 (task_id, task_name, pages, subtasks[])."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 가 설정되지 않았습니다.")

    # 이 과제 구간의 페이지 모으기 (과제 + 세부과제 페이지 합집합)
    page_nos: set[int] = set(task_meta.get("pages", []))
    for st in task_meta.get("subtasks", []):
        page_nos.update(st.get("pages", []))
    page_nos = sorted(p for p in page_nos if 1 <= p <= doc.page_count)
    pages = [doc.pages[p - 1] for p in page_nos]

    # 텍스트 컨텍스트
    text_ctx = "\n\n".join(f"[PAGE {p.page_no}]\n{p.text}" for p in pages)

    content: list = [{
        "type": "text",
        "text": (
            f"혁신과제 #{task_meta.get('task_id')} '{task_meta.get('task_name','')}' 구간을 추출하라.\n"
            f"인덱스가 추정한 세부과제: "
            + ", ".join(f"{s.get('sub_id')} {s.get('sub_name','')}" for s in task_meta.get('subtasks', []))
            + f"\n\n----- 텍스트 -----\n{text_ctx[:60000]}\n"
        ),
    }]
    # 페이지 이미지 (PDF만 존재). 너무 많으면 앞쪽 위주로 제한.
    img_pages = [p for p in pages if p.image_path and os.path.exists(p.image_path)]
    for p in img_pages[:max_images]:
        content.append({"type": "text", "text": f"[이미지: PAGE {p.page_no}]"})
        content.append(_img_block(p.image_path))

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=OPUS_MODEL,
        max_tokens=6000,
        system=SYSTEM,
        tools=[EXTRACT_TOOL],
        tool_choice={"type": "tool", "name": "extract_task"},
        messages=[{"role": "user", "content": content}],
    )
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use" and block.name == "extract_task":
            out = block.input
            out.setdefault("task_id", task_meta.get("task_id", ""))
            return out
    raise ValueError(f"추출 결과(tool_use)를 받지 못했습니다. task={task_meta.get('task_id')}")
