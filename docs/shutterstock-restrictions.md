# Shutterstock restrictions (guardrails)

Source of truth for shuto's guardrails. The pipeline should check every photo against these
rules (in the prompt and in a deterministic post-check) before it is uploaded.

Compiled 2026-10-12 from Shutterstock's contributor help center ("Known Image Restrictions",
page dated 2026-03-25); the editorial, releases, intellectual-property and PII sections were
added 2026-10-16 from the five articles listed under [Sources](#sources). The lists are
**non-exhaustive** and Shutterstock reviews case by case; re-check the sources below every few
months. The machine-readable copy is `src/shuto/data/restrictions.yaml` (version 2): keep both in
sync.

Note: this file is the first of shuto's three filter layers (Shutterstock rules, theme fit,
your own rules; see [details.md](details.md#theme-and-your-own-rules)). Only this layer
can mark a photo "Can't send as is". Theme fit and your own rules can set a photo aside, but you
can always approve it anyway.

## Outcomes

| Level | Commercial | Editorial | shuto action |
|---|---|---|---|
| `restricted` | rejected (unless property release) | generally OK | force `editorial=yes` |
| `prohibited` | rejected | rejected | do not upload; report |
| `no_upload` | not accepted (Shutterstock's own editorial team covers it) | not accepted | do not upload; report |

## General rules

**Trademarks** ([source](https://submit.shutterstock.com/help/en/articles/12136473-why-was-my-content-rejected-for-trademark-issues))
- Commercial content cannot contain visible trademarks: brand names and logos, shop signs and
  business names, logos on clothing, posters and ads, symbols (Red Cross), distinctive designs
  (three-stripe shoes). Fix: edit it out, or submit as editorial.
- Brand names, business names and trademarked terms must **not** appear in a commercial title or
  keywords; shuto strips them from commercial keywords.
- **Editorial** is different: the IP article says trademarks are generally acceptable in
  documentary editorial, in the image and in titles and keywords. shuto therefore keeps brand
  names in an editorial caption and keywords (owner decision 2026-10-16; the earlier "never" was
  stricter than Shutterstock). Items on the restricted lists below are always rejected.

**Architecture and private land** ([source](https://submit.shutterstock.com/help/en/articles/10617431-known-restrictions-architectural-designs-and-private-land))
- Restricted buildings, commercial: exterior rejected if it fills a large part of the frame
  without a property release; interiors always need a property release.
- Without a property release, a recognizable building can only go in as editorial.

**People**
- Recognizable people without a model release: editorial only.

**Titles and keywords** ([source](https://submit.shutterstock.com/help/en/articles/12136367-why-was-my-content-rejected-for-title-or-keywords))
- Commercial title: English, a complete descriptive sentence of at least 5 words.
- Keywords: no repeats, nothing irrelevant or incomplete.

**Editorial captions** ([how to submit editorial](https://submit.shutterstock.com/help/en/articles/10617417-how-do-i-submit-editorial-content), [contextual metadata](https://submit.shutterstock.com/help/en/articles/10617427-content-publishing-standards-contextual-metadata))
- shuto only makes *documentary* editorial (real photos of real places), so the date and place
  prefix always applies.
- Shutterstock's format: `CITY, STATE/COUNTRY - MONTH DAY, YEAR: Factual sentence that describes
  the content's subject matter.` The portal's own error text reads `City, State / Country - Month
  Day Year: Description`.
- shuto writes `CITY, PREFECTURE, COUNTRY - MONTH D, YYYY: Sentence.`, in English, for example
  `SHIBUYA, TOKYO, JAPAN - OCTOBER 5, 2026: Commuters wait on the platform at Shinjuku Station.`
  The prefecture or state slot is left out only when it is unknown (owner decision 2026-10-16).
- shuto builds the prefix itself from photo facts (GPS place, file location, then the AI's
  guess, then your default place; the capture date from EXIF). The AI writes only the sentence.
  Before the CSV is written shuto repairs small slips in a hand-edited caption (en dash, `July
  31 2026`, `31st`, lower case) and refuses a caption that still fails.
- Day, month and year taken, plus the location, are required. A wrong or missing year or place
  means rejection for "Metadata - Editorial Requirements". Before 2000 the year (or decade)
  alone is enough.
- The date must be a real calendar date, not in the future, and the same as the photo's capture
  date when shuto knows it. The city may repeat the country only for city-states (Singapore,
  Monaco).
- Events: put the event's name in the caption (and title), above all when it needs credentials.
- The description is English, at most 2048 characters, no emoji, no case numbers, no sensitive
  personal data. Approved editorial is labelled "Editorial Use Only" and cannot advertise or
  promote a product or service.
- Select the "Editorial" usage in the portal; shuto's CSV writes `yes` in the Editorial column
  (kept as `yes`/`no`, the values in Shutterstock's own sample file).

## Releases ([legal documentation](https://submit.shutterstock.com/help/en/articles/10617411-content-publishing-standards-legal-documentation))

- **Editorial: no releases.** Model releases are not required for recognizable people in
  editorial content and property releases are not required for editorial content. The portal
  says "Releases cannot be attached to Editorial assets". shuto never sends releases (the CSV and
  FTPS have no release field), so this happens in the portal: do not attach them to editorial
  photos. shuto warns when an editorial photo still has release ids saved.
- **Commercial needs a model release** for a recognizable person, a tattoo on an unrecognizable
  model, nude or suggestive models, and illustrations of real people.
- **Commercial needs a property release** for artwork (paintings, murals, graffiti, drawings,
  prints, tattoos, blueprints), sculptures, statues and monuments, stained glass, sheet music,
  literary works, lyrics, restricted exterior architecture, building interiors and private land.
- **Press credentials.** Editorial from these events needs press credentials and a case number
  from credentials@shutterstock.com *before* submitting: ticketed concerts in private venues,
  historical reenactments, political events, press conferences and panels, red carpets, runway
  shows, select airshows, sporting events (also school and amateur) and ticketed theatre. shuto
  cannot see credentials: when the model reports such an event, the photo goes to "Needs a look"
  with a reminder to name the event in the caption.

## Intellectual property by type ([IP article](https://submit.shutterstock.com/help/en/articles/10617446-content-publishing-standards-intellectual-property))

| Subject | Commercial | Documentary editorial |
|---|---|---|
| Trademarks, brand names, logos | rejected (image, title, keywords) | generally OK, also in titles and keywords; restricted-list trademarks always rejected |
| Artwork (paintings, murals, prints), statues, characters | needs a property release | OK when not the only subject |
| Artwork as the main subject (isolated or filling the frame) | needs a property release | rejected as Non-Licensable (photographic prints are the exception) |
| Temporary art (sand or ice sculpture, face paint, nail or food art) | OK | OK |
| Stained glass | needs a property release | OK |
| Sculptures, statues, monuments | needs a property release | OK unless on the restricted list |
| Postage stamps | - | only with a postmark |
| Currency | OK unless restricted | OK unless restricted |
| Exterior architecture | OK unless on the restricted list and large in the frame | OK unless prohibited |
| Building interiors, private land | needs a property release | OK |
| Fictional characters, media franchises | rejected (also names in title/keywords) | OK unless restricted |
| Content on screens (TV, film, games, apps) | rejected | OK unless it is the primary subject |
| Public figures (alive, or died after 1930) | rejected, names too | OK |

## Personal data (PII) ([PII article](https://submit.shutterstock.com/help/en/articles/10594677-content-publishing-standards-personally-identifiable-information-pii))

The article covers what is visible in the content and in the title and keywords, not file
metadata.
- Real-looking financial account numbers, card numbers and government ID numbers are rejected,
  **commercial and editorial**. shuto **blocks** a photo when the model reports one as readable
  ("Can't send as is"): crop or blur it, then send the photo back to the AI.
- Names, addresses, emails and handles, phone numbers and signatures: commercial needs
  placeholders and is rejected on official-looking documents; editorial is generally
  acceptable. shuto makes such a photo editorial and puts it in "Needs a look".
- Sensitive personal data never belongs in a title or keywords. shuto checks the description and
  keywords for an email address, a phone-like number and a card-like number and sends the photo
  to "Needs a look"; it logs the kind, never the value.
- GPS and EXIF are not mentioned by Shutterstock's PII or metadata articles. shuto strips the GPS
  location from uploaded copies by default (Settings, Privacy) as the owner's own privacy
  choice, which is stricter than Shutterstock asks. EXIF owner and serial-number fields are not
  stripped.

## What shuto does with the signals the model reports

The model fills `detected` for every photo; shuto acts on it deterministically. The same mapping
is `detected_actions` in `restrictions.yaml` (and the press-credential events are
`credential_events`).

| Signal | Commercial | Editorial | shuto action |
|---|---|---|---|
| `artworks_or_characters`, no property release | rejected | OK unless restricted | `force_editorial` |
| `artwork_is_main_subject` | needs a release | Non-Licensable | `review`: "Artwork fills the frame" |
| `private_interior`, no property release | needs a release | OK | `force_editorial` |
| `screen_content` | rejected | OK unless the primary subject | `force_editorial` |
| `event` that needs credentials (concert, sports, rally, press conference, panel, red carpet, runway, theatre, airshow, reenactment) | - | needs press credentials | `review`: "Looks like a ...: Shutterstock needs press credentials and a case number" |
| `event`, other (festival, parade, matsuri) | - | OK | soft note when the caption does not name the event |
| `personal_data`: ID, card or account number | rejected | rejected | `block` |
| `personal_data`: name, address, phone, email, signature | needs placeholders | OK | `review`, and editorial |
| email, phone or card number in the description or keywords | - | - | `review`: "Personal data in the text" |

## Japan and Asia

| Subject | Location | Level | Category |
|---|---|---|---|
| Tokyo Skytree | Sumida, Tokyo | restricted | architecture |
| Ice Village | Hokkaido | restricted | architecture |
| National Museum of Western Art (Le Corbusier) | Ueno, Tokyo | **prohibited** | architecture |
| Shinkansen (bullet train) | - | restricted | trade dress |
| Kikkoman bottles | - | restricted | trade dress |
| Tachikara volleyball designs | - | restricted | trade dress |
| Pokémon "Gotta Catch 'Em All" slogan | - | restricted | trademark |
| Olympic / Paralympic mascots, torches, awards | - | restricted | copyright / trade dress |
| Space Invaders characters | - | **prohibited** | copyright |
| Postcards (front and back) | - | **prohibited** | copyright |
| Lego minifigures, Rubik's Cube, Coca-Cola contour bottle, Crocs, Volkswagen Beetle / Camper Van | - | restricted | trade dress |
| Polaroid cameras and instant film border, Lindt chocolate bunnies | - | **prohibited** | trade dress |
| N Seoul Tower | Seoul | restricted | architecture |
| Taipei 101 | Taipei | restricted | architecture |
| Petronas Twin Towers | Kuala Lumpur | restricted | architecture |
| Canton Tower; Bird's Nest; NCPA | China | restricted | architecture |
| Burj Khalifa; Burj Al Arab; Louvre Abu Dhabi; Bahrain WTC | Middle East | restricted | architecture |
| Merlion; Bruce Lee statue (HK); Terracotta Army | Asia | restricted | artwork |
| Singapore Zoo (incl. Night Safari, River Safari, Jurong Bird Park) | Singapore | **prohibited** | private land |
| Singapore and Macau postage stamps | - | **prohibited** | artwork |
| Capitol Complex (Le Corbusier) | Chandigarh, India | **prohibited** | architecture |

**Tokyo Tower is not on the list** (as of 2026-03-25): commercial is possible when no logos or
signage are visible; as the hero of the frame, editorial is the safe choice.

## Everyday Japan: shuto's own rules

Not on Shutterstock's list, but they follow from the general trademark and people rules.
In street scenes they are the most common rejection causes:
- Konbini (7-Eleven, FamilyMart, Lawson), vending machine brands, drink and snack packaging,
  shop signs and noren with names: editorial, or crop them out.
- Train and railway company logos and liveries (JR, Tokyo Metro, private lines), station names on
  signs: editorial. The Shinkansen is restricted outright.
- Anime, manga and game characters, posters, billboards, ads: editorial at best. Prohibited if
  the character is on the prohibited list.
- Recognizable faces without a model release: editorial.
- Temple and shrine interiors, and grounds that forbid photography: check the site's rules.
  Never upload photos taken where photography is forbidden.
- Drone shots: Japan requires MLIT permits in urban areas.

## Prohibited elsewhere (global, both commercial and editorial)

Architecture: Arsenal Library, Bayreuth Eremitage, Biltmore Estate, Blenheim Palace, Busch Gardens,
Casa Batlló, Casa Milà, Castel Meur, Charles de Gaulle Airport, Château de Chenonceau,
Château de Vauvenargues, Vaux-le-Vicomte, Chatsworth House, Chillon Castle, City of Arts and Sciences
(Valencia), Cité du Vin, Cutty Sark, Disney resort and park buildings, Eiffel Tower **at night**,
FNB Stadium, François-Mitterrand Library, Graceland, Guggenheim Bilbao, Guggenheim NYC,
Herrenchiemsee, Île de Ré Bridge, Chaban-Delmas Bridge, Jean-Vilar Library, St-Pierre Avignon,
Notre Dame du Haut, Cité Radieuse, Grande Arche, Curutchet House, Maison Guiette, Villa Le Lac,
Villa Jeanneret-Perret, Villa Savoye, Centrosoyuz, Sainte Marie de la Tourette, Pavillon Le Corbusier,
Weissenhof houses, Linderhof, Louise Weiss Building, Louvre Museum, Lyon-Saint Exupéry Airport,
Margravial Opera House, Middleport Pottery, Millau Viaduct, Mont Saint-Michel jetty bridge,
Monticello, Moulin Rouge, Munich Residence, MuCEM, National Maritime Museum, National Trust
properties (England, Wales, NI), Neuschwanstein, Nuremberg Castle, Nymphenburg, Opéra Library-Museum,
Palace of Europe, Pompidou Center, Richelieu-Louvois Library, Rio Botanical Garden, Royal Observatory
Greenwich, Sacré-Cœur, Shrine of Remembrance, Sistine Chapel, Stanford University, Atomium,
Bellagio Centre, Queen's House, Unité d'Habitation (Firminy, Berlin), Walhalla, Wrigley Building,
Würzburg Residence.

Private land: Brompton Cemetery, Burning Man, Bushy Park, Englischer Garten, Greenwich Park,
Grosvenor Square, Hyde Park, Munich Hofgarten, Primrose Hill, Richmond Park, Sovereign Hill,
St James's Park, Surf Ranch, The Green Park, Top of the Rock, Victoria Tower Gardens.

Trademarks and products: Bounty, Chanel, Christian Louboutin (incl. red soles), Louis Vuitton,
NBA/WNBA/G League (logos, mascots, trophies), ICC Cricket World Cup, Little League,
Harvard Veritas shield, Pink Lady apples, Smileyworld smiley, World Surf League, TGV trains,
Le Corbusier furniture, NeoCube, UK Ordnance Survey maps.

Artwork: Astor Place Cube, Szukalski's Last Supper, Crazy Horse Memorial, Gzhel, Khokhloma,
Zhostovo designs, Minneapolis Sculpture Garden, Miroir d'eau (Bordeaux), 9/11 Memorial.

Events not accepted (covered by Shutterstock's editorial team): Oscars, Golden Globes, Grammys,
Emmys, Tonys, SAG Awards, Cannes, Venice, Toronto, Sundance, NY/London/Milan/Paris Fashion Week,
US Open (tennis), Wimbledon, Met Gala, FIFA World Cup, and major US music awards. Airshow teams
(Blue Angels, Thunderbirds, Red Arrows, Snowbirds) need press credentials.

The full restricted (editorial-only) lists for other regions are on the source pages:
[architecture](https://submit.shutterstock.com/help/en/articles/10617431-known-restrictions-architectural-designs-and-private-land),
[trademarks and IP](https://submit.shutterstock.com/help/en/articles/10617432-known-restrictions-trademarks-and-other-intellectual-property),
[artwork](https://submit.shutterstock.com/help/en/articles/10617418-known-restrictions-artwork),
[events](https://submit.shutterstock.com/help/en/articles/10617434-known-restrictions-events).

## Sources

Fetched 2026-10-16:
- [How do I submit editorial content?](https://submit.shutterstock.com/help/en/articles/10617417-how-do-i-submit-editorial-content) (Feb 18, 2026)
- [Content publishing standards: contextual metadata](https://submit.shutterstock.com/help/en/articles/10617427-content-publishing-standards-contextual-metadata) (Feb 26, 2026)
- [Content publishing standards: legal documentation](https://submit.shutterstock.com/help/en/articles/10617411-content-publishing-standards-legal-documentation)
- [Content publishing standards: intellectual property](https://submit.shutterstock.com/help/en/articles/10617446-content-publishing-standards-intellectual-property)
- [Content publishing standards: personally identifiable information](https://submit.shutterstock.com/help/en/articles/10594677-content-publishing-standards-personally-identifiable-information-pii)
- Known image restrictions (2026-03-25): the four "known restrictions" pages linked above.
