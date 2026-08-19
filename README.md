# SchemaScout (데이터 명세관)

고객이 작성한 데이터 명세서(엑셀)를 실제 DB와 자동으로 대조·검증해, 최종 제공 가능 명세서를 생성하는 5-Agent LangGraph 파이프라인 PoC입니다.

담당자가 수작업으로 하나씩 확인하던 "이 컬럼 실제로 있나? 타입 맞나? 요청한 기간만큼 데이터 있나?"를 자동화하되, **애매한 판단(유사도 매칭, 타입 불일치)은 담당자 확인(Human-in-the-loop)을 거쳐 확정**하는 구조입니다.

## 파이프라인 개요

```
Parsing Agent → Meta Search Agent → DB Validation Agent → Classification Agent → Report Agent
   (명세서            (메타DB 매칭          (실DB 존재/         (최종 태깅)         (엑셀 산출물
    구조화)          + 담당자 확인)        타입/기간 조회         )                  생성)
                                          + 담당자 확인)
```

거절/미매칭된 컬럼은 Meta Search Agent에서 조기 종결되어 DB Validation·Classification을 거치지 않고 바로 Report Agent 집계에 포함됩니다.

## 최종 태그 체계 (6종)

| 태그 | 의미 |
|---|---|
| `unresolved` | 메타 매칭 실패(유사도 미달 또는 담당자 거절) |
| `not_found` | 명세에는 있으나 실 DB에 컬럼 없음 |
| `type_mismatch` | 명세 type과 실제 type 불일치, 담당자가 갱신 거절 |
| `period_mismatch` | 요청 기간과 실제 보유 기간이 전혀 겹치지 않음 |
| `full_period` | 요청 기간 전체 제공 가능 (요청 기간 미기재 시 보유 기간 전체) |
| `confirm_period` | 요청 기간 일부만 제공 가능(자동 조정) |

## 폴더 구조

```
AI_PROJECT/
├── .env                    # 실제 API 키 등 (git 제외)
├── .env.example            # 설정 템플릿
├── .gitignore
├── llm_client.py            # Azure OpenAI 스타일 엔드포인트 클라이언트
├── requirements.txt
├── run_pipeline.py          # LangGraph 파이프라인 실행 드라이버
├── agents/
│   ├── parsing_agent.py
│   ├── meta_search_agent.py
│   ├── db_validation_agent.py
│   ├── classification_agent.py
│   ├── report_agent.py
│   └── langgraph_pipeline.py    # 5개 Agent를 묶는 StateGraph 정의
├── scripts/
│   ├── test_setup.py             # DuckDB + vss 설치/검증
│   ├── init_audit_db.py          # 감사 DB(SQLite) 초기화
│   ├── load_data_db.py           # xlsx → 실데이터 DB 적재
│   ├── fill_meta_db.py           # 메타 DB column_spec 규칙기반 채우기
│   ├── llm_polish_columns.py     # 메타 설명 LLM으로 다듬기
│   ├── generate_embeddings.py    # 컬럼 설명 임베딩 + HNSW 인덱스
│   └── verify_meta_db.py         # 메타 DB 최종 검증
├── data/
│   ├── sample_spec.xlsx               # 테스트용 샘플 명세서
│   ├── sample_spec_period_test.xlsx   # 기간 로직 전체 케이스 테스트용
│   └── (원본 데이터 xlsx 6개: dim_customer, fact_*)
├── db/
│   ├── schemascout_meta.duckdb    # 메타 DB (컬럼 명세 + 유사도 검색)
│   ├── schemascout_data.duckdb    # 실데이터 DB (raw_telecom_* 6개 테이블)
│   ├── schemascout_audit.sqlite   # 감사 DB (확인 이력, 변경 이력)
│   └── langgraph_checkpoints.sqlite  # LangGraph 체크포인터 (interrupt 상태 저장)
└── docs/
    ├── SchemaScout_DB아키텍처_상세설계.md
    ├── SchemaScout_Agent_상세설계_v3.md
    ├── SchemaScout_시나리오_개정판.md
    └── SchemaScout_SC001_시퀀스다이어그램.png / .mmd
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
python scripts\fill_meta_db.py        # column_spec 규칙기반 채우기
python scripts\init_audit_db.py       # 감사 DB 생성
python scripts\llm_polish_columns.py  # 메타 설명 LLM으로 다듬기
python scripts\generate_embeddings.py # 임베딩 생성 + HNSW 인덱스
python scripts\verify_meta_db.py      # 최종 검증
```

## 파이프라인 실행

```powershell
python -u run_pipeline.py data\sample_spec_period_test.xlsx
```

실행 중 `inferred` 매칭이나 `type` 불일치가 발견되면 콘솔에 확인 요청이 뜹니다(y/n). 응답 후 자동으로 이어서 진행되며, 완료되면 입력 파일과 같은 폴더에 `입력파일명(output)_YYYYMMDD_HHMM.xlsx`로 최종 명세서가 생성됩니다.

같은 `thread_id`로 재실행하면 중단된 지점부터 이어서 진행됩니다 (LangGraph 체크포인터 덕분에 프로세스를 껐다 켜도 유지됩니다).

## 개별 Agent 단위 테스트

전체 파이프라인 대신 특정 Agent까지만 실행해서 확인하고 싶을 때:

```powershell
python -u agents\parsing_agent.py data\sample_spec.xlsx
python -u agents\meta_search_agent.py data\sample_spec.xlsx
python -u agents\db_validation_agent.py data\sample_spec.xlsx
python -u agents\classification_agent.py data\sample_spec.xlsx
python -u agents\report_agent.py data\sample_spec.xlsx
```

## 알려진 제약사항

- `db/`, `data/`의 대용량 파일(duckdb, xlsx)은 `.gitignore`로 커밋에서 제외되어 있습니다. 새로 클론한 환경에서는 위 "DB 초기화" 절차를 다시 밟아야 합니다.
- vss 확장(`INSTALL vss`)은 최초 1회 인터넷 연결이 필요합니다. 사내망에서 `extensions.duckdb.org`가 차단된 경우 오프라인 설치가 필요합니다.
- LangGraph interrupt() 재개 시 노드 함수가 처음부터 재실행되는 특성상, DB 연결/쓰기 로직은 재실행에 안전하도록(idempotent) 방어 코드가 포함되어 있습니다.