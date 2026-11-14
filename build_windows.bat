@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ==============================================
echo              GikAI Windows Builder
echo ==============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден. Установи Python 3.11 или новее и включи Add Python to PATH.
    pause
    exit /b 1
)

echo [1/3] Устанавливаем зависимости...
python -m pip install --upgrade pip
if errorlevel 1 goto :error
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :error

echo.
echo [2/3] Собираем GikAI.exe...
python -m PyInstaller --noconfirm --clean --onefile --windowed --name GikAI --collect-all keyring main.py
if errorlevel 1 goto :error

echo.
echo [3/3] Готово!
echo EXE находится в папке:
echo %CD%\dist\GikAI.exe
explorer "%CD%\dist"
pause
exit /b 0

:error
echo.
echo Во время сборки произошла ошибка. Скопируй текст выше и пришли его сюда.
pause
exit /b 1
