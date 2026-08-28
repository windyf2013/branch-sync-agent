from __future__ import annotations


def _branch(builds: dict) -> dict:
    return {"commits": [{"sha": "a1", "build": builds}]}


def test_enrich_sets_model_label_and_ok_has_no_log_preview(tmp_path):
    from bsa_web.build_labels import enrich_build_outcomes

    fail_log = tmp_path / "b.log"
    fail_log.write_text("line1\nline2\n", encoding="utf-8")
    branch = _branch(
        {
            "2600m": {
                "status": "OK",
                "agent_attempts": 0,
                "log_path": "/logs/ok.log",
                "errors": [],
            },
            # 未配置进 build_rules.yaml 的旧格式型号，用于验证推断兜底
            "2600_CMCC": {
                "status": "FAILED",
                "agent_attempts": 3,
                "log_path": str(fail_log),
                "errors": ["compile error"],
            },
        }
    )

    enrich_build_outcomes(branch, str(tmp_path))

    ok = branch["commits"][0]["build"]["2600m"]
    fail = branch["commits"][0]["build"]["2600_CMCC"]
    # 显式配置的型号 → build_rules.yaml 的 script；未配置的型号 → 旧格式推断。
    assert ok["model_label"] == "RTL9617C_build_ci.sh 2600m"
    assert ok["log_preview"] is None  # OK 结果不拖全量日志
    assert fail["model_label"] == "RTL9617C_build_cmcc.sh 2600"
    assert fail["log_preview"] is not None  # FAILED 才读尾部日志


def test_enrich_legacy_model_uses_legacy_script_label(tmp_path):
    from bsa_web.build_labels import enrich_build_outcomes

    branch = _branch(
        {"2600_CMCC": {"status": "OK", "agent_attempts": 0, "log_path": None, "errors": []}}
    )

    enrich_build_outcomes(branch, str(tmp_path))

    assert branch["commits"][0]["build"]["2600_CMCC"]["model_label"] == (
        "RTL9617C_build_cmcc.sh 2600"
    )
