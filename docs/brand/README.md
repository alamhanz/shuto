# shuto brand

Mark: a hanko (seal) - a white square stamp with a stylised "shu" glyph on a rounded navy tile.

| File | Use |
|---|---|
| `icon.svg` | Square app icon (128x128), favicon, avatars. |
| `logo-light.svg` | Icon + wordmark for light backgrounds (README, docs). |
| `logo-dark.svg` | Icon + wordmark for dark backgrounds. |

Colours: navy `#284B63` (primary mark), teal `#3C6E71` (accent), charcoal `#353535` (wordmark on light), white `#FFFFFF`, grey `#D9D9D9`.

README usage:

```html
<picture><source media="(prefers-color-scheme: dark)" srcset="docs/brand/logo-dark.svg"><img src="docs/brand/logo-light.svg" alt="shuto" width="264"></picture>
```

The wordmark uses `<text>` (Inter, falls back to system sans). Keep the mark at 16px or larger.

Derived copies: `src/shuto/web/static/logo.svg` (same as `icon.svg`), `src/shuto/data/shuto.png` (256) and `shuto.ico` (16/32/48/256).
