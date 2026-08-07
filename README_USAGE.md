# 🐼 Open-AGC 使用说明 (Usage Guide)

欢迎使用 Open-AGC (Panda)！这是一个由大模型驱动的自主代码助手。通过本指南，您可以快速完成配置并开始使用。

## 1. 快速启动 (Quick Start)

项目提供了自动化的启动脚本，会自动检查环境、安装依赖并运行：

### macOS / Linux
运行项目根目录下的启动脚本：
```bash
./start.sh
```

### Windows
双击运行根目录下的：
```bat
start.bat
```

## 2. 配置 API Key (Configuring API Key)

**注意：** 您无需在 `.env` 文件中手动配置。Open-AGC 提供了直观的网页端配置界面。

### 配置步骤 (以 DeepSeek 为例)：
1. **获取 Key**: 前往 [DeepSeek 开放平台](https://platform.deepseek.com/api_keys) 申请 API Key。
2. **打开设置**: 程序启动后，在左侧边栏点击 **「设置 (Settings)」** 图标。
3. **系统配置**: 在设置页面的 **「系统配置」** 标签页中找到 **「模型配置」**。
4. **填入并保存**: 
   - 选择模型（如 `deepseek/deepseek-v4-flash`）。
   - 在对应的 API Key 输入框中粘贴您的 Key。
   - 点击下方的 **「保存配置」**。
5. **开始对话**: 配置完成后，即可回到主界面开始对话。

## 3. 打包分发 (Packaging)

如果您想将程序打包成独立的可执行文件（DMG 或 ZIP）：

### macOS (生成 .dmg)
```bash
./build_mac.sh
```

### Windows (生成 .zip)
双击运行：
```bat
build_win.bat
```
打包后的文件将存放在 `dist/` 目录下。

## 4. 数据存储路径 (Data Persistence)

打包后的程序会将用户数据存放在系统标准位置，以确保更新程序时不丢失记录：
- **macOS**: `~/Library/Application Support/Open-AGC`
- **Windows**: `%APPDATA%\Open-AGC`

## 5. 访问控制 (Access Control)

内置 IP 分层访问控制：**本机免密直连**；**局域网设备需输入访问密码**；**公网（含公网 IPv6）一律拒绝**。

配置访问密码（环境变量为一次性播种，config.json 为唯一事实源）：

```bash
# Docker / 环境变量方式：config.json 未配置时把该值播种进
# data/config.json，之后判定只以 config.json 为准；已配置则忽略
docker run -d -e OPEN_AGC_ACCESS_PASSWORD=你的访问密码 ...
# 或 docker-compose.yml 的 environment 节：
#   - OPEN_AGC_ACCESS_PASSWORD=你的访问密码
```

也可以在启动后通过「设置 → 访问控制」页面配置/修改（保存到 `data/config.json`）。**不设置密码时仅允许本机访问**。

> Docker bridge 部署注意：端口映射经 NAT 后源地址均为 docker 网关，宿主机访问同样需密码，公网/局域网无法在应用层区分。公网禁止请靠映射面保证——compose 默认 `127.0.0.1:8000:8000`；局域网开放绑到具体 LAN 网卡（如 `192.168.x.x:8000:8000`），不要裸写 `8000:8000`。Linux 可用 `network_mode: host` 获得裸机语义。

---

# 🐼 Open-AGC Usage Guide (English)

Welcome to Open-AGC (Panda). This guide will help you get started quickly.

## 1. Quick Start

Use the automated scripts to set up the environment and run the app:

### macOS / Linux
```bash
./start.sh
```

### Windows
Run:
```bat
start.bat
```

## 2. Configuring API Keys

**Note:** You don't need to edit `.env` files manually. API keys are managed through the Web UI.

### Steps (Example: DeepSeek):
1. **Get Key**: Obtain an API Key from [DeepSeek Platform](https://platform.deepseek.com/api_keys).
2. **Settings**: Click the **"Settings"** icon in the left sidebar.
3. **Configuration**: Go to **"System Config"** -> **"Model Config"**.
4. **Save**:
   - Select your model (e.g., `deepseek/deepseek-v4-flash`).
   - Paste the API Key.
   - Click **"Save Settings"**.
5. **Chat**: You're all set to start chatting!

## 3. Packaging

To create standalone executables:

### macOS (.dmg)
```bash
./build_mac.sh
```

### Windows (.zip)
Run:
```bat
build_win.bat
```
Output files are located in the `dist/` folder.

## 4. Data Location
- **macOS**: `~/Library/Application Support/Open-AGC`
- **Windows**: `%APPDATA%\Open-AGC`
