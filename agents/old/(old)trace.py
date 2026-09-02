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


def _brief(value, limit: int = 200):
    """로그에 그대로 찍기엔 너무 큰 값(DataFrame, 긴 리스트 등)을 짧은 문자열로 요약한다."""
    if value is None:
        return None
    s = repr(value)
    return s if len(s) <= limit else s[:limit] + "...(생략)"


class tool_span:
    """with tool_span("retrieve_candidates", model="text-embedding-3-large") as span: ...
    현재 활성화된 trace 수집기에 이 구간의 실행 정보를 기록한다.
    활성 수집기가 없으면(트레이싱 밖에서 단독 호출된 경우) 아무 것도 하지 않는다.

    span.set_args(...)/span.set_result(...)로 입력값·반환값(요약)을 함께 남길 수 있다 -
    특히 담당자 확인(HITL) 지점처럼 "무엇을 근거로 뭘 결정했는지"가 소요시간만큼 중요한
    경우를 위한 것이다. 둘 다 선택 사항이며, 안 부르면 기존과 동일하게 동작한다.

    context는 "지금 몇 번째 행/컬럼을 처리 중인지"처럼, 같은 tool이 루프 안에서 여러 번
    불릴 때 로그에서 구분하기 위한 짧은 태그다(예: "행2: avg_data_amt"). 생성 시점에 몰라도
    span.set_context(...)로 나중에 채울 수 있다.

    예외가 나면(ok=False) exc_type/exc 메시지를 "error"로 함께 남긴다."""

    def __init__(self, tool_name: str, model: str = None, context: str = None):
        self.tool_name = tool_name
        self.model = model
        self.context = context
        self.args = None
        self.result = None
        self._start = None

    def __enter__(self):
        self._start = time.time()
        return self

    def set_args(self, args) -> None:
        self.args = _brief(args)

    def set_result(self, result) -> None:
        self.result = _brief(result)

    def set_context(self, context: str) -> None:
        self.context = context

    def __exit__(self, exc_type, exc, tb):
        end = time.time()
        collector = _current_trace.get()
        if collector is not None:
            entry = {
                "tool": self.tool_name,
                "model": self.model,
                "context": self.context,
                "start": self._start,
                "end": end,
                "duration_sec": round(end - self._start, 3),
                "ok": exc_type is None,
                "args": self.args,
                "result": self.result,
            }
            if exc_type is not None:
                entry["error"] = f"{exc_type.__name__}: {exc}"
            collector.append(entry)
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
