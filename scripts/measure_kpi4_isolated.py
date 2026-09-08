# -*- coding: utf-8 -*-
"""
scripts/measure_kpi4_isolated.py

KPI4(컬럼 재등장 시 재검색 감소율) 본 측정.

격리 원칙
--------
실데이터/운영 메타 DB를 절대 건드리지 않는다. META_DB_PATH 는 반드시 스냅샷
사본이어야 하며(경로에 'kpi4' 포함 강제), 측정은 그 사본에서만 이뤄진다.
run_meta_search는 log_run_metrics를 호출하지 않으므로 감사 DB에도 흔적이 없다.

측정 설계
--------
0) 사본에서 "측정 대상 12개 컬럼(통화시간/MOU 계열)"을 정답으로 가리키는
   confirmed_mapping_examples / confirmed_mapping_embeddings 를 삭제한다.
   = 1회차를 "과거 확인 사례가 전혀 없던 상태(cold start)"로 만든다.
   (형제 컬럼 - spl_ic_mou, loc_ic_t2f_mou 등 - 의 확인 사례는 남겨둔다.
    이웃이 촘촘해야 1차 판단이 애매해져 재검색이 걸리기 때문.)
1) data/KPI4_재검색감소_측정.xlsx 를 동일 입력으로 N_ROUNDS(기본 6)회 반복
   run_parsing -> run_meta_search 한다. 매 회차 담당자 확인은 전부 승인.
   승인하면 persist_confirmed_mapping_example 가 사본에 확인 사례를 적재하므로,
   2회차부터는 그 사례가 후보로 재사용된다.
2) 회차 x 컬럼별 retrieval_attempts 를 기록하고,
   "1회차에 재검색이 걸린(attempts>=1) 컬럼"만 대상으로
   mean(1회차) vs mean(2~N회차) 를 비교해 감소율을 낸다.
   1회차부터 attempts=0 인 컬럼(감소 여지 없음)과 정확 매칭 대조군은 제외.

usage:
  META_DB_PATH=<...kpi4/meta.duckdb> AUDIT_DB_PATH=<...kpi4/audit.sqlite> \
  DATA_DB_PATH=<...kpi4/data.duckdb> \
      venv/Scripts/python.exe scripts/measure_kpi4_isolated.py [--rounds 6]
"""
import argparse
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

META_DB_PATH = os.environ.get("META_DB_PATH", "")
if "kpi4" not in META_DB_PATH.replace("\\", "/"):
    print("거부: META_DB_PATH가 kpi4 스냅샷 사본이 아닙니다. 운영 DB 오염 방지를 위해 중단합니다.")
    print(f"  META_DB_PATH={META_DB_PATH!r}")
    sys.exit(2)

import duckdb

from agents.meta_search_agent import RETRY_CONFIDENCE_FLOOR, HIGH_CONFIDENCE_SKIP_RETRY, run_meta_search
from agents.parsing_agent import run_parsing
from llm_client import embed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(BASE, "data", "KPI4_재검색감소_측정.xlsx")
KEY = os.path.join(BASE, "data", "KPI4_재검색감소_측정.정답키.json")
OUT_TXT = os.path.join(BASE, "measure_kpi4_isolated_result.txt")
OUT_CSV = os.path.join(BASE, "measure_kpi4_isolated_rounds.csv")

answer_key = json.load(open(KEY, encoding="utf-8"))
TARGETS = [k for k in answer_key if k not in ("total_ic_mou", "CumulativeRechargeAmount")]
CONTROLS = ["total_ic_mou", "CumulativeRechargeAmount"]

lines = []


def log(s=""):
    print(s)
    lines.append(str(s))


def reject_all(payload):
    return {"decision": "rejected"}


def approve_all(payload):
    ptype = (payload or {}).get("type")
    if ptype == "inferred_confirmation":
        return "approved"          # run_meta_search는 문자열만 해석. LLM 추천안이 확정됨.
    if ptype == "row_completion_confirmation":
        return {"decision": "approved", "approved_row_indices": [c["row_index"] for c in payload.get("candidates", [])]}
    if ptype in ("header_row_confirmation", "header_mapping_confirmation"):
        return {"decision": "rejected"}   # 이 스펙은 규칙 매핑으로 충분 - 호출될 일 없음
    return {"decision": "rejected"}        # table_disambiguation 등 - MOU는 단일 테이블


def wipe_episodic_for_targets(con):
    """측정 대상 12개 컬럼을 정답으로 가리키는 확인 사례를 사본에서 삭제."""
    target_col_ids = []
    for name in [answer_key[t] for t in TARGETS]:
        rows = con.execute("SELECT column_id FROM column_spec WHERE column_name = ?", [name]).fetchall()
        target_col_ids += [r[0] for r in rows]
    before = con.execute("SELECT count(*) FROM confirmed_mapping_examples").fetchone()[0]
    doomed = con.execute(
        f"SELECT example_id, eng_name, column_id FROM confirmed_mapping_examples "
        f"WHERE column_id IN ({','.join(['?'] * len(target_col_ids))})", target_col_ids
    ).fetchall()
    ids = [d[0] for d in doomed]
    if ids:
        ph = ",".join(["?"] * len(ids))
        con.execute(f"DELETE FROM confirmed_mapping_embeddings WHERE example_id IN ({ph})", ids)
        con.execute(f"DELETE FROM confirmed_mapping_examples  WHERE example_id IN ({ph})", ids)
    after = con.execute("SELECT count(*) FROM confirmed_mapping_examples").fetchone()[0]
    log(f"[사전 정리] confirmed_mapping_examples {before} -> {after} (삭제 {before - after}건)")
    for _, eng, cid in doomed:
        log(f"    - 삭제: eng_name={eng!r}  -> {cid}")
    return before - after


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=6)
    args = ap.parse_args()

    log("=" * 96)
    log(f"KPI4 본 측정 (격리 사본)   META_DB_PATH={META_DB_PATH}")
    log(f"재검색 라우팅 구간: [{RETRY_CONFIDENCE_FLOOR}, {HIGH_CONFIDENCE_SKIP_RETRY})   반복 {args.rounds}회")
    log("=" * 96)

    con = duckdb.connect(META_DB_PATH)
    wipe_episodic_for_targets(con)
    con.close()

    # rows[round][eng_name] = dict(attempts, status, conf, matched, correct)
    rounds_data = []
    for rnd in range(1, args.rounds + 1):
        t0 = time.time()
        parsed = run_parsing(SPEC, confirm_fn=reject_all)
        results = run_meta_search(parsed["parsed_rows"], embed, confirm_fn=approve_all)
        elapsed = time.time() - t0
        by_eng = {}
        for r in results:
            eng = r.get("영문명") or ""
            mr = r.get("meta_row") or {}
            matched = f"{mr.get('table_id', '')}.{mr.get('column_name', '')}" if mr else ""
            truth = answer_key.get(eng)
            by_eng[eng] = {
                "attempts": r.get("retrieval_attempts"),
                "status": r.get("match_status"),
                "conf": r.get("llm_confidence"),
                "matched": matched,
                "matched_col": mr.get("column_name", ""),
                "correct": (truth is not None and mr.get("column_name") == truth),
            }
        rounds_data.append(by_eng)
        got = sum(1 for e in TARGETS if isinstance(by_eng.get(e, {}).get("attempts"), int) and by_eng[e]["attempts"] >= 1)
        log(f"[{rnd}회차] {elapsed:6.1f}s   측정대상 재검색발생 {got}/{len(TARGETS)}   "
            f"(status 분포: {_dist(by_eng, TARGETS)})")

    # ---- CSV ----
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["round", "eng_name", "is_target", "attempts", "match_status", "llm_confidence", "matched_column", "correct_vs_truth"])
        for i, by_eng in enumerate(rounds_data, 1):
            for eng in TARGETS + CONTROLS:
                d = by_eng.get(eng, {})
                w.writerow([i, eng, eng in TARGETS, d.get("attempts"), d.get("status"),
                            d.get("conf"), d.get("matched"), d.get("correct")])

    # ---- 회차별 attempts 표 ----
    log("\n" + "=" * 96)
    log("회차별 retrieval_attempts (측정 대상 12건)")
    log("=" * 96)
    hdr = f"{'영문명':30} {'정답':16} " + " ".join(f"R{i}" for i in range(1, args.rounds + 1)) + "   판정"
    log(hdr)
    log("-" * len(hdr))
    measurable, excluded_zero, mispick = [], [], []
    for eng in TARGETS:
        seq = [rounds_data[i].get(eng, {}).get("attempts") for i in range(args.rounds)]
        r1 = seq[0]
        tail = [x for x in seq[1:] if isinstance(x, int)]
        note = ""
        if not all(rounds_data[i].get(eng, {}).get("correct") for i in range(args.rounds)):
            note = "LLM매핑불일치"
            mispick.append(eng)
        elif isinstance(r1, int) and r1 >= 1:
            red = 1 - (sum(tail) / len(tail)) / r1 if tail and r1 else None
            note = f"측정대상  1회차 {r1} -> 재등장 평균 {sum(tail)/len(tail):.2f}  감소율 {red*100:.1f}%" if red is not None else "측정대상"
            measurable.append((eng, r1, sum(tail) / len(tail) if tail else None, red))
        else:
            note = "제외(1회차부터 0회 - 감소 여지 없음)"
            excluded_zero.append(eng)
        log(f"{eng:30} {answer_key[eng]:16} " + " ".join(f"{str(x):>2}" for x in seq) + f"   {note}")

    # ---- 대조군 ----
    log("\n[대조군]")
    for eng in CONTROLS:
        seq = [rounds_data[i].get(eng, {}).get("attempts") for i in range(args.rounds)]
        st = rounds_data[0].get(eng, {}).get("status")
        log(f"{eng:30} {answer_key[eng]:16} " + " ".join(f"{str(x):>4}" for x in seq) + f"   status={st}")

    # ---- 종합 ----
    log("\n" + "=" * 96)
    log("KPI4 종합")
    log("=" * 96)
    log(f"측정 대상(정확매칭 대조군 제외)         : {len(TARGETS)}건")
    log(f"  - LLM 매핑이 매 회차 정답과 불일치     : {len(mispick)}건 {mispick if mispick else ''} (측정에서 제외)")
    log(f"  - 1회차부터 재검색 0회(감소 여지 없음) : {len(excluded_zero)}건 {excluded_zero if excluded_zero else ''} (지표 정의상 제외)")
    log(f"  - 유효 측정 대상(1회차 재검색 발생)    : {len(measurable)}건")
    if measurable:
        m_r1 = sum(x[1] for x in measurable) / len(measurable)
        m_tail = sum(x[2] for x in measurable if x[2] is not None) / len(measurable)
        reds = [x[3] for x in measurable if x[3] is not None]
        log(f"\n  1회차 평균 재검색 횟수     : {m_r1:.2f}")
        log(f"  재등장(2~{args.rounds}회차) 평균 : {m_tail:.2f}")
        log(f"  ▶ 재검색 감소율(행별 평균) : {sum(reds)/len(reds)*100:.1f}%")
        log(f"  ▶ 재검색 감소율(총량 기준) : {(1 - m_tail/m_r1)*100:.1f}%")
        drop_to_zero = sum(1 for x in measurable if x[2] == 0)
        log(f"  재등장 시 재검색이 완전히 0이 된 컬럼: {drop_to_zero}/{len(measurable)}")
    log("=" * 96)
    open(OUT_TXT, "w", encoding="utf-8").write("\n".join(lines))
    log(f"\n저장: {OUT_TXT}\n저장: {OUT_CSV}")


def _dist(by_eng, engs):
    from collections import Counter
    c = Counter(by_eng.get(e, {}).get("status") for e in engs)
    return dict(c)


if __name__ == "__main__":
    main()
