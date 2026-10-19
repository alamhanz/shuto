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

## If you deleted photos on Shutterstock

shuto cannot see your Shutterstock account (there is no way for it to look), so it does not
know when you delete photos there. It still counts them as sent and leaves them out of new
imports. To send them again, tell shuto:

- **Web app:** on the **Upload** or **Submit** page, open **Deleted photos on Shutterstock?**,
  tick the photos, tick the confirm box and press **These are no longer on Shutterstock: upload
  them again**. Then press **Upload**; only those photos are sent. Photos you already noted as
  submitted are listed separately and unticked; photos Shutterstock accepted or rejected are not
  offered.
- **When adding a folder:** photos left out as "sent before" come with an **Add anyway**
  button. Look-alikes join the batch as new photos; exact copies of a sent photo are sent again.
- **Command line:** `shuto reupload BATCH --ids 12 13 --yes` (or `--all`, optionally with
  `--include-submitted`). Without `--yes` it only says what it would do.

Each photo goes back to *prepared* (or *approved* if its upload copy is gone, or *ready* if it
was only remembered and never described here), its send date is cleared, a `reupload_requested`
event is recorded, and `uploaded.csv` and the batch report are rewritten. Then run the usual
Upload; the Shutterstock CSV is rewritten for those photos.

## Commercial vs Editorial

Shutterstock sells photos under two kinds of licence, and each has its own rules.

**Commercial** photos can be used in ads, products and packaging, so they must be "clean":

- No visible logos, brand names or shop signs.
- Recognizable people need a model release (a signed permission).
- Some buildings and private places need a property release.

**Editorial** photos show real events and places for news, blogs and articles:

- Logos, brands and recognizable people are fine.
- The caption must be strict and factual, in the form
  `CITY, PREFECTURE, COUNTRY - MONTH D, YYYY: Factual sentence.` (the prefecture is left out
  only when it is unknown).
- The image must not be manipulated.
- Brand names are allowed in the caption and keywords (never in Commercial text).

Many everyday street photos contain a sign or a face and can only be sold as Editorial. shuto
handles this for you: when the rules check finds something that makes a photo unsuitable for
Commercial, shuto switches the photo to Editorial and writes the caption in the Editorial
format. You can see and change this in the review step. Photos Shutterstock does not accept
at all are marked **Can't send as is**, with the reason.

### How shuto decides a photo is Editorial

The AI looks at each photo and reports what it sees: logos or brand names, recognizable people,
events (concerts, sports), screens, artwork as the main subject, and readable personal data.
Your own rules and the Shutterstock rules check turn these into a decision:

- Logos and shop signs always keep a photo Editorial, even if you switch it to Commercial: a
  property release does not clear a trademark. Screens with real content do too.
- Recognizable people make the photo Editorial unless it has a model release; with a model
  release you can switch it to Commercial. Artwork, statues, characters and building interiors
  do the same with a property release.
- Events do not make a photo Editorial by themselves. Concerts, sports and similar events ask
  you for a look (press credentials and a case number), and other events remind you to name
  the event in the caption.
- Readable ID, card or account numbers block the photo (crop them out and send it back to the
  AI). Names, addresses and phone numbers make it Editorial and ask for a look.
- Releases cannot be attached to Editorial photos in the Shutterstock portal, so an Editorial
  photo does not need one.

shuto builds the caption prefix itself from the photo's date and place (GPS, then the file's own
data, then the AI, then your Default place), and the AI writes only the sentence. If the date is
unknown, the photo is stopped before upload. Before upload and export, every Editorial caption
is checked: small slips such as an en dash or "July 31 2026" are fixed automatically, and a
caption that is still wrong (on a photo that is already uploaded) is left out of the CSV with a
message naming the photo; the CSV is still written for the others.

**Switching one photo:** open it in Review and switch it between Editorial and Commercial.
Switching to Editorial rebuilds the caption at once; switching back restores the normal
description. No AI call is needed. The CSV marks editorial photos with `yes` and others with `no`.

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
- Titles, descriptions and keywords follow the owner's
  [metadata guide](shutterstock-metadata-guide.md): the prompt is built from it and the checks
  in `src/shuto/metadata/rules.py` enforce it. When a hard rule fails (too few words, a word list, too
  few keywords, a near-duplicate of another photo in the batch, text that is not English) the
  same model is asked again with the problems listed, up to 8 model calls per photo in total
  (`llm.max_tries`, default 8). It stops early when the answer is valid or when the model
  repeats itself. The best answer is kept; if it is still not valid the photo goes to
  "Needs a look" with the reason. Tries, tokens and seconds show in the batch report.
- Live speed check (local models only; `src/shuto/ai/speedcheck.py`): after every 5 photos described
  (`CHECK_EVERY`) the average seconds per photo is compared with the expected seconds per photo
  shown before the run (`src/shuto/ai/estimate.py` / `src/shuto/ai/recommend.py`, your hardware tier or benchmark).
  At 1.3 times that or more (`SLOW_FACTOR`) the job panel offers: fewer tries (3), a faster
  model that fits this machine and is already installed, or keep going (asked again only when
  the next check is slower than the one dismissed). The choices are held in one lock-protected
  object that the describe loop reads before each photo, so they apply to the photos not
  started yet at once; they last for that batch only (the default is Settings -> Tries per
  photo). A faster model that is not installed is only named. Each photo stores the model and
  max tries it was written with (`ai_run` in its metadata; `ai_model` and `ai_max_tries` in
  the report). API providers are not checked.
- Short prompt for small models: a local model whose context window is 4096 tokens or less
  (`context_tokens` in `data/models.yaml`; models not listed get the full prompt) is sent a
  shorter prompt with the same rules and the photo facts, about 40 percent fewer tokens.
  Settings shows a plain warning and names a bigger model when such a model is chosen.
- Context size sent to Ollama: shuto always sends `num_ctx` (8192 tokens, or the model's own
  window when that is smaller; 4096 when the model leaves under 1 GB of memory to spare on this
  computer). Ollama's own default (2048 or 4096) cut the start of the prompt. The short prompt
  is chosen from the `num_ctx` actually sent.
- File text is decoded as UTF-8, else Shift-JIS (or as the IPTC record declares); text that
  decodes as neither is ignored, control characters are removed, and a place name from the file
  is used only when it is plain English letters. A non-Latin city from the file never replaces a
  GPS place; it only fills a missing city when it is in English letters.
- Place for the caption, per part: place from the photo, then the AI, then (city only) the
  photo's prefecture or region ("TOKYO, JAPAN"), then your default place. The default place is
  never used against a prefecture or country the photo knows.
- With Settings -> Privacy "Include GPS location" off, only the ward or city of the GPS place is
  given to the AI (no neighborhood), so it cannot reach the keywords or caption. The command
  line (`shuto prepare`, `upload`, `reupload`, `export`) uses the same effective settings as the
  web app, including this one.
- At upload, with strict English on, a prepared copy whose title, description or keywords hold
  non-Latin text (for example one made by 0.4.0) is not sent; it is listed as failed with
  "edit it in Review".
- English only: text sent to Shutterstock must be English. Accented Latin letters are folded to
  ASCII (assumption: Shutterstock text is plain English letters). Any other script (Japanese,
  Chinese, Korean, Cyrillic, Arabic and so on) in the AI's answer counts as a hard failure and is
  repaired; if it stays, the photo is flagged, cannot be approved, prepared or put in the CSV
  (the CSV writer also refuses it). Words in other scripts from the photo's own title, caption
  and keywords are left out of the facts the AI sees. Place names come from the bundled GeoNames
  ASCII names.
- Metadata language (Settings -> Metadata language; config `style.english_only` and
  `style.metadata_language`, web app setting `ui.language`): strict English is the default. With
  it off, `ai.prompts.language_rule` puts "Write the description and keywords in <language>" in
  both prompts, photo-file hints keep their scripts, `ascii_fold`, the `non_english` check,
  `workflow.checks.not_english` (approve, prepare, export) and the CSV guard are skipped, and the word
  count ignores text in scripts written without spaces (Japanese, Thai). The CSV stays UTF-8
  without a BOM as before. The name is validated by `core.validate.clean_language` (letters,
  spaces, hyphens, 30 characters) because it goes into the prompt. Each photo stores
  `metadata.language` and the report has a `metadata_language` column. Location fields stay in
  English letters (the editorial caption prefix is a fixed English format). Cautions: local
  models may write the language badly; Shutterstock may expect English.
- HEIC/HEIF photos need the optional `pillow-heif` package (included in the installer). A photo
  that cannot be opened is flagged "HEIC photo could not be read" (install HEIC support, or the
  file is damaged) and the rest of the batch carries on.
- At upload time shuto removes any GPS location still inside a prepared copy (for example one
  made by 0.4.x) unless Settings -> Privacy allows it; a copy it cannot check is not sent.
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

## Default place for Editorial captions

An Editorial caption starts with the place: `CITY, PREFECTURE, COUNTRY - MONTH D, YYYY: ...`. The AI
works out the city and country from the picture. It never sees the place stored in the photo
(GPS), so when it cannot tell where a photo was taken, that part is missing, and Shutterstock
does not accept a caption that says `UNKNOWN`.

Set a **Default place** (city and country, for example Tokyo and Japan) in **Settings, Your
style** (or in the style step of the first-run guide). It is used only in Editorial captions, and
only for the part that is missing: a place the AI found always wins. Check that it is right for
each photo before you submit. Photos whose caption used it show a **Default place** label in
Review and on the photo page. With no default place, the missing part stays `UNKNOWN` and the
photo gets the note "No place known: set a default place in Settings or edit the caption".
Changing the caption by hand removes the label.

- **Where it comes from, per photo:** `ai` (the AI knew the place), `default`, `partly default`
  (a mix) or `unknown`. It is kept with the photo's text and shown in the batch report.
- **Config file:** `style.default_place` with `city` and `country` (English letters, up to 60
  characters each). A value saved in Settings wins over the file. `shuto doctor` shows it.

## The batch report (CSV)

Every batch has one report file that shuto keeps up to date after each step (add, check,
approve, edit, upload, export, submit, results). It lists, for each photo, what was checked and
what the answer was.

- **Where:** `batches/<batch>/report.csv` inside your shuto workspace. Every batch has one
  folder that holds all of its own files: `report.csv`, the batch's one Shutterstock CSV (`<batch>-shutterstock.csv`) and a
  `prepared/` folder with the photo copies that get uploaded. Your original photo folder is
  never written to. In the web app press **Download report** on the Review or Submit page, or
  **Open folder** next to it to see the files. On the command line, `shuto report BATCH`
  writes the report and prints its path and the batch folder (`--out PATH` writes a copy
  somewhere else). Folder names keep ASCII letters, digits, spaces and `( ) . _ -` (so `trip (2)` stays
  readable); other characters become `_`. A short code is added when the name had to change or
  has capital letters, so two batches never share a folder (even on Windows and macOS, which
  ignore case), and Windows reserved names such as `nul` or `con` get a `_` in front.
- **Older versions:** files made by 0.4.0 and earlier (`reports/`, `exports/`, `prepared/` in
  the workspace) are not moved or deleted. They keep working, download buttons still find
  them, and new files go to the batch folder.
- **Columns:** file, batch, quality and its reasons, which Shutterstock rules were hit, theme and
  how well the photo fits it (with the reason), one column per rule of yours (pass, fail or
  unknown; blank when the photo was not checked with that rule), the reasons for your rules,
  what set it aside, whether you approved it by hand, status, title, the place used in the caption, where it came from (gps, file, gps + AI, file + AI, ai, default, partly default,
  unknown) and whether it is the default place (yes or no), number of keywords, the AI
  model, how many seconds and tries the AI needed (and the most it was allowed), and the dates it was checked, approved, uploaded, submitted, accepted or rejected
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

shuto is an early version (0.6.1). The pipeline, command line and local web app work end to end,
and the tests run on Linux, macOS and Windows.

Known limits:

- GPS location is removed from the photo copies shuto uploads, also from copies prepared by
  older versions (Settings -> Privacy lets you keep it). It is not removed from anything else
  you share yourself.
- The sizes, speeds and prices in the model list are approximate and need re-checking from time
  to time. See [local-models.md](local-models.md) for choosing a local model.

Ideas for later:

- An OpenAI provider (planned) and other AI providers.
- Richer reports on why photos get rejected.

Issues and suggestions are welcome: see the "Found a problem?" section of the README.

The earlier AWS version (S3, SQS, Lambda, Bedrock) is kept at the git tag `aws-legacy`.
