# Lista as vozes de voz do Windows pelas DUAS APIs, que nao enxergam o mesmo
# conjunto:
#   System.Speech  -> vozes SAPI5 classicas ("Microsoft Maria Desktop")
#   Windows.Media  -> vozes OneCore e as "naturais" do Narrador, que o
#                     System.Speech NAO lista
# Nesta maquina ja se sabe que as vozes "Online" nao sintetizam pelo
# System.Speech. A pergunta aberta e se elas funcionam pela segunda API.

Write-Host ''
Write-Host '=== System.Speech (SAPI5) ===' -ForegroundColor Cyan
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
foreach ($v in $s.GetInstalledVoices()) {
    $i = $v.VoiceInfo
    $marca = if ($i.Culture.Name -like 'pt*') { '  <-- portugues' } else { '' }
    Write-Host ("  {0,-42} {1,-7} {2}{3}" -f $i.Name, $i.Gender, $i.Culture.Name, $marca)
}
$s.Dispose()

Write-Host ''
Write-Host '=== Windows.Media.SpeechSynthesis (OneCore / naturais) ===' -ForegroundColor Cyan
try {
    $null = [Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media, ContentType = WindowsRuntime]
    $todas = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices
    if (-not $todas) { Write-Host '  (nenhuma)'; }
    foreach ($v in $todas) {
        $marca = if ($v.Language -like 'pt*') { '  <-- portugues' } else { '' }
        Write-Host ("  {0,-42} {1,-7} {2}{3}" -f $v.DisplayName, $v.Gender, $v.Language, $marca)
    }
} catch {
    Write-Host ("  nao consegui enumerar: " + $_.Exception.Message)
}

Write-Host ''
Write-Host 'Femininas em portugues, por API, e o que importa:' -ForegroundColor Yellow
Write-Host 'uma voz que aparece aqui ainda pode nao TOCAR — teste antes de confiar.'
