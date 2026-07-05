@echo off
set "NODE_OPTIONS="
set "PATH=C:\Users\Administrator\.workbuddy\binaries\node\versions\22.12.0;%PATH%"
cd /d "D:\analyst agent\biz-analyst-agent\backend"
"C:\Users\Administrator\.workbuddy\binaries\python\envs\biz-analyst\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8000 > backend_run.log 2>&1
