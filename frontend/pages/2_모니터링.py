"""
frontend/pages/2_모니터링.py

Agent 실행 모니터링: 각 Agent(노드)의 시작/종료 시각·소요시간, tool 호출 내역
(어떤 tool이 불렸는지·소요시간·LLM이면 어떤 모델인지)을 전부 확인할 수 있게 구성.
- '업로드' 페이지의 '업로드 및 파싱' 단계(Parsing Agent, 동기 호출)와
  파이프라인 시작 이후의 5-Agent 그래프 실행을 모두 여기서 확인한다.
- 담당자 확인(HITL): inferred_confirmation, type_mismatch_confirmation,
  header_mapping_confirmation, row_completion_confirmation 승인/거절
"""

import time
from datetime import datetime

import pandas as pd
import streamlit as st

from api_client import api_client

st.set_page_config(page_title="모니터링 - SchemaScout", page_icon="🔍", layout="wide")
st.title("2. 모니터링")

LEVEL_BADGE = {
    "plan": "📋 PLAN",
    "decision": "🧠 DECISION",
    "self_correction": "🔁 SELF-CORRECTION",
    "human": "🙋 HUMAN-IN-THE-LOOP",
    "step": "⚙️ STEP",
}


def _fmt_time(ts) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


def _render_tool_calls(tool_calls: list):
    if not tool_calls:
        st.caption("이 구간에서 호출된 tool이 없습니다.")
        return
    rows = [
        {
            "tool": t["tool"],
            "model": t.get("model") or "-",
            "시작": _fmt_time(t["start"]),
            "종료": _fmt_time(t["end"]),
            "소요시간(s)": t["duration_sec"],
        }
        for t in tool_calls
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_agent_trace_card(trace: dict):
    """Agent 시작/종료/소요시간 + 그 안에서 호출된 tool 목록(모델 포함)을 카드 형태로 표시."""
    if not trace:
        st.caption("이 Agent에 대한 트레이스 정보가 없습니다.")
        return
    c1, c2, c3 = st.columns(3)
    c1.metric("시작", _fmt_time(trace.get("agent_start")))
    c2.metric("종료", _fmt_time(trace.get("agent_end")))
    c3.metric("소요시간", f"{trace.get('agent_duration_sec', 0)}s")
    st.markdown(f"**tool 호출 내역** ({len(trace.get('tool_calls', []))}건)")
    _render_tool_calls(trace.get("tool_calls", []))


# ── 업로드/파싱 단계(동기 호출) 트레이스 ────────────────────────
upload_trace = st.session_state.get("upload_trace")
if upload_trace:
    st.subheader("📤 업로드 · 파싱 단계 — Parsing Agent")
    with st.container(border=True):
        _render_agent_trace_card(upload_trace)
    st.divider()

thread_id = st.session_state.get("thread_id")
if not thread_id:
    if not upload_trace:
        st.warning("진행 중인 파이프라인이 없습니다. '업로드' 페이지에서 먼저 시작하세요.")
    st.stop()

st.subheader("🔄 파이프라인 실행 — 5-Agent 그래프")
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

    # 실패 유형 배지 (헤더 매핑 실패는 다른 색으로 구분)
    if payload["type"] == "header_mapping_confirmation":
        st.markdown(
            "<span style='font-size:13px;padding:3px 10px;border-radius:6px;"
            "background:#FAEEDA;color:#854F0B;font-weight:600;'>헤더 매핑 실패</span>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<span style='font-size:13px;padding:3px 10px;border-radius:6px;"
            "background:#E6F1FB;color:#0C447C;font-weight:600;'>행/컬럼 단위 확인</span>",
            unsafe_allow_html=True,
        )

    with st.container(border=True):
        selected_header = None  # header_mapping_confirmation에서만 사용
        custom_ui_handled = False  # row_completion_confirmation처럼 자체 버튼을 그리는 경우 True

        if payload["type"] == "inferred_confirmation":
            st.markdown(f"**원본 컬럼**: {payload['eng_name']} / {payload['kor_name']}")
            st.markdown(f"**원본 설명**: {payload['description']}")
            st.markdown(f"**LLM 확신도**: {payload.get('llm_confidence', payload.get('similarity_score'))}")
            st.markdown(f"**LLM 근거**: {payload.get('llm_evidence', payload.get('match_evidence', ''))}")
            if "candidates" in payload:
                st.markdown("**후보**")
                st.dataframe(payload["candidates"], use_container_width=True)
            elif "candidate_column" in payload:
                st.markdown(f"**매칭 후보**: {payload['candidate_column']} ({payload['candidate_table']})")
                st.markdown(f"**후보 설명**: {payload['candidate_description']}")
            approve_label, reject_label = "✅ 승인", "❌ 거절"

        elif payload["type"] == "type_mismatch_confirmation":
            st.markdown(f"**컬럼**: {payload['column_id']} (테이블: {payload['table']})")
            st.markdown(f"**명세 type**: {payload['spec_type']}")
            st.markdown(f"**실제 type**: {payload['actual_type']}")
            approve_label, reject_label = "✅ 실제 DB 기준으로 갱신", "❌ 명세 기준 유지"

        elif payload["type"] == "header_mapping_confirmation":
            st.markdown(f"**표준 필드 \"{payload['missing_std_field']}\"에 해당하는 컬럼을 찾지 못했습니다**")
            st.caption("규칙 매칭과 LLM 판단을 모두 시도했지만 확신도가 기준 미달이었습니다. "
                        "업로드된 원본 헤더에서 직접 골라주세요.")

            st.markdown("**시도 내역**")
            for a in payload["attempts"]:
                st.markdown(f"- **{a['method']}**: {a['detail']}")

            st.markdown("**원본 헤더**")
            headers = payload["all_headers"]
            cols = st.columns(len(headers))
            for col, h in zip(cols, headers):
                with col:
                    st.markdown(f"**{h['name']}**")
                    st.caption(h["sample"] or "-")
                    if h["mapped_to"]:
                        st.caption(f"✅ {h['mapped_to']}")

            unmapped_names = [h["name"] for h in headers if not h["mapped_to"]]
            suggested = payload.get("suggested_column")
            default_idx = unmapped_names.index(suggested) if suggested in unmapped_names else 0

            selected_header = st.radio(
                "매핑할 헤더 선택",
                unmapped_names,
                index=default_idx,
                horizontal=True,
                key=f"header_select_{thread_id}_{payload['missing_std_field']}",
            )
            approve_label, reject_label = "이 매핑으로 확정", "해당 필드 없음"

        elif payload["type"] == "row_completion_confirmation":
            candidates = payload["candidates"]
            st.markdown(f"**부분 정보만 있던 {len(candidates)}건에 대해 메타DB/LLM이 나머지 필드를 추정했습니다**")
            st.caption("승인한 행만 추정값이 반영되고, 거절/미승인 행은 원래대로 파싱 실패 처리됩니다. "
                       "(이미 값이 있던 필드는 건드리지 않습니다)")

            source_label = {"meta_db": "메타DB", "llm_inference": "LLM 추론"}
            table_df = pd.DataFrame([
                {
                    "승인": c["confidence"] >= 0.7,
                    "row_index": c["row_index"],
                    "누락 필드": ", ".join(c["missing_fields"]),
                    "제안값": ", ".join(f"{k}={v}" for k, v in c["proposed_fill"].items()),
                    "출처": source_label.get(c["source"], c["source"]),
                    "확신도": c["confidence"],
                    "근거": c["evidence"],
                }
                for c in candidates
            ])
            edited = st.data_editor(
                table_df,
                column_config={"승인": st.column_config.CheckboxColumn(help="체크한 행만 제안값을 반영합니다")},
                disabled=["row_index", "누락 필드", "제안값", "출처", "확신도", "근거"],
                hide_index=True,
                use_container_width=True,
                key=f"row_completion_{thread_id}",
            )

            rc1, rc2 = st.columns(2)
            if rc1.button("선택 항목 반영하고 계속", type="primary", use_container_width=True):
                approved = edited.loc[edited["승인"], "row_index"].tolist()
                api_client.confirm(thread_id, {"decision": "approved", "approved_row_indices": approved})
                st.rerun()
            if rc2.button("전체 거절 (모두 파싱 실패 처리)", use_container_width=True):
                api_client.confirm(thread_id, {"decision": "rejected", "approved_row_indices": []})
                st.rerun()
            custom_ui_handled = True

        else:
            st.json(payload)
            approve_label, reject_label = "✅ 승인", "❌ 거절"

        if not custom_ui_handled:
            c1, c2 = st.columns(2)
            if c1.button(approve_label, type="primary", use_container_width=True):
                if payload["type"] == "header_mapping_confirmation":
                    api_client.confirm(thread_id, {"decision": "approved", "selected_column": selected_header})
                else:
                    api_client.confirm(thread_id, "approved")
                st.rerun()
            if c2.button(reject_label, use_container_width=True):
                if payload["type"] == "header_mapping_confirmation":
                    api_client.confirm(thread_id, {"decision": "rejected"})
                else:
                    api_client.confirm(thread_id, "rejected")
                st.rerun()

st.divider()
st.subheader("진행 로그 — Agent별 시작/종료/소요시간 + tool 호출 내역")
for ev in reversed(st.session_state["events"]):
    badge = LEVEL_BADGE.get(ev.get("level", "step"), "⚙️ STEP")
    n_tools = len(ev.get("tool_calls") or [])
    header = (
        f"{badge} · [{ev['seq']}] {ev['label']} — {ev['summary']} "
        f"({ev.get('agent_duration_sec', ev['elapsed_sec'])}s, tool {n_tools}건)"
    )
    with st.expander(header):
        if ev.get("agent_start") is not None:
            _render_agent_trace_card({
                "agent_start": ev.get("agent_start"),
                "agent_end": ev.get("agent_end"),
                "agent_duration_sec": ev.get("agent_duration_sec"),
                "tool_calls": ev.get("tool_calls") or [],
            })
        else:
            # 트레이스 정보가 없는(구버전) 이벤트에 대한 폴백 표시
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