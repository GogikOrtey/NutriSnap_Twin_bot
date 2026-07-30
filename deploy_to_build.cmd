@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM deploy_to_build.cmd — коммит (если нужно), merge в ветку build, push,
REM возврат на исходную ветку. Триггерит GitHub Actions → деплой на SkyNode.

cd /d "%~dp0"

for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD') do set "SRC_BRANCH=%%b"
if "!SRC_BRANCH!"=="" (
  color 47
  echo Не удалось определить текущую ветку.
  pause
  exit /b 1
)

echo Текущая ветка: !SRC_BRANCH!
echo.

REM Есть ли незакоммиченные изменения?
git diff --quiet --exit-code
set "DIFF_EXIT=!errorlevel!"
git diff --cached --quiet --exit-code
set "CACHED_EXIT=!errorlevel!"
git ls-files --others --exclude-standard > "%TEMP%\nutrisnap_untracked.txt" 2>nul
set "HAS_UNTRACKED=0"
for %%A in ("%TEMP%\nutrisnap_untracked.txt") do if %%~zA gtr 0 set "HAS_UNTRACKED=1"
del "%TEMP%\nutrisnap_untracked.txt" >nul 2>&1

if not "!DIFF_EXIT!"=="0" goto :need_commit
if not "!CACHED_EXIT!"=="0" goto :need_commit
if "!HAS_UNTRACKED!"=="1" goto :need_commit
goto :after_commit

:need_commit
echo Есть незакоммиченные изменения на !SRC_BRANCH!.
set /p COMMIT_MSG="Описание коммита (пусто = дата/время): "
if "!COMMIT_MSG!"=="" (
  for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /format:list') do set datetime=%%I
  set "COMMIT_MSG=!datetime:~6,2!.!datetime:~4,2!.!datetime:~0,4! - !datetime:~8,2!:!datetime:~10,2! - deploy"
)
echo.
echo git add -A
git add -A
echo git commit -m "!COMMIT_MSG!"
git commit -m "!COMMIT_MSG!" || (
  color 47
  echo Коммит не создан.
  pause
  exit /b 1
)

:after_commit
echo.
echo Переключаемся на build...
git show-ref --verify --quiet refs/heads/build
if errorlevel 1 (
  echo Локальной ветки build нет — создаём от main.
  git fetch origin main 2>nul
  git checkout -b build main || git checkout -b build
) else (
  git checkout build || (
    color 47
    echo Не удалось переключиться на build.
    pause
    exit /b 1
  )
)

echo.
echo Merge !SRC_BRANCH! → build
if /i not "!SRC_BRANCH!"=="build" (
  git merge "!SRC_BRANCH!" -m "deploy: merge !SRC_BRANCH! into build" || (
    color 47
    echo Merge в build не удался. Разреши конфликты и повтори.
    pause
    exit /b 1
  )
)

echo.
echo git push -u origin build
git push -u origin build || (
  color 47
  echo Push origin build не удался.
  pause
  exit /b 1
)

echo.
echo Возврат на !SRC_BRANCH!...
if /i not "!SRC_BRANCH!"=="build" (
  git checkout "!SRC_BRANCH!" || (
    color 47
    echo Push прошёл, но вернуться на !SRC_BRANCH! не удалось.
    pause
    exit /b 1
  )
)

color 20
echo.
echo Готово: build запушен. GitHub Actions деплоит на SkyNode.
echo.
timeout /t 2 >nul
exit /b 0

REM Важно: Добавить число в версии приложения