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

# Check for local .python/ (prebuilt binary, may be python3 or python)
if [ -z "$PYTHON" ]; then
    for cmd in ".python/bin/python3" ".python/bin/python"; do
        if [ -f "$cmd" ] && "$cmd" -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 2>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    done
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
    # Try apt-get install python3 on Debian-based (may work on UOS)
    if command -v apt-get &> /dev/null; then
        echo "Python 3.9+ not found. Trying apt-get install python3..."
        if sudo apt-get install -y -qq python3 python3-pip python3-venv 2>/dev/null; then
            if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 2>/dev/null; then
                PYTHON="python3"
            fi
        fi
    fi
fi

if [ -z "$PYTHON" ]; then
    # Download prebuilt Python from python-build-standalone (no compile, no sudo)
    echo "Detecting system architecture..."
    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64)  PKG_ARCH="x86_64-unknown-linux-gnu" ;;
        aarch64) PKG_ARCH="aarch64-unknown-linux-gnu" ;;
        armv7l)  PKG_ARCH="armv7-unknown-linux-gnueabi" ;;
        *)
            echo "ERROR: Unsupported architecture: $ARCH"
            echo "Please install Python 3.9+ manually: https://www.python.org/downloads/"
            exit 1
            ;;
    esac
    echo "  Architecture: $ARCH -> $PKG_ARCH"

    echo "Downloading prebuilt Python 3.12 to .python/..."
    echo "  (about 25 MB, may take a moment)"
    mkdir -p .python
    PKG="cpython-3.12.13+20260623-${PKG_ARCH}-install_only.tar.gz"
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
# Rebuild frontend if source files are newer than dist, or dist doesn't exist
NEED_BUILD=0
if [ ! -f "static/dist/open-agc.min.js" ] || [ ! -f "static/dist/open-agc.css" ]; then
    NEED_BUILD=1
else
    # Check if any source file is newer than the dist
    for src in static/app.js static/js/*.js static/style.css package.json vite.config.mjs; do
        if [ "$src" -nt "static/dist/open-agc.min.js" ] 2>/dev/null; then
            NEED_BUILD=1
            break
        fi
    done
fi
if [ $NEED_BUILD -eq 1 ]; then
    # Prefer local .node/ first, then check PATH
    if [ -f ".node/bin/npm" ]; then
        # Verify the existing node binary works (may be wrong architecture)
        if .node/bin/node --version &>/dev/null; then
            export PATH="$PWD/.node/bin:$PATH"
        else
            echo "Existing .node/ binary is not executable (wrong architecture?), re-downloading..."
            rm -rf .node
        fi
    fi
    if ! command -v npm &> /dev/null; then
        echo "npm not found. Downloading portable Node.js to .node/..."
        echo "  (about 45 MB, may take a moment)"
        mkdir -p .node
        # Detect architecture for Node.js
        NODE_ARCH="linux-x64"
        case "$(uname -m)" in
            aarch64) NODE_ARCH="linux-arm64" ;;
            armv7l)  NODE_ARCH="linux-armv7l" ;;
        esac
        echo "  Architecture: $(uname -m) -> ${NODE_ARCH}"
        NODE_URL="https://nodejs.org/dist/v22.14.0/node-v22.14.0-${NODE_ARCH}.tar.xz"
        if command -v curl &> /dev/null; then
            curl -fL --progress-bar "$NODE_URL" -o /tmp/node.tar.xz || {
                echo "ERROR: Node.js download failed (curl exit code $?)"
                exit 1
            }
        elif command -v wget &> /dev/null; then
            wget --show-progress "$NODE_URL" -O /tmp/node.tar.xz || {
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
        # Verify the binary works
        if ! .node/bin/node --version &>/dev/null; then
            echo "ERROR: Downloaded Node.js binary cannot execute (wrong architecture?)"
            echo "  Please install Node.js manually: https://nodejs.org/"
            rm -rf .node
            exit 1
        fi
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
