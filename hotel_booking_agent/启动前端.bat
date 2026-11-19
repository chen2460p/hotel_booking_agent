@echo off
chcp 65001 >nul
cd /d %~dp0

rem 聊天页是 React 构建产物（frontend\dist）：首次或前端改动后自动构建一次，
rem 模板过期会让 web.py 首页返回“前端未构建”提示页。
if not exist "frontend\dist\index.html" (
  where npm >nul 2>nul
  if errorlevel 1 (
    echo [提示] 没找到 npm，跳过前端构建；首页会显示构建说明。
  ) else (
    echo [前端] 正在构建聊天页（首次约 1 分钟）...
    cd frontend
    if not exist "node_modules" call npm ci
    call npm run build
    cd ..
  )
)

python web.py
pause
