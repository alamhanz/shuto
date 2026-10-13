# Shutterstock restrictions (guardrails)

Source of truth for shuto's guardrails. The pipeline should check every photo against these
rules (in the prompt and in a deterministic post-check) before it is uploaded.

Compiled 2026-10-12 from Shutterstock's contributor help center ("Known Image Restrictions",
page dated 2026-03-25). The lists are **non-exhaustive** and Shutterstock reviews case by case;
re-check the sources below every few months.

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
- **Never** put brand names, business names or trademarked terms in titles or keywords,
  commercial or editorial.

**Architecture and private land** ([source](https://submit.shutterstock.com/help/en/articles/10617431-known-restrictions-architectural-designs-and-private-land))
- Restricted buildings, commercial: exterior rejected if it fills a large part of the frame
  without a property release; interiors always need a property release.
- Without a property release, a recognizable building can only go in as editorial.

**People**
- Recognizable people without a model release: editorial only.

**Titles and keywords** ([source](https://submit.shutterstock.com/help/en/articles/12136367-why-was-my-content-rejected-for-title-or-keywords))
- Commercial title: English, a complete descriptive sentence of at least 5 words.
- Keywords: no repeats, nothing irrelevant or incomplete.

**Editorial captions** ([source](https://submit.shutterstock.com/help/en/articles/10617417-how-do-i-submit-editorial-content), [metadata standards](https://submit.shutterstock.com/help/en/articles/10617427-content-publishing-standards-contextual-metadata))
- Format: `CITY, STATE/COUNTRY - MONTH DAY, YEAR: Factual sentence describing the subject.`
  e.g. `TOKYO, JAPAN - OCTOBER 5, 2026: Commuters wait on the platform at Shinjuku Station.`
- Date (day, month, year) and location are required; missing or wrong ones cause rejection.
- Name the event in the caption if it needed credentials.

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
