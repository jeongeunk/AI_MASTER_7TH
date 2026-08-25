"""
SchemaScout LLM 클라이언트 래퍼

Base URL: https://skax.ai-talentlab.com
API 경로 패턴: /openai/deployments/{deployment}/chat/completions?api-version=...
-> 이 구조는 Azure OpenAI 규격과 동일하므로 openai SDK의 AzureOpenAI 클라이언트를 그대로 사용합니다.
   (표준 OpenAI 클라이언트가 아니라 AzureOpenAI를 써야 base_url이 올바르게 조립됩니다.)

.env에 있는 값을 읽어 초기화하며, 절대 키를 코드에 하드코딩하지 않습니다.
"""

import os
from dotenv import load_dotenv
from openai import AzureOpenAI

load_dotenv()

_client = None


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