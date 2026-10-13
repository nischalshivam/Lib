# Manage Antigravity (agy) account snapshots so worker-agy.ps1 can rotate across them.
#
# How it works: agy keeps exactly ONE logged-in Google account, in Windows Credential
# Manager under the target "gemini:antigravity". We snapshot that credential per account,
# and "switch" writes the chosen one back. New agy runs then use that account.
#
# Usage:
#   .\agy-accounts.ps1 snapshot <email>   # save the CURRENTLY logged-in agy account as <email>
#   .\agy-accounts.ps1 switch   <email>   # make <email> the active agy account
#   .\agy-accounts.ps1 list               # list saved accounts (* = active)
#   .\agy-accounts.ps1 current            # show which account is active
#
# Adding an account: in agy type /logout, sign in the next Google account (it must be
# VERIFIED or Antigravity answers 403), wait for the sign-in to finish, then snapshot it.
# "snapshot" refuses to save if the login is identical to an already-saved account -
# that means the new sign-in has not landed yet.
param(
    [Parameter(Mandatory=$true)][ValidateSet('snapshot','switch','list','current')][string]$Action,
    [string]$Email
)
$Store   = if ($env:AGY_ACCOUNTS_DIR) { $env:AGY_ACCOUNTS_DIR } else { Join-Path $HOME '.model-router\agy-accounts' }
$Target  = 'gemini:antigravity'
$User    = 'antigravity'
$Persist = 2   # CRED_PERSIST_LOCAL_MACHINE, matches what agy writes
New-Item -ItemType Directory -Force -Path $Store | Out-Null

if (-not ([System.Management.Automation.PSTypeName]'CredRW').Type) {
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
public static class CredRW {
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  public struct CREDENTIAL { public uint Flags; public uint Type; public string TargetName; public string Comment; public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten; public uint CredentialBlobSize; public IntPtr CredentialBlob; public uint Persist; public uint AttributeCount; public IntPtr Attributes; public string TargetAlias; public string UserName; }
  [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool CredRead(string target, uint type, uint flags, out IntPtr cred);
  [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool CredWrite(ref CREDENTIAL cred, uint flags);
  [DllImport("advapi32.dll")] static extern void CredFree(IntPtr cred);
  public static byte[] Read(string target) {
    IntPtr p; if (!CredRead(target,1,0,out p)) throw new Exception("CredRead failed (is agy logged in?): "+Marshal.GetLastWin32Error());
    try { var c=(CREDENTIAL)Marshal.PtrToStructure(p,typeof(CREDENTIAL)); var b=new byte[c.CredentialBlobSize]; Marshal.Copy(c.CredentialBlob,b,0,b.Length); return b; } finally { CredFree(p); }
  }
  public static void Write(string target, string user, byte[] blob, uint persist) {
    var c=new CREDENTIAL(); c.Type=1; c.TargetName=target; c.UserName=user; c.Persist=persist; c.CredentialBlobSize=(uint)blob.Length;
    c.CredentialBlob=Marshal.AllocHGlobal(blob.Length); Marshal.Copy(blob,0,c.CredentialBlob,blob.Length);
    try { if(!CredWrite(ref c,0)) throw new Exception("CredWrite failed: "+Marshal.GetLastWin32Error()); } finally { Marshal.FreeHGlobal(c.CredentialBlob); }
  }
}
'@
}

$CurrentFile = Join-Path $Store 'current.txt'
switch ($Action) {
    'snapshot' {
        if (-not $Email) { throw "snapshot needs <email>" }
        $blob = [CredRW]::Read($Target)
        # Refuse to save if this login is byte-identical to an already-saved account:
        # that means the new sign-in has not been written yet and we'd mislabel the old one.
        foreach ($f in Get-ChildItem $Store -Filter *.cred -ErrorAction SilentlyContinue) {
            if ($f.BaseName -ne $Email -and [Linq.Enumerable]::SequenceEqual($blob, [IO.File]::ReadAllBytes($f.FullName))) {
                throw "current login is identical to saved account '$($f.BaseName)' - the sign-in for $Email has not completed yet. Finish signing in, then snapshot again."
            }
        }
        $path = Join-Path $Store "$Email.cred"
        [IO.File]::WriteAllBytes($path, $blob)
        Set-Content $CurrentFile $Email
        Write-Host "saved $Email ($($blob.Length) bytes) -> $path" -ForegroundColor Green
    }
    'switch' {
        if (-not $Email) { throw "switch needs <email>" }
        $path = Join-Path $Store "$Email.cred"
        if (-not (Test-Path $path)) { throw "no snapshot for $Email (run: agy-accounts.ps1 snapshot $Email while logged in as it)" }
        [CredRW]::Write($Target, $User, [IO.File]::ReadAllBytes($path), $Persist)
        Set-Content $CurrentFile $Email
        Write-Host "active agy account is now $Email" -ForegroundColor Green
    }
    'list' {
        $files = @(Get-ChildItem $Store -Filter *.cred -ErrorAction SilentlyContinue | Sort-Object Name)
        if ($files.Count -eq 0) { Write-Host "no snapshots yet (store: $Store)" -ForegroundColor Yellow; return }
        $cur = if (Test-Path $CurrentFile) { (Get-Content $CurrentFile).Trim() } else { '' }
        foreach ($f in $files) { $mark = if ($f.BaseName -eq $cur) { '*' } else { ' ' }; "$mark $($f.BaseName)" }
        "($($files.Count) account(s); * = active; store: $Store)"
    }
    'current' {
        if (Test-Path $CurrentFile) { (Get-Content $CurrentFile).Trim() } else { "(unknown - no snapshot taken yet)" }
    }
}
