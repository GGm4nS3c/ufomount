# Secuencia de medicion de corriente de los servos del ufomount.
# El multimetro debe estar EN SERIE con el positivo (ver informe).
# Fases largas y anunciadas para leer el display en cada una.
# Requiere: GUI desconectada (el puerto debe estar libre) y montura centrada.
# Solo ASCII en este archivo (PowerShell 5.1 sin BOM).
param(
    [string]$Port = "COM5"
)

$p = New-Object System.IO.Ports.SerialPort $Port, 115200, 'None', 8, 'One'
$p.NewLine = "`n"
$p.DtrEnable = $true

function Send([string]$cmd) {
    $p.WriteLine($cmd)
    Start-Sleep -Milliseconds 250
    $null = $p.ReadExisting()
}

function Fase([string]$msg, [int]$seg) {
    Write-Host ""
    Write-Host (">>> {0}  (lee el multimetro durante {1} s)" -f $msg, $seg) -ForegroundColor Cyan
    Start-Sleep -Seconds $seg
}

try {
    $p.Open()
    Write-Host "Abriendo $Port, esperando arranque del UNO..."
    Start-Sleep -Milliseconds 2500
    $null = $p.ReadExisting()
    Send "IDLE 0"       # sin auto-detach durante la medicion
    Send "RATE 74"      # lento: ~10 grados/s pan, ~6.7 grados/s tilt

    Write-Host "=== EJE X (pan) ==="
    Send "ATTACHX"
    Fase "FASE 1: X SOSTENIENDO posicion" 8
    Send "X 1200"
    Fase "FASE 2: X MOVIENDO lento (ida)" 5
    Send "X 1800"
    Fase "FASE 2b: X MOVIENDO lento (vuelta larga)" 9
    Send "X 1500"
    Start-Sleep -Seconds 5
    Send "DETACHX"
    Fase "FASE 3: X REPOSO (desacoplado)" 5

    Write-Host "=== EJE Y (tilt, el que carga la camara) ==="
    Send "ATTACHY"
    Fase "FASE 4: Y SOSTENIENDO la camara" 8
    Send "Y 1350"
    Fase "FASE 5: Y MOVIENDO lento (bajando)" 4
    Send "Y 1650"
    Fase "FASE 5b: Y MOVIENDO lento (subiendo)" 6
    Send "Y 1500"
    Start-Sleep -Seconds 4
    Send "DETACHY"
    Fase "FASE 6: Y REPOSO (desacoplado)" 5

    Send "IDLE 3000"    # restaurar auto-detach
    Send "DETACH"
    Write-Host ""
    Write-Host "Secuencia completada, servos en reposo. Anota las lecturas de cada fase." -ForegroundColor Green
}
catch {
    Write-Host ("ERROR: {0}" -f $_.Exception.Message) -ForegroundColor Red
    Write-Host "Si dice 'acceso denegado', desconecta la GUI primero (usa el puerto)."
}
finally {
    if ($p.IsOpen) { $p.Close() }
}
