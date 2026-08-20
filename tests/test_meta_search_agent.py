"""
Meta Search Agent 단위 테스트 (RAG 전환판)

SC-002 개정판(다중소스 검색 + LLM 판단 + 3단계 분기) 대응.
LLM 호출은 chat_fn을 mock으로 주입해 실제 API 없이 검증한다.

실행:
    pytest tests/test_meta_search_agent.py -v
"""

import json
from types import SimpleNamespace

from agents.meta_search_agent import (
    decide_route,
    expand_retrieval_params,
    generate_match_judgment,
    apply_confirmation_result,
    fuzzy_match_candidates,
    AUTO_CONFIRM_CONFIDENCE,
    RETRY_CONFIDENCE_FLOOR,
    MAX_RETRIEVAL_ATTEMPTS,
)


def _fake_chat_response(payload_dict: dict):
    """llm_client.chat()의 반환 형태를 흉내내는 헬퍼"""
    message = SimpleNamespace(content=json.dumps(payload_dict, ensure_ascii=False))
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


class TestDecideRoute:
    def test_high_confidence_auto_confirms(self):
        judgment = {"confidence": AUTO_CONFIRM_CONFIDENCE}
        assert decide_route(judgment, retrieval_attempts=0) == "auto_confirm"

    def test_exactly_at_auto_confirm_threshold(self):
        judgment = {"confidence": AUTO_CONFIRM_CONFIDENCE}
        assert decide_route(judgment, retrieval_attempts=0) == "auto_confirm"

    def test_mid_confidence_retries_when_attempts_remain(self):
        judgment = {"confidence": (AUTO_CONFIRM_CONFIDENCE + RETRY_CONFIDENCE_FLOOR) / 2}
        assert decide_route(judgment, retrieval_attempts=0) == "retry"

    def test_mid_confidence_falls_back_to_human_when_attempts_exhausted(self):
        judgment = {"confidence": (AUTO_CONFIRM_CONFIDENCE + RETRY_CONFIDENCE_FLOOR) / 2}
        assert decide_route(judgment, retrieval_attempts=MAX_RETRIEVAL_ATTEMPTS) == "human_confirm"

    def test_low_confidence_goes_straight_to_human(self):
        judgment = {"confidence": RETRY_CONFIDENCE_FLOOR - 0.01}
        assert decide_route(judgment, retrieval_attempts=0) == "human_confirm"


class TestExpandRetrievalParams:
    def test_top_k_grows_with_attempts(self):
        p1 = expand_retrieval_params(1)
        p2 = expand_retrieval_params(2)
        assert p2["top_k"] > p1["top_k"]

    def test_floor_relaxes_with_attempts_but_has_a_lower_bound(self):
        p1 = expand_retrieval_params(1)
        p5 = expand_retrieval_params(5)
        assert p5["floor"] <= p1["floor"]
        assert p5["floor"] >= 0.55  # 무한정 낮아지지 않도록 하한 존재

    def test_glossary_boost_enabled_on_retry(self):
        assert expand_retrieval_params(1)["include_glossary_boost"] is True


class TestGenerateMatchJudgment:
    def _candidates(self):
        return [
            {"column_id": "col_001", "source": "vss_column", "score": 0.9,
             "meta_row": {"column_name": "SUBS_LINE_CNT", "table_id": "raw_customer", "description": "회선 수"}},
        ]

    def test_valid_response_is_parsed(self):
        def fake_chat(*args, **kwargs):
            return _fake_chat_response({
                "selected_column_id": "col_001", "confidence": 0.95,
                "evidence": "설명이 일치함", "recommend_action": "auto_confirm",
            })
        result = generate_match_judgment({"영문명": "SUBS_LINE_CNT"}, self._candidates(), chat_fn=fake_chat)
        assert result["selected_column_id"] == "col_001"
        assert result["confidence"] == 0.95
        assert result["hallucination_flag"] is False

    def test_hallucinated_column_id_is_rejected(self):
        """후보 목록 밖의 column_id를 응답하면 검증 실패로 처리되고 human_confirm으로 강제 폴백"""
        def fake_chat(*args, **kwargs):
            return _fake_chat_response({
                "selected_column_id": "col_999_존재하지않음", "confidence": 0.99,
                "evidence": "그럴듯한 근거", "recommend_action": "auto_confirm",
            })
        result = generate_match_judgment({"영문명": "SUBS_LINE_CNT"}, self._candidates(), chat_fn=fake_chat)
        assert result["hallucination_flag"] is True
        assert result["selected_column_id"] is None
        assert result["confidence"] == 0.0
        assert result["recommend_action"] == "human_confirm"

    def test_confidence_is_clamped_to_0_1_range(self):
        def fake_chat(*args, **kwargs):
            return _fake_chat_response({
                "selected_column_id": "col_001", "confidence": 1.4,
                "evidence": "근거", "recommend_action": "auto_confirm",
            })
        result = generate_match_judgment({"영문명": "SUBS_LINE_CNT"}, self._candidates(), chat_fn=fake_chat)
        assert result["confidence"] == 1.0

    def test_malformed_json_falls_back_to_human_confirm(self):
        def fake_chat(*args, **kwargs):
            message = SimpleNamespace(content="이건 JSON이 아님")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])
        result = generate_match_judgment({"영문명": "SUBS_LINE_CNT"}, self._candidates(), chat_fn=fake_chat)
        assert result["recommend_action"] == "human_confirm"
        assert result["confidence"] == 0.0


class TestFuzzyMatchCandidates:
    def test_close_typo_is_matched(self):
        pool = [
            {"column_id": "col_001", "column_name": "SUBS_LINE_CNT"},
            {"column_id": "col_999", "column_name": "COMPLETELY_UNRELATED_FIELD"},
        ]
        results = fuzzy_match_candidates("SUB_LINE_CNT", pool, top_k=5)
        ids = [r["column_id"] for r in results]
        assert "col_001" in ids

    def test_weak_matches_are_filtered_out(self):
        pool = [{"column_id": "col_999", "column_name": "COMPLETELY_UNRELATED_FIELD"}]
        results = fuzzy_match_candidates("XYZ", pool, top_k=5)
        assert results == []

    def test_empty_pool_returns_empty_list(self):
        assert fuzzy_match_candidates("ANYTHING", [], top_k=5) == []


class TestApplyConfirmationResult:
    def test_approved_becomes_inferred_confirmed(self):
        result = apply_confirmation_result("approved")
        assert result == {"final_tag": "inferred_confirmed", "confirmation_status": "approved"}

    def test_rejected_becomes_unresolved(self):
        result = apply_confirmation_result("rejected")
        assert result == {"final_tag": "unresolved", "confirmation_status": "rejected"}
