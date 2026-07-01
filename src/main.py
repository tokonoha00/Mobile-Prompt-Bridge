import os
import hashlib
import psutil
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
from winsdk.windows.media.ocr import OcrEngine
from winsdk.windows.globalization import Language
from winsdk.windows.graphics.imaging import SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode

# 繧ｰ繝ｭ繝ｼ繝舌Ν縺ｪ繧ｹ繧ｭ繝｣繝ｳ邨先棡菫晏ｭ倡畑
LATEST_SCAN_RESULT = None

# 謫堺ｽ懷ｯｾ雎｡縺ｮIDE繧ｦ繧｣繝ｳ繝峨え繝上Φ繝峨Ν
# 繝ｭ繧ｰ險ｭ螳・
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/bridge.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("PromptBridge")

# 繧､繝ｳ繧ｹ繧ｿ繝ｳ繧ｹ繝吶・繧ｹ邂｡逅・畑縺ｮ繧ｰ繝ｭ繝ｼ繝舌Ν螟画焚
ACTIVE_INSTANCE_ID = None
IDE_INSTANCES = {}
INSTANCE_TO_TRANSCRIPT = {}
CONFIG_PATH = "config.json"
TEMPLATE_PATH = "config.example.json"

if not os.path.exists(CONFIG_PATH):
    if os.path.exists(TEMPLATE_PATH):
        logger.info(f"config.json 縺瑚ｦ九▽縺九ｉ縺ｪ縺・◆繧√＋TEMPLATE_PATH} 縺九ｉ繧ｳ繝斐・縺励※逕滓・縺励∪縺吶・)
        shutil.copy(TEMPLATE_PATH, CONFIG_PATH)
    else:
        logger.error("險ｭ螳壹ユ繝ｳ繝励Ξ繝ｼ繝・config.example.json 縺悟ｭ伜惠縺励∪縺帙ｓ縲・)
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

# 繧ｻ繧ｭ繝･繝ｪ繝・ぅ繝医・繧ｯ繝ｳ縺ｮ蜿門ｾ励∪縺溘・逕滓・
SECURITY_TOKEN = config.get("security_token", "")
if not SECURITY_TOKEN:
    SECURITY_TOKEN = secrets.token_hex(8)
    logger.info(f"SECURITY_TOKEN: (逕滓・縺輔ｌ縺溘Λ繝ｳ繝繝繝医・繧ｯ繝ｳ) {SECURITY_TOKEN}")
else:
    logger.info(f"SECURITY_TOKEN: (險ｭ螳壹＆繧後◆蝗ｺ螳壹ヨ繝ｼ繧ｯ繝ｳ) {SECURITY_TOKEN}")

# 閭梧勹濶ｲ繧ｭ繝｣繝ｪ繝悶Ξ繝ｼ繧ｷ繝ｧ繝ｳ繝・・繧ｿ縺ｮ繝ｭ繝ｼ繝・
BG_COLOR_CHAT_OPENED = config.get("bg_color_chat_opened", None)
BG_COLOR_CHAT_CLOSED = config.get("bg_color_chat_closed", None)
if BG_COLOR_CHAT_OPENED:
    BG_COLOR_CHAT_OPENED = tuple(BG_COLOR_CHAT_OPENED)
if BG_COLOR_CHAT_CLOSED:
    BG_COLOR_CHAT_CLOSED = tuple(BG_COLOR_CHAT_CLOSED)

# 繧ｭ繝｣繝・す繝･逕ｨ繧ｰ繝ｭ繝ｼ繝舌Ν螟画焚
LAST_TRANSCRIPT_PATH = None
LAST_TRANSCRIPT_MTIME = 0
LAST_TRANSCRIPT_SIZE = 0
CACHED_SESSION_STATE = {"logs": [], "active_question": None}
CACHED_ACTIVE_QUESTION = None

# 螻･豁ｴ繝輔ぃ繧､繝ｫ繝代せ
HISTORY_PATH = "history.json"

# ==================== Windows API (ctypes) 螳夂ｾｩ ====================
user32 = ctypes.windll.user32

# 繧ｳ繝ｼ繝ｫ繝舌ャ繧ｯ蝙九・螳夂ｾｩ
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

# 繧ｦ繧｣繝ｳ繝峨え蠎ｧ讓吝叙蠕礼畑縺ｮRECT讒矩菴薙→API螳夂ｾｩ
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
    """迴ｾ蝨ｨ繧｢繧ｯ繝・ぅ繝悶°縺､蜿ｯ隕也憾諷九・蜈ｨ繧ｦ繧｣繝ｳ繝峨え縺ｮ繝｡繧ｿ繝・・繧ｿ繧定ｿ斐＠縺ｾ縺吶・""
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
    """謖・ｮ壹＆繧後◆ HWND 縺ｮ繧ｦ繧｣繝ｳ繝峨え繧呈怙蜑埼擇蛹厄ｼ医い繧ｯ繝・ぅ繝門喧・峨＠縺ｾ縺吶・""
    hwnd = wintypes.HWND(int(hwnd))
    # 譛蟆丞喧迥ｶ諷九・蝣ｴ蜷医・蜈・↓謌ｻ縺・
    # SW_RESTORE = 9, SW_SHOW = 5
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
    else:
        user32.ShowWindow(hwnd, 5)
        
    # Windows縺ｮ繝輔か繝ｼ繧ｫ繧ｹ螂ｪ蜿夜亟豁｢讖溯・繧偵ヰ繧､繝代せ縺吶ｋ縺溘ａ縲、lt繧ｭ繝ｼ遨ｺ謇薙■繧帝∽ｿ｡
    # VK_MENU = 18 (Alt), KEYEVENTF_KEYUP = 2
    user32.keybd_event(18, 0, 0, 0)
    user32.keybd_event(18, 0, 2, 0)
    
    # 譛蜑埼擇蛹・
    return user32.SetForegroundWindow(hwnd)

# ==================== 繝阪ャ繝医Ρ繝ｼ繧ｯ繝ｦ繝ｼ繝・ぅ繝ｪ繝・ぅ ====================
def get_local_ip():
    """PC縺ｮLAN蜀・Ο繝ｼ繧ｫ繝ｫIP繧｢繝峨Ξ繧ｹ繧貞叙蠕励＠縺ｾ縺吶・""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 螳滄圀縺ｫ繝代こ繝・ヨ縺ｯ騾√ｉ縺壹√Ν繝ｼ繝域､懃ｴ｢縺ｮ縺ｿ
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

# ==================== 繝斐け繧ｻ繝ｫ隗｣譫・(繧ｭ繝｣繝ｪ繝悶Ξ繝ｼ繧ｷ繝ｧ繝ｳ逕ｨ) ====================
def get_pixel_color_at_relative(hwnd, rel_x_from_right=150, rel_y_from_top=300):
    """謖・ｮ壹＆繧後◆繧ｦ繧｣繝ｳ繝峨え縺ｮ逶ｸ蟇ｾ菴咲ｽｮ (蜿ｳ縺九ｉX px, 荳翫°繧浦 px) 縺ｮ逕ｻ髱｢繝斐け繧ｻ繝ｫRGB濶ｲ繧貞叙蠕励＠縺ｾ縺吶・""
    hwnd = wintypes.HWND(int(hwnd))
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        logger.error("GetWindowRect 縺ｫ螟ｱ謨励＠縺ｾ縺励◆縲・)
        return None
    
    # 邨ｶ蟇ｾ蠎ｧ讓吶・險育ｮ・
    abs_x = rect.right - rel_x_from_right
    abs_y = rect.top + rel_y_from_top
    
    # 逕ｻ髱｢隗｣蜒丞ｺｦ蜀・↓蜿弱ａ繧・
    try:
        screen_width, screen_height = pyautogui.size()
        abs_x = max(0, min(abs_x, screen_width - 1))
        abs_y = max(0, min(abs_y, screen_height - 1))
        
        # 繝斐け繧ｻ繝ｫ繧ｫ繝ｩ繝ｼ縺ｮ蜿門ｾ・
        color = pyautogui.pixel(abs_x, abs_y)
        logger.info(f"繝斐け繧ｻ繝ｫ繧ｫ繝ｩ繝ｼ蜿門ｾ・ 逶ｸ蟇ｾ (蜿ｳ-{rel_x_from_right}, 荳・{rel_y_from_top}) -> 邨ｶ蟇ｾ ({abs_x}, {abs_y}) = {color}")
        return color # (R, G, B) 縺ｮ繧ｿ繝励Ν
    except Exception as e:
        logger.error(f"繝斐け繧ｻ繝ｫ繧ｫ繝ｩ繝ｼ縺ｮ蜿門ｾ励↓螟ｱ謨励＠縺ｾ縺励◆: {e}")
        return None

def color_distance(c1, c2):
    """2縺､縺ｮRGB濶ｲ縺ｮ霍晞屬繧定ｨ育ｮ励＠縺ｾ縺吶・""
    if not c1 or not c2:
        return 9999
    return ((c1[0] - c2[0])**2 + (c1[1] - c2[1])**2 + (c1[2] - c2[2])**2) ** 0.5

def is_chat_opened(hwnd):
    """繧ｭ繝｣繝ｪ繝悶Ξ繝ｼ繧ｷ繝ｧ繝ｳ蛟､繧堤畑縺・※縲∫樟蝨ｨ繝√Ε繝・ヨ谺・′髢九＞縺ｦ縺・ｋ縺句愛螳壹＠縺ｾ縺吶・""
    global BG_COLOR_CHAT_OPENED, BG_COLOR_CHAT_CLOSED
    if BG_COLOR_CHAT_OPENED is None or BG_COLOR_CHAT_CLOSED is None:
        logger.info("繧ｭ繝｣繝ｪ繝悶Ξ繝ｼ繧ｷ繝ｧ繝ｳ繝・・繧ｿ縺御ｸ崎ｶｳ縺励※縺・ｋ縺溘ａ縲√ョ繝輔か繝ｫ繝医〒縲朱哩縺倥※縺・ｋ縲上→蛻､螳壹＠縺ｾ縺吶・)
        return False
        
    current_color = get_pixel_color_at_relative(hwnd)
    if not current_color:
        return False
        
    dist_opened = color_distance(current_color, BG_COLOR_CHAT_OPENED)
    dist_closed = color_distance(current_color, BG_COLOR_CHAT_CLOSED)
    
    logger.info(f"濶ｲ蟾ｮ蛻､螳・ 髢九＞縺ｦ縺・ｋ迥ｶ諷九→縺ｮ蟾ｮ={dist_opened:.1f}, 髢峨§縺ｦ縺・ｋ迥ｶ諷九→縺ｮ蟾ｮ={dist_closed:.1f}")
    
    # 髢ｾ蛟､ 35 莉･蜀・〒縲√°縺､縲碁幕縺・※縺・ｋ濶ｲ縲阪↓霑代＞蝣ｴ蜷・
    if dist_opened < dist_closed and dist_opened < 35:
        return True
    return False


def get_latest_transcript_path():
    global ACTIVE_INSTANCE_ID, INSTANCE_TO_TRANSCRIPT, IDE_INSTANCES
    
    if ACTIVE_INSTANCE_ID and ACTIVE_INSTANCE_ID in INSTANCE_TO_TRANSCRIPT:
        mapped = INSTANCE_TO_TRANSCRIPT[ACTIVE_INSTANCE_ID]
        if os.path.exists(mapped):
            return mapped
            
    # 邏舌▼縺代′縺ｪ縺・ｴ蜷医・繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ蛻ｶ蠕｡
    instances_count = len([inst for inst in IDE_INSTANCES.values() if any(allowed in inst["title"].lower() for allowed in ALLOWED_TITLES)])
    if instances_count >= 2:
        # 隍・焚IDE縺後≠繧句ｴ蜷医・縲∵悴邏舌▼縺代〒蜍晄焔縺ｫ譛譁ｰ繧定ｿ斐＆縺ｪ縺・
        logger.warning(f"Multiple IDEs detected ({instances_count}), blocking fallback to newest transcript for unlinked instance {ACTIVE_INSTANCE_ID}.")
        return None
        
    home = os.path.expanduser("~")
    pattern_full = os.path.join(home, ".gemini", "antigravity-ide", "brain", "*", ".system_generated", "logs", "transcript_full.jsonl")
    files = glob.glob(pattern_full)
    if not files:
        files = glob.glob(os.path.join(home, ".gemini", "antigravity-ide", "brain", "*", ".system_generated", "logs", "transcript.jsonl"))
        
    if not files:
        return None
        
    files.sort(key=os.path.getmtime, reverse=True)
    return files[0]

def get_chat_history():
    """荳倶ｽ堺ｺ呈鋤諤ｧ縺ｮ縺溘ａ縲“et_session_state()縺ｮ繝ｭ繧ｰ驛ｨ蛻・・縺ｿ繧定ｿ斐＠縺ｾ縺吶・""
    state = get_session_state()
    return state.get("logs", [])

def get_session_state():
    """譛譁ｰ縺ｮ transcript_full.jsonl 縺九ｉ繝ｭ繧ｰ縺ｨ蝗樒ｭ泌ｾ・■縺ｮ雉ｪ蝠上ｒ謚ｽ蜃ｺ縺励∪縺呻ｼ医く繝｣繝・す繝･讖滓ｧ倶ｻ倥″・峨・""
    global LAST_TRANSCRIPT_PATH, LAST_TRANSCRIPT_MTIME, LAST_TRANSCRIPT_SIZE
    global CACHED_SESSION_STATE, CACHED_ACTIVE_QUESTION
    
    path = get_latest_transcript_path()
    if not path or not os.path.exists(path if path else ""):
        # 譛ｪ邏舌▼縺代√∪縺溘・繝輔ぃ繧､繝ｫ縺悟ｭ伜惠縺励↑縺・ｴ蜷医・遨ｺ縺ｮ迥ｶ諷九ｒ霑斐☆
        empty_state = {"logs": [], "active_question": None, "is_unlinked": True}
        return empty_state
        
    try:
        stat = os.stat(path)
        mtime = stat.st_mtime
        size = stat.st_size
    except Exception as e:
        logger.error(f"Failed to stat {path}: {e}")
        return CACHED_SESSION_STATE
        
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
        logger.error(f"螻･豁ｴ繝輔ぃ繧､繝ｫ縺ｮ隱ｭ縺ｿ霎ｼ縺ｿ縺ｫ螟ｱ謨励＠縺ｾ縺励◆: {e}")
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
                    text += "\n\n**縲舌ち繧ｹ繧ｯ莠亥ｮ壹・*"
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
                summary_text = "繧ｳ繝槭Φ繝牙ｮ溯｡後′螳御ｺ・＠縺ｾ縺励◆縲・
                if "failed with exit code" in content:
                    summary_text = "笞・・繧ｳ繝槭Φ繝牙ｮ溯｡後′繧ｨ繝ｩ繝ｼ邨ゆｺ・＠縺ｾ縺励◆縲・
                preview = content[:200] + ("..." if len(content) > 200 else "")
                chat_logs.append({"sender": "system", "text": f"**[繧ｳ繝槭Φ繝牙ｮ溯｡珪** {summary_text}\n```\n{preview.strip()}\n```", "timestamp": created_at})
            elif step_type == "VIEW_FILE":
                lines = content.split("\n")
                file_path = "繝輔ぃ繧､繝ｫ"
                for l in lines:
                    if "File Path:" in l:
                        file_path = l.replace("File Path:", "").strip(" `")
                        break
                chat_logs.append({"sender": "system", "text": f"唐 繝輔ぃ繧､繝ｫ繧帝夢隕ｧ縺励∪縺励◆:\n`{file_path}`", "timestamp": created_at})
            elif step_type == "GREP_SEARCH":
                preview = content[:200] + ("..." if len(content) > 200 else "")
                chat_logs.append({"sender": "system", "text": f"剥 繝・く繧ｹ繝域､懃ｴ｢繧貞ｮ溯｡後＠縺ｾ縺励◆縲・n{preview.strip()}", "timestamp": created_at})
            elif step_type == "LIST_DIRECTORY":
                preview = content[:200] + ("..." if len(content) > 200 else "")
                chat_logs.append({"sender": "system", "text": f"刀 繝・ぅ繝ｬ繧ｯ繝医Μ荳隕ｧ繧定｡ｨ遉ｺ縺励∪縺励◆縲・n{preview.strip()}", "timestamp": created_at})
            elif step_type == "SEARCH_WEB":
                preview = content[:200] + ("..." if len(content) > 200 else "")
                chat_logs.append({"sender": "system", "text": f"倹 繧ｦ繧ｧ繝匁､懃ｴ｢繧貞ｮ溯｡後＠縺ｾ縺励◆縲・n{preview.strip()}", "timestamp": created_at})
            elif step_type == "ASK_QUESTION":
                ans = ""
                for l in content.split("\n"):
                    if l.startswith("A") and ":" in l:
                        ans = l.split(":", 1)[1].strip()
                        break
                if not ans:
                    ans = "蝗樒ｭ泌ｮ御ｺ・
                chat_logs.append({"sender": "system", "text": f"町 雉ｪ蝠上↓蝗樒ｭ斐＠縺ｾ縺励◆: **{ans}**", "timestamp": created_at})
            elif step_type == "ERROR_MESSAGE":
                preview = content[:200] + ("..." if len(content) > 200 else "")
                chat_logs.append({"sender": "system", "text": f"笶・繧ｨ繝ｩ繝ｼ縺檎匱逕溘＠縺ｾ縺励◆:\n{preview.strip()}", "timestamp": created_at})

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
    """UI Automation繧剃ｽｿ逕ｨ縺励※Proceed繝懊ち繝ｳ繧呈､懃ｴ｢縺励∪縺吶・""
    try:
        # 繧ｹ繝ｬ繝・ラ迺ｰ蠅・〒縺ｮCOM繧ｨ繝ｩ繝ｼ繧帝亟豁｢縺吶ｋ縺溘ａ縺ｫCOM繧貞・譛溷喧
        try:
            ctypes.windll.ole32.CoInitialize(None)
        except Exception:
            pass
        # HWND 縺九ｉ UI Automation 隕∫ｴ繧貞叙蠕・
        ide_control = auto.ControlFromHandle(int(hwnd))
        if not ide_control:
            logger.warning("UI Automation: HWND 縺九ｉ繧ｳ繝ｳ繝医Ο繝ｼ繝ｫ繧貞叙蠕励〒縺阪∪縺帙ｓ縺ｧ縺励◆縲・)
            return None
        
        buttons = []
        
        def walk_and_find(control):
            name = control.Name
            # ControlType 縺・ButtonControl 縺ｧ縲¨ame 縺ｫ "Proceed" 縺ｾ縺溘・ "Submit" 縺悟性縺ｾ繧後ｋ縺狗｢ｺ隱・
            if control.ControlType == auto.ControlType.ButtonControl and name and ("Proceed" in name or "Submit" in name):
                buttons.append(control)
            try:
                for child in control.GetChildren():
                    walk_and_find(child)
            except Exception:
                pass
                
        walk_and_find(ide_control)
        
        # Submit 繝懊ち繝ｳ繧呈怙蜆ｪ蜈医√↑縺代ｌ縺ｰ Proceed 繝懊ち繝ｳ繧呈爾縺・
        submit_buttons = [b for b in buttons if b.Name and "Submit" in b.Name]
        proceed_buttons = [b for b in buttons if b.Name and "Proceed" in b.Name]
        
        target_btn = None
        if submit_buttons:
            submit_buttons.sort(key=lambda b: b.BoundingRectangle.bottom if b.BoundingRectangle else 0, reverse=True)
            target_btn = submit_buttons[0]
            logger.info(f"UI Automation: 'Submit' 繝懊ち繝ｳ繧貞━蜈域､懷・縺励∪縺励◆縲ょ錐蜑・ '{target_btn.Name}'")
        elif proceed_buttons:
            proceed_buttons.sort(key=lambda b: b.BoundingRectangle.bottom if b.BoundingRectangle else 0, reverse=True)
            target_btn = proceed_buttons[0]
            logger.info(f"UI Automation: 'Proceed' 繝懊ち繝ｳ繧呈､懷・縺励∪縺励◆縲ょ錐蜑・ '{target_btn.Name}'")
            
        if not target_btn:
            logger.info("UI Automation: 'Submit' 縺ｾ縺溘・ 'Proceed' 繝懊ち繝ｳ縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縺ｧ縺励◆縲・)
            return None
        
        rect = target_btn.BoundingRectangle
        if not rect:
            logger.warning("UI Automation: 繝懊ち繝ｳ縺ｮ BoundingRectangle 縺悟叙蠕励〒縺阪∪縺帙ｓ縺ｧ縺励◆縲・)
            return None
            
        # 繝懊ち繝ｳ縺ｮ荳ｭ蠢・ｺｧ讓・
        center_x = (rect.left + rect.right) // 2
        center_y = (rect.top + rect.bottom) // 2
        
        # IDE繧ｦ繧｣繝ｳ繝峨え縺ｮ遽・峇蜀・°繝√ぉ繝・け
        ide_rect = RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(ide_rect)):
            if not (ide_rect.left <= center_x <= ide_rect.right and ide_rect.top <= center_y <= ide_rect.bottom):
                logger.warning(f"UI Automation: 讀懷・縺輔ｌ縺溘・繧ｿ繝ｳ蠎ｧ讓・({center_x}, {center_y}) 縺・IDE 繧ｦ繧｣繝ｳ繝峨え縺ｮ遽・峇螟悶〒縺吶・)
                return None
                
        logger.info(f"UI Automation: 'Proceed' 繝懊ち繝ｳ繧呈､懷・縺励∪縺励◆縲ょ錐蜑・ '{target_btn.Name}', 蠎ｧ讓・ ({center_x}, {center_y})")
        return (center_x, center_y)
    except Exception as e:
        logger.error(f"UI Automation 螳溯｡御ｸｭ縺ｫ繧ｨ繝ｩ繝ｼ縺檎匱逕溘＠縺ｾ縺励◆: {e}")
        return None


def find_via_image_processing(hwnd):
    """OpenCV縺ｫ繧医ｋ鬮伜ｺｦ縺ｪ逕ｻ蜒剰ｧ｣譫舌〒Proceed繝懊ち繝ｳ繧呈､懃ｴ｢縺励∪縺呻ｼ・I Automation縺ｮ繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ・峨・""
    try:
        # 繧｢繧ｯ繝・ぅ繝悶え繧｣繝ｳ繝峨え縺ｮ遽・峇繧貞叙蠕・
        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            logger.error("GetWindowRect 縺ｫ螟ｱ謨励＠縺ｾ縺励◆縲・)
            return None
            
        # 讀懃ｴ｢繧ｨ繝ｪ繧｢繧偵え繧｣繝ｳ繝峨え縺ｮ蜿ｳ蛛ｴ 800px 縺ｫ諡｡蠑ｵ縺励∽ｸ贋ｸ狗ｫｯ繧帝勁螟・
        # 荳狗ｫｯ髯､螟悶・15px縺ｫ邵ｮ蟆上＠縺ｦ繝懊ち繝ｳ縺碁勁螟悶＆繧後↑縺・ｈ縺・↓縺吶ｋ
        search_left = max(rect.left, rect.right - 800)
        search_right = rect.right
        search_top = rect.top + 50
        search_bottom = rect.bottom - 15
        
        # 逕ｻ髱｢蜈ｨ菴薙・繧ｹ繧ｯ繝ｪ繝ｼ繝ｳ繧ｷ繝ｧ繝・ヨ繧呈聴蠖ｱ
        scr_path = "temp_proceed_scr.png"
        pyautogui.screenshot(scr_path)
        
        # OpenCV縺ｧ隱ｭ縺ｿ霎ｼ繧
        img_bgr = cv2.imread(scr_path)
        if img_bgr is None:
            logger.error("OpenCV 縺ｧ縺ｮ繧ｹ繧ｯ繝ｪ繝ｼ繝ｳ繧ｷ繝ｧ繝・ヨ隱ｭ縺ｿ霎ｼ縺ｿ縺ｫ螟ｱ謨励＠縺ｾ縺励◆縲・)
            if os.path.exists(scr_path):
                os.remove(scr_path)
            return None
            
        h_img, w_img, _ = img_bgr.shape
        
        # 讀懃ｴ｢蠎ｧ讓吶ｒ逕ｻ蜒冗ｯ・峇蜀・↓蜿弱ａ繧・
        search_left = max(0, min(search_left, w_img - 1))
        search_right = max(0, min(search_right, w_img - 1))
        search_top = max(0, min(search_top, h_img - 1))
        search_bottom = max(0, min(search_bottom, h_img - 1))
        
        # 繝√Ε繝・ヨ繧ｨ繝ｪ繧｢縺ｮ蛻・ｊ蜃ｺ縺・(ROI)
        chat_roi = img_bgr[search_top:search_bottom, search_left:search_right]
        h_roi, w_roi, _ = chat_roi.shape
        
        # 繝槭せ繧ｯ逕ｻ蜒上・菴懈・ (RGB遽・峇: R: 10縲・5, G: 80縲・40, B: 80縲・40 縺ｧG縺ｨB縺ｮ豈皮紫縺瑚ｿ代＞)
        mask = np.zeros((h_roi, w_roi), dtype=np.uint8)
        for y in range(h_roi):
            for x in range(w_roi):
                b, g, r = chat_roi[y, x][:3]
                if (10 <= r <= 45) and (80 <= g <= 140) and (80 <= b <= 140):
                    if (g > 0 and 0.8 <= b / g <= 1.25) and (g > r * 1.5):
                        mask[y, x] = 255
                        
        # 繝｢繝ｫ繝輔か繝ｭ繧ｸ繝ｼ貍皮ｮ・(Morphology Open/Close) 縺ｧ繝弱う繧ｺ髯､蜴ｻ
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask_cleaned = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask_cleaned = cv2.morphologyEx(mask_cleaned, cv2.MORPH_OPEN, kernel)
        
        # 8霑大ｍ騾｣邨先・蛻・・謚ｽ蜃ｺ
        num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(mask_cleaned, connectivity=8)
        
        candidates = []
        for i in range(1, num_labels):
            x_c, y_c, w_c, h_c, area = stats[i]
            
            aspect_ratio = w_c / h_c if h_c > 0 else 0
            fill_ratio = area / (w_c * h_c) if (w_c * h_c) > 0 else 0
            
            # 繧ｯ繝ｭ繝・・縺励※逋ｽ譁・ｭ励ヴ繧ｯ繧ｻ繝ｫ・・, G, B 縺吶∋縺ｦ200莉･荳奇ｼ峨ｒ繧ｫ繧ｦ繝ｳ繝・
            roi_crop = chat_roi[y_c:y_c+h_c, x_c:x_c+w_c]
            white_count = 0
            for cy in range(roi_crop.shape[0]):
                for cx in range(roi_crop.shape[1]):
                    cb, cg, cr = roi_crop[cy, cx][:3]
                    if cr > 200 and cg > 200 and cb > 200:
                        white_count += 1
                        
            white_ratio = white_count / area if area > 0 else 0
            
            # 繧ｹ繧ｳ繧｢縺ｮ險育ｮ・(蛻晄悄蛟､ 1.0)
            score = 1.0
            
            # 1. 繧ｵ繧､繧ｺ蛻ｶ髯・ 蟷・30-120px, 鬮倥＆ 18-45px
            if not (30 <= w_c <= 120):
                score -= 0.4
            if not (18 <= h_c <= 45):
                score -= 0.4
            if w_c >= 150 or h_c >= 60:
                score = 0
                
            # 2. 邵ｦ讓ｪ豈・ 1.5 - 5.0
            if not (1.5 <= aspect_ratio <= 5.0):
                score -= 0.3
                
            # 3. 蝪励ｊ縺､縺ｶ縺怜ｯ・ｺｦ: 0.35 - 0.95
            if not (0.35 <= fill_ratio <= 0.95):
                score -= 0.3
                
            # 4. 逋ｽ譁・ｭ励・譛臥┌・医ユ繧ｭ繧ｹ繝医′蟄伜惠縺吶ｋ縺具ｼ・
            if 0.02 <= white_ratio <= 0.25:
                score += 0.2
            else:
                score -= 0.2
                
            # 5. 菴咲ｽｮ繧ｹ繧ｳ繧｢・医メ繝｣繝・ヨ繝代ロ繝ｫ蜀・・蜿ｳ遶ｯ縺九ｉ蟾ｦ蟇・ｊ縲∽ｸ斐▽荳句ｯ・ｊ繧貞━蜈茨ｼ・
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
                f"逕ｻ蜒剰ｪ崎ｭ伜呵｣・{i}: x={x_c+search_left}, y={y_c+search_top}, w={w_c}, h={h_c}, "
                f"area={area}, fill_ratio={fill_ratio:.2f}, aspect={aspect_ratio:.2f}, "
                f"white_ratio={white_ratio:.3f}, score={score:.2f}"
            )
            
        # 繝・ヰ繝・げ逕ｻ蜒上・菴懈・
        debug_img = img_bgr.copy()
        cv2.rectangle(debug_img, (search_left, search_top), (search_right, search_bottom), (255, 0, 0), 2)
        
        for c in candidates:
            cv2.rectangle(debug_img, (c['x'], c['y']), (c['x'] + c['w'], c['y'] + c['h']), (0, 165, 255), 2)
            cv2.putText(debug_img, f"S:{c['score']:.2f}", (c['x'], c['y'] - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 165, 255), 1)
            
        # 譛牙柑縺ｪ蛟呵｣懊・繝輔ぅ繝ｫ繧ｿ・医せ繧ｳ繧｢ 0.6 莉･荳奇ｼ・
        valid_candidates = [c for c in candidates if c['score'] >= 0.6]
        
        selected_cand = None
        if len(valid_candidates) == 1:
            selected_cand = valid_candidates[0]
            logger.info(f"逕ｻ蜒剰ｪ崎ｭ・ 繧ｹ繧ｳ繧｢蝓ｺ貅悶ｒ貅縺溘☆蜚ｯ荳縺ｮ譛牙柑縺ｪ蛟呵｣・(ID {selected_cand['id']}, score={selected_cand['score']:.2f}) 繧呈治逕ｨ縲・)
        elif len(valid_candidates) > 1:
            valid_candidates.sort(key=lambda c: c['score'], reverse=True)
            best = valid_candidates[0]
            second = valid_candidates[1]
            # 繧ｹ繧ｳ繧｢蟾ｮ縺・0.1 莉･荳翫≠繧後・謗｡逕ｨ
            if best['score'] - second['score'] >= 0.1:
                selected_cand = best
                logger.info(f"逕ｻ蜒剰ｪ崎ｭ・ 繧ｹ繧ｳ繧｢譛螟ｧ蛟呵｣・(ID {best['id']}, score={best['score']:.2f}) 繧呈治逕ｨ (2逡ｪ逶ｮ縺ｨ縺ｮ蟾ｮ {best['score'] - second['score']:.2f})縲・)
            else:
                logger.warning(f"逕ｻ蜒剰ｪ崎ｭ・ 隍・焚蛟呵｣懊・繧ｹ繧ｳ繧｢蟾ｮ縺悟ｰ上＆縺・◆繧∝愛螳壹ｒ繧ｹ繧ｭ繝・・ (best={best['score']:.2f}, second={second['score']:.2f})縲・)
                
        click_pos = None
        if selected_cand:
            if selected_cand['w'] < 130 and selected_cand['h'] < 50:
                click_pos = (int(selected_cand['centroid'][0]), int(selected_cand['centroid'][1]))
                cv2.circle(debug_img, click_pos, 5, (0, 0, 255), -1)
                cv2.putText(debug_img, "CLICK", (click_pos[0] + 10, click_pos[1] + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
                
        # 繝・ヰ繝・げ逕ｻ蜒上・菫晏ｭ・
        os.makedirs("debug", exist_ok=True)
        now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_path = f"debug/proceed_detection_{now_str}.png"
        cv2.imwrite(debug_path, debug_img)
        logger.info(f"繝・ヰ繝・げ逕ｨ逕ｻ蜒上ｒ菫晏ｭ倥＠縺ｾ縺励◆: proceed_detection_{now_str}.png")
            
        if os.path.exists(scr_path):
            os.remove(scr_path)
            
        return click_pos
    except Exception as e:
        logger.error(f"逕ｻ蜒丞・逅・〒繧ｨ繝ｩ繝ｼ縺檎匱逕溘＠縺ｾ縺励◆: {e}")
        return None


def find_and_click_green_proceed_button():
    """Proceed繝懊ち繝ｳ繧呈､懷・縺励※繧ｯ繝ｪ繝・け縺励∪縺吶ゅ∪縺啅I Automation繧定ｩｦ縺励∝､ｱ謨励☆繧後・逕ｻ蜒剰ｪ崎ｭ倥↓繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ縺励∪縺吶・""
    try:
        hwnd = user32.GetForegroundWindow()
        
        # 1. UI Automation 縺ｧ縺ｮ讀懷・繧定ｩｦ縺ｿ繧・
        logger.info("Proceed 繝懊ち繝ｳ讀懷・繝輔ぉ繝ｼ繧ｺ: UI Automation 繧定ｩｦ縺ｿ縺ｾ縺・..")
        click_pos = find_via_uiautomation(hwnd)
        
        # 2. UI Automation 縺ｧ隕九▽縺九ｉ縺ｪ縺九▲縺溷ｴ蜷医・逕ｻ蜒剰ｪ崎ｭ倥↓繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ
        if not click_pos:
            logger.info("UI Automation 縺ｧ隕九▽縺九ｉ縺ｪ縺九▲縺溘◆繧√∫判蜒丞・逅・↓繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ縺励∪縺・..")
            click_pos = find_via_image_processing(hwnd)
            
        if not click_pos:
            logger.warning("Proceed 繝懊ち繝ｳ縺梧､懷・縺輔ｌ縺ｾ縺帙ｓ縺ｧ縺励◆縲・)
            return False
            
        # 3. 讀懷・縺輔ｌ縺溷ｺｧ讓吶ｒ繧ｯ繝ｪ繝・け
        logger.info(f"Proceed 繝懊ち繝ｳ繧貞ｺｧ讓・({click_pos[0]}, {click_pos[1]}) 縺ｫ縺ｦ繧ｯ繝ｪ繝・け縺励∪縺吶・)
        pyautogui.moveTo(click_pos[0], click_pos[1], duration=0.3)
        time.sleep(0.1)
        pyautogui.mouseDown()
        time.sleep(0.1)
        pyautogui.mouseUp()
        return True
    except Exception as e:
        logger.error(f"Proceed 繝懊ち繝ｳ縺ｮ繧ｯ繝ｪ繝・け蜃ｦ逅・ｸｭ縺ｫ閾ｴ蜻ｽ逧・↑繧ｨ繝ｩ繝ｼ縺檎匱逕溘＠縺ｾ縺励◆: {e}")
        return False


# ==================== FastAPI 繧｢繝励Μ繧ｱ繝ｼ繧ｷ繝ｧ繝ｳ ====================
app = FastAPI(title="Mobile Prompt Bridge API")

class PasteRequest(BaseModel):
    text: str
    action: str  # "copy" / "paste" / "paste_send"

class SetTargetWindowRequest(BaseModel):
    hwnd: int

# ---------- 莨夊ｩｱ繝ｭ繧ｰ蜿門ｾ礼畑繝倥Ν繝代・ ----------
def get_raw_transcript() -> str:
    """譛譁ｰ縺ｮ transcript.jsonl 繧呈枚蟄怜・縺ｨ縺励※霑斐☆縲・""
    path = get_latest_transcript_path()
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.error(f"Transcript 縺ｮ隱ｭ縺ｿ霎ｼ縺ｿ縺ｫ螟ｱ謨励＠縺ｾ縺励◆: {e}")
        return ""

class SubmitAnswerRequest(BaseModel):
    key: str
    question_id: str

# 繝医・繧ｯ繝ｳ隱崎ｨｼ髢｢謨ｰ
def check_token(token: str):
    if token != SECURITY_TOKEN:
        logger.warning(f"荳肴ｭ｣縺ｪ繝医・繧ｯ繝ｳ縺ｧ縺ｮ繧｢繧ｯ繧ｻ繧ｹ隧ｦ陦後′縺ゅｊ縺ｾ縺励◆: {token}")
        raise HTTPException(status_code=403, detail="Forbidden: Invalid token")

@app.get("/")
def get_index(token: str = Query(None)):
    check_token(token)
    # web/index.html 繧定ｿ斐☆
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
        logger.error(f"HTML繝輔ぃ繧､繝ｫ縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ: {html_path}")
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

@app.get("/api/ide_windows")
def api_ide_windows(x_bridge_token: str = Header(...)):
    check_token(x_bridge_token)
    global ACTIVE_INSTANCE_ID, IDE_INSTANCES, INSTANCE_TO_TRANSCRIPT
    
    windows = get_visible_windows()
    instances = []
    
    logger.info(f"Checking {len(windows)} visible windows against ALLOWED_TITLES: {ALLOWED_TITLES}")
    
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
    global ACTIVE_INSTANCE_ID
    ACTIVE_INSTANCE_ID = request.instance_id
    logger.info(f"Target IDE changed to instance: {ACTIVE_INSTANCE_ID}")
    return {"status": "success"}

@app.post("/api/link_transcript")
def api_link_transcript(x_bridge_token: str = Header(...)):
    check_token(x_bridge_token)
    global ACTIVE_INSTANCE_ID, INSTANCE_TO_TRANSCRIPT
    if not ACTIVE_INSTANCE_ID:
        raise HTTPException(status_code=400, detail="No active IDE instance to link.")
        
    home = os.path.expanduser("~")
    pattern_full = os.path.join(home, ".gemini", "antigravity-ide", "brain", "*", ".system_generated", "logs", "transcript_full.jsonl")
    files = glob.glob(pattern_full)
    if not files:
        files = glob.glob(os.path.join(home, ".gemini", "antigravity-ide", "brain", "*", ".system_generated", "logs", "transcript.jsonl"))
        
    if not files:
        raise HTTPException(status_code=404, detail="No transcripts found to link.")
        
    files.sort(key=os.path.getmtime, reverse=True)
    newest = files[0]
    INSTANCE_TO_TRANSCRIPT[ACTIVE_INSTANCE_ID] = newest
    logger.info(f"Linked instance {ACTIVE_INSTANCE_ID} to transcript {newest}")
    return {"status": "success", "linked_transcript": newest}

@app.post("/api/paste")
def api_paste(request: PasteRequest, x_bridge_token: str = Header(...)):
    check_token(x_bridge_token)
    
    # 險ｭ螳壹ヵ繧｡繧､繝ｫ繧貞虚逧・↓蜀阪Ο繝ｼ繝峨＠縺ｦ譛譁ｰ縺ｮ險ｭ螳壹ｒ蜿肴丐
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
        logger.warning(f"險ｭ螳壹ヵ繧｡繧､繝ｫ縺ｮ蜍慕噪繝ｭ繝ｼ繝峨↓螟ｱ謨励＠縺ｾ縺励◆ (迴ｾ蝨ｨ縺ｮ繝｡繝｢繝ｪ險ｭ螳壹ｒ邯ｭ謖√＠縺ｾ縺・: {e}")

    text = request.text
    action = request.action
    
    logger.info(f"繝ｪ繧ｯ繧ｨ繧ｹ繝医ｒ蜿嶺ｿ｡: action={action}, 譁・ｭ玲焚={len(text)}")
    
    # 1. 繧ｯ繝ｪ繝・・繝懊・繝峨∈縺ｮ霆｢騾・(蜈ｨ繧｢繧ｯ繧ｷ繝ｧ繝ｳ蜈ｱ騾・
    try:
        pyperclip.copy(text)
        logger.info("繝・く繧ｹ繝医ｒPC繧ｯ繝ｪ繝・・繝懊・繝峨∈繧ｳ繝斐・縺励∪縺励◆縲・)
    except Exception as e:
        logger.error(f"繧ｯ繝ｪ繝・・繝懊・繝峨・謫堺ｽ懊↓螟ｱ謨励＠縺ｾ縺励◆: {e}")
        raise HTTPException(status_code=500, detail=f"Clipboard error: {e}")
        
    if action == "copy":
        save_history(text)
        return {"status": "success", "message": "PC繧ｯ繝ｪ繝・・繝懊・繝峨↓繧ｳ繝斐・縺励∪縺励◆縲・}
        
    # 2. 蟇ｾ雎｡繧ｦ繧｣繝ｳ繝峨え縺ｮ讀懃ｴ｢
    windows = get_visible_windows()
    target_hwnd, target_title = get_active_target_hwnd(windows)
            
    if not target_hwnd:
        logger.warning("險ｱ蜿ｯ繝ｪ繧ｹ繝医↓荳閾ｴ縺吶ｋ繧｢繧ｯ繝・ぅ繝悶え繧｣繝ｳ繝峨え縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縲・)
        logger.warning(f"迴ｾ蝨ｨ讀懷・縺輔ｌ縺ｦ縺・ｋ繧ｦ繧｣繝ｳ繝峨え荳隕ｧ (蜈ｨ {len(windows)} 莉ｶ):")
        for hwnd, title in windows:
            logger.warning(f"  - [HWND: {hwnd}] 繧ｿ繧､繝医Ν: '{title}'")
        raise HTTPException(
            status_code=400, 
            detail="蟇ｾ雎｡縺ｮ繧ｦ繧｣繝ｳ繝峨え (Antigravity IDE/VS Code遲・ 縺訓C荳翫〒襍ｷ蜍輔＠縺ｦ縺・↑縺・°縲∬ｦ九▽縺九ｊ縺ｾ縺帙ｓ縲・
        )

        
    logger.info(f"蟇ｾ雎｡繧ｦ繧｣繝ｳ繝峨え繧堤音螳・ HWND={target_hwnd}, 繧ｿ繧､繝医Ν='{target_title}'")
    
    # 3. 繧ｦ繧｣繝ｳ繝峨え繧呈怙蜑埼擇蛹・
    if not bring_window_to_front(target_hwnd):
        logger.error("繧ｦ繧｣繝ｳ繝峨え縺ｮ繧｢繧ｯ繝・ぅ繝門喧縺ｫ螟ｱ謨励＠縺ｾ縺励◆縲・)
        raise HTTPException(status_code=500, detail="Failed to activate target window.")
        
    # 繧ｦ繧｣繝ｳ繝峨え縺悟燕髱｢縺ｫ譚･繧九・繧貞ｰ代＠蠕・▽ (繝溘Μ遘貞腰菴阪・蠕・ｩ・
    # 300ms 縺ｧ縺ｯ繝輔か繝ｼ繧ｫ繧ｹ縺ｮ驕ｷ遘ｻ縺悟ｮ御ｺ・＠縺ｪ縺・％縺ｨ縺後≠繧九◆繧√・聞繧√↓險ｭ螳・
    time.sleep(0.7)
    
    # 4. 繝輔か繝ｼ繧ｫ繧ｹ繧ｭ繝ｼ縺ｮ騾∽ｿ｡ (繝√Ε繝・ヨ蜈･蜉帶ｬ・ｭ峨↓繝輔か繝ｼ繧ｫ繧ｹ繧貞ｼｷ蛻ｶ遘ｻ蜍輔＆縺帙ｋ縺溘ａ)
    if FOCUS_KEY and FOCUS_KEY.lower() != "none":
        try:
            keys = [k.strip().lower() for k in FOCUS_KEY.split("+")]
            
            # 繧ｭ繝｣繝ｪ繝悶Ξ繝ｼ繧ｷ繝ｧ繝ｳ繝・・繧ｿ縺悟ｭ伜惠縺吶ｋ蝣ｴ蜷医・縺ｿ縲・幕髢臥憾諷九ｒ蛻､螳壹☆繧・
            press_count = 1
            if BG_COLOR_CHAT_OPENED is not None and BG_COLOR_CHAT_CLOSED is not None:
                if is_chat_opened(target_hwnd):
                    logger.info("繝√Ε繝・ヨ谺・・縺吶〒縺ｫ髢九＞縺ｦ縺・ｋ縺ｨ蛻､螳壹＆繧後∪縺励◆縲ゅヵ繧ｩ繝ｼ繧ｫ繧ｹ繧貞ｼｷ蛻ｶ縺吶ｋ縺溘ａ2蝗槭ヨ繧ｰ繝ｫ縺励∪縺吶・)
                    press_count = 2
                else:
                    logger.info("繝√Ε繝・ヨ谺・・髢峨§縺ｦ縺・ｋ縺ｨ蛻､螳壹＆繧後∪縺励◆縲・蝗槭ヨ繧ｰ繝ｫ縺励※髢九″縺ｾ縺吶・)
            else:
                logger.info("繧ｭ繝｣繝ｪ繝悶Ξ繝ｼ繧ｷ繝ｧ繝ｳ譛ｪ險ｭ螳壹・縺溘ａ縲・蝗槭ヵ繧ｩ繝ｼ繧ｫ繧ｹ繧ｭ繝ｼ繧帝∽ｿ｡縺励∪縺吶・)
                
            for i in range(press_count):
                if i > 0:
                    time.sleep(0.2) # 2蝗樊款縺吝ｴ蜷医・髢馴囈
                pyautogui.hotkey(*keys)
                
            # 繝√Ε繝・ヨ谺・′螻暮幕縺励※繝輔か繝ｼ繧ｫ繧ｹ縺悟ｽ薙◆繧九・繧貞ｰ代＠髟ｷ繧√↓蠕・▽
            time.sleep(0.4)
            logger.info(f"繝輔か繝ｼ繧ｫ繧ｹ繧ｭ繝ｼ {FOCUS_KEY} 繧・{press_count} 蝗樣∽ｿ｡縺励∪縺励◆縲・)
        except Exception as e:
            logger.error(f"繝輔か繝ｼ繧ｫ繧ｹ繧ｭ繝ｼ縺ｮ騾∽ｿ｡縺ｫ螟ｱ謨励＠縺ｾ縺励◆: {e}")

    # 5. 雋ｼ繧贋ｻ倥￠ (Ctrl + V)
    try:
        pyautogui.hotkey('ctrl', 'v')
        logger.info("Ctrl+V 繧帝∽ｿ｡縺励※雋ｼ繧贋ｻ倥￠繧貞ｮ御ｺ・＠縺ｾ縺励◆縲・)
    except Exception as e:
        logger.error(f"繧ｭ繝ｼ騾∽ｿ｡(Ctrl+V)縺ｫ螟ｱ謨励＠縺ｾ縺励◆: {e}")
        raise HTTPException(status_code=500, detail=f"Key injection error: {e}")
        
    # 5. 騾∽ｿ｡ (Ctrl+Enter / Enter) - paste_send 縺ｮ蝣ｴ蜷・
    if action == "paste_send":
        if not ENABLE_AUTO_SEND:
            logger.warning("閾ｪ蜍暮∽ｿ｡縺ｯ繧ｵ繝ｼ繝舌・險ｭ螳壹〒辟｡蜉ｹ蛹悶＆繧後※縺・∪縺吶・)
            raise HTTPException(status_code=400, detail="Auto-send is disabled in server config.")
            
        time.sleep(0.1)
        try:
            if SEND_KEY.lower() == "ctrl+enter":
                pyautogui.hotkey('ctrl', 'enter')
                logger.info("Ctrl+Enter 繧帝∽ｿ｡縺励∪縺励◆縲・)
            else:
                pyautogui.press('enter')
                logger.info("Enter 繧帝∽ｿ｡縺励∪縺励◆縲・)
        except Exception as e:
            logger.error(f"騾∽ｿ｡繧ｭ繝ｼ縺ｮ螳溯｡後↓螟ｱ謨励＠縺ｾ縺励◆: {e}")
            raise HTTPException(status_code=500, detail=f"Send key injection error: {e}")

    save_history(text)
    return {"status": "success", "message": "蟇ｾ雎｡繧ｦ繧｣繝ｳ繝峨え縺ｫ雋ｼ繧贋ｻ倥￠縺ｾ縺励◆縲・ + ("・郁・蜍暮∽ｿ｡繧貞ｮ溯｡鯉ｼ・ if action == "paste_send" else "")}

@app.post("/api/scan_question_screen")
def api_scan_question_screen(x_bridge_token: str = Header(...)):
    check_token(x_bridge_token)
    global LATEST_SCAN_RESULT
    
    # 蟇ｾ雎｡繧ｦ繧｣繝ｳ繝峨え縺ｮ讀懃ｴ｢
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
        logger.warning(f"Scan Question: 險ｱ蜿ｯ繝ｪ繧ｹ繝医↓荳閾ｴ縺吶ｋ繧ｦ繧｣繝ｳ繝峨え縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縲よ､懷・謨ｰ={len(windows)}")
        for h, t in windows:
            logger.warning(f"  - [HWND: {h}] 繧ｿ繧､繝医Ν: '{t}'")
        return {"ok": False, "detail": "蟇ｾ雎｡縺ｮIDE繧ｦ繧｣繝ｳ繝峨え縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縲・}
        
    bring_window_to_front(target_hwnd)
    time.sleep(0.7)  # 蜑埼擇蛹悶ｒ蠕・▽
    
    # === UI Automation 縺ｧ縺ｮ謚ｽ蜃ｺ繧定ｩｦ縺ｿ繧・===
    result = extract_question_via_uia(target_hwnd)
    
    # === OCR 繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ ===
    if not result:
        logger.info("UIA縺ｧ縺ｮ謚ｽ蜃ｺ縺ｫ螟ｱ謨励＠縺溘◆繧√＾CR繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ繧貞ｮ溯｡後＠縺ｾ縺吶・)
        # asyncio.run() 縺ｧ髱槫酔譛欅CR繧貞ｮ溯｡・
        result = asyncio.run(extract_question_via_ocr(target_hwnd))
        
    if not result:
        return {"ok": False, "detail": "逕ｻ髱｢縺九ｉ雉ｪ蝠酋I繧呈､懷・縺ｧ縺阪∪縺帙ｓ縺ｧ縺励◆縲・}
        
    # 繧ｹ繧ｭ繝｣繝ｳ邨先棡繧偵Γ繝｢繝ｪ縺ｫ菫晏ｭ・
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
        return {"ok": False, "detail": "繧ｹ繧ｭ繝｣繝ｳ邨先棡縺悟ｭ伜惠縺励∪縺帙ｓ縲ょ・蠎ｦ繧ｹ繧ｭ繝｣繝ｳ縺励※縺上□縺輔＞縲・}
        
    if LATEST_SCAN_RESULT["question_id"] != request.question_id:
        return {"ok": False, "detail": "雉ｪ蝠終D縺御ｸ閾ｴ縺励∪縺帙ｓ縲ょ商縺・判髱｢諠・ｱ縺ｧ縺吶ょ・蠎ｦ繧ｹ繧ｭ繝｣繝ｳ縺励※縺上□縺輔＞縲・}
        
    if time.time() - LATEST_SCAN_RESULT["timestamp"] > 120:
        return {"ok": False, "detail": "繧ｹ繧ｭ繝｣繝ｳ縺九ｉ2蛻・ｻ･荳顔ｵ碁℃縺励※縺・∪縺吶ょ・蠎ｦ繧ｹ繧ｭ繝｣繝ｳ縺励※縺上□縺輔＞縲・}
        
    scan_data = LATEST_SCAN_RESULT["data"]
    
    # 蟇ｾ雎｡縺ｮ驕ｸ謚櫁い繧呈爾縺・
    target_option = None
    for opt in scan_data["options"]:
        if opt["id"] == request.option_id:
            target_option = opt
            break
            
    if not target_option:
        return {"ok": False, "detail": "謖・ｮ壹＆繧後◆驕ｸ謚櫁い縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縲・}
        
    submit_btn = scan_data["submit_btn"]
    if not submit_btn:
        return {"ok": False, "detail": "Submit繝懊ち繝ｳ縺ｮ蠎ｧ讓吶′荳肴・縺ｪ縺溘ａ繧ｯ繝ｪ繝・け縺ｧ縺阪∪縺帙ｓ縲・}
        
    # 繧ｦ繧｣繝ｳ繝峨え縺ｮ蜀肴､懃ｴ｢縺ｨ蜑埼擇蛹・
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
        return {"ok": False, "detail": "IDE繧ｦ繧｣繝ｳ繝峨え縺瑚ｦ九▽縺九ｉ縺ｪ縺上↑縺｣縺溘◆繧√け繝ｪ繝・け縺ｧ縺阪∪縺帙ｓ縲・}
        
    bring_window_to_front(target_hwnd)
    time.sleep(0.5)
    
    try:
        # 驕ｸ謚櫁い繧偵け繝ｪ繝・け
        pyautogui.click(x=target_option["center_x"], y=target_option["center_y"])
        logger.info(f"驕ｸ謚櫁い '{target_option['text']}' ({target_option['center_x']}, {target_option['center_y']}) 繧偵け繝ｪ繝・け縺励∪縺励◆縲・)
        time.sleep(0.5)
        
        # Submit繝懊ち繝ｳ繧偵け繝ｪ繝・け
        pyautogui.click(x=submit_btn["center_x"], y=submit_btn["center_y"])
        logger.info(f"Submit繝懊ち繝ｳ ({submit_btn['center_x']}, {submit_btn['center_y']}) 繧偵け繝ｪ繝・け縺励∪縺励◆縲・)
        
        return {"ok": True}
    except Exception as e:
        logger.error(f"繧ｯ繝ｪ繝・け螳溯｡御ｸｭ縺ｫ繧ｨ繝ｩ繝ｼ縺檎匱逕溘＠縺ｾ縺励◆: {e}")
        return {"ok": False, "detail": f"繧ｯ繝ｪ繝・け螳溯｡後お繝ｩ繝ｼ: {e}"}

# === 謚ｽ蜃ｺ繝ｭ繧ｸ繝・け縺ｮ螳溯｣・===
def is_in_ide_window(hwnd, x, y):
    rect = RECT()
    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (rect.left <= x <= rect.right) and (rect.top <= y <= rect.bottom)
    return False

def extract_question_via_uia(hwnd):
    """UI Automation 繧堤畑縺・※雉ｪ蝠上ｒ謚ｽ蜃ｺ縺励∪縺・""
    try:
        ctypes.windll.ole32.CoInitialize(None)
    except:
        pass
        
    try:
        ide_control = auto.ControlFromHandle(int(hwnd))
        if not ide_control:
            return None
            
        # 繝・Μ繝ｼ繧呈爾邏｢縺励※縲∬ｦ∫ｴ繧帝寔繧√ｋ (縺薙％縺ｧ縺ｯ邁｡逡･蛹悶＠縺ｦ繝・く繧ｹ繝医→繝ｩ繧ｸ繧ｪ繝懊ち繝ｳ鬚ｨ縺ｮ繧ゅ・繧呈爾縺・
        # 螳悟・縺ｪ繝・Μ繝ｼ謗｢邏｢縺ｯ驕・＞縺溘ａ縲∝ｿ・ｦ∵怙蟆城剞縺ｫ縺吶ｋ
        question_text = ""
        options = []
        submit_btn = None
        
        # 窶ｻ螳滄圀縺ｮAntigravity縺ｮHTML讒矩縺袈IA縺ｫ縺ｩ縺・丐繧九°縺ｫ繧医ｊ縺ｾ縺吶′縲・
        # 荳闊ｬ逧・↓ "Submit" 繧・"Proceed" 繝懊ち繝ｳ繧呈爾縺励√◎縺ｮ霑代￥縺ｮ繝ｩ繧ｸ繧ｪ繝懊ち繝ｳ繝ｻ繝・く繧ｹ繝医ｒ謗｢縺励∪縺吶・
        
        buttons = []
        texts = []
        radio_buttons = []
        
        def walk(control):
            name = control.Name
            ct = control.ControlType
            if ct == auto.ControlType.ButtonControl:
                buttons.append(control)
                # radio 縺｣縺ｽ縺・虚菴懊・繧ゅ・繧・utton縺九ｂ縺励ｌ縺ｪ縺・
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
        
        # 1. Submit繝懊ち繝ｳ縺ｮ迚ｹ螳・
        for b in buttons:
            if b.Name and ("Submit" in b.Name or "Proceed" in b.Name):
                submit_btn = b
                break
                
        if not submit_btn:
            logger.info("UIA: Submit/Proceed繝懊ち繝ｳ縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縲・)
            return None
            
        submit_rect = submit_btn.BoundingRectangle
        if not submit_rect:
            return None
            
        s_cx = (submit_rect.left + submit_rect.right) // 2
        s_cy = (submit_rect.top + submit_rect.bottom) // 2
        
        if not is_in_ide_window(hwnd, s_cx, s_cy):
            return None
            
        # 2. 驕ｸ謚櫁い・・adioButtonControl遲会ｼ峨・迚ｹ螳・
        # 驕ｸ謚櫁い繝懊ち繝ｳ縺ｯ縺翫◎繧峨￥Submit繝懊ち繝ｳ繧医ｊ荳奇ｼ・蠎ｧ讓吶′蟆上＆縺・ｼ峨↓縺ゅｋ縺ｯ縺・
        for i, rb in enumerate(radio_buttons):
            rect = rb.BoundingRectangle
            if rect and rect.bottom < submit_rect.top:
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
                options.append({
                    "id": f"opt_uia_{i}",
                    "text": rb.Name if rb.Name else f"驕ｸ謚櫁い {i+1}",
                    "center_x": cx,
                    "center_y": cy
                })
                
        # RadioButton縺檎┌縺・ｴ蜷医・縲∝腰縺ｪ繧毅utton縺九ｉ謗｢縺吶°・・
        # Antigravity縺ｮask_question縺ｮ螳溯｣・ｬ｡隨ｬ縲ら樟迥ｶ縺ｯ繝繝溘・縺ｾ縺溘・荳驛ｨ縺ｮ縺ｿ蟇ｾ蠢懊・
        if not options:
            logger.info("UIA: 驕ｸ謚櫁い(RadioButton)縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縲・)
            return None
            
        # 3. 雉ｪ蝠乗枚縺ｮ迚ｹ螳・(荳逡ｪ荳翫↓縺ゅｋ繝・く繧ｹ繝医↑縺ｩ)
        # 驕ｩ蠖薙↓Submit繝懊ち繝ｳ縺ｮ霑代￥縺ｮ螟ｧ縺阪ａ縺ｮ繝・く繧ｹ繝医ｒ邨仙粋縺吶ｋ縺ｪ縺ｩ
        question_text = "UI Automation縺ｧ雉ｪ蝠上ｒ謚ｽ蜃ｺ縺励∪縺励◆"
        
        return {
            "method": "uia",
            "question": question_text,
            "options": options,
            "submit_btn": {"center_x": s_cx, "center_y": s_cy}
        }
        
    except Exception as e:
        logger.error(f"UIA謚ｽ蜃ｺ荳ｭ縺ｫ繧ｨ繝ｩ繝ｼ: {e}")
        return None

async def extract_question_via_ocr(hwnd):
    """Windows Media OCR 繧堤畑縺・※雉ｪ蝠上ｒ謚ｽ蜃ｺ縺励∪縺・""
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
        
    # 繧ｦ繧｣繝ｳ繝峨え縺ｮ蜿ｳ蛛ｴ驛ｨ蛻・ｼ医メ繝｣繝・ヨ繝代ロ繝ｫ・峨ｒ蛻・ｊ蜃ｺ縺・
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    
    # mss 縺ｧ繧ｹ繧ｯ繝ｪ繝ｼ繝ｳ繧ｷ繝ｧ繝・ヨ
    with mss.mss() as sct:
        # 繝・ぅ繧ｹ繝励Ξ繧､蜈ｨ菴灘ｺｧ讓吶〒縺ｮ蛻・ｊ蜃ｺ縺・
        # 蠢ｵ縺ｮ縺溘ａ繧ｦ繧｣繝ｳ繝峨え蜈ｨ菴薙ｒ繧ｭ繝｣繝励メ繝｣縺励∝ｾ後〒蜿ｳ蛛ｴ繧偵け繝ｭ繝・・縺吶ｋ
        monitor = {"top": rect.top, "left": rect.left, "width": width, "height": height}
        try:
            sct_img = sct.grab(monitor)
            img = np.array(sct_img)
        except Exception as e:
            logger.error(f"mss grab error: {e}")
            return None
            
    # 蜿ｳ蛛ｴ40%縺上ｉ縺・′繝√Ε繝・ヨ谺・→莉ｮ螳・
    crop_x = int(width * 0.6)
    chat_img = img[:, crop_x:]
    chat_global_offset_x = rect.left + crop_x
    chat_global_offset_y = rect.top
    
    # 繝・ヰ繝・げ逕ｨ縺ｫ逕ｻ蜒上ｒ菫晏ｭ・
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
        logger.info("OCR縺ｧ繝・く繧ｹ繝医′隕九▽縺九ｊ縺ｾ縺帙ｓ縺ｧ縺励◆縲・)
        return None
        
    # 謚ｽ蜃ｺ繝ｭ繧ｸ繝・け・医ヲ繝･繝ｼ繝ｪ繧ｹ繝・ぅ繝・け・・
    # "Submit" 繧・"Proceed" 縺ｨ縺・≧蜊倩ｪ槭ｒ謗｢縺・
    submit_btn = None
    options = []
    question_lines = []
    
    lines = ocr_result.lines
    for i, line in enumerate(lines):
        text = line.text
        # Submit繝懊ち繝ｳ謗｢縺・
        if "Submit" in text or "Proceed" in text or "騾∽ｿ｡" in text:
            # 譛蛻昴・蜊倩ｪ槭・遏ｩ蠖｢繧偵・繧ｿ繝ｳ縺ｮ荳ｭ蠢・→縺吶ｋ
            rect_w = line.words[0].bounding_rect
            cx = int(rect_w.x + rect_w.width / 2) + chat_global_offset_x
            cy = int(rect_w.y + rect_w.height / 2) + chat_global_offset_y
            submit_btn = {"center_x": cx, "center_y": cy}
            
        # 驕ｸ謚櫁い謗｢縺暦ｼ・1." "2." "笳・ "繝ｻ" 縺ｧ蟋九∪繧玖｡後↑縺ｩ繧帝∈謚櫁い縺ｨ縺ｿ縺ｪ縺呻ｼ・
        # 邁｡蜊倥・縺溘ａ縲ヾubmit繝懊ち繝ｳ繧医ｊ荳翫〒縲∫洒繧√・陦後ｒ驕ｸ謚櫁い蛟呵｣懊→縺吶ｋ
        elif len(text) < 50:
            rect_w = line.words[0].bounding_rect
            cx = int(rect_w.x + rect_w.width / 2) + chat_global_offset_x
            cy = int(rect_w.y + rect_w.height / 2) + chat_global_offset_y
            options.append({
                "id": f"opt_ocr_{i}",
                "text": text,
                "center_x": cx,
                "center_y": cy,
                "rect_y": rect_w.y # Y蠎ｧ讓吶〒繧ｽ繝ｼ繝育畑
            })
            
    # Submit繝懊ち繝ｳ繧医ｊ荳九↓縺ゅｋ驕ｸ謚櫁い縺ｯ髯､螟・
    if submit_btn:
        options = [o for o in options if o["center_y"] < submit_btn["center_y"]]
        
    # 繝・ヰ繝・げ逕ｻ蜒上↓謠冗判
    dbg_img = cv2.imread(debug_filename)
    if submit_btn:
        cv2.circle(dbg_img, (submit_btn["center_x"] - chat_global_offset_x, submit_btn["center_y"] - chat_global_offset_y), 5, (0, 0, 255), -1)
    for opt in options:
        cv2.circle(dbg_img, (opt["center_x"] - chat_global_offset_x, opt["center_y"] - chat_global_offset_y), 5, (0, 255, 0), -1)
    cv2.imwrite(debug_filename, dbg_img)
    
    if not options:
        logger.info("OCR: 驕ｸ謚櫁い蛟呵｣懊′隕九▽縺九ｊ縺ｾ縺帙ｓ縲・)
        return None
        
    # 雉ｪ蝠乗枚縺ｯOCR邨先棡縺ｮ荳贋ｽ阪↓縺ゅｋ繝・く繧ｹ繝医→莉ｮ螳・
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
    
    # 繧ｦ繧｣繝ｳ繝峨え縺ｮ迚ｹ螳壹→蜑埼擇蛹・
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
        raise HTTPException(status_code=400, detail="蟇ｾ雎｡縺ｮ繧ｦ繧｣繝ｳ繝峨え縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縲・)
        
    bring_window_to_front(target_hwnd)
    time.sleep(0.8)  # 蜑埼擇蛹悶ｒ蠕・▽
    
    success = find_and_click_green_proceed_button()
    if success:
        return {"status": "success", "message": "PC蛛ｴ縺ｧ Proceed 繝懊ち繝ｳ繧呈､懷・縺励※繧ｯ繝ｪ繝・け縺励∪縺励◆縲・}
    else:
        raise HTTPException(status_code=404, detail="逕ｻ髱｢荳翫↓ Proceed 繝懊ち繝ｳ・育ｷ題牡・峨′隕九▽縺九ｊ縺ｾ縺帙ｓ縺ｧ縺励◆縲・)

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

# ---------- 逕溘・莨夊ｩｱ繝ｭ繧ｰ蜿門ｾ励お繝ｳ繝峨・繧､繝ｳ繝・----------
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
        
    # 繧ｦ繧｣繝ｳ繝峨え縺ｮ迚ｹ螳・
    windows = get_visible_windows()
    logger.info(f"繧ｭ繝｣繝ｪ繝悶Ξ繝ｼ繧ｷ繝ｧ繝ｳ: 讀懷・縺輔ｌ縺溘え繧｣繝ｳ繝峨え謨ｰ={len(windows)}, 險ｱ蜿ｯ繝ｪ繧ｹ繝・{ALLOWED_TITLES}")
    target_hwnd = None
    for hwnd, title in windows:
        title_lower = title.lower()
        logger.info(f"  繧ｦ繧｣繝ｳ繝峨え: HWND={hwnd}, 繧ｿ繧､繝医Ν='{title}'")
        for allowed in ALLOWED_TITLES:
            if allowed in title_lower:
                target_hwnd = hwnd
                break
        if target_hwnd:
            break
            
    if not target_hwnd:
        logger.warning(f"繧ｭ繝｣繝ｪ繝悶Ξ繝ｼ繧ｷ繝ｧ繝ｳ: 險ｱ蜿ｯ繝ｪ繧ｹ繝医↓荳閾ｴ縺吶ｋ繧ｦ繧｣繝ｳ繝峨え縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縲よ､懷・謨ｰ={len(windows)}")
        raise HTTPException(status_code=400, detail=f"蟇ｾ雎｡縺ｮ繧ｦ繧｣繝ｳ繝峨え縺瑚ｦ九▽縺九ｊ縺ｾ縺帙ｓ縲よ､懷・縺輔ｌ縺溘え繧｣繝ｳ繝峨え謨ｰ: {len(windows)}")
        
    # 譛蜑埼擇蛹・
    bring_window_to_front(target_hwnd)
    time.sleep(0.5)  # 譛蜑埼擇蛹悶ｒ蠕・▽
    
    # 濶ｲ繧ｵ繝ｳ繝励Μ繝ｳ繧ｰ
    color = get_pixel_color_at_relative(target_hwnd)
    if not color:
        raise HTTPException(status_code=500, detail="繝斐け繧ｻ繝ｫ繧ｫ繝ｩ繝ｼ縺ｮ蜿門ｾ励↓螟ｱ謨励＠縺ｾ縺励◆縲・)
        
    # 險ｭ螳壹・譖ｴ譁ｰ縺ｨ菫晏ｭ・
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            curr_config = json.load(f)
            
        key_name = "bg_color_chat_opened" if state == "opened" else "bg_color_chat_closed"
        curr_config[key_name] = list(color)
        
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(curr_config, f, ensure_ascii=False, indent=2)
            
        logger.info(f"繧ｭ繝｣繝ｪ繝悶Ξ繝ｼ繧ｷ繝ｧ繝ｳ螳御ｺ・ {state} 縺ｮ濶ｲ繧・{color} 縺ｫ菫晏ｭ倥＠縺ｾ縺励◆縲・)
        
        # 繧ｰ繝ｭ繝ｼ繝舌Ν螟画焚繧ょ叉譎よ峩譁ｰ
        global BG_COLOR_CHAT_OPENED, BG_COLOR_CHAT_CLOSED
        if state == "opened":
            BG_COLOR_CHAT_OPENED = color
        else:
            BG_COLOR_CHAT_CLOSED = color
            
        return {"status": "success", "message": f"繝√Ε繝・ヨ縺後須'髢九＞縺ｦ縺・ｋ' if state == 'opened' else '髢峨§縺ｦ縺・ｋ'}縲醍憾諷九・閭梧勹濶ｲ繧堤匳骭ｲ縺励∪縺励◆縲・}
    except Exception as e:
        logger.error(f"繧ｭ繝｣繝ｪ繝悶Ξ繝ｼ繧ｷ繝ｧ繝ｳ繝・・繧ｿ縺ｮ菫晏ｭ倥↓螟ｱ謨励＠縺ｾ縺励◆: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save calibration data: {e}")

def save_history(text: str):
    """騾∽ｿ｡縺輔ｌ縺溘ユ繧ｭ繧ｹ繝医ｒ螻･豁ｴ繝輔ぃ繧､繝ｫ縺ｫ菫晏ｭ倥＠縺ｾ縺吶・""
    history = []
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []
            
    # 驥崎､・亟豁｢・育峩霑代→蜷後§繝・く繧ｹ繝医↑繧我ｿ晏ｭ倥＠縺ｪ縺・√∪縺溘・鬆・分蜈･繧梧崛縺茨ｼ・
    history = [h for h in history if h.get("text") != text]
    
    history.insert(0, {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "text": text
    })
    
    # 譛螟ｧ30莉ｶ菫晏ｭ・
    history = history[:30]
    
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"螻･豁ｴ縺ｮ菫晏ｭ倥↓螟ｱ謨励＠縺ｾ縺励◆: {e}")

# ==================== 襍ｷ蜍募・逅・====================

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

    
    # 0.0.0.0縺ｧ蠕・■蜿励￠・・AN蜀・・莉悶ョ繝舌う繧ｹ縺九ｉ繧｢繧ｯ繧ｻ繧ｹ縺ｧ縺阪ｋ繧医≧縺ｫ縺吶ｋ縺溘ａ・・
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
