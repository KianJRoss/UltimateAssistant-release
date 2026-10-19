@echo off
if not exist C:\AI\perception-mcp\logs mkdir C:\AI\perception-mcp\logs
cd /d C:\AI\perception-mcp
.venv\Scripts\python.exe activity_logger.py >> C:\AI\perception-mcp\logs\activity-logger-run.log 2>&1
