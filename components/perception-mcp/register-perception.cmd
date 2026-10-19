@echo off
openclaw mcp add perception --command "C:\AI\perception-mcp\.venv\Scripts\python.exe" --arg "C:\AI\perception-mcp\server.py" --cwd "C:\AI\perception-mcp" --env "PERCEPTION_VISION_MODEL=qwen3-vl:8b" --env "PERCEPTION_CAPTURE_DIR=C:\AI\perception-mcp\captures" --timeout 240
