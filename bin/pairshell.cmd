@echo off
rem Zero-install launcher for cmd.exe / PowerShell: add this bin folder to PATH.
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 "%~dp0pairshell" %*
) else (
  python "%~dp0pairshell" %*
)
exit /b %ERRORLEVEL%
