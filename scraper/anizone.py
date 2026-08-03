"""Scraper for anizone.to.

The site is a Laravel (Livewire) app behind Cloudflare, so plain ``requests``
gets a 403 challenge. ``curl_cffi`` with browser impersonation bypasses it.
Pages are plain server-rendered HTML, so BeautifulSoup is enough.

Public pages:
    /                -> latest anime, latest episodes, top tags
    /anime?page=N    -> paginated anime index (24 per page)
    /anime/{slug}    -> anime detail (metadata + episode list)
    /anime/{slug}/{ep} -> episode page (HLS stream, subtitles, ...)
    /episode?page=N  -> paginated episode index
    /tag/{slug}      -> anime in a tag
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

BASE_URL = "https://anizone.to"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
PAGE_SIZE = 24

_LANG_PRIORITY = ["5", "1", "2", "8", "7", "9"]


def session(impersonate: str = "chrome") -> cffi_requests.Session:
    s = cffi_requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    s.impersonate = impersonate
    return s


def _get(s, url: str, retries: int = 3, delay: float = 1.0) -> str:
    last = None
    for attempt in range(retries):
        r = s.get(url, timeout=30)
        if r.status_code == 200 and "Just a moment" not in r.text[:2000]:
            return r.text
        last = f"status={r.status_code}"
        time.sleep(delay * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last}")


def _json_in_html(html: str, attr: str = "anmTitles") -> dict[str, str]:
    """Extract a title-map JSON object from an Alpine x-data attribute."""
    m = re.search(attr + r":\s*JSON\.parse\('(.*?)'\)", html, re.S)
    if not m:
        return {}
    raw = m.group(1)
    try:
        return json.loads(raw.encode().decode("unicode_escape"))
    except Exception:
        try:
            return json.loads(raw)
        except Exception:
            return {}


def _pick_title(titles: dict[str, str], fallback: str = "") -> str:
    for key in _LANG_PRIORITY:
        if titles.get(key):
            return titles[key]
    for v in titles.values():
        if v:
            return v
    return fallback


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- #
# Index / homepage
# --------------------------------------------------------------------------- #


@dataclass
class AnimeSummary:
    slug: str
    url: str
    titles: dict[str, str] = field(default_factory=dict)
    title: str = ""
    image: str = ""
    type: str = ""
    year: str = ""
    episodes_count: str = ""
    status: str = ""
    tags: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "url": self.url,
            "title": self.title or _pick_title(self.titles),
            "titles": self.titles,
            "image": self.image,
            "type": self.type,
            "year": self.year,
            "episodes_count": self.episodes_count,
            "status": self.status,
            "tags": self.tags,
        }


def _parse_anime_summary_div(div) -> AnimeSummary | None:
    a = div.find("a", href=re.compile(r"/anime/"))
    if not a:
        return None
    slug = a["href"].split("/anime/")[-1].strip("/")
    img = div.find("img")
    tags = []
    for t in div.select('a[href*="/tag/"]'):
        m = re.search(r"/tag/([^/]+)", t["href"])
        tags.append({"name": t.get_text(strip=True), "slug": m.group(1), "url": t["href"]})

    meta = div.get_text("|", strip=True)
    bits = [b for b in (x.strip() for x in meta.split("|")) if b]
    type_ = year = eps = status = ""
    for b in bits:
        if b in {"TV Series", "OVA", "Movie", "Other", "Web", "TV Special", "Music Video", "Unknown"}:
            type_ = b
        elif re.fullmatch(r"(19|20)\d{2}", b):
            year = b
        elif re.fullmatch(r"\d+ Eps?", b):
            eps = b
        elif b in {"Ongoing", "Completed", "Upcoming", "Cancelled", "Publishing"}:
            status = b
        elif re.fullmatch(r"Release.*|Pub.*", b):
            continue

    titles = {}
    script = div.find("script")
    return AnimeSummary(
        slug=slug,
        url=f"{BASE_URL}/anime/{slug}",
        titles=titles,
        image=img["src"] if img and img.get("src") else "",
        type=type_,
        year=year,
        episodes_count=eps,
        status=status,
        tags=tags,
    )


def parse_anime_index(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for div in soup.select('div[x-data*="anmTitles"]'):
        item = _parse_anime_summary_div(div)
        if item is None:
            continue
        d = item.to_dict()
        d["titles"] = _json_in_html(str(div), "anmTitles")
        d["title"] = _pick_title(d["titles"], item.title)
        d["tags"] = item.tags
        out.append(d)
    return out


def parse_homepage(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    latest_anime = []
    for a in soup.select('main a[href*="/anime/"][href$=""]'):
        href = a["href"]
        if not re.fullmatch(rf"{re.escape(BASE_URL)}/anime/\w+", href):
            continue
        img = a.find("img")
        latest_anime.append(
            {
                "slug": href.split("/anime/")[-1],
                "url": href,
                "image": img["src"] if img and img.get("src") else "",
            }
        )

    latest_episodes = []
    for a in soup.select('main a[href*="/anime/"][href*="/"]'):
        m = re.fullmatch(rf"{re.escape(BASE_URL)}/anime/(\w+)/([^/]+)", a["href"])
        if not m:
            continue
        img = a.find("img")
        latest_episodes.append(
            {
                "slug": m.group(1),
                "episode": m.group(2),
                "url": a["href"],
                "image": img["src"] if img and img.get("src") else "",
            }
        )

    tags = []
    for a in soup.select('main a[href*="/tag/"]'):
        m = re.search(r"/tag/([^/]+)", a["href"])
        if not m or any(t["slug"] == m.group(1) for t in tags):
            continue
        img = a.find("img")
        text = a.get_text(" ", strip=True)
        name = re.sub(r"\s*\(\d+\)\s*$", "", text).strip()
        count = None
        c = re.search(r"\((\d+)\)", text)
        if c:
            count = int(c.group(1))
        tags.append(
            {
                "name": name,
                "slug": m.group(1),
                "url": a["href"],
                "count": count,
                "image": img["src"] if img and img.get("src") else "",
            }
        )

    return {
        "latest_anime": latest_anime,
        "latest_episodes": latest_episodes,
        "top_tags": tags,
    }


def scrape_homepage(s=None) -> dict[str, Any]:
    s = s or session()
    return parse_homepage(_get(s, BASE_URL))


def scrape_anime_index(max_pages: int = 1, s=None) -> list[dict[str, Any]]:
    s = s or session()
    out: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        url = f"{BASE_URL}/anime?page={page}" if page > 1 else f"{BASE_URL}/anime"
        items = parse_anime_index(_get(s, url))
        out.extend(items)
        if len(items) < PAGE_SIZE:
            break
        time.sleep(0.5)
    return out


# --------------------------------------------------------------------------- #
# Anime detail
# --------------------------------------------------------------------------- #


def parse_anime_detail(html: str, slug: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    titles = _json_in_html(html, "anmTitles")

    image = ""
    img = soup.find("img", src=re.compile(r"/images/anime/"))
    if img and img.get("src"):
        image = img["src"]

    meta: dict[str, str] = {}
    for span in soup.select("span.inline-block"):
        text = span.get_text(strip=True)
        if not text:
            continue
        if text in {"TV Series", "OVA", "Movie", "Other", "Web", "TV Special", "Music Video", "Unknown"}:
            meta["type"] = text
        elif text in {"Ongoing", "Completed", "Upcoming", "Cancelled", "Publishing"}:
            meta["status"] = text
        elif re.fullmatch(r"(19|20)\d{2}", text):
            meta["year"] = text
        elif re.fullmatch(r"\d+ Episodes?", text):
            meta["episodes_count"] = text
    meta.pop("year", None)
    year_span = soup.find("span", string=re.compile(r"^(19|20)\d{2}$"))
    if year_span:
        meta["year"] = year_span.get_text(strip=True)

    syn = soup.select_one("h3.sr-only, h3")
    synopsis = ""
    if syn and syn.get_text(strip=True) == "Synopsis":
        nxt = syn.find_next_sibling()
        if nxt:
            synopsis = _clean(nxt.get_text(" ", strip=True))

    tags = []
    for a in soup.select('a[href*="/tag/"]'):
        m = re.search(r"/tag/([^/]+)", a["href"])
        name = a.get_text(strip=True)
        if m and name and not any(t["slug"] == m.group(1) for t in tags):
            tags.append({"name": name, "slug": m.group(1), "url": a["href"]})

    official_site = ""
    site_a = soup.find("a", href=True, rel="nofollow noopener noreferrer")
    if site_a:
        official_site = site_a["href"]

    episodes = []
    for li in soup.select("li"):
        a = li.find("a", href=True)
        if not a:
            continue
        m = re.fullmatch(rf"{re.escape(BASE_URL)}/anime/{re.escape(slug)}/([^/]+)", a["href"])
        if not m:
            continue
        number = m.group(1)
        bits = [b for b in (x.strip() for x in li.get_text("|", strip=True).split("|")) if b]
        ep_type = ""
        air_date = ""
        for b in bits:
            if b in {
                "Regular Episode",
                "Special",
                "Opening/Ending",
                "Trailer/Promo/Ads",
                "Parody/Fandub",
                "Other",
            }:
                ep_type = b
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", b):
                air_date = b
        snapshot = ""
        img = li.find("img")
        if img and img.get("src") and "/snapshot.webp" in img["src"]:
            snapshot = img["src"]
        episodes.append(
            {
                "number": number,
                "url": a["href"],
                "type": ep_type,
                "air_date": air_date,
                "snapshot": snapshot,
            }
        )

    return {
        "slug": slug,
        "url": f"{BASE_URL}/anime/{slug}",
        "title": _pick_title(titles),
        "titles": titles,
        "image": image,
        "official_site": official_site,
        "synopsis": synopsis,
        "type": meta.get("type", ""),
        "status": meta.get("status", ""),
        "year": meta.get("year", ""),
        "episodes_count": meta.get("episodes_count", ""),
        "tags": tags,
        "episodes": episodes,
    }


# --------------------------------------------------------------------------- #
# Episode page (stream)
# --------------------------------------------------------------------------- #


def parse_episode_page(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    stream_url = ""
    player = soup.find("media-player")
    if player and player.get("src"):
        stream_url = player["src"]

    poster = ""
    poster_el = soup.find("media-poster")
    if poster_el and poster_el.get("src"):
        poster = poster_el["src"]

    storyboard = ""
    layout = soup.find("media-video-layout")
    if layout and layout.get("thumbnails"):
        storyboard = layout["thumbnails"]

    chapters = ""
    for tr in soup.find_all("track"):
        if tr.get("kind") == "chapters":
            chapters = tr.get("src", "")

    subtitles = []
    for tr in soup.find_all("track"):
        if tr.get("kind") == "subtitles":
            subtitles.append(
                {
                    "label": tr.get("label", ""),
                    "lang": tr.get("srclang", ""),
                    "url": tr.get("src", ""),
                }
            )

    episode_title = ""
    for h1 in soup.find_all("h1"):
        txt = _clean(h1.get_text(" ", strip=True))
        if txt:
            episode_title = txt
    m = re.search(r"-\s*(Episode\s+\S+)\s*$", episode_title)
    if m:
        episode_title = m.group(1)

    meta: dict[str, str] = {}
    for span in soup.select("span.inline-block"):
        text = span.get_text(strip=True)
        if text in {
            "Regular Episode",
            "Special",
            "Opening/Ending",
            "Trailer/Promo/Ads",
            "Parody/Fandub",
            "Other",
        }:
            meta["type"] = text
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            meta["air_date"] = text

    return {
        "title": episode_title,
        "type": meta.get("type", ""),
        "air_date": meta.get("air_date", ""),
        "stream_url": stream_url,
        "poster": poster,
        "snapshot": poster,
        "storyboard": storyboard,
        "chapters": chapters,
        "subtitles": subtitles,
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def scrape_anime(
    slug: str,
    s=None,
    fetch_episodes: bool = True,
    episode_limit: Optional[int] = None,
    delay: float = 0.3,
) -> dict[str, Any]:
    """Scrape one anime: metadata + optional per-episode stream info."""
    s = s or session()
    detail = parse_anime_detail(_get(s, f"{BASE_URL}/anime/{slug}"), slug)

    if fetch_episodes and detail["episodes"]:
        detail["episodes"] = detail["episodes"][:episode_limit] if episode_limit else detail["episodes"]
        for ep in detail["episodes"]:
            try:
                stream = parse_episode_page(_get(s, ep["url"]))
            except Exception as e:  # keep going if one episode fails
                stream = {"error": str(e)}
            ep.update(stream)
            time.sleep(delay)

    return detail


def run(
    *,
    max_pages: int = 1,
    anime_limit: Optional[int] = None,
    fetch_episodes: bool = True,
    episode_limit: Optional[int] = None,
    delay: float = 0.3,
) -> dict[str, Any]:
    """Scrape the anime index (``max_pages`` pages) and each anime fully."""
    s = session()
    index = scrape_anime_index(max_pages=max_pages, s=s)
    if anime_limit:
        index = index[:anime_limit]

    anime = []
    for i, entry in enumerate(index, 1):
        print(f"[{i}/{len(index)}] {entry['title']}")
        anime.append(scrape_anime(entry["slug"], s=s, fetch_episodes=fetch_episodes, episode_limit=episode_limit, delay=delay))

    return {"scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "anime": anime}


def main(argv=None) -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(description="Scrape anizone.to")
    p.add_argument("--pages", type=int, default=1, help="anime index pages to scan (24 per page)")
    p.add_argument("--anime", type=int, default=None, help="max anime to scrape from the index")
    p.add_argument("--slug", action="append", help="scrape a specific slug instead of the index (repeatable)")
    p.add_argument("--no-episodes", action="store_true", help="skip fetching episode stream pages")
    p.add_argument("--episode-limit", type=int, default=None, help="max episode pages per anime")
    p.add_argument("--delay", type=float, default=0.3, help="seconds between requests")
    p.add_argument("--out", default="anizone_catalog.json", help="output JSON path")
    args = p.parse_args(argv)

    s = session()
    if args.slug:
        data = {
            "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "anime": [
                scrape_anime(
                    sl,
                    s=s,
                    fetch_episodes=not args.no_episodes,
                    episode_limit=args.episode_limit,
                    delay=args.delay,
                )
                for sl in args.slug
            ],
        }
    else:
        data = run(
            max_pages=args.pages,
            anime_limit=args.anime,
            fetch_episodes=not args.no_episodes,
            episode_limit=args.episode_limit,
            delay=args.delay,
        )

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Wrote {args.out} ({len(data['anime'])} anime)")


if __name__ == "__main__":
    main()
