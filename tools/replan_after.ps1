# After the TAHM re-look: rebuild B99 01 from PLAN on (new punch-in rules, no name cards/annotations, unique depth
# words) - BELOW_NORMAL. Keeps clue/voice/words.
$py = "C:\Users\Dell\AppData\Local\Programs\Python\Python314\python.exe"
$status = "D:\Lib\tools\test_batch_status.txt"
while (-not (Select-String -Path $status -Pattern 'END relook TAHM01' -Quiet)) { Start-Sleep 60 }
$script = "D:\Movies\English\B99\01 Brooklyn Nine-Nine Never Finished This Story\Script.txt"
$b = Join-Path (Split-Path $script -Parent) "Script_v2build"
foreach ($f in "plan.json", "state.json", "look.json", "silent.mp4") { Remove-Item -Force (Join-Path $b $f) -ErrorAction SilentlyContinue }
foreach ($d in "comp", "assets") { Remove-Item -Recurse -Force (Join-Path $b $d) -ErrorAction SilentlyContinue }
$log = "D:\Lib\tools\replan_B99.log"
"$(Get-Date -Format 'HH:mm') START replan B99" | Out-File $status -Append -Encoding utf8
$inner = "`"$py`" -u -m editv2 build `"$script`" --lang en --look auto --save `"D:\Movies\English\B99\FINAL Videos`" > `"$log`" 2>&1"
Start-Process -FilePath cmd.exe -ArgumentList "/c start `"replan B99`" /belownormal /wait /min cmd /c `"$inner`"" -WorkingDirectory D:\Lib -WindowStyle Hidden -Wait
$done = Select-String -Path $log -Pattern '^DONE ' | Select-Object -Last 1
"$(Get-Date -Format 'HH:mm') END replan B99: $(if ($done) { $done.Line } else { 'FAILED - see ' + $log })" | Out-File $status -Append -Encoding utf8
