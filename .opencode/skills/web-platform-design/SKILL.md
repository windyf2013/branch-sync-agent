---
name: web-platform-design
description: Use when styling, reworking, or redesigning the BSA web platform UI (templates, style.css, pages). Apply the design system from the reference branch-maintenance report (brand masthead, panel sections, KPI cards, status badges, toolbar/filters) — never default to generic bare styling. Trigger keywords: 平台web、整改平台、设计风格、太丑、按参考报告风格、UI 改版、样式、页面布局、workbench 页面。
---

# Web 平台设计系统（BSA 分支维护工作台）

## 触发

涉及 BSA 平台 Web UI 的样式、布局、改版、新页面时，必须遵循本设计系统，参考权威样式源：

`/home/sam/rc_projects/aiskill/aiskill/common/packages/branch-maintenance/scripts/branch_maintenance/templates/branch_maintenance_report.html`

（实现样式的权威落点：`src/bsa_web/static/style.css` + `src/bsa_web/templates/*.html`。先读该文件获取当前 CSS 变量与组件，再按本 skill 约束改。）

## 设计令牌（与参考报告一致）

```css
--ink:#0f172a  --ink-2:#1e293b  --muted:#64748b
--line:#e2e8f0  --line-strong:#cbd5e1
--surface:#fff  --canvas:#f1f5f9  --canvas-2:#e8eef5
--brand:#0b3d5c  --brand-2:#125a82  --brand-soft:#e8f1f7  --accent:#0e7490
--need:#b91c1c(bg#fef2f2/bd#fecaca)
--review:#a16207(bg#fffbeb/bd#fde68a)
--ok:#166534(bg#f0fdf4/bd#bbf7d0)
--scope:#475569(bg#f8fafc)  --pending:#0369a1(bg#f0f9ff/bd#bae6fd)
--shadow:0 1px 2px rgba(15,23,42,.06),0 8px 24px rgba(15,23,42,.04)
--radius:10px  --max:1180px
字体：sans = "Segoe UI","PingFang SC","Microsoft YaHei"；mono = "Cascadia Code",Consolas
背景：radial-gradient(#dbeafe) + linear-gradient(canvas-2→canvas)
```

## 页面骨架（必须的结构）

- `base.html`：品牌 masthead（渐变 `linear-gradient(135deg, brand→brand-2→#0e7490)`，含 logo 方块 + eyebrow + h1 + 导航 + user-chip + 登出）+ `.page`（max-width 1180 居中）+ `.footer`。
- 每个区块用 `section.panel`：`.panel-head`（渐变底 #fbfcfe→#f8fafc，h2 标题 + `.panel-note` 说明）+ `.panel-body`。
- 顶部可加 sticky 导航（toc 风格）。

## 组件约束

- **KPI 卡**：`.kpi`（左侧 3px 色条，`::before`；`kpi-need/review/pending/ok/neutral` 色条与数值同色）。
- **状态徽章**：`.badge.running/.queued/.pending`（蓝）、`.failed/.need`（红）、`.success/.ok`（绿）、`.partial/.review`（琥珀）、`.manual/.scope`（灰蓝）、`.skip/.abandoned`（灰）。
- **表格**：`.table-wrap` 带边框圆角；th 深底 #f1f5f9；斑马行 + hover。
- **按钮**：`.btn` 描边圆角；`button[type=submit]`/`.btn-primary` 品牌底白字；`.btn-outline` brand-soft 底。
- **表单**：label 加粗、input/select 圆角 + 聚焦 `outline rgba(18,90,130,.25)`。
- **任务面板**：`.task-panel` 卡片（border+圆角+hover 阴影），徽章+链接+发起人/来源。
- **登录页**：`.login-body` 品牌渐变底，`.login-card` 白卡（阴影 + 品牌 logo + eyebrow/h1 + 字段 + 主色登录按钮）。
- **空状态/错误**：`.empty-state`（虚线框居中）、`.op-error`（红底）。

## 硬性要求

- 每次 UI 改动后跑 `uv run pytest tests/web/` 确认模板渲染不破 + `uv run ruff check src/`。
- 新页面必须复用现有面板/徽章/表格组件，禁止另起一套风格或内联裸样式。
- 不确定某组件样式时，回读参考报告文件对应段落，不要猜。
