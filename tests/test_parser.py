import unittest
import os
import json
import shutil
import tempfile
import sys

# テストターゲットのインポート用パス追加
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))
import main

class TestParser(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        
    def tearDown(self):
        shutil.rmtree(self.test_dir)
        
    def test_jsonl_parser(self):
        transcript_path = os.path.join(self.test_dir, "transcript.jsonl")
        
        test_records = [
            # 1. ユーザー入力
            {"step_index": 1, "source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": "2026-06-26T10:00:00Z", "content": "<USER_REQUEST>テスト指示</USER_REQUEST>"},
            # 2. AIの思考とタスク予定
            {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": "2026-06-26T10:00:05Z", "content": "考え中", "tool_calls": [{"name": "run_command", "args": {"toolSummary": "テスト実行", "CommandLine": "echo hello"}}]},
            # 3. コマンド実行（成功）
            {"step_index": 3, "source": "MODEL", "type": "RUN_COMMAND", "created_at": "2026-06-26T10:00:10Z", "content": "Completed At: 2026-06-26T10:00:10Z\nOutput:\nhello"},
            # 4. エラー行（破損JSON）
            "{" , # 破損した行
            # 5. ask_question コール
            {"step_index": 4, "source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": "2026-06-26T10:00:15Z", "content": "質問します", "tool_calls": [{"name": "ask_question", "args": {"questions": [{"question": "テスト質問", "options": ["はい", "いいえ"], "is_multi_select": False}], "toolSummary": "質問表示"}}]}
        ]
        
        with open(transcript_path, "w", encoding="utf-8") as f:
            for r in test_records:
                if isinstance(r, str):
                    f.write(r + "\n")
                else:
                    f.write(json.dumps(r) + "\n")
                    
        # get_latest_transcript_path をモック化
        original_get_latest = main.get_latest_transcript_path
        main.get_latest_transcript_path = lambda: transcript_path
        
        try:
            state = main.get_session_state()
            
            # 検証 1: ログ件数
            logs = state["logs"]
            self.assertEqual(len(logs), 4) # 破損行はスキップされる
            
            # 検証 2: 各送信者とテキスト
            self.assertEqual(logs[0]["sender"], "user")
            self.assertEqual(logs[0]["text"], "テスト指示")
            
            self.assertEqual(logs[1]["sender"], "ai")
            self.assertTrue("【タスク予定】" in logs[1]["text"])
            
            self.assertEqual(logs[2]["sender"], "system")
            self.assertTrue("[コマンド実行]" in logs[2]["text"])
            
            # 検証 3: active_question の抽出
            active_q = state["active_question"]
            self.assertIsNotNone(active_q)
            self.assertEqual(active_q["question"], "テスト質問")
            self.assertEqual(active_q["options"], ["はい", "いいえ"])
            self.assertIsNotNone(active_q["question_id"])
            
        finally:
            # 元に戻す
            main.get_latest_transcript_path = original_get_latest

if __name__ == "__main__":
    unittest.main()
