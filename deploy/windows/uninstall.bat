@echo off
echo Stopping SuperCC services...
net stop supercc-wecom 2>nul
net stop supercc-feishu 2>nul
net stop supercc-main 2>nul

echo Removing NSSM services...
nssm remove supercc-main /q 2>nul
nssm remove supercc-feishu /q 2>nul
nssm remove supercc-wecom /q 2>nul

echo SuperCC services uninstalled.