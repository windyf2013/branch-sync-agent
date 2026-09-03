"""三级角色常量与层级判据：admin / operator / viewer。

admin 是 operator 的超集（管理员可做操作员的一切业务操作）；viewer 只读。
未知角色字符串按 viewer 宽松回退（不报错），保证既有 env 部署零改动兼容。
"""

ADMIN = "admin"
OPERATOR = "operator"
VIEWER = "viewer"

ALL_ROLES = frozenset({ADMIN, OPERATOR, VIEWER})

ROLE_LABELS_ZH = {ADMIN: "管理员", OPERATOR: "操作者", VIEWER: "查看者"}


def normalize_role(role: str | None) -> str:
    """把任意角色字符串收敛到三级之一；未识别值按 viewer 兜底（宽松回退）。"""
    if role is not None:
        r = role.strip().lower()
        if r == ADMIN:
            return ADMIN
        if r == OPERATOR:
            return OPERATOR
    return VIEWER


def role_label_zh(role: str | None) -> str:
    """角色 → 中文标签（用于模板徽章/设置页），未识别兜底「查看者」。"""
    return ROLE_LABELS_ZH[normalize_role(role)]


def is_operator_role(role: str | None) -> bool:
    """是否具备操作者权限：operator 或 admin 均视为操作者（admin 超集）。"""
    return normalize_role(role) in (OPERATOR, ADMIN)


def is_admin_role(role: str | None) -> bool:
    """是否管理员。"""
    return normalize_role(role) == ADMIN
