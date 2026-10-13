# Overnight editor test: one video per show, sequential, IDLE priority (indexing workers run BELOW_NORMAL and always win).
# Runs outside Claude (start it with run_outside.ps1). Waits for any running editv2 build first; skips a show whose
# video is already in its FINAL Videos folder.
$py = "C:\Users\Dell\AppData\Local\Programs\Python\Python314\python.exe"
$voice = "D:\Imp Files\Audio Samples FINAL\Sheldon Cooper Audio.mp3"
$jobs = @(
  @{ show = "TAHM";  dir = "D:\Movies\English\TAHM\01 Alan Harper Didn't Start Out Like This" },
  @{ show = "B99";   dir = "D:\Movies\English\B99\01 Brooklyn Nine-Nine Never Finished This Story" },
  @{ show = "House"; dir = "D:\Movies\English\House\01 Who Was Actually House's Smartest Fellow" },
  @{ show = "TWD";   dir = "D:\Movies\English\TWD\01 How Long Could a Walker Actually Keep Walking" }
)
$status = "D:\Lib\tools\test_batch_status.txt"
"$(Get-Date -Format 'HH:mm') runner (re)started" | Out-File $status -Append -Encoding utf8
while (Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match '-m editv2 build' }) { Start-Sleep 30 }
foreach ($j in $jobs) {
  $save = Join-Path (Split-Path $j.dir -Parent) "FINAL Videos"
  $topic = Split-Path $j.dir -Leaf
  $script = Get-ChildItem $j.dir -File -Filter *.txt | Where-Object { $_.Name -notlike '*_QA.txt' } | Select-Object -First 1
  $name = if ($script.BaseName -in @('Script', 'script', 'clean', 'narration')) { $topic } else { $script.BaseName }
  if (Test-Path (Join-Path $save "$name.mp4")) { "$(Get-Date -Format 'HH:mm') SKIP $($j.show) (already in FINAL Videos)" | Out-File $status -Append -Encoding utf8; continue }
  $log = "D:\Lib\tools\test_$($j.show).log"
  "$(Get-Date -Format 'HH:mm') START $($j.show)" | Out-File $status -Append -Encoding utf8
  # the redirect must be INSIDE the started cmd, else it only captures 'start' itself (empty log, 23:39)
  $inner = "`"$py`" -u -m editv2 build `"$($script.FullName)`" --lang en --look auto --voice-ref `"$voice`" --save `"$save`" > `"$log`" 2>&1"
  $a = "/c start `"editv2 $($j.show)`" /belownormal /wait /min cmd /c `"$inner`""
  Start-Process -FilePath cmd.exe -ArgumentList $a -WorkingDirectory D:\Lib -WindowStyle Hidden -Wait
  $done = Select-String -Path $log -Pattern '^DONE ' | Select-Object -Last 1
  "$(Get-Date -Format 'HH:mm') END $($j.show): $(if ($done) { $done.Line } else { 'FAILED - see ' + $log })" | Out-File $status -Append -Encoding utf8
}
"$(Get-Date -Format 'HH:mm') ALL DONE" | Out-File $status -Append -Encoding utf8
