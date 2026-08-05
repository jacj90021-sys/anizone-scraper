# Site: anizone.to (anizone-scraper)

Aggregator used by the AniwaveStream app as its **subtitle source** (the app's
`AnizoneApi` is the one that correctly shows subs, unlike anikage's broken relative-
URL path — see `METHODS.md` M7).

## Method notes
- Mirrors the anikage/just4anime Flask contract (M6) so the app consumes it unchanged.
- **Subtitle absolutization is the key win** (M7): anizone returns absolute subtitle
  URLs + 2-letter lang (`en`). The app routes anizone subtitles through
  `buildAnikageMediaSource` with the referer attached — subs show.
- This is the reference implementation of M7 done RIGHT. Copy it when wiring a new
  site's subtitles into ExoPlayer.

## Why it matters in the playbook
anizone is the proof that M7 (absolute URL + 2-letter lang + same-origin proxy) is
what makes subtitles actually appear. anikage's original bug (relative `/api/proxy`
→ host-less Uri → silent no-subs) is the anti-pattern to avoid.

## Repo
`github.com/jacj90021-sys/anizone-scraper`
