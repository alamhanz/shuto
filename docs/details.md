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

## Theme and your own rules

After the quality check, every photo goes through three filter layers. All three are answered
in the **same AI call** that writes the title, description and keywords, with the same model.
There is no second model, no extra download and no extra photo upload to an online AI.

1. **Shutterstock rules.** The check described above (brand names, releases, caption format).
   It is the only layer that can stop a photo completely (**Can't send as is**).
2. **Theme fit.** If you set a theme (for example "Everyday Japan") the AI says whether the
   photo fits it: yes, partly or no. A "no" puts the photo under **Set aside**. A "partly" is
   let through, and the reason is shown in Review and in the report. You can turn the theme
   check off, or make "partly" show up as **Needs a look** instead.
3. **Your rules.** Up to 10 rules of your own, such as "Must avoid: people" or "Must include:
   a street scene" (2 to 80 characters each). Each rule is either **set aside** or **just
   flag** (the photo shows as **Needs a look**) when it fails. The rules are global: they apply
   to every batch.

The AI always gives a short reason with each answer, and Review shows it. If the AI gives no
usable answer for a check, the photo goes to **Needs a look** with a message. It is never
passed silently and never set aside by mistake.

**Set aside is not final.** Open the photo and press **Approve anyway** if you disagree. shuto
remembers that you did, so a later edit does not send it back. A photo that breaks a Shutterstock
rule still cannot be approved this way.

**Rule changes only apply to photos described from now on.** Photos already described keep the
results (and the rules) they were checked with. shuto never re-checks old photos on its own and
never moves approved or uploaded photos.

Small local models judge less reliably than large online ones. That is why the reason is always
shown and why set aside can be undone.

Where to set this up: **Settings > Your photo rules** in the web app, or on the command line
(`shuto rules`). Both use the same rules. The config file can also hold them in the `filters:`
section of [shuto.example.yaml](../shuto.example.yaml); rules saved in the web app or with
`shuto rules` replace the ones from the file.

## The batch report (CSV)

Every batch has one report file that shuto keeps up to date after each step (add, check,
approve, edit, upload, export, submit, results). It lists, for each photo, what was checked and
what the answer was.

- **Where:** `batches/<batch>/report.csv` inside your shuto workspace. Every batch has one
  folder that holds all of its own files: `report.csv`, the Shutterstock CSV files and a
  `prepared/` folder with the photo copies that get uploaded. Your original photo folder is
  never written to. In the web app press **Download report** on the Review or Submit page, or
  **Open folder** next to it to see the files. On the command line, `shuto report BATCH`
  writes the report and prints its path and the batch folder (`--out PATH` writes a copy
  somewhere else). Folder names keep only letters, digits and `. _ -`; other characters become
  `_` and a short code is added so two batches never share a folder.
- **Older versions:** files made by 0.4.0 and earlier (`reports/`, `exports/`, `prepared/` in
  the workspace) are not moved or deleted. They keep working, download buttons still find
  them, and new files go to the batch folder.
- **Columns:** file, batch, quality and its reasons, which Shutterstock rules were hit, theme and
  how well the photo fits it (with the reason), one column per rule of yours (pass, fail or
  unknown; blank when the photo was not checked with that rule), the reasons for your rules,
  what set it aside, whether you approved it by hand, status, title, number of keywords, the AI
  model, how many seconds and tries the AI needed, and the dates it was checked, approved, uploaded, submitted, accepted or rejected
  (with the rejection reason).
- **Excel:** the file opens directly in Excel and shows Japanese text correctly. Text that
  could be read as a formula is stored as plain text. If Excel has the file open, shuto cannot
  update it; close it, and the next step refreshes it (the step itself still succeeds).

## Upgrading to 0.4.0

0.4.0 upgrades shuto's database (a one-time, automatic step the first time it starts). Photos
described before 0.4.0 simply show as "not checked" for theme and rules. **Going back to 0.3.x
afterwards will not work:** the older version cannot open the upgraded database. Keep a copy of
the workspace folder first if you may want to go back.

<details>
<summary><strong>For developers</strong></summary>

- Filters add two optional properties at the end of the describe JSON schema: `theme_fit`
  (only with an active theme) and `checks` (one required key `r1`..`rN` per rule). Both are
  `{"reason", "answer"|"fit"}` objects, reason first. The core fields are validated as before.
- Rules are asked as neutral statements ("Is this in or true of the photo: ..."); shuto maps
  yes/no to pass/fail by rule kind. Rule text is quoted in the prompt.
- Anything unreadable or missing in the filter part becomes `unknown` and never raises.
- Statuses: guardrail block, then `set_aside`, then `needs_review`, then `described`.
- Results are stored per photo (`photos.filters_json`) with a copy of the theme and rules used.
- `shuto rules list|add|remove|clear|mode|theme|partly` writes the `ui.filters` setting.

</details>

## Status and roadmap

shuto is an early version (0.4.0). The pipeline, command line and local web app work end to end,
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
