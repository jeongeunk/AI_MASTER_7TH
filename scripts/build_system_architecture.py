# -*- coding: utf-8 -*-
"""
scripts/build_system_architecture.py

SchemaScout 시스템 구성도(레이어드 아키텍처)를 PNG로 생성한다.
문서(산출물/*.md)와 코드(agents/·backend/·frontend/·scripts/) 기준.

레이어: Front-end UI / Presentation / Business(Agent Orchestrator) / Data / Database
      + Runtime(외부, 우측)
각 Agent는 영문명 아래 한글 역할, 이모지 포함.
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

# 한글(Malgun Gothic) + 이모지(Segoe UI Emoji, 흑백 글리프) per-glyph 폰트 폴백
# ※ family를 리스트로 직접 지정해야 글리프 단위 폴백이 동작한다.
plt.rcParams["font.family"] = ["Malgun Gothic", "Segoe UI Emoji", "Segoe UI Symbol", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "산출물", "시스템_구성도.png")

C = {
    "frontend": "#DCEAF7", "presentation": "#C3DBEF", "business": "#8FB8DE",
    "data": "#6E9BC7", "database": "#4C7BAE", "runtime": "#F4E3C1", "hitl": "#F6D6D6",
}
BORDER = "#2E4B6B"
TD = "#12263A"     # dark text
TL = "#FFFFFF"     # light text

fig, ax = plt.subplots(figsize=(17, 12), dpi=200)
ax.set_xlim(0, 100)
ax.set_ylim(-4, 100)
ax.axis("off")

ax.text(50, 98.5, "SchemaScout  시스템 구성도", ha="center", va="top",
        fontsize=21, fontweight="bold", color=TD)
ax.text(50, 95.0, "명세서·실데이터 정합성 자동 판별 — 6-Agent LangGraph 파이프라인 + Human-in-the-Loop",
        ha="center", va="top", fontsize=10.5, color=TD, style="italic")

LEFT, MAIN_W = 3.5, 68.0


def layer(top, h, title, chips, color, tcolor=TD, left=LEFT, width=MAIN_W, chip_fs=8.0, rows=1):
    ax.add_patch(FancyBboxPatch((left, top - h), width, h,
                 boxstyle="round,pad=0.3,rounding_size=1.4",
                 linewidth=1.6, edgecolor=BORDER, facecolor=color))
    ax.text(left + 2, top - 2.0, title, ha="left", va="top",
            fontsize=12.5, fontweight="bold", color=tcolor)
    per = (len(chips) + rows - 1) // rows
    gy = h - 5.2
    row_h = gy / rows - 1.0
    for idx, txt in enumerate(chips):
        r, cchip = idx // per, idx % per
        chip_w = (width - 4) / per - 1.0
        cx = left + 2 + cchip * (chip_w + 1.0)
        cy_top = top - 4.6 - r * (row_h + 1.0)
        ax.add_patch(FancyBboxPatch((cx, cy_top - row_h), chip_w, row_h,
                     boxstyle="round,pad=0.2,rounding_size=0.7",
                     linewidth=1.0, edgecolor=BORDER, facecolor="white", alpha=0.92))
        ax.text(cx + chip_w / 2, cy_top - row_h / 2, txt, ha="center", va="center",
                fontsize=chip_fs, color=TD, linespacing=1.45)


# y 배치
t_fe, h_fe = 92.0, 11.0
t_pr, h_pr = 79.0, 11.0
t_bz, h_bz = 66.0, 25.0        # Agent 6개 + 공통 가드 행 → 2행, 크게
t_da, h_da = 37.0, 12.0
t_db, h_db = 21.5, 12.0

layer(t_fe, h_fe, "① Front-end UI Layer   ·  Streamlit",
      ["📤 명세서 업로드\n(app.py)", "📡 모니터링\nAgent 로그 · HITL 카드",
       "📁 결과 · 다운로드\n(다중 시트 xlsx)", "📈 KPI 리포트\n(기준시점/전체)",
       "🧭 에이전트 트레이스\n(7대 구성요소 타임라인)"],
      C["frontend"], chip_fs=7.6)

layer(t_pr, h_pr, "② Presentation Layer   ·  FastAPI",
      ["🚪 API Gateway\n/api/specs · /api/pipeline · /api/kpi",
       "🔄 pipeline_runner\n백그라운드 스레드 + HITL 폴링",
       "📦 Pydantic 스키마\n요청 · 응답 검증"],
      C["presentation"], chip_fs=8.0)

layer(t_bz, h_bz,
      "③ Business Layer   ·  Agent Orchestrator  (LangGraph StateGraph + 조건부 엣지 + interrupt/HITL + Checkpointing)",
      ["📄 Parsing Agent\n명세서 구조화\n(규칙→LLM→담당자)",
       "🔍 Meta Search Agent\n컬럼 매칭\n(RAG 4소스 + AI 판단)",
       "🔗 Join Resolution Agent\n조인 가능성 검증\n(값 겹침 실측)",
       "✅ DB Validation Agent\n실 데이터 검증\n(존재·타입·기간)",
       "🏷️ Classification Agent\n최종 판정\n(결정론적 규칙)",
       "📊 Report Agent\n결과 리포트\n(엑셀 산출물)",
       "🛡️ Closed-Set Grounding\n후보 밖 응답 차단(환각)",
       "🧩 Structured Output\nPydantic · Literal 액션값",
       "🚨 prompt_guard\n인젝션 문구 사전 스캔",
       "⚡ 병렬화\nThreadPoolExecutor(안전 구간)",
       "🔁 재시도\ntenacity 지수 백오프",
       "👤 HITL 8개 지점\n추정 매칭은 확신도 무관 필수"],
      C["business"], tcolor=TD, chip_fs=7.4, rows=2)

layer(t_da, h_da, "④ Data Layer   ·  조회 / 검색 추상화",
      ["🧠 RAG Pipeline\nretrieve_candidates\n(컬럼 임베딩+용어집+Episodic Memory+fuzzy)",
       "🔎 Vector Search\nDuckDB vss (HNSW · 코사인)",
       "🔗 check_value_overlap\nSEMI JOIN 값 포함률 실측",
       "🛡️ DB Guardrail\nSQL 구조검증 · READ_ONLY · 식별자 허용목록"],
      C["data"], tcolor=TL, chip_fs=7.2)

layer(t_db, h_db, "⑤ Database Layer",
      ["📚 schemascout_meta.duckdb\n컬럼 명세 · 관계 그래프 · 임베딩\n· Episodic Memory(승인 사례)",
       "🔒 schemascout_data.duckdb\nraw_telecom_* 6개 테이블\n(READ_ONLY)",
       "📝 schemascout_audit.sqlite\n감사 로그 · 실행 계측\n· kpi_baseline · token_usage",
       "💾 langgraph_checkpoints.sqlite\ninterrupt 상태 영속\n(프로세스 재시작 후 재개)"],
      C["database"], tcolor=TL, chip_fs=7.0)

# ── Runtime (우측 외부) ──────────────────────────────────────────────
r_left = LEFT + MAIN_W + 4.0
r_w = 22.5
r_top = t_pr
r_bot = t_da - h_da
r_h = r_top - r_bot
ax.add_patch(FancyBboxPatch((r_left, r_bot), r_w, r_h,
             boxstyle="round,pad=0.3,rounding_size=1.4",
             linewidth=1.6, edgecolor=BORDER, facecolor=C["runtime"]))
ax.text(r_left + r_w / 2, r_top - 2.0, "⑥ Runtime  ·  외부", ha="center", va="top",
        fontsize=12.5, fontweight="bold", color=TD)
r_items = [
    "☁️ Azure OpenAI\n\ngpt-4.1-mini  (헤더/행 단순 분류)\ngpt-5-mini  (컬럼 매칭 최종 판단)\ntext-embedding-3-large  (임베딩)",
    "🔧 llm_client.py\nchat · chat_parsed · embed\n(엔드포인트/모델은 환경변수)",
    "💰 토큰·비용 계측\n명세서 1건당 API 원가 ≈ 16원",
]
seg = (r_h - 6.0) / len(r_items)
for i, it in enumerate(r_items):
    cy = r_top - 4.6 - i * seg
    ax.add_patch(FancyBboxPatch((r_left + 1.4, cy - (seg - 1.4)), r_w - 2.8, seg - 1.4,
                 boxstyle="round,pad=0.2,rounding_size=0.7",
                 linewidth=1.0, edgecolor=BORDER, facecolor="white", alpha=0.92))
    ax.text(r_left + r_w / 2, cy - (seg - 1.4) / 2, it, ha="center", va="center",
            fontsize=7.0, color=TD, linespacing=1.5)

# ── 흐름 화살표 ─────────────────────────────────────────────────────
xmid = LEFT + MAIN_W / 2
for a_top, a_h, b_top in [(t_fe, h_fe, t_pr), (t_pr, h_pr, t_bz),
                          (t_bz, h_bz, t_da), (t_da, h_da, t_db)]:
    ax.add_patch(FancyArrowPatch((xmid, a_top - a_h), (xmid, b_top),
                 arrowstyle="-|>", mutation_scale=17, linewidth=1.8, color=BORDER))

# Business/Data → Runtime (LLM/임베딩 호출) — 메인 컬럼과 Runtime 사이 여백에만
gap_mid = (LEFT + MAIN_W + r_left) / 2
for yy, lbl in [(57.0, "LLM 호출"), (t_da - h_da / 2, "임베딩 호출")]:
    ax.add_patch(FancyArrowPatch((LEFT + MAIN_W, yy), (r_left, yy),
                 arrowstyle="-|>", mutation_scale=13, linewidth=1.3,
                 color=BORDER, linestyle="dashed"))
    ax.text(gap_mid, yy + 1.2, lbl, fontsize=7.0, color=TD, ha="center", va="bottom", style="italic")

# Business ↔ langgraph_checkpoints (interrupt 상태 저장/복원) — 여백 세로 경로
cp_x = gap_mid
ax.add_patch(FancyArrowPatch((cp_x, t_bz - h_bz), (cp_x, t_db - 1.0),
             arrowstyle="<|-|>", mutation_scale=12, linewidth=1.2,
             color="#7A4B8B", linestyle=(0, (2, 2))))
ax.text(cp_x, (t_bz - h_bz + t_db) / 2 + 2.0, "체크포인트\n저장 · 복원", fontsize=6.6,
        color="#5C3A6B", ha="center", va="center", style="italic",
        bbox=dict(boxstyle="round,pad=0.2", facecolor="white", edgecolor="none", alpha=0.85))

# ── 하단 캡션 ──────────────────────────────────────────────────────
cap_y = t_db - h_db - 4.0
ax.text(50, cap_y,
        "명세서 업로드  →  📄 파싱  →  🔍 메타DB 매칭  →  🔗 조인 가능성 검증  →  ✅ 실DB 검증  →  🏷️ 최종 태깅  →  📊 엑셀 리포트",
        ha="center", va="top", fontsize=10.0, color=TD, fontweight="bold")
ax.text(50, cap_y - 3.6,
        "확정 규칙은 코드로 통제 · 애매한 자연어 판단만 AI · 오판 파급이 큰 결정(중복·추정 매칭·조인키 추정)은 담당자 최종 승인",
        ha="center", va="top", fontsize=8.4, color="#3A566E", style="italic")

plt.tight_layout()
os.makedirs(os.path.dirname(OUT), exist_ok=True)
plt.savefig(OUT, dpi=200, bbox_inches="tight", facecolor="white")
print(f"작성 완료: {OUT}")
