# 🚀 AGY Statusline

为 **Google Antigravity CLI (`agy`)** 与 **Gemini CLI** 打造的实时两行 HUD 终端状态栏。

[English](README.md) | [中文说明](README_zh.md)

```text
🤖 4 Argon (High) [gemini-4-argon-trial] │ 📁 my-project │ 🌿 main │ ⚡ reviewing │ ⏱️ 42s / 18m 30s
🧠 Context ██░░░░░░░░ 244.6k/1.0M (24.5%) │ ⏳ 5h: 95% (4h 56m) │ 📅 Wk: 99% (167h 56m)
```

---

## ✨ 特性

- 🤖 **精简模型显示 + 上游真实型号透视** — 智能简化前端模型名称（如 `Gemini 4 Argon (High)` → `4 Argon (High)`），并直接从会话库 `gen_metadata` 实时解析上游返回的真实 `response_model`（如 `[gemini-4-argon-trial]`、`[gemini-3.8-flash-n]`）。
- 📁 **工作目录展示** — 实时展示当前项目目录。
- 🌿 **极速 Git 分支检测** — 通过向上遍历目录直接解析 `.git/HEAD` 文件（原生兼容主分支、Git Worktree 与 Submodule），**不启动任何 Git 子进程**，零性能损耗、毫秒级响应。
- ⚡ **Agent 运行状态** — 实时反映 Agent 当前生命周期（`idle`、`ready`、`reviewing` 等），带自适应颜色高亮。
- ⏱️ **本轮与会话持续时长** — 同屏展示 **本轮持续时长 / 会话总持续时长**（如 `⏱️ 42s / 18m 30s`）；执行中实时计时并以黄色高亮，进入空闲后自动定格上一轮实际耗时。
- 🧠 **Context 进度条与 Token 统计** — 直观展示上下文用量进度条（`██░░░░░░░░`）与 Token 计数（如 `244.6k/1.0M`），三段颜色预警：
  - 🟢 绿色（< 60%）
  - 🟡 黄色（60% - 85%）
  - 🔴 红色（≥ 85%）
- ⏳ **5 小时滚动配额跟踪** — 实时显示 5 小时内剩余额度百分比及重置倒计时（例如 `95% (4h 56m)`）。
- 📅 **每周配额跟踪** — 实时显示每周剩余额度百分比及重置倒计时（例如 `99% (167h 56m)`）。
- 🪶 **零外部依赖** — 纯 Python 3 标准库（`sys`、`json`、`os`、`sqlite3`、`time`）实现，原生跨平台支持 Windows、macOS 与 Linux。

---

## ⚡ 快速安装

### 方式 1：自动安装脚本（推荐）

1. 克隆本仓库：
   ```bash
   git clone https://github.com/doraemonkeys/agy-statusline.git
   cd agy-statusline
   ```

2. 运行安装脚本：
   ```bash
   python install.py
   ```

安装脚本将自动执行：
- 将 `statusline.py` 复制到 `~/.gemini/antigravity-cli/statusline.py`
- 固定安装时使用的 Python 解释器绝对路径，并加上 `-S` 跳过 site 初始化
- 在 Windows 上创建 `statusline.cmd`，正确处理 CLI 的命令转义
- 验证实际安装的启动命令后，再备份并保存配置
- 更新 `"statusLine"` 命令钩子，保留其他设置

需要 Python 3.8 或更新版本。移动或删除已固定的 Python 安装后（例如删除 mise 中的旧版本），请重新运行安装脚本。

### 方式 2：手动配置

1. 将 `statusline.py` 复制到 Antigravity CLI 配置目录下：
   - **Windows**：`C:\Users\<你的用户名>\.gemini\antigravity-cli\statusline.py`
   - **macOS / Linux**：`~/.gemini/antigravity-cli/statusline.py`

2. 在 macOS/Linux 上，打开 `~/.gemini/antigravity-cli/settings.json`，使用解释器和已复制脚本的绝对路径配置 `"statusLine"`：

   ```json
   {
     "statusLine": {
       "type": "command",
       "command": "/absolute/path/to/python3 -S /absolute/path/to/statusline.py",
       "enabled": true
     }
   }
   ```

   请替换示例中的绝对路径；路径包含空格或特殊字符时，需要分别进行 shell 转义。Windows 请使用自动安装脚本：安装器会生成批处理启动脚本和正确转义的命令，避免在 CLI 配置中直接嵌入带引号的 Python 参数。

---

## 🔍 预览与测试

无需启动 Antigravity，即可在终端直接预览状态栏的渲染效果：

```bash
python -S statusline.py --preview
```

效果输出：
```text
🤖 4 Argon (High) [gemini-4-argon-trial] │ 📁 agy-statusline │ 🌿 main │ ⚡ reviewing │ ⏱️ 42s / 18m 30s
🧠 Context ██░░░░░░░░ 244.6k/1.0M (24.5%) │ ⏳ 5h: 95% (4h 56m) │ 📅 Wk: 99% (167h 56m)
```

运行 WAL 读取、连接释放、时长统计和安装命令执行的回归检查：

```bash
python -S -m unittest -v
```

---

## 🛠️ 工作原理

Google Antigravity CLI (`agy`) 在每次交互更新时，都会将当前环境状态的 JSON 数据流式写入标准输入（`stdin`）。`statusline.py` 读取并解析以下数据：
- `model`: 模型配置、显示名，以及从会话库 `gen_metadata` 只读解析的上游 `response_model`
- `cwd` / `workspace`: 当前工作目录
- `agent_state`: Agent 当前生命周期状态
- `session` / `turn` / 会话轨迹 `steps`: 本轮持续时长与会话总持续时长（复用同一次只读 SQLite 连接，零额外连接开销）
- `context_window`: 当前使用的 Token 数、最大 Token 限制及使用比例
- `quota`: 5 小时滚动配额、每周配额及重置秒数倒计时

最后格式化为带有 ANSI 颜色高亮的两行文本输出给 Antigravity HUD 渲染。

---

## 🗑️ 卸载

如需卸载状态栏配置：

```bash
python install.py --uninstall
```

或者直接在 `settings.json` 中删除 `"statusLine"` 配置项即可。

---

## 📄 开源协议

[MIT License](LICENSE) © 2025-2026 doraemonkeys
