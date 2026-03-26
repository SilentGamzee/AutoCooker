#!/bin/bash
# Ollama Project Planner launcher
cd "$(dirname "$0")"
echo "Checking dependencies..."
pip install -r requirements.txt -q --break-system-packages 2>/dev/null || pip install -r requirements.txt -q
echo "Starting Ollama Project Planner..."
python main.py
