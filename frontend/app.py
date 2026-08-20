"""
frontend/app.py

실행: streamlit run frontend/app.py
"""

import streamlit as st

st.set_page_config(page_title="SchemaScout", page_icon="📋", layout="wide")

st.title("SchemaScout")
st.caption("데이터 명세서 자동 검증 파이프라인")

st.markdown("""
왼쪽 사이드바에서 순서대로 진행하세요.

1. **업로드** — 명세서 엑셀을 올리고 파이프라인을 시작합니다
2. **모니터링** — 각 Agent가 어떤 tool/LLM으로 무엇을 처리하고 있는지 실시간으로 확인하고,
   담당자 확인이 필요하면 여기서 승인/거절합니다
3. **결과 및 다운로드** — 컬럼별 최종 판정을 조회하고 명세서를 내려받습니다
""")

if "thread_id" in st.session_state:
    st.info(f"진행 중인 파이프라인: `{st.session_state['thread_id']}`")
else:
    st.warning("아직 시작된 파이프라인이 없습니다. '업로드' 페이지에서 시작하세요.")
