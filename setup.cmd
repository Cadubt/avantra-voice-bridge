@echo off
REM Cria o ambiente e instala tudo. Roda uma vez, depois de clonar.
cd /d "%~dp0"

REM Python 3.12 de verdade. O "python" do Windows pode ser so o atalho da
REM Microsoft Store, que nao funciona — por isso tenta o "py" primeiro.
if not exist ".venv\Scripts\python.exe" (
  py -3.12 -m venv .venv 2>nul || python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo Nao consegui criar o ambiente. Instale o Python 3.12:
  echo   winget install Python.Python.3.12
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt

REM Copia do python com outro nome: o processo aparece como "Avantra" no
REM Gerenciador de Tarefas e no mixer de volume, em vez de um python qualquer.
copy /y ".venv\Scripts\python.exe" ".venv\Scripts\Avantra.exe" >nul

REM O config.toml real fica fora do git (senha falada, caminhos pessoais).
if not exist config.toml (
  copy config.example.toml config.toml >nul
  echo Criei config.toml a partir do exemplo. Ajuste caminhos e a senha.
)

echo.
echo Pronto. Rode start.cmd
pause
