# Grava 6 segundos do QuadCast usando DirectShow via ffmpeg e mede o volume.
# Serve para separar "problema no Python" de "problema no dispositivo":
# o ffmpeg usa outra pilha de audio que o sounddevice/PortAudio.

$ff = (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue).Source
if (-not $ff) {
    $pkg = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    $ff = (Get-ChildItem $pkg -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue |
           Select-Object -First 1).FullName
}
if (-not $ff) { Write-Host "ffmpeg nao encontrado."; exit 1 }

$dev = 'audio=Microfone (HyperX Quadcast)'
$wav = Join-Path $PSScriptRoot '..\tmp\mic_teste.wav'

Write-Host ''
Write-Host '============================================================'
Write-Host ' FALE AGORA, SEM PARAR, por 6 segundos.'
Write-Host '============================================================'
Write-Host ''
Start-Sleep -Milliseconds 700

& $ff -hide_banner -loglevel error -f dshow -i $dev -t 6 -y $wav
if ($LASTEXITCODE -ne 0) { Write-Host "ffmpeg falhou ao gravar."; exit 1 }

Write-Host ''
Write-Host '--- niveis do que foi gravado ---'
$saida = & $ff -hide_banner -i $wav -af volumedetect -f null - 2>&1
$saida | Select-String -Pattern 'mean_volume|max_volume|Duration|Stream' | ForEach-Object {
    Write-Host ("  " + $_.Line.Trim())
}

Write-Host ''
$max = ($saida | Select-String -Pattern 'max_volume: (-?[\d.]+)').Matches.Groups[1].Value
if ($max) {
    $m = [double]$max
    if ($m -lt -45) {
        Write-Host ' VEREDITO: silencio. O ffmpeg tambem nao recebeu sua voz,'
        Write-Host '           entao o problema NAO esta no codigo Python.'
    } elseif ($m -lt -25) {
        Write-Host ' VEREDITO: chegou audio, mas muito baixo. Suba o ganho do'
        Write-Host '           QuadCast (botao giratorio atras) ou o nivel no Windows.'
    } else {
        Write-Host ' VEREDITO: o microfone esta entregando audio normalmente.'
        Write-Host '           O problema esta do lado do Python/PortAudio.'
    }
}
Write-Host ''
Write-Host ("Arquivo gravado: " + $wav)
Write-Host 'Toque ele para conferir se a sua voz esta la.'
