# 使用官方 Python 基础镜像
FROM python:3.10-slim

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive
ENV PORT=8000

# 安装系统依赖 (包括 xvfb 用于支持 PyAutoGUI 的无头模式，以及相关图形库)
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    xvfb \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libxcomposite1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    x11-utils \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Playwright 的浏览器二进制文件及系统依赖
RUN playwright install --with-deps chromium

# 复制应用程序代码
COPY . .

# 暴露端口
EXPOSE 8000

# 启动命令：使用 xvfb 运行 uvicorn 服务器，以支持那些需要显示器的依赖操作（如 pyautogui）
CMD ["sh", "-c", "xvfb-run -a -s \"-screen 0 1280x800x24\" uvicorn api.server:app --host 0.0.0.0 --port $PORT"]
