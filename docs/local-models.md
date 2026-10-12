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

To store models on another drive, set `OLLAMA_MODELS` to a folder there and restart Ollama.
Models already pulled stay in the old folder unless you move them.

**Linux** (systemd service):

```
sudo systemctl edit ollama
# add:
#   [Service]
#   Environment="OLLAMA_MODELS=/mnt/bigdisk/ollama"
sudo systemctl restart ollama
```

The `ollama` service user needs write access to that folder. If you run `ollama serve` by
hand, use `export OLLAMA_MODELS=/mnt/bigdisk/ollama` first.

**macOS**:

```
launchctl setenv OLLAMA_MODELS /Volumes/BigDisk/ollama
```

Then quit and reopen the Ollama app. (For `ollama serve` in a terminal, use `export` instead.)

**Windows** (PowerShell):

```
setx OLLAMA_MODELS "D:\ollama\models"
```

Then quit Ollama from the system tray and start it again. `setx` only affects new processes.

`shuto doctor` prints the models folder in use and the free space there.

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
