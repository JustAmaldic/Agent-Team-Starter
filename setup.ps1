[CmdletBinding()]
param(
    [switch]$SkipPolicyChange
)
$ErrorActionPreference = 'Stop'

function Test-Command($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

if (-not (Test-Command 'git')) {
    throw 'Git for Windows is required. Install Git, reopen PowerShell, and retry.'
}
if (-not (Test-Command 'py') -and -not (Test-Command 'python')) {
    throw 'Python 3.10+ is required. Install Python, reopen PowerShell, and retry.'
}

$atsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Test-Path (Join-Path $atsDir 'ats.py'))) {
    throw 'ATS setup must be run from a cloned .ats directory.'
}

$profilePath = $PROFILE.CurrentUserCurrentHost
$profileDir = Split-Path -Parent $profilePath
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
if (-not (Test-Path $profilePath)) {
    New-Item -ItemType File -Path $profilePath | Out-Null
}

$marker = '# ATS PowerShell command'
$existing = Get-Content -LiteralPath $profilePath -Raw -ErrorAction SilentlyContinue
if ($null -eq $existing) { $existing = '' }
$upgradeMarker = '# ATS PowerShell command v2'
if (-not $existing.Contains($upgradeMarker)) {
    $definition = @'
# ATS PowerShell command v2
function global:.ats {
    $repoRoot = & git rev-parse --show-toplevel 2>$null
    $appIsGit = ($LASTEXITCODE -eq 0 -and $repoRoot)
    $script = $null
    if ($LASTEXITCODE -eq 0 -and $repoRoot) {
        $script = Join-Path $repoRoot '.ats/ats.py'
    }
    if (-not $script -or -not (Test-Path $script)) {
        $script = Join-Path (Get-Location).Path '.ats/ats.py'
    }
    if (-not (Test-Path $script)) {
        Write-Error "ATS: no .ats/ats.py found. Open the application directory containing .ats."
        return
    }
    if (($args.Count -gt 0) -and ($args[0] -eq 'task') -and (-not $appIsGit)) {
        Write-Error "ATS task requires the application folder itself to be a Git repository. Run git init or clone the application repository first."
        return
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 $script @args
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python $script @args
    } else {
        Write-Error "ATS: Python 3 is required."
    }
}
'@
    Add-Content -LiteralPath $profilePath -Value ([Environment]::NewLine + $definition)
    Write-Host 'ATS v2 command added to the PowerShell profile.'
} else {
    Write-Host 'ATS command already present in the PowerShell profile.'
}

$policyList = Get-ExecutionPolicy -List
$managed = $policyList | Where-Object {
    ($_.Scope -eq 'MachinePolicy' -or $_.Scope -eq 'UserPolicy') -and
    $_.ExecutionPolicy -ne 'Undefined'
}
if ($managed) {
    Write-Warning 'Execution policy is managed by your organization. ATS will not override it.'
} elseif (-not $SkipPolicyChange -and (Get-ExecutionPolicy) -eq 'Restricted') {
    Write-Host 'Current execution policy prevents loading your PowerShell profile.'
    $choice = Read-Host 'Set CurrentUser execution policy to RemoteSigned? (y/N)'
    if ($choice -match '^(y|yes|j|ja)$') {
        Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force
    }
}

if ((Get-ExecutionPolicy) -eq 'RemoteSigned') {
    $zone = $null
    try { $zone = Get-Item -LiteralPath $profilePath -Stream Zone.Identifier -ErrorAction Stop } catch { $zone = $null }
    if ($zone) {
        Write-Warning 'Your PowerShell profile has a download-zone marker and may be blocked.'
        $choice = Read-Host 'Unblock only this PowerShell profile? (y/N)'
        if ($choice -match '^(y|yes|j|ja)$') {
            Unblock-File -LiteralPath $profilePath
        }
    }
}

Write-Host ''
Write-Host 'Setup finished. Open a NEW PowerShell window in your application repository.'
Write-Host 'Then run: .ats doctor'
Write-Host 'Then run: .ats install'
Write-Host 'Without a profile shortcut you can always run: py -3 .ats/ats.py doctor'
