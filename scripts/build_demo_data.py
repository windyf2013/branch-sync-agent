"""Build a rich demo dataset under logs-demo/ for UI review.

Reuses real commits from the manual cycle state.json, then synthesizes
multiple target branches in every status (SUCCESS / PARTIAL / FAILED /
MANUAL) with action_required, tasks rows, patch files and build logs so
every page of the workbench renders fully.
"""

import json
import os
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_STATE = ROOT / "logs/manual-20260826-180253232834-1127791/state.json"
DEMO = ROOT / "logs-demo"
CYCLE_ID = "cycle-2026-08-27"

SRC = json.loads(SRC_STATE.read_text(encoding="utf-8"))
COMMITS = (SRC.get("detected_commits") or [])[:5]
BRANCHES = ["br_v4.33_develop_20260702", "br_v4.33_5200B_develop_20260702",
            "br_v4.34_develop_20261030", "br_v4.33_5200_sdwan_release_feature_quantum_20260625"]
PROD = "RTL9617C 5200"
PROD_B = "X86 5200B"


def c(sha):
    return next((x for x in COMMITS if x["sha"].startswith(sha)), None)


def mk_commit(sha, cp, build=None, conflict=None):
    return {"sha": sha, "cherry_pick": cp, "conflict_resolution": conflict,
            "build": build or {}}


def ok_build():
    return {PROD: {"status": "OK", "model_label": PROD},
            PROD_B: {"status": "OK", "model_label": PROD_B}}


def fail_build(reason, attempts=3, errors=None, log_lines=None):
    out = {"status": "FAILED", "model_label": PROD, "agent_attempts": attempts,
           "reason": reason, "fix_diff": "--- a/plat/lib/sys/ios_syslib.c\n+++ b/plat/lib/sys/ios_syslib.c\n@@ -412,6 +412,7 @@ int ios_sys_info_init(void)\n \tdefault:\n \t\tbreak;\n \t}\n+\treturn ret;\n",
           "errors": errors or ["error: implicit declaration of function 'ios_get_cpu_cores'",
                                "plat/lib/sys/ios_syslib.c:412: undefined reference to 'ios_get_cpu_cores'"],
           "log_preview": log_lines or "\n".join(f"build-{i}: linking... (warning) {i}" for i in range(1, 90)) + "\nmake: *** [Makefile:421: ios] Error 1",
           "log_truncated": True}
    return {PROD: out}


def branch_success(target, shas):
    return {"target_branch": target,
            "worktree_path": f"/srv/bsa/worktrees/{target}-{CYCLE_ID}",
            "status": "SUCCESS",
            "commits": [mk_commit(s, "OK", ok_build()) for s in shas],
            "patch_path": f"logs-demo/patch/{target}.patch",
            "stop_reason": None}


def branch_partial(target, shas):
    commits = [mk_commit(s, "OK", ok_build()) for s in shas[:2]]
    commits.append(mk_commit(shas[2], "OK", fail_build(
        "introduced_by_commit", errors=["undefined reference to 'fibocom_mode_switch_version_ok'"])))
    commits.append(mk_commit(shas[3], "OK", ok_build()))
    return {"target_branch": target,
            "worktree_path": f"/srv/bsa/worktrees/{target}-{CYCLE_ID}",
            "status": "PARTIAL",
            "commits": commits,
            "patch_path": None,
            "stop_reason": "fail-fast: commit 3a20e665 compiled fail; subsequent commits judged related, batch stopped"}


def branch_failed_baseline(target):
    return {"target_branch": target, "worktree_path": None,
            "status": "FAILED", "commits": [],
            "patch_path": None,
            "stop_reason": "baseline full compile failed: branch tip not buildable (pre-existing)"}


def branch_manual(target, shas):
    return {"target_branch": target, "worktree_path": f"/srv/bsa/worktrees/{target}-{CYCLE_ID}",
            "status": "MANUAL",
            "commits": [mk_commit(s, "CONFLICT", {}, None) for s in shas],
            "patch_path": None,
            "stop_reason": None}


def main():
    if DEMO.exists():
        shutil.rmtree(DEMO)
    DEMO.mkdir()
    (DEMO / "cycle-2026-08-27").mkdir()
    (DEMO / "patch").mkdir()
    (DEMO / "tasks").mkdir()

    detected = [c(x) for x in ["1371936f", "3a20e665", "177498d0", "ea4d5894"] if c(x)]
    for i, dc in enumerate(detected):
        dc = dict(dc)
        dc["source_branch"] = "br_v4.33_5200_CU_develop_20260518"
        dc["homologous_section"] = "组网产品分支"
        dc["patch_text"] = dc.get("patch_text") or f"diff --git a/plat/lib/sys/ios_syslib.c\n+ demo patch {i}"
        detected[i] = dc

    decisions = {}
    for dc in detected:
        decisions[dc["sha"]] = {BRANCHES[0]: {"kind": "NeedSync", "evidence": [], "confidence": "high"},
                                BRANCHES[1]: {"kind": "NeedSync", "evidence": [], "confidence": "high"},
                                BRANCHES[2]: {"kind": "OutOfScope", "evidence": ["非同源产品线（4.34 产品主线）"], "confidence": "high"},
                                BRANCHES[3]: {"kind": "AlreadyIncluded", "evidence": ["patch-id 已在目标分支历史"], "confidence": "high"}}

    branch_results = {
        BRANCHES[0]: branch_success(BRANCHES[0], [detected[0]["sha"], detected[1]["sha"]]),
        BRANCHES[1]: branch_partial(BRANCHES[1], [d["sha"] for d in detected[:4]]),
        BRANCHES[2]: branch_failed_baseline(BRANCHES[2]),
        BRANCHES[3]: branch_manual(BRANCHES[3], [detected[0]["sha"]]),
    }

    action_required = [
        {"sha": detected[0]["sha"], "branch": BRANCHES[3], "kind": "ManualReview",
         "reason": "规则层判定 is_bug_fix 置信度不足，需人工裁决",
         "evidence": ["分类证据：commit 信息含 bug 关键词，但改动含重构性质"]},
        {"sha": detected[1]["sha"], "branch": BRANCHES[1], "kind": "ManualReview",
         "reason": "目标分支已包含部分内容，是否覆盖同步存疑",
         "evidence": ["patch-id 部分匹配"]},
        {"node": "prepare_worktree", "error": f"分支 {BRANCHES[2]} 基线全量编译失败（原分支 tip 不可编译），任务停止",
         "branch": BRANCHES[2]},
    ]

    payload = {"cycle_id": CYCLE_ID, "status": "REPORTED",
               "scan_window": ["2026-08-26T22:00:00+08:00", "2026-08-27T22:00:00+08:00"],
               "detected_commits": detected, "decisions": decisions,
               "branch_results": branch_results, "action_required": action_required}
    (DEMO / CYCLE_ID / "state.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (DEMO / CYCLE_ID / "cycle.json").write_text(json.dumps({
        "cycle_id": CYCLE_ID, "status": "REPORTED",
        "report_path": f"logs-demo/{CYCLE_ID}/report.html",
        "mail_status": "sent", "started_at": "2026-08-27T00:00:01", "finished_at": "2026-08-27T00:03:20"}, indent=2), encoding="utf-8")
    (DEMO / CYCLE_ID / "report.html").write_text("<h1>demo report</h1>", encoding="utf-8")

    for target in BRANCHES:
        (DEMO / "patch" / f"{target}.patch").write_text(
            "From demo patch\nSubject: synced commits for " + target +
            "\n\n" + "".join(dc.get("patch_text", "") for dc in detected), encoding="utf-8")

    # tasks table (manual tasks + cycle task) for the platform DB
    import bsa_web.db as webdb
    db = webdb.init_db(DEMO / "platform.sqlite3")
    now = "2026-08-27T08:30:00+08:00"
    db.execute("INSERT INTO tasks(kind,user,target,src,fresh,state,error,cycle_id,source,created_at,shas,commits) VALUES('cycle','system',NULL,NULL,0,'succeeded',NULL,?, 'cron',?,NULL,NULL)", (CYCLE_ID, now))
    db.execute("INSERT INTO tasks(kind,user,target,src,fresh,state,error,cycle_id,source,created_at,shas,commits) VALUES('sync','yangfu','br_v4.34_MSG_develop_20260805','br_v4.33_5200_CU_develop_20260518',0,'succeeded',NULL,'manual-20260827-090000-1','web',?,?,2)",
               ("2026-08-27T09:00:00+08:00", json.dumps([detected[0]["sha"], detected[1]["sha"]])))
    db.execute("INSERT INTO tasks(kind,user,target,src,fresh,state,error,cycle_id,source,created_at,shas,commits) VALUES('rerun','yangfu','br_v4.33_5200B_develop_20260702',NULL,1,'running',NULL,'manual-20260827-093000-2','web',?,NULL,NULL)",
               ("2026-08-27T09:30:00+08:00",))
    db.execute("INSERT INTO audit_log(ts,user,action,cycle_id,target,sha,detail_json,result) VALUES(?,?,?,?,?,?,?,?)",
               ("2026-08-27T10:00:00+08:00", "yangfu", "push", CYCLE_ID, BRANCHES[0], None, json.dumps({"commits": 2}), "succeeded"))
    db.close()

    # a build log file for download links
    (DEMO / "tasks" / "task-2.log").write_text("line1\nline2\ncherry-pick OK\nbuild starting...\nmake: Error 1\n", encoding="utf-8")

    print("demo data written to", DEMO)


if __name__ == "__main__":
    main()
