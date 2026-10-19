@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
py -3 server.py >> logs\console.log 2>&1
