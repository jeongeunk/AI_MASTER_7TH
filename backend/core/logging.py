"""
backend/core/logging.py

reference/개발가이드_v2.md Week3 Step1이 요구하는 고수준 로그(make_log) 구현.
LangGraph의 각 노드가 "7대 구성요소 중 무엇으로서 무엇을 했는지"를 사람이 읽기 좋은
한 줄 요약 + 상세로 남긴다.

이 프로젝트는 이미 agents/trace.py의 tool_span/instrument_agent로 함수(tool) 단위
계측(모델·소요시간·토큰 등)을 갖추고 있다 - make_log는 이를 대체하는 게 아니라,
그 위에 "이 동작이 7대 구성요소 중 무엇에 해당하는가" 분류 레이어 하나를 얹는 것이다.
LangGraph PipelineState의 agent_logs 필드(Annotated[list, operator.add])에 누적되며,
콘솔·모니터링 화면 양쪽에서 그대로 표시에 쓸 수 있다.
"""

from datetime import datetime
from typing import Optional

COMPONENT_EMOJI = {
    "Perception": "👁️",
    "Reasoning": "🧠",
    "Memory": "💾",
    "Action": "⚡",
    "Feedback": "🔄",
    "Evaluation": "📊",
    "Governance": "🛡️",
    "HITL": "🙋",  # Human-in-the-Loop 전용
}


def make_log(
    component: str,
    step: str,
    summary: str,
    detail: str = "",
    metadata: Optional[dict] = None,
) -> dict:
    """고수준 에이전트 로그 1건을 생성한다.

    component: 7대 구성요소 중 하나 또는 "HITL" (COMPONENT_EMOJI 키 참고)
    step:      이 노드/구간의 구체적 이름 (예: "meta_exact_check")
    summary:   화면에 한 줄로 보여줄 요약
    detail:    펼쳐봤을 때 보여줄 상세 내용 (선택)
    metadata:  토큰 수, 소요 시간 등 부가 정보 (선택)
    """
    return {
        "component": component,
        "emoji": COMPONENT_EMOJI.get(component, "📌"),
        "step": step,
        "summary": summary,
        "detail": detail,
        "metadata": metadata or {},
        "timestamp": datetime.now().isoformat(),
    }


def format_log_for_display(log: dict) -> str:
    """콘솔/화면 표시용 한 줄 포맷."""
    return f"{log['emoji']} [{log['component']}] {log['summary']}"
