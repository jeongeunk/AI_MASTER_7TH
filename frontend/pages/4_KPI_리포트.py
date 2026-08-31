"""
frontend/pages/4_KPI_리포트.py

run_metrics(감사 DB)에 쌓인 웹 업로드 실행 이력을 집계해
KPI1(검증 소요 시간)·KPI2(자동 판별 커버리지)·KPI3(판정 재현성)·
KPI4(컬럼 재등장 시 재검색 감소율)를 보여준다.

evaluation/run_metrics_kpi_report.py의 CLI와 집계 기준이 동일하다
(백엔드 /api/kpi/report가 같은 compute_* 함수를 그대로 호출).

화면을 위아래로 두 구간으로 나눈다 - 집계 단위가 다르기 때문이다.
- KPI1·2: 실행(thread_id) 단위 지표라 컬럼 필터의 영향을 받지 않는다.
- KPI3·4: 컬럼(eng_name) 단위 지표라 아래 필터로 특정 컬럼만 볼 수 있다.
동일 명세서를 웹에서 최소 2회 이상 업로드해야 값이 나온다(1회만 실행하면
재등장 이력이 없어 측정 대상 자체가 0건).
"""

import streamlit as st

from api_client import api_client
from sidebar_progress import render_sidebar_progress

st.set_page_config(page_title="KPI 리포트 - SchemaScout", page_icon="📈", layout="wide")
render_sidebar_progress()
st.title("4. KPI 리포트")
st.caption("웹으로 여러 번 업로드된 실행 이력(run_metrics)을 재실행 없이 그대로 집계한 결과입니다.")

try:
    report = api_client.get_kpi_report()  # KPI1·2는 항상 전체 데이터 기준(필터 없음)
except Exception as e:
    st.error(f"KPI 리포트 조회 실패: {e}")
    st.stop()

if not report["available"]:
    st.info(report["message"])
    st.stop()

# ── 실행(thread_id) 단위 KPI ────────────────────────────────────────────
proc = report["processing_time"]
cov = report["coverage"]

st.subheader("KPI1. 검증 소요 시간")
if proc["run_count"] == 0:
    st.info("측정 대상 없음 - 끝까지 완료된 실행 이력이 아직 없습니다.")
else:
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("실행 건수", proc["run_count"])
    col2.metric("평균 처리시간", f"{proc['avg_seconds']}초")
    col3.metric("최소", f"{proc['min_seconds']}초")
    col4.metric("최대", f"{proc['max_seconds']}초")
    with st.expander("실행별 처리시간"):
        st.dataframe(
            [{"실행(thread_id)": r["thread_id"][:8], "입력파일": r["input_file"], "처리시간(초)": r["seconds"]}
             for r in proc["runs"]],
            use_container_width=True,
        )

st.divider()
st.subheader("KPI2. 자동 판별 커버리지")
if cov["run_count"] == 0:
    st.info("측정 대상 없음 - 실행 이력이 아직 없습니다.")
else:
    col1, col2, col3 = st.columns(3)
    col1.metric("실행 건수", cov["run_count"])
    col2.metric("평균 커버리지", f"{cov['avg_coverage_pct']}%")
    col3.metric("100% 미만 실행", cov["below_100_count"])

    if cov["below_100_count"] > 0:
        st.warning(
            "100% 미만인 실행이 있습니다 — 설계상 이 값은 항상 100%여야 합니다"
            "(판별 안 되는 컬럼도 반드시 최종태그가 채워지도록 만들었으므로). "
            "파이프라인에서 컬럼이 조용히 누락됐을 가능성이 있어 확인이 필요합니다."
        )
    with st.expander("실행별 커버리지"):
        st.dataframe(
            [
                {
                    "실행(thread_id)": r["thread_id"][:8], "입력파일": r["input_file"],
                    "태그 확정": r["numerator"], "전체 컬럼": r["denominator"],
                    "커버리지(%)": r["coverage_pct"],
                }
                for r in cov["runs"]
            ],
            use_container_width=True,
        )

st.divider()

# ── 컬럼(eng_name) 단위 KPI ─────────────────────────────────────────────
eng_name_filter = st.text_input("컬럼(영문명) 필터 - 비워두면 전체 집계 (KPI3·4에만 적용)", value="")

if eng_name_filter:
    try:
        report = api_client.get_kpi_report(eng_name_filter)
    except Exception as e:
        st.error(f"KPI 리포트 조회 실패: {e}")
        st.stop()

repro = report["reproducibility"]
retrieval = report["retrieval_attempts"]

st.subheader("KPI3. 판정 재현성")
if repro["total"] == 0:
    st.info("측정 대상 없음 - 동일 컬럼이 서로 다른 실행(thread_id)으로 2회 이상 재등장한 이력이 아직 없습니다.")
else:
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("재현율", f"{repro['rate']}%")
    col2.metric("측정 대상(추정 매칭)", repro["total"])
    col3.metric("매번 동일", repro["stable"])
    col4.metric("불일치", repro["unstable"])
    st.caption(f"정확 매칭 {repro['skipped']}건은 구조상 항상 100% 재현되어 집계에서 제외했습니다.")

    if repro["mismatches"]:
        st.warning("불일치 발견")
        for m in repro["mismatches"]:
            st.write(f"**{m['eng_name']}**")
            st.dataframe(m["occurrences"], use_container_width=True)

st.divider()
st.subheader("KPI4. 컬럼 재등장 시 재검색 감소율")
if retrieval["measured"] == 0:
    st.info("측정 대상 없음 - 재검색이 1회 이상 발생한 컬럼이 서로 다른 실행(thread_id)으로 재등장한 이력이 아직 없습니다.")
else:
    status_label = {
        "reduced": "감소",
        "unchanged": "동일(감소 없음)",
        "unchanged_zero": "관찰 불가(1회차부터 0회)",
        "increased": "증가(원인 확인 필요)",
    }
    rows = [
        {
            "영문명": col["eng_name"],
            "재검색 횟수 이력": col["attempts"],
            "1회차": col["first"],
            "재등장 시 평균": col["avg_rest"],
            "감소율(%)": col["reduction_pct"],
            "상태": status_label.get(col["status"], col["status"]),
        }
        for col in retrieval["columns"]
    ]
    st.dataframe(rows, use_container_width=True)
