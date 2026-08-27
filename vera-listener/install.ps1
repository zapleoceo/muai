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
# Get-ChildItem отдаёт FileInfo (.FullName), Get-Command — CommandInfo (.Source):
# берём то, что есть, иначе вторая ветка поиска молча даёт пустой путь.
if ($python) {
    $pythonPath = if ($python.FullName) { $python.FullName } else { $python.Source }
    $pythonHome = Split-Path -Parent $pythonPath
}

if (-not $ExePath) {
$venv = Join-Path $Root "venv"
if (-not (Test-Path $venv)) {
    Write-Host "Создаю venv в $venv"
    & $pythonPath -m venv $venv
}
$venvPy = Join-Path $venv "Scripts\python.exe"
$venvPyw = Join-Path $venv "Scripts\pythonw.exe"

Write-Host "Ставлю зависимости (это надолго: OpenVINO тянет ~200 МБ)"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install --quiet $source

& $venvPy -m vera_listener --setup

# В Диспетчере задач процесс должен называться VeraListener, а не pythonw.
# Собранный PyInstaller-ом exe для этого не годится, если на машине включён
# Smart App Control: он режет любой неподписанный бинарник (поймано вживую,
# Code Integrity 3077). Копия настоящего интерпретатора сохраняет подпись
# Python Software Foundation внутри самого PE-файла, поэтому политику проходит.
#
# Копируем именно БАЗОВЫЙ pythonw.exe, а НЕ venv-овский: тот лишь перенаправляет
# запуск и порождает настоящий интерпретатор дочерним процессом. Имя тогда
# получает пустышка на один поток и 2 МБ, а работу — и место в Диспетчере
# задач — дочерний pythonw.exe. Поймано вживую при первой же проверке нагрузки.
#
# Рядом кладём ВСЕ библиотеки из каталога интерпретатора: без python312.dll
# копия не стартует вовсе, а без vcruntime140* падает уже на импорте нативных
# модулей — «DLL load failed while importing silero_vad». Своей папки копии
# хватает: PATH под планировщиком каталога Python не содержит. Всё это
# обновляется при каждом запуске установщика, поэтому после обновления Python
# его надо прогнать заново — иначе библиотеки разъедутся со стандартной
# библиотекой venv.
$launcher = Join-Path $venv "Scripts\VeraListener.exe"

# Работающий слушатель держит свои exe и dll открытыми, и Copy-Item падает с
# IOException — повторная установка молча оставалась бы на старых файлах.
# Поэтому сначала останавливаем задачу и ждём, пока процесс действительно умрёт.
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
foreach ($i in 1..20) {
    $alive = Get-Process -Name "VeraListener" -ErrorAction SilentlyContinue
    if (-not $alive) { break }
    if ($i -eq 10) { $alive | Stop-Process -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
}

Copy-Item (Join-Path $pythonHome "pythonw.exe") $launcher -Force
foreach ($dll in @("python3.dll", "python312.dll",
                   "vcruntime140.dll", "vcruntime140_1.dll")) {
    $from = Join-Path $pythonHome $dll
    if (Test-Path $from) { Copy-Item $from (Join-Path $venv "Scripts\$dll") -Force }
}

$runner = $launcher
$runnerArgs = "-m vera_listener"
$workdir = $Root

# Прогрев ПОСЛЕ остановки задачи, а не до неё: кэш компиляции OpenVINO один на
# всё состояние слушателя, и живой старый процесс писал бы в него одновременно
# с прогревом — битый кэш пришлось бы пересобирать, а то и ловить как ошибку
# загрузки при следующем старте. Нашло ревью.
#
# Без прогрева первый же разговор ждал бы загрузку модели (790 МБ) и первую
# компиляцию под нейропроцессор — замер: 153 секунды. Звук при этом копится в
# памяти и не теряется, но выжимка пришла бы через минуты, а со стороны
# выглядело бы как повисание. Компиляция кэшируется — платим один раз здесь.
Write-Host "Прогреваю распознавание (первый раз это минуты: модель 790 МБ + компиляция)"
& $venvPy -m vera_listener --warmup
if ($LASTEXITCODE -ne 0) {
    # Молчать нельзя: иначе задача планировщика встанет, а сбой перенесётся на
    # первый реальный разговор в фоне, без консоли — то есть ровно в тот
    # сценарий, ради которого прогрев и добавлен.
    throw "Прогрев распознавания не прошёл (код $LASTEXITCODE). Проверь сеть и место на диске, затем запусти установщик заново."
}
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
