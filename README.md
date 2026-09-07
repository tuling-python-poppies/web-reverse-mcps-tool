# web-reverse-mcps-tool

面向 JavaScript 逆向 / 反爬分析 / 微信小程序逆向的 **MCP 工具集**。

Agent 原生设计：工具粒度、输出边界、错误提示都围绕 AI Agent 的连续推理决策来组织，可直接接入 opencode、Pi、Claude Code、Codex、Cursor、Cherry Studio 等支持 MCP 的 AI 客户端。

## 目录结构

| 目录 | 工具 | 类型 | 说明 |
| --- | --- | --- | --- |
| `Camoufox/camoufox-reverse-mcp` | **camoufox-reverse-mcp** | Python MCP | 基于 Camoufox（引擎级指纹伪装的隐形浏览器）的逆向 MCP：hook、JSVMP 插桩、网络捕获、引擎级属性追踪、指纹比对 |
| `CloakBrowser/js-reverse-mcp-local-cloak` | **js-reverse-mcp** | Node/TS MCP | AI-first 浏览器逆向 MCP：断点调试、脚本分析、网络/WebSocket、Set-Cookie 识别、页面状态重放；默认 Patchright 反检测，`--cloak` 切换 CloakBrowser |
| `WMPFDebugger` | **WMPFDebugger** | Node (GPL) | 微信小程序 Windows 调试器：将小程序私有 CDP 协议（protobuf）转换为标准 Chrome 调试协议，通过 DevTools 调试任意小程序 |
| `WMPFDebugger/miniapp-reverse-mcp` | **miniapp-reverse-mcp** | Python MCP | 连接 WMPFDebugger 的 CDP 端点，把小程序调试能力暴露为 MCP 工具（网络监控、断点、运行时等） |

> 浏览器二进制（Camoufox、CloakBrowser 本体约 1.5GB+）**不包含在本仓库**（Gitee 单文件 100MB 上限，git 仓库无法承载）。见下文各工具说明获取二进制。

## 环境要求

- **Python ≥ 3.10**（camoufox-reverse-mcp）、**Python ≥ 3.11**（miniapp-reverse-mcp）
- **Node.js v22+**（js-reverse-mcp 要求 v20.19+，WMPFDebugger 建议 v22 LTS）
- 建议将仓库克隆到**不含中文/空格的路径**下（Windows 下减少脚本兼容问题）

---

## 1. camoufox-reverse-mcp（Camoufox）

Python 反检测浏览器 MCP Server，生态：`mcp>=1.0.0`、`camoufox[geoip]`、`playwright`、`esprima`（AST 重写）。

核心能力：

- 浏览器 launch / navigate / click / 截图（Camoufox 引擎级指纹模拟，无 webdriver 痕迹）
- `inject_hook_preset` / `hook_function` / `hook_jsvmp_interpreter`：XHR/Fetch/cookie/JSVMP 运行时插桩
- `network_capture` / `get_request_initiator`：请求捕获 + 发起栈定位签名函数
- `instrumentation`：对 JSVMP 字节码脚本做源码级 AST 重写埋点
- `trace_property_access`：SpiderMonkey 引擎级属性访问追踪（需 custom build）
- `analyze_cookie_sources` / `verify_signer_offline`：cookie 来源归因、签名离线验证

### 安装

```powershell
pip install -e C:\path\to\web-reverse-mcps-tool\Camoufox\camoufox-reverse-mcp
```

### 浏览器二进制

下载地址：<https://github.com/WhiteNightShadow/camoufox-reverse/releases>（本仓库附带的 `Camoufox\camoufox.exe` 即来自该发行版）

本机路径：`Camoufox\camoufox.exe`（或另装，通过 `CAMOUFOX_EXECUTABLE_PATH` 指定）。启动 MCP 前需设置环境变量（仓库内的 `launch.bat` 已封装好，直接作为启动命令使用即可）：

```
CAMOUFOX_EXECUTABLE_PATH        ->  Camoufox\camoufox.exe
CAMOUFOX_BROWSER_ROOT           ->  Camoufox\
CAMOUFOX_DATA_DIR               ->  Camoufox\.camoufox-data
CAMOUFOX_REVERSE_RUNTIME_DIR    ->  Camoufox\.camoufox-runtime
PLAYWRIGHT_BROWSERS_PATH        ->  Camoufox\.camoufox-runtime\playwright-browsers
```

### 手动验证

```powershell
C:\path\to\web-reverse-mcps-tool\Camoufox\camoufox-reverse-mcp\launch.bat
```

---

## 2. js-reverse-mcp（CloakBrowser）

AI-first 浏览器逆向 MCP（v3.0.x），把 DevTools 能力重组成适合 Agent 推理的工具集。默认基于 Patchright（协议层 stealth），强反爬站点可启用 CloakBrowser（源码层指纹）。

核心能力：

- 断点：`set_breakpoint_on_text` / XHR 断点 / 事件监听断点，暂停后 `get_paused_info`、作用域求值、单步
- 脚本：`list_scripts` / `search_in_sources` / `save_script_source`（自动格式化压缩脚本）
- 网络：`list_network_requests`（Set-Cookie 流）/ `get_request_initiator` / body/headers 导出
- 状态重放：`clear_site_data`（cookies/cache/storage）+ reload 复现风控流程
- 反检测：无头/有头、`--cloak` 切换 CloakBrowser 二进制

### 安装

```powershell
cd C:\path\to\web-reverse-mcps-tool\CloakBrowser\js-reverse-mcp-local-cloak
npm install
npm run build          # 产物: node build/src/index.js
```

### 浏览器二进制

- 默认：Patchright（随依赖安装，本机 Chrome/Chromium）
- Cloak：`--cloak` 首次使用自动下载（约 200MB），或 `--cloakBinaryPath D:\path\to\CloakBrowser\chrome.exe` 指向本机二进制（本仓库预留的 CloakBrowser 目录即为此用途）
- CloakBrowser 发行版下载：<https://github.com/CloakHQ/CloakBrowser/releases>

### 手动验证

```powershell
node .\build\src\index.js
# 换成 CloakBrowser:
node .\build\src\index.js --cloak --cloakBinaryPath "D:\path\to\CloakBrowser\chrome.exe"
```

---

## 3. WMPFDebugger + miniapp-reverse-mcp（微信小程序）

WMPFDebugger（fork 自 [evi0s/WMPFDebugger](https://github.com/evi0s/WMPFDebugger)，GPL-2.0）：patch 微信小程序 CDP 过滤器，强制小程序走 LanDebug 远程调试；将私有 protobuf 调试协议转换成标准 Chrome 协议。

配合 `miniapp-reverse-mcp`（Python，`mcp>=1.27` + `cdp-use`）把调试能力输出为 MCP 工具：`list_network_requests`（自动连接端点、推断当前页面 target、单 target XHR/Fetch 监控）、断点、运行时、脚本、WebSocket。

### 基本流程

```powershell
# 1) 安装 WMPFDebugger（微信小程序调试器）
cd C:\path\to\web-reverse-mcps-tool\WMPFDebugger
yarn
npx ts-node src/index.ts          # 启动调试服务器 + CDP 代理 (ws://127.0.0.1:62000)

# 2) 打开要调试的小程序（顺序: 先开小程序，再进 DevTools）

# 3) 浏览器访问 DevTools 端点（人工/可忽略，MCP 会自动连）
#    devtools://devtools/bundled/inspector.html?ws=127.0.0.1:62000

# 4) 安装小程序 MCP
pip install -e C:\path\to\web-reverse-mcps-tool\WMPFDebugger\miniapp-reverse-mcp
```

`list_network_requests` 默认端点：`devtools://devtools/bundled/inspector.html?ws=127.0.0.1:62000`（工具参数可覆盖）。

---

## 各 AI 客户端配置示例

以下 `<repo>` 均为 `C:/path/to/web-reverse-mcps-tool` 或仓库实际路径的占位符。三个服务器名建议固定为：`camoufox` / `js-reverse` / `miniapp`。

### opencode（`opencode.json`，项目或 `~/.config/opencode/opencode.json`）

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "camoufox": {
      "type": "local",
      "command": ["cmd", "/c", "C:/path/to/web-reverse-mcps-tool/Camoufox/camoufox-reverse-mcp/launch.bat"],
      "enabled": true
    },
    "js-reverse": {
      "type": "local",
      "command": [
        "node",
        "C:/path/to/web-reverse-mcps-tool/CloakBrowser/js-reverse-mcp-local-cloak/build/src/index.js",
        "--cloak",
        "--cloakBinaryPath",
        "C:/path/to/CloakBrowser/chrome.exe"
      ],
      "enabled": true,
      "environment": { "DEBUG": "" }
    },
    "miniapp": {
      "type": "local",
      "command": [
        "python",
        "C:/path/to/web-reverse-mcps-tool/WMPFDebugger/miniapp-reverse-mcp/run_mcp_server.py"
      ],
      "enabled": true,
      "environment": { "PYTHONUTF8": "1" }
    }
  }
}
```

要点：

- `command` 必须是**数组**；Windows 下启动 `.bat` 用 `["cmd","/c", "..."]` 形式
- 不需要某台服务器时 `"enabled": false` 整体停用
- 改完重启 opencode 生效

### Pi（Pi Coding Agent）

Pi 通过 [pi-mcp-adapter](https://www.npmjs.com/package/pi-mcp-adapter) 使用 MCP，标准 MCP 配置文件即可复用：

```bash
pi install npm:pi-mcp-adapter
```

项目级：`.mcp.json`；全局：`~/.config/mcp/mcp.json`（或 `~/.pi/agent/mcp.json`）：

```json
{
  "mcpServers": {
    "camoufox": {
      "command": "cmd",
      "args": ["/c", "C:/path/to/web-reverse-mcps-tool/Camoufox/camoufox-reverse-mcp/launch.bat"]
    },
    "js-reverse": {
      "command": "node",
      "args": [
        "C:/path/to/web-reverse-mcps-tool/CloakBrowser/js-reverse-mcp-local-cloak/build/src/index.js",
        "--cloak",
        "--cloakBinaryPath",
        "C:/path/to/CloakBrowser/chrome.exe"
      ]
    },
    "miniapp": {
      "command": "python",
      "args": ["C:/path/to/web-reverse-mcps-tool/WMPFDebugger/miniapp-reverse-mcp/run_mcp_server.py"]
    }
  }
}
```

Pi 内用 `/mcp` 查看状态，`/mcp reconnect <server>` 重连；适配器默认**懒加载**（首次调用工具才拉起进程）。工具较多想直接注册可加 `"directTools": [...]`。

### Claude Code

```bash
claude mcp add camoufox -- cmd /c "C:/path/to/web-reverse-mcps-tool/Camoufox/camoufox-reverse-mcp/launch.bat"
claude mcp add js-reverse -- node "C:/path/to/web-reverse-mcps-tool/CloakBrowser/js-reverse-mcp-local-cloak/build/src/index.js"
claude mcp add miniapp -- python "C:/path/to/web-reverse-mcps-tool/WMPFDebugger/miniapp-reverse-mcp/run_mcp_server.py"
```

### Codex

```bash
codex mcp add camoufox -- cmd /c "C:/path/to/web-reverse-mcps-tool/Camoufox/camoufox-reverse-mcp/launch.bat"
codex mcp add js-reverse -- node "C:/path/to/web-reverse-mcps-tool/CloakBrowser/js-reverse-mcp-local-cloak/build/src/index.js"
codex mcp add miniapp -- python "C:/path/to/web-reverse-mcps-tool/WMPFDebugger/miniapp-reverse-mcp/run_mcp_server.py"
```

### Cursor

`Cursor Settings` → `MCP` → `Add new MCP server`，在 JSON 配置中填入 `mcpServers`（格式如 Pi 一节）。

### VS Code Copilot

```bash
code --add-mcp '{"name":"js-reverse","command":"node","args":["C:/path/to/web-reverse-mcps-tool/CloakBrowser/js-reverse-mcp-local-cloak/build/src/index.js"]}'
```

### Cherry Studio / 其他桌面客户端

设置 → MCP 服务器 → 添加（类型：stdio）：

- json 格式与 Pi 一节的 `mcpServers` 相同
- 三个服务器各自为一条记录

---

## 常见问题

| 问题 | 处理 |
| --- | --- |
| 浏览器路径不对启动报错 | 检查 `CAMOUFOX_EXECUTABLE_PATH`（camoufox）/ `--cloakBinaryPath`（js-reverse）是否指向存在的 exe |
| Windows 下 `cmd /c` 启动 bat 失败 | 路径含空格时确保整条路径用双引号包裹；避免中文路径 |
| miniapp 连不上 | 先 `npx ts-node src/index.ts` 启动 WMPFDebugger，再打开小程序，最后让 MCP 走 `ws://127.0.0.1:62000`；确认端口未被占用 |
| 首次 `npm run build` 报 TS 错误 | js-reverse-mcp 要求 Node ≥ 20.19，先升级 `node -v` |
| Gitee 上找不到浏览器 | 本仓库只有 MCP 源码；Camoufox 浏览器从官方 release 下载，CloakBrowser 可用 `--cloak` 自动下载或使用本机二进制 |

## License

- `camoufox-reverse-mcp`：MIT
- `js-reverse-mcp-local-cloak`：Apache-2.0（原始项目 [zhizhuodemao/js-reverse-mcp](https://github.com/zhizhuodemao/js-reverse-mcp)）
- `WMPFDebugger`：GPL-2.0（原始项目 [evi0s/WMPFDebugger](https://github.com/evi0s/WMPFDebugger)）

> 仅限学习与技术研究使用，请遵守各站点的服务条款。
