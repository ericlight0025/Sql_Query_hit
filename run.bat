@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ==========================================================
rem  SQL Coverage Finder - 一鍵執行
rem  所有設定都在 config.toml，改完存檔後雙擊本檔即可。
rem  html = true 時，產生的 report.html 會自動用瀏覽器開啟。
rem
rem  進階用法：
rem    run.bat                       → 使用 config.toml
rem    run.bat bank.toml             → 使用指定的設定檔
rem    run.bat config.toml --top 10  → 臨時覆蓋設定檔中的值
rem ==========================================================

set "CFG=%~1"
if "%CFG%"=="" set "CFG=config.toml"

where python >nul 2>nul
if errorlevel 1 (
    echo [錯誤] 找不到 python，請先安裝 Python 3.11 以上並加入 PATH。
    pause
    exit /b 1
)

if not exist "%CFG%" (
    echo [錯誤] 找不到設定檔 %CFG%
    pause
    exit /b 1
)

python sql_coverage_finder.py --config "%CFG%" --open %2 %3 %4 %5 %6 %7 %8 %9
if errorlevel 1 echo [錯誤] 程式執行失敗，請檢查上面的訊息與 %CFG%。
pause
endlocal
