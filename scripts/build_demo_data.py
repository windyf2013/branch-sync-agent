"""Build a comprehensive demo dataset under logs-demo/ for UI review.

Covers every big-picture state and its click-through so each page renders
coherently and every navigation target exists:

- 周期/任务大状态：成功 / 失败 / 进行中；
- 分支状态：SUCCESS、停批（编译失败+修复重编译耗尽）、失败（基线编译失败）、
  待处理（冲突解决失败 / 判定待人工 / severity_gate / fix_missing）、已放弃；
- 进行中阶段：基线全量编译时、cherry-pick 时、解决冲突时、修复重编译时
  （由 progress.jsonl 未闭合 start 驱动，/tasks/{id} 页显示实时步骤）；
- 多分支自动周期（当前 cycle-2026-08-28）+ 历史三个周期 + 手动 sync/rerun
  单分支周期 + 进行中任务；跨页跳转全部可达。

当前周期上下文 = cycle-2026-08-28（最近完成 cron 周期）。手动任务按发起时间
落在当天（>= 周期启动时刻）→ 显示在工作台；更早的归档进历史页。

布局（logs-demo/）：
  历史: cycle-2026-08-25 FAILED · cycle-2026-08-26 PARTIAL · cycle-2026-08-27 SUCCESS
  当前: cycle-2026-08-28 FAILED(rich, 5 分支)          ← 工作台当前
  手动: manual-20260826(历史同步) · manual-20260827 sync+rerun(fttr, 归并演示)
        manual-20260828-0900 今日成功同步(政企)
  进行中(manual/rerun-20260828-*, 无 state.json, 有 progress.jsonl):
        manual-…-1300 sync  @ cherry-pick 时
        rerun-…-1400   FAIL_B @ 基线全量编译时
        rerun-…-1410   MAN_B  @ 解决冲突时
        rerun-…-1420   PART_B @ 修复重编译时
"""

import json
import shutil
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "logs-demo"

# --- 周期 id ---
C25 = "cycle-2026-08-25"   # FAILED
C26 = "cycle-2026-08-26"   # PARTIAL
C27 = "cycle-2026-08-27"   # SUCCESS（历史）
C28 = "cycle-2026-08-28"   # 当前 rich FAILED

# --- 手动/重跑投影目录（写 state.json，不写 cycle.json → 不被枚举）---
M_OLD = "manual-20260826-180000-0"          # 历史手动 sync（政企）
M_FTTR_SYNC = "manual-20260827-090000-1"    # 历史 fttr sync（rerun 归并父）
R_FTTR_RERUN = "rerun-20260827-120000-5"    # 历史 fttr rerun（归并覆盖）
M_TODAY = "manual-20260828-090000-2"        # 今日成功 sync（政企）

# --- 进行中目录（progress.jsonl，无 state.json）---
RUN_CHERRY = "manual-20260828-130000-3"     # sync @ cherry-pick
RUN_BASELINE = "rerun-20260828-140000-4"    # rerun @ 基线编译
RUN_RESOLVE = "rerun-20260828-141000-5"     # rerun @ 解决冲突
RUN_FIX = "rerun-20260828-142000-6"         # rerun @ 修复重编译

# --- 分支（与 spec/branch.md 章节一致；running 目标需互不相同）---
SUCC_B = "br_v4.33_develop_20260702"
PART_B = "br_v4.33_5200B_develop_20260702"
FAIL_B = "br_v4.34_develop_20261030"
MAN_B = "br_v4.33_5200_sdwan_release_feature_quantum_20260625"
ABANDON_B = "br_v4.35_develop_20260625"     # 路由器类（已放弃 SUCCESS）
OK_TGT = "br_v4.34_MSG_develop_20260805"    # 政企：今日同步成功 + 进行中同步
FTTR_TGT = "br_v4.34_develop_fttr_20260811"  # fttr：历史 sync+rerun 归并
OK_SRC = "br_v4.33_5200_CU_develop_20260518"

WT = "/srv/bsa/worktrees/{target}-{cycle}"
PROD = "5200"
PROD_B = "5200B"

# --- 检测到的 commit 池（d1..d5）---
DETECTED = [
    dict(sha="1371936fba520a3bf4db29f92489dda1876adb8a", author="zhao.li",
         committed_at="2026-08-27T09:12:33+08:00",
         message="fix: ios_sys_info 时钟源切换导致 CPU 频率读错",
         changed_files=["plat/lib/sys/ios_syslib.c", "plat/lib/sys/ios_syslib.h"],
         patch_id="p-1371936f", homologous_section="组网产品分支",
         source_branch=OK_SRC, issue_ids=[], symbols=["ios_sys_info_init"],
         patch_text="diff --git a/plat/lib/sys/ios_syslib.c\n@@ -410,6 +410,8 @@ int ios_sys_info_init(void)\n+  clock_source=CLK_SRC_PLL;\n"),
    dict(sha="3a20e6654c5d8e3172d1d920145ed26e16c2a677", author="wang.fang",
         committed_at="2026-08-27T11:02:17+08:00",
         message="feat: 组网 5200 增加 16 层标签 VLAN 透传支持",
         changed_files=["plat/lib/datapath/vlan.c", "include/plat/vlan.h", "Makefile"],
         patch_id="p-3a20e665", homologous_section="组网产品分支",
         source_branch=OK_SRC, issue_ids=["RCIOS-4421"], symbols=["vlan_ingress"],
         patch_text="diff --git a/plat/lib/datapath/vlan.c\n@@ -221,6 +221,9 @@ int vlan_ingress(struct sk_buff *skb)\n+\tif (tag>15) tag=15;\n"),
    dict(sha="177498d0f3e1b6d2b9c8a0d1e2f3a4b5c6d7e8f9", author="sun.qi",
         committed_at="2026-08-27T13:47:02+08:00",
         message="fix: 组网 mcast 表项超限时错误上报中断链路",
         changed_files=["plat/lib/datapath/mcast.c", "plat/lib/datapath/mcast.h"],
         patch_id="p-177498d0", homologous_section="组网产品分支",
         source_branch=OK_SRC, issue_ids=[], symbols=["mcast_add_entry"],
         patch_text="diff --git a/plat/lib/datapath/mcast.c\n@@ -88,7 +88,7 @@ int mcast_add_entry(entry, len)\n-\tif (ret < 0) return ret;\n+\tif (ret < 0) { mcast_report_full(); return ret; }\n"),
    dict(sha="ea4d5894f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d", author="liu.tian",
         committed_at="2026-08-27T15:23:44+08:00",
         message="fix: PPPoE 会话老化定时器偶发提前清除会话",
         changed_files=["plat/lib/ppp/pppoe.c"],
         patch_id="p-ea4d5894", homologous_section="组网产品分支",
         source_branch=OK_SRC, issue_ids=[], symbols=["pppoe_age"],
         patch_text="diff --git a/plat/lib/ppp/pppoe.c\n@@ -143,6 +143,8 @@ static void pppoe_age(struct timer *t)\n+\tif (age<MIN_AGE) return;\n"),
    dict(sha="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b", author="zhao.li",
         committed_at="2026-08-27T16:01:55+08:00",
         message="fix: 组网 5200 sdwan 子卡协商速率读寄存器偏移错位",
         changed_files=["plat/lib/sdwan/sdwan_link.c"],
         patch_id="p-a1b2c3d4", homologous_section="组网产品分支",
         source_branch=OK_SRC, issue_ids=["RCIOS-4429"], symbols=["sdwan_speed"],
         patch_text="diff --git a/plat/lib/sdwan/sdwan_link.c\n@@ -77,7 +77,7 @@ u32 sdwan_speed(void)\n-\treturn reg_val & 0xf;\n+\treturn (reg_val>>4) & 0xf;\n"),
]


def _dc(i):
    return DETECTED[i]


def _log_write(path, lines):
    full = DEMO / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# 构造器
# --------------------------------------------------------------------------

def mk_commit(sha, cp, build=None, conflict=None, resolution_error=None):
    return {"sha": sha, "cherry_pick": cp, "conflict_resolution": conflict,
            "build": build or {}, "resolution_error": resolution_error}


def cb(status, model=PROD, reason=None, log_path=None, errors=None,
       agent_attempts=0, fix_diff=None):
    return {"model": model, "status": status, "log_path": log_path,
            "errors": errors or [], "agent_attempts": agent_attempts,
            "fix_diff": fix_diff, "reason": reason}


def commit_build_ok(model=PROD):
    return cb("OK", model=model)


def _commit_log(cid, target, sha):
    return f"{cid}/build/{target}/{sha}/build.log"


def make_build_failed(cid, target, sha):
    """一个修复重编译 N 次仍失败的 build outcome：errors + 日志 + fix_diff。"""
    lp = _commit_log(cid, target, sha)
    _log_write(lp, [
        "Compiling plat/lib/datapath/vlan.c ...",
        "plat/lib/datapath/vlan.c: In function 'vlan_ingress':",
        "  error: 'tag' undeclared (first use in this function)",
        "make[1]: *** [Makefile:421: vlan.o] Error 1",
        "make: *** [Makefile:89: all] Error 2", "ERROR: build failed",
    ])
    return {PROD: cb("FAILED", reason="introduced_by_commit", log_path=lp,
                     errors=["undefined reference to 'vlan_tag_ingress'",
                             "implicit declaration of function 'vlan_tag_ingress'"],
                     agent_attempts=3,
                     fix_diff="--- a/plat/lib/datapath/vlan.c\n+++ b/plat/lib/datapath/vlan.c\n@@ -218,7 +218,8 @@ int vlan_ingress(struct sk_buff *skb)\n \tint tag;\n \tif (unlikely(!skb)) return -EINVAL;\n+\ttag = ntohs(vlan_hdr->h_vlan_TCI) & 0xfff;\n")}


def branch_success(cid, target, shas, models=None):
    models = models or [PROD, PROD_B]
    commits = [mk_commit(s, "OK", build={m: commit_build_ok(m) for m in models})
               for s in shas]
    return {"target_branch": target,
            "worktree_path": WT.format(target=target, cycle=cid),
            "status": "SUCCESS", "commits": commits,
            "patch_path": f"logs-demo/{cid}/{target}.patch", "stop_reason": None,
            "baseline": {m: commit_build_ok(m) for m in models}}


def branch_stop_at_build(cid, target, ok_shas, fail_sha):
    commits = [mk_commit(s, "OK", build={PROD: commit_build_ok(PROD)}) for s in ok_shas]
    commits.append(mk_commit(fail_sha, "OK", build=make_build_failed(cid, target, fail_sha)))
    return {"target_branch": target,
            "worktree_path": WT.format(target=target, cycle=cid),
            "status": "PARTIAL", "commits": commits, "patch_path": None,
            "stop_reason": f"fail-fast: commit {fail_sha[:8]} 编译失败，修复重编译 3 次仍失败；后续 commit 判定关联，批次停止",
            "baseline": {PROD: commit_build_ok(PROD)}}


def branch_baseline_fail(cid, target, model=PROD, reason=None):
    lp = f"{cid}/build/{target}/baseline/{model}.log"
    _log_write(lp, ["Configuring for RTL9617C ...", "checking for gcc ... ok",
                    f"error: {reason or 'toolchain for target not found'}",
                    "make: *** No rule to make target 'clean'. Stop."])
    return {"target_branch": target, "worktree_path": None, "status": "FAILED",
            "commits": [], "patch_path": None,
            "stop_reason": reason or "baseline full compile failed: branch tip not buildable (pre-existing)",
            "baseline": {model: cb("FAILED", model=model, reason="baseline failed",
                                   log_path=lp, errors=["toolchain for target not found"])}}


def branch_conflict_failed(cid, target, sha):
    return {"target_branch": target,
            "worktree_path": WT.format(target=target, cycle=cid),
            "status": "MANUAL",
            "commits": [mk_commit(sha, "CONFLICT",
                                  resolution_error="冲突文件疑似二进制（无法安全自动合并），转人工处理")],
            "patch_path": None, "stop_reason": None,
            "baseline": {PROD: commit_build_ok(PROD)}}


def branch_success_one(cid, target, sha, conflict=None):
    commit = mk_commit(sha, "OK",
                       conflict=conflict,
                       build={PROD: commit_build_ok(PROD), PROD_B: commit_build_ok(PROD_B)})
    return {"target_branch": target,
            "worktree_path": WT.format(target=target, cycle=cid),
            "status": "SUCCESS", "commits": [commit],
            "patch_path": f"logs-demo/{cid}/{target}.patch", "stop_reason": None,
            "baseline": {PROD: commit_build_ok(PROD)}}


def branch_success_repaired(cid, target, sha, conflict=None, fix_diff=None):
    """SUCCESS 但过程有 冲突已解决 + 编译失败→智能体修复→通过：
    commit.cherry_pick=OK（冲突已并入）、build 终态 OK 但带 agent_attempts+fix_diff
    （引擎在 fix_build 成功后把修复产物留在终态 outcome，见 nodes.fix_build）。"""
    commit = mk_commit(sha, "OK", conflict=conflict,
                       build={PROD: cb("OK", agent_attempts=2, fix_diff=fix_diff,
                                       reason="introduced_by_commit"),
                              PROD_B: commit_build_ok(PROD_B)})
    return {"target_branch": target,
            "worktree_path": WT.format(target=target, cycle=cid),
            "status": "SUCCESS", "commits": [commit],
            "patch_path": f"logs-demo/{cid}/{target}.patch", "stop_reason": None,
            "baseline": {PROD: commit_build_ok(PROD)}}


def write_state(cid, payload):
    d = DEMO / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding="utf-8")


def write_cycle(cid, status, started, finished):
    d = DEMO / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "cycle.json").write_text(json.dumps(
        {"cycle_id": cid, "status": status, "report_path": f"{cid}/report.html",
         "mail_status": "sent", "started_at": started, "finished_at": finished},
        ensure_ascii=False, indent=2), encoding="utf-8")
    (d / "report.html").write_text(f"<h1>demo report {cid} {status}</h1>", encoding="utf-8")


def write_patch(cid, target, shas):
    d = DEMO / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{target}.patch").write_text(
        "From demo\nSubject: demo sync patch\n\n" + "\n".join(shas), encoding="utf-8")


# --------------------------------------------------------------------------
# 当前周期 cycle-2026-08-28（rich FAILED）
# --------------------------------------------------------------------------

def build_current_cycle():
    d = {i: _dc(i)["sha"] for i in range(5)}
    branches = {
        SUCC_B: branch_success(C28, SUCC_B, [d[0], d[1], d[2]]),
        PART_B: branch_stop_at_build(C28, PART_B, [d[0]], d[1]),
        FAIL_B: branch_baseline_fail(C28, FAIL_B),
        MAN_B: branch_conflict_failed(C28, MAN_B, d[2]),
        ABANDON_B: branch_success_one(C28, ABANDON_B, d[3]),
    }
    detected = [dict(_dc(i)) for i in range(5)]
    for dc in detected:
        dc["source_branch"] = OK_SRC
    # decisions[sha][branch] -> kind
    nd = {"kind": "NeedSync", "evidence": [], "confidence": "high"}
    inc = {"kind": "AlreadyIncluded", "evidence": ["patch-id 已在目标分支历史"], "confidence": "high"}
    oos = {"kind": "OutOfScope", "evidence": ["非同源产品线"], "confidence": "high"}
    decisions = {}
    for i in range(5):
        decisions[d[i]] = {}
    decisions[d[0]] = {SUCC_B: nd, PART_B: nd, FAIL_B: oos, MAN_B: inc, ABANDON_B: nd}
    decisions[d[1]] = {SUCC_B: nd, PART_B: {**nd, "kind": "ManualReview",
                                             "evidence": ["目标分支已包含部分内容，是否覆盖同步存疑"],
                                             "confidence": "low", "cause": "fix_missing"},
                       FAIL_B: oos, MAN_B: inc, ABANDON_B: nd}
    decisions[d[2]] = {SUCC_B: nd, PART_B: inc, FAIL_B: oos, MAN_B: nd, ABANDON_B: inc}
    decisions[d[3]] = {SUCC_B: nd, PART_B: inc, FAIL_B: oos, MAN_B: inc, ABANDON_B: nd}
    decisions[d[4]] = {SUCC_B: inc,
                       PART_B: {**oos, "kind": "ManualReview",
                                "evidence": ["改动符号跨产品线共享，风险待人工判定"],
                                "confidence": "medium", "cause": "severity_gate"},
                       FAIL_B: oos,
                       MAN_B: {**oos, "kind": "ManualReview",
                               "evidence": ["LLM 未判定（pending），转人工审核"],
                               "confidence": "low", "cause": "pending"},
                       ABANDON_B: inc}
    # action_required：ManualReview 项（带 cause）+ 一条 MAN_B 冲突解决失败的节点错误
    action_required = [
        {"sha": d[1], "branch": PART_B, "kind": "ManualReview", "cause": "fix_missing",
         "evidence": ["目标分支已包含部分内容，是否覆盖同步存疑"]},
        {"sha": d[4], "branch": PART_B, "kind": "ManualReview", "cause": "severity_gate",
         "evidence": ["改动符号跨产品线共享，风险待人工判定"]},
        {"sha": d[4], "branch": MAN_B, "kind": "ManualReview", "cause": "pending",
         "evidence": ["LLM 未判定（pending），转人工审核"]},
        {"node": "resolve_conflict", "branch": MAN_B,
         "error": f"commit {d[2][:8]} 冲突文件疑似二进制，无法自动解决"},
    ]
    payload = {"cycle_id": C28, "status": "FAILED",
               "scan_window": ["2026-08-27T22:00:00+08:00", "2026-08-28T22:00:00+08:00"],
               "detected_commits": detected, "decisions": decisions,
               "branch_results": branches, "action_required": action_required,
               "sources": [OK_SRC],
               "targets": [SUCC_B, PART_B, FAIL_B, MAN_B, ABANDON_B]}
    write_state(C28, payload)
    write_cycle(C28, "FAILED", "2026-08-28T00:00:01", "2026-08-28T00:06:40")
    write_patch(C28, SUCC_B, [d[0], d[1], d[2]])
    write_patch(C28, ABANDON_B, [d[3]])
    return payload


# --------------------------------------------------------------------------
# 历史周期
# --------------------------------------------------------------------------

def build_history_cycles():
    d = {i: _dc(i)["sha"] for i in range(5)}
    # C25 FAILED：单分支基线失败
    payload = {
        "cycle_id": C25, "status": "FAILED",
        "scan_window": ["2026-08-24T22:00:00+08:00", "2026-08-25T22:00:00+08:00"],
        "detected_commits": [], "decisions": {},
        "branch_results": {FAIL_B: branch_baseline_fail(C25, FAIL_B)},
        "action_required": [{"node": "baseline_build", "branch": FAIL_B,
                             "error": f"分支 {FAIL_B} 基线编译失败"}],
        "sources": [], "targets": [FAIL_B],
    }
    write_state(C25, payload)
    write_cycle(C25, "FAILED", "2026-08-25T00:00:01", "2026-08-25T00:02:00")
    # C26 PARTIAL：停批分支
    payload = {
        "cycle_id": C26, "status": "PARTIAL",
        "scan_window": ["2026-08-25T22:00:00+08:00", "2026-08-26T22:00:00+08:00"],
        "detected_commits": [dict(_dc(0)), dict(_dc(1))],
        "decisions": {d[0]: {PART_B: {"kind": "NeedSync", "evidence": [], "confidence": "high"}},
                      d[1]: {PART_B: {"kind": "NeedSync", "evidence": [], "confidence": "high"}}},
        "branch_results": {PART_B: branch_stop_at_build(C26, PART_B, [d[0]], d[1])},
        "action_required": [{"sha": d[1], "branch": PART_B, "kind": "ManualReview",
                             "cause": "fix_missing", "evidence": ["编译失败重试耗尽"]}],
        "sources": [OK_SRC], "targets": [PART_B],
    }
    write_state(C26, payload)
    write_cycle(C26, "PARTIAL", "2026-08-26T00:00:01", "2026-08-26T00:05:10")
    # C27 SUCCESS：单分支全成功
    payload = {
        "cycle_id": C27, "status": "SUCCESS",
        "scan_window": ["2026-08-26T22:00:00+08:00", "2026-08-27T22:00:00+08:00"],
        "detected_commits": [dict(_dc(0)), dict(_dc(1))],
        "decisions": {d[0]: {SUCC_B: {"kind": "NeedSync", "evidence": [], "confidence": "high"}},
                      d[1]: {SUCC_B: {"kind": "NeedSync", "evidence": [], "confidence": "high"}}},
        "branch_results": {SUCC_B: branch_success(C27, SUCC_B, [d[0], d[1]], models=[PROD])},
        "action_required": [], "sources": [OK_SRC], "targets": [SUCC_B],
    }
    write_state(C27, payload)
    write_cycle(C27, "SUCCESS", "2026-08-27T00:00:01", "2026-08-27T00:04:00")
    write_patch(C27, SUCC_B, [d[0], d[1]])


# --------------------------------------------------------------------------
# 手动 / 重跑 单分支周期
# --------------------------------------------------------------------------

def build_manual_cycles():
    d = {i: _dc(i)["sha"] for i in range(5)}
    def single(cid, status, detected, decisions, branch, action=None):
        write_state(cid, {
            "cycle_id": cid, "status": status, "scan_window": ["", ""],
            "detected_commits": detected, "decisions": decisions,
            "branch_results": {branch[0]: branch[1]}, "action_required": action or []})

    # M_OLD 历史手动同步（政企，成功）
    single(M_OLD, "SUCCESS", [dict(_dc(4))],
           {d[4]: {OK_TGT: {"kind": "NeedSync", "evidence": [], "confidence": "high"}}},
           (OK_TGT, branch_success_one(M_OLD, OK_TGT, d[4])))
    write_patch(M_OLD, OK_TGT, [d[4]])
    # M_FTTR_SYNC 历史 fttr sync（rerun 归并父，成功但过程有 冲突解决 + 编译修复）
    single(M_FTTR_SYNC, "SUCCESS", [dict(_dc(1))],
           {d[1]: {FTTR_TGT: {"kind": "NeedSync", "evidence": [], "confidence": "high"}}},
           (FTTR_TGT, branch_success_repaired(
               M_FTTR_SYNC, FTTR_TGT, d[1],
               conflict={"files": ["plat/lib/datapath/vlan.c"],
                         "agent_reason": "目标分支 vlan_ingress 已内联改写，取目标侧并保留新增 15 层截断逻辑",
                         "diff": ("--- a/plat/lib/datapath/vlan.c\n+++ b/plat/lib/datapath/vlan.c\n"
                                  "@@ -218,7 +218,9 @@ int vlan_ingress(struct sk_buff *skb)\n"
                                  "-\tif (unlikely(vlan_id > 4095)) return -EINVAL;\n"
                                  "+\tif (unlikely(vlan_id > 4095)) return -EINVAL;\n"
                                  "+\tif (unlikely(tag_bits > 15)) tag_bits = 15;\n")},
               fix_diff=("--- a/plat/lib/datapath/vlan.c\n+++ b/plat/lib/datapath/vlan.c\n"
                         "@@ -225,7 +225,7 @@ int vlan_ingress(struct sk_buff *skb)\n"
                         "-\treturn (ntohs(vlan_hdr->h_vlan_TCI) & 0xfff);\n"
                         "+\treturn tag_bits;\n"))))
    write_patch(M_FTTR_SYNC, FTTR_TGT, [d[1]])
    # R_FTTR_RERUN 历史 fttr rerun（成功 → 归并覆盖父 sync 结果）
    single(R_FTTR_RERUN, "SUCCESS", [dict(_dc(1))],
           {d[1]: {FTTR_TGT: {"kind": "NeedSync", "evidence": [], "confidence": "high"}}},
           (FTTR_TGT, branch_success_one(R_FTTR_RERUN, FTTR_TGT, d[1])))
    write_patch(R_FTTR_RERUN, FTTR_TGT, [d[1]])
    # M_TODAY 今日成功同步（政企，显示在工作台）
    single(M_TODAY, "SUCCESS", [dict(_dc(0)), dict(_dc(1))],
           {d[0]: {OK_TGT: {"kind": "NeedSync", "evidence": [], "confidence": "high"}},
            d[1]: {OK_TGT: {"kind": "NeedSync", "evidence": [], "confidence": "high"}}},
           (OK_TGT, branch_success(M_TODAY, OK_TGT, [d[0], d[1]])))
    write_patch(M_TODAY, OK_TGT, [d[0], d[1]])


# --------------------------------------------------------------------------
# 进行中周期 + 步骤进度（progress.jsonl，不写 state.json/cycle.json）
# --------------------------------------------------------------------------

def _p(cid, node, step, phase, status=None, ts=0.0, duration=None, target=None,
       sha=None, model=None, log_path=None):
    r = {"cycle_id": cid, "node": node, "step": step, "target": target, "sha": sha,
         "phase": phase, "status": status, "ts": ts}
    if duration is not None:
        r["duration_ms"] = duration
    if model:
        r["model"] = model
    if log_path:
        r["log_path"] = log_path
    return r


def _write_progress(cid, records):
    d = DEMO / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "progress.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8")


# 已完成周期（写 state.json）的步骤进度：写入脚本与引擎埋点对齐的时序记录，
# 让成功/停批/失败/待处理周期的任务详情也能渲染「处理过程」时间线。
def _baseline_ok_log(cid, target):
    lp = f"{cid}/build/{target}/baseline/5200.log"
    _log_write(lp, ["Building baseline 5200 ...", "Linking ok", "baseline build OK"])
    return lp


def _detect_pair(cid):
    return [
        _p(cid, "detect_commits", "代码迁出", "start", ts=0.2),
        _p(cid, "detect_commits", "代码迁出", "end", status="DETECTED",
           duration=1200, ts=0.5),
    ]


def _prepare_pair(cid, target):
    return [
        _p(cid, "prepare_worktree", "建立 worktree", "start", target=target, ts=1.0),
        _p(cid, "prepare_worktree", "建立 worktree", "end", status="PREPARED",
           duration=14000, ts=1.3, target=target),
    ]


def _baseline_pair(cid, target, status, ts=2.0, dur=60000):
    bl = f"{cid}/build/{target}/baseline/5200.log"
    if status == "BASELINE_OK":
        _log_write(bl, ["Building baseline 5200 ...", "Linking ok", "baseline build OK"])
    else:
        _log_write(bl, ["Configuring for RTL9617C ...", "checking for gcc ... ok",
                        "error: toolchain for target not found",
                        "make: *** No rule to make target 'clean'. Stop."])
    return [
        _p(cid, "baseline_build", "基线编译", "start", target=target, model=PROD,
           log_path=bl, ts=ts),
        _p(cid, "baseline_build", "基线编译", "end", status=status, duration=dur,
           ts=ts + 0.2, target=target, model=PROD, log_path=bl),
    ]


def _cp_pair(cid, target, sha, status, ts, dur=300):
    return [
        _p(cid, "cherry_pick", "cherry-pick", "start", target=target, sha=sha, ts=ts),
        _p(cid, "cherry_pick", "cherry-pick", "end", status=status, duration=dur,
           ts=ts + 0.2, target=target, sha=sha),
    ]


def _resolve_pair(cid, target, sha, status, ts, dur=12000, log_path=None):
    return [
        _p(cid, "resolve_conflict", "解决冲突", "start", target=target, sha=sha, ts=ts),
        _p(cid, "resolve_conflict", "解决冲突", "end", status=status, duration=dur,
           ts=ts + 0.2, target=target, sha=sha),
    ]


def _build_pair(cid, target, sha, status, ts, dur=40000, model=PROD, log_lines=None):
    lp = f"{cid}/build/{target}/{sha}/build.log"
    _log_write(lp, log_lines or ["Compiling ...", "Linking ok", "build OK"])
    return [
        _p(cid, "build", "编译", "start", target=target, sha=sha, model=model,
           log_path=lp, ts=ts),
        _p(cid, "build", "编译", "end", status=status, duration=dur, ts=ts + 0.2,
           target=target, sha=sha, model=model, log_path=lp),
    ]


def _fix_pair(cid, target, sha, status, ts, attempt, dur=45000, log_lines=None):
    lp = f"{cid}/build/{target}/{sha}/build.log"
    if log_lines is not None:
        _log_write(lp, log_lines)
    return [
        _p(cid, "fix_build", "修复重编译", "start", target=target, sha=sha, model=PROD,
           log_path=lp, ts=ts),
        _p(cid, "fix_build", "修复重编译", "end", status=status, duration=dur,
           ts=ts + 0.2, target=target, sha=sha, model=PROD, log_path=lp),
    ]


def _failfast_pair(cid, target, sha, ts, dur=400):
    return [
        _p(cid, "fail_fast", "失败关联判定", "start", target=target, sha=sha, ts=ts),
        _p(cid, "fail_fast", "失败关联判定", "end", status="FAILFAST_STOP",
           duration=dur, ts=ts + 0.2, target=target, sha=sha),
    ]


def _genpatch_pair(cid, target, sha, ts, dur=3000):
    return [
        _p(cid, "generate_patch", "生成 patch", "start", target=target, sha=sha, ts=ts),
        _p(cid, "generate_patch", "生成 patch", "end", status="PATCHED",
           duration=dur, ts=ts + 0.2, target=target, sha=sha),
    ]


def _patch_report_pair(cid, target, status, ts, sha=None, dur_patch=3000, dur_rep=1500):
    """单分支周期收尾：generate_patch + report（report 周期级，target=None）。"""
    return _genpatch_pair(cid, target, sha, ts, dur=dur_patch) + _report_pair(
        cid, status, ts + 0.3, dur=dur_rep)


# 引擎写不了日志的两处演示细节：停批 commit 的 build 日志 + 冲突解决失败原因。
_VLAN_FAIL_LOG = [
    "Compiling plat/lib/datapath/vlan.c ...",
    "  error: 'tag' undeclared (first use in this function)",
    "make[1]: *** [Makefile:421: vlan.o] Error 1",
    "make: *** [Makefile:89: all] Error 2", "ERROR: build failed",
]


def _report_pair(cid, status, ts, dur=1500):
    return [
        _p(cid, "report", "生成报告", "start", ts=ts),
        _p(cid, "report", "生成报告", "end", status=status, duration=dur, ts=ts + 0.2),
    ]


def _conflict_manual_body(cid, target, sha, model=PROD):
    """待处理(MANUAL)分支体：cherry-pick 冲突 → 解决失败 → 停批。

    纯分支体（不含周期级 detect/report），由调用方按周期显式加 detect 与收尾
    report，避免多分支周期合成单条流水时重复 detect/各自的 report 污染其他分支。
    """
    recs = _prepare_pair(cid, target) + _baseline_pair(cid, target, "BASELINE_OK")
    recs += _cp_pair(cid, target, sha, "CHERRY_PICK_CONFLICT", ts=2.5)
    # 冲突解决失败，引擎把原因写进 action_required/errors，投影不存——演示状态即可
    recs += _resolve_pair(cid, target, sha, "RESOLUTION_FAILED", ts=3.0)
    recs += _failfast_pair(cid, target, sha, ts=3.5)
    return recs


def _paused_body(cid, target, ok_shas, fail_sha):
    """停批(PARTIAL)分支体：ok_sha 通过，fail_sha 编译失败→修复 3 次→停批。

    纯分支体：以 fail_fast 结束（该分支不再 generate_patch / 无 report），
    detect 与 report 由周期级显式补。
    """
    recs = _prepare_pair(cid, target) + _baseline_pair(cid, target, "BASELINE_OK")
    recs += _cp_pair(cid, target, ok_shas[0], "CHERRY_PICK_OK", ts=2.5)
    recs += _build_pair(cid, target, ok_shas[0], "BUILD_OK", ts=2.9)
    recs += _cp_pair(cid, target, fail_sha, "CHERRY_PICK_OK", ts=3.4)
    recs += _build_pair(cid, target, fail_sha, "BUILD_FAILED", ts=3.8, log_lines=_VLAN_FAIL_LOG)
    recs += _fix_pair(cid, target, fail_sha, "BUILD_FAILED", ts=4.5, attempt=1)
    recs += _fix_pair(cid, target, fail_sha, "BUILD_FAILED", ts=5.3, attempt=2)
    recs += _fix_pair(cid, target, fail_sha, "BUILD_FAILED", ts=6.1, attempt=3)
    recs += _failfast_pair(cid, target, fail_sha, ts=6.9)
    return recs


def build_finalized_progress():
    """给已结束周期（写 state.json 的）补 progress.jsonl，让任务详情也能渲染
    「处理过程」。覆盖：当前 rich 周期全分支 + 历史成功/停批/基线失败。

    多分支周期（C28）的多个 target 顺序执行，progress 需累加进同一文件，
    不能各自整体覆盖——先各自生成片段，再按 target 顺序合并追加写。"""
    d = {i: _dc(i)["sha"] for i in range(5)}
    # 0) C28 当前 rich FAILED 周期：五分支顺序执行合成一份流水。
    #    流水 = detect(1 次) + 各分支体(纯分支) + 周期末单个 report；
    #    成功分支体各自以 generate_patch 收尾（引擎真实如此），停批/失败分支体
    #    到 fail_fast / baseline_failed 即止，不产 patch 也不发 report。
    cid = C28
    body: list[dict] = []
    #    SUCC_B（d0,d1 通过 + d2 已在目标，3 commit 全 build 5200/5200B 通过）
    recs = _prepare_pair(cid, SUCC_B) + _baseline_pair(cid, SUCC_B, "BASELINE_OK")
    for i, sha in enumerate((d[0], d[1], d[2])):
        base = 2.5 + i * 1.1
        recs += _cp_pair(cid, SUCC_B, sha, "CHERRY_PICK_OK", ts=base)
        recs += _build_pair(cid, SUCC_B, sha, "BUILD_OK", ts=base + 0.4, model=PROD)
        recs += _build_pair(cid, SUCC_B, sha, "BUILD_OK", ts=base + 0.8, model=PROD_B)
    recs += _genpatch_pair(cid, SUCC_B, d[2], ts=6.0)  # patch 记在最后一个 commit
    body += recs
    #    PART_B（停批：修复 3 次仍失败 → fail_fast）
    body += _paused_body(cid, PART_B, [d[0]], d[1])
    #    MAN_B（冲突解决失败 → fail_fast → 待人工）
    body += _conflict_manual_body(cid, MAN_B, d[2])
    #    ABANDON_B（已放弃的成功单 commit）
    tgt = ABANDON_B
    recs = _prepare_pair(cid, tgt) + _baseline_pair(cid, tgt, "BASELINE_OK")
    recs += _cp_pair(cid, tgt, d[3], "CHERRY_PICK_OK", ts=2.5)
    recs += _build_pair(cid, tgt, d[3], "BUILD_OK", ts=2.9)
    recs += _genpatch_pair(cid, tgt, d[3], ts=3.4)
    body += recs
    #    FAIL_B（基线失败，直接终）
    body += _prepare_pair(cid, FAIL_B) + _baseline_pair(cid, FAIL_B, "BASELINE_FAILED")
    #    周期收尾：单个 report（FAILED，因 C28 有停批/待处理）
    _write_progress(cid, _detect_pair(cid) + body + _report_pair(cid, "FAILED", ts=14.0))

    # 1) C27 历史成功周期：单分支 2 commit 全通过（展示成功详情也有过程）
    cid, tgt = C27, SUCC_B
    recs = _detect_pair(cid) + _prepare_pair(cid, tgt) + _baseline_pair(cid, tgt, "BASELINE_OK")
    recs += _cp_pair(cid, tgt, d[0], "CHERRY_PICK_OK", ts=2.5)
    recs += _build_pair(cid, tgt, d[0], "BUILD_OK", ts=2.9, model=PROD)
    recs += _build_pair(cid, tgt, d[0], "BUILD_OK", ts=3.4, model=PROD_B)
    recs += _cp_pair(cid, tgt, d[1], "CHERRY_PICK_OK", ts=4.0)
    recs += _build_pair(cid, tgt, d[1], "BUILD_OK", ts=4.4, model=PROD)
    recs += _build_pair(cid, tgt, d[1], "BUILD_OK", ts=4.9, model=PROD_B)
    recs += _patch_report_pair(cid, tgt, "SUCCESS", ts=5.5)
    _write_progress(cid, recs)
    # 2) M_FTTR_SYNC 历史手动 sync：成功，但过程 冲突已解决 + 编译失败→修复×2→通过
    #   （成功详情页应能看到这些中间处理，而非只见终态 OK）
    cid, tgt = M_FTTR_SYNC, FTTR_TGT
    recs = _prepare_pair(cid, tgt) + _baseline_pair(cid, tgt, "BASELINE_OK")
    recs += _cp_pair(cid, tgt, d[1], "CHERRY_PICK_CONFLICT", ts=2.5)
    recs += _resolve_pair(cid, tgt, d[1], "RESOLVED", ts=3.0)
    recs += _build_pair(cid, tgt, d[1], "BUILD_FAILED", ts=3.6,
                        log_lines=_VLAN_FAIL_LOG)
    recs += _fix_pair(cid, tgt, d[1], "BUILD_FAILED", ts=4.4, attempt=1)
    recs += _fix_pair(cid, tgt, d[1], "BUILD_OK", ts=5.2, attempt=2,
                      log_lines=["Compiling plat/lib/datapath/vlan.c ...",
                                 "Linking ok", "build OK"])
    recs += _build_pair(cid, tgt, d[1], "BUILD_OK", ts=6.0, model=PROD_B)
    recs += _patch_report_pair(cid, tgt, "SUCCESS", ts=6.6)
    _write_progress(cid, recs)
    # 3) C26 停批周期：commit0 通过，commit1 编译失败→修复 3 次失败→停批
    cid, tgt = C26, PART_B
    recs = _detect_pair(cid) + _prepare_pair(cid, tgt) + _baseline_pair(cid, tgt, "BASELINE_OK")
    recs += _cp_pair(cid, tgt, d[0], "CHERRY_PICK_OK", ts=2.5)
    recs += _build_pair(cid, tgt, d[0], "BUILD_OK", ts=2.9)
    recs += _cp_pair(cid, tgt, d[1], "CHERRY_PICK_OK", ts=3.4)
    recs += _build_pair(cid, tgt, d[1], "BUILD_FAILED", ts=3.8, log_lines=_VLAN_FAIL_LOG)
    recs += _fix_pair(cid, tgt, d[1], "BUILD_FAILED", ts=4.5, attempt=1)
    recs += _fix_pair(cid, tgt, d[1], "BUILD_FAILED", ts=5.3, attempt=2)
    recs += _fix_pair(cid, tgt, d[1], "BUILD_FAILED", ts=6.1, attempt=3)
    recs += _failfast_pair(cid, tgt, d[1], ts=6.9)
    recs += _patch_report_pair(cid, tgt, "PARTIAL", ts=7.2, sha=d[1])
    _write_progress(cid, recs)
    # 4) C25 失败周期：基线编译失败即整批停（无 commit，仅 report）
    cid, tgt = C25, FAIL_B
    recs = _detect_pair(cid) + _prepare_pair(cid, tgt) + _baseline_pair(cid, tgt, "BASELINE_FAILED")
    recs += _report_pair(cid, "FAILED", ts=2.8)
    _write_progress(cid, recs)


def build_running_tasks():
    """四个进行中任务，覆盖 基线全量编译 / cherry-pick / 解决冲突 / 修复重编译。"""
    d2 = _dc(1)["sha"]
    target2 = PART_B
    # 1) RUN_CHERRY：今日进行中 sync @ cherry-pick 时
    cid = RUN_CHERRY
    bl = f"{cid}/build/{OK_TGT}/baseline/5200.log"
    _log_write(bl, ["Building baseline 5200 ...", "Linking ok", "baseline build OK"])
    _write_progress(cid, [
        _p(cid, "prepare_worktree", "建立 worktree", "start", target=OK_TGT),
        _p(cid, "prepare_worktree", "建立 worktree", "end", status="PREPARED",
           duration=15000, ts=1.0, target=OK_TGT),
        _p(cid, "baseline_build", "基线编译", "start", target=OK_TGT, model=PROD, log_path=bl),
        _p(cid, "baseline_build", "基线编译", "end", status="BASELINE_OK", duration=61000,
           ts=2.0, target=OK_TGT, model=PROD, log_path=bl),
        _p(cid, "cherry_pick", "cherry-pick", "start", target=OK_TGT, sha=d2),
        # 未闭合 start → 进行中
    ])
    # 2) RUN_BASELINE：FAIL_B rerun @ 基线全量编译时（fresh 重建）
    cid = RUN_BASELINE
    _write_progress(cid, [
        _p(cid, "prepare_worktree", "建立 worktree", "start", target=FAIL_B),
        _p(cid, "prepare_worktree", "建立 worktree", "end", status="PREPARED",
           duration=18200, ts=1.0, target=FAIL_B),
        _p(cid, "baseline_build", "基线编译", "start", target=FAIL_B, model=PROD,
           log_path=f"{cid}/build/{FAIL_B}/baseline/5200.log"),
        # 进行中：全量编译
    ])
    _log_write(f"{cid}/build/{FAIL_B}/baseline/5200.log",
               ["Compiling target tree (clean full build) ...", "make[1]: Entering ..."])
    # 3) RUN_RESOLVE：MAN_B rerun @ 解决冲突时
    cid = RUN_RESOLVE
    bl = f"{cid}/build/{MAN_B}/baseline/5200.log"
    _log_write(bl, ["Building baseline 5200 ...", "Linking ok", "baseline build OK"])
    _write_progress(cid, [
        _p(cid, "prepare_worktree", "建立 worktree", "start", target=MAN_B),
        _p(cid, "prepare_worktree", "建立 worktree", "end", status="PREPARED",
           duration=12000, ts=1.0, target=MAN_B),
        _p(cid, "baseline_build", "基线编译", "start", target=MAN_B, model=PROD, log_path=bl),
        _p(cid, "baseline_build", "基线编译", "end", status="BASELINE_OK", duration=58000,
           ts=2.0, target=MAN_B, model=PROD, log_path=bl),
        _p(cid, "cherry_pick", "cherry-pick", "start", target=MAN_B, sha=d2),
        _p(cid, "cherry_pick", "cherry-pick", "end", status="CHERRY_PICK_CONFLICT",
           duration=300, ts=3.0, target=MAN_B, sha=d2),
        _p(cid, "resolve_conflict", "解决冲突", "start", target=MAN_B, sha=d2),
        # 进行中：智能体解决冲突
    ])
    # 4) RUN_FIX：PART_B rerun @ 修复重编译时
    cid = RUN_FIX
    bl = f"{cid}/build/{target2}/baseline/5200.log"
    buildlog = f"{cid}/build/{target2}/{d2}/build.log"
    _log_write(bl, ["Building baseline 5200 ...", "Linking ok", "baseline build OK"])
    _log_write(buildlog, ["Compiling plat/lib/datapath/vlan.c ...",
                          "  error: 'tag' undeclared", "make: Error 1"])
    _write_progress(cid, [
        _p(cid, "prepare_worktree", "建立 worktree", "start", target=target2),
        _p(cid, "prepare_worktree", "建立 worktree", "end", status="PREPARED",
           duration=11000, ts=1.0, target=target2),
        _p(cid, "baseline_build", "基线编译", "start", target=target2, model=PROD, log_path=bl),
        _p(cid, "baseline_build", "基线编译", "end", status="BASELINE_OK", duration=57000,
           ts=2.0, target=target2, model=PROD, log_path=bl),
        _p(cid, "cherry_pick", "cherry-pick", "start", target=target2, sha=d2),
        _p(cid, "cherry_pick", "cherry-pick", "end", status="CHERRY_PICK_OK", duration=220,
           ts=3.0, target=target2, sha=d2),
        _p(cid, "build", "编译", "start", target=target2, sha=d2, model=PROD, log_path=buildlog),
        _p(cid, "build", "编译", "end", status="BUILD_FAILED", duration=41000, ts=4.0,
           target=target2, sha=d2, model=PROD, log_path=buildlog),
        _p(cid, "fix_build", "修复重编译", "start", target=target2, sha=d2, model=PROD,
           log_path=buildlog),
        # 进行中：智能体修复后重编译
    ])


# --------------------------------------------------------------------------
# 任务 / 审计 / 会话 表
# --------------------------------------------------------------------------

def _utc(iso_local):
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(iso_local)
    return dt.astimezone(timezone.utc).isoformat()


def seed_db(db):
    d = {i: _dc(i)["sha"] for i in range(5)}
    ids: dict[str, int] = {}

    def ins(kind, user, target, state, *, cycle_id=None, src=None, shas=None,
            fresh=0, error=None, created=None, started=None, finished=None,
            commits=None, source="web"):
        created = created or "2026-08-28T12:00:00+08:00"
        cur = db.execute(
            "INSERT INTO tasks(kind,user,target,src,fresh,state,error,cycle_id,source,"
            "created_at,started_at,finished_at,shas,commits) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, user, target, src, fresh, state, error, cycle_id, source,
             _utc(created), _utc(started) if started else None,
             _utc(finished) if finished else None,
             json.dumps(shas) if shas else None, commits))
        return cur.lastrowid

    # 周期任务（cron）
    ins("cycle", "system", None, "failed", source="cron", cycle_id=C25,
        error="baseline_build 节点错误：分支基线编译失败",
        created="2026-08-25T00:00:01+08:00", started="2026-08-25T00:00:02+08:00",
        finished="2026-08-25T00:02:00+08:00")
    ins("cycle", "system", None, "failed", source="cron", cycle_id=C26,
        created="2026-08-26T00:00:01+08:00", started="2026-08-26T00:00:02+08:00",
        finished="2026-08-26T00:05:10+08:00")
    ins("cycle", "system", None, "succeeded", source="cron", cycle_id=C27,
        created="2026-08-27T00:00:01+08:00", started="2026-08-27T00:00:02+08:00",
        finished="2026-08-27T00:04:00+08:00")
    ins("cycle", "system", None, "failed", source="cron", cycle_id=C28,
        error="resolve_conflict 节点错误：commit 3a20e665 冲突无法自动解决",
        created="2026-08-28T00:00:01+08:00", started="2026-08-28T00:00:02+08:00",
        finished="2026-08-28T00:06:40+08:00")

    # 手动 sync / rerun —— 指向已落盘 state.json（历史）或 progress.jsonl（进行中）
    ids["m_old"] = ins("sync", "yangfu", OK_TGT, "succeeded", src=OK_SRC, cycle_id=M_OLD,
                       shas=[d[4]], commits=1,
                       created="2026-08-26T18:00:00+08:00",
                       started="2026-08-26T18:00:02+08:00", finished="2026-08-26T18:03:00+08:00")
    ids["fttr_sync"] = ins("sync", "yangfu", FTTR_TGT, "succeeded", src=OK_SRC,
                           cycle_id=M_FTTR_SYNC, shas=[d[1]], commits=1,
                           created="2026-08-27T09:00:00+08:00",
                           started="2026-08-27T09:00:03+08:00", finished="2026-08-27T09:05:00+08:00")
    ids["fttr_rerun"] = ins("rerun", "demo", FTTR_TGT, "succeeded", fresh=1,
                            cycle_id=R_FTTR_RERUN, commits=1,
                            created="2026-08-27T12:00:00+08:00",
                            started="2026-08-27T12:00:02+08:00", finished="2026-08-27T12:06:00+08:00")
    ids["today_ok"] = ins("sync", "yangfu", OK_TGT, "succeeded", src=OK_SRC,
                          cycle_id=M_TODAY, shas=[d[0], d[1]], commits=2,
                          created="2026-08-28T09:00:00+08:00",
                          started="2026-08-28T09:00:03+08:00", finished="2026-08-28T09:06:00+08:00")

    # 进行中任务（4 个，target 互不相同）
    ids["run_cherry"] = ins("sync", "demo", OK_TGT, "running", src=OK_SRC,
                            cycle_id=RUN_CHERRY, shas=[d[1]], commits=1,
                            created="2026-08-28T13:00:00+08:00",
                            started="2026-08-28T13:00:02+08:00", finished=None)
    ids["run_baseline"] = ins("rerun", "demo", FAIL_B, "running", fresh=1,
                              cycle_id=RUN_BASELINE, commits=1,
                              created="2026-08-28T14:00:00+08:00",
                              started="2026-08-28T14:00:02+08:00", finished=None)
    ids["run_resolve"] = ins("rerun", "demo", MAN_B, "running", fresh=1,
                             cycle_id=RUN_RESOLVE, commits=1,
                             created="2026-08-28T14:10:00+08:00",
                             started="2026-08-28T14:10:02+08:00", finished=None)
    ids["run_fix"] = ins("rerun", "demo", PART_B, "running", fresh=1,
                         cycle_id=RUN_FIX, commits=1,
                         created="2026-08-28T14:20:00+08:00",
                         started="2026-08-28T14:20:02+08:00", finished=None)

    # 审计
    def audit(ts, user, action, cycle_id, target=None, sha=None, detail=None, result=""):
        db.execute(
            "INSERT INTO audit_log(ts,user,action,cycle_id,target,sha,detail_json,result) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (ts, user, action, cycle_id, target, sha,
             json.dumps(detail, ensure_ascii=False) if detail else None, result))
    audit("2026-08-27T22:00:00+08:00", "system", "cycle_run", C25, result="failed")
    audit("2026-08-28T06:40:00+08:00", "system", "cycle_run", C28, result="failed")
    audit("2026-08-28T09:00:00+08:00", "yangfu", "sync_submit", M_TODAY, OK_TGT, d[0],
          {"commits": 2}, "ok")
    audit("2026-08-28T09:06:00+08:00", "yangfu", "push", M_TODAY, OK_TGT, None,
          {"commits": 2}, "succeeded")
    audit("2026-08-28T14:00:00+08:00", "demo", "rerun", RUN_BASELINE, FAIL_B, None,
          {"fresh": True}, "queued")

    # abandons：当前周期 ABANDON_B 整分支放弃（展示恢复入口）
    db.execute("INSERT INTO abandons(cycle_id,target,sha,user,created_at) "
               "VALUES(?,?,?,?,?)",
               (C28, ABANDON_B, None, "yangfu", _utc("2026-08-28T07:00:00+08:00")))

    # 会话（便于直接带 cookie 走查）
    db.execute("INSERT OR REPLACE INTO sessions(token,user,role,created_at,expires_at) "
               "VALUES('demo-token','demo','operator',?,?)",
               (_utc("2026-08-28T12:00:00+08:00"), _utc("2026-08-29T12:00:00+08:00")))
    db.commit()
    return ids


def write_task_logs(ids):
    def w(tid, lines):
        (DEMO / "tasks" / f"task-{tid}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    w(ids["run_cherry"], ["cherry-pick 3a20e665 ... 应用中（cherry-pick 阶段）…"])
    w(ids["run_baseline"], ["fresh 重建，正在全量编译基线（5200）…", "make[1]: Entering directory ..."])
    w(ids["run_resolve"], ["cherry-pick 3a20e665 ... CONFLICT", "冲突文件: vlan.c",
                           "正在调用智能体解决冲突 …（保留现场）"])
    w(ids["run_fix"], ["cherry-pick 3a20e665 ... OK", "编译 5200 ... FAILED",
                       "智能体修复后正在重编译 …（第 1 轮）"])
    w(ids["today_ok"], ["cherry-pick 1371936f ... OK", "cherry-pick 3a20e665 ... OK",
                        "build 5200/5200B ... OK", "patch 已生成"])


# --------------------------------------------------------------------------
def main():
    if DEMO.exists():
        shutil.rmtree(DEMO)
    DEMO.mkdir()
    (DEMO / "tasks").mkdir()

    build_current_cycle()
    build_history_cycles()
    build_manual_cycles()
    build_finalized_progress()
    build_running_tasks()

    import bsa_web.db as webdb
    db = webdb.init_db(DEMO / "platform.sqlite3")
    db.row_factory = sqlite3.Row
    ids = seed_db(db)
    db.close()
    write_task_logs(ids)

    print("demo data written to", DEMO)
    print("task ids:", {k: v for k, v in ids.items()})


if __name__ == "__main__":
    main()
