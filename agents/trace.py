"""
agents/trace.py

Agent/Tool 실행을 계측하기 위한 경량 트레이서.
- tool_span(name, model=None): 개별 tool/LLM 호출 구간을 감싸 (tool, model, 시작/종료/소요시간)을 기록
- instrument_agent(label): Agent(오케스트레이션 함수 또는 LangGraph 노드 함수)의 시작/종료/소요시간을
  기록하고, 그 안에서 발생한 tool_span들을 모아 반환값에 "trace_log" 키로 붙인다.

contextvar 기반이라 스레드/async 경계에서도 안전하며, instrument_agent가 중첩되어도
가장 바깥쪽 스코프만 실제로 수집·part 병합한다(nesting-safe) — 예를 들어
parsing_node()가 이미 @instrument_agent가 붙은 run_parsing()을 호출해도 이중 계측되지 않는다.
"""

import time
import contextvars

_current_trace = contextvars.ContextVar("current_trace", default=None)


def _ensure_trace():
    """현재 활성 trace가 없으면 새로 시작. (token, created) 반환."""
    if _current_trace.get() is not None:
        return None, False
    token = _current_trace.set([])
    return token, True


def _finish_trace(token, created):
    """created=True인(직접 시작한) 스코프만 실제로 수거하고 정리."""
    if not created:
        return None
    events = _current_trace.get()
    _current_trace.reset(token)
    return events


class tool_span:
    """with tool_span("retrieve_candidates", model="text-embedding-3-large"): ...
    현재 활성화된 trace 수집기에 이 구간의 실행 정보를 기록한다.
    활성 수집기가 없으면(트레이싱 밖에서 단독 호출된 경우) 아무 것도 하지 않는다."""

    def __init__(self, tool_name: str, model: str = None):
        self.tool_name = tool_name
        self.model = model
        self._start = None

    def __enter__(self):
        self._start = time.time()
        return self

    def __exit__(self, exc_type, exc, tb):
        end = time.time()
        collector = _current_trace.get()
        if collector is not None:
            collector.append({
                "tool": self.tool_name,
                "model": self.model,
                "start": self._start,
                "end": end,
                "duration_sec": round(end - self._start, 3),
                "ok": exc_type is None,
            })
        return False  # 예외는 그대로 전파


def instrument_agent(agent_label: str):
    """dict를 반환하는 Agent 함수/노드 함수에 붙이는 데코레이터.
    실행 시작/종료/소요시간 + 내부에서 발생한 tool_span들을 결과 dict의 "trace_log" 키로 병합.
    반환값이 dict가 아니면(예: 예외로 함수가 끝나기 전 다른 경로) trace_log를 붙이지 않는다."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            token, created = _ensure_trace()
            agent_start = time.time()
            try:
                result = fn(*args, **kwargs)
            finally:
                agent_end = time.time()
                sub_events = _finish_trace(token, created)
            if created and isinstance(result, dict):
                result = dict(result)
                result["trace_log"] = {
                    "agent": agent_label,
                    "agent_start": agent_start,
                    "agent_end": agent_end,
                    "agent_duration_sec": round(agent_end - agent_start, 3),
                    "tool_calls": sub_events or [],
                }
            return result
        wrapper.__name__ = getattr(fn, "__name__", "wrapped")
        wrapper.__doc__ = fn.__doc__
        return wrapper
    return decorator
