"""Minimal anizone.to scraping API — anidb-shaped, bandwidth-free.

anizone.to serves BARE DIRECT HLS m3u8 urls (vid-cdn.xyz / xin-cdn.xyz) with
NO token, NO Cloudflare Worker proxy, NO encryption. This backend only SCRAPES
(title -> slug -> episode page -> hand back the direct m3u8 URL as a few-KB JSON).
The actual video bytes go device -> CDN directly, so this service costs ZERO
Render video bandwidth (same model as anidb-scraper).

Endpoints (mirror anidb-scraper so the Android client can reuse the anidb pattern):
    GET /api/health                     -> {"ok": true}
    GET /api/search?q=<title>           -> {"results":[{"id":<slug>,"name":<title>}]}
    GET /api/sources?slug=<slug>&ep=<N> -> {"servers":[{"server":"default","name":"Anizone","m3u8":<direct>,"subtitles":[...]}]}

anizone.to has NO /search route, so we build a full slug->[aliases] index by
crawling /anime?page=N (all pages). The crawl is HEAVY (~2000 pages), so it runs
ONCE in a background thread at startup and is cached. /api/search reads the cache
instantly. Until the first crawl finishes, search returns whatever is loaded so far.
"""
import re
import time
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify

from curl_cffi import requests as cffi
from bs4 import BeautifulSoup

BASE = "https://anizone.to"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

app = Flask(__name__)

_session = None
_index_lock = threading.Lock()
_index = {}            # slug -> [alias, ...]
_index_built = False


def session():
    global _session
    if _session is None:
        s = cffi.Session()
        s.headers.update({
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        s.impersonate = "chrome"
        _session = s
    return _session


def _get(url, retries=3):
    last = None
    for i in range(retries):
        try:
            r = session().get(url, timeout=20)
            if r.status_code == 200 and "Just a moment" not in r.text[:2000]:
                return r.text
        except Exception as e:
            last = e
        time.sleep(1.0 * (i + 1))
    if last:
        raise last
    raise RuntimeError(f"fetch failed {url}")

def _titles_from(html):
    """Extract every anmTitles alias blob from a page of HTML."""
    out = []
    for m in re.finditer(r"anmTitles:\s*JSON\.parse\('(.*?)'\)", html, re.S):
        try:
            raw = m.group(1).encode().decode("unicode_escape")
            d = json.loads(raw)
        except Exception:
            try:
                d = json.loads(m.group(1))
            except Exception:
                continue
        for v in d.values():
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
    return out


def _crawl_page(page):
    url = f"{BASE}/anime?page={page}" if page > 1 else f"{BASE}/anime"
    try:
        html = _get(url)
    except Exception:
        return {}
    soup = BeautifulSoup(html, "html.parser")
    page_map = {}
    for d in soup.select('div[x-data*="anmTitles"]'):
        a = d.find("a", href=re.compile(r"/anime/"))
        if not a:
            continue
        slug = a["href"].split("/anime/")[-1].strip("/")
        m = re.search(r"anmTitles:\s*JSON\.parse\('(.*?)'\)", str(d), re.S)
        titles = []
        if m:
            try:
                blob = m.group(1).encode().decode("unicode_escape")
                data = json.loads(blob)
            except Exception:
                try:
                    data = json.loads(m.group(1))
                except Exception:
                    data = {}
            titles = [v for v in data.values() if isinstance(v, str) and v.strip()]
        if slug:
            page_map.setdefault(slug, [])
            for t in titles:
                if t not in page_map[slug]:
                    page_map[slug].append(t)
    return page_map


def _load_index():
    """Crawl ALL anime listing pages concurrently and merge into _index."""
    global _index, _index_built
    merged = {}
    # discover total pages by probing; anizone caps listing, stop when a page is short
    with ThreadPoolExecutor(max_workers=8) as ex:
        # probe pages 1..200; break on first short/empty page
        futures = {}
        next_page = 1
        # simple sequential page-count discovery but parallel fetch of batches
        PAGE_CAP = 220
        for page in range(1, PAGE_CAP + 1):
            futures[page] = ex.submit(_crawl_page, page)
        for page in range(1, PAGE_CAP + 1):
            try:
                pm = futures[page].result()
            except Exception:
                pm = {}
            if not pm:
                break
            for slug, titles in pm.items():
                merged.setdefault(slug, [])
                for t in titles:
                    if t not in merged[slug]:
                        merged[slug].append(t)
            if len(pm) < 24:
                break
    with _index_lock:
        _index = merged
        _index_built = True


def _ensure_index():
    with _index_lock:
        built = _index_built
        snap = dict(_index)
    if built:
        return snap
    # not built yet -> trigger a (best-effort) sync crawl so first call still works
    # (usually the background thread already did this)
    try:
        _load_index()
    except Exception:
        pass
    with _index_lock:
        return dict(_index)


def resolve_slug(title):
    idx = _ensure_index()
    q = re.sub(r"[^a-z0-9]", "", title.lower())
    if not q:
        return None
    exact = None
    contains = None
    for slug, aliases in idx.items():
        for alias in aliases:
            a = re.sub(r"[^a-z0-9]", "", alias.lower())
            if not a:
                continue
            if a == q:
                exact = slug
                break
            if q in a or a in q:
                contains = slug
        if exact:
            break
    return exact or contains


def parse_episode(html):
    soup = BeautifulSoup(html, "html.parser")
    m3u8 = ""
    player = soup.find("media-player")
    if player and player.get("src"):
        m3u8 = player["src"]
    subs = []
    for tr in soup.find_all("track"):
        if tr.get("kind") == "subtitles":
            subs.append({
                "label": tr.get("label", ""),
                "lang": tr.get("srclang", ""),
                "url": tr.get("src", ""),
            })
    return m3u8, subs


# --------------------------------------------------------------------------- #
@app.route("/api/health")
def health():
    with _index_lock:
        n = len(_index)
        built = _index_built
    return jsonify({"ok": True, "index": n, "built": built})


@app.route("/api/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    slug = resolve_slug(q)
    if not slug:
        return jsonify({"results": []})
    return jsonify({"results": [{"id": slug, "name": q}]})


@app.route("/api/sources")
def sources():
    slug = request.args.get("slug", "").strip()
    ep = request.args.get("ep", "1").strip()
    if not slug:
        return jsonify({"servers": []}), 400
    try:
        html = _get(f"{BASE}/anime/{slug}/{ep}")
    except Exception as e:
        return jsonify({"error": str(e), "servers": []}), 502
    m3u8, subs = parse_episode(html)
    if not m3u8:
        return jsonify({"servers": []})
    return jsonify({
        "servers": [{
            "server": "default",
            "name": "Anizone",
            "m3u8": m3u8,
            "subtitles": subs,
        }]
    })


# Kick off the heavy crawl in the background at startup so /api/search is instant.
_thread = threading.Thread(target=_load_index, daemon=True)
_thread.start()
