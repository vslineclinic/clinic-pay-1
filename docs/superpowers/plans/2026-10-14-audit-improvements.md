# clinic-pay Audit Improvements Implementation Plan

> For agentic workers: use the approved spec and task boundaries below. User already authorized development. Independent worktrees avoid app.py conflicts; integrate and review each change before final delivery.

**Goal:** 감사 12를 제외한 19개 항목을 고치고 현행 지출관리앱의 디자인 기반을 적용한다.
**Architecture:** 기존 app.py 경로를 유지하며 공통 계산을 고친다. 병렬 구현은 개별 worktree에서 수행하고 함수 단위로 통합한다.
**Tech Stack:** Python, Streamlit, pandas, openpyxl, pytest.
**Spec:** ../specs/2026-10-14-audit-improvements.md

## Global Constraints
- GOOGLE_API_KEY 입력칸 기본값 전달 동작 유지.
- 결정론적 계산, 베가스 기준 부호, import-safe 유지.
- 실데이터·외부 API 호출 없는 합성 회귀 검증.
- DEVLOG는 코드와 같은 커밋에 기록.

## Review Focus
- 단일/다중 날짜 및 날짜 결측 입력은 선택일 검증을 우회하지 않아야 한다.
- 승인/취소·부분결제·동명이인·승인번호 접미사 충돌은 근거 없는 확정 연결을 만들면 안 된다.
- 기간·화면·엑셀은 같은 환불 순금액과 플랫폼 차이를 보여야 한다.
- 비로그인 접근·공개 시트 주소·AI 전송·캐시는 환자 정보가 의도치 않게 노출되지 않아야 한다.
- 실제 지원 최소 Streamlit과 데스크톱/모바일 UI가 로드되어야 한다.

## Task 1 — 입력과 날짜 (감사 1,10,11)
Files: app.py load_file, filter_to_single_date, load_gsheet_daily; tests/test_parsers.py, test_gsheet.py, new test_dates.py.
- [x] 기존 합성 재현을 실패 테스트로 고정한다.
- [x] CSV 머리글과 선택 날짜/불명 날짜를 검증하고 gviz sentinel을 최신 응답에 대조한다.
- [x] 관련 테스트와 전체 pytest를 실행하고 DEVLOG와 함께 커밋한다.

## Task 2 — 환불과 모호한 연결 (감사 3,4,5,6)
Files: app.py find_channel_suspects, build_verification and shared matching keys; tests/test_verification.py, test_reconciliation.py.
- [x] 정상 취소·부분 연결·동명이인·긴 승인번호 충돌을 실패 테스트로 고정한다.
- [x] 순금액 및 완전한 연결 근거를 사용하고 모호하면 검토 후보로 남긴다.
- [x] 관련 테스트와 전체 pytest, DEVLOG를 포함해 커밋한다.

## Task 3 — 기간·내보내기·성능 (감사 2,7,9,17)
Files: app.py period reconciliation/detail helpers, split matching helper, build_ai_excel; tests/test_period.py, test_matching.py, test_outputs.py.
- [x] 같은 승인번호의 금액 차이/부분 취소/채널 혼합, 플랫폼 단독 차이, 엑셀 비교 열을 실패 테스트로 고정한다.
- [x] 실제 불일치가 상세에서 사라지지 않게 하고 조합 탐색의 반복을 제한한다.
- [x] 500+10 합성 부하와 기능 회귀를 확인하고 DEVLOG와 커밋한다.

## Task 4 — 개인정보·설정·AI 캐시 (감사 14,15,16)
Files: app.py settings/access/Google HTTP/AI data/cache; tests/test_privacy.py, test_gsheet.py; README configuration.
- [x] 접근제어·비공개 시트·가명화·캐시 만료/모델 구분의 실패 테스트를 만든다.
- [x] 하드코딩 운영 주소·기본 파일 암호를 제거하고 비공개 Google 읽기를 지원한다.
- [x] API 키 입력칸은 유지하되 AI 전송은 가명화하고 캐시는 세션+TTL로 제한한다.
- [x] 오프라인 테스트 후 운영 설정 전환 방법과 DEVLOG를 함께 커밋한다.

## Task 5 — UI·호환성·의존성 (감사 8,18,19,20 + 디자인)
Files: app.py main/theme/period UI, theme assets/config, requirements.txt; real Streamlit smoke test.
- [x] 환불 누락 보조 화면·합계 일치 안내의 회귀를 고정한다.
- [x] 공통 순금액과 검증 상태를 UI에 연결하고 최소 버전 호환 API를 쓴다.
- [x] 확인된 지출앱 색상·폰트·로고를 적용하고 미사용 anthropic을 제거한다.
- [x] 실제 Streamlit AppTest와 시각 검증, DEVLOG를 포함한다.

## Task 6 — 엑셀 안전·통합·인계 (감사 13)
Files: app.py Excel output helpers, tests/test_outputs.py; CI, README/HANDOVER/DEVLOG.
- [x] =1+1 문자열이 수식으로 실행되지 않는 실패 테스트를 쓴다.
- [x] 모든 XLSX 쓰기 경로에서 외부 문자열을 리터럴로 저장한다.
- [x] 전체 테스트, 최소버전 UI, 벤치마크와 독립 리뷰를 통과한다.
- [x] 감사 12 보존과 최신 인계 문서·PR 전달용 변경을 준비한다.
