# enroll.ps1 - dead-simple account adder.
#
# You log each Google account into agy, come back here, press Enter, and it
# saves that account automatically (auto-named acct-01, acct-02, ...). Repeat
# for as many accounts as you want. Run it again any day to add more.
#
# IMPORTANT: stop the rotating endpoint (close the start.bat window) before
# enrolling, so nothing changes the active account under you.

$ErrorActionPreference = 'Stop'
$here  = Split-Path -Parent $MyInvocation.MyCommand.Path
$mgr   = Join-Path $here 'agy-accounts.ps1'
$Store = if ($env:AGY_ACCOUNTS_DIR) { $env:AGY_ACCOUNTS_DIR } else { Join-Path $HOME '.model-router\agy-accounts' }
New-Item -ItemType Directory -Force -Path $Store | Out-Null

function Next-Label {
    $nums = Get-ChildItem $Store -Filter 'acct-*.cred' -ErrorAction SilentlyContinue |
            ForEach-Object { if ($_.BaseName -match 'acct-(\d+)') { [int]$Matches[1] } }
    $max = 0; if ($nums) { $max = ($nums | Measure-Object -Maximum).Maximum }
    return ('acct-{0:D2}' -f ($max + 1))
}

Write-Host ""
Write-Host "==================== ADD GOOGLE ACCOUNTS ====================" -ForegroundColor Cyan
Write-Host "For EACH account you want to add:"
Write-Host "  1. Open a NEW terminal and run:  agy" -ForegroundColor Yellow
Write-Host "  2. In it type:  /logout   then sign in the NEXT Google account"
Write-Host "     in the browser. Wait until it finishes signing in."
Write-Host "  3. Come back HERE and press Enter - I will save it."
Write-Host "Repeat for each account. Type  q  then Enter when done."
Write-Host "The first, already-logged-in account is already saved."
Write-Host "=============================================================" -ForegroundColor Cyan

$saved = 0
while ($true) {
    $label = Next-Label
    Write-Host ""
    $ans = Read-Host "Signed in the next account in agy? Press Enter to save as $label  [q = finish]"
    if ($ans -match '^\s*q') { break }
    try {
        & $mgr snapshot $label *>&1 | Out-Null
        $saved++
        Write-Host "  [OK] saved $label" -ForegroundColor Green
    } catch {
        $msg = "$($_.Exception.Message)"
        if ($msg -match 'identical to saved account') {
            Write-Host "  [skip] Same as an account already saved, or sign-in not finished yet." -ForegroundColor Yellow
            Write-Host "         In agy do /logout, sign in a NEW account, finish it, press Enter." -ForegroundColor Yellow
        } elseif ($msg -match 'CredRead failed') {
            Write-Host "  [wait] agy is not logged in yet. Sign in an account in agy, then Enter." -ForegroundColor Yellow
        } else {
            Write-Host "  [err] $msg" -ForegroundColor Red
        }
    }
}

Write-Host ""
Write-Host "Done. Accounts in the pool:" -ForegroundColor Cyan
& $mgr list
Write-Host ""
Write-Host "Now start or restart the endpoint:  start.bat" -ForegroundColor Green
