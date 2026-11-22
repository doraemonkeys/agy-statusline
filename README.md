# 🚀 AGY Statusline

A sleek, real-time two-line HUD statusline for **Google Antigravity CLI (`agy`)** and **Gemini CLI**.

[English](README.md) | [中文说明](README_zh.md)

```text
🤖 4 Argon (High) [gemini-4-argon-trial] │ 📁 my-project │ 🌿 main │ ⚡ reviewing · [plan] │ ⏱️ 42s / 18m 30s
🧠 Context ██░░░░░░░░ 256.8k/1.05M (24.5%) │ ⏳ 5h: 95% (4h 56m) │ 📅 Wk: 99% (167h 56m)
```

---

## ✨ Features

- 🤖 **Clean Model Indicator & Upstream Variant Inspection** — Intelligently shortens frontend model identifiers (e.g. `Gemini 4 Argon (High)` → `4 Argon (High)`) and extracts the exact upstream `response_model` (e.g. `[gemini-4-argon-trial]`, `[gemini-3.8-flash-n]`) directly from conversation `gen_metadata`.
- 📁 **Workspace Awareness** — Displays the current working directory.
- 🌿 **Ultra-fast Git Detection** — Reads `.git/HEAD` directly by walking parent directories (supports standard repos, worktrees, and submodules). **Zero subprocess spawning**, eliminating lag.
- ⚡ **Agent State and Execution Mode** — Shows the live agent state and the execution mode reported by agy, such as `⚡ idle · [plan]`.
- ⏱️ **Turn & Session Duration Tracking** — Displays both the **current/latest turn duration** and **total session elapsed duration** (`⏱️ 42s / 18m 30s`), ticking live while the agent works and freezing the turn stopwatch once idle.
- 🧠 **Context Window Progress Bar** — Visual token usage meter (`██░░░░░░░░`) with color-coded safety tiers:
  - 🟢 Green (< 60%)
  - 🟡 Yellow (60% - 85%)
  - 🔴 Red (≥ 85%)
- ⏳ **5-Hour Quota Tracker** — Real-time rolling quota percentage and countdown to quota reset (e.g., `95% (4h 56m)`), matched to the active model's quota group (`gemini-*` vs `3p-*`) and automatically synced across concurrent terminals on the same account via a local SQLite WAL cache.
- 📅 **Weekly Quota Tracker** — Weekly quota percentage and reset countdown (e.g., `99% (167h 56m)`).
- 🪶 **Zero Dependencies** — Built entirely with standard library Python (`sys`, `json`, `os`, `sqlite3`, `time`). Works out-of-the-box on Windows, macOS, and Linux.

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
- Copy `statusline.py` atomically into `~/.gemini/antigravity-cli/statusline.py`
- Pin the installing Python interpreter's absolute path and use `-S` to skip site initialization
- On Windows, launch `python.exe -S statusline.py` directly when both paths are `cmd /c`-safe (avoiding batch wrapper file locks), and automatically fall back to a minimal `statusline.cmd` launcher only when paths contain spaces or `cmd.exe` metacharacters
- Validate the installed command before saving settings, and back up the existing configuration
- Configure the `"statusLine"` command hook while preserving other settings

Requires Python 3.8 or newer. Rerun the installer after moving or removing the pinned Python installation (for example, when removing an old mise Python version).

### Method 2: Manual Setup

1. Copy `statusline.py` to your Antigravity CLI config directory:
   - **Windows**: `C:\Users\<YourUser>\.gemini\antigravity-cli\statusline.py`
   - **macOS / Linux**: `~/.gemini/antigravity-cli/statusline.py`

2. On macOS/Linux, open `~/.gemini/antigravity-cli/settings.json` and configure absolute paths to your interpreter and the copied script:

   ```json
   {
     "statusLine": {
       "type": "command",
       "command": "/absolute/path/to/python3 -S /absolute/path/to/statusline.py",
       "enabled": true
     }
   }
   ```

   Replace the example paths, and shell-quote each path if it contains spaces or special characters. On Windows, you can use the same unquoted `C:\...\python.exe -S C:\...\statusline.py` form when neither path contains spaces or `cmd.exe` special characters; otherwise use the automatic installer so it generates a `.cmd` launcher instead of embedding quoted Python arguments in the CLI configuration.

---

## 🔍 Preview & Testing

You can test and preview the statusline rendering without opening Antigravity:

```bash
python -S statusline.py --preview
```

Output:
```text
🤖 4 Argon (High) [gemini-4-argon-trial] │ 📁 agy-statusline │ 🌿 main │ ⚡ reviewing · [plan] │ ⏱️ 42s / 18m 30s
🧠 Context ██░░░░░░░░ 256.8k/1.05M (24.5%) │ ⏳ 5h: 95% (4h 56m) │ 📅 Wk: 99% (167h 56m)
```

Run the regression checks for WAL reads, connection cleanup, duration tracking, and installed command execution:

```bash
python -S -m unittest -v
```

---

## 🛠️ How It Works

Google Antigravity CLI (`agy`) supports custom status lines by streaming a JSON state snapshot to `stdin` on every turn. `statusline.py` parses:
- `model`: Model configuration, display name, and read-only upstream `response_model` from `gen_metadata`
- `cwd` / `workspace`: Active project directory
- `agent_state` / `cycle_mode`: Current agent activity and the selected execution mode when agy reports it
- `session` / `turn` / trajectory `steps`: Current turn duration and total session elapsed time (queried in the same single read-only SQLite pass)
- `context_window`: Exact latest-turn token usage from `current_usage` (falling back to `total_input_tokens` / `used_percentage` without double-counting cumulative `total_output_tokens`), window capacity, and consistent usage percentage
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
