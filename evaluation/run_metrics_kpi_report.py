"""
evaluation/run_metrics_kpi_report.py

`run_metrics`(db/schemascout_audit.sqlite) 테이블에 실제로 쌓인 실행 이력을 SQL로
집계해서 KPI3(판정 재현성)·KPI4(컬럼 재등장 시 재검색 감소율)를 산출한다.

run_reproducibility_check.py / run_retrieval_attempts_check.py와의 차이:
- 그 두 스크립트는 run_parsing/run_meta_search를 직접 호출해 "지금 이 자리에서" N회
  반복 실행하고, 결과를 메모리(dict)로 비교한다 - run_metrics 테이블은 아예 거치지 않는다.
- 이 스크립트는 반대로 "이미 쌓여있는" run_metrics를 읽기만 한다 - 즉 실제 웹
  백엔드(backend/core/pipeline_runner.py)를 통해 명세서를 여러 번 업로드해서 실행한
  이력이 있어야 의미 있는 결과가 나온다. 새로 실행을 하지 않는다.

두 방식은 서로 대체재가 아니라 상호 보완 관계다:
- 빠르게 값을 뽑아보고 싶다 -> run_reproducibility_check.py / run_retrieval_attempts_check.py
- 실제 웹 업로드로 쌓인 이력 그대로(사람이 실제로 클릭한 담당자 확인 포함)를 집계하고
  싶다 -> 이 스크립트

집계 대상은 두 스크립트와 동일한 기준을 따른다:
- 정확 매칭(match_status='matched')은 구조상 항상 100% 재현되므로 KPI3 집계에서 제외
- retrieval_attempts가 NULL인 행(정확 매칭 등 애초에 재검색을 타지 않는 경로)은
  KPI4 집계에서 제외

실행:
    python evaluation/run_metrics_kpi_report.py
    python evaluation/run_metrics_kpi_report.py --eng-name mobile_number   # 특정 컬럼만
"""

import argparse
import os
import sqlite3
from collections import Counter, defaultdict

AUDIT_DB_PATH = os.environ.get("AUDIT_DB_PATH", "./db/schemascout_audit.sqlite")


def _fetch_rows(con, eng_name: str = None):
    query = (
        "SELECT eng_name, thread_id, column_id, match_status, retrieval_attempts, run_started_at "
        "FROM run_metrics"
    )
    params = []
    if eng_name:
        query += " WHERE eng_name = ?"
        params.append(eng_name)
    query += " ORDER BY eng_name, run_started_at"
    return con.execute(query, params).fetchall()


def _group_by_eng_name(rows):
    grouped = defaultdict(list)
    for eng_name, thread_id, column_id, match_status, retrieval_attempts, run_started_at in rows:
        grouped[eng_name].append({
            "thread_id": thread_id, "column_id": column_id, "match_status": match_status,
            "retrieval_attempts": retrieval_attempts, "run_started_at": run_started_at,
        })
    return grouped


def report_reproducibility(grouped: dict) -> None:
    """KPI3: 추정 매칭 컬럼(inferred_confirmed)이 재등장할 때마다 같은 column_id로 확정되는가."""
    print("\n=== KPI3. 판정 재현성 (run_metrics 실측 이력 기반) ===")
    stable, unstable, skipped = 0, 0, 0
    for eng_name, entries in sorted(grouped.items()):
        distinct_threads = {e["thread_id"] for e in entries}
        if len(distinct_threads) < 2:
            continue  # 재등장 이력이 없으면 재현성 자체를 판단할 수 없음

        statuses = {e["match_status"] for e in entries}
        if statuses == {"matched"}:
            skipped += 1
            continue  # 정확 매칭은 구조상 항상 100% 재현 -> 집계 제외

        column_ids = [e["column_id"] for e in entries]
        agreement = Counter(column_ids).most_common(1)[0][1] / len(column_ids)
        if agreement == 1.0:
            stable += 1
        else:
            unstable += 1
            print(f"  [불일치] {eng_name}: {[(e['thread_id'][:8], e['column_id']) for e in entries]}")

    total = stable + unstable
    if total == 0:
        print("  측정 대상 없음 - 동일 컬럼이 서로 다른 thread_id로 2회 이상 재등장한 이력이 아직 없습니다.")
        print("  (같은 명세서를 웹에서 최소 2회 이상 업로드해서 실행해야 값이 나옵니다)")
        return
    rate = round(stable / total * 100, 1)
    print(f"  정확 매칭 {skipped}건은 재현율 집계에서 제외")
    print(f"  측정 대상(추정 매칭) {total}건 중 {stable}건 매번 동일한 column_id -> 재현율 {rate}%")


def report_retrieval_attempts(grouped: dict) -> None:
    """KPI4: 컬럼이 재등장할 때 retrieval_attempts(재검색 횟수)가 줄어드는가."""
    print("\n=== KPI4. 컬럼 재등장 시 재검색 감소율 (run_metrics 실측 이력 기반) ===")
    measured = 0
    for eng_name, entries in sorted(grouped.items()):
        distinct_threads = {e["thread_id"] for e in entries}
        if len(distinct_threads) < 2:
            continue

        # thread_id마다 run_started_at 순서로 정렬돼 있으므로(쿼리에서 이미 정렬), 그대로 사용
        attempts = [e["retrieval_attempts"] for e in entries if e["retrieval_attempts"] is not None]
        if not attempts:
            continue  # 정확 매칭 등 애초에 retrieval_attempts가 없는 컬럼은 제외

        first, rest = attempts[0], attempts[1:]
        if not rest:
            continue
        measured += 1
        avg_rest = sum(rest) / len(rest)
        print(f"  {eng_name}: {attempts}", end="  ")
        if first == 0:
            print("-> 1회차부터 이미 0회라 감소율 관찰 불가(0에서 더 못 줄어듦)")
        elif avg_rest < first:
            reduction = round((1 - avg_rest / first) * 100, 1)
            print(f"-> 1회차 {first}회 대비 재등장 시 평균 {avg_rest:.1f}회로 {reduction}% 감소")
        elif avg_rest == first:
            print(f"-> 1회차와 재등장 시 재검색 횟수 동일({first}회) - 감소 없음")
        else:
            print(f"-> 재등장 시 오히려 증가(1회차 {first}회 -> 평균 {avg_rest:.1f}회) - 원인 확인 필요")

    if measured == 0:
        print("  측정 대상 없음 - 재검색이 1회 이상 발생한 컬럼이 서로 다른 thread_id로 재등장한 이력이 아직 없습니다.")
        print("  (재검색이 실제로 발생하는 컬럼을 포함한 명세서를 웹에서 최소 2회 이상 업로드해야 값이 나옵니다)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--eng-name", default=None, help="특정 컬럼(영문명)만 집계")
    args = parser.parse_args()

    con = sqlite3.connect(AUDIT_DB_PATH)
    try:
        con.execute("SELECT 1 FROM run_metrics LIMIT 1")
    except sqlite3.OperationalError:
        print("run_metrics 테이블이 아직 없습니다 - 웹 백엔드로 명세서를 최소 1회 이상 실행한 뒤 다시 시도하세요.")
        return

    rows = _fetch_rows(con, args.eng_name)
    if not rows:
        print("run_metrics에 기록된 데이터가 없습니다.")
        return
    grouped = _group_by_eng_name(rows)

    report_reproducibility(grouped)
    report_retrieval_attempts(grouped)


if __name__ == "__main__":
    main()
