#!/bin/bash
# Test script to run server with debug mode enabled

echo "=== Starting AgentFinder with DEBUG mode enabled ==="
echo ""

# Source environment variables
source set_keys.sh

# Enable debug mode
export WHO_DEBUG=true

echo "WHO_DEBUG is set to: $WHO_DEBUG"
echo ""
echo "Starting server on port 8000..."
echo "Debug logs will appear below with [WHO_DEBUG] prefix"
echo "=========================================="
echo ""

# Run the server (it will print debug logs to stdout)
python3 code/agent_finder.py
