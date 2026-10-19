@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
py -3 agent.py run >> logs\agent.log 2>&1
