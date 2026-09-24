#!/usr/bin/env python3
"""fetch_token_plan.py - 拉一次 MiniMax Token Plan 快照,写入 PostgreSQL.

Key 读取顺序(优先级从高到低):
  1) --key CLI 参数(仅本地测试)
  2) macOS Keychain: service=minimax-token-plan-key, account=minimax-token-plan
  3) secret 文件 ~/.config/minimax/secret
  4) 环境变量 MINIMAX_TOKEN_PLAN_KEY

Key 在 fetch 完后立刻丢弃引用,从不写日志/文件/数据库.

Usage:
  fetch_token_plan.py            # 正常拉一次,入库
  fetch_token_plan.py --key xxx  # 本地测试(临时,生产请走 Keychain)
  fetch_token_plan.py --dry-run  # 只打印解析,不入库
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone


API_URL = "https://www.minimax.cn/v1/token_plan/remains"
KEYCHAIN_SERVICE = "minimax-token-plan-key"
KEYCHAIN_ACCOUNT = "minimax-token-plan"
SECRET_FILE = os.environ.get(
    "MINIMAX_TOKEN_PLAN_SECRET_FILE",
    os.path.expanduser("~/.config/minimax/secret"),
)
DEFAULT_DSN = os.environ.get(
    "MINIMAX_TOKEN_PLAN_DSN",
    "host=127.0.0.1 port=5432 dbname=minimax_token_plan user=postgres",
)


def get_key(args):
    # 1) CLI
    if args.key:
        return args.key
    # 2) macOS Keychain (MacBook 环境)
    try:
        out = subprocess.check_output(
            [
                "security", "find-generic-password",
                "-s", KEYCHAIN_SERVICE,
                "-a", KEYCHAIN_ACCOUNT,
                "-w",
            ],
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    # 3) 文件(Linux 服务器)
    if os.path.exists(SECRET_FILE):
        try:
            with open(SECRET_FILE) as f:
                return f.read().strip()
        except OSError:
            pass
    # 4) env
    env_key = os.environ.get("MINIMAX_TOKEN_PLAN_KEY")
    if env_key:
        return env_key
    print(
        "ERROR: 未找到 Key. 用以下任一方式:\n"
        f"  1) security add-generic-password -s {KEYCHAIN_SERVICE} "
        f"-a {KEYCHAIN_ACCOUNT} -w '<KEY>'\n"
        "  2) export MINIMAX_TOKEN_PLAN_KEY=<KEY>\n"
        "  3) --key <KEY>(仅本地测试)",
        file=sys.stderr,
    )
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
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s)
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", help="API Key(仅本地测试)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--url", default=API_URL)
    parser.add_argument("--db", default=DEFAULT_DSN)
    parser.add_argument(
        "--captured-at",
        help="覆盖快照时间(ISO 8601,默认 NOW),用于 backfill 历史",
    )
    parser.add_argument(
        "--mock",
        help="直接给 JSON payload,跳过 API(本地回填/测试用)",
    )
    args = parser.parse_args()

    if args.mock:
        payload = json.loads(args.mock)
        print("MOCK 跳过 API,使用内联 JSON", file=sys.stderr)
    else:
        key = get_key(args)
        try:
            payload = fetch(args.url, key)
        except urllib.error.HTTPError as e:
            print(f"ERROR: HTTP {e.code} {e.reason}", file=sys.stderr)
            sys.exit(3)
        except Exception as e:
            print(f"ERROR: fetch failed: {e}", file=sys.stderr)
            sys.exit(3)
        finally:
            del key

    base_resp = payload.get("base_resp", {})
    if base_resp.get("status_code") != 0:
        print(f"ERROR: API 非 0: {base_resp}", file=sys.stderr)
        sys.exit(4)

    models = payload.get("model_remains", [])
    summary = ", ".join(
        f"{m.get('model_name')}:"
        f"{m.get('current_weekly_remaining_percent')}%week/"
        f"{m.get('current_interval_remaining_percent')}%5h"
        for m in models
    )
    print(f"OK  models={len(models)}  {summary}")

    if args.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    import psycopg
    if args.captured_at:
        captured_at = datetime.fromisoformat(args.captured_at)
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=timezone.utc)
    else:
        captured_at = datetime.now(timezone.utc)
    with psycopg.connect(args.db) as conn:
        with conn.cursor() as cur:
            sid = insert_snapshot(cur, captured_at, args.url, payload)
        conn.commit()
    print(f"   snapshot_id={sid}")


if __name__ == "__main__":
    main()
