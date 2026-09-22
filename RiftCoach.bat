@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title RiftCoach AI

REM ===================================================================
REM  Arquivo unico. O usuario da DOIS CLIQUES aqui e nao precisa saber
REM  o que e terminal, Python ou uv.
REM
REM  Ele so instala o que falta e entrega o resto para `riftcoach start`,
REM  que conversa com a pessoa um passo de cada vez. A regra: nunca pedir
REM  duas coisas ao mesmo tempo, e sempre dizer qual e o proximo passo.
REM ===================================================================

echo.
echo   ====================================================
echo     RiftCoach AI - analise de partidas de League
echo   ====================================================
echo.

REM --- 1. O uv existe? Ele instala o Python sozinho, entao e a unica
REM ---    dependencia real do projeto.
where uv >nul 2>&1
if %errorlevel% equ 0 goto :tem_uv

echo   [1/3] Instalando o uv ^(so na primeira vez, leva ~1 minuto^)...
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
echo   [2/3] Preparando o ambiente ^(a primeira vez demora, depois e rapido^)...
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
uv run riftcoach start

echo.
echo   ====================================================
echo     Pode fechar esta janela.
echo   ====================================================
pause
