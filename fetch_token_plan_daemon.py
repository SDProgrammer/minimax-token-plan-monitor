#!/usr/bin/env python3
"""fetch_token_plan_daemon.py - 长跑 daemon,精确在 5h 窗口重置前 2 分钟触发.

用法:
  fetch_token_plan_daemon.py
"""
import argparse, json, os, subprocess, sys, time, traceback, urllib.error, urllib.request
from datetime import datetime, timezone

API_URL = "https://www.minimax.cn/v1/token_plan/remains"
DEFAULT_DSN = os.environ.get(
    "MINIMAX_TOKEN_PLAN_DSN",
    "host=127.0.0.1 port=5432 dbname=minimax_token_plan user=postgres",
)
LOG = os.environ.get("TOKEN_PLAN_LOG", os.path.expanduser("~/Documents/MiniMax/token-plan/daemon.log"))
MAX_SLEEP = 1800      # 保底 30 分钟(防止 rolling anchor 漂移漏抓)
MIN_SLEEP = 60        # 至少 1 分钟
TRIGGER_BEFORE_RESET = 120  # 重置前 2 分钟触发


def log(msg):
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def get_key():
    path = os.environ.get(
        "MINIMAX_TOKEN_PLAN_SECRET_FILE",
        os.path.expanduser("~/.config/minimax/secret"),
    )
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    env_key = os.environ.get("MINIMAX_TOKEN_PLAN_KEY")
    if env_key:
        return env_key
    log("ERROR: 未找到 Key,在 ~/.config/minimax/secret 或 MINIMAX_TOKEN_PLAN_KEY")
    sys.exit(2)


def fetch(url, key):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def insert_snapshot(cur, captured_at, url, payload):
    import psycopg
    base_resp = payload.get("base_resp", {})
    cur.execute(
        """
        INSERT INTO token_plan_snapshots
            (captured_at, source_url, raw_response,
             base_status_code, base_status_msg)
        VALUES (%s, %s, %s::jsonb, %s, %s)
        RETURNING id
        """,
        (
            captured_at, url, json.dumps(payload),
            base_resp.get("status_code"), base_resp.get("status_msg"),
        ),
    )
    sid = cur.fetchone()[0]
    for m in payload.get("model_remains", []):
        cur.execute(
            """
            INSERT INTO token_plan_model_usage
                (snapshot_id, model_name,
                 interval_start_ms, interval_end_ms, interval_remains_ms,
                 interval_total_count, interval_usage_count,
                 interval_remaining_pct, interval_status,
                 weekly_start_ms, weekly_end_ms, weekly_remains_ms,
                 weekly_total_count, weekly_usage_count,
                 weekly_remaining_pct, weekly_status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s)
            """,
            (
                sid, m.get("model_name"),
                m.get("start_time"), m.get("end_time"), m.get("remains_time"),
                m.get("current_interval_total_count"),
                m.get("current_interval_usage_count"),
                m.get("current_interval_remaining_percent"),
                m.get("current_interval_status"),
                m.get("weekly_start_time"),
                m.get("weekly_end_time"),
                m.get("weekly_remains_time"),
                m.get("current_weekly_total_count"),
                m.get("current_weekly_usage_count"),
                m.get("current_weekly_remaining_percent"),
                m.get("current_weekly_status"),
            ),
        )
    return sid


def next_sleep(payload):
    if not payload.get("model_remains"):
        return MIN_SLEEP
    delays = []
    for m in payload["model_remains"]:
        rem_s = (m.get("remains_time") or 0) / 1000
        delay = max(MIN_SLEEP, rem_s - TRIGGER_BEFORE_RESET)
        delays.append(delay)
    sleep_s = min(delays)
    sleep_s = min(sleep_s, MAX_SLEEP)
    return sleep_s


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=API_URL)
    parser.add_argument("--db", default=DEFAULT_DSN)
    parser.add_argument("--once", action="store_true", help="跑一次后退出(调试用)")
    args = parser.parse_args()

    import psycopg
    key = get_key()
    log("daemon started")

    while True:
        try:
            payload = fetch(args.url, key)
            base_resp = payload.get("base_resp", {})
            if base_resp.get("status_code") != 0:
                log(f"API non-0: {base_resp}")
                time.sleep(60)
                continue

            captured_at = datetime.now(timezone.utc)
            with psycopg.connect(args.db) as conn:
                with conn.cursor() as cur:
                    sid = insert_snapshot(cur, captured_at, args.url, payload)
                conn.commit()
            summary = ", ".join(
                f"{m['model_name']}:{m.get('current_interval_remaining_percent')}%5h"
                for m in payload["model_remains"]
            )
            log(f"snapshot_id={sid} {summary}")

            sleep_s = next_sleep(payload)
            log(f"sleep {sleep_s:.0f}s ({sleep_s/60:.1f} min)")
            if args.once:
                break
            time.sleep(sleep_s)
        except urllib.error.HTTPError as e:
            log(f"HTTP {e.code} {e.reason}")
            time.sleep(60)
        except Exception as e:
            log(f"ERROR: {e}")
            log(traceback.format_exc())
            time.sleep(60)


if __name__ == "__main__":
    main()
