# Bundled fonts

These fonts are vendored locally so the signage appliance can render its
control panel with no internet access. They are loaded by `fonts.css`, which
the app serves from `/static/fonts/`.

All three families are licensed under the **SIL Open Font License, Version 1.1**
(full text in [`OFL.txt`](./OFL.txt)), which permits bundling and redistribution.

## Families and weights

| Family (Google Fonts name) | Weight | File |
| --- | --- | --- |
| Bricolage Grotesque | 600 | `bricolage-grotesque-600.woff2` |
| Bricolage Grotesque | 700 | `bricolage-grotesque-700.woff2` |
| Hanken Grotesk | 400 | `hanken-grotesk-400.woff2` |
| Hanken Grotesk | 500 | `hanken-grotesk-500.woff2` |
| Hanken Grotesk | 600 | `hanken-grotesk-600.woff2` |
| Space Mono | 400 | `space-mono-400.woff2` |

## Source

Downloaded from the Google Fonts CDN (the `latin` subset, WOFF2 format):

- Bricolage Grotesque — <https://fonts.google.com/specimen/Bricolage+Grotesque>
- Hanken Grotesk — <https://fonts.google.com/specimen/Hanken+Grotesk>
- Space Mono — <https://fonts.google.com/specimen/Space+Mono>

Bricolage Grotesque and Hanken Grotesk are variable fonts upstream; Google
serves a single `latin` WOFF2 per family that covers the requested weights, so
the per-weight files for a given family are byte-identical. Each weight still
gets its own filename and `@font-face` rule for clarity and stability.
