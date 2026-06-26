# -*- coding: utf-8 -*-
import ctypes
from ctypes import wintypes
import sys

# stdout をutf-8に
sys.stdout.reconfigure(encoding='utf-8')

user32 = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

results = []

def callback(hwnd, lParam):
    if user32.IsWindowVisible(hwnd):
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            results.append((int(hwnd), title))
    return True

cb = WNDENUMPROC(callback)
user32.EnumWindows(cb, 0)

allowed = ["antigravity", "visual studio code", "cursor", "windsurf"]
print(f"Total visible windows: {len(results)}")
for hwnd, title in results:
    match = any(a in title.lower() for a in allowed)
    tag = " <<< MATCH" if match else ""
    print(f"  [{hwnd}] {title}{tag}")

matched = [t for _, t in results if any(a in t.lower() for a in allowed)]
print(f"\nMatched: {len(matched)}")
if not matched:
    print("WARNING: No matching window found!")
    print("Antigravity IDE may have a different window title.")
