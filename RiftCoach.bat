@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title RiftCoach AI

REM ===================================================================
REM  Arquivo unico. Dois cliques aqui e pronto.
REM
REM  O CAMINHO RAPIDO VEM PRIMEIRO, e isso e de proposito: depois da
REM  primeira vez, esta janela preta aparece por alguns milissegundos e
REM  some, porque quem assume e a JANELA do RiftCoach (pythonw, que nao
REM  tem console). Terminal so aparece quando ha mesmo o que instalar —
REM  e ai ele e util, porque mostra que alguma coisa esta acontecendo.
REM ===================================================================

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m riftcoach gui
    exit /b 0
)

echo.
echo   ====================================================
echo     RiftCoach AI - primeira vez, vamos preparar tudo
echo   ====================================================
echo.
echo   Isso acontece UMA vez. Da proxima, abre direto.
echo.

REM --- 1. O uv existe? Ele instala o Python sozinho, entao e a unica
REM ---    dependencia real do projeto.
where uv >nul 2>&1
if %errorlevel% equ 0 goto :tem_uv

echo   [1/3] Instalando o uv ^(leva ~1 minuto^)...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"

REM O instalador adiciona ao PATH so para janelas NOVAS. Esta aqui
REM precisa do caminho na mao, senao o comando seguinte falha logo apos
REM uma instalacao bem-sucedida — que e a pior hora para falhar.
set "PATH=%USERPROFILE%\.local\bin;%LOCALAPPDATA%\Programs\uv;%PATH%"

where uv >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   [X] O uv foi instalado mas esta janela nao o encontrou.
    echo       FECHE esta janela e abra o RiftCoach.bat de novo.
    echo.
    pause
    exit /b 1
)

:tem_uv
echo   [2/3] Baixando o que falta ^(Python e bibliotecas^)...
echo.
uv sync --extra web --quiet
if %errorlevel% neq 0 (
    echo.
    echo   [X] Nao consegui preparar o ambiente.
    echo       Verifique sua conexao com a internet e tente de novo.
    echo.
    pause
    exit /b 1
)

echo   [3/3] Abrindo o RiftCoach...
echo.

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m riftcoach gui
    exit /b 0
)

REM Sem pythonw nao da para esconder o console. Melhor abrir o assistente
REM de texto, que faz exatamente a mesma coisa, do que nao abrir nada.
uv run riftcoach start
echo.
echo   Pode fechar esta janela.
pause
