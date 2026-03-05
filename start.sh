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
echo "Starting the API server on http://localhost:8000 ..."
python -m uvicorn api.server:app --host 0.0.0.0 --port 8000
