<#
  shuto installer for Windows. Per-user, no admin rights needed.

  Usage (once the repository is public):
    irm https://raw.githubusercontent.com/alamhanz/shuto/main/install/install.ps1 | iex

  Options (when running the downloaded file):
    -From <source>    wheel path, PyPI name or URL to install instead of the default
    -NoShortcut       do not create Desktop / Start menu shortcuts
    -NoStart          do not start shuto at the end
  The SHUTO_SOURCE environment variable does the same as -From.
#>
param(
    [string]$From = $env:SHUTO_SOURCE,
    [switch]$NoShortcut,
    [switch]$NoStart
)

& {
    $ErrorActionPreference = 'Stop'
    $DefaultSource = 'https://github.com/alamhanz/shuto/archive/refs/heads/main.zip'
    if (-not $From) { $From = $DefaultSource }

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
        Say 'Step 1 of 5: checking for the uv installer helper...'
        $uv = Find-Uv
        if (-not $uv) {
            Say 'Installing uv (a small tool that also installs Python for you)...'
            Invoke-RestMethod $UvInstaller | Invoke-Expression
            $uv = Find-Uv
            if (-not $uv) { throw 'uv was installed but could not be found. Open a new PowerShell window and run this line again.' }
        }
        $env:Path = "$(Split-Path $uv);$env:Path"

        Say 'Step 2 of 5: installing shuto (this can take a few minutes the first time)...'
        Native { & $uv tool install --force --python 3.12 $From }
        if ($LASTEXITCODE -ne 0) { throw 'The shuto install did not finish. Check your internet connection and run this line again.' }

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
