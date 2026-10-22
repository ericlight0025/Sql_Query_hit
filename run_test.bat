@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ==========================================================
rem  產生測試資料並驗證
rem  用法：
rem    run_test.bat                   → 5000 支、full 情境
rem    run_test.bat partial           → 5000 支、不可能 100% 的情境
rem    run_test.bat wide 2000         → 50 欄位情境、2000 支
rem    run_test.bat stress            → 壓力測試（5000 與 10000 支）
rem ==========================================================

set "SCENARIO=%~1"
set "COUNT=%~2"
if "%SCENARIO%"=="" set "SCENARIO=full"
if "%COUNT%"=="" set "COUNT=5000"

if /i "%SCENARIO%"=="stress" (
    python test\stress_test.py 5000,10000 stress_data
    pause
    exit /b
)

echo 產生測試資料：%SCENARIO%，%COUNT% 支 ...
python test\gen_test_data.py test_data 42 %SCENARIO% %COUNT%
if errorlevel 1 ( pause & exit /b 1 )

echo.
echo 執行搜尋（設定檔 test_data\config.toml）...
python sql_coverage_finder.py --config test_data\config.toml --output-dir test_data --html --open

echo.
echo 驗證結果 ...
python test\verify_test.py test_data
pause
endlocal
