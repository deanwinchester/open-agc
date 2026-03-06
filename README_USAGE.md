# 🐼 Open-AGC 使用说明 (Usage Guide)

欢迎使用 Open-AGC (Panda)！这是一个由大模型驱动的自主代码助手。

## 1. 快速开始 (Quick Start)

### macOS / Linux
运行项目根目录下的启动脚本：
```bash
./start.sh
```

### Windows
双击运行：
```bat
start.bat
```

## 2. 配置 API Key (Configuring API Key)

在第一次运行程序时，您需要配置大模型的 API Key 才能开始对话。

### 以 Kimi (Moonshot) 为例：
1. **申请 Key**: 前往 [Moonshot 开放平台](https://platform.moonshot.cn/console/api-keys) 注册并创建一个 API Key。
2. **进入设置**: 启动程序后，在左侧边栏点击「设置 (Settings)」。
3. **模型配置**: 在「系统配置」标签页下找到「模型配置」。
4. **填写保存**: 选择或填入 `moonshot/kimi-k2.5`，并在 API Key 栏位填入您刚才申请的长字符串，点击保存。

## 3. 功能特性 (Features)

- **自主编程**: 能够阅读、搜索、修改代码并执行测试。
- **视觉能力**: 可以截取屏幕并分析 UI 界面。
- **技能系统**: 支持学习新技能，并将其固化为 Markdown 文档。
- **沙箱安全**: 默认在 `workspace` 目录下执行操作，保护您的系统安全。

## 4. 常见问题 (FAQ)

- **找不到 Open-AGC 文件夹？**
  - 打包运行后，数据默认存储在 `~/Library/Application Support/Open-AGC` (Mac) 或 `%APPDATA%\Open-AGC` (Windows)。
- **Agent 报错 "local variable 'os'"？**
  - 请确保您使用的是最新版本的代码/安装包，此问题已在 v1.0.1 中修复。

---
感谢使用！如有任何问题，欢迎在对话框中向我反馈。🐼✨
