# PI Agent Dashboard

혁신과제 상세정의서(PDF/PPT) → **PI 검토 결과서(Excel)** 변환 대시보드.
드래그앤드롭으로 문서를 올리면 **인덱싱(Haiku) → 추출(Opus·비전) → 검증(코드) → 사람 검토** 를 거쳐
기존 `fill_template.py` 로 검토 결과서 xlsx 를 생성합니다.

## 핵심 설계 (5단계 파이프라인)
| 단계 | 내용 | 담당 |
|---|---|---|
| ① 정규화 | PDF=텍스트(pdfplumber)+페이지이미지(PyMuPDF) / PPTX=텍스트(python-pptx) | 코드 |
| ② 구조 인덱싱 | 페이지 → 혁신과제/세부과제 지도(196p 통째로 안 던짐) · **하이브리드: 코드(규칙) 우선 → 실패 시 Haiku 폴백**. 실문서(PS모듈, 196p)에서 `과제 개요 N.` / `세부 실행 과제 개요 N.M` 헤더를 1차 신호로 사용(layout 다중공백 보정), 혁신과제 3개·세부과제 9개를 LLM 호출 없이 정확 추출 검증 | 코드 + **Haiku** |
| ③ 세부과제 추출 | [페이지이미지+텍스트] → tool_use 스키마 강제, 필드별 출처페이지·신뢰도 | **Opus(비전)** |
| ④ 검증·정규화 | ID 연속성 / 개수 대조 / 솔루션 화이트리스트 / 저신뢰 플래그 | 코드 |
| ⑤ 검토 게이트 | 좌(필드·플래그) ↔ 우(출처 페이지 원문) 1:1 대조 + 인라인 수정 | **사람** |

> **`fill_template.py` 는 일절 변경하지 않습니다.** 확정된 `pi_data.json` 스키마만 subprocess 로 전달합니다.
> 결정적인 것은 코드(도메인·오너→사업부 매핑·검증), 판단은 LLM(G/D/S 요약·솔루션 판독), 최종 책임은 사람.

## 기능 / API
| 경로 | 메서드 | 설명 |
|---|---|---|
| `/` | GET | 대시보드 UI |
| `/api/health` | GET | 상태·API키 여부·솔루션 화이트리스트 |
| `/api/process` | POST (file) | 업로드 → ①~④ 수행, 검토용 enriched/flags 반환 |
| `/api/session/{sid}/page/{n}` | GET | 출처 페이지 이미지(PDF) / 텍스트(PPTX) |
| `/api/session/{sid}/page/{n}/text` | GET | 페이지 원문 텍스트 |
| `/api/generate` | POST (data_json) | 확정 JSON → `fill_template.py` → xlsx 다운로드 |
| `/api/demo` | GET | 데모 데이터(분석 없이 즉시 Excel 테스트) |

## 컬럼 매핑 (검토 결과서)
A 도메인 / B 과제ID / C 과제명 / D 과제개요 / E 세부ID / F 세부실행과제명 / G 과제정의 /
H 관련프로세스ID(공란) / I~P 사업부(오너 기준 O) / Q 과제오너 / R 솔루션 / S 기대효과-정성 / T 기대효과-정량

## 로컬 실행
```bash
pip install -r requirements.txt
cd app && uvicorn main:app --host 0.0.0.0 --port 3000
# 또는 PM2: pm2 start ecosystem.config.cjs
```
`.env.example` 을 `.env` 로 복사하고 `ANTHROPIC_API_KEY` 를 채우면 분석까지 동작합니다.
(키가 없어도 "데모 데이터로 바로 Excel" 은 동작)

## Railway 배포
1. GitHub 저장소 연결 또는 `railway up`
2. **Variables** 에 환경변수 등록:
   - `ANTHROPIC_API_KEY` (필수)
   - `ANTHROPIC_INDEX_MODEL` (선택, 기본 `claude-3-5-haiku-20241022`)
   - `ANTHROPIC_EXTRACT_MODEL` (선택, 기본 `claude-opus-4-20250514`)
3. 빌드: Nixpacks(`nixpacks.toml`) · 시작: `cd app && uvicorn main:app --host 0.0.0.0 --port $PORT`

배포 파일: `requirements.txt`, `Procfile`, `railway.json`, `nixpacks.toml`, `runtime.txt`

## 데이터 / 저장
- 세션은 **인메모리**(단일 인스턴스 기준). 페이지 이미지는 임시 디렉토리에 저장(ephemeral).
- 영구 저장 불필요 — 결과 xlsx 는 다운로드로 즉시 전달.

## 기술 스택
FastAPI · Anthropic Claude(Haiku/Opus, tool_use+vision) · pdfplumber · PyMuPDF · python-pptx · openpyxl

## 미구현 / 다음 단계
- PPTX 페이지 **이미지** 대조(현재 텍스트 대조) — LibreOffice 필요해 의도적 제외
- 다중 인스턴스 확장 시 세션 외부 저장소(Redis 등) 필요
- 관련프로세스 ID(H열) 매핑표 연동
- 이미지 기반(스캔) PDF의 OCR

## 상태
- ✅ 데모 → Excel E2E 동작 / 정규화·검증 로직 검증 완료
- ⏳ 실제 PDF 분석은 `ANTHROPIC_API_KEY` 설정 후 동작
- **Last Updated**: 2026-06-19
