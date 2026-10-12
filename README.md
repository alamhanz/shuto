# shuto

A local-first pipeline for Shutterstock contributors. It takes a folder of photos and carries
them through a fixed workflow:

```
collect -> cull -> describe -> guardrails -> prepare -> FTPS + CSV -> track
```

- **collect**: find photos, skip exact duplicates, group near duplicates, split into weekly batches.
- **cull**: drop photos that are too small, blurry or badly exposed, with the reason recorded.
- **describe**: a vision model writes the title, description and keywords in your style.
- **guardrails**: check the result against known Shutterstock restrictions.
- **prepare**: write upload-ready JPEG copies with the metadata embedded (IPTC/XMP). Originals are never modified.
- **FTPS + CSV**: upload the copies and write the metadata CSV files.
- **track**: keep per-photo status, acceptances and rejection reasons in a local SQLite database. Rejection reasons are fed back into later prompts.

shuto is an independent project and is not affiliated with or endorsed by Shutterstock.

## Why it works this way

Shutterstock has no upload API for contributors. The official paths are the web uploader,
FTPS upload and metadata CSV import, and shuto uses FTPS and CSV. The last step, **Submit on
the contributor portal, stays manual on purpose**. shuto never automates the portal: you
review and submit yourself.

## Privacy

Everything runs on your machine by default. The default AI provider is a local model served
by [Ollama](https://ollama.com). The only network calls are to Ollama on `127.0.0.1` and to
Shutterstock's FTPS host when you upload. If you choose an API provider (Anthropic,
Gemini, Bedrock), the images you describe are sent to that vendor. The web UI listens on
`127.0.0.1` only.

## Install

Requires Python 3.12 or newer. Works on Linux, macOS and Windows.

```
git clone https://github.com/alamhanz/shuto.git
cd shuto
python -m venv .venv
```

Activate the environment (`source .venv/bin/activate` on Linux/macOS, `.venv\Scripts\activate`
on Windows), then:

```
pip install -e .
```

Optional extras, combined as needed, for example `pip install -e ".[anthropic,keyring]"`:

| Extra | Adds |
|---|---|
| `anthropic` | Anthropic API provider |
| `gemini` | Google Gemini API provider |
| `bedrock` | Amazon Bedrock provider (uses your normal AWS credentials) |
| `keyring` | store secrets in the OS keyring |
| `heic` | read HEIC/HEIF photos |
| `all` | everything above |

### Local model (default): install Ollama

Download Ollama from <https://ollama.com/download> (installers for Windows and macOS; an
install script for Linux). shuto never installs software for you. Run `shuto doctor` to see
whether Ollama is installed and running, and which models are pulled.

## Quickstart

```
shuto init                 # creates the workspace, shows hardware and a top-8 model table
shuto run ./my-photos      # import + cull + describe (shows an estimate first)
shuto                      # opens the web UI at http://127.0.0.1:8765 for review
```

1. `shuto init` detects your hardware and lists up to eight models that fit, with an estimated
   time per image. Pick one; shuto asks before downloading anything. See
   [docs/local-models.md](docs/local-models.md).
2. `shuto run ./folder` imports the photos, culls them and describes the rest. Before
   describing, it prints the estimated time (local) or cost (API) and asks to continue.
3. Review in the web UI (`shuto` or `shuto ui`): edit text, set release ids, approve. The CLI
   equivalents are `shuto review BATCH`, `shuto edit ID` and `shuto approve BATCH`.
4. `shuto prepare BATCH` writes the upload-ready copies.
5. `shuto upload BATCH` sends them over FTPS. Upload is off by default: set
   `deliver.upload_enabled` in the config, or pass `--yes`.
6. `shuto export BATCH` writes the CSV files (`--dry-run` shows what would be written).
7. Import the CSV and press **Submit** on the Shutterstock contributor portal yourself.
8. `shuto submitted BATCH` marks the batch. Later, record outcomes with `shuto accept FILE` or
   `shuto reject FILE --reason CODE`.

Batch ids are listed by `shuto status`.

### Commands

| Command | Purpose |
|---|---|
| `ui` | start the web UI (the default when no command is given) |
| `init`, `doctor` | set up the workspace and model; report hardware, Ollama and model status |
| `models` | `list`, `pull TAG`, `use SPEC`, `bench [MODEL]` |
| `import`, `cull`, `describe`, `run` | the early stages, or all three with `run` |
| `ledger export [PATH]`, `ledger import PATH` | back up / restore the list of uploaded photos |
| `review`, `edit`, `approve` | inspect and change metadata, approve photos |
| `prepare`, `upload`, `export`, `submitted` | delivery |
| `status`, `accept`, `reject` | tracking |
| `secrets set NAME`, `check-ftps` | store a secret; test the FTPS login |
| `restrictions` | `list` rules or `check TEXT` against them |

Use `shuto COMMAND --help` for flags. Global options: `--config FILE`, `-v`.

## Already-uploaded memory

shuto remembers what you have sent to Shutterstock, offline, so you do not pay to process
(or upload) the same picture twice.

- **What is remembered:** every photo that reached `uploaded`, `exported`, `submitted`,
  `accepted` or `rejected`, with its checksum and a small visual fingerprint (dHash).
- **On import:** an exact copy of such a photo, or an edited / re-saved / resized copy of it
  (fingerprint within `import.near_duplicate_distance`, default 6), is not processed again.
  It is recorded as `already_uploaded` and reported, for example
  `IMG_1.jpg: already uploaded on 2025-03-04 as 20250304-station-001.jpg`.
  Use `shuto import FOLDER --force` (web: "Import anyway") to bring such photos in regardless;
  `already_uploaded` photos can also be sent back to `ready` by hand.
- **Photos uploaded before shuto:** `shuto import FOLDER --mark-uploaded` (web: "These were
  uploaded before") only registers them as uploaded; no thumbnails, no describing, no upload.
  It asks for confirmation (CLI: `--yes` skips it) because it cannot be undone, and it cannot be
  combined with `--force`. `shuto run` has no `--force`; use `shuto import --force` instead.
  Rejected photos are remembered too and reported as "previously submitted and rejected".
- **The ledger:** `<workspace>/uploaded.csv` (UTF-8: sha256, dhash, orig_name, submit_name,
  uploaded_at, batch_id) is rewritten after each upload and after `--mark-uploaded`. Rows
  already in the file are never dropped. Copy it somewhere safe as your backup.
  `shuto ledger export [PATH]` writes it from the database on demand.
- **New machine or workspace:** copy `uploaded.csv` over and run `shuto ledger import PATH`.
  It is safe to repeat; photos are matched by sha256.

## Web UI

`shuto` (or `shuto ui`) opens <http://127.0.0.1:8765>. It has a batch overview, a batch page
with thumbnails and bulk actions, a photo page for review and editing, and a settings page
with hardware info, the model picker and provider key status. Everything the UI does is also
available from the CLI.

## Commercial vs Editorial

Shutterstock accepts photos under two licences, and the rules differ. If you are new to it:

**Commercial** is for ads, products and packaging.
- No visible logos, brand names or shop signs.
- Recognizable people need a model release.
- Restricted buildings need a property release.

**Editorial** is for news, blogs and articles.
- Logos, brands and recognizable people are fine.
- The caption must be strict and factual: `CITY, COUNTRY - MONTH D, YYYY: Factual sentence.`
- No manipulation of the image.

Many street photos contain a sign or a face and can only be sold as editorial. shuto handles
this through its guardrails: when a rule says a photo is restricted for commercial use, the
photo is switched to editorial automatically and the caption is built in the editorial
format. Photos that Shutterstock does not accept at all are blocked and reported.

## Guardrails

Guardrails apply a rule set both in the prompt and as a deterministic check on the model's
output (for example, brand names in keywords). The rules and their sources are in
[docs/shutterstock-restrictions.md](docs/shutterstock-restrictions.md). The lists are
**non-exhaustive**, Shutterstock reviews case by case, and its policies change, so re-check
them periodically. Guardrails reduce rejections; they do not guarantee acceptance.

## Configuration

Copy [shuto.example.yaml](shuto.example.yaml) to `shuto.yaml` and edit it, then pass it with
`--config` or let shuto find it. Settings cover sources, batching, cull thresholds, the LLM
provider and model, your style (theme, tone, keyword counts), review and the FTPS host. The
theme in the example, "Everyday Japan", is only an example style; set your own.

Model choice precedence: CLI flag, then the choice made in the UI or with `shuto models use`,
then the config file, then `auto`.

## Secrets

Secrets never go in the config file. shuto reads them from environment variables, a `.env`
file, or the OS keyring (`pip install -e ".[keyring]"`, then `shuto secrets set NAME`).

| Name | Used for |
|---|---|
| `SHUTO_FTPS_USER`, `SHUTO_FTPS_PASSWORD` | Shutterstock FTPS login |
| `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` | API providers |

Bedrock uses the standard AWS credential chain. `shuto check-ftps` tests the FTPS login
without uploading anything.

## Status and roadmap

Early version (0.1). The pipeline, CLI and local web app work end to end, and tests run on
Linux, macOS and Windows. Known limits: GPS data in photos is not stripped in v1 (the review
page warns when it is present), and the model catalog's sizes, speeds and prices are
approximate and need periodic re-checking. Ideas for later: GPS stripping, an OpenAI provider (planned), more providers,
richer rejection analytics. Issues and suggestions are welcome.

The earlier AWS version (S3, SQS, Lambda, Bedrock) is preserved at the git tag `aws-legacy`.

## Credits

The benchmark image `src/shuto/data/sample.jpg` is CC0 from Wikimedia Commons; see
[src/shuto/data/SAMPLE_CREDIT.md](src/shuto/data/SAMPLE_CREDIT.md).

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE), copyright 2026 Alamsyah Hanza.
