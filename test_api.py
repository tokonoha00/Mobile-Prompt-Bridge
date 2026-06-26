"""APIレスポンスのテスト用スクリプト"""
import requests
import json

BASE_URL = "http://127.0.0.1:8712"
TOKEN = "Jijrsidj423j423"

# 1. session_state のテスト
print("=== /api/session_state テスト ===")
try:
    r = requests.get(f"{BASE_URL}/api/session_state", headers={"X-Bridge-Token": TOKEN}, timeout=5)
    print(f"ステータス: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        logs = data.get("logs", [])
        active_q = data.get("active_question")
        print(f"ログ件数: {len(logs)}")
        if logs:
            print(f"最初のログ: sender={logs[0].get('sender')}, text={logs[0].get('text')[:80]}...")
            print(f"最後のログ: sender={logs[-1].get('sender')}, text={logs[-1].get('text')[:80]}...")
        else:
            print("ログが空です！")
        print(f"アクティブ質問: {active_q}")
    else:
        print(f"エラー: {r.text}")
except Exception as e:
    print(f"例外: {e}")

# 2. chat_history のテスト
print("\n=== /api/chat_history テスト ===")
try:
    r = requests.get(f"{BASE_URL}/api/chat_history", headers={"X-Bridge-Token": TOKEN}, timeout=5)
    print(f"ステータス: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"ログ件数: {len(data)}")
        if data:
            print(f"最初: sender={data[0].get('sender')}, text={data[0].get('text')[:80]}...")
        else:
            print("ログが空です！")
    else:
        print(f"エラー: {r.text}")
except Exception as e:
    print(f"例外: {e}")

# 3. transcript パスの確認
print("\n=== transcript パスの確認 ===")
import glob
import os
home = os.path.expanduser("~")
pattern = os.path.join(home, ".gemini", "antigravity-ide", "brain", "*", ".system_generated", "logs", "transcript.jsonl")
files = glob.glob(pattern)
print(f"検索パターン: {pattern}")
print(f"見つかったファイル数: {len(files)}")
if files:
    files.sort(key=os.path.getmtime, reverse=True)
    for f in files[:3]:
        size = os.path.getsize(f)
        mtime = os.path.getmtime(f)
        print(f"  {f}")
        print(f"    サイズ: {size} bytes, 最終更新: {mtime}")
