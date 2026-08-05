# anizone-scraper

Anime streaming scraper for [anizone.to](https://anizone.to) — resolves real,
playable `m3u8`/`mp4` streams + the required `Referer` + subtitles for ExoPlayer.

> **📚 Scraping method playbook:** this repo also documents the general A→Z
> techniques for scraping anime streams. See [`docs/`](docs/):
> - [`docs/METHODS.md`](docs/METHODS.md) — 8 site-agnostic methods (M1–M8)
> - [`docs/sites/`](docs/sites/) — per-site writeups (anizone, anikage, just4anime, otakuhg, animegg, megaplay, vivibebe)
> - [`docs/just4anime_PROOF.md`](docs/just4anime_PROOF.md) — live-verified proof all 6 just4anime servers work
> - [`docs/templates/`](docs/templates/) — reusable code (node packer decoder, HLS verify, Flask API)
> - [`docs/pitfalls.md`](docs/pitfalls.md) — the mistakes that cost the most time
> - [`docs/methods.json`](docs/methods.json) — machine-readable index

## How it works
The scraper mirrors the anikage/just4anime Flask contract
(`/api/anime/<id>/servers`, `/api/anime/<id>/stream`, `/api/proxy`) so the
AniwaveStream app can consume it with no app changes. Subtitles are absolutized
against the backend BASE and use 2-letter lang codes (see `docs/METHODS.md` M7).
