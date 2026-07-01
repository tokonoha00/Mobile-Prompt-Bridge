import os
import hashlib
import psutil
import sys
import json
import time
import threading
import socket
import secrets
import logging
import shutil
import ctypes
import subprocess
import re
import collections
import urllib.parse
from ctypes import wintypes
from fastapi import FastAPI, Request, Query, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import pyperclip
import pyautogui
import glob
import datetime
import uiautomation as auto
import cv2
import numpy as np
import mss
import asyncio
from winsdk.windows.media.ocr import OcrEngine
from winsdk.windows.globalization import Language
from winsdk.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode

# グローバルなスキャン結果保存用
LATEST_SCAN_RESULT = None
ACTIVE_INSTANCE_ID = None
IDE_INSTANCES = {}
INSTANCE_TO_TRANSCRIPT = {}

# 複数IDEタブの紐付け(差分検知)用
# instance_id -> {path: (mtime, size)} 紐付け開始時点のスナップショット (手動フォールバック用)
PENDING_LINK_SNAPSHOTS = {}
# 既に何らかのインスタンスに紐づけ済みの transcript パス一覧
# (同じログファイルが複数タブに誤って紐づくのを防ぐ)
CLAIMED_TRANSCRIPT_PATHS = set()

# ---- 自動タブ検出(バックグラウンド常時監視)用 ----
AUTO_DETECT_ENABLED = True
AUTO_DETECT_INTERVAL_SEC = 1.5
_BG_LAST_SNAPSHOT = {}

# ログディレクトリの作成
os.makedirs("logs", exist_ok=True)

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/bridge.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("PromptBridge")

# 設定ファイルの処理
CONFIG_PATH = "config.json"
TEMPLATE_PATH = "config.example.json"

if not os.path.exists(CONFIG_PATH):
    if os.path.exists(TEMPLATE_PATH):
        logger.info(f"config.json が見つからないため、{TEMPLATE_PATH} からコピーして生成します。")
        shutil.copy(TEMPLATE_PATH, CONFIG_PATH)
    else:
        logger.error("設定テンプレート config.example.json が存在しません。")
        sys.exit(1)

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

PORT = config.get("port", 8712)
ALLOWED_TITLES = [t.lower() for t in config.get("allowed_window_titles", [])]
SEND_KEY = config.get("send_key", "Enter")
FOCUS_KEY = config.get("focus_key", "None")
ENABLE_AUTO_SEND = config.get("enable_auto_send", False)
LOG_LINES_LIMIT = config.get("log_lines_limit", 300)
ACTIVE_Q_LINES_LIMIT = config.get("active_q_lines_limit", 3000)

# セキュリティトークンの取得または生成
SECURITY_TOKEN = config.get("security_token", "")
if not SECURITY_TOKEN:
    SECURITY_TOKEN = secrets.token_hex(8)
    logger.info(f"SECURITY_TOKEN: (生成されたランダムトークン) {SECURITY_TOKEN}")
else:
    logger.info(f"SECURITY_TOKEN: (設定された固定トークン) {SECURITY_TOKEN}")

# 背景色キャリブレーションデータのロード
BG_COLOR_CHAT_OPENED = config.get("bg_color_chat_opened", None)
BG_COLOR_CHAT_CLOSED = config.get("bg_color_chat_closed", None)
if BG_COLOR_CHAT_OPENED:
    BG_COLOR_CHAT_OPENED = tuple(BG_COLOR_CHAT_OPENED)
if BG_COLOR_CHAT_CLOSED:
    BG_COLOR_CHAT_CLOSED = tuple(BG_COLOR_CHAT_CLOSED)

# キャッシュ用グローバル変数
SESSION_STATE_CACHE_BY_PATH = {}

# 履歴ファイルパス
HISTORY_PATH = "history.json"

# ==================== Windows API (ctypes) 定義 ====================
user32 = ctypes.windll.user32

# コールバック型の定義
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

# ウィンドウ座標取得用のRECT構造体とAPI定義
class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long)
    ]

user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL

def get_visible_windows():
    """現在アクティブかつ可視状態の全ウィンドウのメタデータを返します。"""
    windows = []
    
    def enum_windows_callback(hwnd, lParam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                title = buffer.value
                
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                
                exe_name = "unknown"
                try:
                    proc = psutil.Process(pid.value)
                    exe_name = proc.name()
                except Exception:
                    pass
                    
                windows.append((hwnd, pid.value, title, exe_name))
        return True

    user32.EnumWindows(WNDENUMPROC(enum_windows_callback), 0)
    return windows

def bring_window_to_front(hwnd):
    """指定された HWND のウィンドウを最前面化（アクティブ化）します。"""
    hwnd = wintypes.HWND(int(hwnd))
    # 最小化状態の場合は元に戻す
    # SW_RESTORE = 9, SW_SHOW = 5
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
    else:
        user32.ShowWindow(hwnd, 5)
        
    # Windowsのフォーカス奪取防止機能をバイパスするため、Altキー空打ちを送信
    # VK_MENU = 18 (Alt), KEYEVENTF_KEYUP = 2
    user32.keybd_event(18, 0, 0, 0)
    user32.keybd_event(18, 0, 2, 0)
    
    # 最前面化
    return user32.SetForegroundWindow(hwnd)

# ==================== ネットワークユーティリティ ====================
def get_local_ip():
    """PCのLAN内ローカルIPアドレスを取得します。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 実際にパケットは送らず、ルート検索のみ
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

# ==================== ピクセル解析 (キャリブレーション用) ====================
def get_pixel_color_at_relative(hwnd, rel_x_from_right=150, rel_y_from_top=300):
    """指定されたウィンドウの相対位置 (右からX px, 上からY px) の画面ピクセルRGB色を取得します。"""
    hwnd = wintypes.HWND(int(hwnd))
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        logger.error("GetWindowRect に失敗しました。")
        return None
    
    # 絶対座標の計算
    abs_x = rect.right - rel_x_from_right
    abs_y = rect.top + rel_y_from_top
    
    # 画面解像度内に収める
    try:
        screen_width, screen_height = pyautogui.size()
        abs_x = max(0, min(abs_x, screen_width - 1))
        abs_y = max(0, min(abs_y, screen_height - 1))
        
        # ピクセルカラーの取得
        color = pyautogui.pixel(abs_x, abs_y)
        logger.info(f"ピクセルカラー取得: 相対 (右-{rel_x_from_right}, 上+{rel_y_from_top}) -> 絶対 ({abs_x}, {abs_y}) = {color}")
        return color # (R, G, B) のタプル
    except Exception as e:
        logger.error(f"ピクセルカラーの取得に失敗しました: {e}")
        return None

def color_distance(c1, c2):
    """2つのRGB色の距離を計算します。"""
    if not c1 or not c2:
        return 9999
    return ((c1[0] - c2[0])**2 + (c1[1] - c2[1])**2 + (c1[2] - c2[2])**2) ** 0.5

def is_chat_opened(hwnd):
    """キャリブレーション値を用いて、現在チャット欄が開いているか判定します。"""
    global BG_COLOR_CHAT_OPENED, BG_COLOR_CHAT_CLOSED
    if BG_COLOR_CHAT_OPENED is None or BG_COLOR_CHAT_CLOSED is None:
        logger.info("キャリブレーションデータが不足しているため、デフォルトで『閉じている』と判定します。")
        return False
        
    current_color = get_pixel_color_at_relative(hwnd)
    if not current_color:
        return False
        
    dist_opened = color_distance(current_color, BG_COLOR_CHAT_OPENED)
    dist_closed = color_distance(current_color, BG_COLOR_CHAT_CLOSED)
    
    logger.info(f"色差判定: 開いている状態との差={dist_opened:.1f}, 閉じている状態との差={dist_closed:.1f}")
    
    # 閾値 35 以内で、かつ「開いている色」に近い場合
    if dist_opened < dist_closed and dist_opened < 35:
        return True
    return False


def get_all_transcript_files():
    """存在する全ての会話フォルダ(conversation-id毎)のtranscriptファイルを列挙します。
    transcript_full.jsonl があればそれを、無ければ同フォルダの transcript.jsonl を使います。
    1会話フォルダにつき1パスを返します(複数IDEタブを区別するための土台)。"""
    home = os.path.expanduser("~")
    brain_root = os.path.join(home, ".gemini", "antigravity-ide", "brain")

    result = []
    try:
        conv_dirs = glob.glob(os.path.join(brain_root, "*"))
    except Exception:
        conv_dirs = []

    for conv_dir in conv_dirs:
        full_path = os.path.join(conv_dir, ".system_generated", "logs", "transcript_full.jsonl")
        light_path = os.path.join(conv_dir, ".system_generated", "logs", "transcript.jsonl")
        if os.path.exists(full_path):
            result.append(full_path)
        elif os.path.exists(light_path):
            result.append(light_path)

    return result


def snapshot_transcript_mtimes():
    """全transcriptファイルの (mtime, size) スナップショットを取得します。
    紐付け操作の前後を比較して「どのタブのログが実際に更新されたか」を検出するために使います。"""
    snap = {}
    for path in get_all_transcript_files():
        try:
            snap[path] = (os.path.getmtime(path), os.path.getsize(path))
        except OSError:
            continue
    return snap


def get_latest_transcript_path():
    """最も新しく更新された transcript_full.jsonl ファイルのパスを取得します。なければ transcript.jsonl にフォールバックします。
    注意: IDEが1つしか開いていない場合の簡易フォールバック専用です。
    複数タブがある場合にどのタブのログか特定する用途には使わないでください
    (「新しい方を選ぶ」だけでは他のタブの更新と混同するため)。"""
    files = get_all_transcript_files()

    if not files:
        logger.warning(f"履歴ファイルが見つかりません。")
        return None
        
    # 更新日時でソートして最新のものを返す
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0]

def get_chat_history():
    """下位互換性のため、get_session_state()のログ部分のみを返します。"""
    state = get_session_state()
    return state.get("logs", [])


def get_transcript_path_for_active_instance():
    global ACTIVE_INSTANCE_ID, INSTANCE_TO_TRANSCRIPT
    if ACTIVE_INSTANCE_ID and ACTIVE_INSTANCE_ID in INSTANCE_TO_TRANSCRIPT:
        path = INSTANCE_TO_TRANSCRIPT[ACTIVE_INSTANCE_ID]
        import os
        if os.path.exists(path):
            return path
    return None

def get_session_state():
    global SESSION_STATE_CACHE_BY_PATH, IDE_INSTANCES, ACTIVE_INSTANCE_ID
    
    path = get_transcript_path_for_active_instance()
    is_unlinked = False
    
    if not path:
        if len(IDE_INSTANCES) == 1:
            path = get_latest_transcript_path()
        else:
            is_unlinked = True
            
    if not path:
        return {
            "active_instance_id": ACTIVE_INSTANCE_ID,
            "transcript_path": None,
            "is_unlinked": is_unlinked,
            "logs": [],
            "active_question": None
        }

    if path not in SESSION_STATE_CACHE_BY_PATH:
        SESSION_STATE_CACHE_BY_PATH[path] = {
            "mtime": 0,
            "size": 0,
            "state": {"logs": [], "active_question": None}
        }
        
    cache = SESSION_STATE_CACHE_BY_PATH[path]
    try:
        mtime = os.path.getmtime(path)
        size = os.path.getsize(path)
    except OSError:
        return {
            "active_instance_id": ACTIVE_INSTANCE_ID,
            "transcript_path": path,
            "is_unlinked": is_unlinked,
            "logs": cache["state"]["logs"],
            "active_question": cache["state"]["active_question"]
        }

    if mtime == cache["mtime"] and size == cache["size"]:
        res = dict(cache["state"])
        res["active_instance_id"] = ACTIVE_INSTANCE_ID
        res["transcript_path"] = path
        res["is_unlinked"] = is_unlinked
        return res

    logs = []
    active_question = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except:
                    continue
                
                # Active Question parsing
                if "tool_calls" in entry:
                    for t in entry["tool_calls"]:
                        if t.get("toolName") == "ask_question":
                            try:
                                args = json.loads(t.get("arguments", "{}"))
                                qs = args.get("questions", [])
                                if qs:
                                    q_data = qs[0]
                                    active_question = {
                                        "question": q_data.get("question", ""),
                                        "options": q_data.get("options", []),
                                        "is_multi_select": q_data.get("is_multi_select", False)
                                    }
                            except:
                                pass
                
                # Role detection
                role = "unknown"
                if entry.get("source") == "MODEL":
                    role = "ai"
                elif entry.get("source") == "USER_EXPLICIT":
                    role = "user"
                elif entry.get("source") == "SYSTEM":
                    role = "system"
                    
                content = entry.get("content", "")
                if not content and entry.get("tool_calls"):
                    tool_names = [t.get("toolName") for t in entry["tool_calls"]]
                    content = f"[Tool Calls: {', '.join(tool_names)}]"

                if content:
                    logs.append({
                        "sender": role,
                        "text": content,
                        "timestamp": entry.get("timestamp", "00:00:00")
                    })
    except Exception as e:
        logger.error(f"Error reading transcript: {e}")

    cache["mtime"] = mtime
    cache["size"] = size
    cache["state"] = {
        "logs": logs,
        "active_question": active_question
    }
    
    return {
        "active_instance_id": ACTIVE_INSTANCE_ID,
        "transcript_path": path,
        "is_unlinked": is_unlinked,
        "logs": logs,
        "active_question": active_question
    }
        
    if path == LAST_TRANSCRIPT_PATH and mtime == LAST_TRANSCRIPT_MTIME and size == LAST_TRANSCRIPT_SIZE:
        return CACHED_SESSION_STATE
        
    try:
        stat = os.stat(path)
        mtime = stat.st_mtime
        size = stat.st_size
    except Exception as e:
        logger.error(f"Failed to stat {path}: {e}")
        return CACHED_SESSION_STATE
        
    if path == LAST_TRANSCRIPT_PATH and mtime == LAST_TRANSCRIPT_MTIME and size == LAST_TRANSCRIPT_SIZE:
        return CACHED_SESSION_STATE

    all_items = []
    success_count = 0
    fail_count = 0
    read_count = 0
    
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = collections.deque(f, maxlen=ACTIVE_Q_LINES_LIMIT)
            read_count = len(lines)
            for line in lines:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    all_items.append(data)
                    success_count += 1
                except Exception:
                    fail_count += 1
                    continue
    except Exception as e:
        logger.error(f"履歴ファイルの読み込みに失敗しました: {e}")
        return CACHED_SESSION_STATE

    display_items = all_items[-LOG_LINES_LIMIT:] if LOG_LINES_LIMIT > 0 else all_items
    
    chat_logs = []
    for data in display_items:
        step_type = data.get("type")
        source = data.get("source")
        content = data.get("content", "")
        created_at = data.get("created_at", "")
        
        if step_type == "USER_INPUT" and source == "USER_EXPLICIT":
            if content:
                clean_content = content
                if "<USER_REQUEST>" in clean_content:
                    clean_content = clean_content.split("<USER_REQUEST>")[1].split("</USER_REQUEST>")[0].strip()
                chat_logs.append({"sender": "user", "text": clean_content, "timestamp": created_at})
                
        elif step_type == "PLANNER_RESPONSE" and source == "MODEL":
            if content:
                text = content
                tool_calls = data.get("tool_calls", [])
                if tool_calls:
                    text += "\n\n**【タスク予定】**"
                    for tc in tool_calls:
                        name = tc.get("name", "")
                        args = tc.get("args", {})
                        summary = args.get("toolSummary", args.get("toolAction", name))
                        if isinstance(summary, str):
                            summary = summary.strip('"')
                        text += f"\n* `{summary}` ({name})"
                chat_logs.append({"sender": "ai", "text": text, "timestamp": created_at})
                
        elif source == "MODEL" or step_type in ["RUN_COMMAND", "VIEW_FILE", "GREP_SEARCH", "LIST_DIRECTORY", "SEARCH_WEB", "ASK_QUESTION", "ERROR_MESSAGE"]:
            if not content:
                continue
            if step_type == "RUN_COMMAND":
                summary_text = "コマンド実行が完了しました。"
                if "failed with exit code" in content:
                    summary_text = "⚠️ コマンド実行がエラー終了しました。"
                preview = content[:200] + ("..." if len(content) > 200 else "")
                chat_logs.append({"sender": "system", "text": f"**[コマンド実行]** {summary_text}\n```\n{preview.strip()}\n```", "timestamp": created_at})
            elif step_type == "VIEW_FILE":
                lines = content.split("\n")
                file_path = "ファイル"
                for l in lines:
                    if "File Path:" in l:
                        file_path = l.replace("File Path:", "").strip(" `")
                        break
                chat_logs.append({"sender": "system", "text": f"📂 ファイルを閲覧しました:\n`{file_path}`", "timestamp": created_at})
            elif step_type == "GREP_SEARCH":
                preview = content[:200] + ("..." if len(content) > 200 else "")
                chat_logs.append({"sender": "system", "text": f"🔍 テキスト検索を実行しました。\n{preview.strip()}", "timestamp": created_at})
            elif step_type == "LIST_DIRECTORY":
                preview = content[:200] + ("..." if len(content) > 200 else "")
                chat_logs.append({"sender": "system", "text": f"📁 ディレクトリ一覧を表示しました。\n{preview.strip()}", "timestamp": created_at})
            elif step_type == "SEARCH_WEB":
                preview = content[:200] + ("..." if len(content) > 200 else "")
                chat_logs.append({"sender": "system", "text": f"🌐 ウェブ検索を実行しました。\n{preview.strip()}", "timestamp": created_at})
            elif step_type == "ASK_QUESTION":
                ans = ""
                for l in content.split("\n"):
                    if l.startswith("A") and ":" in l:
                        ans = l.split(":", 1)[1].strip()
                        break
                if not ans:
                    ans = "回答完了"
                chat_logs.append({"sender": "system", "text": f"💬 質問に回答しました: **{ans}**", "timestamp": created_at})
            elif step_type == "ERROR_MESSAGE":
                preview = content[:200] + ("..." if len(content) > 200 else "")
                chat_logs.append({"sender": "system", "text": f"❌ エラーが発生しました:\n{preview.strip()}", "timestamp": created_at})

    LAST_TRANSCRIPT_PATH = path
    LAST_TRANSCRIPT_MTIME = mtime
    LAST_TRANSCRIPT_SIZE = size
    CACHED_SESSION_STATE = {
        "logs": chat_logs
    }

    logger.info(f"Parsed {path}: mtime={mtime}, size={size}, read_lines={read_count}, "
                f"success={success_count}, fail={fail_count}")

    return CACHED_SESSION_STATE


def find_via_uiautomation(hwnd):
    """UI Automationを使用してProceedボタンを検索します。"""
    try:
        # スレッド環境でのCOMエラーを防止するためにCOMを初期化
        try:
            ctypes.windll.ole32.CoInitialize(None)
        except Exception:
            pass
        # HWND から UI Automation 要素を取得
        ide_control = auto.ControlFromHandle(int(hwnd))
        if not ide_control:
            logger.warning("UI Automation: HWND からコントロールを取得できませんでした。")
            return None
        
        buttons = []
        
        def walk_and_find(control):
            name = control.Name
            # ControlType が ButtonControl で、Name に "Proceed" または "Submit" が含まれるか確認
            if control.ControlType == auto.ControlType.ButtonControl and name and ("Proceed" in name or "Submit" in name):
                buttons.append(control)
            try:
                for child in control.GetChildren():
                    walk_and_find(child)
            except Exception:
                pass
                
        walk_and_find(ide_control)
        
        # Submit ボタンを最優先、なければ Proceed ボタンを探す
        submit_buttons = [b for b in buttons if b.Name and "Submit" in b.Name]
        proceed_buttons = [b for b in buttons if b.Name and "Proceed" in b.Name]
        
        target_btn = None
        if submit_buttons:
            submit_buttons.sort(key=lambda b: b.BoundingRectangle.bottom if b.BoundingRectangle else 0, reverse=True)
            target_btn = submit_buttons[0]
            logger.info(f"UI Automation: 'Submit' ボタンを優先検出しました。名前: '{target_btn.Name}'")
        elif proceed_buttons:
            proceed_buttons.sort(key=lambda b: b.BoundingRectangle.bottom if b.BoundingRectangle else 0, reverse=True)
            target_btn = proceed_buttons[0]
            logger.info(f"UI Automation: 'Proceed' ボタンを検出しました。名前: '{target_btn.Name}'")
            
        if not target_btn:
            logger.info("UI Automation: 'Submit' または 'Proceed' ボタンが見つかりませんでした。")
            return None
        
        rect = target_btn.BoundingRectangle
        if not rect:
            logger.warning("UI Automation: ボタンの BoundingRectangle が取得できませんでした。")
            return None
            
        # ボタンの中心座標
        center_x = (rect.left + rect.right) // 2
        center_y = (rect.top + rect.bottom) // 2
        
        # IDEウィンドウの範囲内かチェック
        ide_rect = RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(ide_rect)):
            if not (ide_rect.left <= center_x <= ide_rect.right and ide_rect.top <= center_y <= ide_rect.bottom):
                logger.warning(f"UI Automation: 検出されたボタン座標 ({center_x}, {center_y}) が IDE ウィンドウの範囲外です。")
                return None
                
        logger.info(f"UI Automation: 'Proceed' ボタンを検出しました。名前: '{target_btn.Name}', 座標: ({center_x}, {center_y})")
        return (center_x, center_y)
    except Exception as e:
        logger.error(f"UI Automation 実行中にエラーが発生しました: {e}")
        return None


def find_via_image_processing(hwnd):
    """OpenCVによる高度な画像解析でProceedボタンを検索します（UI Automationのフォールバック）。"""
    try:
        # アクティブウィンドウの範囲を取得
        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            logger.error("GetWindowRect に失敗しました。")
            return None
            
        # 検索エリアをウィンドウの右側 800px に拡張し、上下端を除外
        # 下端除外は15pxに縮小してボタンが除外されないようにする
        search_left = max(rect.left, rect.right - 800)
        search_right = rect.right
        search_top = rect.top + 50
        search_bottom = rect.bottom - 15
        
        # 画面全体のスクリーンショットを撮影
        scr_path = "temp_proceed_scr.png"
        pyautogui.screenshot(scr_path)
        
        # OpenCVで読み込む
        img_bgr = cv2.imread(scr_path)
        if img_bgr is None:
            logger.error("OpenCV でのスクリーンショット読み込みに失敗しました。")
            if os.path.exists(scr_path):
                os.remove(scr_path)
            return None
            
        h_img, w_img, _ = img_bgr.shape
        
        # 検索座標を画像範囲内に収める
        search_left = max(0, min(search_left, w_img - 1))
        search_right = max(0, min(search_right, w_img - 1))
        search_top = max(0, min(search_top, h_img - 1))
        search_bottom = max(0, min(search_bottom, h_img - 1))
        
        # チャットエリアの切り出し (ROI)
        chat_roi = img_bgr[search_top:search_bottom, search_left:search_right]
        h_roi, w_roi, _ = chat_roi.shape
        
        # マスク画像の作成 (RGB範囲: R: 10〜45, G: 80〜140, B: 80〜140 でGとBの比率が近い)
        mask = np.zeros((h_roi, w_roi), dtype=np.uint8)
        for y in range(h_roi):
            for x in range(w_roi):
                b, g, r = chat_roi[y, x][:3]
                if (10 <= r <= 45) and (80 <= g <= 140) and (80 <= b <= 140):
                    if (g > 0 and 0.8 <= b / g <= 1.25) and (g > r * 1.5):
                        mask[y, x] = 255
                        
        # モルフォロジー演算 (Morphology Open/Close) でノイズ除去
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask_cleaned = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask_cleaned = cv2.morphologyEx(mask_cleaned, cv2.MORPH_OPEN, kernel)
        
        # 8近傍連結成分の抽出
        num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(mask_cleaned, connectivity=8)
        
        candidates = []
        for i in range(1, num_labels):
            x_c, y_c, w_c, h_c, area = stats[i]
            
            aspect_ratio = w_c / h_c if h_c > 0 else 0
            fill_ratio = area / (w_c * h_c) if (w_c * h_c) > 0 else 0
            
            # クロップして白文字ピクセル（R, G, B すべて200以上）をカウント
            roi_crop = chat_roi[y_c:y_c+h_c, x_c:x_c+w_c]
            white_count = 0
            for cy in range(roi_crop.shape[0]):
                for cx in range(roi_crop.shape[1]):
                    cb, cg, cr = roi_crop[cy, cx][:3]
                    if cr > 200 and cg > 200 and cb > 200:
                        white_count += 1
                        
            white_ratio = white_count / area if area > 0 else 0
            
            # スコアの計算 (初期値 1.0)
            score = 1.0
            
            # 1. サイズ制限: 幅 30-120px, 高さ 18-45px
            if not (30 <= w_c <= 120):
                score -= 0.4
            if not (18 <= h_c <= 45):
                score -= 0.4
            if w_c >= 150 or h_c >= 60:
                score = 0
                
            # 2. 縦横比: 1.5 - 5.0
            if not (1.5 <= aspect_ratio <= 5.0):
                score -= 0.3
                
            # 3. 塗りつぶし密度: 0.35 - 0.95
            if not (0.35 <= fill_ratio <= 0.95):
                score -= 0.3
                
            # 4. 白文字の有無（テキストが存在するか）
            if 0.02 <= white_ratio <= 0.25:
                score += 0.2
            else:
                score -= 0.2
                
            # 5. 位置スコア（チャットパネル内の右端から左寄り、且つ下寄りを優先）
            rel_x = x_c / w_roi
            rel_y = y_c / h_roi
            pos_score = (1.0 - rel_x) * 0.1 + rel_y * 0.1
            score += pos_score
            
            score = max(0.0, min(score, 1.0))
            
            candidates.append({
                'id': i,
                'x': x_c + search_left,
                'y': y_c + search_top,
                'w': w_c,
                'h': h_c,
                'area': area,
                'fill_ratio': fill_ratio,
                'aspect_ratio': aspect_ratio,
                'white_ratio': white_ratio,
                'score': score,
                'centroid': (centroids[i][0] + search_left, centroids[i][1] + search_top)
            })
            
            logger.info(
                f"画像認識候補 {i}: x={x_c+search_left}, y={y_c+search_top}, w={w_c}, h={h_c}, "
                f"area={area}, fill_ratio={fill_ratio:.2f}, aspect={aspect_ratio:.2f}, "
                f"white_ratio={white_ratio:.3f}, score={score:.2f}"
            )
            
        # デバッグ画像の作成
        debug_img = img_bgr.copy()
        cv2.rectangle(debug_img, (search_left, search_top), (search_right, search_bottom), (255, 0, 0), 2)
        
        for c in candidates:
            cv2.rectangle(debug_img, (c['x'], c['y']), (c['x'] + c['w'], c['y'] + c['h']), (0, 165, 255), 2)
            cv2.putText(debug_img, f"S:{c['score']:.2f}", (c['x'], c['y'] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 165, 255), 1)
            
        # 有効な候補のフィルタ（スコア 0.6 以上）
        valid_candidates = [c for c in candidates if c['score'] >= 0.6]
        
        selected_cand = None
        if len(valid_candidates) == 1:
            selected_cand = valid_candidates[0]
            logger.info(f"画像認識: スコア基準を満たす唯一の有効な候補 (ID {selected_cand['id']}, score={selected_cand['score']:.2f}) を採用。")
        elif len(valid_candidates) > 1:
            valid_candidates.sort(key=lambda c: c['score'], reverse=True)
            best = valid_candidates[0]
            second = valid_candidates[1]
            # スコア差が 0.1 以上あれば採用
            if best['score'] - second['score'] >= 0.1:
                selected_cand = best
                logger.info(f"画像認識: スコア最大候補 (ID {best['id']}, score={best['score']:.2f}) を採用 (2番目との差 {best['score'] - second['score']:.2f})。")
            else:
                logger.warning(f"画像認識: 複数候補のスコア差が小さいため判定をスキップ (best={best['score']:.2f}, second={second['score']:.2f})。")
                
        click_pos = None
        if selected_cand:
            if selected_cand['w'] < 130 and selected_cand['h'] < 50:
                click_pos = (int(selected_cand['centroid'][0]), int(selected_cand['centroid'][1]))
                cv2.circle(debug_img, click_pos, 5, (0, 0, 255), -1)
                cv2.putText(debug_img, "CLICK", (click_pos[0] + 10, click_pos[1] + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
                
        # デバッグ画像の保存
        os.makedirs("debug", exist_ok=True)
        now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_path = f"debug/proceed_detection_{now_str}.png"
        cv2.imwrite(debug_path, debug_img)
        logger.info(f"デバッグ用画像を保存しました: proceed_detection_{now_str}.png")
            
        if os.path.exists(scr_path):
            os.remove(scr_path)
            
        return click_pos
    except Exception as e:
        logger.error(f"画像処理でエラーが発生しました: {e}")
        return None


def find_and_click_green_proceed_button():
    """Proceedボタンを検出してクリックします。まずUI Automationを試し、失敗すれば画像認識にフォールバックします。"""
    try:
        hwnd = user32.GetForegroundWindow()
        
        # 1. UI Automation での検出を試みる
        logger.info("Proceed ボタン検出フェーズ: UI Automation を試みます...")
        click_pos = find_via_uiautomation(hwnd)
        
        # 2. UI Automation で見つからなかった場合は画像認識にフォールバック
        if not click_pos:
            logger.info("UI Automation で見つからなかったため、画像処理にフォールバックします...")
            click_pos = find_via_image_processing(hwnd)
            
        if not click_pos:
            logger.warning("Proceed ボタンが検出されませんでした。")
            return False
            
        # 3. 検出された座標をクリック
        logger.info(f"Proceed ボタンを座標 ({click_pos[0]}, {click_pos[1]}) にてクリックします。")
        pyautogui.moveTo(click_pos[0], click_pos[1], duration=0.3)
        time.sleep(0.1)
        pyautogui.mouseDown()
        time.sleep(0.1)
        pyautogui.mouseUp()
        return True
    except Exception as e:
        logger.error(f"Proceed ボタンのクリック処理中に致命的なエラーが発生しました: {e}")
        return False


# ==================== FastAPI アプリケーション ====================
app = FastAPI(title="Mobile Prompt Bridge API")


@app.on_event("startup")
def _start_auto_detect_thread():
    """サーバー起動時に、複数IDEタブを自動識別するバックグラウンド監視スレッドを開始する。
    これにより、ユーザーはスマホ側で紐付け操作を行う必要がなくなる
    (PC側で普段通りIDEに入力するだけで、どのタブのログかが自動的に判定される)。"""
    t = threading.Thread(target=_transcript_auto_detect_loop, daemon=True)
    t.start()

class PasteRequest(BaseModel):
    text: str
    action: str  # "copy" / "paste" / "paste_send"

# ---------- 会話ログ取得用ヘルパー ----------
def get_raw_transcript() -> str:
    """最新の transcript.jsonl を文字列として返す。"""
    path = get_latest_transcript_path()
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.error(f"Transcript の読み込みに失敗しました: {e}")
        return ""

class SubmitAnswerRequest(BaseModel):
    key: str
    question_id: str

# トークン認証関数
def check_token(token: str):
    if token != SECURITY_TOKEN:
        logger.warning(f"不正なトークンでのアクセス試行がありました: {token}")
        raise HTTPException(status_code=403, detail="Forbidden: Invalid token")

@app.get("/")
def get_index(token: str = Query(None)):
    check_token(token)
    # web/index.html を返す
    html_path = os.path.join("web", "index.html")
    if os.path.exists(html_path):
        return FileResponse(
            html_path,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
    else:
        logger.error(f"HTMLファイルが見つかりません: {html_path}")
        raise HTTPException(status_code=404, detail="HTML file not found")


def get_active_target_hwnd(windows):
    global ACTIVE_INSTANCE_ID, IDE_INSTANCES
    
    if ACTIVE_INSTANCE_ID and ACTIVE_INSTANCE_ID in IDE_INSTANCES:
        inst = IDE_INSTANCES[ACTIVE_INSTANCE_ID]
        for hwnd, pid, title, exe in windows:
            if hwnd == inst["hwnd"]:
                title_lower = title.lower()
                if any(allowed in title_lower for allowed in ALLOWED_TITLES):
                    return hwnd, title
        ACTIVE_INSTANCE_ID = None
        
    for hwnd, pid, title, exe in windows:
        title_lower = title.lower()
        if any(allowed in title_lower for allowed in ALLOWED_TITLES):
            instance_id = hashlib.sha1(f"{hwnd}:{pid}:{title}".encode()).hexdigest()[:12]
            ACTIVE_INSTANCE_ID = instance_id
            return hwnd, title
            
    return None, None


# ==================== 書き込み監視に頼らない静的な紐付け(workspaceStorage方式) ====================
# Antigravity IDE は VSCode 系のアーキテクチャを持つため、開いているワークスペース(フォルダ)ごとに
# 「workspaceStorage/<hash>/」という設定フォルダが既に存在する。ここには
#   - workspace.json  : そのウィンドウで開いているフォルダの絶対パス
#   - state.vscdb      : そのワークスペースの内部状態(SQLite)。会話ID等が埋め込まれていることがある
# が入っており、これらは「読むだけ」で済むので、チャットへの書き込みを待つ必要が無い。
# ただし内部フォーマットは非公開・バージョン依存のため、うまく取れない場合は
# 従来の書き込み監視(_transcript_auto_detect_tick)にフォールバックする。

_UUID_RE = re.compile(
    rb'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
)


def _find_antigravity_appdata_root():
    """Antigravity IDE のアプリケーションデータルート(workspaceStorageの親)を探す。
    フォルダ名がバージョンによって違う("Antigravity" / "Antigravity IDE" 等)ため候補を順に試す。"""
    candidates = []
    appdata = os.environ.get("APPDATA")  # Windows: C:\Users\<user>\AppData\Roaming
    if appdata:
        candidates += [
            os.path.join(appdata, "Antigravity IDE"),
            os.path.join(appdata, "Antigravity"),
            os.path.join(appdata, "antigravity-ide"),
        ]
    for c in candidates:
        if os.path.isdir(os.path.join(c, "User", "workspaceStorage")):
            return c
    return None


def _workspace_uri_to_path(uri):
    """"file:///C:/foo/bar" のようなURI文字列をパスに変換する簡易ヘルパー"""
    if not uri:
        return None
    if uri.startswith("file:///"):
        try:
            return urllib.parse.unquote(uri[len("file:///"):])
        except Exception:
            return uri[len("file:///"):]
    return uri


def _list_workspace_storage_folders():
    """workspaceStorage/<hash>/workspace.json を全て読み、
    { 開いているフォルダ名(小文字): {"state_db": state.vscdbのパス, "folder_path": 絶対パス} }
    を返す。設定ファイルを読むだけなので即座に(書き込み待ちなしに)実行できる。"""
    root = _find_antigravity_appdata_root()
    if not root:
        return {}

    result = {}
    ws_root = os.path.join(root, "User", "workspaceStorage")
    for hash_dir in glob.glob(os.path.join(ws_root, "*")):
        ws_json_path = os.path.join(hash_dir, "workspace.json")
        if not os.path.exists(ws_json_path):
            continue
        try:
            with open(ws_json_path, "r", encoding="utf-8") as f:
                ws_data = json.load(f)
        except Exception:
            continue

        folder_uri = ws_data.get("folder") or ws_data.get("workspace")
        folder_path = _workspace_uri_to_path(folder_uri)
        if not folder_path:
            continue

        base_name = os.path.basename(folder_path.rstrip("/\\")).lower()
        state_db_path = os.path.join(hash_dir, "state.vscdb")
        result[base_name] = {
            "folder_path": folder_path,
            "state_db": state_db_path if os.path.exists(state_db_path) else None,
        }
    return result


def _get_known_brain_uuids():
    home = os.path.expanduser("~")
    brain_root = os.path.join(home, ".gemini", "antigravity-ide", "brain")
    return {
        os.path.basename(d) for d in glob.glob(os.path.join(brain_root, "*")) if os.path.isdir(d)
    }


def _find_conversation_ids_in_state_db(state_db_path, known_uuids):
    """state.vscdb (SQLite)を生バイト単位でスキャンし、実在する brain/<uuid> と一致する
    UUID文字列を探す。protobufの正式スキーマは非公開のため、埋め込まれた文字列を
    正規表現で拾う簡易的な方法(=それらしいUUIDが1つも見つからない/複数見つかる場合は
    誤爆を避けるため諦めてフォールバックする)。"""
    if not state_db_path or not os.path.exists(state_db_path):
        return []
    try:
        with open(state_db_path, "rb") as f:
            raw = f.read()
    except Exception:
        return []

    found = set()
    for m in _UUID_RE.finditer(raw):
        candidate = m.group().decode("ascii").lower()
        if candidate in known_uuids:
            found.add(candidate)
    return list(found)


def resolve_transcript_by_workspace(workspace_hint):
    """ウィンドウタイトルから得た workspace_hint (フォルダ名らしき文字列) を手がかりに、
    書き込みを一切待たずにtranscriptファイルを特定する。
    特定できなければ None を返し、呼び出し側は書き込み監視の自動検出にフォールバックする。"""
    if not workspace_hint:
        return None

    try:
        ws_map = _list_workspace_storage_folders()
        entry = ws_map.get(workspace_hint.strip().lower())
        if not entry or not entry.get("state_db"):
            return None

        known_uuids = _get_known_brain_uuids()
        if not known_uuids:
            return None

        candidates = _find_conversation_ids_in_state_db(entry["state_db"], known_uuids)
        if len(candidates) != 1:
            # 0件(この版のAntigravityでは未対応の形式) or 複数件(過去の会話も含み曖昧)
            # のいずれの場合も、誤った紐付けをするよりは判定を諦める方が安全
            return None

        conv_id = candidates[0]
        home = os.path.expanduser("~")
        brain_root = os.path.join(home, ".gemini", "antigravity-ide", "brain")
        full_path = os.path.join(brain_root, conv_id, ".system_generated", "logs", "transcript_full.jsonl")
        light_path = os.path.join(brain_root, conv_id, ".system_generated", "logs", "transcript.jsonl")
        if os.path.exists(full_path):
            return full_path
        if os.path.exists(light_path):
            return light_path
        return None
    except Exception as e:
        logger.debug(f"resolve_transcript_by_workspace 失敗(フォールバックします): {e}")
        return None


def _make_instance_id(hwnd, pid, title):
    return hashlib.sha1(f"{hwnd}:{pid}:{title}".encode()).hexdigest()[:12]


def _get_foreground_ide_instance():
    """現在OSの最前面(フォーカスが当たっている)ウィンドウが許可リストに合致するIDEかどうかを判定し、
    合致すればそのinstance_idを返す。ユーザーがスマホを一切操作せずPCで普通にタイピングしているだけで
    この判定に必要な情報が揃う(=追加のPC操作やトークン消費なしで済む)。"""
    global IDE_INSTANCES, ALLOWED_TITLES

    try:
        fg_hwnd = user32.GetForegroundWindow()
    except Exception:
        return None
    if not fg_hwnd:
        return None

    length = user32.GetWindowTextLengthW(fg_hwnd)
    if length <= 0:
        return None
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(fg_hwnd, buffer, length + 1)
    title = buffer.value.strip()
    if not title:
        return None
    title_lower = title.lower()
    if not any(allowed in title_lower for allowed in ALLOWED_TITLES):
        return None

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(fg_hwnd, ctypes.byref(pid))
    instance_id = _make_instance_id(fg_hwnd, pid.value, title)

    # /api/ide_windows がまだ呼ばれていない(=フロント未起動)状態でも学習できるよう、
    # ここでも IDE_INSTANCES に登録しておく
    if instance_id not in IDE_INSTANCES:
        ide_type = "antigravity" if "antigravity" in title_lower else "vscode" if "code" in title_lower else "unknown"
        workspace_hint = title.split(" - ")[0] if " - " in title else title
        label = f"IDE - {workspace_hint}"
        if ide_type == "antigravity":
            label = f"Antigravity - {workspace_hint}"
        elif ide_type == "vscode":
            label = f"VSCode - {workspace_hint}"
        IDE_INSTANCES[instance_id] = {
            "instance_id": instance_id, "hwnd": fg_hwnd, "pid": pid.value,
            "ide_type": ide_type, "title": title, "label": label,
            "workspace_hint": workspace_hint, "is_foreground": True,
            "has_transcript": instance_id in INSTANCE_TO_TRANSCRIPT
        }

    return instance_id


def _transcript_auto_detect_tick():
    """1ティック分の自動検出処理。
    直前のtickからの間に「更新されたtranscriptファイルが1件だけ」で、かつ
    その間ずっと(あるいは今)特定のIDEウィンドウが最前面にあった場合、
    そのファイルをそのウィンドウ(インスタンス)に紐付ける。
    複数ファイルが同時に更新された場合は誤判定を避けるため何もしない。"""
    global _BG_LAST_SNAPSHOT, INSTANCE_TO_TRANSCRIPT, CLAIMED_TRANSCRIPT_PATHS

    fg_instance = _get_foreground_ide_instance()
    current_snapshot = snapshot_transcript_mtimes()

    if _BG_LAST_SNAPSHOT and fg_instance:
        changed_paths = []
        for path, (mtime, size) in current_snapshot.items():
            prev = _BG_LAST_SNAPSHOT.get(path)
            if prev is None:
                continue  # 新規に出現したファイルは今回は判定せず、次回以降のtickで判定する
            prev_mtime, prev_size = prev
            if mtime > prev_mtime or size != prev_size:
                changed_paths.append(path)

        if len(changed_paths) == 1:
            path = changed_paths[0]
            current_owner = None
            for inst, p in INSTANCE_TO_TRANSCRIPT.items():
                if p == path:
                    current_owner = inst
                    break

            if current_owner is None or current_owner == fg_instance:
                if INSTANCE_TO_TRANSCRIPT.get(fg_instance) != path:
                    INSTANCE_TO_TRANSCRIPT[fg_instance] = path
                    CLAIMED_TRANSCRIPT_PATHS.add(path)
                    logger.info(f"[自動検出] インスタンス {fg_instance} をログ '{path}' に自動で紐付けました。")
            else:
                # 既に別インスタンスに紐付いているファイル -> フォアグラウンド判定がズレている可能性が
                # あるため上書きはせず、警告のみ出す
                logger.debug(
                    f"[自動検出] '{path}' は既にインスタンス {current_owner} に紐付け済みのため、"
                    f"{fg_instance} への割当はスキップしました。"
                )
        elif len(changed_paths) > 1:
            logger.debug(f"[自動検出] 同一tickで複数ファイルが更新されたため今回は判定をスキップします: {changed_paths}")

    _BG_LAST_SNAPSHOT = current_snapshot


def _transcript_auto_detect_loop():
    logger.info("複数IDEタブの自動検出ループを開始しました。")
    while True:
        try:
            if AUTO_DETECT_ENABLED:
                _transcript_auto_detect_tick()
        except Exception as e:
            logger.error(f"自動検出ループでエラーが発生しました: {e}")
        time.sleep(AUTO_DETECT_INTERVAL_SEC)


@app.get("/api/ide_windows")
def api_ide_windows(x_bridge_token: str = Header(...)):
    check_token(x_bridge_token)
    global ACTIVE_INSTANCE_ID, IDE_INSTANCES, INSTANCE_TO_TRANSCRIPT, ALLOWED_TITLES
    
    # 設定ファイルを動的に再ロードして最新の設定を反映
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                curr_config = json.load(f)
                ALLOWED_TITLES = [t.lower() for t in curr_config.get("allowed_window_titles", [])]
    except Exception as e:
        logger.warning(f"設定ファイルのロードに失敗: {e}")
    
    windows = get_visible_windows()
    instances = []
    
    for hwnd, pid, title, exe in windows:
        title_lower = title.lower()
        if any(allowed in title_lower for allowed in ALLOWED_TITLES):
            instance_id = hashlib.sha1(f"{hwnd}:{pid}:{title}".encode()).hexdigest()[:12]
            
            ide_type = "antigravity" if "antigravity" in title_lower else "vscode" if "code" in title_lower else "unknown"
            workspace_hint = title.split(" - ")[0] if " - " in title else title
            label = f"IDE - {workspace_hint}"
            if ide_type == "antigravity":
                label = f"Antigravity - {workspace_hint}"
            elif ide_type == "vscode":
                label = f"VSCode - {workspace_hint}"

            # まだ紐付いていないタブは、書き込みを待たずに済む静的な方法(workspaceStorage)を
            # まず試す。取れなければ何もしない(バックグラウンドの書き込み監視が後で補完する)。
            if instance_id not in INSTANCE_TO_TRANSCRIPT:
                resolved_path = resolve_transcript_by_workspace(workspace_hint)
                if resolved_path:
                    INSTANCE_TO_TRANSCRIPT[instance_id] = resolved_path
                    CLAIMED_TRANSCRIPT_PATHS.add(resolved_path)
                    logger.info(f"[静的解決] インスタンス {instance_id} をログ '{resolved_path}' に紐付けました。(workspaceStorage方式)")

            inst = {
                "instance_id": instance_id,
                "hwnd": hwnd,
                "pid": pid,
                "ide_type": ide_type,
                "title": title,
                "label": label,
                "workspace_hint": workspace_hint,
                "is_foreground": False,
                "has_transcript": instance_id in INSTANCE_TO_TRANSCRIPT
            }
            instances.append(inst)
            IDE_INSTANCES[instance_id] = inst
            
    if not ACTIVE_INSTANCE_ID and instances:
        ACTIVE_INSTANCE_ID = instances[0]["instance_id"]
        
    return {"status": "success", "instances": instances, "active_instance_id": ACTIVE_INSTANCE_ID}

class SetActiveIdeRequest(BaseModel):
    instance_id: str

@app.post("/api/set_active_ide")
def api_set_active_ide(request: SetActiveIdeRequest, x_bridge_token: str = Header(...)):
    check_token(x_bridge_token)
    global ACTIVE_INSTANCE_ID, IDE_INSTANCES, INSTANCE_TO_TRANSCRIPT
    
    if request.instance_id not in IDE_INSTANCES:
        # Refresh windows to see if it exists now
        api_ide_windows(x_bridge_token)
        
    if request.instance_id not in IDE_INSTANCES:
        raise HTTPException(status_code=400, detail="Instance not found")
        
    ACTIVE_INSTANCE_ID = request.instance_id
    logger.info(f"Target IDE changed to instance: {ACTIVE_INSTANCE_ID}")
    
    return {
        "status": "success",
        "active_instance_id": ACTIVE_INSTANCE_ID,
        "has_linked_transcript": ACTIVE_INSTANCE_ID in INSTANCE_TO_TRANSCRIPT
    }

@app.post("/api/link_transcript")
def api_link_transcript(x_bridge_token: str = Header(...)):
    """紐付けフェーズ1: 現在の全transcriptファイルのmtime/sizeを記録し、
    対象タブで実際にメッセージを送ってもらうのを待つ状態にする。
    (ここで「一番新しいファイル」を即決めてしまうと、他のタブの活動と混同するため決めない)"""
    check_token(x_bridge_token)
    global ACTIVE_INSTANCE_ID, PENDING_LINK_SNAPSHOTS
    if not ACTIVE_INSTANCE_ID:
        raise HTTPException(status_code=400, detail="No active IDE instance to link.")

    snap = snapshot_transcript_mtimes()
    PENDING_LINK_SNAPSHOTS[ACTIVE_INSTANCE_ID] = snap
    logger.info(f"Link start for instance {ACTIVE_INSTANCE_ID}: snapshotted {len(snap)} transcript file(s).")

    return {
        "status": "waiting_for_activity",
        "active_instance_id": ACTIVE_INSTANCE_ID,
        "message": "対象のIDEタブのチャット欄で何かひとこと送信してから、確定ボタンを押してください。"
    }


@app.post("/api/link_transcript/confirm")
def api_link_transcript_confirm(x_bridge_token: str = Header(...)):
    """紐付けフェーズ2: フェーズ1以降にmtime/sizeが変化した(=実際に書き込みがあった)
    transcriptファイルを特定し、そのファイルを対象タブに紐付ける。
    他のタブが同時に会話していて複数ファイルが変化した場合は、
    まだ他インスタンスに紐付けられていないファイルの中から
    最も変化量(更新時刻の進み)が大きいものを採用する。"""
    check_token(x_bridge_token)
    global ACTIVE_INSTANCE_ID, INSTANCE_TO_TRANSCRIPT, PENDING_LINK_SNAPSHOTS, CLAIMED_TRANSCRIPT_PATHS

    if not ACTIVE_INSTANCE_ID:
        raise HTTPException(status_code=400, detail="No active IDE instance to link.")

    before = PENDING_LINK_SNAPSHOTS.get(ACTIVE_INSTANCE_ID)
    if before is None:
        raise HTTPException(status_code=400, detail="先に /api/link_transcript を呼び出してスナップショットを取得してください。")

    after = snapshot_transcript_mtimes()

    candidates = []
    for path, (mtime_after, size_after) in after.items():
        mtime_before, size_before = before.get(path, (0, 0))
        if mtime_after > mtime_before or size_after != size_before:
            # 既に他のアクティブなインスタンスに紐付け済みのファイルは除外
            # (自分自身の再紐付けは許可する)
            already_owned_by_other = any(
                p == path and inst != ACTIVE_INSTANCE_ID
                for inst, p in INSTANCE_TO_TRANSCRIPT.items()
            )
            if already_owned_by_other:
                continue
            delta = mtime_after - mtime_before
            candidates.append((delta, path))

    if not candidates:
        return {
            "status": "waiting_for_activity",
            "active_instance_id": ACTIVE_INSTANCE_ID,
            "message": "まだ変化が検出されていません。対象タブでメッセージを送信してから再度お試しください。"
        }

    # 更新が最も大きい(＝直近で最も活発に書き込まれた)ファイルを採用
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, chosen = candidates[0]

    if len(candidates) > 1:
        logger.warning(
            f"link_transcript_confirm: 複数の候補ファイルが変化していました({len(candidates)}件)。"
            f"最も更新量の大きい '{chosen}' を採用します。他のタブと同時に操作した場合は結果を確認してください。"
        )

    INSTANCE_TO_TRANSCRIPT[ACTIVE_INSTANCE_ID] = chosen
    CLAIMED_TRANSCRIPT_PATHS.add(chosen)
    PENDING_LINK_SNAPSHOTS.pop(ACTIVE_INSTANCE_ID, None)
    logger.info(f"Linked instance {ACTIVE_INSTANCE_ID} to transcript {chosen}")

    state = get_session_state()

    return {
        "status": "success",
        "linked_transcript": chosen,
        "active_instance_id": ACTIVE_INSTANCE_ID,
        "is_unlinked": state.get("is_unlinked", False),
        "ambiguous": len(candidates) > 1
    }

@app.post("/api/paste")
def api_paste(request: PasteRequest, x_bridge_token: str = Header(...)):
    check_token(x_bridge_token)
    
    # 設定ファイルを動的に再ロードして最新の設定を反映
    global ALLOWED_TITLES, SEND_KEY, FOCUS_KEY, ENABLE_AUTO_SEND, BG_COLOR_CHAT_OPENED, BG_COLOR_CHAT_CLOSED
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                curr_config = json.load(f)
                ALLOWED_TITLES = [t.lower() for t in curr_config.get("allowed_window_titles", [])]
                SEND_KEY = curr_config.get("send_key", "Enter")
                FOCUS_KEY = curr_config.get("focus_key", "None")
                ENABLE_AUTO_SEND = curr_config.get("enable_auto_send", False)
                
                opened_val = curr_config.get("bg_color_chat_opened", None)
                closed_val = curr_config.get("bg_color_chat_closed", None)
                BG_COLOR_CHAT_OPENED = tuple(opened_val) if opened_val else None
                BG_COLOR_CHAT_CLOSED = tuple(closed_val) if closed_val else None
    except Exception as e:
        logger.warning(f"設定ファイルの動的ロードに失敗しました (現在のメモリ設定を維持します): {e}")

    text = request.text
    action = request.action
    
    logger.info(f"リクエストを受信: action={action}, 文字数={len(text)}")
    
    # 1. クリップボードへの転送 (全アクション共通)
    try:
        pyperclip.copy(text)
        logger.info("テキストをPCクリップボードへコピーしました。")
    except Exception as e:
        logger.error(f"クリップボードの操作に失敗しました: {e}")
        raise HTTPException(status_code=500, detail=f"Clipboard error: {e}")
        
    if action == "copy":
        save_history(text)
        return {"status": "success", "message": "PCクリップボードにコピーしました。"}
        
    # 2. 対象ウィンドウの検索 (paste / paste_send の場合)
    windows = get_visible_windows()
    target_hwnd = None
    target_title = None
    
    # 許可されたウィンドウタイトルに一致するものを探す
    for hwnd, title in windows:
        title_lower = title.lower()
        for allowed in ALLOWED_TITLES:
            if allowed in title_lower:
                target_hwnd = hwnd
                target_title = title
                break
        if target_hwnd:
            break
            
    if not target_hwnd:
        logger.warning("許可リストに一致するアクティブウィンドウが見つかりません。")
        logger.warning(f"現在検出されているウィンドウ一覧 (全 {len(windows)} 件):")
        for hwnd, title in windows:
            logger.warning(f"  - [HWND: {hwnd}] タイトル: '{title}'")
        raise HTTPException(
            status_code=400, 
            detail="対象のウィンドウ (Antigravity IDE/VS Code等) がPC上で起動していないか、見つかりません。"
        )

        
    logger.info(f"対象ウィンドウを特定: HWND={target_hwnd}, タイトル='{target_title}'")
    
    # 3. ウィンドウを最前面化
    if not bring_window_to_front(target_hwnd):
        logger.error("ウィンドウのアクティブ化に失敗しました。")
        raise HTTPException(status_code=500, detail="Failed to activate target window.")
        
    # ウィンドウが前面に来るのを少し待つ (ミリ秒単位の待機)
    # 300ms ではフォーカスの遷移が完了しないことがあるため、長めに設定
    time.sleep(0.7)
    
    # 4. フォーカスキーの送信 (チャット入力欄等にフォーカスを強制移動させるため)
    if FOCUS_KEY and FOCUS_KEY.lower() != "none":
        try:
            keys = [k.strip().lower() for k in FOCUS_KEY.split("+")]
            
            # キャリブレーションデータが存在する場合のみ、開閉状態を判定する
            press_count = 1
            if BG_COLOR_CHAT_OPENED is not None and BG_COLOR_CHAT_CLOSED is not None:
                if is_chat_opened(target_hwnd):
                    logger.info("チャット欄はすでに開いていると判定されました。フォーカスを強制するため2回トグルします。")
                    press_count = 2
                else:
                    logger.info("チャット欄は閉じていると判定されました。1回トグルして開きます。")
            else:
                logger.info("キャリブレーション未設定のため、1回フォーカスキーを送信します。")
                
            for i in range(press_count):
                if i > 0:
                    time.sleep(0.2) # 2回押す場合の間隔
                pyautogui.hotkey(*keys)
                
            # チャット欄が展開してフォーカスが当たるのを少し長めに待つ
            time.sleep(0.4)
            logger.info(f"フォーカスキー {FOCUS_KEY} を {press_count} 回送信しました。")
        except Exception as e:
            logger.error(f"フォーカスキーの送信に失敗しました: {e}")

    # 5. 貼り付け (Ctrl + V)
    try:
        pyautogui.hotkey('ctrl', 'v')
        logger.info("Ctrl+V を送信して貼り付けを完了しました。")
    except Exception as e:
        logger.error(f"キー送信(Ctrl+V)に失敗しました: {e}")
        raise HTTPException(status_code=500, detail=f"Key injection error: {e}")
        
    # 5. 送信 (Ctrl+Enter / Enter) - paste_send の場合
    if action == "paste_send":
        if not ENABLE_AUTO_SEND:
            logger.warning("自動送信はサーバー設定で無効化されています。")
            raise HTTPException(status_code=400, detail="Auto-send is disabled in server config.")
            
        time.sleep(0.1)
        try:
            if SEND_KEY.lower() == "ctrl+enter":
                pyautogui.hotkey('ctrl', 'enter')
                logger.info("Ctrl+Enter を送信しました。")
            else:
                pyautogui.press('enter')
                logger.info("Enter を送信しました。")
        except Exception as e:
            logger.error(f"送信キーの実行に失敗しました: {e}")
            raise HTTPException(status_code=500, detail=f"Send key injection error: {e}")

    save_history(text)
    return {"status": "success", "message": "対象ウィンドウに貼り付けました。" + ("（自動送信を実行）" if action == "paste_send" else "")}

@app.post("/api/scan_question_screen")
def api_scan_question_screen(x_bridge_token: str = Header(...)):
    check_token(x_bridge_token)
    global LATEST_SCAN_RESULT
    
    # 対象ウィンドウの検索
    windows = get_visible_windows()
    target_hwnd = None
    for hwnd, title in windows:
        title_lower = title.lower()
        for allowed in ALLOWED_TITLES:
            if allowed in title_lower:
                target_hwnd = hwnd
                break
        if target_hwnd:
            break
            
    if not target_hwnd:
        logger.warning(f"Scan Question: 許可リストに一致するウィンドウが見つかりません。検出数={len(windows)}")
        for h, t in windows:
            logger.warning(f"  - [HWND: {h}] タイトル: '{t}'")
        return {"ok": False, "detail": "対象のIDEウィンドウが見つかりません。"}
        
    bring_window_to_front(target_hwnd)
    time.sleep(0.7)  # 前面化を待つ
    
    # === UI Automation での抽出を試みる ===
    result = extract_question_via_uia(target_hwnd)
    
    # === OCR フォールバック ===
    if not result:
        logger.info("UIAでの抽出に失敗したため、OCRフォールバックを実行します。")
        # asyncio.run() で非同期OCRを実行
        result = asyncio.run(extract_question_via_ocr(target_hwnd))
        
    if not result:
        return {"ok": False, "detail": "画面から質問UIを検出できませんでした。"}
        
    # スキャン結果をメモリに保存
    scan_id = f"screen_question_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    LATEST_SCAN_RESULT = {
        "question_id": scan_id,
        "timestamp": time.time(),
        "data": result
    }
    
    return {
        "ok": True,
        "question_id": scan_id,
        "question": result["question"],
        "options": [
            {"option_id": opt["id"], "text": opt["text"], "confidence": opt.get("confidence", 1.0)} 
            for opt in result["options"]
        ],
        "submit_available": result["submit_btn"] is not None,
        "method": result["method"]
    }

class SubmitScreenQuestionRequest(BaseModel):
    question_id: str
    option_id: str

@app.post("/api/submit_screen_question")
def api_submit_screen_question(request: SubmitScreenQuestionRequest, x_bridge_token: str = Header(...)):
    check_token(x_bridge_token)
    global LATEST_SCAN_RESULT
    
    if not LATEST_SCAN_RESULT:
        return {"ok": False, "detail": "スキャン結果が存在しません。再度スキャンしてください。"}
        
    if LATEST_SCAN_RESULT["question_id"] != request.question_id:
        return {"ok": False, "detail": "質問IDが一致しません。古い画面情報です。再度スキャンしてください。"}
        
    if time.time() - LATEST_SCAN_RESULT["timestamp"] > 120:
        return {"ok": False, "detail": "スキャンから2分以上経過しています。再度スキャンしてください。"}
        
    scan_data = LATEST_SCAN_RESULT["data"]
    
    # 対象の選択肢を探す
    target_option = None
    for opt in scan_data["options"]:
        if opt["id"] == request.option_id:
            target_option = opt
            break
            
    if not target_option:
        return {"ok": False, "detail": "指定された選択肢が見つかりません。"}
        
    submit_btn = scan_data["submit_btn"]
    if not submit_btn:
        return {"ok": False, "detail": "Submitボタンの座標が不明なためクリックできません。"}
        
    # ウィンドウの再検索と前面化
    windows = get_visible_windows()
    target_hwnd = None
    for hwnd, title in windows:
        title_lower = title.lower()
        for allowed in ALLOWED_TITLES:
            if allowed in title_lower:
                target_hwnd = hwnd
                break
        if target_hwnd:
            break
            
    if not target_hwnd:
        return {"ok": False, "detail": "IDEウィンドウが見つからなくなったためクリックできません。"}
        
    bring_window_to_front(target_hwnd)
    time.sleep(0.5)
    
    try:
        # 選択肢をクリック
        pyautogui.click(x=target_option["center_x"], y=target_option["center_y"])
        logger.info(f"選択肢 '{target_option['text']}' ({target_option['center_x']}, {target_option['center_y']}) をクリックしました。")
        time.sleep(0.5)
        
        # Submitボタンをクリック
        pyautogui.click(x=submit_btn["center_x"], y=submit_btn["center_y"])
        logger.info(f"Submitボタン ({submit_btn['center_x']}, {submit_btn['center_y']}) をクリックしました。")
        
        return {"ok": True}
    except Exception as e:
        logger.error(f"クリック実行中にエラーが発生しました: {e}")
        return {"ok": False, "detail": f"クリック実行エラー: {e}"}

# === 抽出ロジックの実装 ===
def is_in_ide_window(hwnd, x, y):
    rect = RECT()
    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (rect.left <= x <= rect.right) and (rect.top <= y <= rect.bottom)
    return False

def extract_question_via_uia(hwnd):
    """UI Automation を用いて質問を抽出します"""
    try:
        ctypes.windll.ole32.CoInitialize(None)
    except:
        pass
        
    try:
        ide_control = auto.ControlFromHandle(int(hwnd))
        if not ide_control:
            return None
            
        # ツリーを探索して、要素を集める (ここでは簡略化してテキストとラジオボタン風のものを探す)
        # 完全なツリー探索は遅いため、必要最小限にする
        question_text = ""
        options = []
        submit_btn = None
        
        # ※実際のAntigravityのHTML構造がUIAにどう映るかによりますが、
        # 一般的に "Submit" や "Proceed" ボタンを探し、その近くのラジオボタン・テキストを探します。
        
        buttons = []
        texts = []
        radio_buttons = []
        
        def walk(control):
            name = control.Name
            ct = control.ControlType
            if ct == auto.ControlType.ButtonControl:
                buttons.append(control)
                # radio っぽい動作のものもButtonかもしれない
            elif ct == auto.ControlType.TextControl:
                texts.append(control)
            elif ct == auto.ControlType.RadioButtonControl:
                radio_buttons.append(control)
                
            try:
                for child in control.GetChildren():
                    walk(child)
            except:
                pass
                
        walk(ide_control)
        
        # 1. Submitボタンの特定
        for b in buttons:
            if b.Name and ("Submit" in b.Name or "Proceed" in b.Name):
                submit_btn = b
                break
                
        if not submit_btn:
            logger.info("UIA: Submit/Proceedボタンが見つかりません。")
            return None
            
        submit_rect = submit_btn.BoundingRectangle
        if not submit_rect:
            return None
            
        s_cx = (submit_rect.left + submit_rect.right) // 2
        s_cy = (submit_rect.top + submit_rect.bottom) // 2
        
        if not is_in_ide_window(hwnd, s_cx, s_cy):
            return None
            
        # 2. 選択肢（RadioButtonControl等）の特定
        # 選択肢ボタンはおそらくSubmitボタンより上（Y座標が小さい）にあるはず
        for i, rb in enumerate(radio_buttons):
            rect = rb.BoundingRectangle
            if rect and rect.bottom < submit_rect.top:
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
                options.append({
                    "id": f"opt_uia_{i}",
                    "text": rb.Name if rb.Name else f"選択肢 {i+1}",
                    "center_x": cx,
                    "center_y": cy
                })
                
        # RadioButtonが無い場合は、単なるButtonから探すか？
        # Antigravityのask_questionの実装次第。現状はダミーまたは一部のみ対応。
        if not options:
            logger.info("UIA: 選択肢(RadioButton)が見つかりません。")
            return None
            
        # 3. 質問文の特定 (一番上にあるテキストなど)
        # 適当にSubmitボタンの近くの大きめのテキストを結合するなど
        question_text = "UI Automationで質問を抽出しました"
        
        return {
            "method": "uia",
            "question": question_text,
            "options": options,
            "submit_btn": {"center_x": s_cx, "center_y": s_cy}
        }
        
    except Exception as e:
        logger.error(f"UIA抽出中にエラー: {e}")
        return None

async def extract_question_via_ocr(hwnd):
    """Windows Media OCR を用いて質問を抽出します"""
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
        
    # ウィンドウの右側部分（チャットパネル）を切り出す
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    
    # mss でスクリーンショット
    with mss.mss() as sct:
        # ディスプレイ全体座標での切り出し
        # 念のためウィンドウ全体をキャプチャし、後で右側をクロップする
        monitor = {"top": rect.top, "left": rect.left, "width": width, "height": height}
        try:
            sct_img = sct.grab(monitor)
            img = np.array(sct_img)
        except Exception as e:
            logger.error(f"mss grab error: {e}")
            return None
            
    # 右側40%くらいがチャット欄と仮定
    crop_x = int(width * 0.6)
    chat_img = img[:, crop_x:]
    chat_global_offset_x = rect.left + crop_x
    chat_global_offset_y = rect.top
    
    # デバッグ用に画像を保存
    debug_dir = "logs/debug"
    os.makedirs(debug_dir, exist_ok=True)
    debug_filename = f"{debug_dir}/question_scan_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    cv2.imwrite(debug_filename, chat_img)
    
    # OpenCV (BGRA) -> SoftwareBitmap (RGBA)
    img_bgra = cv2.cvtColor(chat_img, cv2.COLOR_BGRA2RGBA)
    h, w, _ = img_bgra.shape
    software_bitmap = SoftwareBitmap(BitmapPixelFormat.RGBA8, w, h, BitmapAlphaMode.PREMULTIPLIED)
    software_bitmap.copy_from_buffer(memoryview(img_bgra.flatten()))
    
    engine = OcrEngine.try_create_from_language(Language("ja-JP"))
    if not engine:
        engine = OcrEngine.try_create_from_user_profile_languages()
        
    if not engine:
        logger.error("OCR Engine could not be created.")
        return None
        
    ocr_result = await engine.recognize_async(software_bitmap)
    
    if not ocr_result or not ocr_result.lines:
        logger.info("OCRでテキストが見つかりませんでした。")
        return None
        
    # 抽出ロジック（ヒューリスティック）
    # "Submit" や "Proceed" という単語を探す
    submit_btn = None
    options = []
    question_lines = []
    
    lines = ocr_result.lines
    for i, line in enumerate(lines):
        text = line.text
        # Submitボタン探し
        if "Submit" in text or "Proceed" in text or "送信" in text:
            # 最初の単語の矩形をボタンの中心とする
            rect_w = line.words[0].bounding_rect
            cx = int(rect_w.x + rect_w.width / 2) + chat_global_offset_x
            cy = int(rect_w.y + rect_w.height / 2) + chat_global_offset_y
            submit_btn = {"center_x": cx, "center_y": cy}
            
        # 選択肢探し（"1." "2." "○" "・" で始まる行などを選択肢とみなす）
        # 簡単のため、Submitボタンより上で、短めの行を選択肢候補とする
        elif len(text) < 50:
            rect_w = line.words[0].bounding_rect
            cx = int(rect_w.x + rect_w.width / 2) + chat_global_offset_x
            cy = int(rect_w.y + rect_w.height / 2) + chat_global_offset_y
            options.append({
                "id": f"opt_ocr_{i}",
                "text": text,
                "center_x": cx,
                "center_y": cy,
                "rect_y": rect_w.y # Y座標でソート用
            })
            
    # Submitボタンより下にある選択肢は除外
    if submit_btn:
        options = [o for o in options if o["center_y"] < submit_btn["center_y"]]
        
    # デバッグ画像に描画
    dbg_img = cv2.imread(debug_filename)
    if submit_btn:
        cv2.circle(dbg_img, (submit_btn["center_x"] - chat_global_offset_x, submit_btn["center_y"] - chat_global_offset_y), 5, (0, 0, 255), -1)
    for opt in options:
        cv2.circle(dbg_img, (opt["center_x"] - chat_global_offset_x, opt["center_y"] - chat_global_offset_y), 5, (0, 255, 0), -1)
    cv2.imwrite(debug_filename, dbg_img)
    
    if not options:
        logger.info("OCR: 選択肢候補が見つかりません。")
        return None
        
    # 質問文はOCR結果の上位にあるテキストと仮定
    question_text = "\n".join([line.text for line in lines[:3]])
    
    return {
        "method": "ocr",
        "question": question_text,
        "options": options,
        "submit_btn": submit_btn
    }

@app.get("/api/session_state")
def api_get_session_state(x_bridge_token: str = Header(...)):
    check_token(x_bridge_token)
    return get_session_state()

@app.post("/api/click_proceed")
def api_click_proceed(x_bridge_token: str = Header(...)):
    check_token(x_bridge_token)
    
    # ウィンドウの特定と前面化
    windows = get_visible_windows()
    target_hwnd = None
    for hwnd, title in windows:
        title_lower = title.lower()
        for allowed in ALLOWED_TITLES:
            if allowed in title_lower:
                target_hwnd = hwnd
                break
        if target_hwnd:
            break
            
    if not target_hwnd:
        raise HTTPException(status_code=400, detail="対象のウィンドウが見つかりません。")
        
    bring_window_to_front(target_hwnd)
    time.sleep(0.8)  # 前面化を待つ
    
    success = find_and_click_green_proceed_button()
    if success:
        return {"status": "success", "message": "PC側で Proceed ボタンを検出してクリックしました。"}
    else:
        raise HTTPException(status_code=404, detail="画面上に Proceed ボタン（緑色）が見つかりませんでした。")

@app.get("/api/chat_history")
def api_get_chat_history(x_bridge_token: str = Header(...)):
    check_token(x_bridge_token)
    return get_chat_history()

@app.get("/api/history")
def api_get_history(x_bridge_token: str = Header(...)):
    check_token(x_bridge_token)
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

@app.get("/api/config")
def api_get_config(x_bridge_token: str = Header(...)):
    check_token(x_bridge_token)
    return {
        "enable_auto_send": ENABLE_AUTO_SEND,
        "send_key": SEND_KEY,
        "has_calibration": (BG_COLOR_CHAT_OPENED is not None and BG_COLOR_CHAT_CLOSED is not None)
    }

# ---------- 生の会話ログ取得エンドポイント ----------
from fastapi.responses import PlainTextResponse

@app.get("/api/raw_transcript")
def api_raw_transcript(x_bridge_token: str = Header(...)):
    check_token(x_bridge_token)
    raw = get_raw_transcript()
    if not raw:
        raise HTTPException(status_code=404, detail="Transcript not found")
    return PlainTextResponse(content=raw, media_type="text/plain")

class CalibrateRequest(BaseModel):
    state: str  # "opened" / "closed"

@app.post("/api/calibrate")
def api_calibrate(request: CalibrateRequest, x_bridge_token: str = Header(...)):
    check_token(x_bridge_token)
    
    state = request.state
    if state not in ["opened", "closed"]:
        raise HTTPException(status_code=400, detail="Invalid state. Choose 'opened' or 'closed'.")
        
    # ウィンドウの特定
    windows = get_visible_windows()
    logger.info(f"キャリブレーション: 検出されたウィンドウ数={len(windows)}, 許可リスト={ALLOWED_TITLES}")
    target_hwnd = None
    for hwnd, title in windows:
        title_lower = title.lower()
        logger.info(f"  ウィンドウ: HWND={hwnd}, タイトル='{title}'")
        for allowed in ALLOWED_TITLES:
            if allowed in title_lower:
                target_hwnd = hwnd
                break
        if target_hwnd:
            break
            
    if not target_hwnd:
        logger.warning(f"キャリブレーション: 許可リストに一致するウィンドウが見つかりません。検出数={len(windows)}")
        raise HTTPException(status_code=400, detail=f"対象のウィンドウが見つかりません。検出されたウィンドウ数: {len(windows)}")
        
    # 最前面化
    bring_window_to_front(target_hwnd)
    time.sleep(0.5)  # 最前面化を待つ
    
    # 色サンプリング
    color = get_pixel_color_at_relative(target_hwnd)
    if not color:
        raise HTTPException(status_code=500, detail="ピクセルカラーの取得に失敗しました。")
        
    # 設定の更新と保存
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            curr_config = json.load(f)
            
        key_name = "bg_color_chat_opened" if state == "opened" else "bg_color_chat_closed"
        curr_config[key_name] = list(color)
        
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(curr_config, f, ensure_ascii=False, indent=2)
            
        logger.info(f"キャリブレーション完了: {state} の色を {color} に保存しました。")
        
        # グローバル変数も即時更新
        global BG_COLOR_CHAT_OPENED, BG_COLOR_CHAT_CLOSED
        if state == "opened":
            BG_COLOR_CHAT_OPENED = color
        else:
            BG_COLOR_CHAT_CLOSED = color
            
        return {"status": "success", "message": f"チャットが【{'開いている' if state == 'opened' else '閉じている'}】状態の背景色を登録しました。"}
    except Exception as e:
        logger.error(f"キャリブレーションデータの保存に失敗しました: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save calibration data: {e}")

def save_history(text: str):
    """送信されたテキストを履歴ファイルに保存します。"""
    history = []
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []
            
    # 重複防止（直近と同じテキストなら保存しない、または順番入れ替え）
    history = [h for h in history if h.get("text") != text]
    
    history.insert(0, {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "text": text
    })
    
    # 最大30件保存
    history = history[:30]
    
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"履歴の保存に失敗しました: {e}")

# ==================== 起動処理 ====================

if __name__ == "__main__":
    import uvicorn
    
    local_ip = get_local_ip()
    access_url = f"http://{local_ip}:{PORT}/?token={SECURITY_TOKEN}"
    
    print("\n" + "="*70)
    print("        Mobile Prompt Bridge MVP (Started)")
    print("="*70)
    print(f"\n[URL]\n-->  {access_url}\n")
    print("Please open the URL above on your mobile phone browser.")
    print("="*70)
    print("Security Alert:")
    print(f"- Random token ({SECURITY_TOKEN}) is integrated in the URL.")
    print("- Only accessible within the same local network.")
    print("- Press Ctrl + C to exit this server.")
    print("="*70 + "\n")

    
    # 0.0.0.0で待ち受け（LAN内の他デバイスからアクセスできるようにするため）
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
