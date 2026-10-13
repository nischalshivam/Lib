# Run a command OUTSIDE the Claude desktop app's MSIX container, the way the Movie Editor runs (fresh user
# environment, real AppData). WMI creates the process, so it is not a child of Claude. Prints the new PID.
#   powershell -File D:\Lib\tools\run_outside.ps1 -Cmd "py tools\envcheck.py" -Cwd D:\Lib -Log D:\Lib\tools\envcheck.log
param([Parameter(Mandatory)][string]$Cmd, [string]$Cwd = "D:\Lib", [Parameter(Mandatory)][string]$Log)
$full = "cmd /c `"$Cmd > `"$Log`" 2>&1`""
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $full; CurrentDirectory = $Cwd }
if ($r.ReturnValue -ne 0) { throw "Win32_Process.Create failed: $($r.ReturnValue)" }
$r.ProcessId
