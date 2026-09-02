"""
agents/prompt_guard.py

명세서에서 온 자유 텍스트(항목설명·헤더명·샘플값 등)에 프롬프트 인젝션으로 흔히
쓰이는 문구가 섞여 있는지 사전 스캔한다.

이 탐지는 "차단"이 아니라 "경고"용이다 - 정상적인 컬럼 설명에 우연히 비슷한
단어가 섞여 있을 수 있어(오탐 가능성) 자동으로 값을 버리거나 처리를 중단하지
않는다. 대신 탐지되면 해당 HITL(담당자 확인) payload에 경고 필드를 추가로
실어서, 최종 판단은 언제나 담당자가 직접 하도록 한다.

meta_search_agent.py의 request_inferred_confirmation, parsing_agent.py의
request_header_row_confirmation/header_mapping_confirmation/
row_completion_confirmation 4곳에서 공통으로 사용한다(로직 중복을 피하기 위해
탐지 패턴을 이 파일 하나에만 둔다).
"""

import re

# 흔히 쓰이는 프롬프트 인젝션 트리거 문구(한글/영문). 오탐 가능성이 있는 만큼
# "명백히 지시문 형태"인 패턴 위주로만 구성 - 지나치게 넓히면 정상 설명까지
# 자꾸 경고가 떠서 담당자가 경고 자체를 무시하게 되는 역효과가 생긴다.
_INJECTION_PATTERNS = [
    r"이전\s*(지시|명령|프롬프트|instruction)\s*(를|은|는|들)?\s*(무시|잊)",
    r"ignore\s+(all\s+|the\s+)?(previous|above|prior)\s+(instructions?|prompts?)",
    r"시스템\s*프롬프트",
    r"system\s*prompt",
    r"너는\s*이제\s*",
    r"you\s+are\s+now\s+",
    r"disregard\s+(the\s+)?(above|previous|prior)",
    r"confidence\s*(를|을|=|:)?\s*1\.0\s*(으로|로)?\s*(응답|답)",
    r"무조건\s*(승인|approve|confirm)\s*(해|하라|해줘)",
    r"당신의\s*(역할|규칙)\s*(을|를)?\s*(바꿔|변경)",
]
_COMPILED = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]


def scan_for_injection_risk(*texts) -> list:
    """texts에 전달된 문자열들을 스캔해서, 매칭된 패턴을 만든 원본 텍스트 조각과 함께 반환한다.
    반환 형식: [{"text": 의심되는 원본 텍스트, "pattern": 매칭된 패턴 설명}, ...]
    빈 리스트면 의심되는 문구가 없다는 뜻.
    """
    findings = []
    for text in texts:
        if not text:
            continue
        text = str(text)
        for compiled in _COMPILED:
            match = compiled.search(text)
            if match:
                findings.append({"text": text, "pattern": match.group(0)})
                break  # 같은 텍스트에서 패턴 여러 개 겹쳐도 1건만 보고(중복 경고 방지)
    return findings
