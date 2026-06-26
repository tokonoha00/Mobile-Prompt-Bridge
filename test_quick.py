import requests
r = requests.get("http://127.0.0.1:8712/api/session_state", headers={"X-Bridge-Token": "Jijrsidj423j423"}, timeout=5)
print(f"Status: {r.status_code}")
d = r.json()
logs = d.get("logs", [])
print(f"Logs count: {len(logs)}")
print(f"Active question: {d.get('active_question')}")
if logs:
    for log in logs[-3:]:
        print(f"  [{log['sender']}] {log['text'][:80]}")
