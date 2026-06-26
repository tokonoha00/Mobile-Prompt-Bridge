import ctypes

user32 = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

windows = []

def enum_windows_callback(hwnd, lParam):
    length = user32.GetWindowTextLengthW(hwnd)
    if length > 0:
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if title:
            windows.append((hwnd, title))
    return True

user32.EnumWindows(WNDENUMPROC(enum_windows_callback), 0)

print("--- All Window Titles ---")
for hwnd, title in windows:
    print(f"HWND: {hwnd} | Title: {title}")
print("-------------------------")
