# How shuto works in detail

The [README](../README.md) covers install and everyday use. This page explains four things
that happen behind the scenes: how shuto remembers what you already sent, the difference
between Commercial and Editorial photos, how the rules check works, and where the project is
heading.

Contents: [Already-sent memory](#already-sent-memory) |
[Commercial vs Editorial](#commercial-vs-editorial) | [Rules check](#rules-check-guardrails) |
[Status and roadmap](#status-and-roadmap)

## Already-sent memory

shuto remembers which photos you have sent to Shutterstock, so it never spends time (or money,
with an online AI) on the same picture twice. This memory lives only on your computer.

**What it does for you**

- When you add a folder, photos you sent before are set aside and shown as **Sent before**.
  This also catches edited, re-saved or resized copies, not only identical files.
- You get a short list of what was skipped, for example
  `IMG_1.jpg: already uploaded on 2025-03-04 as 20250304-station-001.jpg`.
- A photo that Shutterstock rejected earlier is remembered too. It is reported as
  "previously submitted and rejected" so you do not send it again by accident.
- Want a skipped photo anyway? In the web app, tick **Add even photos I sent before** on the
  Add page. Skipped photos can also be sent back to the waiting list by hand.

**Photos you uploaded before you started using shuto**

On the Add page, choose **I already uploaded these: just remember them**. shuto only notes the
photos as sent. It does not check, describe or upload them. It asks you to confirm because this
cannot be undone, and it cannot be combined with "add even photos I sent before".

**Backing up the memory**

The list is saved as `uploaded.csv` in your shuto workspace folder and is updated after every
upload. Copy that file somewhere safe. On a new computer, put it back and import it (see
below); importing twice is harmless.

<details>
<summary><strong>For developers</strong></summary>

- Remembered photos are those that reached `uploaded`, `exported`, `submitted`, `accepted` or
  `rejected`. Each is stored with its sha256 checksum and a dHash visual fingerprint.
- On import, an exact checksum match, or a fingerprint within `import.near_duplicate_distance`
  (default 6, range 0-64), is recorded with status `already_uploaded` and not processed
  further. Matches against rejected photos are reported as "previously submitted and
  rejected".
- CLI:
  - `shuto import FOLDER --force` imports such photos anyway. `shuto run` has no `--force`; use
    `shuto import --force` first.
  - `shuto import FOLDER --mark-uploaded` registers photos as uploaded and does nothing else.
    It prompts for confirmation unless `--yes` is given, and errors when combined with
    `--force`.
  - `shuto ledger export [PATH]` writes the ledger from the database (default
    `<workspace>/uploaded.csv`; rows already in the default file are kept).
  - `shuto ledger import PATH` merges a ledger CSV into the database, matching by sha256, and
    reports how many photos were added or marked uploaded.
- `uploaded.csv` is UTF-8 with the columns sha256, dhash, orig_name, submit_name, uploaded_at,
  batch_id. It is rewritten after each upload and after `--mark-uploaded`.

</details>

## Commercial vs Editorial

Shutterstock sells photos under two kinds of licence, and each has its own rules.

**Commercial** photos can be used in ads, products and packaging, so they must be "clean":

- No visible logos, brand names or shop signs.
- Recognizable people need a model release (a signed permission).
- Some buildings and private places need a property release.

**Editorial** photos show real events and places for news, blogs and articles:

- Logos, brands and recognizable people are fine.
- The caption must be strict and factual, in the form
  `CITY, COUNTRY - MONTH D, YYYY: Factual sentence.`
- The image must not be manipulated.

Many everyday street photos contain a sign or a face and can only be sold as Editorial. shuto
handles this for you: when the rules check finds something that makes a photo unsuitable for
Commercial, shuto switches the photo to Editorial and writes the caption in the Editorial
format. You can see and change this in the review step. Photos Shutterstock does not accept
at all are marked **Can't send as is**, with the reason.

<details>
<summary><strong>For developers</strong></summary>

- Rule outcomes (`restricted`, `prohibited`, `no_upload`) and the resulting action are listed
  in [shutterstock-restrictions.md](shutterstock-restrictions.md#outcomes). `restricted` forces
  `editorial=yes`; the other two block the upload (status `blocked`).
- `shuto edit ID --editorial` / `--no-editorial` changes the flag by hand; guardrails re-run
  after each edit.
- Web app wording for the two licences lives in `src/shuto/web/ux.py` (`HELP`).

</details>

## Rules check (guardrails)

Before anything is uploaded, shuto checks every photo against a list of known Shutterstock
restrictions: brand names, trademarked designs, restricted buildings, caption format, and
more. In the web app this appears as the **Shutterstock rules check**; photos that need your
attention are marked **Needs a look**.

Two important limits:

- The lists are **not complete**. Shutterstock reviews each photo case by case and changes its
  policies over time.
- The check lowers the chance of a rejection; it cannot promise acceptance.

The rules and their sources are in [shutterstock-restrictions.md](shutterstock-restrictions.md).
If you notice a rule that changed, please
[report a rule update](https://github.com/alamhanz/shuto/issues/new?template=restriction_update.yml).
For how the check looks in the web app, see [web-guide.md](web-guide.md).

<details>
<summary><strong>For developers</strong></summary>

- The rules apply twice: they are put into the AI prompt, and a deterministic post-check runs
  on the model's output (for example, brand names in keywords). Both run again after any edit.
- Rules are kept in `docs/shutterstock-restrictions.md` and a machine-readable copy.
- `shuto restrictions list` shows the rules; `shuto restrictions check TEXT` tests a piece of
  text against them.

</details>

## Status and roadmap

shuto is an early version (0.3.0). The pipeline, command line and local web app work end to end,
and the tests run on Linux, macOS and Windows.

Known limits:

- GPS location data inside photos is not removed in this version. The review page warns you
  when a photo contains it.
- The sizes, speeds and prices in the model list are approximate and need re-checking from time
  to time. See [local-models.md](local-models.md) for choosing a local model.

Ideas for later:

- Removing GPS data automatically.
- An OpenAI provider (planned) and other AI providers.
- Richer reports on why photos get rejected.

Issues and suggestions are welcome: see the "Found a problem?" section of the README.

The earlier AWS version (S3, SQS, Lambda, Bedrock) is kept at the git tag `aws-legacy`.
