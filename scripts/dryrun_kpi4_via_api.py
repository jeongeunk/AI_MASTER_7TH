# -*- coding: utf-8 -*-
"""
scripts/dryrun_kpi4_via_api.py

KPI4 측정 명세서(data/KPI4_재검색감소_측정.xlsx)의 사전 검증을, 실행 중인
백엔드(localhost:8000)를 통해 1회 태워보고 run_metrics의 retrieval_attempts를
읽어 "1회차에 재검색이 실제로 걸리는가"를 확인한다.

이 스크립트는 사전 검증 전용이라, 모든 담당자 확인(inferred_confirmation)을
**거절**한다. 거절 경로는 retrieval_attempts를 run_metrics에 그대로 남기면서도
Episodic Memory(confirmed_mapping_examples)에는 아무것도 적재하지 않는다
(persist_confirmed_mapping_example는 승인 경로에서만 호출됨) - 실데이터 메타 DB에
남는 부수효과를 최소화하기 위함.

  - 측정 대상 12건 중 attempts>=1 이 8건 이상  -> 시나리오 성립. 본 측정(3회 반복,
    정답 승인)으로 진행 가능.
  - attempts=0 이 다수  -> 설명을 더 뭉툭하게 / 영문명을 더 우회적으로 재조정.
  - unresolved(no_match) 다수  -> 패러프레이즈가 너무 멀어짐. 정답 쪽으로 당김.
"""
import json
import os
import sqlite3
import sys
import time

import requests

BACKEND = os.environ.get("BACKEND_URL", "http://localhost:8000")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(BASE, "data", "KPI4_재검색감소_측정.xlsx")
KEY = os.path.join(BASE, "data", "KPI4_재검색감소_측정.정답키.json")
AUDIT_DB = os.environ.get("AUDIT_DB_PATH", os.path.join(BASE, "db", "schemascout_audit.sqlite"))
OUT = os.path.join(BASE, "dryrun_kpi4_via_api_result.txt")

answer_key = json.load(open(KEY, encoding="utf-8"))
CONTROLS = {"total_ic_mou", "CumulativeRechargeAmount"}
lines = []


def log(s=""):
    print(s)
    lines.append(str(s))


def upload() -> str:
    with open(SPEC, "rb") as f:
        r = requests.post(f"{BACKEND}/api/specs/upload", files={"file": (os.path.basename(SPEC), f)}, timeout=120)
    if r.status_code == 200:
        return r.json()["file_path"]
    if r.status_code == 422:
        detail = r.json().get("detail", {})
        if isinstance(detail, dict) and detail.get("file_path"):
            log(f"[업로드] 미리보기 파싱은 실패(422)했으나 파일은 저장됨 - 파이프라인으로 직접 진행: {detail['file_path']}")
            return detail["file_path"]
    raise RuntimeError(f"업로드 실패: {r.status_code} {r.text[:400]}")


def confirm(thread_id: str, payload: dict):
    ptype = (payload or {}).get("type")
    if ptype == "header_row_confirmation":
        pv = payload.get("row_previews") or []
        idx = pv[1]["row_idx"] if len(pv) > 1 else (pv[0]["row_idx"] if pv else 0)
        body = {"decision": {"decision": "approved", "selected_row_idx": idx}}
    elif ptype == "header_mapping_confirmation":
        sug = payload.get("suggested_column")
        body = {"decision": {"decision": "approved", "selected_column": sug}} if sug else {"decision": {"decision": "rejected"}}
    elif ptype == "row_completion_confirmation":
        body = {"decision": {"decision": "approved", "approved_row_indices": [c["row_index"] for c in payload.get("candidates", [])]}}
    else:
        # inferred_confirmation / table_disambiguation_confirmation / join_key_confirmation /
        # missing_join_key_confirmation / type_mismatch_confirmation -> 전부 거절.
        # 사전 검증은 Meta Search의 retrieval_attempts만 보므로, 거절이 가장 부수효과가 적다
        # (Episodic Memory 미적재, 실데이터 조회 최소화).
        body = {"decision": "rejected"}
    log(f"    -> confirm({ptype}): {json.dumps(body, ensure_ascii=False)}")
    rr = requests.post(f"{BACKEND}/api/pipeline/{thread_id}/confirm", json=body, timeout=60)
    rr.raise_for_status()


def drive(thread_id: str):
    since, t0, last_status = 0, time.time(), None
    while True:
        if time.time() - t0 > 1800:
            raise TimeoutError("30분 초과 - 중단")
        ev = requests.get(f"{BACKEND}/api/pipeline/{thread_id}/events", params={"since": since}, timeout=60).json()
        since = ev.get("next_since", since)
        status = ev.get("status")
        for e in ev.get("events", []):
            node = e.get("node") or e.get("label") or e.get("step") or ""
            msg = e.get("summary") or e.get("message") or ""
            if node or msg:
                log(f"  · {node} {msg}".rstrip())
        if status != last_status:
            log(f"  [status] {status}")
            last_status = status
        if status == "waiting_human":
            confirm(thread_id, ev.get("confirm_payload"))
            time.sleep(0.5)
        elif status == "done":
            return ev
        elif status == "error":
            raise RuntimeError(f"파이프라인 오류: {ev.get('error')}")
        else:
            time.sleep(2)


def read_metrics(thread_id: str):
    con = sqlite3.connect(f"file:{AUDIT_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT eng_name, column_id, match_status, retrieval_attempts, final_tag, resolution_path "
            "FROM run_metrics WHERE thread_id = ? ORDER BY rowid", [thread_id]
        ).fetchall()
    finally:
        con.close()
    return rows


def main():
    log("=" * 90)
    log(f"KPI4 사전 검증 (라이브 API) - 모든 담당자 확인은 '거절'  BACKEND={BACKEND}")
    log("=" * 90)
    fp = upload()
    tid = requests.post(f"{BACKEND}/api/pipeline/start", json={"file_path": fp}, timeout=60).json()["thread_id"]
    log(f"[thread_id] {tid}\n")
    drive(tid)
    log("\n" + "=" * 90)
    log("run_metrics 집계 결과")
    log("=" * 90)
    rows = read_metrics(tid)
    if not rows:
        log("run_metrics에 이 thread_id 행이 없습니다 - 로깅 경로 확인 필요")
        return
    log(f"\n{'영문명':30} {'정답':16} {'status':14} {'attempts':>8} {'final_tag':>14}  matched_column_id")
    log("-" * 110)
    retry_cnt = zero_cnt = nomatch_cnt = 0
    for eng, cid, status, att, ftag, rpath in rows:
        truth = answer_key.get(eng, "-")
        is_ctrl = eng in CONTROLS
        log(f"{(eng or ''):30} {truth:16} {(status or ''):14} {str(att):>8} {str(ftag):>14}  {cid or ''}"
            + ("   <대조군>" if is_ctrl else ""))
        if is_ctrl:
            continue
        if isinstance(att, int) and att >= 1:
            retry_cnt += 1
        elif (rpath == "no_match") or (status == "unresolved" and att == 0 and rpath != "rejected_by_human"):
            nomatch_cnt += 1
        elif isinstance(att, int) and att == 0:
            zero_cnt += 1
    log("\n" + "-" * 110)
    log(f"측정 대상 12건: 재검색 발생(attempts>=1) {retry_cnt}건 / 재검색 없음(attempts=0) {zero_cnt}건 / no_match {nomatch_cnt}건")
    verdict = ("시나리오 성립 - 본 측정(3회 반복 + 정답 승인) 진행 가능"
               if retry_cnt >= 8 else
               "재조정 필요 - attempts=0 다수면 설명을 더 뭉툭하게, no_match 다수면 패러프레이즈를 정답 쪽으로")
    log(f"판정: {verdict}")
    log("=" * 90)
    open(OUT, "w", encoding="utf-8").write("\n".join(lines))
    log(f"\n결과 저장: {OUT}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"\n[중단] {type(e).__name__}: {e}")
        open(OUT, "w", encoding="utf-8").write("\n".join(lines))
        sys.exit(1)
