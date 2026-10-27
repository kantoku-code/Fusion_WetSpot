@echo off
setlocal
chcp 65001 >nul

rem Keep this script in the installer folder next to the WetSpot folder.
for %%I in ("%~dp0..\WetSpot") do set "SOURCE=%%~fI"
set "DESTINATION=%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns\WetSpot"

if not exist "%SOURCE%\WetSpot.manifest" (
    echo ERROR: WetSpot folder was not found next to the installer folder.
    echo エラー: installer フォルダーの隣に WetSpot フォルダーが見つかりません。
    echo Expected / 想定パス: "%SOURCE%"
    pause
    exit /b 1
)

echo WetSpot will be copied to:
echo WetSpot のコピー先:
echo "%DESTINATION%"
echo.
choice /C YN /M "Copy WetSpot to the Fusion add-ins folder? / Fusion のアドインフォルダーに WetSpot をコピーしますか？"
if errorlevel 2 (
    echo Installation cancelled.
    echo インストールを中止しました。
    pause
    exit /b 0
)

robocopy "%SOURCE%" "%DESTINATION%" /E /R:2 /W:1
if errorlevel 8 (
    echo.
    echo ERROR: WetSpot installation failed. See the copy errors above.
    echo エラー: WetSpot のインストールに失敗しました。上のコピーエラーを確認してください。
    pause
    exit /b 1
)

echo.
echo WetSpot installation completed. Restart Fusion if it is running.
echo WetSpot のインストールが完了しました。Fusion が起動中の場合は再起動してください。
explorer.exe "%DESTINATION%"
pause
exit /b 0
