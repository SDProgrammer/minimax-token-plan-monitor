#!/usr/bin/env python3
"""report_token_plan.py - 从 PostgreSQL 读快照,生成周报 Markdown (5h 维度版).

报告范围(默认):上周一 00:00 (北京时间) ~ 上周日 24:00.
输出: ~/Documents/MiniMax/token-plan/reports/report_YYYY-Www.md

口径说明:Token Plan 套餐"周限额"对无限档(如 Plus)是"无限制",所以按 weekly_usage_count
        算周 diff 永远没意义。本报告以"5h 窗口 × 模型分类"为主维度.

Usage:
  report_token_plan.py                 # 默认生成上周一周报
  report_token_plan.py --now <iso8601> # 用指定时间算"上一周"
  report_token_plan.py --stdout-only   # 只打印,不写文件
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

TZ_OFFSET_HOURS = 8
DSN = os.environ.get(
    "MINIMAX_TOKEN_PLAN_DSN",
    "host=127.0.0.1 port=5432 dbname=minimax_token_plan user=postgres",
)
REPORT_DIR = Path(
    os.environ.get(
        "TOKEN_PLAN_REPORT_DIR",
        os.path.expanduser("~/Documents/MiniMax/token-plan/reports"),
    )
)


def iso_week_bounds(now_local):
    last_mon = now_local - timedelta(days=now_local.weekday() + 7)
    last_mon = last_mon.replace(hour=0, minute=0, second=0, microsecond=0)
    last_sun_end = last_mon + timedelta(days=7)
    return last_mon, last_sun_end


def fetch_snapshots(cur, start, end):
    cur.execute("""
        SELECT id, captured_at, base_status_code, base_status_msg
        FROM token_plan_snapshots
        WHERE captured_at >= %s AND captured_at < %s
        ORDER BY captured_at ASC
    """, (start, end))
    return cur.fetchall()


def fetch_model_usage(cur, snapshot_ids):
    if not snapshot_ids:
        return []
    cur.execute("""
        SELECT snapshot_id, model_name,
               interval_start_ms, interval_end_ms, interval_remains_ms,
               interval_usage_count, interval_remaining_pct, interval_status,
               weekly_start_ms, weekly_end_ms, weekly_remains_ms,
               weekly_usage_count, weekly_remaining_pct, weekly_status
        FROM token_plan_model_usage
        WHERE snapshot_id = ANY(%s)
        ORDER BY snapshot_id, model_name
    """, (snapshot_ids,))
    return cur.fetchall()


def fmt_pct(p):
    return f"{p}%" if p is not None else "—"


def fmt_int(x):
    return str(x) if x is not None else "—"


def ms_to_human(ms):
    if ms is None or ms <= 0:
        return "已重置"
    s = ms // 1000
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    return f"{days}天{hours}小时{minutes}分"


def iso_to_local(ts, tz):
    return ts.astimezone(tz).strftime("%m-%d %H:%M")


def evaluate_windows(model_rows, threshold=5, tz=None):
    """把同一 model 的 snapshot 按 interval_start_ms 分组,每个 5h 窗口找最小 remaining%.
    返回 list of dict: window_start, window_end, model_name, min_pct, used, is_full
    """
    by_window = defaultdict(list)
    for r in model_rows:
        iv_start = r["iv_start_ms"]
        if iv_start is None:
            continue
        by_window[iv_start].append(r)

    windows = []
    for iv_start, rows in by_window.items():
        iv_start_int = iv_start
        iv_end_int = rows[0]["iv_end_ms"]
        valid = [r for r in rows if r["iv_pct"] is not None]
        if not valid:
            continue
        min_pct = min(r["iv_pct"] for r in valid)
        is_full = min_pct <= threshold
        windows.append({
            "window_start_ms": iv_start_int,
            "window_end_ms": iv_end_int,
            "model_name": rows[0].get("model_name", "?"),
            "min_pct": min_pct,
            "sample_count": len(valid),
            "is_full": is_full,
        })
    windows.sort(key=lambda x: x["window_start_ms"])
    return windows


def aggregate_daily(windows, tz):
    """按日期聚合窗口.返回 list of (date_str, total, full, ratio)."""
    by_day = defaultdict(lambda: {"total": 0, "full": 0})
    for w in windows:
        if w["window_start_ms"] is None:
            continue
        start_local = datetime.fromtimestamp(
            w["window_start_ms"] / 1000, tz=timezone.utc
        ).astimezone(tz)
        day = start_local.strftime("%m-%d")
        by_day[day]["total"] += 1
        if w["is_full"]:
            by_day[day]["full"] += 1
    out = []
    for day in sorted(by_day):
        d = by_day[day]
        ratio = (d["full"] / d["total"] * 100) if d["total"] else 0
        out.append({"day": day, "total": d["total"], "full": d["full"], "ratio": ratio})
    return out


def generate(now=None, stdout_only=False):
    tz = timezone(timedelta(hours=TZ_OFFSET_HOURS))
    now = now or datetime.now(timezone.utc)
    now_local = now.astimezone(tz)
    last_mon, last_sun_end = iso_week_bounds(now_local)
    iso_year, iso_week, _ = last_mon.isocalendar()

    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            snapshots = fetch_snapshots(cur, last_mon, last_sun_end)
            snap_ids = [s[0] for s in snapshots]
            usage_rows = fetch_model_usage(cur, snap_ids)

    snap_index = {s[0]: s for s in snapshots}

    # 整理: model_name -> [(captured_at, dict)]
    by_model = defaultdict(list)
    for row in usage_rows:
        (sid, mname, iv_start, iv_end, iv_rem, iv_used, iv_pct, iv_status,
         wk_start, wk_end, wk_rem, wk_used, wk_pct, wk_status) = row
        by_model[mname].append({
            "captured_at": snap_index[sid][1],
            "iv_pct": iv_pct,
            "iv_used": iv_used,
            "iv_rem_ms": iv_rem,
            "iv_start_ms": iv_start,
            "iv_end_ms": iv_end,
            "wk_pct": wk_pct,
            "wk_used": wk_used,
            "wk_rem_ms": wk_rem,
            "wk_start_ms": wk_start,
            "wk_end_ms": wk_end,
        })

    lines = []
    title = f"Token Plan 周报 · {iso_year}-W{iso_week:02d}"
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"> 报告生成时间: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    lines.append(f"> 数据范围: {last_mon.strftime('%Y-%m-%d %H:%M')} ~ "
                 f"{last_sun_end.strftime('%Y-%m-%d %H:%M')} (北京时间)")
    lines.append(f"> 数据源: MiniMax `/v1/token_plan/remains`")
    lines.append(f"> 数据库: `minimax_token_plan`")
    lines.append("")

    # === 0. 报告口径说明 ===
    lines.append("## 0. 报告口径说明")
    lines.append("")
    lines.append("- Token Plan 的**周限额**视套餐档位而定:无限档(如 Plus 档)周使用量不会被窗口限制")
    lines.append("  - 若你的周限额是无限档,「周用量 diff」没有意义,本报告以 **5h 窗口**为主维度")
    lines.append("  - 若为有限周限额,可另行按 `weekly_usage_count` 做周 diff")
    lines.append("- API 返回的 `current_interval_*` = **5h 窗口**;该窗口按**模型分类**独立计时")
    lines.append("  - 控制台汇总显示一个 5h 重置倒计时,实际是某个 model 的视图")
    lines.append("- API 模型分类命名 vs 控制台:")
    lines.append("  - `general` → 通用文本/编程模型汇总")
    lines.append("  - `video` → 视频生成相关")
    lines.append("")

    # === 1. 快照覆盖 ===
    lines.append("## 1. 快照覆盖情况")
    lines.append("")
    if not snapshots:
        lines.append("⚠️ 该时间段内**没有任何快照**。")
    else:
        first_at = snapshots[0][1].astimezone(tz).strftime('%Y-%m-%d %H:%M:%S')
        last_at = snapshots[-1][1].astimezone(tz).strftime('%Y-%m-%d %H:%M:%S')
        lines.append(f"- 快照数: **{len(snapshots)}**")
        lines.append(f"- 首条: {first_at}")
        lines.append(f"- 末条: {last_at}")
    lines.append("")

    # === 2. 整体用满率 ===
    lines.append("## 2. 整体用满率(上一周汇总)")
    lines.append("")
    lines.append("- **判定**:每个 5h 窗口内 `current_interval_remaining_percent` 最小值 ≤ 5% → 用满")
    lines.append("- **目标**:≥ 80% 用满率 = 套餐配额刚好够用,< 50% = 浪费")
    lines.append("")

    all_windows_per_model = {}
    for mname in sorted(by_model):
        all_windows_per_model[mname] = evaluate_windows(by_model[mname], threshold=5, tz=tz)

    grand_total = 0
    grand_full = 0
    for mname in all_windows_per_model:
        for w in all_windows_per_model[mname]:
            grand_total += 1
            if w["is_full"]:
                grand_full += 1
    grand_ratio = (grand_full / grand_total * 100) if grand_total else 0

    if grand_total == 0:
        lines.append("⚠️ 没有可评估的窗口(无快照或快照不在窗口内)")
    else:
        if grand_ratio >= 80:
            evaluate = "✅ 用满率优秀"
            advice = "继续保持,套餐配额刚好够用"
        elif grand_ratio >= 50:
            evaluate = "🟡 用满率中等"
            advice = "在剩余 > 20% 时加大调用密度"
        else:
            evaluate = "⚠️ 用满率偏低"
            advice = "大量 5h 配额被浪费,可考虑降低套餐档位"
        lines.append(f"- **总用满次数**:**{grand_full} 次**(用满)")
        lines.append(f"- **总窗口数**:**{grand_total} 次**")
        lines.append(f"- **用满率**:**{grand_ratio:.1f}%**")
        lines.append(f"- **评估**:{evaluate}")
        lines.append(f"- **建议**:{advice}")
    lines.append("")

    # === 3. 每日汇总 ===
    lines.append("## 3. 每日汇总")
    lines.append("")
    if grand_total == 0:
        lines.append("⚠️ 无快照")
    else:
        lines.append("| 日期 | 用满窗口 | 总窗口 | 用满率 | 评估 |")
        lines.append("|---|---|---|---|---|")
        by_day_total = defaultdict(lambda: {"total": 0, "full": 0})
        for mname, windows in all_windows_per_model.items():
            for w in windows:
                start_local = datetime.fromtimestamp(
                    w["window_start_ms"] / 1000, tz=timezone.utc
                ).astimezone(tz)
                day = start_local.strftime("%m-%d")
                by_day_total[day]["total"] += 1
                if w["is_full"]:
                    by_day_total[day]["full"] += 1
        for day in sorted(by_day_total):
            d = by_day_total[day]
            ratio = (d["full"] / d["total"] * 100) if d["total"] else 0
            if ratio >= 80:
                tag = "✅ 用满"
            elif ratio >= 50:
                tag = "🟡 部分"
            else:
                tag = "⚠️ 浪费"
            lines.append(
                f"| {day} | {d['full']} | {d['total']} | {ratio:.0f}% | {tag} |"
            )
    lines.append("")

    # === 4. 控制台 vs API ===
    lines.append("## 4. 控制台 vs API 对照(参考)")
    lines.append("")
    lines.append("- API `/v1/token_plan/remains` 只给「剩余% / 已用次数」,**不给 token 数 / 缓存命中率**")
    lines.append("- 控制台「近 7 天 token 总量」是按调用明细累计的,该接口不直接返回(可能需要控制台其他接口)")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"_自动生成 · 脚本 `report_token_plan.py` (5h 维度版)_")

    md = "\n".join(lines)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    fname = REPORT_DIR / f"report_{iso_year}-W{iso_week:02d}.md"
    fname.write_text(md, encoding="utf-8")
    print(f"Wrote: {fname}", file=sys.stderr)
    print(md)
    return fname


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--now", help="覆盖当前时间 (ISO 8601)")
    parser.add_argument("--stdout-only", action="store_true")
    args = parser.parse_args()
    now = datetime.fromisoformat(args.now) if args.now else None
    if now and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    generate(now, stdout_only=args.stdout_only)


if __name__ == "__main__":
    main()
