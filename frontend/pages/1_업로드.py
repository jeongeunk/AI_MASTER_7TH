"""
frontend/pages/1_업로드.py

명세서 엑셀 업로드 → 파싱 미리보기 확인 → 파이프라인 시작
모니터링(Agent 시작/종료 시각·소요시간·tool 호출 내역)은 별도의 '모니터링' 페이지에서 확인한다.
"""

import streamlit as st

from api_client import api_client

st.set_page_config(page_title="업로드 - SchemaScout", page_icon="📤", layout="wide")
st.title("1. 업로드")

uploaded_file = st.file_uploader("명세서 엑셀 파일(.xlsx)을 선택하세요", type=["xlsx"])

if uploaded_file is not None and st.button("업로드 및 파싱"):
    with st.spinner("파싱 중..."):
        try:
            result = api_client.upload_spec(uploaded_file.getvalue(), uploaded_file.name)
        except Exception as e:
            st.error(f"업로드 실패: {e}")
        else:
            st.session_state["upload_result"] = result
            st.session_state["upload_trace"] = result.get("trace")  # 모니터링 페이지에서 표시
            st.session_state.pop("thread_id", None)  # 새 업로드 시 이전 파이프라인 상태 초기화

upload_result = st.session_state.get("upload_result")
if upload_result:
    col1, col2, col3 = st.columns(3)
    col1.metric("전체", upload_result["total_count"])
    col2.metric("파싱 성공", upload_result["success_count"])
    col3.metric("파싱 실패", upload_result["fail_count"])

    if upload_result.get("trace"):
        t = upload_result["trace"]
        st.info(f"Parsing Agent 실행 완료 — 소요시간 {t['agent_duration_sec']}s, "
                f"tool 호출 {len(t['tool_calls'])}건. 상세 내역은 왼쪽 사이드바의 "
                f"**모니터링** 페이지에서 확인하세요.")

    st.subheader("미리보기 (최대 10건)")
    st.dataframe(upload_result["preview"], use_container_width=True)

    if upload_result["failed_rows"]:
        with st.expander(f"파싱 실패 {upload_result['fail_count']}건 사유 보기"):
            st.dataframe(upload_result["failed_rows"], use_container_width=True)

    st.divider()
    if st.button("파이프라인 시작", type="primary"):
        with st.spinner("파이프라인 시작 중..."):
            try:
                start_result = api_client.start_pipeline(upload_result["file_path"])
            except Exception as e:
                st.error(f"파이프라인 시작 실패: {e}")
            else:
                st.session_state["thread_id"] = start_result["thread_id"]
                st.session_state["events"] = []
                st.session_state["events_since"] = 0
                st.session_state["events_thread_id"] = start_result["thread_id"]
                st.success(f"파이프라인 시작됨: `{start_result['thread_id']}`")
                st.info("왼쪽 사이드바에서 '모니터링' 페이지로 이동해 진행 상황을 확인하세요.")
else:
    st.caption("파일을 업로드하고 '업로드 및 파싱' 버튼을 누르세요.")
