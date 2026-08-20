"""
frontend/pages/2_모니터링.py

파이프라인 진행 상황(어떤 tool/LLM을 쓰고 있는지) 실시간 확인 + 담당자 확인(HITL) 승인/거절
"""

import time

import streamlit as st

from api_client import api_client

st.set_page_config(page_title="모니터링 - SchemaScout", page_icon="🔍", layout="wide")
st.title("2. 모니터링")

thread_id = st.session_state.get("thread_id")
if not thread_id:
    st.warning("진행 중인 파이프라인이 없습니다. '업로드' 페이지에서 먼저 시작하세요.")
    st.stop()

st.caption(f"thread_id: `{thread_id}`")

if st.session_state.get("events_thread_id") != thread_id:
    st.session_state["events"] = []
    st.session_state["events_since"] = 0
    st.session_state["events_thread_id"] = thread_id

try:
    snapshot = api_client.get_events(thread_id, st.session_state["events_since"])
except Exception as e:
    st.error(f"진행 상황 조회 실패: {e}")
    st.stop()

st.session_state["events"].extend(snapshot["events"])
st.session_state["events_since"] = snapshot["next_since"]
status = snapshot["status"]

status_label = {
    "running": "🔵 진행 중",
    "waiting_human": "🟡 담당자 확인 대기",
    "done": "🟢 완료",
    "error": "🔴 오류",
}.get(status, status)
st.subheader(f"상태: {status_label}")

if status == "error":
    st.error(snapshot["error"])

if status == "waiting_human" and snapshot["confirm_payload"]:
    payload = snapshot["confirm_payload"]
    st.warning("담당자 확인이 필요합니다.")
    with st.container(border=True):
        if payload["type"] == "inferred_confirmation":
            st.markdown(f"**원본 컬럼**: {payload['eng_name']} / {payload['kor_name']}")
            st.markdown(f"**원본 설명**: {payload['description']}")
            st.markdown(f"**LLM 확신도**: {payload['llm_confidence']}")
            st.markdown(f"**LLM 근거**: {payload['llm_evidence']}")
            st.markdown("**후보**")
            st.dataframe(payload["candidates"], use_container_width=True)
            approve_label, reject_label = "✅ 승인", "❌ 거절"
        elif payload["type"] == "type_mismatch_confirmation":
            st.markdown(f"**컬럼**: {payload['column_id']} (테이블: {payload['table']})")
            st.markdown(f"**명세 type**: {payload['spec_type']}")
            st.markdown(f"**실제 type**: {payload['actual_type']}")
            approve_label, reject_label = "✅ 실제 DB 기준으로 갱신", "❌ 명세 기준 유지"
        else:
            st.json(payload)
            approve_label, reject_label = "✅ 승인", "❌ 거절"

        c1, c2 = st.columns(2)
        if c1.button(approve_label, type="primary", use_container_width=True):
            api_client.confirm(thread_id, "approved")
            st.rerun()
        if c2.button(reject_label, use_container_width=True):
            api_client.confirm(thread_id, "rejected")
            st.rerun()

st.divider()
st.subheader("진행 로그")
for ev in reversed(st.session_state["events"]):
    with st.expander(f"[{ev['seq']}] {ev['label']} — {ev['summary']} ({ev['elapsed_sec']}s)"):
        st.markdown(f"- **tool**: {ev['tool']}")
        st.markdown(f"- **model**: {ev['model'] or '해당 없음'}")

if status == "done":
    st.success("파이프라인이 완료되었습니다. 왼쪽 사이드바에서 '결과 및 다운로드' 페이지로 이동하세요.")
elif status in ("running", "waiting_human"):
    auto_refresh = st.checkbox("자동 새로고침 (2초 간격)", value=True)
    if st.button("지금 새로고침"):
        st.rerun()
    if auto_refresh:
        time.sleep(2)
        st.rerun()
