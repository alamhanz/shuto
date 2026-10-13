<#
  shuto installer for Windows. Per-user, no admin rights needed.

  Usage:
    irm https://raw.githubusercontent.com/alamhanz/shuto/main/install/install.ps1 | iex

  Options (when running the downloaded file):
    -From <source>    wheel path, PyPI name or URL to install instead of the latest release
                      (when run from a copy of the repository, that copy is installed)
    -NoShortcut       do not create Desktop / Start menu shortcuts
    -NoStart          do not start shuto at the end
  The SHUTO_SOURCE environment variable does the same as -From.
  With none of these, the latest release on GitHub is installed.
#>
param(
    [string]$From = $env:SHUTO_SOURCE,
    [switch]$NoShortcut,
    [switch]$NoStart
)

& {
    $ErrorActionPreference = 'Stop'
    # The "latest release" page redirects to the newest tag (no API, so no rate limit).
    $LatestReleasePage = 'https://github.com/alamhanz/shuto/releases/latest'
    $ReleaseDownload = 'https://github.com/alamhanz/shuto/releases/download'
    # Windows PowerShell 5.1 does not use TLS 1.2 by default, which GitHub and astral.sh require.
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }

    # Run from a downloaded or cloned copy of the repository (and no source given)? Then build and
    # install that copy. $PSScriptRoot is empty when the script is piped in with "irm | iex".
    $Checkout = $null
    if (-not $From -and $PSScriptRoot) {
        $root = Split-Path -Parent $PSScriptRoot
        $toml = Join-Path $root 'pyproject.toml'
        if ((Test-Path $toml) -and (Select-String -Path $toml -Pattern '^name\s*=\s*"shuto"' -Quiet)) {
            $Checkout = $root
        }
    }
    $UseLatest = (-not $From) -and (-not $Checkout)

    # uv installer pinned to a known release (0.12.22, current when this was written) so a
    # changed "latest" installer cannot alter what runs here. Raise it on purpose after checking.
    $UvInstaller = 'https://astral.sh/uv/0.12.22/install.ps1'

    # Native programs write progress to stderr; under 'Stop' Windows PowerShell 5.1 turns that into
    # an error. Run them under 'Continue' and check $LASTEXITCODE instead.
    function Native([scriptblock]$Block) {
        $saved = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { & $Block } finally { $ErrorActionPreference = $saved }
    }

    function Say($text) { Write-Host "[shuto] $text" }

    # Find the newest release and return its wheel as @{ Version; Spec }, or $null. The version
    # comes from where the "latest" page redirects to; the wheel's existence is checked too.
    function Get-LatestRelease {
        try {
            $headers = @{ 'User-Agent' = 'shuto-installer' }
            $page = Invoke-WebRequest -Uri $LatestReleasePage -Method Head -Headers $headers -UseBasicParsing
            $final = $null
            if ($page.BaseResponse.ResponseUri) { $final = $page.BaseResponse.ResponseUri.AbsoluteUri }
            elseif ($page.BaseResponse.RequestMessage) { $final = $page.BaseResponse.RequestMessage.RequestUri.AbsoluteUri }
            if (-not $final -or $final -notmatch '/releases/tag/v(?<ver>\d+(\.\d+)*([.-]?(a|b|rc|alpha|beta|dev|post)\.?\d+)?)$') { return $null }
            $ver = $Matches['ver']
            $url = "$ReleaseDownload/v$ver/shuto-$ver-py3-none-any.whl"
            $null = Invoke-WebRequest -Uri $url -Method Head -Headers $headers -UseBasicParsing
            return @{ Version = $ver; Spec = 'shuto[heic] @ ' + $url }
        } catch { }
        return $null
    }

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
        if ($UseLatest) {
            Say 'Looking for the latest shuto release...'
            $latest = Get-LatestRelease
            if (-not $latest) { throw 'Could not find the latest shuto release on GitHub. Check your internet connection and run this line again (on a shared network, try again later), or download it from https://github.com/alamhanz/shuto/releases' }
            $From = $latest.Spec
            Say "Installing shuto $($latest.Version)..."
        }

        Say 'Step 1 of 5: checking for the uv installer helper...'
        $uv = Find-Uv
        if (-not $uv) {
            Say 'Installing uv (a small tool that also installs Python for you)...'
            Invoke-RestMethod $UvInstaller | Invoke-Expression
            $uv = Find-Uv
            if (-not $uv) { throw 'uv was installed but could not be found. Open a new PowerShell window and run this line again.' }
        }
        $env:Path = "$(Split-Path $uv);$env:Path"

        $buildDir = $null
        $installCode = 1
        try {
            if ($Checkout) {
                Say 'Preparing shuto from this folder...'
                $buildDir = Join-Path ([System.IO.Path]::GetTempPath()) ('shuto-build-' + [guid]::NewGuid().ToString('N'))
                Native { & $uv build --wheel --python 3.12 --out-dir $buildDir $Checkout }
                $wheel = Get-ChildItem -Path $buildDir -Filter 'shuto-*.whl' -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($LASTEXITCODE -ne 0 -or -not $wheel) { throw 'Could not prepare shuto from this folder. Check your internet connection and run this again.' }
                $From = 'shuto[heic] @ ' + ([Uri]$wheel.FullName).AbsoluteUri
            }

            Say 'Step 2 of 5: installing shuto (this can take a few minutes the first time)...'
            Stop-RunningShuto $uv
            $installOutput = Native { & $uv tool install --force --python 3.12 $From 2>&1 | ForEach-Object { Write-Host "$_"; "$_" } }
            $installCode = $LASTEXITCODE
        } finally {
            if ($buildDir -and (Test-Path $buildDir)) { Remove-Item -Recurse -Force $buildDir -ErrorAction SilentlyContinue }
        }
        if ($installCode -ne 0) {
            if (($installOutput | Out-String) -match 'Access is denied|os error 5|os error 32|being used by another process') {
                throw 'A shuto file is in use - usually shuto is still open, or antivirus is scanning it. Close shuto, wait a few seconds and run this line again.'
            }
            throw 'The shuto install did not finish. This can be a dropped internet connection or a blocked download. Check your connection and run this line again.'
        }

        Say 'Step 3 of 5: making the shuto command available...'
        Native { & $uv tool update-shell *> $null }
        if ($LASTEXITCODE -ne 0) { Say 'Could not update PATH automatically; open a new window if shuto is not found.' }
        $binDir = ((Native { & $uv tool dir --bin }) | Out-String).Trim()
        $env:Path = "$binDir;$env:Path"
        $shuto = Join-Path $binDir 'shuto.exe'
        if (-not (Test-Path $shuto)) { throw "shuto was installed but $shuto was not found." }

        if ($NoShortcut) {
            Say 'Step 4 of 5: skipping the shortcut.'
        } else {
            Say 'Step 4 of 5: creating the shuto icon on your Desktop and Start menu...'
            Native { & $shuto shortcut }
            if ($LASTEXITCODE -ne 0) { Say 'Could not create the shortcut. You can still start shuto by typing: shuto' }
        }

        Say 'Step 5 of 5: checking for Ollama (the free AI helper that runs on your computer)...'
        $ollama = (Get-Command ollama -ErrorAction SilentlyContinue) -or
            (Test-Path "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe")
        if ($ollama) {
            Say 'Ollama is installed.'
        } else {
            Say 'Ollama is not installed yet. shuto needs it to write titles and keywords.'
            Say 'Opening the download page: https://ollama.com/download (install it, then come back).'
            if (-not $NoStart) { Start-Process 'https://ollama.com/download' }
        }

        Say 'Done. Next time, double-click the shuto icon on your Desktop, or type: shuto'
        if (-not $NoStart) {
            Say 'Starting shuto in your browser...'
            Native { & $shuto }
        }
    } catch {
        Write-Host "[shuto] Something went wrong: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host '[shuto] Nothing else was changed. See docs/install.md (Troubleshooting) or run the line again.' -ForegroundColor Red
        if ($MyInvocation.ScriptName) { exit 1 }
    }
}
