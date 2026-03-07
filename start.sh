#!/bin/bash

# Navigate to the script's directory
cd "$(dirname "$0")"

echo "==================================="
echo "     Starting Open-AGC (Panda)     "
echo "==================================="

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is not installed or not in your PATH."
    exit 1
fi

# Check for virtual environment
if [ ! -d "venv" ]; then
    echo "Virtual environment not found. Creating one..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Install dependencies if requirements.txt exists
if [ -f "requirements.txt" ]; then
    echo "Checking / Installing dependencies..."
    pip install -r requirements.txt
fi

# Start the server
if [ -z "$PORT" ]; then
    # Default to 8000, if occupied, find a free one
    if lsof -Pi :8000 -sTCP:LISTEN -t >/dev/null ; then
        echo "Port 8000 is occupied, finding a free port..."
        PORT=$(python3 -c "import socket; s=socket.socket(); s.bind(('', 0)); print(s.getsockname()[1]); s.close()")
    else
        PORT=8000
    fi
fi

echo "==================================="
echo "Open-AGC is running at:"
echo "http://localhost:$PORT"
echo "==================================="

# Use python3 consistently
python3 -m uvicorn api.server:app --host 0.0.0.0 --port "$PORT"
