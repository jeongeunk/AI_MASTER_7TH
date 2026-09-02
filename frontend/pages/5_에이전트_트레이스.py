"""
frontend/pages/5_에이전트_트레이스.py

reference/개발가이드_v2.md Week8 Step1 "에이전트 트레이스 시각화 페이지"를 반영한 화면.
LangGraph 각 노드가 make_log()로 남긴 고수준 로그(state["agent_logs"])를
7대 구성요소별 색상·이모지로 구분해 타임라인으로 보여준다.

2_모니터링.py의 tool_span 기반 로그(무슨 함수가 몇 초 걸렸는지, 실행 중인 상세 로그)와는
관점이 다르다 - 이 페이지는 "이 동작이 7대 구성요소 중 무엇에 해당하는가"를 보여주는
요약 뷰라, 시연·발표용으로 더 적합하다. 둘 다 같은 실행(thread_id)의 서로 다른 단면이다.
"""

import json

import streamlit as st

from api_client import api_client
from sidebar_progress import render_sidebar_progress

st.set_page_config(page_title="에이전트 트레이스 - SchemaScout", page_icon="🧭", layout="wide")
render_sidebar_progress()
st.title("5. 에이전트 트레이스")
st.caption("각 Agent가 남긴 고수준 로그를 7대 구성요소(Perception/Reasoning/Memory/Action/"
           "Feedback/Evaluation/Governance/HITL) 관점으로 보여줍니다.")

COMPONENT_COLOR = {
    "Perception": "#1A5276",
    "Reasoning": "#1E8449",
    "Memory": "#6C3483",
    "Action": "#B7950B",
    "Feedback": "#CA6F1E",
    "Evaluation": "#2471A3",
    "Governance": "#922B21",
    "HITL": "#E67E22",  # 인간 개입 구간 강조
}

thread_id = st.session_state.get("thread_id")
if not thread_id:
    st.warning("진행 중인 파이프라인이 없습니다. SchemaScout 메인 화면에서 먼저 시작하세요.")
    st.stop()


@st.fragment(run_every=2)
def _trace_panel(thread_id: str):
    if st.session_state.get("trace_events_thread_id") != thread_id:
        st.session_state["trace_events"] = []
        st.session_state["trace_events_since"] = 0
        st.session_state["trace_events_thread_id"] = thread_id

    try:
        snapshot = api_client.get_events(thread_id, st.session_state["trace_events_since"])
    except Exception as e:
        st.error(f"트레이스 조회 실패: {e}")
        return

    st.session_state["trace_events"].extend(snapshot["events"])
    st.session_state["trace_events_since"] = snapshot["next_since"]
    status = snapshot["status"]

    logs = [log for ev in st.session_state["trace_events"] for log in (ev.get("agent_logs") or [])]

    if status == "waiting_human":
        st.markdown(
            f"<div style='background:{COMPONENT_COLOR['HITL']}22;border:1px solid {COMPONENT_COLOR['HITL']};"
            f"border-radius:8px;padding:10px 14px;margin-bottom:10px;'>"
            f"🙋 <b>인간 검토 대기 중</b> — '모니터링' 페이지에서 확인 후 처리하세요.</div>",
            unsafe_allow_html=True,
        )

    if not logs:
        st.info("아직 기록된 고수준 로그가 없습니다.")
        return

    total_duration = None
    if logs[0].get("timestamp") and logs[-1].get("timestamp"):
        from datetime import datetime
        try:
            start = datetime.fromisoformat(logs[0]["timestamp"])
            end = datetime.fromisoformat(logs[-1]["timestamp"])
            total_duration = round((end - start).total_seconds(), 1)
        except ValueError:
            pass

    c1, c2, c3 = st.columns(3)
    c1.metric("총 단계 수", len(logs))
    c2.metric("실행 시간", f"{total_duration}s" if total_duration is not None else "-")
    hitl_count = sum(1 for log in logs if log["component"] == "HITL")
    c3.metric("HITL 개입 횟수", hitl_count)

    with st.expander("📋 전체 로그 JSON 복사"):
        st.code(json.dumps(logs, ensure_ascii=False, indent=2), language="json")

    st.divider()
    st.markdown("**타임라인** (위 = 첫 단계)")
    for log in logs:
        color = COMPONENT_COLOR.get(log["component"], "#5F5E5A")
        is_hitl = log["component"] == "HITL"
        bg = f"{color}22" if not is_hitl else f"{color}33"
        with st.container(border=True):
            st.markdown(
                f"<div style='display:flex;align-items:center;gap:10px;'>"
                f"<span style='background:{bg};color:{color};border-radius:6px;padding:2px 10px;"
                f"font-weight:600;font-size:13px;white-space:nowrap;'>{log['emoji']} {log['component']}</span>"
                f"<span style='font-size:14px;'>{log['summary']}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
            if log.get("detail"):
                with st.expander("상세", expanded=False):
                    st.write(log["detail"])
                    if log.get("metadata"):
                        st.json(log["metadata"])

    if status == "done":
        st.success("파이프라인이 완료되었습니다.")


_trace_panel(thread_id)
