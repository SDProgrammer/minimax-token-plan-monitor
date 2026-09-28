#!/usr/bin/env python3
"""push_token_plan_to_feishu.py - 把周报 Markdown 摘要推到飞书自定义机器人."""
import argparse, json, os, re, sys, urllib.request
from pathlib import Path

DEFAULT_WEBHOOK_FILE = os.path.expanduser("~/.config/minimax/feishu_webhook")

def get_webhook():
    w = os.environ.get("FEISHU_WEBHOOK")
    if w: return w.strip()
    p = Path(DEFAULT_WEBHOOK_FILE)
    if p.is_file(): return p.read_text().strip()
    print(f"ERROR: 找不到 webhook (env 或 {DEFAULT_WEBHOOK_FILE})", file=sys.stderr)
    sys.exit(2)

def extract(md):
    out = {"week": "", "body": []}
    m = re.search(r"^#\s*(.+)$", md, re.MULTILINE)
    if m: out["week"] = m.group(1).strip()
    section = None
    for ln in md.splitlines():
        if ln.startswith("## "):
            section = ln.strip(); continue
        if section is None: continue
        if section.startswith("## 2"):
            s = ln.strip()
            if s.startswith("- **总窗口数**") or s.startswith("- **平均使用率**") \
               or s.startswith("- **用满窗口") or s.startswith("- **评估**") \
               or s.startswith("- **建议**"):
                out["body"].append(s)
            if "⚠️" in ln and "没有可评估的窗口" in ln:
                out["body"].append("> ⚠️ 本周没有可评估的窗口(快照不足)")
        elif section.startswith("## 3"):
            if ln.startswith("|"):
                out["body"].append(ln)
    return out

def build_card(week, body_lines, source_file):
    body_md = "\n".join(body_lines) if body_lines else "(报告里没解析到关键行)"
    color = "grey"
    if "浪费" in body_md: color = "red"
    elif "🟡" in body_md: color = "yellow"
    elif "✅" in body_md: color = "green"
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": week or "Token Plan 周报"},
                       "template": color},
            "elements": [
                {"tag": "markdown", "content": body_md},
                {"tag": "hr"},
                {"tag": "note", "elements": [
                    {"tag": "plain_text",
                     "content": f"完整报告: {os.path.basename(source_file)} · 已存服务器"}
                ]},
            ],
        },
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    args = ap.parse_args()
    md_path = Path(args.file)
    if not md_path.is_file():
        print(f"ERROR: 文件不存在 {md_path}", file=sys.stderr); sys.exit(1)
    md = md_path.read_text(encoding="utf-8")
    info = extract(md)
    card = build_card(info["week"], info["body"], str(md_path))
    webhook = get_webhook()
    # 重试机制: 飞书侧偶发 19006 等瞬时错误, 重试 3 次, 间隔 5s
    import time as _time
    last_err = None
    for attempt in range(1, 4):
        req = urllib.request.Request(
            webhook, data=json.dumps(card).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
            if result.get("code") == 0 or result.get("StatusCode") == 0:
                print(f"OK pushed: {info['week']} (attempt {attempt})")
                return
            last_err = f"飞书返回非 0: {result}"
        except Exception as e:
            last_err = str(e)
        print(f"  [attempt {attempt}/3] push failed: {last_err}", file=sys.stderr)
        if attempt < 3:
            _time.sleep(5)
    print(f"ERROR: 推送失败(已重试3次): {last_err}", file=sys.stderr)
    sys.exit(4)

if __name__ == "__main__":
    main()
