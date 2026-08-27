"""
frontend/pages/3_결과_및_다운로드.py

컬럼별 최종 판정 조회 + 최종 명세서 엑셀 다운로드
"""

import streamlit as st

from api_client import api_client
from sidebar_progress import render_sidebar_progress

st.set_page_config(page_title="결과 및 다운로드 - SchemaScout", page_icon="📊", layout="wide")
render_sidebar_progress()
st.title("3. 결과 및 다운로드")

thread_id = st.session_state.get("thread_id")
if not thread_id:
    st.warning("진행 중인 파이프라인이 없습니다. SchemaScout 메인 화면에서 먼저 시작하세요.")
    st.stop()

try:
    result = api_client.get_results(thread_id)
except Exception as e:
    st.error(f"결과 조회 실패: {e}")
    st.stop()

if not result["ready"]:
    st.info("아직 결과가 준비되지 않았습니다. '모니터링' 페이지에서 파이프라인이 완료될 때까지 기다려주세요.")
    st.stop()

stats = result["stats"]
if stats:
    col1, col2 = st.columns(2)
    col1.metric("전체 컬럼 수", stats["total"])
    col2.metric("정상 검증 건수", stats["resolution_path_counts"].get("validated", 0))

    with st.expander("태그별 분포"):
        st.bar_chart(stats["tag_counts"])

st.subheader("컬럼별 최종 판정")
st.dataframe(result["rows"], use_container_width=True)

st.divider()
try:
    content = api_client.download_report(thread_id)
except Exception as e:
    st.info(f"다운로드 파일이 아직 준비되지 않았습니다: {e}")
else:
    st.download_button(
        "📥 최종 명세서 다운로드",
        data=content,
        file_name=f"schemascout_report_{thread_id}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )