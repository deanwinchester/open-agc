#!/bin/bash

# Open-AGC startup script — auto-installs dependencies on macOS & Linux

set -e

cd "$(dirname "$0")"

echo "==================================="
echo "     Starting Open-AGC (Panda)     "
echo "==================================="

# ── 1. Python ────────────────────────────────────────────────
# Search for a suitable Python (3.9+) — prefer higher versions first,
# since the system default (python3) may be too old (e.g. UOS has 3.7).
PYTHON=""
for cmd in python3.12 python3.11 python3.10 python3.9 python3 python; do
    if command -v "$cmd" &> /dev/null; then
        if "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 2>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    fi
done

# Check for local .python/ (prebuilt binary)
if [ -z "$PYTHON" ] && [ -f ".python/bin/python3" ]; then
    if .python/bin/python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 2>/dev/null; then
        PYTHON=".python/bin/python3"
    fi
fi

if [ -z "$PYTHON" ]; then
    # macOS: use brew (user-local, no sudo)
    if command -v brew &> /dev/null; then
        echo "Python 3.9+ not found. Installing via brew..."
        brew install python@3.12
        for cmd in python3.12 python3.11 python3.10 python3; do
            if command -v "$cmd" &> /dev/null; then
                PYTHON="$cmd"
                break
            fi
        done
    fi
fi

if [ -z "$PYTHON" ]; then
    # Download prebuilt Python from python-build-standalone (no compile, no sudo)
    echo "Downloading prebuilt Python 3.12 to .python/..."
    echo "  (about 25 MB, may take a moment)"
    mkdir -p .python
    PKG="cpython-3.12.13+20260623-x86_64-unknown-linux-gnu-install_only.tar.gz"
    URL="https://github.com/astral-sh/python-build-standalone/releases/download/20260623/$PKG"
    if command -v curl &> /dev/null; then
        curl -fL --progress-bar "$URL" -o /tmp/python.tar.gz || {
            echo "ERROR: Download failed (curl exit code $?)"
            echo "URL: $URL"
            echo "Try downloading manually and extracting to .python/"
            exit 1
        }
    elif command -v wget &> /dev/null; then
        wget --show-progress "$URL" -O /tmp/python.tar.gz || {
            echo "ERROR: Download failed (wget exit code $?)"
            echo "URL: $URL"
            echo "Try downloading manually and extracting to .python/"
            exit 1
        }
    else
        echo "ERROR: Need curl or wget to download Python."
        exit 1
    fi
    echo "Extracting..."
    tar -xzf /tmp/python.tar.gz -C .python --strip-components=1 || {
        echo "ERROR: Failed to extract Python archive."
        echo "The download may be corrupted. Try deleting .python/ and re-running."
        rm -f /tmp/python.tar.gz
        exit 1
    }
    rm /tmp/python.tar.gz
    echo "Python 3.12 extracted to .python/"
    # The binary may be "python3" or "python" depending on the build
    if [ -f ".python/bin/python3" ]; then
        PYTHON=".python/bin/python3"
    elif [ -f ".python/bin/python" ]; then
        PYTHON=".python/bin/python"
    else
        echo "ERROR: Extracted Python binary not found in .python/bin/"
        ls -la .python/bin/ 2>/dev/null || echo "  (bin/ directory missing)"
        exit 1
    fi
fi

if [ -z "$PYTHON" ] || ! $PYTHON -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)"; then
    echo "================================================"
    echo " ERROR: Cannot find or install Python 3.9+."
    if [ -n "$PYTHON" ]; then
        echo " The binary at $PYTHON failed to execute."
        echo " This may be due to incompatible glibc (system library) version."
        echo " System glibc version:"
        ldd --version 2>&1 | head -1
        echo ""
        echo " Try installing Python 3.9+ via your system package manager:"
        if command -v apt-get &> /dev/null; then
            echo "   sudo apt-get install python3"
        elif command -v yum &> /dev/null; then
            echo "   sudo yum install python3"
        fi
    fi
    echo " Or download manually from: https://www.python.org/downloads/"
    echo "================================================"
    exit 1
fi

echo "Using: $($PYTHON --version)"

# ── 2. Virtual environment ─────────────────────────────────
if [ ! -d "venv" ]; then
    echo "Virtual environment not found. Creating one..."
    $PYTHON -m venv venv
fi

echo "Activating virtual environment..."
source venv/bin/activate

# Upgrade pip inside venv
$PYTHON -m pip install --upgrade pip --quiet 2>/dev/null

# ── 3. Python dependencies ─────────────────────────────────
if [ -f "requirements.txt" ]; then
    echo "Installing Python dependencies..."
    $PYTHON -m pip install -r requirements.txt
fi

# ── 4. Node.js / frontend build ─────────────────────────────
if [ -d "static/dist" ] && [ -f "static/dist/open-agc.css" ] && [ -f "static/dist/open-agc.min.js" ]; then
    echo "Frontend assets found (static/dist/), skipping build."
else
    # Prefer local .node/ first, then check PATH
    if [ -f ".node/bin/npm" ]; then
        export PATH="$PWD/.node/bin:$PATH"
    elif ! command -v npm &> /dev/null; then
        echo "npm not found. Downloading portable Node.js to .node/..."
        echo "  (about 45 MB, may take a moment)"
        mkdir -p .node
        if command -v curl &> /dev/null; then
            curl -fL --progress-bar https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz -o /tmp/node.tar.xz || {
                echo "ERROR: Node.js download failed (curl exit code $?)"
                exit 1
            }
        elif command -v wget &> /dev/null; then
            wget --show-progress https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz -O /tmp/node.tar.xz || {
                echo "ERROR: Node.js download failed (wget exit code $?)"
                exit 1
            }
        else
            echo "ERROR: curl or wget required to download Node.js."
            echo "Install Node.js manually: https://nodejs.org/"
            exit 1
        fi
        echo "Extracting..."
        tar -xf /tmp/node.tar.xz -C .node --strip-components=1 || {
            echo "ERROR: Failed to extract Node.js archive."
            rm -f /tmp/node.tar.xz
            exit 1
        }
        rm /tmp/node.tar.xz
        echo "Node.js extracted to .node/"
        export PATH="$PWD/.node/bin:$PATH"
    fi

    if command -v npm &> /dev/null && [ -f "package.json" ]; then
        echo "Building frontend assets with Vite..."
        if [ ! -d "node_modules" ]; then
            echo "Installing Node.js dependencies..."
            npm install
        fi
        npm run build
        echo "Frontend build complete."
    fi
fi

# ── 5. Start server ────────────────────────────────────────
if [ -z "$PORT" ]; then
    if command -v lsof &> /dev/null; then
        if lsof -Pi :8000 -sTCP:LISTEN -t &>/dev/null ; then
            echo "Port 8000 is occupied, finding a free port..."
            PORT=$($PYTHON -c "import socket; s=socket.socket(); s.bind(('', 0)); print(s.getsockname()[1]); s.close()")
        else
            PORT=8000
        fi
    else
        PORT=8000
    fi
fi

echo "==================================="
echo "Open-AGC is running at:"
echo "http://localhost:$PORT"
echo "==================================="

$PYTHON -m uvicorn api.server:app --host 0.0.0.0 --port "$PORT"
