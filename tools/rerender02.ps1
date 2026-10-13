# After the overnight test batch: re-render Reacher 02 (6 sound-up flash tails fixed in its clips), IDLE priority.
while (-not (Select-String -Path D:\Lib\tools\test_batch_status.txt -Pattern 'ALL DONE' -Quiet)) { Start-Sleep 60 }
$d = "D:\Movies\English\Reacher\02 Why Reacher Season 4 Feels So Different"
$py = "C:\Users\Dell\AppData\Local\Programs\Python\Python314\python.exe"
"$(Get-Date -Format 'HH:mm') START Reacher02 re-render" | Out-File D:\Lib\tools\test_batch_status.txt -Append -Encoding utf8
$a = "/c start `"editv2 R02`" /belownormal /wait /min `"$py`" -u -m editv2 build `"$d\Script.txt`" --lang en --look auto --save `"D:\Movies\English\Reacher\FINAL Videos`" > D:\Lib\tools\reacher02_rerender.log 2>&1"
Start-Process -FilePath cmd.exe -ArgumentList $a -WorkingDirectory D:\Lib -WindowStyle Hidden -Wait
$done = Select-String -Path D:\Lib\tools\reacher02_rerender.log -Pattern '^DONE ' | Select-Object -Last 1
"$(Get-Date -Format 'HH:mm') END Reacher02 re-render: $(if ($done) { $done.Line } else { 'FAILED - see reacher02_rerender.log' })" | Out-File D:\Lib\tools\test_batch_status.txt -Append -Encoding utf8
