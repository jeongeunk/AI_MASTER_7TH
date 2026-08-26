"""
SchemaScout LLM 클라이언트 래퍼

Base URL: https://skax.ai-talentlab.com
API 경로 패턴: /openai/deployments/{deployment}/chat/completions?api-version=...
-> 이 구조는 Azure OpenAI 규격과 동일하므로 openai SDK의 AzureOpenAI 클라이언트를 그대로 사용합니다.
   (표준 OpenAI 클라이언트가 아니라 AzureOpenAI를 써야 base_url이 올바르게 조립됩니다.)

.env에 있는 값을 읽어 초기화하며, 절대 키를 코드에 하드코딩하지 않습니다.

Exception Handling: 일시적 오류(rate limit/timeout/connection/5xx)는 지수 백오프로
자동 재시도하고, 영구적 오류(인증 실패·잘못된 요청 등)는 재시도해봐야 소용없으므로
즉시 그대로 전파한다. 재시도가 모두 소진되면 마지막 예외를 그대로 올려
(reraise) 호출부(Agent)가 이를 감지·처리할 수 있게 한다.
"""

import os
import sys

from dotenv import load_dotenv
from openai import (
    AzureOpenAI,
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

load_dotenv()

_client = None

# 재시도 대상: 요청 측 문제(인증 실패, 잘못된 요청, 컨텐츠 필터 등)가 아니라
# "다시 시도하면 성공할 수도 있는" 일시적 오류만 포함한다.
_RETRYABLE_ERRORS = (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)


def _log_retry(retry_state):
    exc = retry_state.outcome.exception()
    print(
        f"[llm_client] 일시적 오류로 재시도 {retry_state.attempt_number}회차 "
        f"({type(exc).__name__}: {exc}) - {retry_state.next_action.sleep:.1f}초 후 재시도",
        file=sys.stderr,
    )


_retry_llm_call = retry(
    retry=retry_if_exception_type(_RETRYABLE_ERRORS),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    stop=stop_after_attempt(4),
    before_sleep=_log_retry,
    reraise=True,
)


def get_client() -> AzureOpenAI:
    global _client
    if _client is None:
        base_url = os.environ["LLM_BASE_URL"]
        api_key = os.environ["LLM_API_KEY"]
        api_version = os.environ.get("LLM_API_VERSION", "2024-12-01-preview")
        _client = AzureOpenAI(
            azure_endpoint=base_url,
            api_key=api_key,
            api_version=api_version,
        )
    return _client


@_retry_llm_call
def chat(deployment_env_key: str, messages: list, **kwargs):
    """
    deployment_env_key 예: "DEPLOYMENT_GPT41_MINI" (.env에 정의된 배포명 환경변수 키)
    """
    client = get_client()
    deployment = os.environ[deployment_env_key]
    response = client.chat.completions.create(
        model=deployment,
        messages=messages,
        **kwargs,
    )
    return response


@_retry_llm_call
def chat_parsed(deployment_env_key: str, messages: list, response_format, **kwargs):
    """
    Structured Output 전용 호출. response_format에 Pydantic 모델을 전달하면
    스키마를 준수하는 응답을 강제하고, resp.choices[0].message.parsed 에
    검증된 모델 인스턴스를 채워 반환한다(실패/거부 시 parsed는 None, refusal에 사유).
    """
    client = get_client()
    deployment = os.environ[deployment_env_key]
    response = client.chat.completions.parse(
        model=deployment,
        messages=messages,
        response_format=response_format,
        **kwargs,
    )
    return response


@_retry_llm_call
def embed(deployment_env_key: str, input_text):
    """
    deployment_env_key 예: "DEPLOYMENT_EMBED_LARGE"
    input_text: str 또는 list[str]
    """
    client = get_client()
    deployment = os.environ[deployment_env_key]
    response = client.embeddings.create(
        model=deployment,
        input=input_text,
    )
    return response


if __name__ == "__main__":
    # 간단 연결 테스트: python llm_client.py
    r = chat("DEPLOYMENT_GPT41_MINI", [{"role": "user", "content": "안녕? 한 문장으로만 답해줘."}])
    print("[chat] ", r.choices[0].message.content)

    e = embed("DEPLOYMENT_EMBED_LARGE", "가입회선수")
    print("[embed] dim =", len(e.data[0].embedding))