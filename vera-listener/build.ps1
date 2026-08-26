# Сборка слушателя в обычное приложение Windows: dist\VeraListener\VeraListener.exe
#
# Зачем: в Диспетчере задач процесс называется VeraListener, а не pythonw.exe, и
# папку можно унести на другой ноутбук — Python там не нужен.
#
# ФАЙЛ ОБЯЗАН БЫТЬ В UTF-8 С BOM И С CRLF (см. install.ps1 — то же правило).
[CmdletBinding()]
param(
    [string]$Venv = "$env:USERPROFILE\.vera\listenerenv",
    [switch]$Zip
)

$ErrorActionPreference = "Stop"
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $source
try {
    $py = Join-Path $Venv "Scripts\python.exe"
    if (-not (Test-Path $py)) {
        throw "Нет venv в $Venv. Сначала install.ps1"
    }

    & $py -m pip install --quiet --upgrade pyinstaller
    # Иконка рисуется тем же кодом, что и трей: один источник правды.
    & $py -c "import sys; sys.path.insert(0, 'src'); from vera_listener import tray; tray.write_ico('packaging/VeraListener.ico')"
    & $py -m PyInstaller --noconfirm --clean VeraListener.spec

    $out = Join-Path $source "dist\VeraListener"
    $exe = Join-Path $out "VeraListener.exe"
    if (-not (Test-Path $exe)) { throw "Сборка не дала $exe" }
    $size = [math]::Round((Get-ChildItem $out -Recurse | Measure-Object Length -Sum).Sum / 1MB)

    Write-Host ""
    Write-Host "Готово: $exe ($size МБ)"

    if ($Zip) {
        $zip = Join-Path $source "dist\VeraListener.zip"
        if (Test-Path $zip) { Remove-Item $zip }
        Compress-Archive -Path "$out\*" -DestinationPath $zip
        Write-Host "Архив для переноса: $zip"
    }

    Write-Host ""
    Write-Host "На другом ноутбуке:"
    Write-Host "  1. распакуй папку куда угодно"
    Write-Host "  2. создай %USERPROFILE%\.vera\listener.env с VERA_GATEWAY_URL и INTERNAL_SECRET"
    Write-Host "  3. .\VeraListener.exe --probe 15   (из своей сессии Windows!)"
    Write-Host "  4. .\install.ps1 -ExePath <путь к VeraListener.exe>  — автозапуск"
    Write-Host ""
    Write-Host "Модель распознавания (~500 МБ) скачается при первом разговоре — нужен интернет."
}
finally {
    Pop-Location
}
