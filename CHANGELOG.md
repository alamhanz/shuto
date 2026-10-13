# Changelog

Release notes are also published on the GitHub Releases page. Newest first.

## 0.5.0

### Privacy
- **GPS is removed from the photos you upload.** The copy shuto prepares has no GPS location
  (EXIF GPS block removed; location fields in old XMP/IPTC are replaced). Date, camera and other
  EXIF stay. Your original files are never changed. To keep the location, turn on
  Settings -> Privacy -> "Include GPS location in uploaded photos". Photos prepared before
  updating keep what they had; prepare them again.
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
  known. The report gets `place`, `place_source` (gps, file, ai, default, partly default,
  unknown) and `default_place` columns, and Review shows a "Default place" chip.
- **Metadata guide:** prompts and checks follow the Shutterstock metadata guide (one clear
  sentence, 25-45 strong keywords, no repeats, no two photos in a batch alike). If the first
  answer breaks a rule, one repair request asks the same model to fix it.

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
