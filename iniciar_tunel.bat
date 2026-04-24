@echo off
:: Pedir permisos de Administrador
>nul 2>&1 "%SYSTEMROOT%\system32\cacls.exe" "%SYSTEMROOT%\system32\config\system"
if '%errorlevel%' NEQ '0' (
    echo Pidiendo permisos de administrador...
    goto UACPrompt
) else ( goto gotAdmin )

:UACPrompt
    echo Set UAC = CreateObject^("Shell.Application"^) > "%temp%\getadmin.vbs"
    echo UAC.ShellExecute "%~s0", "", "", "runas", 1 >> "%temp%\getadmin.vbs"
    "%temp%\getadmin.vbs"
    del "%temp%\getadmin.vbs"
    exit /B

:gotAdmin
    pushd "%CD%"
    CD /D "%~dp0"

:: Lanzar PowerShell completamente oculto (sin flash de consola)
echo Set WshShell = CreateObject("WScript.Shell") > "%temp%\launch_tunnel.vbs"
echo WshShell.Run "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File ""%~dp0iniciar_tunel.ps1""", 0, False >> "%temp%\launch_tunnel.vbs"
wscript "%temp%\launch_tunnel.vbs"
del "%temp%\launch_tunnel.vbs"