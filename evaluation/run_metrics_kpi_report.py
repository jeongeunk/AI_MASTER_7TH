"""
evaluation/run_metrics_kpi_report.py

`run_metrics`(db/schemascout_audit.sqlite) 테이블에 실제로 쌓인 실행 이력을 SQL로
집계해서 KPI1(검증 소요 시간)·KPI2(자동 판별 커버리지)·KPI3(판정 재현성)·
KPI4(컬럼 재등장 시 재검색 감소율) 4개 모두를 산출한다.

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

집계 단위가 두 갈래다:
- KPI1(검증 소요 시간)·KPI2(자동 판별 커버리지)는 **실행(thread_id) 단위** 지표다
  ("이번 업로드 1건이 몇 초 걸렸는가", "이번 업로드에서 컬럼이 몇 % 빠짐없이 태그됐는가").
- KPI3(판정 재현성)·KPI4(재검색 감소율)는 **컬럼(eng_name) 단위** 지표다
  ("이 컬럼이 재등장했을 때 결과가 같은가").
group_by_thread_id()/group_by_eng_name() 두 그룹핑 함수가 이 구분에 대응한다.

집계 대상은 두 스크립트와 동일한 기준을 따른다:
- 정확 매칭(match_status='matched')은 구조상 항상 100% 재현되므로 KPI3 집계에서 제외
- retrieval_attempts가 NULL인 행(정확 매칭 등 애초에 재검색을 타지 않는 경로)은
  KPI4 집계에서 제외

이 모듈은 CLI(`main`)뿐 아니라 backend/api/kpi.py에서도 그대로 import해서 쓴다.
집계 로직(compute_*)과 화면/콘솔 출력(report_*)을 분리해 둔 이유가 그것이다 -
API는 compute_* 결과를 JSON으로 그대로 반환하고, CLI는 같은 결과를 report_*가
사람이 읽기 좋은 텍스트로 출력한다. 즉 어느 쪽을 쓰든 집계 기준은 항상 하나다.

실행:
    python evaluation/run_metrics_kpi_report.py
    python evaluation/run_metrics_kpi_report.py --eng-name mobile_number   # 특정 컬럼만(KPI3·4에만 적용)
"""

import argparse
import os
import sqlite3
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.core.metrics_store import _ensure_run_metrics_table

AUDIT_DB_PATH = os.environ.get("AUDIT_DB_PATH", "./db/schemascout_audit.sqlite")


def get_connection():
    return sqlite3.connect(AUDIT_DB_PATH)


def run_metrics_table_exists(con) -> bool:
    """테이블 존재 여부 확인과 함께, 있으면 최신 스키마로 마이그레이션까지 보장한다
    (final_tag 컬럼처럼 나중에 추가된 컬럼이 이 스크립트를 직접 돌릴 때도 준비돼 있도록 -
    metrics_store.py의 마이그레이션 로직을 그대로 재사용해 이중 관리를 피한다)."""
    try:
        con.execute("SELECT 1 FROM run_metrics LIMIT 1")
    except sqlite3.OperationalError:
        return False
    _ensure_run_metrics_table(con)
    con.commit()
    return True


def fetch_rows(con, eng_name: str = None):
    """eng_name을 넘기면 그 컬럼만 필터링한다 - KPI3·4(컬럼 단위)에서만 의미가 있고,
    KPI1·2(실행 단위)는 항상 eng_name=None(전체)으로 호출해야 한다(특정 컬럼만 걸러내면
    실행의 분모/처리시간이 왜곡된다)."""
    query = (
        "SELECT eng_name, thread_id, column_id, match_status, retrieval_attempts, "
        "run_started_at, run_completed_at, final_tag, input_file "
        "FROM run_metrics"
    )
    params = []
    if eng_name:
        query += " WHERE eng_name = ?"
        params.append(eng_name)
    query += " ORDER BY eng_name, run_started_at"
    return con.execute(query, params).fetchall()


def group_by_eng_name(rows):
    """KPI3(판정 재현성)·KPI4(재검색 감소율) 집계용 - 컬럼(eng_name) 단위 그룹핑."""
    grouped = defaultdict(list)
    for eng_name, thread_id, column_id, match_status, retrieval_attempts, run_started_at, \
            run_completed_at, final_tag, input_file in rows:
        grouped[eng_name].append({
            "thread_id": thread_id, "column_id": column_id, "match_status": match_status,
            "retrieval_attempts": retrieval_attempts, "run_started_at": run_started_at,
        })
    return grouped


def group_by_thread_id(rows):
    """KPI1(검증 소요 시간)·KPI2(자동 판별 커버리지) 집계용 - 실행(thread_id) 단위 그룹핑.
    run_started_at/run_completed_at/input_file은 같은 thread_id의 모든 행에 동일하게
    중복 저장돼 있으므로(파이프라인 전체 시작/종료 시각), 그대로 그 실행의 대표값으로 쓴다."""
    grouped = {}
    for eng_name, thread_id, column_id, match_status, retrieval_attempts, run_started_at, \
            run_completed_at, final_tag, input_file in rows:
        entry = grouped.setdefault(thread_id, {
            "input_file": input_file, "run_started_at": run_started_at,
            "run_completed_at": run_completed_at, "columns": [],
        })
        entry["columns"].append({"eng_name": eng_name, "final_tag": final_tag})
    return grouped


def compute_reproducibility(grouped: dict) -> dict:
    """KPI3: 추정 매칭 컬럼(inferred_confirmed)이 재등장할 때마다 같은 column_id로 확정되는가.

    반환값은 report_reproducibility(콘솔 출력)와 backend/api/kpi.py(JSON 응답)가 공유한다.
    """
    stable, unstable, skipped = 0, 0, 0
    mismatches = []
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
            mismatches.append({
                "eng_name": eng_name,
                "occurrences": [{"thread_id": e["thread_id"], "column_id": e["column_id"]} for e in entries],
            })

    total = stable + unstable
    rate = round(stable / total * 100, 1) if total else None
    return {
        "stable": stable, "unstable": unstable, "skipped": skipped,
        "total": total, "rate": rate, "mismatches": mismatches,
    }


def compute_retrieval_attempts(grouped: dict) -> dict:
    """KPI4: 컬럼이 재등장할 때 retrieval_attempts(재검색 횟수)가 줄어드는가."""
    columns = []
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

        avg_rest = sum(rest) / len(rest)
        if first == 0:
            status, reduction_pct = "unchanged_zero", None
        elif avg_rest < first:
            status, reduction_pct = "reduced", round((1 - avg_rest / first) * 100, 1)
        elif avg_rest == first:
            status, reduction_pct = "unchanged", 0.0
        else:
            status, reduction_pct = "increased", None

        columns.append({
            "eng_name": eng_name, "attempts": attempts, "first": first,
            "avg_rest": round(avg_rest, 2), "status": status, "reduction_pct": reduction_pct,
        })

    return {"measured": len(columns), "columns": columns}


def compute_processing_time(thread_grouped: dict) -> dict:
    """KPI1: 명세서 업로드 ~ 최종 report 산출까지 실행(thread_id) 단위 전체 소요 시간."""
    runs = []
    for thread_id, entry in thread_grouped.items():
        started, completed = entry["run_started_at"], entry["run_completed_at"]
        if started is None or completed is None:
            continue  # 파이프라인이 끝까지 안 간 실행(중간 오류 등)은 처리시간 집계에서 제외
        seconds = round(completed - started, 2)
        runs.append({
            "thread_id": thread_id, "input_file": entry["input_file"],
            "seconds": seconds, "run_started_at": started,
        })
    runs.sort(key=lambda r: r["run_started_at"])

    if not runs:
        return {"run_count": 0, "avg_seconds": None, "min_seconds": None, "max_seconds": None, "runs": []}

    all_seconds = [r["seconds"] for r in runs]
    return {
        "run_count": len(runs),
        "avg_seconds": round(sum(all_seconds) / len(all_seconds), 2),
        "min_seconds": min(all_seconds),
        "max_seconds": max(all_seconds),
        "runs": runs,
    }


def compute_coverage(thread_grouped: dict) -> dict:
    """KPI2: 자동 판별 커버리지 = final_tag가 채워진 컬럼 수 ÷ 전체 컬럼 수(실행 단위).
    분모(전체 컬럼 수)는 그 실행에서 run_metrics에 적재된 행 수 그대로다 - meta_results
    자체가 이미 "Parsing이 유효한 컬럼으로 인정한 것 + Join Resolution이 추가한 것"의
    합이라(문제 정의 문서 KPI2 공식 참고), 별도로 분모를 다시 계산할 필요가 없다.

    설계상 이 값은 항상 100%여야 한다(판별 안 되는 컬럼도 final_tag='unresolved'로
    반드시 채워지도록 만들었으므로) - 100% 미만이 나오면 그 자체가 파이프라인 어딘가에서
    컬럼이 조용히 누락되고 있다는 버그 신호로 해석해야 한다.
    """
    runs = []
    for thread_id, entry in thread_grouped.items():
        columns = entry["columns"]
        denominator = len(columns)
        if denominator == 0:
            continue
        numerator = sum(1 for c in columns if c["final_tag"])
        coverage_pct = round(numerator / denominator * 100, 1)
        runs.append({
            "thread_id": thread_id, "input_file": entry["input_file"],
            "numerator": numerator, "denominator": denominator,
            "coverage_pct": coverage_pct, "run_started_at": entry["run_started_at"],
        })
    runs.sort(key=lambda r: r["run_started_at"] or 0)

    if not runs:
        return {"run_count": 0, "avg_coverage_pct": None, "below_100_count": 0, "runs": []}

    pcts = [r["coverage_pct"] for r in runs]
    below_100 = [r for r in runs if r["coverage_pct"] < 100.0]
    return {
        "run_count": len(runs),
        "avg_coverage_pct": round(sum(pcts) / len(pcts), 1),
        "below_100_count": len(below_100),
        "runs": runs,
    }


def report_processing_time(thread_grouped: dict) -> None:
    print("\n=== KPI1. 검증 소요 시간 (run_metrics 실측 이력 기반) ===")
    result = compute_processing_time(thread_grouped)
    if result["run_count"] == 0:
        print("  측정 대상 없음 - 끝까지 완료된 실행 이력이 아직 없습니다.")
        return
    print(f"  실행 {result['run_count']}건, 평균 {result['avg_seconds']}초 "
          f"(최소 {result['min_seconds']}초 ~ 최대 {result['max_seconds']}초)")
    for r in result["runs"]:
        print(f"  {r['thread_id'][:8]} ({r['input_file']}): {r['seconds']}초")


def report_coverage(thread_grouped: dict) -> None:
    print("\n=== KPI2. 자동 판별 커버리지 (run_metrics 실측 이력 기반) ===")
    result = compute_coverage(thread_grouped)
    if result["run_count"] == 0:
        print("  측정 대상 없음 - 실행 이력이 아직 없습니다.")
        return
    print(f"  실행 {result['run_count']}건, 평균 커버리지 {result['avg_coverage_pct']}%")
    if result["below_100_count"] > 0:
        print(f"  ⚠️ 100% 미만인 실행 {result['below_100_count']}건 발견 - 설계상 100%가 보장돼야 하므로 "
              "파이프라인에서 컬럼이 누락됐을 가능성이 있습니다.")
    for r in result["runs"]:
        flag = " ⚠️" if r["coverage_pct"] < 100.0 else ""
        print(f"  {r['thread_id'][:8]} ({r['input_file']}): {r['numerator']}/{r['denominator']} "
              f"-> {r['coverage_pct']}%{flag}")


def report_reproducibility(grouped: dict) -> None:
    print("\n=== KPI3. 판정 재현성 (run_metrics 실측 이력 기반) ===")
    result = compute_reproducibility(grouped)
    for m in result["mismatches"]:
        occ = [(o["thread_id"][:8], o["column_id"]) for o in m["occurrences"]]
        print(f"  [불일치] {m['eng_name']}: {occ}")

    if result["total"] == 0:
        print("  측정 대상 없음 - 동일 컬럼이 서로 다른 thread_id로 2회 이상 재등장한 이력이 아직 없습니다.")
        print("  (같은 명세서를 웹에서 최소 2회 이상 업로드해서 실행해야 값이 나옵니다)")
        return
    print(f"  정확 매칭 {result['skipped']}건은 재현율 집계에서 제외")
    print(f"  측정 대상(추정 매칭) {result['total']}건 중 {result['stable']}건 매번 동일한 column_id "
          f"-> 재현율 {result['rate']}%")


def report_retrieval_attempts(grouped: dict) -> None:
    print("\n=== KPI4. 컬럼 재등장 시 재검색 감소율 (run_metrics 실측 이력 기반) ===")
    result = compute_retrieval_attempts(grouped)
    for col in result["columns"]:
        print(f"  {col['eng_name']}: {col['attempts']}", end="  ")
        if col["status"] == "unchanged_zero":
            print("-> 1회차부터 이미 0회라 감소율 관찰 불가(0에서 더 못 줄어듦)")
        elif col["status"] == "reduced":
            print(f"-> 1회차 {col['first']}회 대비 재등장 시 평균 {col['avg_rest']:.1f}회로 "
                  f"{col['reduction_pct']}% 감소")
        elif col["status"] == "unchanged":
            print(f"-> 1회차와 재등장 시 재검색 횟수 동일({col['first']}회) - 감소 없음")
        else:
            print(f"-> 재등장 시 오히려 증가(1회차 {col['first']}회 -> 평균 {col['avg_rest']:.1f}회) "
                  "- 원인 확인 필요")

    if result["measured"] == 0:
        print("  측정 대상 없음 - 재검색이 1회 이상 발생한 컬럼이 서로 다른 thread_id로 재등장한 이력이 아직 없습니다.")
        print("  (재검색이 실제로 발생하는 컬럼을 포함한 명세서를 웹에서 최소 2회 이상 업로드해야 값이 나옵니다)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--eng-name", default=None, help="특정 컬럼(영문명)만 집계(KPI3·4에만 적용)")
    args = parser.parse_args()

    con = get_connection()
    if not run_metrics_table_exists(con):
        print("run_metrics 테이블이 아직 없습니다 - 웹 백엔드로 명세서를 최소 1회 이상 실행한 뒤 다시 시도하세요.")
        return

    # KPI1·2(실행 단위)는 항상 전체 데이터 기준 - eng_name 필터를 적용하면 안 됨
    all_rows = fetch_rows(con)
    if not all_rows:
        print("run_metrics에 기록된 데이터가 없습니다.")
        return
    thread_grouped = group_by_thread_id(all_rows)
    report_processing_time(thread_grouped)
    report_coverage(thread_grouped)

    # KPI3·4(컬럼 단위)는 --eng-name 필터 적용 가능
    rows = fetch_rows(con, args.eng_name) if args.eng_name else all_rows
    grouped = group_by_eng_name(rows)
    report_reproducibility(grouped)
    report_retrieval_attempts(grouped)


if __name__ == "__main__":
    main()
