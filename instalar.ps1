Add-Type -AssemblyName System.Windows.Forms

# ============================================================
#  Instalador de WSTunnel Portable
#  - Prepara el icono
#  - Crea acceso directo en el Escritorio
#  - Opcionalmente lo anyade al inicio de Windows
# ============================================================

$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$batPath    = Join-Path $scriptDir "iniciar_tunel.bat"
$iconDest   = Join-Path $scriptDir "app_icon.ico"
$iconSrc    = Join-Path $scriptDir "app_icon-removebg-preview.ico"
$shortName  = "WSTunnel.lnk"

# -- 1) Verificar el .bat --
if (-not (Test-Path $batPath)) {
    [System.Windows.Forms.MessageBox]::Show(
        "No se encontro iniciar_tunel.bat en:`n$scriptDir",
        "Instalador WSTunnel", "OK", "Error") | Out-Null
    exit
}

# -- 2) Preparar el icono (si no existe app_icon.ico, copiarlo del de removebg) --
if (-not (Test-Path $iconDest)) {
    if (Test-Path $iconSrc) {
        Copy-Item -Path $iconSrc -Destination $iconDest -Force
        Write-Host "Icono preparado: app_icon.ico"
    } else {
        Write-Host "Aviso: no se encontro ningun icono .ico. El acceso directo usara el icono por defecto."
    }
}

# -- 3) Funcion para crear un acceso directo --
#    Apunta a powershell.exe (no al .bat directamente) para que Windows
#    lo trate como una app normal y permita fijarlo en Inicio/barra de tareas.
function New-Shortcut {
    param(
        [string]$LinkPath,
        [string]$PsScript,
        [string]$IconPath,
        [string]$WorkingDir,
        [string]$Description = "WSTunnel - Tunel WireGuard sobre WebSocket"
    )
    $psExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $args  = "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PsScript`""

    $wsh = New-Object -ComObject WScript.Shell
    $sc  = $wsh.CreateShortcut($LinkPath)
    $sc.TargetPath       = $psExe
    $sc.Arguments        = $args
    $sc.WorkingDirectory = $WorkingDir
    $sc.Description      = $Description
    $sc.WindowStyle      = 7   # Minimizado
    if ($IconPath -and (Test-Path $IconPath)) {
        $sc.IconLocation = "$IconPath,0"
    }
    $sc.Save()

    # Marcar "Ejecutar como administrador" en el .lnk (byte 0x15, bit 0x20)
    try {
        $bytes = [System.IO.File]::ReadAllBytes($LinkPath)
        $bytes[0x15] = $bytes[0x15] -bor 0x20
        [System.IO.File]::WriteAllBytes($LinkPath, $bytes)
    } catch {}
}

# -- 4) Acceso directo en el Escritorio --
$desktop     = [Environment]::GetFolderPath("Desktop")
$desktopLink = Join-Path $desktop $shortName
New-Shortcut -LinkPath $desktopLink -PsScript "$scriptDir\iniciar_tunel.ps1" -IconPath $iconDest -WorkingDir $scriptDir
Write-Host "Acceso directo creado en el Escritorio."

# -- 5) Preguntar por el inicio de Windows --
$resp = [System.Windows.Forms.MessageBox]::Show(
    "Quieres que WSTunnel se ejecute automaticamente al iniciar Windows?",
    "Instalador WSTunnel", "YesNo", "Question")

$startupFolder = [Environment]::GetFolderPath("Startup")
$startupLink   = Join-Path $startupFolder $shortName

if ($resp -eq [System.Windows.Forms.DialogResult]::Yes) {
    New-Shortcut -LinkPath $startupLink -PsScript "$scriptDir\iniciar_tunel.ps1" -IconPath $iconDest -WorkingDir $scriptDir
    Write-Host "Acceso directo creado en la carpeta Inicio: $startupFolder"
    $msg = "Instalacion completada.`n`n" +
           "- Acceso directo en el Escritorio: si`n" +
           "- Arranque con Windows: si`n`n" +
           "Puedes lanzarlo desde el icono del Escritorio o esperar al siguiente inicio de sesion.`n`n" +
           "Para fijarlo en el menu Inicio o la barra de tareas:`n" +
           "clic derecho sobre el icono del Escritorio -> 'Fijar en Inicio' o 'Fijar en la barra de tareas'."
} else {
    if (Test-Path $startupLink) { Remove-Item $startupLink -Force -ErrorAction SilentlyContinue }
    $msg = "Instalacion completada.`n`n" +
           "- Acceso directo en el Escritorio: si`n" +
           "- Arranque con Windows: no`n`n" +
           "Para activar el arranque automatico, vuelve a ejecutar este instalador.`n`n" +
           "Para fijarlo en el menu Inicio o la barra de tareas:`n" +
           "clic derecho sobre el icono del Escritorio -> 'Fijar en Inicio' o 'Fijar en la barra de tareas'."
}

[System.Windows.Forms.MessageBox]::Show($msg, "Instalador WSTunnel", "OK", "Information") | Out-Null
