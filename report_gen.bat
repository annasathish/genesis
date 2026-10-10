@echo off
echo ===========================================
echo 🧪 Genesis Chaos Engine - Report Generator
echo ===========================================
echo.

:: Switch to the controller directory where the code and DB live
cd /d "%~dp0\controller"

:: Run the python CLI
python cli.py report

:: Provide a direct link/message about the JSON file
echo.
echo ✅ Your JSON report is ready at: %~dp0controller\report.json
echo ===========================================

:: Return to the original directory
cd /d "%~dp0"
