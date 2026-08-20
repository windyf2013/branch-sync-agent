from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from branch_maintenance.classify import (
    PENDING_AGENT_SOURCE,
    classify_commit,
    lookup_agent_judgment,
)


def test_classify_machine_readable_bug():
    result = classify_commit(
        "[BUG] CQ99999 Fix dhcp lease timeout",
        ["plat/dhcp/dhcp.c"],
        ["dhcp_lease_timeout"],
        "+if (NULL == cfg) return -1;",
    )
    assert result.is_bug_fix is True
    assert result.recognition_source == "machine:[BUG]"
    assert result.needs_agent is False


def test_classify_no_marker_pending_agent():
    result = classify_commit(
        "raisecom-equipment.yang add uuid",
        ["plat/lib/sys/ios_syslib.c"],
        ["ios_get_device_uuid"],
        "+int ios_get_device_uuid(char *buf) { return 0; }\n",
        sha="b3e31641cdcfabc",
    )
    assert result.is_bug_fix is False
    assert result.recognition_source == PENDING_AGENT_SOURCE
    assert result.needs_agent is True


def test_classify_agent_judgment_bug_fix():
    result = classify_commit(
        "update something",
        ["a.c"],
        [],
        "+change\n",
        sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        agent_judgments={
            "deadbeef": {
                "is_bug_fix": True,
                "reason": "Restores missing null check.",
            }
        },
    )
    assert result.is_bug_fix is True
    assert result.recognition_source == "agent:bug-fix"
    assert "null" in (result.reason or "").lower()


def test_classify_agent_judgment_not_bug_fix():
    result = classify_commit(
        "add uuid",
        ["a.c"],
        [],
        "+change\n",
        sha="b3e31641cdcfabc",
        agent_judgments={
            "b3e31641cdcf": {"is_bug_fix": False, "reason": "Feature add."}
        },
    )
    assert result.is_bug_fix is False
    assert result.recognition_source == "agent:not-bug-fix"


def test_classify_merge_no_files_not_pending():
    result = classify_commit("Merge branch x", [], [], "", sha="abc1234567890")
    assert result.is_bug_fix is False
    assert result.needs_agent is False
    assert result.recognition_source == "not-included"


def test_lookup_agent_judgment_prefix():
    judgments = {"abcdef1": {"is_bug_fix": True, "reason": "x"}}
    assert lookup_agent_judgment("abcdef1234567890", judgments)["is_bug_fix"] is True
