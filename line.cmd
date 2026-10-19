@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
py -3 tg.py serve >> logs\telegram.log 2>&1
