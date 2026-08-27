"""
scripts/build_flowchart_diagram.py

agents/langgraph_pipeline.py의 build_graph() 노드/엣지 정의를 그대로 옮긴 플로우차트를
PNG로 생성한다. 조건부 분기(add_conditional_edges)의 실제 분기 조건 문자열을 화살표
라벨로 그대로 사용해, 코드와 다이어그램이 어긋나지 않게 한다.
"""

import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Polygon
from matplotlib.path import Path as MPath

plt.rcParams["font.family"] = "Malgun Gothic"
plt.rcParams["axes.unicode_minus"] = False

OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "산출물", "flowchart_diagram.png",
)

COLORS = {
    "terminal": "#4C7BAE",
    "agent": "#8FB8DE",
    "sub": "#DCEAF7",
    "hitl": "#F4E3C1",
    "router": "#E8D5D0",
}
BORDER = "#2E4B6B"
HITL_BORDER = "#B5651D"
TEXT_DARK = "#1A2E44"
TEXT_LIGHT = "#FFFFFF"

fig, ax = plt.subplots(figsize=(15, 22), dpi=170)
ax.set_xlim(0, 100)
ax.set_ylim(0, 152)
ax.axis("off")

ax.text(50, 150, "SchemaScout LangGraph 파이프라인 — 플로우차트", ha="center", va="top",
        fontsize=17, fontweight="bold", color=TEXT_DARK)
ax.text(50, 147, "(agents/langgraph_pipeline.py build_graph() 기준)", ha="center", va="top",
        fontsize=9.5, color="#555555", style="italic")

NODES = {}


def box(node_id, cx, cy, w, h, label, kind, fontsize=8.5, border_color=None):
    color = COLORS[kind]
    text_color = TEXT_LIGHT if kind in ("terminal",) else TEXT_DARK
    edge_color = border_color or (HITL_BORDER if kind == "hitl" else BORDER)
    lw = 2.2 if kind == "hitl" else 1.4
    b = FancyBboxPatch((cx - w / 2, cy - h / 2), w, h,
                        boxstyle="round,pad=0.3,rounding_size=1.2",
                        linewidth=lw, edgecolor=edge_color, facecolor=color, zorder=3)
    ax.add_patch(b)
    ax.text(cx, cy, label, ha="center", va="center", fontsize=fontsize,
             color=text_color, linespacing=1.35, zorder=4, fontweight=("bold" if kind == "terminal" else "normal"))
    NODES[node_id] = (cx, cy, w, h)


def diamond(node_id, cx, cy, w, h, label, fontsize=8):
    pts = [(cx, cy + h / 2), (cx + w / 2, cy), (cx, cy - h / 2), (cx - w / 2, cy)]
    poly = Polygon(pts, closed=True, linewidth=1.6, edgecolor=BORDER, facecolor=COLORS["router"], zorder=3)
    ax.add_patch(poly)
    ax.text(cx, cy, label, ha="center", va="center", fontsize=fontsize, color=TEXT_DARK,
             linespacing=1.3, zorder=4)
    NODES[node_id] = (cx, cy, w, h)


def arrow(id_from, id_to, label=None, label_pos=0.5, rad=0.0, style="-|>",
          color=BORDER, lw=1.4, ls="solid", from_side=None, to_side=None, label_offset=(0, 0), fontsize=6.8):
    x1, y1, w1, h1 = NODES[id_from]
    x2, y2, w2, h2 = NODES[id_to]

    def anchor(x, y, w, h, side, other_x, other_y):
        if side == "top":
            return x, y + h / 2
        if side == "bottom":
            return x, y - h / 2
        if side == "left":
            return x - w / 2, y
        if side == "right":
            return x + w / 2, y
        # 자동: 상대 위치로 결정
        if abs(other_y - y) >= abs(other_x - x):
            return (x, y - h / 2) if other_y < y else (x, y + h / 2)
        return (x + w / 2, y) if other_x > x else (x - w / 2, y)

    sx, sy = anchor(x1, y1, w1, h1, from_side, x2, y2)
    ex, ey = anchor(x2, y2, w2, h2, to_side, x1, y1)

    a = FancyArrowPatch((sx, sy), (ex, ey), arrowstyle=style, mutation_scale=13,
                         linewidth=lw, color=color, linestyle=ls,
                         connectionstyle=f"arc3,rad={rad}", zorder=2)
    ax.add_patch(a)
    if label:
        mx = sx + (ex - sx) * label_pos + label_offset[0]
        my = sy + (ey - sy) * label_pos + label_offset[1]
        if rad != 0:
            my += rad * 8
        ax.text(mx, my, label, ha="center", va="center", fontsize=fontsize, color="#333333",
                 style="italic", zorder=5,
                 bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.85))


def self_loop(node_id, label, side="right", size=6):
    x, y, w, h = NODES[node_id]
    if side == "right":
        p1 = (x + w / 2, y + h * 0.18)
        p2 = (x + w / 2, y - h * 0.18)
        rad = -1.4
        lx, ly = x + w / 2 + size + 2, y
    else:
        p1 = (x - w / 2, y + h * 0.18)
        p2 = (x - w / 2, y - h * 0.18)
        rad = 1.4
        lx, ly = x - w / 2 - size - 2, y
    a = FancyArrowPatch(p1, p2, arrowstyle="-|>", mutation_scale=12, linewidth=1.3,
                         color=BORDER, connectionstyle=f"arc3,rad={rad}", zorder=2)
    ax.add_patch(a)
    ax.text(lx, ly, label, ha="center", va="center", fontsize=6.6, color="#333333", style="italic",
             rotation=90 if side == "right" else -90,
             bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.85))


# ── 노드 배치 ────────────────────────────────────────────────
box("start", 50, 144, 20, 5, "START", "terminal", fontsize=10)
box("parsing", 50, 135, 30, 7, "parsing_node\n(Parsing Agent — 엑셀 구조화)", "agent")
box("meta_init", 50, 125, 34, 6, "meta_init_node\n(Meta Search 서브플랜 초기화)", "sub")

# Meta Search 서브플로우 경계 박스
sub_box = FancyBboxPatch((6, 58), 88, 63, boxstyle="round,pad=0.5,rounding_size=1.5",
                          linewidth=1.3, edgecolor="#7A8FA6", facecolor="none",
                          linestyle=(0, (5, 3)), zorder=1)
ax.add_patch(sub_box)
ax.text(9, 119, "Meta Search Agent 서브플로우 (컬럼 단위 루프, meta_index)",
         ha="left", va="top", fontsize=8.5, color="#555577", style="italic", fontweight="bold")

box("meta_exact_check", 50, 114, 30, 7, "meta_exact_check\n(정확 매칭 조회)", "sub")
box("meta_table_disambiguation", 84, 105, 26, 9,
    "meta_table_\ndisambiguation\n[HITL: 소속 테이블 선택]", "hitl", fontsize=7.8)
box("meta_retrieve", 50, 100, 28, 7, "meta_retrieve\n(RAG 다중소스 후보 검색)", "sub")
box("meta_no_match", 18, 87, 24, 7, "meta_no_match", "sub")
box("meta_judge", 50, 87, 24, 7, "meta_judge\n(LLM 판단, gpt-5-mini)", "sub")
box("meta_retry", 50, 74, 24, 7, "meta_retry\n(검색 파라미터 확장)", "sub")
box("meta_human_confirm", 84, 74, 26, 9, "meta_human_\nconfirm\n[HITL: 담당자 확인]", "hitl", fontsize=7.8)

diamond("router", 50, 62, 34, 9, "route_after_resolution\n(next_column vs done)")

box("join_resolution", 50, 49, 34, 8, "Join Resolution Agent\n[내부 HITL: 조인키·누락키 확인]", "agent", fontsize=8)
box("db_validation", 50, 38, 34, 8, "DB Validation Agent\n[내부 HITL: type 불일치 확인]", "agent", fontsize=8)
box("classification", 50, 27, 30, 7, "Classification Agent", "agent")
box("report", 50, 17, 30, 7, "Report Agent\n(엑셀 리포트 생성)", "agent")
box("end", 50, 8, 20, 5, "END", "terminal", fontsize=10)

# ── 엣지 ────────────────────────────────────────────────────
arrow("start", "parsing")
arrow("parsing", "meta_init")
arrow("meta_init", "meta_exact_check")

arrow("meta_exact_check", "meta_retrieve", label="search\n(정확 매칭 실패)")
arrow("meta_exact_check", "meta_table_disambiguation", label="ambiguous\n(동일 컬럼명 2개+ 테이블)",
      from_side="right", to_side="left")
self_loop("meta_exact_check", "next_column\n(단일 매칭, 다음 컬럼)", side="left")

arrow("meta_retrieve", "meta_judge", label="judge\n(후보 있음)")
arrow("meta_retrieve", "meta_no_match", label="no_candidates", from_side="left", to_side="top", rad=0.15)

arrow("meta_judge", "meta_retry", label="retry\n(0.70~0.92, 시도<2)")
arrow("meta_judge", "meta_human_confirm", label="human_confirm\n(그 외 — 추정 매칭은 confidence 무관\n항상 담당자 확인, 자동 확정 없음)",
      from_side="right", to_side="top", rad=-0.1, fontsize=6.2)

arrow("meta_retry", "meta_retrieve", label="파라미터 확장\n재검색", from_side="left", to_side="left", rad=-0.35, lw=1.2)

arrow("meta_no_match", "router", from_side="bottom", to_side="left", rad=-0.15)
arrow("meta_human_confirm", "router", from_side="bottom", to_side="right", rad=0.15)
arrow("meta_table_disambiguation", "router", from_side="bottom", to_side="right", rad=0.3)

arrow("router", "meta_exact_check", label="next_column", from_side="left", to_side="right", rad=0.6, ls=(0, (4, 2)),
      label_pos=0.18, label_offset=(-6, -2))
arrow("router", "join_resolution", label="done\n(모든 컬럼 처리 완료)")

arrow("join_resolution", "db_validation")
arrow("db_validation", "classification")
arrow("classification", "report")
arrow("report", "end")

# ── 캡션 (범례) ───────────────────────────────────────────────
caption = "실선 화살표: 확정 전이 | 점선 화살표: 컬럼 루프 반복(next_column) | 굵은 주황 테두리: interrupt() 기반 담당자 확인 지점"
ax.text(50, 3, caption, ha="center", va="top", fontsize=8.5, color="#444444", style="italic")

plt.tight_layout()
os.makedirs(os.path.dirname(OUT), exist_ok=True)
plt.savefig(OUT, dpi=170, bbox_inches="tight", facecolor="white")
print(f"작성 완료: {OUT}")
