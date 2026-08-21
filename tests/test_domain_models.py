import pytest
from pydantic import ValidationError

from bsa.domain.models import (
    BranchResult,
    BuildOutcome,
    CherryPickResult,
    CommitInfo,
    CommitResult,
    Conclusion4,
    ConflictResolution,
    ErrorRecord,
    Report,
    SyncDecision,
)


def valid_commit() -> dict:
    return {
        "sha": "a1b2c3",
        "message": "fix: resolve memleak in x",
        "author": "dev@raisecom.com",
        "committed_at": "2026-08-19T22:00:05+08:00",
        "changed_files": ["dir/x.c"],
        "patch_text": "diff --git a/dir/x.c b/dir/x.c",
        "symbols": ["foo"],
        "patch_id": "abc123",
        "issue_ids": ["RCIOS-100"],
        "source_branch": "develop",
        "homologous_section": "release/fix",
    }


def valid_conclusion() -> dict:
    return {
        "kind": "NeedSync",
        "evidence": ["issue-id matches"],
        "confidence": "high",
    }


def valid_sync_decision() -> dict:
    return {
        "sha": "a1b2c3",
        "is_bug_fix": True,
        "reason": "machine:[BUG]",
        "recognition_source": "machine:[BUG]",
        "needs_agent": False,
    }


def valid_conflict_resolution() -> dict:
    return {
        "files": ["dir/x.c"],
        "diff": "--- a/dir/x.c\n+++ b/dir/x.c",
        "agent_reason": "keep both guards",
    }


def valid_build_outcome(model: str = "RTL9617C") -> dict:
    return {
        "model": model,
        "status": "OK",
        "log_path": "/tmp/build.log",
        "errors": [],
        "agent_attempts": 0,
        "fix_diff": None,
    }


def valid_commit_result() -> dict:
    return {
        "sha": "a1b2c3",
        "cherry_pick": "OK",
        "conflict_resolution": None,
        "build": {"RTL9617C": BuildOutcome(**valid_build_outcome())},
    }


def valid_branch_result() -> dict:
    return {
        "target_branch": "release/r2.1",
        "worktree_path": "/tmp/wt",
        "status": "SUCCESS",
        "commits": [CommitResult(**valid_commit_result())],
        "patch_path": "/tmp/p.patch",
        "stop_reason": None,
    }


def valid_report() -> dict:
    return {
        "cycle_id": "2026-08-19",
        "html_path": "/tmp/report.html",
        "summary": {"branches": 1, "commits": 2, "conclusions": {"NeedSync": 1}},
        "action_required": [{"target": "release/r2.1", "why": "conflict"}],
        "decisions_json_path": "/tmp/decisions.json",
    }


class TestCommitInfo:
    def test_construct_with_valid_data(self):
        c = CommitInfo(**valid_commit())
        assert c.sha == "a1b2c3"
        assert c.patch_id == "abc123"

    def test_patch_id_optional(self):
        data = valid_commit()
        data["patch_id"] = None
        c = CommitInfo(**data)
        assert c.patch_id is None

    def test_required_fields(self):
        for field in ["sha", "message", "author", "committed_at", "changed_files",
                      "patch_text", "symbols", "issue_ids", "source_branch",
                      "homologous_section"]:
            data = valid_commit()
            del data[field]
            with pytest.raises(ValidationError):
                CommitInfo(**data)


class TestSyncDecision:
    def test_construct_with_valid_data(self):
        d = SyncDecision(**valid_sync_decision())
        assert d.recognition_source == "machine:[BUG]"
        assert d.needs_agent is False

    def test_reason_optional(self):
        data = valid_sync_decision()
        data["reason"] = None
        assert SyncDecision(**data).reason is None

    @pytest.mark.parametrize("risk", ["low", "medium", "high"])
    def test_valid_risk(self, risk):
        data = valid_sync_decision()
        data["risk"] = risk
        assert SyncDecision(**data).risk == risk

    def test_risk_optional_defaults_none(self):
        assert SyncDecision(**valid_sync_decision()).risk is None

    def test_invalid_risk_rejected(self):
        data = valid_sync_decision()
        data["risk"] = "CRITICAL"
        with pytest.raises(ValidationError):
            SyncDecision(**data)

    def test_required_fields(self):
        for field in ["sha", "is_bug_fix", "recognition_source", "needs_agent"]:
            data = valid_sync_decision()
            del data[field]
            with pytest.raises(ValidationError):
                SyncDecision(**data)


class TestConclusion4:
    @pytest.mark.parametrize("kind", ["NeedSync", "AlreadyIncluded", "ManualReview", "OutOfScope"])
    def test_valid_kinds(self, kind):
        data = valid_conclusion()
        data["kind"] = kind
        assert Conclusion4(**data).kind == kind

    @pytest.mark.parametrize("kind", ["need-sync", "NEED_SYNC", "unknown", 42])
    def test_invalid_kinds_rejected(self, kind):
        data = valid_conclusion()
        data["kind"] = kind
        with pytest.raises(ValidationError):
            Conclusion4(**data)

    @pytest.mark.parametrize("confidence", ["high", "medium", "low"])
    def test_valid_confidences(self, confidence):
        data = valid_conclusion()
        data["confidence"] = confidence
        assert Conclusion4(**data).confidence == confidence

    def test_invalid_confidence_rejected(self):
        data = valid_conclusion()
        data["confidence"] = "HIGH"
        with pytest.raises(ValidationError):
            Conclusion4(**data)

    def test_required_fields(self):
        for field in ["kind", "evidence", "confidence"]:
            data = valid_conclusion()
            del data[field]
            with pytest.raises(ValidationError):
                Conclusion4(**data)


class TestConflictResolution:
    def test_construct_with_valid_data(self):
        r = ConflictResolution(**valid_conflict_resolution())
        assert r.files == ["dir/x.c"]
        assert r.agent_reason

    def test_required_fields(self):
        for field in ["files", "diff", "agent_reason"]:
            data = valid_conflict_resolution()
            del data[field]
            with pytest.raises(ValidationError):
                ConflictResolution(**data)


class TestCherryPickResult:
    @pytest.mark.parametrize("status", ["OK", "CONFLICT", "EMPTY", "FAILED"])
    def test_valid_statuses(self, status):
        r = CherryPickResult(status=status)
        assert r.status == status

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            CherryPickResult(status="CONFLICTS")

    def test_conflict_files_default_empty(self):
        r = CherryPickResult(status="CONFLICT")
        assert r.conflict_files == []

    def test_required_status(self):
        with pytest.raises(ValidationError):
            CherryPickResult()


class TestBuildOutcome:
    @pytest.mark.parametrize("status", ["OK", "FAILED", "SKIPPED"])
    def test_valid_statuses(self, status):
        data = valid_build_outcome()
        data["status"] = status
        assert BuildOutcome(**data).status == status

    def test_invalid_status_rejected(self):
        data = valid_build_outcome()
        data["status"] = "PASSED"
        with pytest.raises(ValidationError):
            BuildOutcome(**data)

    def test_required_fields(self):
        for field in ["model", "status", "errors", "agent_attempts"]:
            data = valid_build_outcome()
            del data[field]
            with pytest.raises(ValidationError):
                BuildOutcome(**data)


class TestCommitResult:
    def test_construct_with_valid_data(self):
        r = CommitResult(**valid_commit_result())
        assert r.build["RTL9617C"].status == "OK"

    @pytest.mark.parametrize("status", ["OK", "CONFLICT", "FAILED", "EMPTY"])
    def test_valid_cherry_pick_statuses(self, status):
        data = valid_commit_result()
        data["cherry_pick"] = status
        assert CommitResult(**data).cherry_pick == status

    def test_invalid_cherry_pick_rejected(self):
        data = valid_commit_result()
        data["cherry_pick"] = "DONE"
        with pytest.raises(ValidationError):
            CommitResult(**data)

    def test_conflict_resolution_optional(self):
        r = CommitResult(**valid_commit_result())
        assert r.conflict_resolution is None

    def test_required_fields(self):
        for field in ["sha", "cherry_pick", "build"]:
            data = valid_commit_result()
            del data[field]
            with pytest.raises(ValidationError):
                CommitResult(**data)


class TestBranchResult:
    def test_construct_with_valid_data(self):
        r = BranchResult(**valid_branch_result())
        assert r.commits[0].sha == "a1b2c3"

    @pytest.mark.parametrize("status", ["SUCCESS", "PARTIAL", "FAILED", "MANUAL"])
    def test_valid_statuses(self, status):
        data = valid_branch_result()
        data["status"] = status
        assert BranchResult(**data).status == status

    def test_invalid_status_rejected(self):
        data = valid_branch_result()
        data["status"] = "OK"
        with pytest.raises(ValidationError):
            BranchResult(**data)

    def test_required_fields(self):
        for field in ["target_branch", "worktree_path", "status", "commits"]:
            data = valid_branch_result()
            del data[field]
            with pytest.raises(ValidationError):
                BranchResult(**data)


class TestErrorRecord:
    def test_construct_with_valid_data(self):
        r = ErrorRecord(node="detect_commits", error="boom", ts="2026-08-19T22:00:00+08:00")
        assert r.node == "detect_commits"

    def test_required_fields(self):
        for field in ["node", "error", "ts"]:
            data = {"node": "n", "error": "e", "ts": "t"}
            del data[field]
            with pytest.raises(ValidationError):
                ErrorRecord(**data)


class TestReport:
    def test_construct_with_valid_data(self):
        r = Report(**valid_report())
        assert r.cycle_id == "2026-08-19"
        assert r.summary["branches"] == 1

    def test_round_trip_json(self):
        r = Report(**valid_report())
        restored = Report.model_validate_json(r.model_dump_json())
        assert restored == r
        assert restored.html_path == r.html_path
        assert restored.decisions_json_path == r.decisions_json_path
        assert restored.action_required == r.action_required

    def test_round_trip_dump(self):
        r = Report(**valid_report())
        restored = Report.model_validate(r.model_dump())
        assert restored == r

    def test_required_fields(self):
        for field in ["cycle_id", "html_path", "summary", "action_required", "decisions_json_path"]:
            data = valid_report()
            del data[field]
            with pytest.raises(ValidationError):
                Report(**data)
