"""HTML report generator — fully self-contained, no external dependencies.

Produces data/reports/report_{YYYY-MM-DD_HHMM}.html after each full run.
Includes: per-company record tables, status colour badges, change highlights.
Works offline — no CDN required.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from jinja2 import Environment, BaseLoader
from loguru import logger

from .differ import DiffResult
from .scraper import decode_status

_ROOT = Path(__file__).parent.parent
_REPORTS_DIR = _ROOT / "data" / "reports"

# Status → (background, text-color)
# Labels verified against live DOM on 2026-05-19
_STATUS_STYLE: dict[str, tuple[str, str]] = {
    "待审核":   ("#0d6efd", "#fff"),
    "待补正":   ("#ffc107", "#000"),
    "公示中":   ("#0dcaf0", "#000"),
    "登记成功": ("#198754", "#fff"),
    "主动撤回": ("#6c757d", "#fff"),
    "视为撤回": ("#495057", "#fff"),
    "不予登记": ("#dc3545", "#fff"),
    "已消失":   ("#dc3545", "#fff"),
}
_DEFAULT_STYLE = ("#6c757d", "#fff")


def _badge_style(status_label: str) -> str:
    bg, fg = _STATUS_STYLE.get(status_label, _DEFAULT_STYLE)
    return (
        f"background:{bg};color:{fg};padding:2px 8px;"
        "border-radius:4px;font-size:0.78em;white-space:nowrap;"
        "display:inline-block;font-weight:500;"
    )


_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>数知产权状态报表 {{ report_time }}</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body {
      font-family: "PingFang SC","Microsoft YaHei","Hiragino Sans GB",
                   "Noto Sans CJK SC",sans-serif;
      font-size: 14px; color: #212529; background: #f5f5f5;
      margin: 0; padding: 16px;
    }
    h4  { margin: 0 0 4px; font-size: 1.1rem; }
    p   { margin: 0 0 16px; color: #6c757d; font-size: 0.9em; }
    a   { color: #0d6efd; text-decoration: none; }
    a:hover { text-decoration: underline; }

    /* card */
    .card {
      background: #fff; border: 1px solid #dee2e6;
      border-radius: 6px; margin-bottom: 20px;
      box-shadow: 0 1px 3px rgba(0,0,0,.08);
      overflow: hidden;
    }
    .card-header {
      background: #f8f9fa; border-bottom: 1px solid #dee2e6;
      padding: 8px 14px; font-weight: 600;
      display: flex; justify-content: space-between; align-items: center;
    }
    .card-header .sub { font-weight: 400; color: #6c757d; font-size: 0.85em; }

    /* table */
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 6px 10px; border: 1px solid #dee2e6; vertical-align: middle; }
    th { background: #f8f9fa; font-weight: 600; white-space: nowrap; }
    tr:hover td { background: #f0f4ff; }
    .num  { text-align: center; }
    .mono { font-family: "SFMono-Regular",Consolas,"Liberation Mono",monospace;
            font-size: 0.85em; }
    .sm   { font-size: 0.85em; }
    .nowrap { white-space: nowrap; }
    .muted  { color: #6c757d; font-style: italic; }

    /* change highlight rows */
    .row-changed { background: #fff8e1 !important; }
    .row-added   { background: #e8f5e9 !important; }
    .row-removed { background: #fce4ec !important; }

    /* overview text colors */
    .c-success { color: #198754; font-weight: 600; }
    .c-primary  { color: #0d6efd; }
    .c-warning  { color: #856404; }
    .c-danger   { color: #dc3545; font-weight: 600; }

    /* change summary chips */
    .chip-added   { color: #198754; font-weight: 600; }
    .chip-changed { color: #856404; font-weight: 600; margin: 0 4px; }
    .chip-removed { color: #dc3545; font-weight: 600; }

    /* error box */
    .err-box {
      margin: 10px; padding: 8px 12px;
      background: #fff3f3; border: 1px solid #f5c6cb;
      border-radius: 4px; color: #721c24;
    }
    .empty { padding: 12px; color: #6c757d; font-style: italic; }
  </style>
</head>
<body>

<h4>浙江省数据知识产权登记平台 — 状态报表</h4>
<p>生成时间：{{ report_time }}（北京时间）</p>

<!-- ── Overview ─────────────────────────────────────────────────── -->
<div class="card">
  <div class="card-header">汇总</div>
  <table>
    <thead><tr>
      <th>公司</th>
      <th class="num">总条数</th>
      <th class="num">登记成功</th>
      <th class="num">待审核</th>
      <th class="num">待补正</th>
      <th class="num">不予登记</th>
      <th class="num">其他</th>
      <th class="num">本次变化</th>
    </tr></thead>
    <tbody>
    {% for item in companies %}
    <tr>
      <td><a href="#company-{{ loop.index }}">{{ item.company }}</a></td>
      <td class="num">{{ item.total }}</td>
      <td class="num c-success">{{ item.registered }}</td>
      <td class="num c-primary">{{ item.pending }}</td>
      <td class="num c-warning">{{ item.correction }}</td>
      <td class="num c-danger">{{ item.rejected }}</td>
      <td class="num">{{ item.other }}</td>
      <td class="num sm">
        {% if item.has_changes %}
          {% if item.added %}<span class="chip-added">+{{ item.added }}新增</span>{% endif %}
          {% if item.changed %}<span class="chip-changed">~{{ item.changed }}变更</span>{% endif %}
          {% if item.removed %}<span class="chip-removed">-{{ item.removed }}消失</span>{% endif %}
        {% else %}
          <span class="muted">无变化</span>
        {% endif %}
      </td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
</div>

<!-- ── Per-company tables ──────────────────────────────────────── -->
{% for item in companies %}
<div class="card" id="company-{{ loop.index }}">
  <div class="card-header">
    <span>{{ item.company }}</span>
    <span class="sub">{{ item.total }} 条记录</span>
  </div>
  {% if item.error %}
    <div class="err-box">抓取失败：{{ item.error }}</div>
  {% elif not item.records %}
    <div class="empty">暂无登记记录</div>
  {% else %}
  <table>
    <thead><tr>
      <th>登记编号</th>
      <th>名称</th>
      <th>所属行业</th>
      <th>数据类型</th>
      <th>申请时间</th>
      <th>审核状态</th>
      <th>变化</th>
    </tr></thead>
    <tbody>
    {% for r in item.records %}
    <tr class="{{ r.row_class }}">
      <td class="mono">{{ r.reg_no }}</td>
      <td>{{ r.name }}</td>
      <td class="sm">{{ r.industry }}</td>
      <td class="sm">{{ r.data_type }}</td>
      <td class="sm nowrap">{{ r.apply_time }}</td>
      <td><span style="{{ r.badge_style }}">{{ r.status_label }}</span></td>
      <td class="sm">{{ r.change_note }}</td>
    </tr>
    {% endfor %}
    </tbody>
  </table>
  {% endif %}
</div>
{% endfor %}

</body>
</html>""".strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_report(
    run_results: list[dict],
    diffs: dict[str, DiffResult],
    report_time: Optional[datetime] = None,
) -> Path:
    """Render a self-contained HTML report and write it to data/reports/.

    Args:
        run_results: List of per-account result dicts from main.run_all().
        diffs:       {company: DiffResult} map built during the same run.
        report_time: Timestamp to show (defaults to now).

    Returns:
        Path to the written HTML file.
    """
    if report_time is None:
        report_time = datetime.now(timezone.utc)

    cst = timezone(timedelta(hours=8))
    report_time_cst = report_time.astimezone(cst)
    ts_str = report_time_cst.strftime("%Y-%m-%d %H:%M")
    filename = f"report_{report_time_cst.strftime('%Y-%m-%d_%H%M')}.html"

    companies = []
    for res in run_results:
        company = res["company"]
        records = res.get("records", [])
        error = res.get("error")
        diff = diffs.get(company)

        added_nos   = {r["dataRegNo"] for r in diff.added}   if diff else set()
        changed_nos = {c.reg_no: c   for c in diff.changed}  if diff else {}

        rendered_records = []
        for r in records:
            no           = r.get("dataRegNo", "")
            status_label = decode_status(r.get("dataRegAuditStatus"))

            if no in added_nos:
                row_class   = "row-added"
                change_note = "🆕 新增"
            elif no in changed_nos:
                row_class   = "row-changed"
                c           = changed_nos[no]
                change_note = f"{c.old_status} → {c.new_status}"
            else:
                row_class   = ""
                change_note = ""

            rendered_records.append({
                "reg_no":      no,
                "name":        r.get("dataRegName", ""),
                "industry":    r.get("dataRegIndustry", ""),
                "data_type":   r.get("dataRegDataType", ""),
                "apply_time":  _fmt_ts(r.get("dataRegApplyTime")),
                "status_label": status_label,
                "badge_style":  _badge_style(status_label),
                "row_class":   row_class,
                "change_note": change_note,
            })

        # Removed records shown at bottom in red
        for r in (diff.removed if diff else []):
            rendered_records.append({
                "reg_no":      r.get("dataRegNo", ""),
                "name":        r.get("dataRegName", ""),
                "industry":    r.get("dataRegIndustry", ""),
                "data_type":   r.get("dataRegDataType", ""),
                "apply_time":  _fmt_ts(r.get("dataRegApplyTime")),
                "status_label": "已消失",
                "badge_style":  _badge_style("已消失"),
                "row_class":   "row-removed",
                "change_note": "⚠ 记录消失",
            })

        # Overview counts
        from collections import Counter
        status_counts = Counter(
            decode_status(r.get("dataRegAuditStatus")) for r in records
        )
        companies.append({
            "company":    company,
            "total":      len(records),
            "registered": status_counts.get("登记成功", 0),
            "pending":    status_counts.get("待审核", 0),
            "correction": status_counts.get("待补正", 0),
            "rejected":   status_counts.get("不予登记", 0),
            "other":      sum(
                v for k, v in status_counts.items()
                if k not in ("登记成功", "待审核", "待补正", "不予登记")
            ),
            "has_changes": diff.has_changes if diff else False,
            "added":       len(diff.added)   if diff else 0,
            "changed":     len(diff.changed) if diff else 0,
            "removed":     len(diff.removed) if diff else 0,
            "records":     rendered_records,
            "error":       error,
        })

    env = Environment(loader=BaseLoader(), autoescape=True)
    tmpl = env.from_string(_TEMPLATE)
    html = tmpl.render(report_time=ts_str, companies=companies)

    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _REPORTS_DIR / filename
    out_path.write_text(html, encoding="utf-8")
    logger.info(f"HTML report → {out_path.name}")
    return out_path


def _fmt_ts(ts: object) -> str:
    """Convert Unix-ms timestamp to a readable date string."""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(
            int(ts) / 1000, tz=timezone(timedelta(hours=8))
        ).strftime("%Y-%m-%d")
    except Exception:
        return str(ts)
