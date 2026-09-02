"""
scripts/build_parsing_sequence_diagram.py

Parsing Agent의 Tool 호출 순서를 시퀀스 다이어그램으로 그린다.
실제 실행(6행 샘플 명세서, mobile_number/total_recharge_amt 포함)에서 캡처한
25회 호출 트레이스를 기반으로 하되, 행2~5의 반복 구간은 UML loop 프래그먼트로
묶어 가독성을 확보했다.
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
    "산출물", "parsing_agent_sequence_diagram.png",
)

LIFELINES = ["담당자\n(confirm_fn)", "Parsing Agent\n(run_parsing)", "gpt-4.1-mini", "Meta DB\n(DuckDB)", "Data DB\n(DuckDB)"]
X = [10, 34, 60, 82, 102]
BORDER = "#2E4B6B"
HEAD_COLOR = "#8FB8DE"
NOTE_COLOR = "#FBF3D5"
NOTE_BORDER = "#C9A227"
FRAG_BORDER = "#7A8FA6"
TEXT_DARK = "#1A2E44"

TOP_Y = 150
BOTTOM_Y = -18

fig, ax = plt.subplots(figsize=(16, 24), dpi=165)
ax.set_xlim(0, 112)
ax.set_ylim(BOTTOM_Y, TOP_Y + 6)
ax.axis("off")

ax.text(56, TOP_Y + 5, "Parsing Agent — Tool 호출 시퀀스 다이어그램", ha="center", va="top",
        fontsize=17, fontweight="bold", color=TEXT_DARK)
ax.text(56, TOP_Y + 2.2,
        "(실제 실행 트레이스 기반: 6행 샘플 명세서 — av_rech_amt_data / avg_data_amt / mobile_number / total_recharge_amt / 위성설명 / 공란행)",
        ha="center", va="top", fontsize=8.8, color="#555555", style="italic")

for i, label in enumerate(LIFELINES):
    x = X[i]
    head = FancyBboxPatch((x - 9, TOP_Y - 5), 18, 6, boxstyle="round,pad=0.3,rounding_size=1",
                           linewidth=1.5, edgecolor=BORDER, facecolor=HEAD_COLOR, zorder=5)
    ax.add_patch(head)
    ax.text(x, TOP_Y - 2, label, ha="center", va="center", fontsize=8.3, color=TEXT_DARK,
             fontweight="bold", linespacing=1.3, zorder=6)
    ax.plot([x, x], [TOP_Y - 5, BOTTOM_Y], linestyle=(0, (3, 3)), linewidth=1.0, color="#9AA7B5", zorder=1)


def arrow_seq(y, i_from, i_to, label, dashed=False, fontsize=7.0, label_dy=0.9, color=BORDER):
    x1, x2 = X[i_from], X[i_to]
    style = (0, (4, 2)) if dashed else "solid"
    a = FancyArrowPatch((x1, y), (x2, y), arrowstyle="-|>", mutation_scale=11,
                         linewidth=1.3, color=color, linestyle=style, zorder=3, shrinkA=0, shrinkB=0)
    ax.add_patch(a)
    mx = (x1 + x2) / 2
    ax.text(mx, y + label_dy, label, ha="center", va="bottom", fontsize=fontsize, color="#222222",
             linespacing=1.25, zorder=4,
             bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.9))


def self_arrow(y, i, label, fontsize=7.0, w=7, h=2.6):
    x = X[i]
    ax.plot([x, x + w, x + w, x], [y, y, y - h, y - h], color=BORDER, linewidth=1.3, zorder=3)
    a = FancyArrowPatch((x + w, y - h), (x, y - h), arrowstyle="-|>", mutation_scale=11,
                         linewidth=1.3, color=BORDER, zorder=3, shrinkA=0, shrinkB=0)
    ax.add_patch(a)
    ax.text(x + w + 1, y - h / 2, label, ha="left", va="center", fontsize=fontsize, color="#222222",
             linespacing=1.25, zorder=4)


def note(y, i_from, i_to, text, fontsize=7.2, height=3.6):
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
    tab_w, tab_h = 12 + len(tag) * 0.9, 3.2
    tab = Polygon([(x1, y_top), (x1 + tab_w, y_top), (x1 + tab_w, y_top - tab_h),
                   (x1 + tab_w - 1.6, y_top - tab_h * 1.5), (x1, y_top - tab_h * 1.5)],
                  closed=True, linewidth=1.2, edgecolor=FRAG_BORDER, facecolor="#EDF1F5", zorder=1)
    ax.add_patch(tab)
    ax.text(x1 + 1, y_top - tab_h / 2 - 0.2, tag, ha="left", va="center", fontsize=7.6, fontweight="bold",
             color=TEXT_DARK, zorder=2)
    ax.text(x1 + tab_w + 2, y_top - tab_h / 2 - 0.2, title, ha="left", va="center", fontsize=7.2,
             color="#444444", style="italic", zorder=2)


# ── 이벤트 ───────────────────────────────────────────────────
y = TOP_Y - 9

self_arrow(y, 1, "select_header_row_with_llm(candidates=[])\n→ 즉시 (None, '후보 행이 전혀 없음') 반환(LLM 호출 없음)"); y -= 6.2
arrow_seq(y, 1, 0, "request_header_row_confirmation\n(시트 8행 전체 미리보기 + 시도내역)"); y -= 5.6
arrow_seq(y, 0, 1, "selected_row_idx = 1", dashed=True); y -= 6.0
self_arrow(y, 1, "parse_excel_to_df 재로드(header=1)\n→ 6행×5열 DataFrame"); y -= 6.0
self_arrow(y, 1, "map_columns_by_header\n→ {} (5개 헤더 전부 규칙 키워드 불일치)"); y -= 6.2

arrow_seq(y, 1, 2, "generate_header_mapping_judgment\n(missing=[영문명,한글명,항목설명] + 헤더별 샘플값)"); y -= 6.0
arrow_seq(y, 2, 1, "영문명→필드코드(0.95), 항목설명→비고(0.95)\n한글명→코드2(0.1, 기준 0.75 미달)", dashed=True); y -= 6.4

arrow_seq(y, 1, 0, "header_mapping_confirmation\n(한글명, 제안:코드2 conf 0.1, 전체 헤더 목록)"); y -= 5.6
arrow_seq(y, 0, 1, "approved, selected_column='코드2'", dashed=True); y -= 6.4

self_arrow(y, 1, "행1(av_rech_amt_data): validate_row_schema\n→ valid(이름필드 3/3 존재, 완전한 행이라 보완 불필요)"); y -= 6.0
arrow_seq(y, 1, 4, "map_candidate_tables('av_rech_amt_data')"); y -= 5.4
arrow_seq(y, 4, 1, "['raw_telecom_fact_recharge_data']", dashed=True); y -= 6.6

loop1_top = y
arrow_seq(y, 1, 3, "find_row_completion 내부:\nexact_match_meta_db(eng_name)"); y -= 5.4
arrow_seq(y, 3, 1, "not found\n(mobile_number는 6개 테이블 중복→ambiguous)", dashed=True); y -= 5.6
arrow_seq(y, 1, 2, "infer_name_fields_with_llm\n(present=1~2개 필드, missing=나머지)"); y -= 5.4
arrow_seq(y, 2, 1, "추론값 + confidence\n(예: 한글명='이동전화번호', 0.95)", dashed=True); y -= 5.8
frag_box(loop1_top + 2.4, y - 1.0, 1, 3, "loop ×4", "행2·3·4·5 — 전부 meta DB 매칭 실패 → LLM 추론")
y -= 3.4

self_arrow(y, 1, "행6(이름필드 전부 공란): validate_row_schema\n→ invalid(missing=[영문명,한글명,항목설명]) → failed_rows"); y -= 6.6

arrow_seq(y, 1, 0, "request_row_completion_confirmation\n(행2~5 보완 제안 4건 일괄)"); y -= 5.6
arrow_seq(y, 0, 1, "approved, approved_row_indices=[1,2,3,4]", dashed=True); y -= 6.4

loop2_top = y
self_arrow(y, 1, "validate_row_schema(보완 반영 후)\n→ valid"); y -= 5.8
arrow_seq(y, 1, 4, "map_candidate_tables(영문명)"); y -= 5.4
arrow_seq(y, 4, 1, "후보 테이블 목록\n(mobile_number만 6개 테이블, 나머지는 [] — 명세서 표기와\n실제 DB 컬럼명이 달라 카탈로그에 없음)", dashed=True, fontsize=6.6); y -= 6.2
frag_box(loop2_top + 2.4, y - 1.0, 1, 4, "loop ×4", "행2·3·4·5 — 보완된 값으로 최종 확정")
y -= 3.6

note(y, 0, 4, "최종 결과: parsed_rows 5건(행1~5) 성공, failed_rows 1건(행6) — 총 25회 Tool 호출, 3회 담당자 확인", height=4.2)

plt.tight_layout()
os.makedirs(os.path.dirname(OUT), exist_ok=True)
plt.savefig(OUT, dpi=165, bbox_inches="tight", facecolor="white")
print(f"작성 완료: {OUT}")
