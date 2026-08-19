"""
LangGraph 파이프라인 실행 드라이버

interrupt()가 발생하면 그래프 실행이 멈추고 이 스크립트로 제어가 돌아옵니다.
콘솔에서 담당자 응답(y/n)을 받아 Command(resume=...)로 그래프를 재개합니다.

실행: python run_pipeline.py data\\sample_spec_period_test.xlsx
"""

import sys
import uuid

from langgraph.types import Command

sys.path.insert(0, "agents")
from agents.langgraph_pipeline import build_graph, CHECKPOINT_DB_PATH
from langgraph.checkpoint.sqlite import SqliteSaver


def format_confirmation_prompt(payload: dict) -> str:
    if payload["type"] == "inferred_confirmation":
        return (
            "\n" + "=" * 60 +
            "\n[담당자 확인 요청 - inferred 후보]" +
            f"\n  원본 컬럼   : {payload['eng_name']} / {payload['kor_name']}" +
            f"\n  원본 설명   : {payload['description']}" +
            f"\n  매칭 후보   : {payload['candidate_column']} ({payload['candidate_table']})" +
            f"\n  후보 설명   : {payload['candidate_description']}" +
            f"\n  유사도 점수 : {payload['similarity_score']}" +
            "\n" + "=" * 60 +
            "\n이 매칭을 승인하시겠습니까? (y/n): "
        )
    elif payload["type"] == "type_mismatch_confirmation":
        return (
            "\n" + "=" * 60 +
            "\n[담당자 확인 요청 - type 불일치]" +
            f"\n  컬럼      : {payload['column_id']} (테이블: {payload['table']})" +
            f"\n  명세 type : {payload['spec_type']}" +
            f"\n  실제 type : {payload['actual_type']}" +
            "\n" + "=" * 60 +
            "\n실제 DB 기준으로 갱신하시겠습니까? (y/n): "
        )
    return f"\n[확인 요청] {payload}\n승인하시겠습니까? (y/n): "


def main():
    if len(sys.argv) < 2:
        print("사용법: python run_pipeline.py <명세서.xlsx> [thread_id]")
        sys.exit(1)

    input_file = sys.argv[1]
    thread_id = sys.argv[2] if len(sys.argv) > 2 else str(uuid.uuid4())

    print(f"[파이프라인 시작] 입력파일={input_file}, thread_id={thread_id}")

    graph_def = build_graph()

    with SqliteSaver.from_conn_string(CHECKPOINT_DB_PATH) as checkpointer:
        app = graph_def.compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": thread_id}}

        result = app.invoke({"input_file": input_file}, config=config)

        # interrupt가 남아있는 동안 계속 응답 처리
        while "__interrupt__" in result:
            interrupt_obj = result["__interrupt__"][0]
            payload = interrupt_obj.value
            prompt_text = format_confirmation_prompt(payload)
            ans = input(prompt_text).strip().lower()
            decision = "approved" if ans == "y" else "rejected"
            result = app.invoke(Command(resume=decision), config=config)

        print("\n" + "=" * 60)
        print("[파이프라인 완료]")
        print("=" * 60)
        print(f"산출물: {result.get('report_excel_path')}")
        stats = result.get("report_stats", {})
        print(f"\n총 {stats.get('total', 0)}건")
        print("\n[태그별 건수]")
        for tag, cnt in stats.get("tag_counts", {}).items():
            print(f"  {tag}: {cnt}건")
        print("\n[처리 경로별 건수]")
        for path, cnt in stats.get("resolution_path_counts", {}).items():
            print(f"  {path}: {cnt}건")


if __name__ == "__main__":
    main()