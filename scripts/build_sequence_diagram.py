"""
scripts/build_sequence_diagram.py

명세서 업로드 -> 6-Agent LangGraph 파이프라인 실행 -> HITL(interrupt/resume) 왕복 ->
리포트 다운로드까지의 실제 호출 순서를 시퀀스 다이어그램으로 그린다.
근거: backend/api/{specs,pipeline}.py, backend/core/pipeline_runner.py,
      agents/langgraph_pipeline.py (graph_confirm_fn -> interrupt(), Command(resume=...))
"""

import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle, Polygon

plt.rcParams["font.family"] = "Malgun Gothic"
plt.rcParams["axes.unicode_minus"] = False

OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "산출물", "sequence_diagram.png",
)

LIFELINES = [
    "사용자\n(브라우저)",
    "Streamlit UI",
    "FastAPI\nBackend",
    "Pipeline Runner\n(백그라운드 스레드)",
    "DuckDB\n(Meta DB / Data DB)",
    "Azure OpenAI",
]
X = [8, 27, 47, 68, 88, 103]
BORDER = "#2E4B6B"
HEAD_COLOR = "#8FB8DE"
NOTE_COLOR = "#FBF3D5"
NOTE_BORDER = "#C9A227"
FRAG_BORDER = "#7A8FA6"
TEXT_DARK = "#1A2E44"

TOP_Y = 196
BOTTOM_Y = -66  # 이벤트 총 개수가 많아 0 아래까지 내려감 - 실측 후 여유 마진 포함해 설정

fig, ax = plt.subplots(figsize=(19, 40), dpi=165)
ax.set_xlim(0, 111)
ax.set_ylim(BOTTOM_Y, TOP_Y + 6)
ax.axis("off")

ax.text(55.5, TOP_Y + 5, "SchemaScout 파이프라인 — 시퀀스 다이어그램", ha="center", va="top",
        fontsize=18, fontweight="bold", color=TEXT_DARK)
ax.text(55.5, TOP_Y + 2.2,
        "(명세서 업로드 → 6-Agent LangGraph 실행 → HITL interrupt()/Command(resume) 왕복 → 리포트 다운로드)",
        ha="center", va="top", fontsize=9.5, color="#555555", style="italic")

# ── 라이프라인 헤더 + 세로 점선 ─────────────────────────────
for i, label in enumerate(LIFELINES):
    x = X[i]
    head = FancyBboxPatch((x - 8, TOP_Y - 5), 16, 6, boxstyle="round,pad=0.3,rounding_size=1",
                           linewidth=1.5, edgecolor=BORDER, facecolor=HEAD_COLOR, zorder=5)
    ax.add_patch(head)
    ax.text(x, TOP_Y - 2, label, ha="center", va="center", fontsize=8.3, color=TEXT_DARK,
             fontweight="bold", linespacing=1.3, zorder=6)
    ax.plot([x, x], [TOP_Y - 5, BOTTOM_Y], linestyle=(0, (3, 3)), linewidth=1.0, color="#9AA7B5", zorder=1)


def arrow_seq(y, i_from, i_to, label, dashed=False, fontsize=7.2, label_dy=0.9, color=BORDER):
    x1, x2 = X[i_from], X[i_to]
    style = (0, (4, 2)) if dashed else "solid"
    a = FancyArrowPatch((x1, y), (x2, y), arrowstyle="-|>", mutation_scale=11,
                         linewidth=1.3, color=color, linestyle=style, zorder=3,
                         shrinkA=0, shrinkB=0)
    ax.add_patch(a)
    mx = (x1 + x2) / 2
    ax.text(mx, y + label_dy, label, ha="center", va="bottom", fontsize=fontsize, color="#222222",
             linespacing=1.25, zorder=4,
             bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.88))


def self_arrow(y, i, label, fontsize=7.2, w=6, h=2.6):
    x = X[i]
    path_x = [x, x + w, x + w, x]
    path_y = [y, y, y - h, y - h]
    ax.plot(path_x, path_y, color=BORDER, linewidth=1.3, zorder=3)
    a = FancyArrowPatch((x + w, y - h), (x, y - h), arrowstyle="-|>", mutation_scale=11,
                         linewidth=1.3, color=BORDER, zorder=3, shrinkA=0, shrinkB=0)
    ax.add_patch(a)
    ax.text(x + w + 1, y - h / 2, label, ha="left", va="center", fontsize=fontsize, color="#222222",
             linespacing=1.25, zorder=4)


def note(y, i_from, i_to, text, fontsize=7.4, height=4.0):
    x1, x2 = X[i_from], X[i_to]
    if x1 > x2:
        x1, x2 = x2, x1
    pad = 2.5
    box = FancyBboxPatch((x1 - pad, y - height / 2), (x2 - x1) + pad * 2, height,
                          boxstyle="round,pad=0.25,rounding_size=0.6",
                          linewidth=1.2, edgecolor=NOTE_BORDER, facecolor=NOTE_COLOR, zorder=4)
    ax.add_patch(box)
    ax.text((x1 + x2) / 2, y, text, ha="center", va="center", fontsize=fontsize, color=TEXT_DARK,
             linespacing=1.3, zorder=5, style="italic")


def frag_box(y_top, y_bottom, i_left, i_right, tag, title):
    x1, x2 = X[i_left] - 6, X[i_right] + 6
    box = Rectangle((x1, y_bottom), x2 - x1, y_top - y_bottom, linewidth=1.4,
                     edgecolor=FRAG_BORDER, facecolor="none", linestyle=(0, (1, 0)), zorder=0)
    ax.add_patch(box)
    tab_w, tab_h = 12 + len(tag) * 0.9, 3.4
    tab = Polygon([(x1, y_top), (x1 + tab_w, y_top), (x1 + tab_w, y_top - tab_h),
                   (x1 + tab_w - 1.6, y_top - tab_h * 1.55), (x1, y_top - tab_h * 1.55)],
                  closed=True, linewidth=1.2, edgecolor=FRAG_BORDER, facecolor="#EDF1F5", zorder=1)
    ax.add_patch(tab)
    ax.text(x1 + 1, y_top - tab_h / 2 - 0.3, tag, ha="left", va="center", fontsize=8, fontweight="bold",
             color=TEXT_DARK, zorder=2)
    ax.text(x1 + tab_w + 2, y_top - tab_h / 2 - 0.3, title, ha="left", va="center", fontsize=7.6,
             color="#444444", style="italic", zorder=2)


# ── 이벤트 ───────────────────────────────────────────────────
y = TOP_Y - 9

arrow_seq(y, 0, 1, "명세서(.xlsx) 업로드"); y -= 5.6
arrow_seq(y, 1, 2, "POST /api/specs/upload"); y -= 5.6
arrow_seq(y, 2, 1, "file_path 반환", dashed=True); y -= 5.6
arrow_seq(y, 1, 2, "POST /api/pipeline/start\n(file_path)"); y -= 6.4
arrow_seq(y, 2, 3, "백그라운드 스레드 시작\n(graph.stream, checkpointer=SqliteSaver)"); y -= 6.4
arrow_seq(y, 2, 1, "thread_id 반환", dashed=True); y -= 5.2
note(y, 0, 3, "Streamlit UI는 이후 GET /api/pipeline/{thread_id}/events 를 주기적으로 폴링(스트리밍 노드 진행상황 표시)", height=3.6)
y -= 6.5

self_arrow(y, 3, "parsing_node 실행\n(Parsing Agent — 헤더 탐지·행 구조화)"); y -= 6.0
arrow_seq(y, 3, 4, "exact_match_meta_db / retrieve_candidates\n(일부 필드만 있는 행 보완, RAG)"); y -= 5.6
arrow_seq(y, 4, 3, "후보 메타 행 또는 없음", dashed=True); y -= 5.6
arrow_seq(y, 3, 5, "gpt-4.1-mini\n(헤더 매핑·행 보완 판단, Structured Output)"); y -= 5.6
arrow_seq(y, 5, 3, "판단 결과(JSON)", dashed=True); y -= 6.2
note(y, 3, 3, "parsed_rows 완성 (담당자 확인이 필요하면 header_row/header_mapping/row_completion HITL 발생 — 아래와 동일 패턴)", height=3.6)
y -= 7.0

self_arrow(y, 3, "meta_exact_check 루프 시작\n(Meta Search Agent — 컬럼 단위)"); y -= 6.0
arrow_seq(y, 3, 4, "exact_match_meta_db(영문명)"); y -= 5.6
arrow_seq(y, 4, 3, "후보 0 / 1 / 2개 이상", dashed=True); y -= 6.4

frag_top = y
arrow_seq(y, 3, 4, "retrieve_candidates\n(vss 컬럼·용어집 임베딩 + fuzzy 병합)"); y -= 5.8
arrow_seq(y, 4, 3, "후보 목록", dashed=True); y -= 5.6
arrow_seq(y, 3, 5, "gpt-5-mini MatchJudgment\n(Structured Output)"); y -= 5.6
arrow_seq(y, 5, 3, "confidence / evidence / recommend_action", dashed=True); y -= 5.8
frag_box(frag_top + 2.6, y - 1.4, 3, 5, "opt", "정확 매칭 실패 시(search) — RAG 다중소스 검색 + LLM 판단")
y -= 3.0

hitl_top = y
self_arrow(y, 3, "interrupt(payload) 호출\n→ 그래프 실행 일시정지, 상태 체크포인트 저장"); y -= 6.0
arrow_seq(y, 3, 2, "__interrupt__ 청크 감지\nconfirm_payload 저장, run.state = WAITING", dashed=True); y -= 5.8
arrow_seq(y, 2, 1, "폴링 응답에 confirm_payload 포함", dashed=True); y -= 5.6
arrow_seq(y, 1, 0, "확인 카드 표시(승인/거절, 후보 근거)"); y -= 5.6
arrow_seq(y, 0, 1, "담당자 결정 입력"); y -= 5.6
arrow_seq(y, 1, 2, "POST /api/pipeline/{id}/confirm\n(decision)"); y -= 5.8
arrow_seq(y, 2, 3, "Command(resume=decision)\n→ 그래프 재개"); y -= 4.5
frag_box(hitl_top + 2.6, y - 1.0, 0, 3, "opt", "confidence < 0.92 등 담당자 확인 필요 시 — interrupt()/Command(resume) 왕복 (아래도 동일 패턴)")
y -= 3.4

note(y, 3, 3,
     "동일한 HITL 왕복 패턴이 meta_table_disambiguation(동일 컬럼명 중복)·\n"
     "Join Resolution(조인키/누락키 확인)·DB Validation(type 불일치 확인)에서도 반복됨 — 이하 생략 표기",
     height=5.2)
y -= 7.0

self_arrow(y, 3, "join_resolution_node 실행\n(Join Resolution Agent)"); y -= 6.0
arrow_seq(y, 3, 4, "SEMI JOIN 값 포함률 실측\n(check_value_overlap, Data DB)"); y -= 5.6
arrow_seq(y, 4, 3, "containment, relation_type", dashed=True); y -= 6.4

self_arrow(y, 3, "db_validation_node 실행\n(DB Validation Agent)"); y -= 6.0
arrow_seq(y, 3, 4, "ATTACH ... READ_ONLY\n존재 여부·실제 type·보유기간 조회"); y -= 5.6
arrow_seq(y, 4, 3, "검증 결과", dashed=True); y -= 6.4

self_arrow(y, 3, "classification_node 실행\n(존재→type→기간 순 태깅)"); y -= 6.0
self_arrow(y, 3, "report_node 실행\n(엑셀 리포트 생성 — 컬럼별 결과 + 조인 가능성 시트)"); y -= 6.0
arrow_seq(y, 3, 2, "run.state = DONE\nreport_excel_path / report_stats", dashed=True); y -= 6.4

arrow_seq(y, 1, 2, "GET /api/pipeline/{id}/results (폴링)"); y -= 5.6
arrow_seq(y, 2, 1, "report_stats", dashed=True); y -= 5.6
arrow_seq(y, 1, 2, "GET /api/pipeline/{id}/download"); y -= 5.6
arrow_seq(y, 2, 1, "엑셀 파일", dashed=True); y -= 5.6
arrow_seq(y, 1, 0, "다운로드 제공")

plt.tight_layout()
os.makedirs(os.path.dirname(OUT), exist_ok=True)
plt.savefig(OUT, dpi=165, bbox_inches="tight", facecolor="white")
print(f"작성 완료: {OUT}")
