#!/bin/bash

# Navigate to the script's directory
cd "$(dirname "$0")"

echo "==================================="
echo "     Starting Open-AGC (Panda)     "
echo "==================================="

# Detect Python command (python3 on some distros, python on others)
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &> /dev/null; then
        PYTHON="$cmd"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Error: Python 3 is not installed or not in your PATH."
    echo "Please install Python 3 from https://www.python.org/downloads/"
    exit 1
fi

echo "Using: $($PYTHON --version)"

# Check for virtual environment
if [ ! -d "venv" ]; then
    echo "Virtual environment not found. Creating one..."
    $PYTHON -m venv venv
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Upgrade pip inside venv to avoid install issues
$PYTHON -m pip install --upgrade pip --quiet 2>/dev/null

# Install dependencies if requirements.txt exists
if [ -f "requirements.txt" ]; then
    echo "Checking / Installing dependencies..."
    $PYTHON -m pip install -r requirements.txt
fi

# Build frontend assets with Vite (Node.js required)
if [ -d "static/dist" ] && [ -f "static/dist/open-agc.css" ] && [ -f "static/dist/open-agc.min.js" ]; then
    echo "Frontend assets found (static/dist/), skipping build."
else
    if command -v npm &> /dev/null && [ -f "package.json" ]; then
        echo "Building frontend assets with Vite..."
        if [ -f "node_modules/.package-lock.json" ] || [ -d "node_modules" ]; then
            echo "node_modules found, skipping install."
        else
            echo "Installing Node.js dependencies..."
            npm install
        fi
        npm run build
        echo "Frontend build complete."
    else
        echo "==============================================="
        echo " WARNING: npm not found or package.json missing."
        echo " Frontend assets will not be built."
        echo " The page may load without CSS/styles."
        echo " To fix: install Node.js and run:"
        echo "   npm install && npm run build"
        echo "==============================================="
    fi
fi

# Start the server
if [ -z "$PORT" ]; then
    # Default to 8000, if occupied, find a free one
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
