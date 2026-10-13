# IDLE priority starved the editor's Drive reads (1 frame grab took minutes, TAHM plan 53+ min). Indexing workers run
# BELOW_NORMAL; the editor test builds get the same class (never higher). Lifts any IDLE editv2 build + its children.
# Ends when the test batch and the Reacher 02 re-render are finished.
function Kids($id) { Get-CimInstance Win32_Process -Filter "ParentProcessId=$id" | ForEach-Object { $_; Kids $_.ProcessId } }
while (-not (Select-String -Path D:\Lib\tools\test_batch_status.txt -Pattern 'END Reacher02' -Quiet)) {
  $builds = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match '-m editv2 build' }
  foreach ($b in $builds) {
    foreach ($p in @($b) + @(Kids $b.ProcessId)) {
      try { $q = Get-Process -Id $p.ProcessId -EA Stop; if ($q.PriorityClass -eq 'Idle') { $q.PriorityClass = 'BelowNormal' } } catch {}
    }
  }
  Start-Sleep 30
}
