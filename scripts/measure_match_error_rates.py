# -*- coding: utf-8 -*-
"""
scripts/measure_match_error_rates.py

가이드 렌즈2 B3 "정량 오류 분석(FP/FN)" 실측.

무엇을 재는가
------------
정답키(Ground Truth)가 있는 테스트 명세서를 파이프라인의 컬럼 매칭 단계에 태우되,
담당자 확인 지점에서 **AI 추천안을 그대로 승인**(= 자동 확정 / rubber-stamp 가정)한 뒤
시스템이 고른 컬럼을 정답키와 대조해 아래로 분류한다.

  정탐(TP)     : matched/inferred_confirmed 로 확정 + 고른 컬럼 == 정답
  오매칭(FP)   : matched/inferred_confirmed 로 확정했으나 틀림
                 - 고른 컬럼 != 정답 (정답이 실존하는 행), 또는
                 - 정답키가 __NO_MATCH__ 인데 무언가로 확정 (매칭되면 안 되는데 매칭)
  놓침(FN)     : unresolved 로 종결 + 정답 컬럼이 실제로 존재
  정상 기각(TN): unresolved 로 종결 + 정답키도 "매칭 대상 아님"(__NO_MATCH__)

오매칭(FP) 건수 = "담당자 확인이 없는 자동 확정 구조였다면 그대로 오류가 됐을 건수"
(= 문서의 'HITL 정정 건'). 실제 시스템은 이 지점에 담당자 확인이 있어 걸러진다.

격리 원칙
--------
run_meta_search 는 승인 시 update_meta_tag / persist_confirmed_mapping_example 로 메타 DB에
쓴다. 따라서 META_DB_PATH 는 반드시 스냅샷 사본이어야 하며(경로에 'errspec' 강제),
측정은 그 사본에서만 이뤄진다. 감사 DB에는 쓰지 않는다(run_meta_search 는 log_run_metrics 미호출).

정답키 형식 (KPI4_재검색감소_측정.정답키.json 과 동일)
  { "<명세서 영문명>": "<기대 실제 컬럼명>", ... , "<영문명>": "__NO_MATCH__" }
- 생략된 영문명은, 그 영문명이 메타 DB의 실제 column_name 과 정확히 일치하면 자동으로 정답 처리.
- 그래도 정답을 못 정한 행은 비율 계산에서 제외하고 목록에 남긴다.
- 첫 실행 시 <key>.gt_template.json 을 함께 떨궈, null 칸만 채우면 되도록 한다.

usage
  META_DB_PATH=C:/.../errspec/meta.duckdb \
  DATA_DB_PATH=C:/.../errspec/data.duckdb  AUDIT_DB_PATH=C:/.../errspec/audit.sqlite \
      venv/Scripts/python.exe scripts/measure_match_error_rates.py \
        --spec data/KPI측정용_컬럼매칭_다량테스트.xlsx \
        --key  data/대량컬럼매칭.정답키.json
"""
import argparse
import csv
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

META_DB_PATH = os.environ.get("META_DB_PATH", "")
if "errspec" not in META_DB_PATH.replace("\\", "/"):
    print("거부: META_DB_PATH가 errspec 스냅샷 사본이 아닙니다. 운영 DB 오염 방지를 위해 중단합니다.")
    print(f"  META_DB_PATH={META_DB_PATH!r}")
    sys.exit(2)

import duckdb

from agents.meta_search_agent import run_meta_search
from agents.parsing_agent import run_parsing
from llm_client import embed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NO_MATCH = "__NO_MATCH__"

lines = []


def log(s=""):
    print(s, flush=True)
    lines.append(str(s))


# ── 담당자 확인 자동 응답 ────────────────────────────────────────────────
def approve_ai_pick(payload):
    """AI 추천안을 그대로 승인. 매칭 정확도 측정을 방해하는 다른 확인은 안전하게 처리."""
    t = (payload or {}).get("type")
    if t == "inferred_confirmation":
        return "approved"                      # judgment.selected_column_id 가 확정됨
    if t == "row_completion_confirmation":
        return {"decision": "approved",
                "approved_row_indices": [c["row_index"] for c in payload.get("candidates", [])]}
    if t == "table_disambiguation_confirmation":
        # 동일 컬럼명이 여러 테이블 → 첫 후보 테이블로 확정(컬럼명 단위 정확도 측정이므로 테이블 선택은 무해)
        cands = payload.get("candidates") or []
        if cands:
            return {"decision": "approved", "selected_table_id": cands[0]["table_id"]}
        return {"decision": "rejected"}
    # header_row / header_mapping → 이 측정 대상 아님
    return {"decision": "rejected"}


def reject_all(payload):
    return {"decision": "rejected"}


# ── 정답키 로딩 + 자동 보강 ─────────────────────────────────────────────
def load_ground_truth(spec_rows, key_path):
    key = {}
    if key_path and os.path.exists(key_path):
        key = json.load(open(key_path, encoding="utf-8"))

    con = duckdb.connect(META_DB_PATH, read_only=True)
    real_names = {r[0] for r in con.execute("SELECT DISTINCT column_name FROM column_spec").fetchall()}
    con.close()

    gt, auto, missing = {}, [], []
    for eng in spec_rows:
        if eng in key and key[eng]:
            gt[eng] = key[eng]
        elif eng in real_names:                # 영문명이 실제 컬럼명과 정확히 일치 → 자동 정답
            gt[eng] = eng
            auto.append(eng)
        else:
            gt[eng] = None
            missing.append(eng)

    if missing and key_path:
        tmpl = {eng: (gt[eng] if gt[eng] else None) for eng in spec_rows}
        tp = key_path.replace(".json", "") + ".gt_template.json"
        json.dump(tmpl, open(tp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        log(f"[정답키] 미지정 {len(missing)}건 → 템플릿 생성: {tp}  (null 칸을 채워 --key 로 다시 지정)")
    log(f"[정답키] 총 {len(spec_rows)}행  |  명시 {len(gt) - len(auto) - len(missing)}  자동보강 {len(auto)}  미지정 {len(missing)}")
    return gt


def wipe_episodic_for_gt(gt):
    """정답 컬럼을 가리키는 기존 확인 사례를 사본에서 삭제 → 콜드 상태에서 순수 정확도 측정."""
    targets = sorted({v for v in gt.values() if v and v != NO_MATCH})
    if not targets:
        return
    con = duckdb.connect(META_DB_PATH)
    col_ids = []
    for name in targets:
        col_ids += [r[0] for r in con.execute(
            "SELECT column_id FROM column_spec WHERE column_name = ?", [name]).fetchall()]
    if col_ids:
        ph = ",".join(["?"] * len(col_ids))
        doomed = [r[0] for r in con.execute(
            f"SELECT example_id FROM confirmed_mapping_examples WHERE column_id IN ({ph})", col_ids).fetchall()]
        if doomed:
            dph = ",".join(["?"] * len(doomed))
            con.execute(f"DELETE FROM confirmed_mapping_embeddings WHERE example_id IN ({dph})", doomed)
            con.execute(f"DELETE FROM confirmed_mapping_examples  WHERE example_id IN ({dph})", doomed)
        log(f"[콜드 스타트] 정답 컬럼 대상 confirmed_mapping_examples {len(doomed)}건 삭제")
    con.close()


# ── 분류 ────────────────────────────────────────────────────────────────
def classify(status, chosen, truth):
    confirmed = status in ("matched", "inferred_confirmed")
    if truth is None:
        return "GT미지정"
    if truth == NO_MATCH:
        return "오매칭" if confirmed else "정상기각"   # 확정 = 오수용도 오매칭으로 통합
    if confirmed:
        return "정탐" if chosen == truth else "오매칭"
    return "놓침"                              # unresolved 인데 정답이 실존


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True)
    ap.add_argument("--key", default=None, help="정답키 JSON 경로(없어도 영문명 일치분은 자동 채점)")
    ap.add_argument("--rounds", type=int, default=1, help="반복 실행 수(FP/FN은 1회로 충분, 비결정성 확인 시 상향)")
    ap.add_argument("--cold", dest="cold", action="store_true", default=True)
    ap.add_argument("--no-cold", dest="cold", action="store_false")
    args = ap.parse_args()

    spec = args.spec if os.path.isabs(args.spec) else os.path.join(BASE, args.spec)
    if not os.path.exists(spec):
        log(f"명세서 없음: {spec}")
        log("먼저 생성: venv/Scripts/python.exe scripts/generate_kpi_test_specs.py "
            "(또는 generate_diversity_and_scale_specs.py / generate_type_mismatch_spec.py)")
        sys.exit(1)

    tag = os.path.splitext(os.path.basename(spec))[0]
    out_txt = os.path.join(BASE, f"match_error_rates__{tag}.txt")
    out_csv = os.path.join(BASE, f"match_error_rates__{tag}.rows.csv")

    log("=" * 100)
    log(f"정량 오류 분석 (FP/FN)   spec={os.path.basename(spec)}")
    log(f"META_DB_PATH={META_DB_PATH}")
    log(f"담당자 확인 = AI 추천안 자동 승인(자동 확정 가정)   콜드 스타트={args.cold}   반복 {args.rounds}회")
    log("=" * 100)

    # 명세서 영문명 목록 (parsing 을 한 번 돌려서 실제 인식된 행 기준으로)
    parsed0 = run_parsing(spec, confirm_fn=reject_all)
    spec_engs = [r.get("영문명") or "" for r in parsed0["parsed_rows"]]
    log(f"[parsing] 유효 인식 행 {len(spec_engs)}건")

    gt = load_ground_truth(spec_engs, args.key)
    if args.cold:
        wipe_episodic_for_gt(gt)

    # rounds
    per_round = []
    for rnd in range(1, args.rounds + 1):
        t0 = time.time()
        parsed = run_parsing(spec, confirm_fn=reject_all)
        results = run_meta_search(parsed["parsed_rows"], embed, confirm_fn=approve_ai_pick)
        dt = time.time() - t0
        row_map = {}
        for r in results:
            eng = r.get("영문명") or ""
            mr = r.get("meta_row") or {}
            chosen = mr.get("column_name") or ""
            verdict = classify(r.get("match_status"), chosen, gt.get(eng))
            row_map[eng] = {
                "status": r.get("match_status"),
                "chosen": chosen,
                "chosen_full": f"{mr.get('table_id','')}.{chosen}" if chosen else "",
                "truth": gt.get(eng),
                "conf": r.get("llm_confidence"),
                "attempts": r.get("retrieval_attempts"),
                "evidence": (r.get("llm_evidence") or r.get("match_evidence") or "")[:160],
                "verdict": verdict,
            }
        per_round.append(row_map)
        log(f"[{rnd}회차] {dt:6.1f}s   {dict(Counter(v['verdict'] for v in row_map.values()))}")

    # 대표 회차 = 1회차 기준으로 표/집계 (여러 회차면 verdict 안정성만 부기)
    base = per_round[0]
    counts = Counter(v["verdict"] for v in base.values())
    tp, fp, fn, tn = counts["정탐"], counts["오매칭"], counts["놓침"], counts["정상기각"]
    scored = tp + fp + fn + tn                                    # 정답이 정해진 전체 행

    log("\n" + "=" * 100)
    log("행별 결과 (1회차)")
    log("=" * 100)
    hdr = f"{'영문명':32} {'정답':22} {'시스템 선택':26} {'conf':>5} {'재검색':>4}  판정"
    log(hdr); log("-" * len(hdr))
    for eng in spec_engs:
        d = base.get(eng, {})
        tr = d.get("truth") or "(미지정)"
        log(f"{eng:32.32} {str(tr):22.22} {d.get('chosen_full',''):26.26} "
            f"{str(d.get('conf','')):>5.5} {str(d.get('attempts','')):>4.4}  {d.get('verdict','')}")

    prec = tp / (tp + fp) * 100 if (tp + fp) else None
    rec = tp / (tp + fn) * 100 if (tp + fn) else None
    log("\n" + "=" * 100)
    log("정량 오류 분석 종합")
    log("=" * 100)
    log(f"  채점 대상(정답 확정 행)      : {scored}건")
    log(f"   - 정탐(TP)                  : {tp}")
    log(f"   - 오매칭(FP)                : {fp}   → 오매칭률 {fp/scored*100:.1f}% (전체 대비)"
        if scored else "   - 오매칭(FP): 0")
    log(f"   - 놓침(FN)                  : {fn}   → 놓침률   {fn/scored*100:.1f}% (전체 대비)"
        if scored else "   - 놓침(FN): 0")
    log(f"   - 정상 기각(TN)             : {tn}   (정답키 __NO_MATCH__ 를 unresolved 로 정확히 종결)")
    log(f"  GT 미지정(집계 제외)          : {counts['GT미지정']}")
    if prec is not None:
        log(f"  정밀도 TP/(TP+FP) = {prec:.1f}%   재현율 TP/(TP+FN) = {rec:.1f}%")
    log(f"\n  ▶ 자동 확정 구조였다면 오류가 됐을 건수(FP) = {fp}건")
    log(f"    (실제 시스템은 이 지점에 담당자 확인이 있어 걸러짐 = 'HITL 정정 건')")

    if fp:
        log("\n  [오매칭 상세 — 원인 분류용]")
        for eng in spec_engs:
            d = base.get(eng, {})
            if d.get("verdict") == "오매칭":
                log(f"   - {eng}: 정답={d.get('truth')} / 선택={d.get('chosen_full')} / "
                    f"conf={d.get('conf')} / 재검색={d.get('attempts')} / 근거=\"{d.get('evidence')}\"")
    if fn:
        log("\n  [놓침 상세]")
        for eng in spec_engs:
            d = base.get(eng, {})
            if d.get("verdict") == "놓침":
                log(f"   - {eng}: 정답={d.get('truth')} / status={d.get('status')} / 재검색={d.get('attempts')}")

    if args.rounds > 1:
        unstable = [eng for eng in spec_engs
                    if len({per_round[i].get(eng, {}).get("verdict") for i in range(args.rounds)}) > 1]
        log(f"\n  회차 간 판정이 흔들린 행: {len(unstable)}건 {unstable if unstable else ''}")

    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["round", "eng_name", "truth", "match_status", "chosen_column",
                    "llm_confidence", "retrieval_attempts", "verdict", "evidence"])
        for i, rm in enumerate(per_round, 1):
            for eng in spec_engs:
                d = rm.get(eng, {})
                w.writerow([i, eng, d.get("truth"), d.get("status"), d.get("chosen_full"),
                            d.get("conf"), d.get("attempts"), d.get("verdict"), d.get("evidence")])

    open(out_txt, "w", encoding="utf-8").write("\n".join(lines))
    log(f"\n저장: {out_txt}\n저장: {out_csv}")


if __name__ == "__main__":
    main()
