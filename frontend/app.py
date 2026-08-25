"""
frontend/app.py

SchemaScout 메인 화면 (구 app.py + 1_업로드.py 통합)
실행: streamlit run frontend/app.py

- 센터 히어로: 아이콘/타이틀 + 명세서 업로드
- 업로드 직후: 전체 행 통합 미리보기 표(호버로 사유 확인) + 파이프라인 시작 버튼
- 왼쪽 사이드바: 페이지 이동(모니터링/결과) + 파이프라인이 시작된 뒤에는
  Agent 5단계별 진행 상황과 각 단계에서 실제 호출된 tool/LLM을 계층으로 표시
  (아이콘은 이모지만 사용 - Streamlit에는 별도 아이콘 폰트가 없어 아이콘 폰트 클래스는 깨짐)
"""

import html

import streamlit as st

from api_client import api_client
from sidebar_progress import render_sidebar_progress

st.set_page_config(page_title="SchemaScout", page_icon="🔍", layout="wide")

render_sidebar_progress()


# ── 메인: 센터 히어로 + 업로드 ────────────────────────────────
st.markdown(
    """
    <style>
    div[data-testid="stFileUploaderDropzone"] {
        border: 1.5px dashed rgba(120,120,120,0.4);
        border-radius: 12px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

_, center_col, _ = st.columns([1, 1.4, 1])
with center_col:
    st.markdown(
        """
        <div style="text-align:center;margin-bottom:20px;">
            <div style="font-size:34px;margin-bottom:6px;">🔍</div>
            <p style="font-size:22px;font-weight:600;margin:0 0 4px;">SchemaScout</p>
            <p style="font-size:13px;color:gray;margin:0;">데이터 명세서 자동 검증 파이프라인</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    uploaded_file = st.file_uploader("명세서 엑셀 파일을 끌어다 놓으세요", type=["xlsx"])

    if uploaded_file is not None:
        size_kb = len(uploaded_file.getvalue()) / 1024
        size_label = f"{size_kb/1024:.1f}MB" if size_kb >= 1024 else f"{size_kb:.0f}KB"
        st.markdown(
            f"""
            <div style="display:flex;align-items:center;justify-content:space-between;
                        background:rgba(120,120,120,0.08);border:1px solid rgba(120,120,120,0.2);
                        border-radius:8px;padding:8px 12px;margin:8px 0;font-size:14px;">
                <span>📄 {html.escape(uploaded_file.name)}</span>
                <span style="color:gray;">{size_label}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

    upload_clicked = st.button(
        "업로드 및 파싱", type="primary", use_container_width=True,
        disabled=uploaded_file is None,
    )

if uploaded_file is not None and upload_clicked:
    with st.spinner("파싱 중..."):
        try:
            result = api_client.upload_spec(uploaded_file.getvalue(), uploaded_file.name)
        except Exception as e:
            st.error(f"업로드 실패: {e}")
        else:
            st.session_state["upload_result"] = result
            st.session_state["upload_trace"] = result.get("trace")
            st.session_state.pop("thread_id", None)  # 새 업로드 시 이전 파이프라인 상태 초기화

upload_result = st.session_state.get("upload_result")

if upload_result:
    st.divider()
    col1, col2, col3 = st.columns(3)
    col1.metric("전체", upload_result["total_count"])
    col2.metric("파싱 성공", upload_result["success_count"])
    col3.metric("확인 필요", upload_result["fail_count"])

    if upload_result.get("header_mapping_status") == "failed":
        st.warning(
            "일부 표준 필드는 헤더 매핑에 실패했습니다 (규칙+LLM+담당자 확인 모두 실패): "
            + ", ".join(upload_result.get("unresolved_header_fields", []))
            + " — 아래 표에서 노란 행으로 표시됩니다. 이 필드는 파이프라인 시작 후 "
            "담당자 확인 화면에서 다시 확인할 수 있습니다."
        )

    if upload_result.get("trace"):
        t = upload_result["trace"]
        st.caption(
            f"Parsing Agent 실행 완료 — 소요시간 {t['agent_duration_sec']}s, "
            f"tool 호출 {len(t['tool_calls'])}건. 상세 내역은 왼쪽 사이드바의 "
            f"**모니터링** 페이지에서 확인하세요."
        )

    st.subheader(f"전체 미리보기 ({upload_result['total_count']}건)")

    columns = ["영문명", "한글명", "항목설명", "type", "시점(기간)"]
    header_html = "".join(f"<th style='text-align:left;padding:6px 8px;'>{c}</th>" for c in columns)

    rows_html = ""
    for row in upload_result["rows"]:
        is_warning = row["status"] == "warning"
        row_bg = "background:rgba(230,170,20,0.12);" if is_warning else ""
        cells = ""
        for c in columns:
            val = row.get(c)
            cell_text = html.escape(str(val)) if val not in (None, "") else "<span style='color:gray;'>—</span>"
            cells += f"<td style='padding:6px 8px;'>{cell_text}</td>"

        if is_warning:
            reason = html.escape(row.get("reason") or "")
            icon = f"<span title='{reason}' style='color:#B7830B;cursor:help;font-weight:600;'>⚠</span>"
        else:
            icon = "<span style='color:#2E7D32;font-weight:600;'>✓</span>"

        rows_html += (
            f"<tr style='{row_bg}border-bottom:1px solid rgba(120,120,120,0.15);'>"
            f"<td style='padding:6px 8px;color:gray;'>{row['row_index'] + 1}</td>"
            f"{cells}"
            f"<td style='padding:6px 8px;text-align:center;'>{icon}</td>"
            f"</tr>"
        )

    table_html = f"""
    <div style="overflow-x:auto;border:1px solid rgba(120,120,120,0.2);border-radius:8px;">
    <table style="width:100%;font-size:13px;border-collapse:collapse;">
        <tr style="background:rgba(120,120,120,0.08);border-bottom:1px solid rgba(120,120,120,0.2);">
            <th style="text-align:left;padding:6px 8px;width:36px;">#</th>
            {header_html}
            <th style="text-align:center;padding:6px 8px;width:48px;">상태</th>
        </tr>
        {rows_html}
    </table>
    </div>
    """
    st.markdown(table_html, unsafe_allow_html=True)
    st.caption("⚠ 표시에 마우스를 올리면 확인이 필요한 사유가 표시됩니다. "
               "(영문명/한글명/항목설명 중 일부만 있는 행은 파이프라인 시작 후 메타DB/LLM이 "
               "나머지를 보완 제안하며, 이때는 정상(✓)으로 표시됩니다.)")

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
                st.session_state.pop("sidebar_events", None)
                st.session_state.pop("sidebar_events_since", None)
                st.session_state.pop("sidebar_events_thread_id", None)
                st.success(f"파이프라인 시작됨: `{start_result['thread_id']}`")
                st.info("왼쪽 사이드바에서 진행 상황을 바로 확인하거나, "
                        "'모니터링' 페이지로 이동해 담당자 확인을 진행하세요.")
                st.rerun()