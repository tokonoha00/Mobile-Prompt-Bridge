# Mobile Prompt Bridge - Agent Knowledge

You are an AI assistant in a workspace containing the **Mobile Prompt Bridge** project.
This file provides you with critical context about this specific project so that you can perfectly assist the user in setting it up, troubleshooting it, and understanding its architecture.

## 1. What is this project?
"Mobile Prompt Bridge" is a tool that allows users to operate their PC-based AI IDEs (like Antigravity IDE, Cursor, Windsurf, VS Code) remotely from their smartphones.
Because these IDEs do not provide an official API for remote interaction, this tool "hacks" the system from the outside using:
- `pyautogui` and `uiautomation` for simulating keyboard/mouse inputs.
- `mss` and `winsdk` (Windows OCR) to capture the screen and read the UI elements (like `ask_question` dialogs).
- File polling to read the AI's chat logs (`transcript_full.jsonl`).

## 2. Setup & Execution Instructions (CRITICAL)
If the user asks you "How do I set this up?" or "How do I use this?", guide them through the following steps:

1. **Install dependencies**: `pip install -r requirements.txt`
2. **Start the server**: Run `run.bat` (or `python src/main.py`).
   - **⚠️ CRITICAL "SESSION 0" RULE**: YOU, THE AI AGENT, MUST **NEVER** RUN `run.bat` IN THE BACKGROUND FOR THE USER using a `run_command` tool.
   - If the server is run in the background by the AI, it runs in "Session 0" (a headless session with no GUI). The screen capture (`mss`) and UI Automation will fail to see the IDE, resulting in `0 detected windows` or black screenshots.
   - You MUST instruct the user to **manually double-click `run.bat` in their own Windows Explorer** or run it from their own visible terminal.
3. **Expose to the internet**: Run `ngrok http 8712` in a separate terminal.
4. **Access from Mobile**: Open the ngrok URL (e.g., `https://xxxxx.ngrok-free.app/?token=YOUR_TOKEN`) on the smartphone. The token is displayed in the `run.bat` terminal or can be customized in `config.json`.

## 3. Configuration (`config.json`)
If the user wants to customize the tool, tell them about `config.json` (which is generated from `config.example.json` on first run).
- `allowed_window_titles`: List of window titles the tool is allowed to paste text into.
- `security_token`: Can be set to a fixed password so the user doesn't have to copy a new token on every restart.
- `enable_auto_send`: Set to `true` if they want the text to be automatically sent (`Enter`) after pasting.

## 4. Troubleshooting
- **"対象ウィンドウが見つかりません" (Target window not found)**: The user might have minimized the IDE, or the window title doesn't match `allowed_window_titles` in `config.json`.
- **Screen scan (OCR) fails to find questions**: The chat panel must be visible on the screen. If the IDE is heavily obscured or running in Session 0 (started by the AI agent), it will fail.
- **ngrok not found**: The user needs to download ngrok from ngrok.com and add it to their system PATH.

When assisting the user with this project, proactively provide the above information, especially the **Session 0 trap** and the **ngrok URL/token setup**. Always reply in polite Japanese.
