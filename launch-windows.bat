@echo off
rem Aegis launcher - wraps launch-windows.ps1 so it runs regardless of the
rem machine's PowerShell execution policy. Double-click this file, or run
rem it from any terminal:  .\launch-windows.bat
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch-windows.ps1" %*
