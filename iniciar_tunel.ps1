Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$ErrorActionPreference = "Stop"

# ============================================================
#  Rutas y variables globales
# ============================================================
$script:scriptDir            = Split-Path -Parent $MyInvocation.MyCommand.Definition
$script:wstunnelExe          = Join-Path $script:scriptDir "wstunnel.exe"
$script:logFile              = Join-Path $script:scriptDir "wstunnel.log"
$script:configFile           = Join-Path $script:scriptDir "tunnel_config.json"
$script:wireguardExe         = "C:\Program Files\WireGuard\wireguard.exe"
$script:appIcon              = Join-Path $script:scriptDir "app_icon.ico"
$script:tempCmd              = Join-Path $script:scriptDir "_run_wstunnel.cmd"
$script:wgConfPath           = ""
$script:wgTunnelName         = $null
$script:tunnelProc           = $null
$script:gateway              = $null
$script:notifyIcon           = $null

# Reconexión
$script:reconnectAttempt     = 0
$script:maxReconnectAttempts = 5
$script:reconnectDelays      = @(5, 10, 20, 30, 60)   # segundos entre intentos
$script:isDisconnecting      = $false
$script:reconnectTimer       = $null
$script:stabilityTimer       = $null
$script:watchTimer           = $null
$script:pendingReconnect     = $false

# Ventana de logs
$script:logForm              = $null
$script:logTextBox           = $null
$script:logTimer             = $null
$script:lastLogPos           = 0

# ============================================================
#  Mutex: evitar doble instancia
# ============================================================
$mutexName    = "Global\WSTunnelPortable"
$script:mutex = New-Object System.Threading.Mutex($false, $mutexName)
if (-not $script:mutex.WaitOne(0)) {
    $resp = [System.Windows.Forms.MessageBox]::Show(
        "WSTunnel ya esta en ejecucion (revisa el icono en la bandeja, esquina inferior derecha).`n`n" +
        "Pulsa 'Reiniciar' para cerrar la instancia actual y abrir una nueva.`n" +
        "Pulsa 'Cancelar' para dejarlo como esta.",
        "WSTunnel ya en ejecucion",
        [System.Windows.Forms.MessageBoxButtons]::OKCancel,
        [System.Windows.Forms.MessageBoxIcon]::Information)

    if ($resp -eq [System.Windows.Forms.DialogResult]::OK) {
        # Matar instancia anterior: buscar el proceso powershell que corre nuestro .ps1
        $myScript = $MyInvocation.MyCommand.Definition
        Get-WmiObject Win32_Process -Filter "Name='powershell.exe'" |
            Where-Object { $_.CommandLine -like "*iniciar_tunel.ps1*" } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        # Matar tambien wstunnel y wireguard
        Get-Process -Name "wstunnel"  -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Get-Process -Name "wireguard" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 800
        # Esperar a que el mutex quede libre (maximo 3s)
        if (-not $script:mutex.WaitOne(3000)) {
            [System.Windows.Forms.MessageBox]::Show(
                "No se pudo liberar la instancia anterior. Cierra WSTunnel manualmente desde la bandeja e intentalo de nuevo.",
                "WSTunnel", "OK", "Error") | Out-Null
            exit
        }
        # El mutex ya es nuestro, continuamos la ejecucion normal
    } else {
        exit
    }
}

# ============================================================
#  Helpers
# ============================================================
function Write-Log {
    param([string]$Message)
    try {
        "[$(Get-Date -Format 'HH:mm:ss')] $Message" |
            Add-Content -Path $script:logFile -Encoding UTF8
    } catch {}
}

function Rotate-Log {
    # Limita el log a los ultimos 512 KB para evitar que crezca indefinidamente
    $maxBytes = 512 * 1024
    if (-not (Test-Path $script:logFile)) { return }
    try {
        $info = Get-Item $script:logFile
        if ($info.Length -le $maxBytes) { return }
        $content = [System.IO.File]::ReadAllText($script:logFile, [System.Text.Encoding]::UTF8)
        $trimmed = $content.Substring($content.Length - $maxBytes)
        # Recortar hasta el primer salto de linea completo
        $idx = $trimmed.IndexOf("`n")
        if ($idx -ge 0) { $trimmed = $trimmed.Substring($idx + 1) }
        $header = "--- Log rotado el $(Get-Date -Format 'dd/MM/yyyy HH:mm:ss') (se conservan los ultimos 512 KB) ---`n"
        [System.IO.File]::WriteAllText($script:logFile, $header + $trimmed, [System.Text.Encoding]::UTF8)
    } catch {}
}

function Test-ServerReachable {
    # Comprueba si el servidor acepta conexiones TCP antes de intentar el tunel
    param(
        [string]$Hostname  = "vpn.fcrespo.tech",
        [int]   $Port      = 443,
        [int]   $TimeoutMs = 3000
    )
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $ar  = $tcp.BeginConnect($Hostname, $Port, $null, $null)
        $ok  = $ar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)
        if ($ok -and $tcp.Connected) { $tcp.Close(); return $true }
        try { $tcp.Close() } catch {}
        return $false
    } catch { return $false }
}

# ============================================================
#  Crear acceso directo en Escritorio y/o Inicio de Windows
# ============================================================
function New-AppShortcut {
    $psExe     = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $psScript  = Join-Path $script:scriptDir "iniciar_tunel.ps1"
    $iconPath  = $script:appIcon
    $shortName = "WSTunnel.lnk"

    # Preparar icono: si no existe app_icon.ico, copiarlo del removebg
    if (-not (Test-Path $iconPath)) {
        $iconSrc = Join-Path $script:scriptDir "app_icon-removebg-preview.ico"
        if (Test-Path $iconSrc) { Copy-Item $iconSrc $iconPath -Force }
    }

    function _CreateLnk {
        param([string]$dest)
        $wsh = New-Object -ComObject WScript.Shell
        $sc  = $wsh.CreateShortcut($dest)
        $sc.TargetPath       = $psExe
        $sc.Arguments        = "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$psScript`""
        $sc.WorkingDirectory = $script:scriptDir
        $sc.Description      = "WSTunnel - Tunel WireGuard sobre WebSocket"
        $sc.WindowStyle      = 7
        if (Test-Path $iconPath) { $sc.IconLocation = "$iconPath,0" }
        $sc.Save()
        # Marcar "Ejecutar como administrador" (byte 0x15, bit 0x20)
        try {
            $bytes = [System.IO.File]::ReadAllBytes($dest)
            $bytes[0x15] = $bytes[0x15] -bor 0x20
            [System.IO.File]::WriteAllBytes($dest, $bytes)
        } catch {}
    }

    $desktop       = [Environment]::GetFolderPath("Desktop")
    $startupFolder = [Environment]::GetFolderPath("Startup")
    $desktopLink   = Join-Path $desktop       $shortName
    $startupLink   = Join-Path $startupFolder $shortName

    # Formulario de opciones
    $f                 = New-Object System.Windows.Forms.Form
    $f.Text            = "WSTunnel - Acceso Directo"
    $f.Size            = New-Object System.Drawing.Size(360, 230)
    $f.StartPosition   = "CenterScreen"
    $f.FormBorderStyle = "FixedDialog"
    $f.MaximizeBox     = $false
    $f.MinimizeBox     = $false
    $f.TopMost         = $true
    if (Test-Path $iconPath) { $f.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon($iconPath) }

    $lbl          = New-Object System.Windows.Forms.Label
    $lbl.Text     = "Selecciona donde crear el acceso directo:"
    $lbl.Location = New-Object System.Drawing.Point(20, 20)
    $lbl.Size     = New-Object System.Drawing.Size(310, 20)
    $f.Controls.Add($lbl)

    $chkDesktop          = New-Object System.Windows.Forms.CheckBox
    $chkDesktop.Text     = "Escritorio"
    $chkDesktop.Location = New-Object System.Drawing.Point(30, 55)
    $chkDesktop.Size     = New-Object System.Drawing.Size(290, 24)
    $chkDesktop.Checked  = $true
    $f.Controls.Add($chkDesktop)

    $chkStartup          = New-Object System.Windows.Forms.CheckBox
    $chkStartup.Text     = "Inicio de Windows (arrancar automaticamente)"
    $chkStartup.Location = New-Object System.Drawing.Point(30, 85)
    $chkStartup.Size     = New-Object System.Drawing.Size(290, 24)
    $chkStartup.Checked  = Test-Path $startupLink
    $f.Controls.Add($chkStartup)

    $btnOk              = New-Object System.Windows.Forms.Button
    $btnOk.Text         = "Crear"
    $btnOk.Location     = New-Object System.Drawing.Point(80, 140)
    $btnOk.Size         = New-Object System.Drawing.Size(90, 30)
    $btnOk.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $f.Controls.Add($btnOk)

    $btnCancel              = New-Object System.Windows.Forms.Button
    $btnCancel.Text         = "Cancelar"
    $btnCancel.Location     = New-Object System.Drawing.Point(185, 140)
    $btnCancel.Size         = New-Object System.Drawing.Size(90, 30)
    $btnCancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $f.Controls.Add($btnCancel)

    $f.AcceptButton = $btnOk
    $f.CancelButton = $btnCancel

    if ($f.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { return }

    $created = @()

    if ($chkDesktop.Checked) {
        _CreateLnk $desktopLink
        $created += "Escritorio"
    }
    if ($chkStartup.Checked) {
        _CreateLnk $startupLink
        $created += "Inicio de Windows"
    } elseif (Test-Path $startupLink) {
        # Si estaba marcado y ahora no, quitarlo
        Remove-Item $startupLink -Force -ErrorAction SilentlyContinue
    }

    if ($created.Count -gt 0) {
        [System.Windows.Forms.MessageBox]::Show(
            "Acceso directo creado en:`n- " + ($created -join "`n- ") +
            "`n`nPara fijarlo en la barra de tareas o el menu Inicio, haz clic derecho sobre el icono del Escritorio.",
            "WSTunnel", "OK", "Information") | Out-Null
    }
}

# ============================================================
#  Seleccion de tunel WireGuard
# ============================================================
function Get-TunnelChoice {
    $wgDataDir = "C:\Program Files\WireGuard\Data\Configurations"
    if (-not (Test-Path $wgDataDir)) {
        [System.Windows.Forms.MessageBox]::Show(
            "No se encontro la carpeta de configuraciones de WireGuard.`n¿Esta instalado en C:\Program Files\WireGuard?",
            "Error", "OK", "Error") | Out-Null
        return $null
    }

    $files = Get-ChildItem -Path $wgDataDir -Filter "*.conf.dpapi"
    if ($files.Count -eq 0) {
        [System.Windows.Forms.MessageBox]::Show(
            "No se encontraron tuneles configurados en WireGuard.`nPor favor, importa un tunel primero en la aplicacion de WireGuard.",
            "Error", "OK", "Error") | Out-Null
        return $null
    }

    $form                 = New-Object System.Windows.Forms.Form
    $form.Text            = "WSTunnel - Seleccionar Tunel"
    $form.Size            = New-Object System.Drawing.Size(350, 295)
    $form.StartPosition   = "CenterScreen"
    $form.FormBorderStyle = "FixedDialog"
    $form.MaximizeBox     = $false
    $form.MinimizeBox     = $false
    $form.TopMost         = $true
    if (Test-Path $script:appIcon) {
        $form.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon($script:appIcon)
    }

    $label          = New-Object System.Windows.Forms.Label
    $label.Text     = "Se han detectado los siguientes tuneles.`nSelecciona el que quieras usar:"
    $label.Location = New-Object System.Drawing.Point(20, 20)
    $label.Size     = New-Object System.Drawing.Size(300, 40)
    $form.Controls.Add($label)

    $listBox          = New-Object System.Windows.Forms.ListBox
    $listBox.Location = New-Object System.Drawing.Point(20, 70)
    $listBox.Size     = New-Object System.Drawing.Size(290, 80)
    foreach ($file in $files) {
        $name = $file.BaseName.Replace(".conf", "")
        $listBox.Items.Add($name) | Out-Null
    }
    $listBox.SelectedIndex = 0
    $form.Controls.Add($listBox)

    $btnOk              = New-Object System.Windows.Forms.Button
    $btnOk.Text         = "Guardar y Conectar"
    $btnOk.Location     = New-Object System.Drawing.Point(20, 165)
    $btnOk.Size         = New-Object System.Drawing.Size(145, 30)
    $btnOk.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $btnOk.Add_Click({ $form.Close() })
    $form.AcceptButton  = $btnOk
    $form.Controls.Add($btnOk)

    $btnShortcut          = New-Object System.Windows.Forms.Button
    $btnShortcut.Text     = "Acceso Directo..."
    $btnShortcut.Location = New-Object System.Drawing.Point(175, 165)
    $btnShortcut.Size     = New-Object System.Drawing.Size(135, 30)
    $btnShortcut.Add_Click({ New-AppShortcut })
    $form.Controls.Add($btnShortcut)

    $sep          = New-Object System.Windows.Forms.Label
    $sep.BorderStyle = "Fixed3D"
    $sep.Location = New-Object System.Drawing.Point(20, 208)
    $sep.Size     = New-Object System.Drawing.Size(290, 2)
    $form.Controls.Add($sep)

    $lblHint          = New-Object System.Windows.Forms.Label
    $lblHint.Text     = "Tip: usa 'Acceso Directo' para fijar WSTunnel en el Escritorio o el inicio de Windows."
    $lblHint.Location = New-Object System.Drawing.Point(20, 215)
    $lblHint.Size     = New-Object System.Drawing.Size(290, 34)
    $lblHint.ForeColor = [System.Drawing.Color]::Gray
    $lblHint.Font      = New-Object System.Drawing.Font("Segoe UI", 8)
    $form.Controls.Add($lblHint)

    if ($form.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        return $listBox.SelectedItem.ToString()
    }
    return $null
}

# ============================================================
#  Estado del tray: icono + tooltip segun estado
# ============================================================
function Update-TrayStatus {
    param([string]$State)   # 'connected' | 'reconnecting' | 'failed'
    if (-not $script:notifyIcon) { return }
    switch ($State) {
        'connected' {
            $script:notifyIcon.Icon = if (Test-Path $script:appIcon) {
                [System.Drawing.Icon]::ExtractAssociatedIcon($script:appIcon)
            } else { [System.Drawing.SystemIcons]::Shield }
            $script:notifyIcon.Text = "WSTunnel ($script:wgTunnelName) - Conectado"
        }
        'reconnecting' {
            $script:notifyIcon.Icon = [System.Drawing.SystemIcons]::Warning
            $script:notifyIcon.Text = "WSTunnel ($script:wgTunnelName) - Reconectando ($script:reconnectAttempt/$script:maxReconnectAttempts)..."
        }
        'failed' {
            $script:notifyIcon.Icon = [System.Drawing.SystemIcons]::Error
            $script:notifyIcon.Text = "WSTunnel ($script:wgTunnelName) - Sin conexion"
        }
    }
}

# ============================================================
#  Lanzar proceso wstunnel + WireGuard
# ============================================================
function Start-TunnelProcess {
    # Matar instancia anterior de wstunnel si queda en pie
    Get-Process -Name "wstunnel" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 300

    # Generar comando temporal
    @"
@echo off
"$($script:wstunnelExe)" client -L "udp://127.0.0.1:51820:10.43.9.43:51820?timeout_sec=0" --http-upgrade-path-prefix "ClaveSegura123" wss://vpn.fcrespo.tech >> "$($script:logFile)" 2>&1
"@ | Set-Content -Path $script:tempCmd -Encoding ASCII

    $psi                  = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName         = $script:tempCmd
    $psi.CreateNoWindow   = $true
    $psi.UseShellExecute  = $false
    $psi.WorkingDirectory = $script:scriptDir

    $script:tunnelProc = [System.Diagnostics.Process]::Start($psi)
    Start-Sleep -Milliseconds 500

    if ($script:tunnelProc.HasExited) {
        Write-Log "ERROR: wstunnel.exe se cerro inmediatamente tras el lanzamiento."
        return $false
    }

    # Instalar servicio WireGuard solo si no esta ya activo
    if ($script:wgConfPath -ne "" -and (Test-Path $script:wgConfPath) -and (Test-Path $script:wireguardExe)) {
        $svc = Get-Service -Name "WireGuardTunnel$($script:wgTunnelName)" -ErrorAction SilentlyContinue
        if (-not $svc) {
            $wgPsi                 = New-Object System.Diagnostics.ProcessStartInfo
            $wgPsi.FileName        = $script:wireguardExe
            $wgPsi.Arguments       = "/installtunnelservice `"$script:wgConfPath`""
            $wgPsi.CreateNoWindow  = $true
            $wgPsi.UseShellExecute = $false
            [System.Diagnostics.Process]::Start($wgPsi) | Out-Null
        }
    }

    # Timer de estabilidad: si el proceso sobrevive 30s, reinicia el contador de reconexion
    if (-not $script:stabilityTimer) {
        $script:stabilityTimer          = New-Object System.Windows.Forms.Timer
        $script:stabilityTimer.Interval = 30000
        $script:stabilityTimer.Add_Tick({
            $script:stabilityTimer.Stop()
            if (-not $script:isDisconnecting -and
                $script:tunnelProc -and
                -not $script:tunnelProc.HasExited -and
                $script:reconnectAttempt -gt 0) {
                $script:reconnectAttempt = 0
                Write-Log "INFO: Conexion estable 30s. Contador de reconexion reiniciado."
            }
        })
    } else {
        $script:stabilityTimer.Stop()
    }
    $script:stabilityTimer.Start()

    return $true
}

# ============================================================
#  Reconexion automatica con backoff exponencial
# ============================================================
function Invoke-Reconnect {
    if ($script:isDisconnecting)   { return }
    # Si ya hay una reconexion programada, no reprogramar (evita resetear el countdown)
    if ($script:pendingReconnect)  { return }

    $script:reconnectAttempt++

    if ($script:reconnectAttempt -gt $script:maxReconnectAttempts) {
        # Parar el watchdog para que no siga llamando a Invoke-Reconnect
        if ($script:watchTimer) { $script:watchTimer.Stop() }
        Update-TrayStatus 'failed'
        Write-Log "ERROR: Maximo de intentos de reconexion ($script:maxReconnectAttempts) alcanzado. Se requiere accion manual."
        $script:notifyIcon.ShowBalloonTip(
            6000, "WSTunnel",
            "No se pudo reconectar tras $script:maxReconnectAttempts intentos.`nUsa 'Reconectar' en el menu para reintentar.",
            [System.Windows.Forms.ToolTipIcon]::Error)
        return
    }

    $delayS = $script:reconnectDelays[$script:reconnectAttempt - 1]
    Update-TrayStatus 'reconnecting'
    Write-Log "INFO: Tunel caido. Reintentando en ${delayS}s (intento $script:reconnectAttempt/$script:maxReconnectAttempts)..."
    $script:notifyIcon.ShowBalloonTip(
        3000, "WSTunnel",
        "Tunel caido. Reconectando en ${delayS}s (intento $script:reconnectAttempt/$script:maxReconnectAttempts)...",
        [System.Windows.Forms.ToolTipIcon]::Warning)

    # Crear el timer de reconexion la primera vez; reutilizarlo en adelante
    if (-not $script:reconnectTimer) {
        $script:reconnectTimer = New-Object System.Windows.Forms.Timer
        $script:reconnectTimer.Add_Tick({
            $script:reconnectTimer.Stop()
            $script:pendingReconnect = $false
            if ($script:isDisconnecting) { return }
            Write-Log "INFO: Ejecutando intento de reconexion #$script:reconnectAttempt..."
            $ok = Start-TunnelProcess
            if ($ok) {
                Update-TrayStatus 'connected'
                $script:notifyIcon.ShowBalloonTip(
                    3000, "WSTunnel",
                    "Tunel reconectado ($script:wgTunnelName)",
                    [System.Windows.Forms.ToolTipIcon]::Info)
                Write-Log "INFO: Reconexion exitosa."
            } else {
                Invoke-Reconnect   # el proceso murio de inmediato, siguiente intento
            }
        })
    }
    $script:pendingReconnect         = $true
    $script:reconnectTimer.Interval  = $delayS * 1000
    $script:reconnectTimer.Start()
}

# ============================================================
#  Reconexion manual desde el menu del tray
# ============================================================
function Reconnect-Now {
    if ($script:stabilityTimer)  { $script:stabilityTimer.Stop() }
    if ($script:reconnectTimer)  { $script:reconnectTimer.Stop() }

    # Matar proceso actual
    Get-Process -Name "wstunnel" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue

    $script:reconnectAttempt = 0
    $script:pendingReconnect = $false
    # Rearrancar el watchdog si estaba parado por haber agotado los intentos
    if ($script:watchTimer -and -not $script:watchTimer.Enabled) { $script:watchTimer.Start() }
    Update-TrayStatus 'reconnecting'
    Write-Log "INFO: Reconexion manual solicitada por el usuario."

    $ok = Start-TunnelProcess
    if ($ok) {
        Update-TrayStatus 'connected'
        $script:notifyIcon.ShowBalloonTip(
            3000, "WSTunnel",
            "Tunel reconectado ($script:wgTunnelName)",
            [System.Windows.Forms.ToolTipIcon]::Info)
        Write-Log "INFO: Reconexion manual exitosa."
    } else {
        Invoke-Reconnect
    }
}

# ============================================================
#  Ventana de logs en tiempo real
# ============================================================
function Show-LogWindow {
    if ($script:logForm -and -not $script:logForm.IsDisposed) {
        $script:logForm.Show()
        $script:logForm.WindowState = [System.Windows.Forms.FormWindowState]::Normal
        $script:logForm.BringToFront()
        return
    }

    $script:logForm               = New-Object System.Windows.Forms.Form
    $script:logForm.Text          = "WSTunnel - Logs (Portable)"
    $script:logForm.Size          = New-Object System.Drawing.Size(850, 520)
    $script:logForm.StartPosition = "CenterScreen"
    $script:logForm.BackColor     = [System.Drawing.Color]::FromArgb(25, 25, 25)
    if (Test-Path $script:appIcon) {
        $script:logForm.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon($script:appIcon)
    }

    $script:logTextBox             = New-Object System.Windows.Forms.RichTextBox
    $script:logTextBox.Dock        = "Fill"
    $script:logTextBox.ReadOnly    = $true
    $script:logTextBox.BackColor   = [System.Drawing.Color]::FromArgb(15, 15, 15)
    $script:logTextBox.ForeColor   = [System.Drawing.Color]::FromArgb(0, 230, 118)
    $script:logTextBox.Font        = New-Object System.Drawing.Font("Consolas", 10)
    $script:logTextBox.WordWrap    = $false
    $script:logTextBox.BorderStyle = "None"
    $script:logForm.Controls.Add($script:logTextBox)

    $script:lastLogPos = 0
    try {
        $fs      = [System.IO.File]::Open($script:logFile, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $reader  = New-Object System.IO.StreamReader($fs)
        $content = $reader.ReadToEnd()
        $script:lastLogPos = $fs.Position
        $reader.Close(); $fs.Close()
        if ($content) {
            $script:logTextBox.Text = $content
            $script:logTextBox.SelectionStart = $script:logTextBox.Text.Length
            $script:logTextBox.ScrollToCaret()
        }
    } catch {}

    $script:logTimer          = New-Object System.Windows.Forms.Timer
    $script:logTimer.Interval = 500
    $script:logTimer.Add_Tick({
        if (-not $script:logTextBox -or $script:logTextBox.IsDisposed) { return }
        try {
            $fs = [System.IO.File]::Open($script:logFile, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
            if ($fs.Length -gt $script:lastLogPos) {
                $fs.Seek($script:lastLogPos, [System.IO.SeekOrigin]::Begin) | Out-Null
                $reader     = New-Object System.IO.StreamReader($fs)
                $newContent = $reader.ReadToEnd()
                $script:lastLogPos = $fs.Position
                $reader.Close()
                if ($newContent) {
                    $script:logTextBox.AppendText($newContent)
                    $script:logTextBox.SelectionStart = $script:logTextBox.Text.Length
                    $script:logTextBox.ScrollToCaret()
                }
            }
            $fs.Close()
        } catch {}
    })
    $script:logTimer.Start()

    $script:logForm.Add_FormClosing({ param($s, $e) $e.Cancel = $true; $script:logForm.Hide() })
    $script:logForm.Show()
}

# ============================================================
#  Desconexion y limpieza completa
# ============================================================
function Disconnect-Tunnel {
    $script:isDisconnecting = $true

    if ($script:watchTimer)     { $script:watchTimer.Stop();     $script:watchTimer.Dispose() }
    if ($script:stabilityTimer) { $script:stabilityTimer.Stop(); $script:stabilityTimer.Dispose() }
    if ($script:reconnectTimer) { $script:reconnectTimer.Stop(); $script:reconnectTimer.Dispose() }
    if ($script:logTimer)       { $script:logTimer.Stop();       $script:logTimer.Dispose() }
    if ($script:logForm -and -not $script:logForm.IsDisposed) { $script:logForm.Dispose() }

    # Detener WireGuard: desinstalar el servicio del tunel y matar el proceso
    if ($script:wgTunnelName -and (Test-Path $script:wireguardExe)) {
        $svc = Get-Service -Name "WireGuardTunnel$($script:wgTunnelName)" -ErrorAction SilentlyContinue
        if ($svc) {
            $wgPsi                 = New-Object System.Diagnostics.ProcessStartInfo
            $wgPsi.FileName        = $script:wireguardExe
            $wgPsi.Arguments       = "/uninstalltunnelservice `"$($script:wgTunnelName)`""
            $wgPsi.CreateNoWindow  = $true
            $wgPsi.UseShellExecute = $false
            $wgProc = [System.Diagnostics.Process]::Start($wgPsi)
            # Esperar a que termine (maximo 5s) para asegurar que el servicio se desinstala
            $wgProc.WaitForExit(5000) | Out-Null
        }
        # Matar el proceso wireguard.exe si sigue en pie (GUI o servicio residual)
        Get-Process -Name "wireguard" -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
    }

    # Detener wstunnel
    Get-Process -Name "wstunnel" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    if ($script:tunnelProc -and -not $script:tunnelProc.HasExited) {
        try { $script:tunnelProc.Kill() } catch {}
    }

    # Limpiar rutas Cloudflare
    try { route delete 188.114.96.5 2>$null } catch {}
    try { route delete 188.114.97.5 2>$null } catch {}

    # Eliminar archivo temporal
    if ($script:tempCmd -and (Test-Path $script:tempCmd)) {
        Remove-Item $script:tempCmd -Force -ErrorAction SilentlyContinue
    }

    try { $script:mutex.ReleaseMutex() } catch {}

    $script:notifyIcon.Visible = $false
    $script:notifyIcon.Dispose()
    [System.Windows.Forms.Application]::Exit()
}

# ============================================================
#  MAIN
# ============================================================
try {
    # -- Cargar o pedir configuracion de tunel --
    if (Test-Path $script:configFile) {
        try {
            $config = Get-Content $script:configFile | ConvertFrom-Json
            $script:wgTunnelName = $config.TunnelName
        } catch {}
    }

    if ($script:wgTunnelName) {
        $script:wgConfPath = "C:\Program Files\WireGuard\Data\Configurations\$($script:wgTunnelName).conf.dpapi"
    }

    if (-not $script:wgTunnelName -or -not (Test-Path $script:wgConfPath)) {
        $script:wgTunnelName = Get-TunnelChoice
        if (-not $script:wgTunnelName) {
            try { $script:mutex.ReleaseMutex() } catch {}
            exit
        }
        @{ TunnelName = $script:wgTunnelName } | ConvertTo-Json | Set-Content $script:configFile -Force
        $script:wgConfPath = "C:\Program Files\WireGuard\Data\Configurations\$($script:wgTunnelName).conf.dpapi"
    }

    if (-not (Test-Path $script:wstunnelExe)) {
        throw "No se encontro wstunnel.exe en: $script:scriptDir"
    }

    # -- Rotar log si supera 512 KB --
    Rotate-Log

    # -- Cabecera de sesion en el log --
    Add-Content -Path $script:logFile -Encoding UTF8 -Value `
        "[$(Get-Date -Format 'HH:mm:ss')] ===== WSTunnel iniciando (Portable) -- Tunel: $script:wgTunnelName ====="

    # -- Obtener puerta de enlace --
    $script:gateway = (Get-NetRoute -DestinationPrefix "0.0.0.0/0" |
                        Sort-Object RouteMetric |
                        Select-Object -First 1).NextHop
    if (-not $script:gateway) { throw "No se pudo detectar la puerta de enlace." }

    # -- Rutas Cloudflare (con manejo de error granular) --
    try { route delete 188.114.96.5 2>$null } catch {}
    try { route delete 188.114.97.5 2>$null } catch {}

    # Guardar ErrorActionPreference para no afectar comandos nativos con 2>&1
    $prevPref = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    $r96 = route add 188.114.96.5 MASK 255.255.255.255 $script:gateway 2>&1; $c96 = $LASTEXITCODE
    $r97 = route add 188.114.97.5 MASK 255.255.255.255 $script:gateway 2>&1; $c97 = $LASTEXITCODE
    $ErrorActionPreference = $prevPref

    if ($c96 -ne 0) { Write-Log "WARN: route add 188.114.96.5 fallo (cod $c96): $r96" }
    if ($c97 -ne 0) { Write-Log "WARN: route add 188.114.97.5 fallo (cod $c97): $r97" }

    # -- Limpiar instancias anteriores --
    Get-Process -Name "wstunnel" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

    if ($script:wgTunnelName -and (Test-Path $script:wireguardExe)) {
        $svc = Get-Service -Name "WireGuardTunnel$($script:wgTunnelName)" -ErrorAction SilentlyContinue
        if ($svc) {
            $wgClean                 = New-Object System.Diagnostics.ProcessStartInfo
            $wgClean.FileName        = $script:wireguardExe
            $wgClean.Arguments       = "/uninstalltunnelservice `"$($script:wgTunnelName)`""
            $wgClean.CreateNoWindow  = $true
            $wgClean.UseShellExecute = $false
            $wgCleanProc = [System.Diagnostics.Process]::Start($wgClean)
            $wgCleanProc.WaitForExit(5000) | Out-Null
        }
        Get-Process -Name "wireguard" -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
    }

    # -- Verificar disponibilidad del servidor antes de conectar --
    Write-Log "INFO: Verificando disponibilidad del servidor vpn.fcrespo.tech:443..."
    if (-not (Test-ServerReachable)) {
        Write-Log "WARN: Servidor no responde en TCP 443."
        $resp = [System.Windows.Forms.MessageBox]::Show(
            "No se puede alcanzar el servidor vpn.fcrespo.tech:443.`n`n¿Deseas intentar conectar igualmente?",
            "WSTunnel - Servidor no disponible", "YesNo", "Warning")
        if ($resp -eq [System.Windows.Forms.DialogResult]::No) {
            try { $script:mutex.ReleaseMutex() } catch {}
            exit
        }
    } else {
        Write-Log "INFO: Servidor alcanzable. Iniciando tunel..."
    }

    # -- Lanzar tunel --
    $ok = Start-TunnelProcess
    if (-not $ok) { throw "wstunnel.exe se cerro inmediatamente. Revisa wstunnel.log" }

    # -- Tray icon --
    $script:notifyIcon = New-Object System.Windows.Forms.NotifyIcon
    Update-TrayStatus 'connected'
    $script:notifyIcon.Visible = $true
    $script:notifyIcon.ShowBalloonTip(
        3000, "WSTunnel",
        "Tunel conectado ($script:wgTunnelName)",
        [System.Windows.Forms.ToolTipIcon]::Info)

    # -- Menu contextual --
    $contextMenu = New-Object System.Windows.Forms.ContextMenuStrip

    $menuLogs      = New-Object System.Windows.Forms.ToolStripMenuItem
    $menuLogs.Text = "Ver Logs"
    $menuLogs.Add_Click({ Show-LogWindow })

    $menuReconnect      = New-Object System.Windows.Forms.ToolStripMenuItem
    $menuReconnect.Text = "Reconectar"
    $menuReconnect.Add_Click({ Reconnect-Now })

    $menuShortcut      = New-Object System.Windows.Forms.ToolStripMenuItem
    $menuShortcut.Text = "Generar acceso directo..."
    $menuShortcut.Add_Click({ New-AppShortcut })

    $menuSep  = New-Object System.Windows.Forms.ToolStripSeparator
    $menuSep2 = New-Object System.Windows.Forms.ToolStripSeparator

    $menuDisconnect      = New-Object System.Windows.Forms.ToolStripMenuItem
    $menuDisconnect.Text = "Desconectar"
    $menuDisconnect.Add_Click({ Disconnect-Tunnel })

    $contextMenu.Items.AddRange(@($menuLogs, $menuReconnect, $menuSep, $menuShortcut, $menuSep2, $menuDisconnect))
    $script:notifyIcon.ContextMenuStrip = $contextMenu
    $script:notifyIcon.Add_DoubleClick({ Show-LogWindow })

    # -- Watchdog: detecta caida del proceso sin bloquear la UI --
    $script:watchTimer          = New-Object System.Windows.Forms.Timer
    $script:watchTimer.Interval = 2000
    $script:watchTimer.Add_Tick({
        if ($script:isDisconnecting) { $script:watchTimer.Stop(); return }
        if ($script:tunnelProc -and $script:tunnelProc.HasExited) {
            # Cancelar el timer de estabilidad: la conexion no fue estable
            if ($script:stabilityTimer) { $script:stabilityTimer.Stop() }
            Write-Log "WARN: wstunnel.exe se ha cerrado inesperadamente."
            Invoke-Reconnect
        }
    })
    $script:watchTimer.Start()

    $appContext = New-Object System.Windows.Forms.ApplicationContext
    [System.Windows.Forms.Application]::Run($appContext)

} catch {
    $_ | Out-File (Join-Path $script:scriptDir "tunnel_error.log") -Append -Force
    [System.Windows.Forms.MessageBox]::Show(
        "Error: $($_.Exception.Message)", "WSTunnel - Error", "OK", "Error") | Out-Null
    try { $script:mutex.ReleaseMutex() } catch {}
}
