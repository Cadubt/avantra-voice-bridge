@echo off
REM Liga a ponte de voz. Deixe esta janela aberta enquanto conversa.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Ambiente nao encontrado. Rode setup.cmd primeiro.
  pause
  exit /b 1
)
REM Avantra.exe e o python com outro nome, para o processo ser reconhecivel.
REM Se faltar (ambiente criado na mao), cai para o python normal.
set EXE=.venv\Scripts\Avantra.exe
if not exist "%EXE%" set EXE=.venv\Scripts\python.exe
"%EXE%" voice_bridge.py %*
pause
