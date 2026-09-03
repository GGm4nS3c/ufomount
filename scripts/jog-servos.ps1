# Control manual (jog) de los servos del ufomount por teclado.
# EJECUTAR EN UNA VENTANA POWERSHELL PROPIA:
#   cd c:\Projects\ufomount
#   powershell -ExecutionPolicy Bypass -File .\scripts\jog-servos.ps1
#
# Teclas:
#   1       activa/desactiva el servo X (D9)   (arranca sostenido en 1500us)
#   2       activa/desactiva el servo Y (D10)
#   A / D   mueve X  (-/+ un paso)
#   W / S   mueve Y  (+/- un paso)
#   Q / E   cambia el paso: 5 / 25 / 100 us
#   C       centra (1500us) los ejes activos
#   ESC     centra, suelta todo y sale
#
# Recomendacion con el MP1584EN: manten UN solo eje activo a la vez.
# Solo caracteres ASCII en este archivo (PowerShell 5.1 sin BOM).
param(
    [string]$Port = "COM5"
)

$p = New-Object System.IO.Ports.SerialPort $Port, 115200, 'None', 8, 'One'
$p.NewLine = "`n"
$p.DtrEnable = $true

function Send([string]$cmd) {
    $p.WriteLine($cmd)
    Start-Sleep -Milliseconds 60
    $r = $p.ReadExisting().Trim()
    if ($r) { Write-Host ("  [{0}] {1}" -f $cmd, ($r -replace "`r?`n", " | ")) -ForegroundColor DarkGray }
}

try {
    $p.Open()
    Write-Host "Abriendo $Port (el UNO se resetea)..."
    Start-Sleep -Milliseconds 2500
    $null = $p.ReadExisting()

    Send "LIMX 500 2500"
    Send "LIMY 500 2500"
    Send "RATE 900"

    $tgtX = 1500; $tgtY = 1500
    $attX = $false; $attY = $false
    $steps = @(5, 25, 100)
    $stepIdx = 1

    Write-Host ""
    Write-Host "JOG listo. 1/2=activar X/Y  A,D=X  W,S=Y  Q,E=paso  C=centrar  ESC=salir" -ForegroundColor Cyan

    while ($true) {
        $step = $steps[$stepIdx]
        Write-Host ("`rX={0}us({1})  Y={2}us({3})  paso={4}us   " -f $tgtX, $(if ($attX) {"ON"} else {"off"}), $tgtY, $(if ($attY) {"ON"} else {"off"}), $step) -NoNewline

        if (-not [Console]::KeyAvailable) { Start-Sleep -Milliseconds 40; continue }
        $k = [Console]::ReadKey($true)

        switch ($k.Key) {
            'D1' { if ($attX) { Send "DETACHX"; $attX = $false } else { Send "ATTACHX"; $attX = $true; $tgtX = 1500 } }
            'D2' { if ($attY) { Send "DETACHY"; $attY = $false } else { Send "ATTACHY"; $attY = $true; $tgtY = 1500 } }
            'A'  { if ($attX) { $tgtX = [Math]::Max(500,  $tgtX - $step); Send ("X " + $tgtX) } }
            'D'  { if ($attX) { $tgtX = [Math]::Min(2500, $tgtX + $step); Send ("X " + $tgtX) } }
            'W'  { if ($attY) { $tgtY = [Math]::Min(2500, $tgtY + $step); Send ("Y " + $tgtY) } }
            'S'  { if ($attY) { $tgtY = [Math]::Max(500,  $tgtY - $step); Send ("Y " + $tgtY) } }
            'Q'  { $stepIdx = [Math]::Max(0, $stepIdx - 1) }
            'E'  { $stepIdx = [Math]::Min($steps.Count - 1, $stepIdx + 1) }
            'C'  { if ($attX) { $tgtX = 1500; Send "X 1500" }; if ($attY) { $tgtY = 1500; Send "Y 1500" } }
            'Escape' {
                if ($attX) { Send "X 1500" }
                if ($attY) { Send "Y 1500" }
                Start-Sleep -Milliseconds 2500   # deja terminar la rampa al centro
                Send "DETACH"
                Write-Host "`nCentrado y desacoplado. Saliendo."
                return
            }
        }
    }
}
finally {
    if ($p.IsOpen) { $p.Close() }
}
