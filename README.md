# 🚀 AGY Statusline

A sleek, real-time two-line HUD statusline for **Google Antigravity CLI (`agy`)** and **Gemini CLI**.

[English](README.md) | [中文说明](README_zh.md)

```text
🤖 4 Argon (High) [gemini-4-argon-trial] │ 📁 my-project │ 🌿 main │ ⚡ reviewing
🧠 Context ██░░░░░░░░ 244.6k/1.0M (24.5%) │ ⏳ 5h: 95% (4h 56m) │ 📅 Wk: 99% (167h 56m)
```

---

## ✨ Features

- 🤖 **Clean Model Indicator & Upstream Variant Inspection** — Intelligently shortens frontend model identifiers (e.g. `Gemini 4 Argon (High)` → `4 Argon (High)`) and extracts the exact upstream `response_model` (e.g. `[gemini-4-argon-trial]`, `[gemini-3.8-flash-n]`) directly from conversation `gen_metadata`.
- 📁 **Workspace Awareness** — Displays the current working directory.
- 🌿 **Ultra-fast Git Detection** — Reads `.git/HEAD` directly by walking parent directories (supports standard repos, worktrees, and submodules). **Zero subprocess spawning**, eliminating lag.
- ⚡ **Agent State Display** — Live agent lifecycle state (`idle`, `ready`, `reviewing`, etc.) with dynamic color coding.
- 🧠 **Context Window Progress Bar** — Visual token usage meter (`██░░░░░░░░`) with color-coded safety tiers:
  - 🟢 Green (< 60%)
  - 🟡 Yellow (60% - 85%)
  - 🔴 Red (≥ 85%)
- ⏳ **5-Hour Quota Tracker** — Real-time rolling quota percentage and countdown to quota reset (e.g., `95% (4h 56m)`).
- 📅 **Weekly Quota Tracker** — Weekly quota percentage and reset countdown (e.g., `99% (167h 56m)`).
- 🪶 **Zero Dependencies** — Built entirely with standard library Python (`sys`, `json`, `os`). Works out-of-the-box on Windows, macOS, and Linux.

---

## ⚡ Quick Installation

### Method 1: Automatic Installer (Recommended)

1. Clone this repository:
   ```bash
   git clone https://github.com/doraemonkeys/agy-statusline.git
   cd agy-statusline
   ```

2. Run the installer:
   ```bash
   python install.py
   ```

The script will automatically:
- Copy `statusline.py` into `~/.gemini/antigravity-cli/statusline.py`
- Backup your existing `settings.json`
- Configure the `"statusLine"` command hook

### Method 2: Manual Setup

1. Copy `statusline.py` to your Antigravity CLI config directory:
   - **Windows**: `C:\Users\<YourUser>\.gemini\antigravity-cli\statusline.py`
   - **macOS / Linux**: `~/.gemini/antigravity-cli/statusline.py`

2. Open `~/.gemini/antigravity-cli/settings.json` and add or update the `"statusLine"` section:

   ```json
   {
     "statusLine": {
       "type": "command",
       "command": "python ~/.gemini/antigravity-cli/statusline.py",
       "enabled": true
     }
   }
   ```

   *(On Windows, use forward slashes or escaped backslashes, e.g., `"python C:/Users/<YourUser>/.gemini/antigravity-cli/statusline.py"`)*.

---

## 🔍 Preview & Testing

You can test and preview the statusline rendering without opening Antigravity:

```bash
python statusline.py --preview
```

Output:
```text
🤖 3.8 Flash (High) │ 📁 agy-statusline │ 🌿 main │ ⚡ reviewing
🧠 Context ██░░░░░░░░ 244.6k/1.0M (24.5%) │ ⏳ 5h: 95% (4h 56m) │ 📅 Wk: 99% (167h 56m)
```

---

## 🛠️ How It Works

Google Antigravity CLI (`agy`) supports custom status lines by streaming a JSON state snapshot to `stdin` on every turn. `statusline.py` parses:
- `model`: Model configuration and display name
- `cwd` / `workspace`: Active project directory
- `agent_state`: Current agent execution phase
- `context_window`: Token usage, maximum context limit, and percentage
- `quota`: 5-hour rolling and weekly rate limits and countdown seconds

It then outputs ANSI color-formatted text that Antigravity renders above the interactive prompt.

---

## 🗑️ Uninstallation

To remove the statusline:

```bash
python install.py --uninstall
```

Or manually remove the `"statusLine"` block from your `settings.json`.

---

## 📄 License

[MIT License](LICENSE) © 2025-2026 doraemonkeys
