@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem AgentLink 管理面板的 Windows 启动入口：双击运行，或传入参数，例如 start_agentlink.bat --no-browser
set "AGENTLINK_DIR=%~dp0"
if "%AGENTLINK_DIR:~-1%"=="\" set "AGENTLINK_DIR=%AGENTLINK_DIR:~0,-1%"
set "AGENTLINK_ENTRY=%AGENTLINK_DIR%\agentlink.py"

if not exist "%AGENTLINK_ENTRY%" goto :agentlink_no_entry

set "AGENTLINK_PY="
where python >nul 2>nul && set "AGENTLINK_PY=python"
if not defined AGENTLINK_PY (
  where py >nul 2>nul
  if not errorlevel 1 set "AGENTLINK_PY=py -3"
)
if not defined AGENTLINK_PY goto :agentlink_no_python

rem 始终从脚本所在目录启动，避免依赖“当前工作目录”
pushd "%AGENTLINK_DIR%"
%AGENTLINK_PY% -I "%AGENTLINK_ENTRY%" panel %*
set "AGENTLINK_EXIT=%ERRORLEVEL%"
popd

if not "%AGENTLINK_EXIT%"=="0" (
  echo.
  echo [AgentLink] 管理面板启动失败，退出码 %AGENTLINK_EXIT%。
  goto :agentlink_halt
)
exit /b 0

:agentlink_no_entry
echo [AgentLink] 未找到入口文件：%AGENTLINK_ENTRY%
echo [AgentLink] 请确认 start_agentlink.bat 位于 AgentLink 项目根目录。
set "AGENTLINK_EXIT=1"
goto :agentlink_halt

:agentlink_no_python
echo [AgentLink] 未找到可用的 Python。
echo [AgentLink] 请安装 Python 3.8 或更高版本，并勾选 Add Python to PATH 后重新运行。
set "AGENTLINK_EXIT=1"

:agentlink_halt
echo.
echo 按任意键关闭此窗口……
pause >nul
exit /b %AGENTLINK_EXIT%
