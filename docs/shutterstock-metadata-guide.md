# Shutterstock Metadata Guide

> This is the project owner's own guide. shuto's AI prompts and pre-export checks follow it.

Oct 13, 2026 · @alamhanz

Write a factual English sentence of roughly 70–150 characters, and 25–45 relevant keywords. Shutterstock's hard limits (2048 characters, 50 keywords) are far looser than what performs well.

## Hard rules (official Shutterstock)

Shutterstock has no separate title field. The Description column is both title and description.

| Field | Rule |
| --- | --- |
| Description | Factual English sentence, at least 5 words, max 2048 characters |
| Description | Accurately covers what is in the image; no special characters, emojis, spelling errors or spam |
| Description | Not a comma-separated word list (rejected as a keyword list) |
| Description (editorial) | Exactly `CITY, STATE/COUNTRY - MONTH DAY, YEAR: Factual sentence that describes the content's subject matter.` The day, month and year taken and the geographic location are required (before 2000 only the year or decade); a wrong or missing one is rejected for "Metadata - Editorial Requirements". shuto writes `CITY, PREFECTURE, COUNTRY - MONTH D, YYYY: Sentence.` |
| Description (editorial) | Name the event in the caption (and title) when the photo is from an event, above all one that needs credentials |
| Editorial | No releases: model and property releases are not required for editorial and cannot be attached to editorial assets |
| Title, description, keywords | No sensitive personal data (ID, card or account numbers, phone numbers, emails, case numbers); commercial titles and keywords also no trademarks, while documentary editorial may name brands |
| Keywords | Minimum 7, maximum 50, all relevant, in English |
| Keywords | No repeated words or phrases; no irrelevant terms |
| Keywords | Avoid restricted combinations (e.g. "teen" with "sexy"); Shutterstock audits for spam |
| Categories | 1 or 2 per file, from the official list |

## Best practices

**Description style.** Write it like a news headline: who, what, when, where, why, plus the mood. Shutterstock's own example is short and plain: "Aerial view of Paris at sunset."

**Description length.** Shutterstock's examples run 6–12 words. A healthy range is 8–20 words (60–200 characters); aim for about 70–150. Long descriptions tend to read as keyword stuffing.

**Uniqueness.** Never reuse the same title or keyword set across a series of similar images. A weekly batch pipeline will drift toward near-identical output unless this is checked.

**Keyword count.** Don't pad to 50. Weak filler keywords have triggered Shutterstock's spam filter for some contributors. Target 25–45 strong keywords, mixing:

- Literal subject (what is in the image)
- Setting and location
- Composition terms (aerial view, close-up, copy space, no people)
- Mood and concept (calm, freedom, travel)
- Multi-word long-tail phrases, which tend to beat generic single words

**Keyword order.** Sources disagree on whether order affects ranking. Sorting by relevance costs nothing, so put the most important keywords first.

## For the Shuto pipeline

**CSV upload.** Required columns: Filename, Description, Keywords, Categories. Optional: Illustration, Mature Content, Editorial (Yes or No only). Column names must match exactly, and accented characters in filenames can break matching.

> Note: shuto takes the CSV header from Shutterstock's own sample CSV.

**Lambda validation before export:**

> Note: where this guide says "Lambda", shuto means its own pre-export checks.


- [ ] Description is English, at least 5 words, 200 characters or fewer
- [ ] Description is a sentence, not a comma list, with no repeated words
- [ ] Editorial caption passes the format check: upper-case `CITY, PREFECTURE, COUNTRY`, a real `MONTH D, YYYY` date that matches the capture date, no UNKNOWN part, a sentence of 3 or more words
- [ ] No email, phone or card number in the description or keywords
- [ ] 7–50 keywords, deduplicated including singular/plural twins
- [ ] 1–2 categories from the official list
- [ ] Location filled in when known (Japan shots are a niche-search strength)

**AI policy.** Shutterstock doesn't accept AI-generated images. Real photos with AI-written metadata are fine. Instruct the model to describe only what is visible, so it never invents details.

## Sources

- [Shutterstock: Content Publishing Standards – Contextual Metadata](https://submit.shutterstock.com/help/en/articles/10617427-content-publishing-standards-contextual-metadata)
- [Shutterstock: Description and Keyword Best Practices](https://submit.shutterstock.com/help/en/articles/10594702-description-and-keyword-best-practices)
- [Shutterstock: Why was my content rejected for title or keywords](https://submit.shutterstock.com/help/en/articles/12136367-why-was-my-content-rejected-for-title-or-keywords)
- [Shutterstock: How can I maximize my visibility](https://submit.shutterstock.com/help/en/articles/10617438-how-can-i-maximize-my-visibility)
- [Shutterstock: Include existing metadata via CSV](https://submit.shutterstock.com/help/en/articles/10617486-how-do-i-include-existing-metadata-with-my-content-submission)
- [Photokeyworder: Complete Shutterstock Contributor Guide](https://blog.photokeyworder.ai/?p=185)
- [AutoKeyWorder: Shutterstock keyword tool](https://autokeyworder.com/blog/shutterstock-keyword-tool)
- [Metadata Reactor: AI metadata for stock photos](https://metadatareactor.com/blog/ai-metadata-for-stock-photos/)
- [Metadata Reactor: Shutterstock vs Adobe Stock keywords](https://metadatareactor.com/blog/shutterstock-vs-adobe-stock-keywords/)
