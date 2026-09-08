# -*- coding: utf-8 -*-
"""
scripts/measure_kpi4_via_api_groundtruth.py

KPI4 본 측정 (방식 B) - 격리된 2번째 백엔드(:8010, kpi4b 사본에 연결)를 통해
data/KPI4_재검색감소_측정.xlsx 를 N회 반복 실행한다.

이전 측정(measure_kpi4_isolated.py)의 결함이었던 "LLM 추천안 그대로 승인 ->
오답이 Episodic Memory에 박힘"을 제거하기 위해, 담당자 확인을 **정답 column_id로**
승인한다(그래프 경로 meta_human_confirm_node는 {"decision":"approved",
"selected_column_id": ...} dict를 honor함). 후보에 정답이 안 보이면 "후보군조회"
(get_more_candidates, floor 없이 순위 기반)를 눌러 정답이 나올 때까지 확장한 뒤 승인한다.
후보군조회는 retrieval_attempts(자동 재검색) 예산과 분리돼 있어 KPI4 지표를 왜곡하지 않는다.

측정 후 격리 audit DB의 run_metrics를 읽어 compute_retrieval_attempts로 KPI4를 낸다.
"""
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evaluation.run_metrics_kpi_report import (  # noqa: E402
    compute_processing_time, compute_coverage, compute_reproducibility, compute_retrieval_attempts,
    group_by_eng_name, group_by_thread_id,
)

BACKEND = os.environ.get("BACKEND_URL", "http://localhost:8010")
ROUNDS = int(os.environ.get("ROUNDS", "6"))
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(BASE, "data", "KPI4_재검색감소_측정.xlsx")
KEY = os.path.join(BASE, "data", "KPI4_재검색감소_측정.정답키.json")
AUDIT_DB = os.environ["AUDIT_DB_PATH"]           # 격리 사본 (필수)
OUT = os.path.join(BASE, "measure_kpi4_via_api_groundtruth_result.txt")

assert "kpi4" in AUDIT_DB.replace("\\", "/"), f"AUDIT_DB_PATH가 격리 사본이 아님: {AUDIT_DB}"

answer_key = json.load(open(KEY, encoding="utf-8"))
CONTROLS = {"total_ic_mou", "CumulativeRechargeAmount"}
TARGETS = [k for k in answer_key if k not in CONTROLS]
lines = []
thread_ids = []


def log(s=""):
    print(s, flush=True)
    lines.append(str(s))


def _find_truth_cid(payload):
    eng = payload.get("eng_name")
    truth = answer_key.get(eng)
    if not truth:
        return eng, None, None
    for c in payload.get("candidates", []):
        cid = c.get("column_id") or ""
        if cid.split(".")[-1] == truth:
            return eng, truth, cid
    return eng, truth, None


def confirm(tid, payload, more_budget):
    ptype = (payload or {}).get("type")
    if ptype != "inferred_confirmation":
        return {"decision": "rejected"}, more_budget, None
    eng, truth, cid = _find_truth_cid(payload)
    if cid:
        return ({"decision": {"decision": "approved", "selected_column_id": cid}}, more_budget,
                f"approve->{cid.split('.')[-1]}")
    if more_budget > 0 and not payload.get("candidates_exhausted"):
        return {"decision": "more_candidates"}, more_budget - 1, f"more_candidates(정답 {truth} 미노출, 잔여 {more_budget-1})"
    # 폴백: 정답을 끝내 못 봄 -> LLM 추천안 승인하되 플래그
    rec = payload.get("recommended_column_id")
    if rec:
        return ({"decision": {"decision": "approved", "selected_column_id": rec}}, more_budget,
                f"FALLBACK approve->{rec.split('.')[-1]} (정답 {truth} 후보풀에 없음)")
    return {"decision": "rejected"}, more_budget, f"FALLBACK reject (정답 {truth} 없음)"


def upload():
    with open(SPEC, "rb") as f:
        r = requests.post(f"{BACKEND}/api/specs/upload", files={"file": (os.path.basename(SPEC), f)}, timeout=120)
    if r.status_code == 200:
        return r.json()["file_path"]
    if r.status_code == 422 and isinstance(r.json().get("detail"), dict):
        return r.json()["detail"]["file_path"]
    raise RuntimeError(f"upload {r.status_code}: {r.text[:300]}")


def run_round(rnd):
    fp = upload()
    tid = requests.post(f"{BACKEND}/api/pipeline/start", json={"file_path": fp}, timeout=60).json()["thread_id"]
    thread_ids.append(tid)
    since, t0, more_budget, last = 0, time.time(), 8, None
    while True:
        if time.time() - t0 > 1800:
            raise TimeoutError(f"round {rnd} 30분 초과")
        ev = requests.get(f"{BACKEND}/api/pipeline/{tid}/events", params={"since": since}, timeout=60).json()
        since = ev.get("next_since", since)
        st = ev.get("status")
        if st != last:
            last = st
        if st == "waiting_human":
            body, more_budget, note = confirm(tid, ev.get("confirm_payload"), more_budget)
            if note:
                log(f"    R{rnd} {(ev['confirm_payload'] or {}).get('eng_name','')}: {note}")
            requests.post(f"{BACKEND}/api/pipeline/{tid}/confirm", json=body, timeout=60).raise_for_status()
            time.sleep(0.3)
        elif st == "done":
            log(f"  R{rnd} done  thread={tid[:8]}  {time.time()-t0:.1f}s")
            return
        elif st == "error":
            raise RuntimeError(f"round {rnd} pipeline error: {ev.get('error')}")
        else:
            time.sleep(2)


def fetch_rows():
    con = sqlite3.connect(f"file:{AUDIT_DB}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT eng_name, thread_id, column_id, match_status, retrieval_attempts, "
            "run_started_at, run_completed_at, final_tag, input_file FROM run_metrics "
            f"WHERE thread_id IN ({','.join('?'*len(thread_ids))}) ORDER BY eng_name, run_started_at",
            thread_ids).fetchall()
    finally:
        con.close()


def main():
    log("=" * 96)
    log(f"KPI4 본 측정 (방식 B, 격리 :8010)  ROUNDS={ROUNDS}  AUDIT_DB={AUDIT_DB}")
    log("=" * 96)
    for rnd in range(1, ROUNDS + 1):
        run_round(rnd)

    rows = fetch_rows()
    log(f"\nrun_metrics 수집: {len(rows)}행  (thread {len(thread_ids)}개)")

    thread_grouped = group_by_thread_id(rows)
    grouped = group_by_eng_name(rows)

    # 회차별 attempts 매트릭스
    seq = defaultdict(dict)
    for (eng, tid, cid, status, att, s0, s1, ftag, inp) in rows:
        r = thread_ids.index(tid) + 1
        seq[eng][r] = (att, status, cid)
    log("\n" + "=" * 96)
    log("회차별 retrieval_attempts")
    log("=" * 96)
    log(f"{'영문명':30} {'정답':16} " + " ".join(f"R{i}" for i in range(1, ROUNDS + 1)) + "   비고")
    log("-" * 96)
    for eng in TARGETS + sorted(CONTROLS):
        cells = []
        okall = True
        for i in range(1, ROUNDS + 1):
            att, status, cid = seq.get(eng, {}).get(i, (None, None, None))
            cells.append("·" if att is None and status is None else ("m" if status == "matched" else str(att)))
            if eng in answer_key and cid and cid.split(".")[-1] != answer_key[eng]:
                okall = False
        tag = "" if eng not in answer_key else ("정답매핑 일관" if okall else "⚠️매핑불일치")
        log(f"{eng:30} {answer_key.get(eng,'-'):16} " + " ".join(f"{c:>2}" for c in cells) + f"   {tag}")

    k4 = compute_retrieval_attempts(grouped)
    k3 = compute_reproducibility(grouped)
    k2 = compute_coverage(thread_grouped)
    k1 = compute_processing_time(thread_grouped)
    log("\n" + "=" * 96)
    log("compute_retrieval_attempts (= KPI 리포트 KPI4 집계 결과)")
    log("=" * 96)
    for c in k4["columns"]:
        log(f"  {c['eng_name']:30} attempts={c['attempts']}  first={c['first']} avg_rest={c['avg_rest']} "
            f"status={c['status']} reduction_pct={c['reduction_pct']}")
    log(f"\n  measured(=KPI4 표에 뜨는 행 수): {k4['measured']}")
    reduced = [c for c in k4["columns"] if c["status"] == "reduced"]
    if reduced:
        avg_red = sum(c["reduction_pct"] for c in reduced) / len(reduced)
        log(f"  그 중 status=reduced: {len(reduced)}건, 평균 감소율 {avg_red:.1f}%")
        log(f"  재등장 평균이 0이 된(=완전 소거) 컬럼: {sum(1 for c in reduced if c['avg_rest']==0)}건")
    log(f"\n  [참고] 같은 라운드가 KPI1/2/3에 주는 값:")
    log(f"    KPI1 처리시간: {k1['run_count']}건 평균 {k1['avg_seconds']}초 ({k1['min_seconds']}~{k1['max_seconds']})")
    log(f"    KPI2 커버리지: {k2['run_count']}건 평균 {k2['avg_coverage_pct']}%  (100%미만 {k2['below_100_count']}건)")
    log(f"    KPI3 재현성  : 대상 {k3['total']}건 중 {k3['stable']}건 일관 -> {k3['rate']}%  (정확매칭 {k3['skipped']}건 제외)")
    log("=" * 96)
    log("thread_ids: " + " ".join(thread_ids))
    open(OUT, "w", encoding="utf-8").write("\n".join(lines))
    log(f"\n저장: {OUT}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"\n[중단] {type(e).__name__}: {e}")
        open(OUT, "w", encoding="utf-8").write("\n".join(lines))
        raise
