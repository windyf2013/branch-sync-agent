"""bsa_web.branches：从 branch.md 提取 RCIOS 仓库分支列表。"""

from bsa_web.branches import rcios_branch_names

_SAMPLE = """\
# 所有待审核分支
## 1 RCIOS代码库
- 路径：rcios

### 1.1 组网产品分支
- br_v4.33_5200_CU_develop_20260518
- br_v4.33_develop_20260702

### 1.2 FTTR-B产品分支
- br_v4.34_develop_20260130

## 2 其他代码库
- 路径：other_repo

### 2.1 其他产品
- br_other_develop_20260101
"""


class TestRciosBranchNames:
    def test_returns_only_rcios_repo_branches(self, tmp_path):
        p = tmp_path / "branch.md"
        p.write_text(_SAMPLE, encoding="utf-8")
        names = rcios_branch_names(p)
        assert names == [
            "br_v4.33_5200_CU_develop_20260518",
            "br_v4.33_develop_20260702",
            "br_v4.34_develop_20260130",
        ]
        assert "br_other_develop_20260101" not in names

    def test_missing_file_returns_empty(self, tmp_path):
        assert rcios_branch_names(tmp_path / "nope.md") == []

    def test_empty_text_returns_empty(self, tmp_path):
        p = tmp_path / "branch.md"
        p.write_text("", encoding="utf-8")
        assert rcios_branch_names(p) == []

    def test_case_insensitive_path_marker(self, tmp_path):
        p = tmp_path / "branch.md"
        p.write_text("## 1 RCIOS\n- 路径: RCIOS\n\n### 1.1\n- br_a_develop\n", encoding="utf-8")
        assert rcios_branch_names(p) == ["br_a_develop"]
