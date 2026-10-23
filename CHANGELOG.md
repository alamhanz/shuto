# Changelog

Release notes are also published on the GitHub Releases page. Newest first.

## 0.6.3

### Changed
- **Send back to AI** now runs the AI in the background. You can keep reviewing the other
  photos while it works, and the sent-back photos return one by one in their new group.
- The Review page shows a small note while the AI is working, and when photos are back.

### Fixed
- Sending back all photos on the screen no longer ends with a confusing "not allowed" message.
- **Approve all that look good** only approves the photos you can see as good, not ones that
  changed while the page was open.
- If the AI fails on a sent-back photo, it keeps its old text and appears under Needs a look.
- Jobs left unfinished when the app was closed no longer look like they are still running.

## 0.6.2

### Fixed
- When an older Ollama (for example one inside WSL or Docker) answers instead of the one you
  installed, shuto now says so in plain words instead of "No AI model set" or a confusing
  "requires a newer version" error when downloading a model.

### Added
- The warning, with simple steps, shows in the model banner, Settings, the setup wizard and
  `shuto doctor`. The Help page has a new entry, "shuto says another Ollama is answering".
- Detection only runs for an Ollama on this computer, not for one you set up on another machine.

## 0.6.1

### Added
- **Editorial photos get a proper caption.** Captions now read
  `CITY, PREFECTURE, COUNTRY - MONTH D, YYYY: Sentence`. shuto builds the place and date itself
  and the AI writes only the sentence.
- Switching a photo to editorial (or shuto making it editorial) rebuilds its description right
  away, and switching back restores the normal description. No AI call is needed.
- Editorial photos are checked before upload and export. Small slips (dashes, "July 31 2026") are
  fixed automatically; an already-uploaded photo whose caption is still wrong is left out of the
  CSV with a reason naming it, and the CSV is still written for the others.
- The AI now also reports events, screen content, artwork as the main subject and readable
  personal data. Concerts, sports and similar events remind you about press credentials;
  readable ID, card or account numbers (exact kinds only, not "license plate" or "business card")
  block the photo; names, addresses and phone numbers make it
  editorial and ask for a look.
- Brand names are allowed in editorial descriptions and keywords (still never in commercial
  ones).
- Phone numbers, email addresses and card numbers in a description or keywords are flagged.
- A warning explains that releases cannot be attached to editorial photos in the Shutterstock
  portal.
- Photos Shutterstock already has (sent before, uploaded, submitted, accepted) show a navy
  "Sent" chip with the upload icon and a "Sent" ribbon on the Review thumbnail; the photo page
  says so too.
- The Submit page shows real Shutterstock screenshots (Portfolio, Submit content, Upload CSV
  window, Submit buttons). The steps say "Pending content" and note that editorial photos take
  no releases.
- Docs: new Releases, Intellectual property by type, Personal data and Sources sections in the
  restrictions guide; `restrictions.yaml` is now version 2.

### Fixed
- **Send back to AI** now removes the photos from every Review list (including All). A note says
  how many wait for the AI and links to Check. They return in their new group once the AI has
  finished.
- Keyword top-up also runs when a photo has 7-24 keywords and keeps asking for new ones until it
  reaches 25 (never past the maximum tries or keyword limit; it stops as soon as a call adds no
  new keywords).
- Unticking Editorial on a photo that the rules keep Editorial no longer replaces its factual
  sentence with the commercial headline.
- "none" or "N/A" answers from the AI in the new detected fields count as empty.
- Releases on an editorial photo now show a plain note instead of the "Shutterstock will reject"
  warning.
- Ollama started by shuto keeps your proxy and certificate settings, so model downloads still
  work behind a proxy.
- Windows: a models folder stored as an expandable registry value (`%USERPROFILE%\...`) is now
  read correctly.
- Windows: restarting Ollama only closes the current user's Ollama, not another signed-in user's.
- Ollama started by shuto no longer inherits shuto's virtual environment or API keys.

### To check after your next upload
- The CSV writes the editorial column as `yes`/`no`. After uploading, confirm in the Shutterstock
  portal that **Usage** shows **Editorial** for those photos.

## 0.6.0

### Internal refactor, no behavior change
- The code is reorganised into packages with clear layers (`core`, `store`, `ingest`, `places`,
  `metadata`, `ai`, `ollama`, `deliver`, `settings`, `update`, `system`, `workflow`, `cli`, `web`)
  instead of one flat folder with a 2,700-line `pipeline.py`. A test keeps the layers honest.
- Nothing you see changes: same web app, command line, settings, database, photo files, CSV and
  self-update. Settings saved by 0.4.0 and 0.5.0 keep working; the update from either
  version was simulated before release.
- One table now describes every AI provider (name, label, key, SDK), and the same small helpers
  are no longer copied in several places.

### Fixed
- **Thinking models work** (for example qwen3.5): shuto turns the hidden "thinking" off for
  models that have it, so the answer is no longer cut off with nothing written. If a model still
  returns only thinking, the error now says so and the photo is tried again.
- **Too few keywords are topped up.** When a small model gives fewer keywords than Shutterstock
  needs, shuto shows it the keywords it has and asks for more specific new ones (same model, your
  language setting), then merges them.
- **More tries before "Needs a look".** Every retry is a little warmer (up to a limit), so a model
  that repeats itself gets a real chance. Tries stop early only when two retries in a row bring
  the same answer; the best answer is kept and the reasons are listed otherwise.
- **Warning when you prepare a photo with a problem.** If you approved a photo that still breaks a
  Shutterstock text rule (for example too few keywords), preparing it now says so in the web app
  and in `shuto prepare`. It is still prepared, because you approved it.

## 0.5.0

### Privacy
- **GPS is removed from the photos you upload.** The copy shuto prepares has no GPS location
  (EXIF GPS block removed; location fields in old XMP/IPTC are replaced). Date, camera and other
  EXIF stay. Your original files are never changed. To keep the location, turn on
  Settings -> Privacy -> "Include GPS location in uploaded photos". Photos prepared before
  updating are cleaned again at upload time, so no location leaves while the setting is off
  (a copy that cannot be checked is not sent).
- The photo page and Review chips ("Place included" / "Place removed") follow this setting.

### Better titles, keywords and places
- **Photo facts first.** shuto reads each photo's own EXIF, IPTC and XMP (date, camera, lens,
  your title, caption and keywords) and turns GPS into a place name offline (GeoNames, no
  internet needed, coordinates never leave your computer). These facts go to the AI as data to
  use instead of guessing.
- **Place levels:** neighborhood, ward/city, prefecture or region, country (for example
  Sakuragaokacho, Shibuya, Tokyo, Japan). Editorial captions use the ward or city.
- **Place order:** GPS, then the location written in the file, then the AI's guess, then your
  default place.
- **Default place** (Settings -> Your style): used in editorial captions when nothing else is
  known. The report gets `place`, `place_source` (gps, file, gps + AI, file + AI, ai, default, partly default,
  unknown) and `default_place` columns, and Review shows a "Default place" chip.
- **Metadata guide:** prompts and checks follow the Shutterstock metadata guide (one clear
  sentence, 25-45 strong keywords, no repeats, no two photos in a batch alike). If an
  answer breaks a rule, the same model is asked to fix it (see "Tries, languages and small
  models").

### Metadata language
- **Settings -> Metadata language.** "English only (strict)" is on by default and works as
  before. Turn it off and type the language for descriptions and keywords (for example
  Japanese): both prompts then say "Write the description and keywords in <language>" instead
  of the English-only rule, words in other scripts from the photo file are kept as hints, accents
  are not changed, and the not-English checks (AI answer, text you type, CSV guard) are off.
  The CSV stays UTF-8. The language name takes letters, spaces and hyphens only (30 characters
  at most) because it goes into the AI's instructions; off with an empty box cannot be saved.
- **Cautions** are shown when you turn strict off and next to the setting: local AI models may
  write the language badly (shown prominently when the chosen model is local), and Shutterstock
  may expect English metadata.
- **Traceable:** each photo remembers the language it was written in, and the batch report has a
  `metadata_language` column. Photos already checked keep their text when you switch.

### Tries, languages and small models
- **Up to 8 tries per photo.** If an answer breaks a rule (too short, a word list, too few
  keywords, almost the same as another photo in the batch) the AI is asked again, up to 8 model
  calls in total (`llm.max_tries`). It stops when the answer is valid or the AI repeats itself;
  if it still is not valid the photo goes to "Needs a look" with the reason.
- **Max tries per photo** is a setting (Settings -> Tries per photo, 1 to 8, default 8): fewer
  is faster, more gives better titles and keywords.
- **Live speed check** (local AI only). After every 5 photos the AI has written, shuto compares
  the average time per photo with the time it told you before the run. If it is 30 percent
  slower or more, the page offers, right away and for the photos not started yet: fewer tries
  (up to 3), a faster model that is already on this computer, or "Keep going" (asked again
  only if it gets slower still). A faster model that is not downloaded yet is named, with a
  pointer to Settings; nothing is downloaded during a batch. Online AI is not checked.
- The batch report has an `ai_max_tries` column next to `ai_model`, so a change during a
  batch can be traced photo by photo.
- **English only.** Japanese, Chinese, Korean, Cyrillic and other non-Latin text in a title or
  keyword is a failure that gets repaired, and the photo is flagged if it stays. Such a photo
  cannot be approved, prepared or put in the CSV. Accents are turned into plain letters
  (cafe, Kyoto). Non-English words from the photo's own title or keywords are not passed on.
- **Short prompt for small models.** Models with a small memory window (moondream, llava-phi3)
  get a shorter prompt (about 40 percent fewer tokens, same rules and photo facts). Settings
  warns that a bigger model gives better results and names one.
- **HEIC photos** that cannot be read are flagged ("HEIC photo could not be read - install HEIC
  support") instead of failing quietly; the rest of the batch continues.
- Place source says "gps + AI" (or "file + AI") when the photo only gave the country and the AI
  supplied the city.

### Fixes after review
- File text (IPTC/XMP): Shift-JIS text is read correctly (UTF-8 first, then Shift-JIS, or as the
  record declares); unreadable text is ignored instead of becoming garbage, control characters
  are removed, and long titles, captions and keywords are capped. A place from the file must be
  plain English letters to be used.
- No empty "Photographer title: ." lines when the title or keywords are only Japanese.
- A Japanese city in the file no longer replaces a GPS place (the GPS prefecture and country are
  kept).
- Captions: when only the prefecture is known (for example Tokyo) and the AI gives no city,
  the caption uses the prefecture ("TOKYO, JAPAN") instead of your default city. The default
  place is never used against a known prefecture or country.
- With "Include GPS location" off, the neighborhood is not given to the AI, so it cannot appear
  in keywords or captions.
- `shuto prepare`, `upload`, `reupload` and `export` now follow the Privacy setting from
  Settings, like the web app.
- Ollama gets an explicit context size (`num_ctx`, 8192 or less) so long prompts are no longer
  cut; the short prompt follows it.
- Upload refuses a prepared copy with non-English text when strict English is on ("edit it in
  Review").
- Older Latin-1 file text (for example Köln, España) is read correctly or ignored, never
  shortened; Shift-JIS is only used for text that really is Shift-JIS. Place names in XMP win
  over a doubtful IPTC reading.
- `llm.max_tries` in the config is limited to 1-8, like the setting in the app.
- A failing speed check no longer stops a Check run; it just goes on without it.
- Latin letters such as Ł, Đ, Þ, ð and ħ are no longer counted as non-English.

### Check and Add
- Wizard Continue works as soon as a model download starts; the download bar stays visible
  while scrolling and the page reloads when the download finishes.
- "Name for this batch" starts as the folder name.
- Honest Check progress: step 1 of 2 and step 2 of 2, with time left.
- Folders with more than 250 photos are split into batches of at most 250.

### Upload and Submit
- Real Cancel for uploads; the upload waits (about 4 minutes) when the internet drops, and shows
  honest progress and time left.
- Each batch has one folder with its report, its one Shutterstock CSV (Shutterstock's exact
  format) and the prepared copies, with an "Open folder" button.
- The Submit page explains the real Shutterstock steps (Portfolio -> Not submitted -> Upload
  CSV -> Submit -> Pending).
- Photos you deleted on Shutterstock can be sent again, and "Add anyway" imports them again.
- Readable, collision-safe batch folder names.

### Upgrading
Settings -> Updates -> Update now (verified against the release checksum).
