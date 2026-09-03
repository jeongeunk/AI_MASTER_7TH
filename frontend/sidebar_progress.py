"""
frontend/sidebar_progress.py

파이프라인 진행 상황을 왼쪽 사이드바에 표시하는 공용 컴포넌트.
app.py / 2_모니터링.py / 3_결과_및_다운로드.py 3곳에서 동일하게 import해서 쓴다
(페이지를 이동해도 사이드바 내용이 끊기지 않고 그대로 유지되도록).

아이콘은 이모지만 사용한다 - Streamlit에는 별도 아이콘 폰트가 실려있지 않아
Tabler 같은 아이콘 폰트 클래스를 쓰면 실제 화면에서 네모(□)로 깨진다.
"""

import html
from datetime import datetime

import streamlit as st

from api_client import api_client

STAGE_ORDER = [
    (1, "Parsing"),
    (2, "Meta Search"),
    (3, "DB Validation"),
    (4, "Classification"),
    (5, "Report"),
]


def render_sidebar_progress():
    """현재 페이지가 무엇이든 상관없이, 진행 중인 파이프라인(thread_id)이 있으면
    사이드바에 5-Agent 단계별 진행 상황 + 각 단계에서 실제 호출된 tool/LLM을 표시한다."""
    thread_id = st.session_state.get("thread_id")
    if not thread_id:
        st.sidebar.caption("파이프라인을 시작하면 여기에 진행 상황이 표시됩니다.")
        return

    @st.fragment(run_every=2)
    def _panel():
        if st.session_state.get("sidebar_events_thread_id") != thread_id:
            st.session_state["sidebar_events"] = []
            st.session_state["sidebar_events_since"] = 0
            st.session_state["sidebar_events_thread_id"] = thread_id

        try:
            snapshot = api_client.get_events(thread_id, st.session_state["sidebar_events_since"])
        except Exception:
            # 폴링(2초 간격) 자체가 실패했다는 뜻 - "마지막 확인 시각"이 여기서 멈춘 채로
            # 안 늘어나는 것 자체가 "연결이 끊겼다"는 신호가 된다(화면이 죽었는지 폴링이
            # 죽었는지 구분이 안 되던 문제의 직접적인 해결책).
            st.caption(f"⚠️ 진행 상황을 불러오지 못했습니다 (마지막 성공: "
                       f"{st.session_state.get('sidebar_last_ok', '없음')})")
            return

        st.session_state["sidebar_events"].extend(snapshot["events"])
        st.session_state["sidebar_events_since"] = snapshot["next_since"]
        status = snapshot["status"]
        events = st.session_state["sidebar_events"]

        # 폴링이 방금 성공적으로 한 번 돌았다는 증거 - 새 이벤트가 없어도 이 시각 자체는
        # run_every=2에 맞춰 2초마다 계속 갱신된다("화면이 안 바뀌어서 죽은 줄 알았다" 문제의
        # 핵심 해결책: 이 시각이 계속 올라가면 폴링은 살아있는 것, 멈추면 진짜 끊긴 것).
        now_str = datetime.now().strftime("%H:%M:%S")
        st.session_state["sidebar_last_ok"] = now_str

        seen_steps = [int(ev["plan_step"].split("/")[0]) for ev in events if ev.get("plan_step")]
        max_step = max(seen_steps) if seen_steps else 0

        status_label = {"running": "진행 중", "waiting_human": "담당자 확인 대기",
                         "done": "완료", "error": "오류"}.get(status, status)
        st.markdown(f"**파이프라인 진행** · {status_label}  \n"
                    f"<span style='font-size:12px;color:gray;'>🔄 마지막 확인: {now_str}</span>",
                    unsafe_allow_html=True)

        for i, label in STAGE_ORDER:
            if status == "done" or i < max_step:
                st.markdown(f"<span style='color:#2E7D32;font-weight:600;font-size:13px;'>✓ {i}. {label}</span>",
                            unsafe_allow_html=True)
            elif i == max_step and status in ("running", "waiting_human"):
                icon = "🟡" if status == "waiting_human" else "🔄"
                st.markdown(f"<span style='color:#0C447C;font-weight:600;font-size:13px;'>{icon} {i}. {label}</span>",
                            unsafe_allow_html=True)
            else:
                st.markdown(f"<span style='color:gray;font-size:13px;'>{i}. {label}</span>",
                            unsafe_allow_html=True)
                continue  # 아직 시작 안 한 단계는 하위 tool/llm 목록 없이 넘어감

            stage_events = [ev for ev in events if str(ev.get("plan_step", "")).startswith(f"{i}/")]
            seen_tools, seen_models = [], []
            for ev in stage_events:
                for tc in (ev.get("tool_calls") or []):
                    tool_name = tc.get("tool")
                    model_name = tc.get("model")
                    if tool_name and tool_name not in seen_tools:
                        seen_tools.append(tool_name)
                    if model_name and model_name not in seen_models:
                        seen_models.append(model_name)

            for t in seen_tools:
                st.markdown(f"<div style='padding-left:14px;font-size:12px;color:#666;'>🔧 {html.escape(t)}</div>",
                            unsafe_allow_html=True)
            for m in seen_models:
                st.markdown(f"<div style='padding-left:14px;font-size:12px;color:#0C447C;'>✨ {html.escape(m)}</div>",
                            unsafe_allow_html=True)

        if status == "waiting_human":
            st.caption("담당자 확인이 필요합니다 → **모니터링** 페이지에서 처리하세요.")

    with st.sidebar:
        st.divider()
        _panel()