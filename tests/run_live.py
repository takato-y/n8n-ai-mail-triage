#!/usr/bin/env python3
"""ローカルの n8n でテストケースを実際に実行する。

  python3 tests/run_live.py [TC-001 TC-002 ...]

流れ: テスト用の写しを作成 → 有効化 → Webhook に各ケースを POST → 無効化 → 削除。
写しの Webhook パスは毎回ランダムにし、本番用のワークフローとぶつからないようにする。
途中で失敗しても、作成した写しは finally で必ず削除する。

n8n の API への接続（どちらか一方）:
  - 環境変数 N8N_API_KEY にキーを入れる（キーはリポジトリに置かない）
  - 環境変数 N8N_HELPER_DIR に、API クライアント（n8n_api.py）のあるフォルダを指定する
任意: N8N_URL（既定 http://localhost:5678）
"""
import copy
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import mock_llm  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / "workflow" / "ai-mail-triage.json"
CASES = ROOT / "tests" / "cases"
OUT = ROOT / "tests" / "out"
N8N_URL = os.environ.get("N8N_URL", "http://localhost:5678")
MOCK_PORT = int(os.environ.get("MOCK_LLM_PORT", "18765"))


# ---------------------------------------------------------------- API

def _api():
    """(method, path, body) -> (status, json) を返す関数を用意する。"""
    if os.environ.get("N8N_API_KEY"):
        key = os.environ["N8N_API_KEY"]

        def call(method, path, body=None):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(f"{N8N_URL}/api/v1{path}", data=data, method=method,
                                         headers={"X-N8N-API-KEY": key, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw else {})
        return call
    helper = os.environ.get("N8N_HELPER_DIR")
    if helper:
        sys.path.insert(0, helper)
        import n8n_api  # noqa: E402  キーの読み込みは API クライアント側で行う
        return n8n_api._req
    sys.exit("N8N_API_KEY または N8N_HELPER_DIR を設定してください")


def post_webhook(path, payload, timeout=600):
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(f"{N8N_URL}/webhook/{path}", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code, raw = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read().decode()
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = raw
    return code, body, round(time.time() - t0, 1)


# ---------------------------------------------------------------- 準備

def make_test_copy(wf, variant, path):
    """テスト用の写し。Webhook パスと識別子を差し替え、ケースの設定上書きを当てる。"""
    w = copy.deepcopy(wf)
    w["name"] = f"[TEST] {wf['name']} {time.strftime('%Y%m%d-%H%M%S')}"
    for n in w["nodes"]:
        if n["type"] == "n8n-nodes-base.webhook":
            n["parameters"]["path"] = path
            n["webhookId"] = str(uuid.uuid4())
        if n["name"] == "Set Config" and variant:
            for a in n["parameters"]["assignments"]["assignments"]:
                if a["name"] in variant:
                    a["value"] = variant[a["name"]]
    return {"name": w["name"], "nodes": w["nodes"], "connections": w["connections"],
            "settings": w.get("settings", {"executionOrder": "v1"})}


def fetch_error_log(api, wid):
    """直近の実行から Log Error ノードの出力（エラーログ）を取り出す。"""
    for _ in range(10):
        _, d = api("GET", f"/executions?workflowId={wid}&limit=1&includeData=true")
        data = d.get("data") or []
        if data and data[0].get("finished") is not None:
            run = (((data[0].get("data") or {}).get("resultData") or {}).get("runData") or {})
            if "Log Error" in run:
                return run["Log Error"][-1]["data"]["main"][0][0]["json"].get("logEntry")
            return None
        time.sleep(0.5)
    return None


def check(case, code, body):
    """期待値との照合。合否と理由を返す。"""
    exp = case["expect"]
    got = body if isinstance(body, dict) else {}
    problems = []
    if "httpStatus" in exp and code != exp["httpStatus"]:
        problems.append(f"HTTP {code} != {exp['httpStatus']}")
    if "status" in exp and got.get("status") != exp["status"]:
        problems.append(f"status {got.get('status')} != {exp['status']}")
    if "category" in exp and got.get("category") != exp["category"]:
        problems.append(f"category {got.get('category')} != {exp['category']}")
    if "urgency" in exp and (got.get("extracted") or {}).get("urgency") != exp["urgency"]:
        problems.append(f"urgency {(got.get('extracted') or {}).get('urgency')} != {exp['urgency']}")
    if "attempts" in exp and got.get("attempts") != exp["attempts"]:
        problems.append(f"attempts {got.get('attempts')} != {exp['attempts']}")
    if "mockRequests" in exp and case.get("_mockRequests") != exp["mockRequests"]:
        problems.append(f"mock requests {case.get('_mockRequests')} != {exp['mockRequests']}")
    if "errorStage" in exp and (got.get("error") or {}).get("stage") != exp["errorStage"]:
        problems.append(f"error.stage {(got.get('error') or {}).get('stage')} != {exp['errorStage']}")
    return (not problems), problems


# ---------------------------------------------------------------- 実行

def run(case_ids):
    api = _api()
    wf = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    cases = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CASES.glob("TC-*.json"))]
    if case_ids:
        cases = [c for c in cases if c["id"] in case_ids]
    # 設定上書き（variant）ごとに写しを分ける
    groups = {}
    for c in cases:
        groups.setdefault(json.dumps(c.get("variant") or {}, sort_keys=True), []).append(c)

    OUT.mkdir(exist_ok=True)
    mock = mock_llm.start(MOCK_PORT)
    results, created = [], []
    try:
        for vkey, group in groups.items():
            path = f"test-{uuid.uuid4().hex[:12]}"
            payload = make_test_copy(wf, json.loads(vkey), path)
            st, d = api("POST", "/workflows", payload)
            wid = d["id"]
            created.append(wid)
            api("POST", f"/workflows/{wid}/activate")
            time.sleep(1)
            for c in group:
                before = len(mock_llm.REQUESTS)
                code, body, sec = post_webhook(path, c["payload"])
                c["_mockRequests"] = len(mock_llm.REQUESTS) - before
                time.sleep(1)
                error_log = fetch_error_log(api, wid)
                ok, problems = check(c, code, body)
                if c["expect"].get("status", "ok") != "ok" and not error_log:
                    ok = False
                    problems.append("エラーログ（Log Error の出力）がありません")
                results.append({"id": c["id"], "ok": ok, "problems": problems,
                                "http": code, "seconds": sec, "response": body})
                (OUT / f"{c['id']}.json").write_text(
                    json.dumps({"http": code, "seconds": sec, "mockRequests": c["_mockRequests"],
                                "response": body, "errorLog": error_log}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                print(f"{c['id']}: {'PASS' if ok else 'FAIL'} HTTP {code} {sec}s {problems or ''}")
    finally:
        for wid in created:
            try:
                api("POST", f"/workflows/{wid}/deactivate")
            except Exception as e:  # 無効化に失敗しても削除は試みる
                print(f"deactivate {wid}: {e}")
            st, _ = api("DELETE", f"/workflows/{wid}")
            print(f"cleanup: deleted test workflow {wid} (HTTP {st})")
        _, d = api("GET", "/workflows?limit=250")
        left = [w["id"] for w in d.get("data", []) if w["id"] in created]
        print(f"cleanup: leftover={left}")
        mock.shutdown()

    passed = sum(r["ok"] for r in results)
    print(f"SUMMARY: {passed}/{len(results)} PASS")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
