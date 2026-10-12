<#
  shuto uninstaller for Windows. Per-user, no admin rights needed.

  Usage (once the repository is public):
    irm https://raw.githubusercontent.com/alamhanz/shuto/main/install/uninstall.ps1 | iex

  Options (when running the downloaded file):
    -Yes          do not ask for confirmation
    -DeleteData   also delete shuto's own data (database, thumbnails, settings)
    -KeepLogin    keep the saved Shutterstock login
  Your photos, Ollama and its models are never touched. uv itself is not removed.
#>
param(
    [switch]$Yes,
    [switch]$DeleteData,
    [switch]$KeepLogin
)

& {
    $ErrorActionPreference = 'Stop'

    # Native programs write to stderr; under 'Stop' Windows PowerShell 5.1 turns that into an error.
    # Run them under 'Continue' and check $LASTEXITCODE instead.
    function Native([scriptblock]$Block) {
        $saved = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { & $Block } finally { $ErrorActionPreference = $saved }
    }

    function Say($text) { Write-Host "[shuto] $text" }

    function Find-Uv {
        $cmd = Get-Command uv -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
        foreach ($dir in @("$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin")) {
            $candidate = Join-Path $dir 'uv.exe'
            if (Test-Path $candidate) { return $candidate }
        }
        return $null
    }

    try {
        Say 'Step 1 of 2: removing shuto icons, saved login and (if you choose) data...'
        $uv = Find-Uv
        $shuto = $null
        $cmd = Get-Command shuto -ErrorAction SilentlyContinue
        if ($cmd) { $shuto = $cmd.Source }
        if (-not $shuto -and $uv) {
            $candidate = Join-Path (((Native { & $uv tool dir --bin }) | Out-String).Trim()) 'shuto.exe'
            if (Test-Path $candidate) { $shuto = $candidate }
        }
        if ($shuto) {
            $opts = @()
            if ($Yes) { $opts += '--yes' }
            if ($DeleteData) { $opts += '--delete-data' }
            if ($KeepLogin) { $opts += '--keep-login' }
            Native { & $shuto uninstall @opts }
            if ($LASTEXITCODE -eq 1) {
                # 1 = cancelled, or shuto is still open; either way do not remove the program.
                Say 'Stopped. shuto was not removed (it may still be open: close it and run this again).'
                if ($MyInvocation.ScriptName) { exit 2 }
                return
            }
            if ($LASTEXITCODE -ne 0) { Say 'shuto could not finish its cleanup; continuing.' }
        } else {
            Say 'The shuto program was not found. Removing the icons only.'
            $desktop = [Environment]::GetFolderPath('DesktopDirectory')
            $menu = [Environment]::GetFolderPath('Programs')
            foreach ($lnk in @((Join-Path $desktop 'shuto.lnk'), (Join-Path $menu 'shuto.lnk'))) {
                if (Test-Path $lnk) { Remove-Item $lnk -Force; Say "Removed $lnk" }
            }
        }

        Say 'Step 2 of 2: removing the shuto program...'
        if ($uv) {
            Native { & $uv tool uninstall shuto }
            if ($LASTEXITCODE -ne 0) { Say 'shuto was not installed with uv, or is already removed.' }
        } else {
            Say 'uv was not found, so there is nothing more to remove.'
        }

        Say 'Done. Your photos, Ollama and its models were not touched.'
        Say 'To remove a model: ollama rm <model>. To remove uv: uv cache clean, then delete uv.exe from ~\.local\bin.'
    } catch {
        Write-Host "[shuto] Something went wrong: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host '[shuto] See docs/install.md (Uninstall) or run the line again.' -ForegroundColor Red
        if ($MyInvocation.ScriptName) { exit 1 }
    }
}
