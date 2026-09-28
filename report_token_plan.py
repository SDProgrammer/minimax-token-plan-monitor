#!/usr/bin/env python3
"""report_token_plan.py - 从 PostgreSQL 读快照,生成周报 Markdown (5h 维度版).

报告范围(默认):上周一 00:00 (北京时间) ~ 上周日 24:00.
输出: ~/Documents/MiniMax/token-plan/reports/report_YYYY-Www.md

口径说明:Token Plan 套餐"周限额"对 Plus 是"∞ 无限制",所以按 weekly_usage_count
        算周 diff 永远 = 0。本报告以"5h 窗口 × 模型分类"为主维度,
        统计每个窗口的实际使用率(100% - 窗口内最小剩余%),而不是只看是否用满.

Usage:
  report_token_plan.py                 # 默认生成上周一周报
  report_token_plan.py --now <iso8601> # 用指定时间算"上一周"
  report_token_plan.py --stdout-only   # 只打印,不写文件
"""
import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

TZ_OFFSET_HOURS = 8
DSN = "host=127.0.0.1 port=5432 dbname=minimax_token_plan user=postgres"
REPORT_DIR = Path("/root/Documents/MiniMax/token-plan/reports")


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


def usage_band(usage_pct):
    """按使用率分档: >=95 用满, >=80 高效, >=50 中等, >=20 偏低, <20 基本未用."""
    if usage_pct >= 95:
        return "用满(≥95%)"
    if usage_pct >= 80:
        return "高效(80-95%)"
    if usage_pct >= 50:
        return "中等(50-80%)"
    if usage_pct >= 20:
        return "偏低(20-50%)"
    return "浪费(<20%)"


def evaluate_windows(model_rows, threshold=5, tz=None):
    """把同一 model 的 snapshot 按 interval_start_ms 分组,每个 5h 窗口计算:
    - min_pct: 窗口内最小剩余% (最接近重置时的剩余)
    - usage_pct: 实际使用率 = 100 - min_pct
    - band: 使用率分档
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
        usage_pct = round(100 - min_pct, 1) if min_pct is not None else None
        band = usage_band(usage_pct) if usage_pct is not None else "?"
        windows.append({
            "window_start_ms": iv_start_int,
            "window_end_ms": iv_end_int,
            "model_name": rows[0].get("model_name", "?"),
            "min_pct": min_pct,
            "usage_pct": usage_pct,
            "band": band,
            "sample_count": len(valid),
            "is_full": is_full,
        })
    windows.sort(key=lambda x: x["window_start_ms"])
    return windows


def aggregate_daily(windows, tz):
    """按日期聚合窗口.返回 list of (date_str, total, avg_usage, min_usage, full_count)."""
    by_day = defaultdict(lambda: {"total": 0, "full": 0, "usage_sum": 0.0, "min_usage": 101.0})
    for w in windows:
        if w["window_start_ms"] is None or w["usage_pct"] is None:
            continue
        start_local = datetime.fromtimestamp(
            w["window_start_ms"] / 1000, tz=timezone.utc
        ).astimezone(tz)
        day = start_local.strftime("%m-%d")
        by_day[day]["total"] += 1
        by_day[day]["usage_sum"] += w["usage_pct"]
        by_day[day]["min_usage"] = min(by_day[day]["min_usage"], w["usage_pct"])
        if w["is_full"]:
            by_day[day]["full"] += 1
    out = []
    for day in sorted(by_day):
        d = by_day[day]
        avg = d["usage_sum"] / d["total"] if d["total"] else 0
        out.append({
            "day": day, "total": d["total"], "full": d["full"],
            "avg_usage": avg, "min_usage": d["min_usage"],
        })
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

    # === 0. 重要发现(用户最关心的口径) ===
    lines.append("## 0. 报告口径说明")
    lines.append("")
    lines.append("- 你的套餐 `TokenPlanPlus-月度会员` 的 **周限额 = ∞ 无限制**")
    lines.append("  - API 的 `weekly_usage_count` 一直在涨,但**不会被周窗口限制**")
    lines.append("  - 因此「周用量 diff」对 Plus 套餐**没有意义**,改为 **5h 窗口维度**")
    lines.append("- API 返回的 `current_interval_*` = **5h 窗口**;该窗口按**模型分类**独立计时")
    lines.append("  - 控制台汇总显示一个 5h 重置倒计时,实际是某个 model 的视图")
    lines.append("- **使用率口径**:每个 5h 窗口取 `current_interval_remaining_percent` 最小值")
    lines.append("  → 使用率 = 100% - 最小剩余%。低于 100% 说明这个窗口真实用掉了一部分配额,")
    lines.append("  用满率只是「用满」这一种情况的特例")
    lines.append("- API 模型分类命名 vs 控制台:")
    lines.append("  - `general` → 控制台 `MiniMax-M3-512k` + `MiniMax-M2.1` 等通用模型汇总")
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

    # === 2. 整体使用率(上一周汇总) ===
    lines.append("## 2. 整体使用率(上一周汇总)")
    lines.append("")
    lines.append("- **使用率口径**:每个 5h 窗口内最小剩余% → 使用率 = 100% - 剩余%")
    lines.append("- **分档**:≥95% 用满 · 80-95% 高效 · 50-80% 中等 · 20-50% 偏低 · <20% 浪费")
    lines.append("")

    # 算所有 model 合并的窗口评估
    all_windows_per_model = {}
    for mname in sorted(by_model):
        all_windows_per_model[mname] = evaluate_windows(by_model[mname], threshold=5, tz=tz)

    grand_total = 0
    grand_full = 0
    usage_values = []
    band_counts = defaultdict(int)
    for mname in all_windows_per_model:
        for w in all_windows_per_model[mname]:
            grand_total += 1
            if w["is_full"]:
                grand_full += 1
            if w["usage_pct"] is not None:
                usage_values.append(w["usage_pct"])
                band_counts[w["band"]] += 1

    if grand_total == 0:
        lines.append("⚠️ 没有可评估的窗口(无快照或快照不在窗口内)")
    else:
        avg_usage = sum(usage_values) / len(usage_values) if usage_values else 0
        lines.append(f"- **总窗口数**:**{grand_total} 次**")
        lines.append(f"- **平均使用率**:**{avg_usage:.1f}%**")
        lines.append(f"- **用满窗口(≥95%)**:**{grand_full} 次**")
        lines.append("")
        lines.append("**使用率分布**:")
        lines.append("")
        lines.append("| 使用率档位 | 窗口数 | 占比 |")
        lines.append("|---|---|---|")
        for band in ["用满(≥95%)", "高效(80-95%)", "中等(50-80%)", "偏低(20-50%)", "浪费(<20%)"]:
            cnt = band_counts.get(band, 0)
            ratio = (cnt / grand_total * 100) if grand_total else 0
            lines.append(f"| {band} | {cnt} | {ratio:.0f}% |")
        lines.append("")
        # 评估基于平均使用率
        if avg_usage >= 80:
            evaluate = "✅ 使用率高"
            advice = "配额利用充分,继续保持"
        elif avg_usage >= 50:
            evaluate = "🟡 使用率中等"
            advice = "还有提升空间,可把空闲窗口利用起来"
        else:
            evaluate = "⚠️ 使用率偏低"
            advice = "大量窗口只用了不到一半,可考虑降低套餐档位或加大使用"
        lines.append(f"- **评估**:{evaluate}")
        lines.append(f"- **建议**:{advice}")
    lines.append("")

    # === 3. 每日汇总 ===
    lines.append("## 3. 每日汇总")
    lines.append("")
    if grand_total == 0:
        lines.append("⚠️ 无快照")
    else:
        lines.append("| 日期 | 窗口数 | 平均使用率 | 最低使用率 | 用满 | 评估 |")
        lines.append("|---|---|---|---|---|---|")
        daily = aggregate_daily(
            [w for windows in all_windows_per_model.values() for w in windows], tz
        )
        for d in daily:
            if d["avg_usage"] >= 80:
                tag = "✅ 高效"
            elif d["avg_usage"] >= 50:
                tag = "🟡 中等"
            else:
                tag = "⚠️ 偏低"
            lines.append(
                f"| {d['day']} | {d['total']} | {d['avg_usage']:.0f}% | "
                f"{d['min_usage']:.0f}% | {d['full']} | {tag} |"
            )
    lines.append("")

    # === 4. 分模型明细 ===
    lines.append("## 4. 分模型明细")
    lines.append("")
    for mname in sorted(all_windows_per_model):
        windows = all_windows_per_model[mname]
        lines.append(f"### {mname}")
        lines.append("")
        if not windows:
            lines.append("(无窗口)")
            lines.append("")
            continue
        total = len(windows)
        full = sum(1 for w in windows if w["is_full"])
        avg = sum(w["usage_pct"] for w in windows if w["usage_pct"] is not None) / total
        lines.append(f"- 窗口数: {total} · 用满: {full} · 平均使用率: **{avg:.1f}%**")
        lines.append("")
        lines.append("| 窗口开始 | 使用率 | 档位 | 采样数 |")
        lines.append("|---|---|---|---|")
        for w in windows:
            start_local = datetime.fromtimestamp(
                w["window_start_ms"] / 1000, tz=timezone.utc
            ).astimezone(tz)
            lines.append(
                f"| {start_local.strftime('%m-%d %H:%M')} | {w['usage_pct']:.0f}% | "
                f"{w['band']} | {w['sample_count']} |"
            )
        lines.append("")

    # === 5. 控制台 vs API 简要对照 ===
    lines.append("## 5. 控制台 vs API 对照(参考)")
    lines.append("")
    lines.append("- 套餐 `TokenPlanPlus` 周限额 = ∞ 无限制,周维度对你无意义")
    lines.append("- API `/v1/token_plan/remains` 只给「剩余% / 已用次数」,**不给 token 数 / 缓存命中率**")
    lines.append("- 控制台「近 7 天 119.36M Token」是按调用明细累计的,API 不返回(需要 `/v1/usage/list` 之类接口)")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"_自动生成 · 脚本 `~/bin/report_token_plan.py` (5h 维度版,含使用率分布)_")

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
