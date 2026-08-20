"""Shared path bootstrap for branch-maintenance package tests."""

from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = PACKAGE_ROOT / "scripts"
# aiskill: common/packages/branch-maintenance -> common/scripts (bug_stale_alert)
COMMON_SCRIPTS = PACKAGE_ROOT.parents[1] / "scripts"

for path in (SCRIPTS_DIR, COMMON_SCRIPTS, TESTS_DIR):
    text = str(path)
    if path.is_dir() and text not in sys.path:
        sys.path.insert(0, text)
