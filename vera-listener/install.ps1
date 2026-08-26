# Установка слушателя: venv, зависимости, задача планировщика, конфиг.
# Запускать из СВОЕЙ сессии Windows (обычный PowerShell, без админа).
#
# ФАЙЛ ОБЯЗАН БЫТЬ В UTF-8 С BOM И С CRLF. Windows PowerShell 5.1 читает .ps1
# без BOM как ANSI: кириллица в комментариях рассыпается, мусорная кавычка
# съедает закрывающую скобку, и файл падает с "Missing closing '}'" на строке,
# где всё в порядке. Именно поэтому установщик не запускался ни разу с момента
# появления — слушатель так и не был установлен. .gitattributes держит CRLF.
[CmdletBinding()]
param(
    # НЕ AppData/Local: у упакованных (MSIX) приложений он виртуализирован,
    # и задача планировщика снаружи контейнера не находит venv (0x80070002).
    # Поймано вживую при первой установке — задача падала мгновенно.
    [string]$Root = "$env:USERPROFILE\.vera\listener",
    [string]$TaskName = "VeraListener",
    # Путь к собранному VeraListener.exe. Указан — задача запускает обычное
    # приложение (в Диспетчере задач так и называется), и venv на этой машине не
    # нужен вовсе. Не указан — работаем через venv, как раньше.
    [string]$ExePath = "",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$source = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Задача $TaskName снята. Очередь и конфиг остались в $Root"
    return
}

if ($ExePath) {
    if (-not (Test-Path $ExePath)) { throw "Нет файла $ExePath" }
    $ExePath = (Resolve-Path $ExePath).Path
    Write-Host "Ставлю как приложение: $ExePath"
    $runner = $ExePath
    $runnerArgs = ""
    $workdir = Split-Path -Parent $ExePath
}

$python = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python3.12 -ErrorAction SilentlyContinue }
if (-not $python -and -not $ExePath) {
    throw "Нет Python 3.12. Поставь: winget install --id Python.Python.3.12 --scope user"
}

if (-not $ExePath) {
$venv = Join-Path $Root "venv"
if (-not (Test-Path $venv)) {
    Write-Host "Создаю venv в $venv"
    & $python.FullName -m venv $venv
}
$venvPy = Join-Path $venv "Scripts\python.exe"
$venvPyw = Join-Path $venv "Scripts\pythonw.exe"

Write-Host "Ставлю зависимости (это надолго: faster-whisper тянет ~300 МБ)"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install --quiet $source

& $venvPy -m vera_listener --setup

# В Диспетчере задач процесс должен называться VeraListener, а не pythonw.
# Собранный PyInstaller-ом exe для этого не годится, если на машине включён
# Smart App Control: он режет любой неподписанный бинарник (поймано вживую,
# Code Integrity 3077). Переименованная копия pythonw сохраняет подпись Python
# Software Foundation внутри самого PE-файла, поэтому проходит политику — и
# ровно так же venv кладёт свои python.exe в Scripts.
$launcher = Join-Path $venv "Scripts\VeraListener.exe"
Copy-Item $venvPyw $launcher -Force

$runner = $launcher
$runnerArgs = "-m vera_listener"
$workdir = $Root
}

# Одна задача с двумя триггерами: вход в систему поднимает слушателя, а
# повтор раз в 15 минут работает вотчдогом — новый запуск игнорируется, пока
# процесс жив, и подхватывает его, если тот умер.
$action = if ($runnerArgs) {
    New-ScheduledTaskAction -Execute $runner -Argument $runnerArgs -WorkingDirectory $workdir
} else {
    New-ScheduledTaskAction -Execute $runner -WorkingDirectory $workdir
}
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# RepetitionDuration: НЕ [TimeSpan]::MaxValue — планировщик отвергает такую
# задачу целиком ("value which is incorrectly formatted or out of range",
# P99999999DT23H59M59S). Год с запасом, а триггер входа перезаводит повтор
# при каждом логоне, так что бесконечность и не нужна.
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 365)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
# Interactive: слушателю нужны аудио-устройства СВОЕЙ сессии Windows. Служба в
# сессии 0 их не слышит вообще — это свойство Windows, а не настройка.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atLogon, $watchdog) `
    -Settings $settings -Principal $principal -Force | Out-Null

# Триггер входа сработает только при следующем логоне, а слушатель нужен
# сейчас — поднимаем сразу. Повторный запуск безопасен: MultipleInstances
# IgnoreNew не даст второй копии.
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
$state = (Get-ScheduledTask -TaskName $TaskName).State

Write-Host ""
Write-Host "Задача $TaskName зарегистрирована и запущена, состояние: $state"
Write-Host "В трее должен появиться кружок: серый - тишина, зелёный - идёт разговор."
Write-Host ""
Write-Host "Готово. Дальше:"
if ($ExePath) {
    Write-Host "  проверь захват:  `"$ExePath`" --probe 15"
} else {
    Write-Host "  проверь захват:  $venvPy -m vera_listener --probe 15"
}
Write-Host "  логи: $Root\listener.log   очередь: $Root\queue"
