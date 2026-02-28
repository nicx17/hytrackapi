#!/bin/bash
echo "Starting the HyTrack API Server..."
uvicorn api:app --host 127.0.0.1 --port 8000
