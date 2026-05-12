@echo off
REM SuperCC 多进程服务安装脚本 (NSSM)
REM 用法: install.bat
REM 需要先安装 NSSM: https://nssm.cc/download

set SERVICE_DIR=%USERPROFILE%\.supercc
set PYTHON=%USERPROFILE%\AppData\Local\Programs\Python\Python311\python.exe

REM 验证 Python 路径
if not exist "%PYTHON%" (
    echo ERROR: Python not found at %PYTHON%
    echo 请修改 install.bat 中的 PYTHON 变量指向实际 Python 路径
    exit /b 1
)

REM 验证 NSSM 路径（尝试常见安装位置）
where nssm >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo ERROR: NSSM not found in PATH
    echo 请先安装 NSSM: https://nssm.cc/download
    exit /b 1
)

echo Installing SuperCC Main service...
nssm install supercc-main "%PYTHON%" "-m supercc main core-only --config %SERVICE_DIR%\config.json --data-dir %SERVICE_DIR%"
nssm set supercc-main AppEnvironment "SUPERCC_CONFIG=%SERVICE_DIR%\config.json"
nssm set supercc-main AppEnvironment "SUPERCC_DATA=%SERVICE_DIR%"
nssm set supercc-main DisplayName "SuperCC Core"
nssm set supercc-main Start SERVICE_DELAYED_AUTO_START
nssm set supercc-main AppRestart 1
echo.

echo Installing SuperCC Feishu service...
nssm install supercc-feishu "%PYTHON%" "-m supercc.plugin.feishu"
nssm set supercc-feishu AppEnvironment "SUPERCC_CONFIG=%SERVICE_DIR%\config.json"
nssm set supercc-feishu AppEnvironment "SUPERCC_DATA=%SERVICE_DIR%"
nssm set supercc-feishu DisplayName "SuperCC Feishu Plugin"
nssm set supercc-feishu Start SERVICE_DELAYED_AUTO_START
nssm set supercc-feishu AppRestart 1
nssm set supercc-feishu AppDependencies supercc-main
echo.

echo Installing SuperCC WeCom service...
nssm install supercc-wecom "%PYTHON%" "-m supercc.plugin.wecom"
nssm set supercc-wecom AppEnvironment "SUPERCC_CONFIG=%SERVICE_DIR%\config.json"
nssm set supercc-wecom AppEnvironment "SUPERCC_DATA=%SERVICE_DIR%"
nssm set supercc-wecom DisplayName "SuperCC WeCom Plugin"
nssm set supercc-wecom Start SERVICE_DELAYED_AUTO_START
nssm set supercc-wecom AppRestart 1
nssm set supercc-wecom AppDependencies supercc-main
echo.

echo Starting services...
net start supercc-main
net start supercc-feishu
net start supercc-wecom

echo.
echo SuperCC services installed and started.
echo.
echo To uninstall: stop services first (net stop supercc-feishu ^&^& net stop supercc-wecom ^&^& net stop supercc-main)
echo           then run: nssm remove supercc-main /q ^&^& nssm remove supercc-feishu /q ^&^& nssm remove supercc-wecom /q