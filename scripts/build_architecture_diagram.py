"""
scripts/build_architecture_diagram.py

기술 아키텍처 슬라이드에 넣을 레이어드 아키텍처 다이어그램을 이미지(PNG)로 생성한다.
Front-end UI / Presentation / Business Layer(Agent 구성) / Data Layer / Database를
수직 스택으로, Runtime(외부 LLM)을 우측에 별도 레이어로 배치한다.
"""

import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

plt.rcParams["font.family"] = "Malgun Gothic"
plt.rcParams["axes.unicode_minus"] = False

OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "산출물", "architecture_diagram.png",
)

# 색상 팔레트 (레이어별 - 파랑 계열 그라데이션 + Runtime만 대비색)
COLORS = {
    "frontend": "#DCEAF7",
    "presentation": "#C3DBEF",
    "business": "#8FB8DE",
    "data": "#6E9BC7",
    "database": "#4C7BAE",
    "runtime": "#F4E3C1",
}
BORDER = "#2E4B6B"
TEXT_DARK = "#1A2E44"
TEXT_LIGHT = "#FFFFFF"

fig, ax = plt.subplots(figsize=(13, 8), dpi=200)
ax.set_xlim(0, 100)
ax.set_ylim(0, 100)
ax.axis("off")

# 제목
ax.text(50, 97, "SchemaScout 기술 아키텍처", ha="center", va="top",
         fontsize=18, fontweight="bold", color=TEXT_DARK)

LEFT, MAIN_W = 4, 66
layer_h = 14
gap = 2
tops = [88, 88 - (layer_h + gap), 88 - 2 * (layer_h + gap), 88 - 3 * (layer_h + gap), 88 - 4 * (layer_h + gap)]


def draw_layer(top, height, label, items, color, text_color=TEXT_DARK, left=LEFT, width=MAIN_W):
    box = FancyBboxPatch((left, top - height), width, height,
                          boxstyle="round,pad=0.3,rounding_size=1.5",
                          linewidth=1.5, edgecolor=BORDER, facecolor=color)
    ax.add_patch(box)
    ax.text(left + 2, top - 2.2, label, ha="left", va="top",
             fontsize=12.5, fontweight="bold", color=text_color)
    # 하위 컴포넌트 chip
    n = len(items)
    chip_w = (width - 4) / n - 1
    chip_h = height - 6.5
    for i, item in enumerate(items):
        cx = left + 2 + i * (chip_w + 1)
        chip = FancyBboxPatch((cx, top - height + 1.3), chip_w, chip_h,
                               boxstyle="round,pad=0.2,rounding_size=0.8",
                               linewidth=1, edgecolor=BORDER, facecolor="white", alpha=0.85)
        ax.add_patch(chip)
        ax.text(cx + chip_w / 2, top - height + 1.3 + chip_h / 2, item,
                 ha="center", va="center", fontsize=7.3, color=TEXT_DARK, linespacing=1.4)


draw_layer(tops[0], layer_h, "Front-end UI  (Streamlit)",
           ["명세서 업로드\n(app.py)", "모니터링\n(Agent 로그 · HITL 카드)", "결과 및 다운로드"],
           COLORS["frontend"])

draw_layer(tops[1], layer_h, "Presentation  (FastAPI)",
           ["/api/specs\n(업로드)", "/api/pipeline\n(시작 · 확인)", "pipeline_runner\n(백그라운드 스레드 + 폴링)"],
           COLORS["presentation"])

draw_layer(tops[2], layer_h, "Business Layer  (Agent 구성)",
           ["Parsing", "Meta Search", "Join Resolution", "DB Validation", "Classification", "Report"],
           COLORS["business"], text_color=TEXT_DARK)

draw_layer(tops[3], layer_h, "Data Layer  (조회/검색 추상화)",
           ["retrieve_candidates\n(임베딩+용어집+fuzzy)", "check_value_overlap\n(SEMI JOIN 실측)",
            "query_retention_period", "Guardrail\n(sqlglot·READ_ONLY)"],
           COLORS["data"], text_color=TEXT_LIGHT)

draw_layer(tops[4], layer_h, "Database",
           ["schemascout_meta.duckdb\n(컬럼·관계·임베딩)", "schemascout_data.duckdb\n(raw_telecom_*, READ_ONLY)",
            "schemascout_audit.sqlite\n(감사 로그)", "langgraph_checkpoints\n.sqlite"],
           COLORS["database"], text_color=TEXT_LIGHT)

# Runtime (우측 외부 레이어)
runtime_left = LEFT + MAIN_W + 4
runtime_w = 22
runtime_top = tops[1]
runtime_h = tops[1] - tops[3] + layer_h
box = FancyBboxPatch((runtime_left, runtime_top - runtime_h), runtime_w, runtime_h,
                      boxstyle="round,pad=0.3,rounding_size=1.5",
                      linewidth=1.5, edgecolor=BORDER, facecolor=COLORS["runtime"])
ax.add_patch(box)
ax.text(runtime_left + runtime_w / 2, runtime_top - 2.2, "Runtime  (외부)", ha="center", va="top",
         fontsize=12.5, fontweight="bold", color=TEXT_DARK)
runtime_items = ["Azure OpenAI\ngpt-4.1-mini / gpt-5-mini\ntext-embedding-3-large",
                 "tenacity\n(지수 백오프 재시도)", "ThreadPoolExecutor\n(안전한 구간만 병렬화)"]
chip_h = (runtime_h - 6.5) / 3 - 1
for i, item in enumerate(runtime_items):
    cy_top = runtime_top - 4.5 - i * (chip_h + 1)
    chip = FancyBboxPatch((runtime_left + 1.5, cy_top - chip_h), runtime_w - 3, chip_h,
                           boxstyle="round,pad=0.2,rounding_size=0.8",
                           linewidth=1, edgecolor=BORDER, facecolor="white", alpha=0.85)
    ax.add_patch(chip)
    ax.text(runtime_left + runtime_w / 2, cy_top - chip_h / 2, item,
             ha="center", va="center", fontsize=7.3, color=TEXT_DARK, linespacing=1.4)

# 레이어 간 수직 화살표 (요청/응답 흐름)
for i in range(4):
    y_start = tops[i] - layer_h
    y_end = tops[i + 1]
    arrow = FancyArrowPatch((LEFT + MAIN_W / 2, y_start), (LEFT + MAIN_W / 2, y_end),
                             arrowstyle="-|>", mutation_scale=14, linewidth=1.5, color=BORDER)
    ax.add_patch(arrow)

# Business/Data Layer -> Runtime 화살표 (LLM/임베딩 호출)
for i in (2, 3):
    y = tops[i] - layer_h / 2
    arrow = FancyArrowPatch((LEFT + MAIN_W, y), (runtime_left, y),
                             arrowstyle="-|>", mutation_scale=12, linewidth=1.3,
                             color=BORDER, linestyle="dashed")
    ax.add_patch(arrow)

ax.text(LEFT + MAIN_W + 1.5, tops[2] - layer_h / 2 + 1.5, "LLM 호출", fontsize=6.5,
         color=TEXT_DARK, ha="left", va="bottom", style="italic")
ax.text(LEFT + MAIN_W + 1.5, tops[3] - layer_h / 2 + 1.5, "임베딩 호출", fontsize=6.5,
         color=TEXT_DARK, ha="left", va="bottom", style="italic")

# 하단 캡션
ax.text(50, tops[4] - layer_h - 4,
         "명세서 업로드 → 파싱 → 메타DB 매칭 → 조인 가능성 검증 → 실DB 검증 → 최종 태깅 → 엑셀 리포트",
         ha="center", va="top", fontsize=9, color=TEXT_DARK, style="italic")

plt.tight_layout()
os.makedirs(os.path.dirname(OUT), exist_ok=True)
plt.savefig(OUT, dpi=200, bbox_inches="tight", facecolor="white")
print(f"작성 완료: {OUT}")
