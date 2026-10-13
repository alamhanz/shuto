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

    # A running shuto (its window, python.exe or uvicorn.exe from the uv tool folder) locks files
    # that uv must replace or delete. Find those processes and offer to close them.
    function Stop-RunningShuto([string]$Uv) {
        $toolDir = ((Native { & $Uv tool dir 2>$null }) | Out-String).Trim()
        if (-not $toolDir) { return }
        $prefix = (Join-Path $toolDir 'shuto') + '\'
        # The tool's python.exe / shuto.exe are launchers; the real interpreter runs as a child
        # from elsewhere, so also collect every descendant of a matched process.
        function Get-ShutoProcesses {
            $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
            $byId = @{}
            foreach ($p in $all) { $byId[[int]$p.ProcessId] = $p }
            # Never touch this script's own process or its ancestors (PowerShell, cmd, terminal).
            $protected = @{}
            $cur = [int]$PID
            while ($cur -and $byId.ContainsKey($cur) -and -not $protected.ContainsKey($cur)) {
                $protected[$cur] = $true
                $cur = [int]$byId[$cur].ParentProcessId
            }
            $ids = @{}
            foreach ($p in $all) {
                if ($p.ExecutablePath -and $p.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -and -not $protected.ContainsKey([int]$p.ProcessId)) { $ids[[int]$p.ProcessId] = $true }
            }
            do {
                $added = $false
                foreach ($p in $all) {
                    $pid2 = [int]$p.ProcessId
                    if ($ids.ContainsKey($pid2) -or $protected.ContainsKey($pid2)) { continue }
                    if (-not $ids.ContainsKey([int]$p.ParentProcessId)) { continue }
                    # Ollama is started by shuto but is not part of it: leave it and its runners alone.
                    if ($p.Name -like 'ollama*') { continue }
                    # A reused parent id must not pull in an older, unrelated process.
                    $parent = $byId[[int]$p.ParentProcessId]
                    if ($p.CreationDate -and $parent.CreationDate -and $p.CreationDate -lt $parent.CreationDate) { continue }
                    $ids[$pid2] = $true
                    $added = $true
                }
            } while ($added)
            $ids.Keys
        }
        $found = @(Get-ShutoProcesses)
        if ($found.Count -eq 0) { return }
        Say 'shuto is still open on this computer, and it has to be closed first.'
        $answer = 'n'
        try { $answer = Read-Host '[shuto] Close it now? [Y/n]' } catch { $answer = 'n' }
        if ($answer -match '^\s*n') {
            throw 'shuto is still open. Close the shuto window (or press Ctrl+C in it) and run this line again.'
        }
        for ($i = 0; $i -lt 10; $i++) {
            foreach ($id in $found) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Seconds 1
            $found = @(Get-ShutoProcesses)
            if ($found.Count -eq 0) { return }
        }
        throw 'shuto is still open. Close the shuto window (or press Ctrl+C in it) and run this line again.'
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
            Stop-RunningShuto $uv
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
