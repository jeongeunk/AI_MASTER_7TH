# SchemaScout (데이터 명세관)

고객이 작성한 데이터 명세서(엑셀)를 실제 DB와 자동으로 대조·검증해, 최종 제공 가능 명세서를 생성하는 6-Agent LangGraph 파이프라인입니다.

담당자가 수작업으로 하나씩 확인하던 "이 컬럼 실제로 있나? 타입 맞나? 요청한 기간만큼 데이터 있나? 서로 다른 테이블에서 온 컬럼을 실제로 합칠 수 있나?"를 자동화하되, **애매한 판단(유사도 매칭, 타입 불일치, 동일 컬럼명 중복, 조인키 추정)은 담당자 확인(Human-in-the-loop)을 거쳐 확정**하는 구조입니다.

## 파이프라인 개요

```
Parsing Agent → Meta Search Agent → Join Resolution Agent → DB Validation Agent → Classification Agent → Report Agent
   (명세서            (메타DB 매칭          (여러 테이블에 걸친        (실DB 존재/         (최종 태깅)         (엑셀 산출물
    구조화)          + 담당자 확인)        요청이면 조인 가능성        타입/기간 조회         )                  생성)
                                          검증 + 담당자 확인)        + 담당자 확인)
```

거절/미매칭된 컬럼은 Meta Search Agent에서 조기 종결되어 이후 단계를 거치지 않고 바로 Report Agent 집계에 포함됩니다. 요청 컬럼이 단일 테이블뿐이면 Join Resolution Agent는 조인 검증 없이 곧장 다음 단계로 넘어갑니다.

## 최종 태그 체계 (6종, 컬럼 단위)

| 태그 | 의미 |
|---|---|
| `unresolved` | 메타 매칭 실패(유사도 미달, 담당자 거절, 또는 동일 컬럼명이 여러 테이블에 있는데 담당자가 선택하지 않음) |
| `not_found` | 명세에는 있으나 실 DB에 컬럼 없음 |
| `type_mismatch` | 명세 type과 실제 type 불일치, 담당자가 갱신 거절 |
| `period_mismatch` | 요청 기간과 실제 보유 기간이 전혀 겹치지 않음 |
| `full_period` | 요청 기간 전체 제공 가능 (요청 기간 미기재 시 보유 기간 전체) |
| `confirm_period` | 요청 기간 일부만 제공 가능(자동 조정) |

## 조인 가능성 검증 (Join Resolution Agent)

컬럼별 태그와 별개로, 명세서가 서로 다른 테이블의 컬럼을 동시에 요청하면 그 테이블들을 실제로 하나의 결과셋으로 묶을 수 있는지를 검증합니다.

1. `table_relationships`에 이미 등록된 관계(직접 연결 또는 중간 테이블 경유 2-hop)가 있으면 그대로 사용
2. 없으면 이름 일치(월별 grain 테이블끼리는 자동으로 복합키 제안) → 임베딩 유사도 순으로 후보를 찾고, **실제 데이터의 SEMI JOIN 값 포함률(containment)**로 검증 — 이름/설명이 그럴듯해도 값이 안 겹치면 채택하지 않음
3. 근거가 충분한 후보만 담당자 확인(HITL)을 거쳐 확정하고(confidence가 아무리 높아도 자동 확정하지 않음), 승인된 키는 `table_relationships`에 저장해 다음 명세서부터 재사용
4. 조인키에 관련된 모든 테이블의 type/보유기간을 검증해 **조인된 결과가 실제로 유효한 기간(교집합)**을 계산
5. 조인에 필요한 키 컬럼이 사용자가 요청한 컬럼 목록에 없으면(예: `mobile_number` 없이 여러 테이블 컬럼만 요청) 알림 + 담당자 확인을 거쳐 자동으로 요청 목록에 추가

결과는 최종 엑셀의 "조인 가능성 검증" 시트로 별도 노출됩니다.

## 폴더 구조

```
AI_PROJECT/
├── .env                    # 실제 API 키 등 (git 제외)
├── .env.example            # 설정 템플릿
├── .gitignore
├── llm_client.py            # Azure OpenAI 스타일 엔드포인트 클라이언트 (chat / chat_parsed / embed)
├── requirements.txt
├── run_pipeline.py          # LangGraph 파이프라인 실행 드라이버 (콘솔)
├── agents/
│   ├── parsing_agent.py         # 헤더/행 파싱 (규칙 → LLM(Structured Output) → 담당자 확인)
│   ├── meta_search_agent.py     # 메타DB 매칭 (exact → RAG 후보 → LLM(Structured Output) 판단)
│   ├── join_resolution_agent.py # 여러 테이블 조인 가능성 검증 + 누락 조인키 알림
│   ├── db_validation_agent.py   # 실DB 존재/타입/보유기간 검증 (Guardrail: READ_ONLY, SQL 구조 검증)
│   ├── classification_agent.py  # 최종 6종 태그 확정 (결정론적 규칙)
│   ├── report_agent.py          # 결과 취합 + 엑셀 산출물 생성
│   ├── langgraph_pipeline.py    # 6개 Agent를 묶는 StateGraph 정의
│   └── trace.py                 # Agent/tool 실행 트레이싱 (모니터링 화면용)
├── backend/                 # FastAPI (LangGraph를 백그라운드 스레드로 실행 + HITL 폴링 API)
│   ├── main.py                   # uvicorn backend.main:app --reload --port 8000
│   ├── api/{specs,pipeline}.py
│   └── core/pipeline_runner.py
├── frontend/                 # Streamlit 웹 UI
│   ├── app.py                    # streamlit run frontend/app.py (업로드 + 진행 상황)
│   ├── pages/2_모니터링.py        # Agent별 실행 로그 + HITL 확인 카드
│   ├── pages/3_결과_및_다운로드.py
│   ├── sidebar_progress.py
│   └── api_client.py
├── scripts/
│   ├── load_data_db.py                  # xlsx → 실데이터 DB 적재
│   ├── test_setup.py                    # DuckDB + vss 설치/검증
│   ├── fill_meta_db.py                  # 메타 DB column_spec 규칙기반 채우기
│   │                                       (TYPE_MISMATCH_INJECT/PHANTOM_COLUMN_INJECT로
│   │                                        타입불일치·not_found 테스트 케이스도 함께 시드)
│   ├── llm_polish_columns.py            # 메타 설명 LLM으로 다듬기
│   ├── generate_embeddings.py           # 컬럼 설명 임베딩 + vss 인덱스
│   ├── build_glossary_seed.py / llm_expand_glossary.py / generate_glossary_embeddings.py
│   │                                       # 용어집(glossary) 구축 (선택)
│   ├── init_audit_db.py                 # 감사 DB(SQLite) 초기화
│   ├── verify_meta_db.py                # 메타 DB 최종 검증
│   └── build_*_spec.py / verify_*.py    # 테스트 명세서 생성·검증 스크립트 (아래 참고)
├── data/
│   ├── (원본 데이터 xlsx 6개: dim_customer, fact_*)
│   └── (테스트 명세서는 build_*.py로 생성 - 아래 "테스트 명세서" 참고)
├── db/
│   ├── schemascout_meta.duckdb    # 메타 DB (컬럼 명세 + 관계 그래프 + 유사도 검색)
│   ├── schemascout_data.duckdb    # 실데이터 DB (raw_telecom_* 6개 테이블, READ_ONLY로만 접근)
│   ├── schemascout_audit.sqlite   # 감사 DB (확인 이력, 변경 이력)
│   └── langgraph_checkpoints.sqlite  # LangGraph 체크포인터 (interrupt 상태 저장)
├── tests/                    # pytest 단위 테스트
└── docs/
    └── SchemaScout_테이블명세.xlsx
```

## 데이터셋

Kaggle [Telecom Churn Dataset](https://www.kaggle.com/datasets/shivam131019/telecom-churn-dataset)(226컬럼)을 정규화해 `dim_customer` + `fact_*` 5개 테이블로 재구성한 합성 데이터를 사용합니다. 실제 규모 대신 PoC 스케일(기준 고객 5,000명, 월 2% 이탈/신규)로 축소·확장했습니다.

## 로컬 환경 세팅

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`.env.example`을 참고해 `.env`를 채웁니다 (Base URL, API 키, 배포명 등).

## DB 초기화 (최초 1회)

```powershell
python scripts\load_data_db.py        # 실데이터 DB 생성
python scripts\test_setup.py          # 메타 DB에 vss 확장 설치
python scripts\fill_meta_db.py        # column_spec 규칙기반 채우기 (타입불일치·not_found 테스트 케이스 포함)
python scripts\init_audit_db.py       # 감사 DB 생성
python scripts\llm_polish_columns.py  # 메타 설명 LLM으로 다듬기
python scripts\generate_embeddings.py # 임베딩 생성 + vss 인덱스
python scripts\verify_meta_db.py      # 최종 검증
```

## 실행 방법

### 1) 웹 앱 (권장 — HITL 확인 카드, 실행 로그를 화면에서 볼 수 있음)

```powershell
uvicorn backend.main:app --reload --port 8000     # 터미널 1
streamlit run frontend/app.py                      # 터미널 2
```

`http://localhost:8501`에서 명세서를 업로드하면 파이프라인이 시작되고, 왼쪽 사이드바/모니터링 페이지에서 Agent별 진행 상황과 tool 호출 내역을, 담당자 확인이 필요한 시점에는 전용 카드(헤더 매핑, 타입 불일치, 매칭 확신도, 동일 컬럼명 중복, 조인키 추정/누락 등)를 확인·승인/거절할 수 있습니다.

### 2) 콘솔 드라이버

```powershell
python -u run_pipeline.py data\sample_spec.xlsx
```

실행 중 확인이 필요하면 콘솔에 요청이 뜹니다(y/n 또는 인덱스 입력). 응답 후 자동으로 이어서 진행되며, 완료되면 입력 파일과 같은 폴더에 `입력파일명(output)_YYYYMMDD_HHMM.xlsx`로 최종 명세서(컬럼별 검증 결과 + 조인 가능성 검증 시트)가 생성됩니다.

같은 `thread_id`로 재실행하면 중단된 지점부터 이어서 진행됩니다 (LangGraph 체크포인터 덕분에 프로세스를 껐다 켜도 유지됩니다).

## 개별 Agent 단위 테스트

전체 파이프라인 대신 특정 Agent까지만 실행해서 확인하고 싶을 때:

```powershell
python -u agents\parsing_agent.py data\sample_spec.xlsx
python -u agents\meta_search_agent.py data\sample_spec.xlsx
python -u agents\join_resolution_agent.py data\sample_spec.xlsx
python -u agents\db_validation_agent.py data\sample_spec.xlsx
python -u agents\classification_agent.py data\sample_spec.xlsx
python -u agents\report_agent.py data\sample_spec.xlsx
```

## 테스트 명세서

주요 분기(LLM fallback, 담당자 확인, 조인 검증 등)를 재현 가능하게 겨냥한 입력 명세서를 스크립트로 생성합니다. `data/`는 git 제외 대상이라 실행해서 만들어야 합니다.

```powershell
python scripts\build_llm_trigger_spec.py                    # data\llm_pathway_spec.xlsx
python scripts\build_join_test_spec.py                      # data\join_test_spec.xlsx
python scripts\build_meta_search_cases_spec.py               # data\meta_search_cases_spec.xlsx
python scripts\build_db_validation_period_cases_spec.py      # data\db_validation_period_cases_spec.xlsx
python scripts\build_join_missing_key_spec.py                # data\join_missing_key_spec.xlsx
```

| 파일 | 무엇을 테스트하는가 |
|---|---|
| `llm_pathway_spec.xlsx` | Parsing Agent의 LLM fallback 3종 (헤더 행 판별 / 헤더 필드 매핑 / 행 일부 필드 보완) |
| `join_test_spec.xlsx` | Join Resolution: 단일 테이블(스킵) / 직접 등록된 관계 / 중간 테이블 경유 2-hop |
| `meta_search_cases_spec.xlsx` | Meta Search: 동일 컬럼명이 여러 테이블에 존재(담당자 테이블 선택) / 근접매칭 LLM 판단 / 후보 없음(no_match) |
| `db_validation_period_cases_spec.xlsx` | DB Validation·Classification 태그: not_found(유령 컬럼) / 타입 불일치 HITL / full·confirm·period_mismatch |
| `join_missing_key_spec.xlsx` | Join Resolution: 조인에 필요한 키가 요청 목록에 없을 때의 알림+담당자 확인+자동 추가 |

각 파일은 `scripts\verify_*.py`로 콘솔 입력 없이(자동 승인) 끝까지 돌려 의도한 분기를 타는지 먼저 검증할 수 있고, 실제 담당자 확인 카드를 눈으로 보려면 웹 앱에 업로드하면 됩니다.

## 알려진 제약사항

- `db/`, `data/`의 대용량 파일(duckdb, xlsx)은 `.gitignore`로 커밋에서 제외되어 있습니다. 새로 클론한 환경에서는 위 "DB 초기화" 절차를 다시 밟아야 합니다.
- vss 확장(`INSTALL vss`)은 최초 1회 인터넷 연결이 필요합니다. 사내망에서 `extensions.duckdb.org`가 차단된 경우 오프라인 설치가 필요합니다.
- LangGraph interrupt() 재개 시 노드 함수가 처음부터 재실행되는 특성상, DB 연결/쓰기 로직은 재실행에 안전하도록(idempotent) 방어 코드가 포함되어 있습니다.
- Join Resolution Agent가 추정한 조인키를 승인하면 `table_relationships`에 영구 저장됩니다. 현재 데이터셋은 모든 fact 테이블이 `dim_customer`와 이미 연결돼 있어, "등록된 관계가 전혀 없는" 추정 경로는 실제 명세서로는 재현되지 않습니다(`scripts\verify_join_resolution.py`가 관계 그래프를 강제로 비워 검증).
