# Prueba guiada de los dos servos del ufomount via ptztest.ino
# Uso: .\scripts\test-servos.ps1 [-Port COM5]
# Secuencia UN EJE A LA VEZ (apta para regulador justo tipo MP1584EN):
#   handshake -> ATTACHX -> movimientos suaves X -> DETACHX
#             -> ATTACHY -> movimientos suaves Y -> DETACHY
# Nota: solo caracteres ASCII en este archivo (PowerShell 5.1 sin BOM).
param(
    [string]$Port = "COM5"
)

$p = New-Object System.IO.Ports.SerialPort $Port, 115200, 'None', 8, 'One'
$p.NewLine = "`n"
$p.DtrEnable = $true   # abrir el puerto resetea el UNO

function Send-Cmd([string]$cmd, [int]$waitMs = 400) {
    $p.WriteLine($cmd)
    Start-Sleep -Milliseconds $waitMs
    $resp = $p.ReadExisting().Trim()
    Write-Host ("> {0,-12} {1}" -f $cmd, $resp)
}

try {
    $p.Open()
    Write-Host "Puerto $Port abierto, esperando arranque del UNO..."
    Start-Sleep -Milliseconds 2500
    Write-Host ("BOOT: " + $p.ReadExisting().Trim())

    Send-Cmd "PING"
    Send-Cmd "STATUS"

    Write-Host ""
    Write-Host "=== EJE X (pan, D2) - solo este servo energizado ==="
    Send-Cmd "ATTACHX" 800
    Send-Cmd "X 1600" 1500
    Send-Cmd "X 1400" 1500
    Send-Cmd "X 1500" 1500
    Send-Cmd "DETACHX" 500

    Write-Host ""
    Write-Host "=== EJE Y (tilt, D3) - solo este servo energizado ==="
    Send-Cmd "ATTACHY" 800
    Send-Cmd "Y 1600" 1500
    Send-Cmd "Y 1400" 1500
    Send-Cmd "Y 1500" 1500
    Send-Cmd "DETACHY" 500

    Send-Cmd "STATUS"
    Write-Host ""
    Write-Host "Prueba completada. Si ambos ejes se movieron suave y sin tirones, todo OK."
    Write-Host "Si un eje tironea o el UNO se reinicia: parar y revisar alimentacion."
}
finally {
    if ($p.IsOpen) { $p.Close() }
}
