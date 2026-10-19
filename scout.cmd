@echo off
cd /d "%~dp0"
if not exist logs mkdir logs
py -3 scout.py >> logs\scout.log 2>&1
