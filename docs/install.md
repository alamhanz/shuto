# Installing shuto

shuto runs on your own computer. Your photos stay on it. These steps need no admin rights and no
programming knowledge. The install lines below work once the shuto repository is public.

## Windows

1. Open PowerShell: press the Windows key, type `PowerShell`, press Enter.
   <!-- screenshot: opening PowerShell from the Start menu (images/install-windows-1.png) -->
2. Paste this line and press Enter:
   ```
   irm https://raw.githubusercontent.com/alamhanz/shuto/main/install/install.ps1 | iex
   ```
   <!-- screenshot: the install line in PowerShell (images/install-windows-2.png) -->
3. Wait. The first time takes a few minutes. It prints "Step 1 of 5" to "Step 5 of 5", then
   opens shuto in your browser.
4. Next time, double-click the **shuto** icon on your Desktop (or find it in the Start menu).
   If shuto is already open, the icon just brings up the same page.

## macOS

1. Open Terminal: press Command + Space, type `Terminal`, press Return.
   <!-- screenshot: opening Terminal with Spotlight (images/install-mac-1.png) -->
2. Paste this line and press Return:
   ```
   curl -LsSf https://raw.githubusercontent.com/alamhanz/shuto/main/install/install.sh | sh
   ```
3. Wait for your browser to open shuto.
4. Next time, double-click **shuto.command** on your Desktop.

## Linux

1. Open a terminal.
2. Paste the same `curl` line as for macOS and press Enter.
3. Next time, open **shuto** from your applications menu or Desktop.

## The AI helper (Ollama)

shuto writes titles and keywords with a free AI helper called Ollama that runs on your computer.
The installer checks for it and opens its download page if it is missing. It never installs
Ollama or downloads an AI model for you; shuto asks you first, inside the app.

- Windows and macOS: download the installer from <https://ollama.com/download> and run it.
- Linux: the Ollama download page gives one command that needs administrator rights. Ask
  someone to help, or choose an online AI in shuto's Settings instead.

## Update

Run the same install line again. It replaces shuto with the latest version and keeps your
photos, batches and settings.

## Uninstall

Close shuto first (close its browser tab and the shuto window), then paste one line. It asks
before it removes anything.

**Windows** (PowerShell):

```
irm https://raw.githubusercontent.com/alamhanz/shuto/main/install/uninstall.ps1 | iex
```

**macOS and Linux** (Terminal):

```
curl -LsSf https://raw.githubusercontent.com/alamhanz/shuto/main/install/uninstall.sh | sh
```

Already have shuto on your computer? You can also type `shuto uninstall`, then run the last
step it prints: `uv tool uninstall shuto`.

What is removed:

- the shuto icons on your Desktop and in your Start / app menu
- your saved Shutterstock login (stored in this computer's keyring)
- the shuto program itself

What is kept unless you say yes when asked (or add `-DeleteData` on Windows, `--delete-data`
on macOS and Linux to the downloaded script, or `shuto uninstall --delete-data`):

- shuto's own data: its database, thumbnails, prepared copies, exported CSV files inside its
  workspace folder, settings and your saved model choice

Never touched, in any case: your photos and source folders, and Ollama with its downloaded
models. To free the model space, run `ollama rm <model>` for each model (`ollama list` shows
them). To remove Ollama itself, use its own uninstaller (Settings, Apps on Windows; drag
Ollama to the Bin on macOS; see ollama.com for Linux).

To keep your saved login, use `-KeepLogin` (Windows) or `--keep-login` (macOS and Linux) with
the downloaded script, or `shuto uninstall --keep-login`.

The `uv` helper is left in place because other programs may use it. To remove it too, see
<https://docs.astral.sh/uv/getting-started/installation/#uninstallation>.

## Try it before the repo is public

The repository is private for now, so the one-line installs above do not work yet. Install from
a copy of the repository on your computer instead. No other setup is needed; the installer gets
what it needs (including iPhone HEIC photo support).

1. Get the files: on GitHub choose Code, then Download ZIP, and unzip it. Or clone the repository.
2. Open the `install` folder and start the installer:
   - **Windows**: double-click `install.cmd`.
   - **macOS**: double-click `install.command`. If macOS refuses, right-click it, choose Open,
     then Open again.
   - **Linux**: open a terminal in the folder and run `sh install/install.sh`.
3. Wait for the steps to finish. On Windows and macOS the window waits for a key press at the end
   so you can read the result. On Linux the terminal you started it from stays as it is.

Advanced: to install a wheel you built yourself (`uv build`), pass it explicitly. A bare wheel
path skips the `heic` extra, so add it like this:
`install\install.ps1 -From "shuto[heic] @ file:///full/path/to/dist/shuto-0.2.0-py3-none-any.whl"`
(`--from` on macOS and Linux takes the same text).

## Troubleshooting

- **Windows says "Windows protected your PC" (SmartScreen)**: this appears for new software.
  Choose "More info", then "Run anyway", only if you started the install yourself.
- **macOS says the file cannot be opened because it is from an unidentified developer
  (Gatekeeper)**: right-click `shuto.command`, choose Open, then Open again.
- **"command not found" or "shuto is not recognized"**: close the window and open a new
  PowerShell or Terminal. The install updates your PATH, and only new windows see it.
- **PowerShell blocks the script**: use this form instead:
  `powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/alamhanz/shuto/main/install/install.ps1 | iex"`
- **The page does not open**: go to <http://127.0.0.1:8765> in your browser.
- **"Access is denied" or "file is in use" when reinstalling (Windows)**: shuto is still open.
  Close the shuto window (or press Ctrl+C in it) and run the line again. The installer also
  offers to close it for you. If it still fails, wait a few seconds (antivirus may be scanning
  the files) and try again.
- **The install stops with a network error**: check your internet connection and run the line
  again. It is safe to repeat.

## Advanced

- Install from another source (a downloaded wheel file, PyPI or a different URL): set
  `SHUTO_SOURCE` first, or pass `-From <source>` to `install.ps1` / `--from <source>` to
  `install.sh`.
- Skip the shortcut or the automatic start: `-NoShortcut` / `-NoStart` (PowerShell),
  `--no-shortcut` / `--no-start` (shell).
- Create or remove the icons yourself: `shuto shortcut` and `shuto shortcut --remove`.
