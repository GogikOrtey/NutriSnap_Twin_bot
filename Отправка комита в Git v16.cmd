@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM Автоматический скрипт, для инициализации репозитория
REM и отправки коммитов в ветку main.
REM 
REM v14
REM 
REM Добавьте этот скрипт в папку вашего проекта,
REM и он всё сделает за вас






REM -------------------------------------------
REM Если git репозиторий проекта ещё не создан:

if not exist .git (
    color 70
    echo Инициализируем новый репозиторий...

    REM Ожидание 3 секунды
    timeout /t 3 >nul

    color 07
    echo.
    echo Инициализация репозитория git
    echo git init
    echo.
    git init

    echo.
    echo Добавление всех файлов в индекс git
    echo git add .
    echo.
    git add .

    echo Создание первого коммита
    echo git commit -m "First commit"
    echo.
    git commit -m "First commit"

    echo.
    echo Открытие веб-страницы https://github.com/new
    timeout /t 3 >nul
    start https://github.com/new
    timeout /t 2 >nul
    echo.

    REM Ожидание ввода URL репозитория
    :ssh_url_input
    set /p repoURL="Введите URL вашего репозитория (SSH): "

    echo.
    echo Добавление удаленного репозитория
    echo git remote add origin !repoURL! 
    echo.
    git remote add origin !repoURL! || (
        color 47
        echo.
        echo При добавлении удаленного репозитория произошла ошибка^^!
        echo Попробуйте ввести URL репозитория заново:
        goto :ssh_url_input
    )

    color 07

    echo Отправка коммита в удаленный репозиторий
    echo git push -u origin main 
    echo.
    REM Отправка коммитов в удаленный репозиторий
    git push -u origin main || (
        color 47
        echo.
        echo При отправке первого коммита произошла ошибка^^!
        echo Рекомендуем удалить папку .git из это директории, и запустите данный скрипт заново
        pause
        exit
    )

    REM Вывожу 30 пустых строк - очищаю экран пользователя
    for /l %%i in (1,1,30) do (
      echo.
    )

    color 20
    echo.
    echo Репозиторий успешно создан ✓ 
    echo.
    echo › Используйте этот-же скрипт, для создания и загрузки коммитов в ветку main ✨
    echo.

    for /l %%i in (1,1,23) do (
      echo.
    )

    pause
    exit
)









REM ----------------------------------------
REM Если git репозиторий проекта уже создан:

echo Создание нового коммита
echo.

REM Проверка наличия файла last_commit.txt и его содержимого
if exist .git/last_commit.txt (
    set /p lastCommit=<.git/last_commit.txt
    if not "!lastCommit!"=="" (
        echo Последний коммит: !lastCommit!
    )
) else (
    echo Предыдущих коммитов нет
)

REM Задаём описание для коммита
set /p commitMessage="Описание коммита: "
if "!commitMessage!"=="" (
    REM Если пользователь ввёл пустую строку, то записывается текущие дата и время
    for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /format:list') do set datetime=%%I
    set datetime=!datetime:~6,2!.!datetime:~4,2!.!datetime:~0,4! - !datetime:~8,2!:!datetime:~10,2! - no description
    set commitMessage=!datetime!
)
 
REM Добавляем все файлы, и создаём новый коммит
echo.

echo git add . 
git add . 

echo.
echo git commit -m "%commitMessage%"
git commit -m "%commitMessage%" || (
    color 47
    echo.
    echo При создании коммита произошла ошибка^^!
    pause 
    pause
    exit
) 


REM Отправляем его в GitHub
echo.
echo Отправка коммита в GitHub...
echo.
echo git push origin main 
git push origin main > .git/temp1.txt || (
    findstr /C:"error: failed to push some refs" .git/temp1.txt > nul
    color 47
    echo.

    if %errorlevel% equ 0 (
        REM Удаление временного файла
        del ".git\temp1.txt"

        goto error_pushing

    ) else (
        REM Удаление временного файла
        del ".git\temp1.txt"
        echo При отправке коммита в GitHub произошла ошибка^^!
        pause
        pause
        exit
    )
)

REM Удаление временного файла
del ".git\temp1.txt"

REM Удаление файла last_commit.txt, если он существует
if exist ".git\last_commit.txt" (
    del ".git\last_commit.txt"
)

REM Создание нового файла ".git\last_commit.txt"
echo !commitMessage! > ".git\last_commit.txt"
REM В этом текстовом файле хранится описание последнего коммита

color 20
echo.
echo Коммит успешно создан и отправлен ✓
timeout /t 1 >nul
REM pause
exit
 













:error_pushing

for /l %%i in (1,1,23) do (
  echo.
)

color 60
echo Похоже, произошла ошибка, связанная с тем, что последние коммиты на локальном и удалённом репозитории отличаются.
echo. 
echo Вы можете выполнить следующие действия:
echo 1. Принудительно отправить на удалённый репоиторий последний ваш локальный коммит [git push -f origin main]. Не используйте эту опцию, если работете в команде.
echo 2. Использовать слияние [git pull origin main]. Это синхронизирует ваш локальный и удалёный репозиторий. Git попытается сохранить все изменения, которые были произведены. При возникновении конфликтов слияния, он об этом сообщит.
echo 3. Также, вы можете загрузить последнюю версию удалённого репозитория [git pull]. Эта операция полностью перезапишет ваш локальный репозиторий, даными из GitHub.
echo.
set /p choice2=Введите номер операции, которую вы хотите выполнить: 
echo.
color 07

if "%choice2%"=="1" (            
    echo git push -f origin main
    git push -f origin main
)

if "%choice2%"=="2" (
    echo git pull origin main
    git pull origin main
    echo.
    echo Если у вас появились ошибки на этом этапе, попробуйте провести слияние самостоятельно
    echo.
    echo Также не забывайте, что после успешного слияния необходимо создать и отправить новый коммит
    pause
    exit
)

if "%choice2%"=="3" (
    echo git fetch origin
    git fetch origin
    echo git reset --hard origin/main
    git reset --hard origin/main
    echo git pull
    git pull
)

REM Удаление файла last_commit.txt, если он существует
if exist ".git\last_commit.txt" (
    del ".git\last_commit.txt"
)

REM Создание нового файла ".git\last_commit.txt"
echo !commitMessage! > ".git\last_commit.txt"
REM В этом текстовом файле хранится описание последнего коммита

echo.
echo Операция успешно выполнена
color 20
pause
exit