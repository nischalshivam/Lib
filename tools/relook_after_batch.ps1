# After the test batch: rebuild LOOK + render + mix + QA (name-card fixes 2026-10-13) for the listed builds, BELOW_NORMAL.
$py = "C:\Users\Dell\AppData\Local\Programs\Python\Python314\python.exe"
$jobs = @(
  @{ name = "TAHM01"; script = "D:\Movies\English\TAHM\01 Alan Harper Didn't Start Out Like This\Script.txt"; save = "D:\Movies\English\TAHM\FINAL Videos" }
)
$status = "D:\Lib\tools\test_batch_status.txt"
while (-not ((Get-Content $status -Raw) -match '(?s)morning re-run.*ALL DONE')) { Start-Sleep 60 }
foreach ($j in $jobs) {
  $b = Join-Path (Split-Path $j.script -Parent) "Script_v2build"
  Remove-Item -Recurse -Force (Join-Path $b "comp"), (Join-Path $b "look.json"), (Join-Path $b "silent.mp4") -ErrorAction SilentlyContinue
  $log = "D:\Lib\tools\relook_$($j.name).log"
  "$(Get-Date -Format 'HH:mm') START relook $($j.name)" | Out-File $status -Append -Encoding utf8
  $inner = "`"$py`" -u -m editv2 build `"$($j.script)`" --lang en --look auto --save `"$($j.save)`" > `"$log`" 2>&1"
  Start-Process -FilePath cmd.exe -ArgumentList "/c start `"relook $($j.name)`" /belownormal /wait /min cmd /c `"$inner`"" -WorkingDirectory D:\Lib -WindowStyle Hidden -Wait
  $done = Select-String -Path $log -Pattern '^DONE ' | Select-Object -Last 1
  "$(Get-Date -Format 'HH:mm') END relook $($j.name): $(if ($done) { $done.Line } else { 'FAILED - see ' + $log })" | Out-File $status -Append -Encoding utf8
}
