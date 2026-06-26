"""transcript.jsonl のパースをサーバー無しで直接テスト"""
import json
import os
import glob

home = os.path.expanduser("~")
pattern = os.path.join(home, ".gemini", "antigravity-ide", "brain", "*", ".system_generated", "logs", "transcript.jsonl")
files = glob.glob(pattern)
files.sort(key=os.path.getmtime, reverse=True)
path = files[0]
print(f"対象ファイル: {path}")
print(f"サイズ: {os.path.getsize(path)} bytes")

all_items = []
errors = 0
with open(path, "r", encoding="utf-8") as f:
    for i, line in enumerate(f):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            all_items.append(data)
        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"JSON parse error at line {i+1}: {e}")
                print(f"  Line preview: {line[:100]}")

print(f"\n総行数: {len(all_items)}, パースエラー: {errors}")

# 最新150件に絞る
items = all_items[-150:]
print(f"最新150件のtypeの内訳:")
type_counts = {}
for item in items:
    t = item.get("type", "UNKNOWN")
    type_counts[t] = type_counts.get(t, 0) + 1
for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
    print(f"  {t}: {c}")

# ユーザー発言を抽出
user_msgs = [item for item in items if item.get("type") == "USER_INPUT" and item.get("source") == "USER_EXPLICIT"]
print(f"\nユーザー発言数: {len(user_msgs)}")
if user_msgs:
    last_msg = user_msgs[-1]
    content = last_msg.get("content", "")
    if "<USER_REQUEST>" in content:
        content = content.split("<USER_REQUEST>")[1].split("</USER_REQUEST>")[0].strip()
    print(f"最新ユーザー発言: {content[:100]}")

# AI応答を抽出
ai_msgs = [item for item in items if item.get("type") == "PLANNER_RESPONSE" and item.get("source") == "MODEL"]
print(f"\nAI応答数: {len(ai_msgs)}")
if ai_msgs:
    last_ai = ai_msgs[-1]
    content = last_ai.get("content", "")
    print(f"最新AI応答 (先頭100文字): {content[:100] if content else '(空)'}")

# 実際にchat_logsを構築してみる
chat_logs = []
for data in items:
    step_type = data.get("type")
    source = data.get("source")
    content = data.get("content", "")
    
    if step_type == "USER_INPUT" and source == "USER_EXPLICIT" and content:
        clean = content
        if "<USER_REQUEST>" in clean:
            clean = clean.split("<USER_REQUEST>")[1].split("</USER_REQUEST>")[0].strip()
        chat_logs.append({"sender": "user", "text": clean})
    elif step_type == "PLANNER_RESPONSE" and source == "MODEL" and content:
        chat_logs.append({"sender": "ai", "text": content[:100]})

print(f"\n構築されたchat_logs件数: {len(chat_logs)}")
if chat_logs:
    print(f"最初のログ: {chat_logs[0]}")
    print(f"最後のログ: {chat_logs[-1]}")
