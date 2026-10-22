# Choosing a local model

shuto describes photos with a vision model served by [Ollama](https://ollama.com). The right
model depends on your hardware and how much quality you need.

## How shuto helps you choose

- `shuto doctor` shows your OS, CPU, RAM, GPU and VRAM (NVIDIA, Apple Silicon unified memory,
  AMD where detectable), whether Ollama is running, and which models are already pulled.
- `shuto models` (or `shuto init`, or the Settings page in the web UI) shows the **top 8**
  models that fit your machine, ranked by a mix of quality and speed. Each row shows the size
  on disk, the estimated seconds per image, whether it is already pulled, and whether it fits
  your free disk space.
- `shuto models pull TAG` downloads a model (after you confirm) and `shuto models use TAG`
  makes it the default.

Ollama installs from <https://ollama.com/download>. shuto does not install it.

## The catalog

The catalog lives in `src/shuto/data/models.yaml`. The table below is a sample of it, not the
list you will see: shuto filters and ranks by your hardware. Sizes are approximate.

| Model tag | Disk | Quality (1-5) | Notes |
|---|---|---|---|
| `moondream` | 1.7 GB | 1 | Fastest; weak keywords, drafts only |
| `qwen2.5vl:3b` | 3.2 GB | 3 | Good for its size |
| `gemma3:4b` | 3.3 GB | 3 | Solid small default for CPU or 6 GB GPUs |
| `qwen2.5vl:7b` | 6.0 GB | 4 | Strong descriptions and keywords |
| `minicpm-v:8b` | 5.5 GB | 4 | Good photo understanding |
| `qwen3-vl:8b` | 6.1 GB | 4 | Best default when it fits (8 GB+ GPU or 16 GB+ Apple) |
| `gemma3:12b` | 8.1 GB | 4 | Very good captions and keywords |
| `gemma3:27b` | 17 GB | 5 | Near-API quality; needs a 24 GB GPU or 32 GB+ Apple |

Some tags in the catalog are marked `verify: true` in the YAML: they were not confirmed online
when written. If a pull fails, check the tag on the Ollama library page.

## Time estimates: heuristic vs benchmark

The seconds-per-image figure comes from one of two sources, and shuto says which:

- **heuristic**: a fixed figure per hardware tier (CPU only, small GPU, large GPU, Apple
  Silicon by memory). Quick, but it can be off by a factor of two or more.
- **benchmark**: `shuto models bench [MODEL]` describes a bundled sample image twice (a cold
  load, then a warm run) and caches the warm time for your machine. Estimates then use the
  measured value. The cache is keyed by model and a hardware fingerprint, so it is redone if
  your hardware changes.

Expect CPU-only runs to be much slower (minutes per image for 8B models) than a GPU. The
first image after loading a model is slower than the rest.

The sample image is CC0, from Wikimedia Commons; see
[../src/shuto/data/SAMPLE_CREDIT.md](../src/shuto/data/SAMPLE_CREDIT.md).

## Disk space and other drives

Models are large. Before a pull, shuto checks free space where Ollama stores models and, if it
is short, lists other drives with enough room and the cheaper API options.

To store models on another drive, open the web app, go to **Settings, Where to keep AI models**
(the setup wizard has the same box on the AI model step), pick or type a folder and press
**Save and restart Ollama**. Tick **Move the models I already downloaded** to bring your existing
models along. No terminal is needed on Windows or macOS.

What shuto does for you:

- **Windows**: saves a per-user `OLLAMA_MODELS` setting (no administrator rights), closes Ollama
  (including the tray app) and starts it again.
- **macOS**: runs `launchctl setenv OLLAMA_MODELS ...`, adds a small login item
  (`~/Library/LaunchAgents/com.shuto.ollama-models.plist`) so it survives a reboot, then quits
  and reopens the Ollama app. **Use the standard folder** removes both again.
- Moving models: Ollama is stopped, the files are copied to the new folder, every size is
  checked, Ollama is restarted and must still list every model named by the old `manifests/`,
  and only then are the old files deleted (manifests first). Folders are compared by real
  identity, so a link, junction, drive alias or different capitalisation of the current folder
  is refused. If anything fails the setting is put back, Ollama is restarted on it, and only
  the files this run copied are removed. shuto checks the free space on the new drive first.
- A folder that already holds models is merged. A file with the same name but different content
  is never overwritten: shuto stops before changing anything and says so.
- A Windows `OLLAMA_MODELS` set for the whole computer (by an administrator) cannot be changed
  by shuto. Settings explains this and hides Save and Reset; ask someone with administrator
  rights.
- macOS: after a restart, the Ollama login item and shuto's login item run in no fixed order. If
  Ollama then shows no models, Settings says so: quit Ollama from the menu bar and open it again.

You can also remove a downloaded model from the model list (**Remove from this computer**; never
the one in use). If a download does not fit on the disk, **Replace: remove X and download Y**
offers the downloaded models whose removal makes room. Both ask for confirmation. The list shows
when it was last updated and gently flags it after about six months.

**Linux** needs an administrator, so it stays manual (systemd service):

```
sudo systemctl edit ollama
# add:
#   [Service]
#   Environment="OLLAMA_MODELS=/mnt/bigdisk/ollama"
sudo systemctl restart ollama
```

The `ollama` service user needs write access to that folder. If you run `ollama serve` by
hand, use `export OLLAMA_MODELS=/mnt/bigdisk/ollama` first.

`shuto doctor` prints the models folder in use and the free space there.

## Another Ollama answering

Only one Ollama can use the address `127.0.0.1:11434`. If a second copy (often inside WSL or
Docker, or an old install) holds it, your own Ollama cannot start and its models do not show up.
shuto notices when the Ollama that answers is older than the one installed here, or when
downloaded models in your models folder are missing from its list. It then says so in the app
(banner, Settings, setup wizard) and in `shuto doctor`, with steps: close the other Ollama, open
yours, press Try again. The Help page has the commands for helpers. Nothing is checked when your
Ollama address points to another computer.

## API fallback and cost

If a local model is too slow or too weak on your machine, you can use an API provider. This
sends each described image to the vendor, so it is not offline. Install the extra
(`anthropic`, `gemini` or `bedrock`), set the key (see the README on secrets), and
choose it with `shuto models use anthropic:` (or `gemini:`, `bedrock:`), the UI, or
`llm.provider` in the config.

Before describing, shuto shows an estimated cost for the batch from the price table in
`models.yaml` (list prices per million tokens plus an assumed image token count). A photo
typically costs a fraction of a cent to a few cents. The prices are approximate and dated, so
check the vendor's pricing page for the real figure.

## Keeping the catalog current

Models and prices change. To update `models.yaml`, check each tag on
<https://ollama.com/library> and each price on the vendor's pricing page, then update the
`updated` date and clear `verify` once confirmed. Pull requests that refresh the catalog
are welcome.
