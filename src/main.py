import os
import hashlib
import sys
import json
import time
import socket
import secrets
import logging
import shutil
import ctypes
import subprocess
import re
import collections
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
LAST_TRANSCRIPT_PATH = None
LAST_TRANSCRIPT_MTIME = 0
LAST_TRANSCRIPT_SIZE = 0
CACHED_SESSION_STATE = {"logs": [], "active_question": None}
CACHED_ACTIVE_QUESTION = None

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
    """現在アクティブかつ可視状態の全ウィンドウの (HWND, タイトル) のリストを返します。"""
    windows = []
    
    def enum_windows_callback(hwnd, lParam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                windows.append((hwnd, buffer.value))
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


def get_latest_transcript_path():
    """最も新しく更新された transcript_full.jsonl ファイルのパスを取得します。なければ transcript.jsonl にフォールバックします。"""
    home = os.path.expanduser("~")
    
    pattern_full = os.path.join(home, ".gemini", "antigravity-ide", "brain", "*", ".system_generated", "logs", "transcript_full.jsonl")
    files = glob.glob(pattern_full)
    
    if not files:
        pattern = os.path.join(home, ".gemini", "antigravity-ide", "brain", "*", ".system_generated", "logs", "transcript.jsonl")
        files = glob.glob(pattern)
        
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

def get_session_state():
    """最新の transcript_full.jsonl からログと回答待ちの質問を抽出します（キャッシュ機構付き）。"""
    global LAST_TRANSCRIPT_PATH, LAST_TRANSCRIPT_MTIME, LAST_TRANSCRIPT_SIZE
    global CACHED_SESSION_STATE, CACHED_ACTIVE_QUESTION
    
    path = get_latest_transcript_path()
    if not path or not os.path.exists(path):
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

