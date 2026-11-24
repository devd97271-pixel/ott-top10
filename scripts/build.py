#!/usr/bin/env python3
"""Builds static Stremio/Nuvio catalogs into ./docs for GitHub Pages.

Rows: cinema (now showing / upcoming) + per platform: new releases and top 10.
Top 10 sources: Netflix Tudum -> FlixPatrol -> JustWatch -> (Gemini) -> TMDB.
New releases: JustWatch -> TMDB. Cinema: BookMyShow -> TMDB.
"""
import colorsys
import csv
import datetime as dt
import html as htmllib
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
TMDB_KEY = os.environ.get("TMDB_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
OMDB_KEY = os.environ.get("OMDB_API_KEY", "")
BASE = os.environ.get("BASE_URL", "").rstrip("/")

COUNTRY = CFG.get("country", "IN").upper()
CNAME = CFG.get("country_name", "India")
PFX = COUNTRY.lower()

OUT = ROOT / "docs"
POSTERS = OUT / "posters"
CACHE_FILE = ROOT / "data" / "cache.json"
TUDUM_TSV = "https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv"
JW_URL = "https://apis.justwatch.com/graphql"

NOW = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=5, minutes=30)
TODAY = NOW.date()  # India date
NEW_DAYS = int(CFG.get("new_release_days", 30))
NEW_LIMIT = int(CFG.get("new_limit", 40))
CINEMA_LIMIT = int(CFG.get("cinema_limit", 60))


def cfg_date(key, default):
    try:
        return dt.date.fromisoformat(str(CFG.get(key)))
    except Exception:
        return default


_first_next = (TODAY.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
SINCE = cfg_date("since", TODAY - dt.timedelta(days=NEW_DAYS))
UPCOMING_UNTIL = cfg_date("upcoming_until",
                          (_first_next + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1))
POSTER_VER = "g3"

S = requests.Session()
S.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
S.headers["Accept-Language"] = "en-IN,en;q=0.9"

CACHE = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
WRITTEN = set()
LOGO_CACHE = {}
STATUS = {}
NOTES = []
ACCENT = (229, 9, 20, 255)


def log(*a):
    print(*a, flush=True)


def note(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    NOTES.append(msg[:240])


# ---------------------------------------------------------------- TMDB
def tmdb(path, **params):
    headers = {}
    if TMDB_KEY.startswith("eyJ"):  # v4 token
        headers["Authorization"] = f"Bearer {TMDB_KEY}"
    else:
        params["api_key"] = TMDB_KEY
    r = S.get("https://api.themoviedb.org/3" + path, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def norm_cert(c):
    c = (c or "").strip()
    if c.upper().startswith("UA") and not c.upper().startswith("U/A"):
        c = "U/A" + c[2:]
    return c


def cert_in(kind, d):
    try:
        if kind == "movie":
            for c in (d.get("release_dates") or {}).get("results", []):
                if c.get("iso_3166_1") == COUNTRY:
                    certs = [x for x in c.get("release_dates", []) if x.get("certification")]
                    certs.sort(key=lambda x: (x.get("type") != 3, x.get("release_date", "")))
                    if certs:
                        return norm_cert(certs[0]["certification"])
        else:
            for c in (d.get("content_ratings") or {}).get("results", []):
                if c.get("iso_3166_1") == COUNTRY and c.get("rating"):
                    return norm_cert(c["rating"])
    except Exception:
        pass
    return ""


def movie_date(d):
    best = ""
    for c in (d.get("release_dates") or {}).get("results", []):
        if c.get("iso_3166_1") == COUNTRY:
            ds = [x for x in c.get("release_dates", []) if x.get("type") in (2, 3) and x.get("release_date")]
            ds.sort(key=lambda x: (x["type"] != 3, x["release_date"]))
            if ds:
                best = ds[0]["release_date"][:10]
    return best or (d.get("release_date") or "")[:10]


def year_range(kind, d):
    first = (d.get("release_date") or d.get("first_air_date") or "")[:4]
    if kind == "movie" or not first:
        return first
    last = (d.get("last_air_date") or "")[:4]
    if d.get("status") in ("Ended", "Canceled") and last:
        return first if last == first else f"{first}\u2013{last}"
    return f"{first}\u2013"


def details(kind, tid):
    """kind is 'movie' or 'tv'."""
    key = f"v3:{kind}:{tid}"
    hit = CACHE.get(key)
    if hit and time.time() - hit["t"] < 3 * 86400:
        return hit
    extra = "external_ids,release_dates" if kind == "movie" else "external_ids,content_ratings"
    d = tmdb(f"/{kind}/{tid}", append_to_response=extra, language="en-US")
    imdb = d.get("imdb_id") or (d.get("external_ids") or {}).get("imdb_id")
    rating = d.get("vote_average") or 0
    if OMDB_KEY and imdb:
        try:
            o = S.get("https://www.omdbapi.com/", params={"apikey": OMDB_KEY, "i": imdb}, timeout=20).json()
            if o.get("imdbRating") not in (None, "N/A"):
                rating = float(o["imdbRating"])
        except Exception:
            pass
    path = d.get("backdrop_path") or d.get("poster_path")
    rec = {
        "t": time.time(),
        "title": d.get("title") or d.get("name") or "",
        "year": (d.get("release_date") or d.get("first_air_date") or "")[:4],
        "yrange": year_range(kind, d),
        "date": movie_date(d) if kind == "movie" else (d.get("first_air_date") or "")[:10],
        "cert": cert_in(kind, d),
        "genre": (d.get("genres") or [{"name": ""}])[0]["name"],
        "imdb": imdb,
        "rating": round(float(rating), 1),
        "img": f"https://image.tmdb.org/t/p/w780{path}" if path else None,
    }
    CACHE[key] = rec
    return rec


def resolve(kind, title, year=None):
    ep = "/search/movie" if kind == "movie" else "/search/tv"
    p = {"query": title, "include_adult": "false"}
    if year:
        p["year" if kind == "movie" else "first_air_date_year"] = year
    res = tmdb(ep, **p).get("results", [])
    if not res and year:
        p.pop("year", None)
        p.pop("first_air_date_year", None)
        res = tmdb(ep, **p).get("results", [])
    return res[0]["id"] if res else None


def resolve_many(kind, items, limit=10):
    """items: titles (str) or dicts with title/year/tmdb. kind: 'movie' or 'series'."""
    ids = []
    for it in items:
        tid = None
        if isinstance(it, dict) and it.get("tmdb"):
            try:
                tid = int(it["tmdb"])
            except (TypeError, ValueError):
                tid = None
        if tid is None:
            title, year = (it, None) if isinstance(it, str) else (it.get("title"), it.get("year"))
            if not title:
                continue
            try:
                tid = resolve(kind, title, year or None)
            except Exception:
                tid = None
        if tid and tid not in ids:
            ids.append(tid)
        if len(ids) >= limit:
            break
    return ids


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("+", "plus"))


BAD = ("withads", "channel", "store", "kids")


def match_names(names, keywords):
    out = []
    for n in names:
        nn = norm(n)
        if any(k in nn for k in keywords) and not any(b in nn for b in BAD):
            out.append(n)
    return sorted(out, key=lambda n: len(norm(n)))


def load_tmdb_providers():
    res = {}
    for kind in ("movie", "tv"):
        for p in tmdb(f"/watch/providers/{kind}", watch_region=COUNTRY, language="en-US").get("results", []):
            res.setdefault(p["provider_name"], p)
    return res


def tmdb_provider_top(kind, provider_ids):
    ep = "/discover/movie" if kind == "movie" else "/discover/tv"
    res = tmdb(ep, watch_region=COUNTRY, with_watch_providers="|".join(provider_ids),
               with_watch_monetization_types="flatrate|free|ads", sort_by="popularity.desc").get("results", [])
    return [r["id"] for r in res[:10]]


def tmdb_provider_new(kind, provider_ids, start):
    key = "primary_release_date" if kind == "movie" else "first_air_date"
    ep = "/discover/movie" if kind == "movie" else "/discover/tv"
    params = {"watch_region": COUNTRY, "with_watch_providers": "|".join(provider_ids),
              "with_watch_monetization_types": "flatrate|free|ads", "sort_by": "popularity.desc",
              f"{key}.gte": str(start), f"{key}.lte": str(TODAY)}
    ids = []
    for page in (1, 2, 3):
        params["page"] = page
        res = tmdb(ep, **params).get("results", [])
        ids += [r["id"] for r in res]
        if len(res) < 20:
            break
    return ids


# ---------------------------------------------------------------- Netflix Tudum
def load_tudum():
    r = S.get(TUDUM_TSV, timeout=90)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.text), delimiter="\t"))


def tudum_titles(rows, cc, kind):
    rows = [r for r in rows if (r.get("country_iso2") or "").upper() == cc]
    if not rows:
        return []
    latest = max(r["week"] for r in rows)
    want = "film" if kind == "movie" else "tv"
    sel = [r for r in rows if r["week"] == latest and (r.get("category") or "").lower().startswith(want)]
    sel.sort(key=lambda r: int(r["weekly_rank"]))
    return [r["show_title"].split(" // ")[0].strip() for r in sel]


# ---------------------------------------------------------------- FlixPatrol
def flix_fetch(slugs):
    for slug in slugs:
        url = f"https://flixpatrol.com/top10/{slug}/{CNAME.lower().replace(' ', '-')}/"
        try:
            r = S.get(url, timeout=30)
            if r.status_code != 200:
                log("FlixPatrol", slug, "HTTP", r.status_code)
                continue
            movies, shows = [], []
            for typ, title in re.findall(r'href="/(movie|tv-show)/[^"]*"[^>]*>\s*([^<]+?)\s*</a>', r.text):
                title = htmllib.unescape(title).strip()
                lst = movies if typ == "movie" else shows
                if title and title not in lst:
                    lst.append(title)
            if len(movies) >= 3 or len(shows) >= 3:
                return {"movie": movies[:10], "series": shows[:10]}
            log("FlixPatrol", slug, "no titles parsed")
        except Exception as e:
            note("FlixPatrol failed", slug, e)
    return None


# ---------------------------------------------------------------- JustWatch
def jw_query(query, variables=None):
    r = S.post(JW_URL, json={"query": query, "variables": variables or {}}, timeout=60)
    r.raise_for_status()
    j = r.json()
    if j.get("errors") and not j.get("data"):
        raise RuntimeError(j["errors"][0].get("message", "JustWatch error"))
    return j["data"]


def load_jw_packages():
    try:
        d = jw_query("query P($country: Country!) { packages(country: $country, platform: WEB) "
                     "{ clearName shortName } }", {"country": COUNTRY})
        return {p["clearName"]: p["shortName"] for p in d["packages"]}
    except Exception as e:
        note("JustWatch packages failed:", e)
        return {}


JW_POP = """query Pop($country: Country!, $language: Language!, $first: Int!, $filter: TitleFilter,
  $sort: PopularTitlesSorting!) {
  popularTitles(country: $country, first: $first, filter: $filter, sortBy: $sort) {
    edges { node { objectType
      content(country: $country, language: $language) {
        title originalReleaseYear originalReleaseDate externalIds { imdbId tmdbId } } } } } }"""


def jw_popular(packages, kind, first=100, year_min=None):
    flt = {"packages": packages, "objectTypes": ["MOVIE" if kind == "movie" else "SHOW"]}
    if year_min:
        flt["releaseYear"] = {"min": year_min}
    d = jw_query(JW_POP, {"country": COUNTRY, "language": "en", "first": first, "filter": flt, "sort": "POPULAR"})
    out = []
    for e in d["popularTitles"]["edges"]:
        c = (e.get("node") or {}).get("content") or {}
        ext = c.get("externalIds") or {}
        out.append({"title": c.get("title"), "year": c.get("originalReleaseYear"),
                    "date": c.get("originalReleaseDate"), "tmdb": ext.get("tmdbId")})
    return out


# ---------------------------------------------------------------- Gemini (optional last resort)
def gemini_top(platform):
    prompt = (
        f"Using Google Search, find the current Top 10 most-watched movies and Top 10 TV series on "
        f"{platform} in {CNAME} this week. Reply ONLY with JSON: "
        '{"movies":[{"title":"","year":0}],"series":[{"title":"","year":0}]} '
        "in rank order. If unsure return empty arrays. Never invent titles."
    )
    body = {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]}
    r = S.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{CFG.get('gemini_model', 'gemini-2.5-flash')}:generateContent",
        params={"key": GEMINI_KEY}, json=body, timeout=120)
    r.raise_for_status()
    text = "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])
    j = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    return j.get("movies", []), j.get("series", [])


# ---------------------------------------------------------------- BookMyShow (best effort)
def ld_names(x, out):
    if isinstance(x, dict):
        if x.get("@type") in ("Movie", "TVSeries") and x.get("name"):
            out.append(x["name"])
        for v in x.values():
            ld_names(v, out)
    elif isinstance(x, list):
        for v in x:
            ld_names(v, out)


def bms_titles(now):
    city = CFG.get("bms_city", "mumbai")
    url = (f"https://in.bookmyshow.com/explore/movies-{city}" if now
           else f"https://in.bookmyshow.com/explore/upcoming-movies-{city}")
    r = S.get(url, timeout=40)
    if r.status_code != 200:
        raise RuntimeError(f"BookMyShow HTTP {r.status_code}")
    titles = []
    for blk in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', r.text, re.S):
        try:
            ld_names(json.loads(blk), titles)
        except Exception:
            continue
    if len(titles) < 3:
        titles = re.findall(r'"(?:EventTitle|eventTitle|EventName)"\s*:\s*"([^"]{2,80})"', r.text)
    clean = []
    for t in titles:
        t = re.sub(r"\s*\((?:hindi|english|tamil|telugu|kannada|malayalam|bengali|marathi|punjabi|gujarati)[^)]*\)",
                   "", htmllib.unescape(t), flags=re.I).strip()
        if t and t not in clean:
            clean.append(t)
    return clean


def cinema_window(now):
    if now:
        return SINCE, TODAY
    return TODAY + dt.timedelta(days=1), UPCOMING_UNTIL


def tmdb_cinema(now):
    lo, hi = cinema_window(now)
    ids = []
    for page in range(1, max(1, (CINEMA_LIMIT + 19) // 20) + 1):
        params = {"region": COUNTRY, "with_release_type": "2|3", "sort_by": "popularity.desc",
                  "include_adult": "false", "language": "en-US", "page": page,
                  "release_date.gte": str(lo), "release_date.lte": str(hi)}
        try:
            res = tmdb("/discover/movie", **params).get("results", [])
        except Exception as e:
            note("TMDB cinema page failed", page, e)
            break
        if not res:
            break
        for r in res:
            if r["id"] not in ids:
                ids.append(r["id"])
    return ids


def cinema_ids(now):
    lo, hi = cinema_window(now)
    ids, src = [], []
    try:
        titles = bms_titles(now)
        years = [hi.year, lo.year] if now else [lo.year, hi.year]
        for t in titles:
            tid = None
            for y in years + [None]:
                try:
                    tid = resolve("movie", t, y)
                except Exception:
                    tid = None
                if tid:
                    rec = details("movie", tid)
                    try:
                        d = dt.date.fromisoformat(rec["date"])
                    except Exception:
                        d = None
                    if d and lo <= d <= hi:
                        break
                    tid = None
            if tid and tid not in ids:
                ids.append(tid)
        if ids:
            src.append("bookmyshow")
        else:
            note("BookMyShow gave", len(titles), "titles, none matched the dates")
    except Exception as e:
        note("BookMyShow failed:", e)
    try:
        for tid in tmdb_cinema(now):
            if tid not in ids:
                ids.append(tid)
        src.append("tmdb")
    except Exception as e:
        note("TMDB cinema failed:", e)
    keep = []
    for tid in ids:
        try:
            d = dt.date.fromisoformat(details("movie", tid)["date"])
        except Exception:
            continue
        if lo <= d <= hi:
            keep.append((d, tid))
    keep.sort(key=lambda x: x[0], reverse=now)  # now showing: newest first; upcoming: soonest first
    return [t for _, t in keep][:CINEMA_LIMIT], "+".join(src) or "none"


# ---------------------------------------------------------------- Poster drawing
def font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default()


def short_genre(g):
    g = (g or "").split(" & ")[0].strip()
    return "Sci-Fi" if g.lower() == "science fiction" else g


def fmt_date(s, weekday=False):
    try:
        return dt.date.fromisoformat((s or "")[:10]).strftime("%a, %d %b %Y" if weekday else "%d %b %Y").upper()
    except Exception:
        return ""


def tint_color(im):
    r, g, b = im.convert("RGB").crop((0, 150, 320, 360)).resize((1, 1), Image.LANCZOS).getpixel((0, 0))
    h, s, _ = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    s = min(max(s, 0.3), 0.75)
    r, g, b = colorsys.hsv_to_rgb(h, s, 0.93)
    return (int(r * 255), int(g * 255), int(b * 255), 255)


def overlay(im, rgb, mask, opacity=1.0):
    lay = Image.new("RGBA", im.size, rgb + (255,)) if isinstance(rgb, tuple) else rgb.copy()
    lay.putalpha(mask.point(lambda p: int(p * opacity)))
    return Image.alpha_composite(im, lay)


def draw_number(im, rank):
    """Transparent glass number: background shows through, bright rim, glossy top."""
    txt = str(rank)
    f = font(250 if rank < 10 else 190)
    l, t, r, b = f.getbbox(txt)
    w, h = r - l, b - t
    x = 36
    top = 24 if rank < 10 else 24 + int(0.729 * (250 - 190))
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).text((x - l, top - t), txt, font=f, fill=255)

    # soft shadow around the glass
    im = overlay(im, (0, 0, 0), mask.filter(ImageFilter.GaussianBlur(11)), 0.38)
    # frosted glass: slightly blurred background inside the digit
    frost = im.filter(ImageFilter.GaussianBlur(5))
    im = overlay(im, frost, mask, 0.45)
    # light-to-dark tint (stays see-through)
    tint = Image.new("RGBA", im.size)
    td = ImageDraw.Draw(tint)
    for yy in range(im.size[1]):
        k = min(max((yy - top) / max(h, 1), 0), 1)
        c = int(235 - 215 * k)
        td.line([(0, yy), (im.size[0], yy)], fill=(c, c, c, 255))
    im = overlay(im, tint, mask, 0.30)
    # glossy highlight on the upper part
    gloss = Image.new("L", im.size, 0)
    gd = ImageDraw.Draw(gloss)
    for yy in range(im.size[1]):
        k = (yy - top) / max(h * 0.6, 1)
        if 0 <= k <= 1:
            gd.line([(0, yy), (im.size[0], yy)], fill=int(255 * (1 - k)))
    im = overlay(im, (255, 255, 255), ImageChops.multiply(mask, gloss), 0.20)
    # glass edges: bright rim, light top-left, dark bottom-right, inner thickness line
    rim = ImageChops.subtract(mask, mask.filter(ImageFilter.MinFilter(5)))
    im = overlay(im, (255, 255, 255), rim, 0.60)
    light = ImageChops.subtract(mask, ImageChops.offset(mask, 4, 4))
    im = overlay(im, (255, 255, 255), light, 0.80)
    dark = ImageChops.subtract(mask, ImageChops.offset(mask, -4, -4))
    im = overlay(im, (0, 0, 0), dark, 0.40)
    line = ImageChops.subtract(mask.filter(ImageFilter.MinFilter(15)), mask.filter(ImageFilter.MinFilter(19)))
    im = overlay(im, (255, 255, 255), line, 0.28)
    return im, x + w / 2


def draw_provider(im, prov, logo_img, ax, y, center):
    """Platform logo (custom logos/<slug>.png, else TMDB icon + name)."""
    d = ImageDraw.Draw(im)
    custom = ROOT / "logos" / f"{prov['slug']}.png"
    if custom.exists():
        lg = Image.open(custom).convert("RGBA")
        lg = lg.resize((max(1, int(lg.width * 30 / lg.height)), 30), Image.LANCZOS)
        if lg.width > 170:
            lg = lg.resize((170, max(1, int(lg.height * 170 / lg.width))), Image.LANCZOS)
        x0 = int(ax - lg.width / 2) if center else int(ax)
        im.alpha_composite(lg, (max(8, x0), y))
        return
    nf = font(17)
    name = prov["name"]
    tw = d.textlength(name, font=nf)
    if logo_img is not None:
        ic = logo_img.convert("RGBA").resize((28, 28), Image.LANCZOS)
        m = Image.new("L", (28, 28), 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, 27, 27), radius=6, fill=255)
        ic.putalpha(m)
        x0 = int(ax - (28 + 8 + tw) / 2) if center else int(ax)
        x0 = max(10, x0)
        im.alpha_composite(ic, (x0, y))
        d.text((x0 + 36, y + 4), name, font=nf, fill=(255, 255, 255, 255),
               stroke_width=1, stroke_fill=(0, 0, 0, 255))
    else:
        x0 = int(ax - tw / 2) if center else int(ax)
        d.text((max(10, x0), y + 4), name, font=nf, fill=(255, 255, 255, 255),
               stroke_width=1, stroke_fill=(0, 0, 0, 255))


def render_poster(base, rec, rank, prov, logo_img):
    """Top 10 card: big glass number, genre / years / rating, platform logo."""
    im = ImageOps.fit(base.convert("RGBA"), (640, 360))
    tint = tint_color(im)
    left = Image.new("L", im.size, 0)
    bottom = Image.new("L", im.size, 0)
    dl, db = ImageDraw.Draw(left), ImageDraw.Draw(bottom)
    for x in range(640):
        dl.line([(x, 0), (x, 360)], fill=int(255 * max(0, 1 - x / 430)))
    for y in range(170, 360):
        db.line([(0, y), (640, y)], fill=int(255 * (y - 170) / 190))
    im = overlay(im, (0, 0, 0), left, 0.78)
    im = overlay(im, (0, 0, 0), bottom, 0.8)
    im, cx = draw_number(im, rank)
    d = ImageDraw.Draw(im)
    f = font(22)
    lines = [(short_genre(rec["genre"]).upper(), tint), (rec.get("yrange") or rec["year"], tint)]
    if rec["rating"]:
        lines.append((f"\u2605 {rec['rating']}", (245, 245, 245, 255)))
    y = 224
    for text, col in lines:
        if not text:
            continue
        w = d.textlength(text, font=f)
        d.text((max(12, cx - w / 2), y), text, font=f, fill=col, stroke_width=1, stroke_fill=(0, 0, 0, 255))
        y += 28
    draw_provider(im, prov, logo_img, cx, 318, True)
    return im.convert("RGB")


def render_info(base, rec, prov, logo_img, tag, weekday=False):
    """Card without number: tag, genre, release date, rating + age rating, platform logo."""
    im = ImageOps.fit(base.convert("RGBA"), (640, 360))
    tint = tint_color(im)
    left = Image.new("L", im.size, 0)
    bottom = Image.new("L", im.size, 0)
    dl, db = ImageDraw.Draw(left), ImageDraw.Draw(bottom)
    for x in range(640):
        dl.line([(x, 0), (x, 360)], fill=int(255 * max(0, 1 - x / 470)))
    for y in range(110, 360):
        db.line([(0, y), (640, y)], fill=int(255 * (y - 110) / 250))
    im = overlay(im, (0, 0, 0), left, 0.7)
    im = overlay(im, (0, 0, 0), bottom, 0.88)
    d = ImageDraw.Draw(im)

    if tag:
        tf = font(16)
        w = d.textlength(tag, font=tf)
        d.rounded_rectangle((28, 24, 28 + w + 20, 24 + 16 + 10), radius=7, fill=ACCENT)
        d.text((38, 29), tag, font=tf, fill=(255, 255, 255, 255))

    f = font(25)
    x, y = 30, 196
    date_txt = fmt_date(rec.get("date"), weekday) or (rec.get("yrange") or rec["year"])
    for text in (short_genre(rec["genre"]).upper(), date_txt):
        if text:
            d.text((x, y), text, font=f, fill=tint, stroke_width=1, stroke_fill=(0, 0, 0, 255))
            y += 33
    cx = x
    if rec["rating"]:
        rt = f"\u2605 {rec['rating']}"
        d.text((x, y), rt, font=f, fill=(245, 245, 245, 255), stroke_width=1, stroke_fill=(0, 0, 0, 255))
        cx = x + d.textlength(rt, font=f) + 16
    if rec.get("cert"):
        pf = font(18)
        w = d.textlength(rec["cert"], font=pf)
        d.rounded_rectangle((cx, y + 1, cx + w + 18, y + 31), radius=6, outline=(255, 255, 255, 255), width=2)
        d.text((cx + 9, y + 5), rec["cert"], font=pf, fill=(255, 255, 255, 255))
    if prov:
        draw_provider(im, prov, logo_img, 30, 318, False)
    return im.convert("RGB")


def provider_logo(path):
    if not path:
        return None
    if path not in LOGO_CACHE:
        try:
            r = S.get("https://image.tmdb.org/t/p/w92" + path, timeout=20)
            r.raise_for_status()
            LOGO_CACHE[path] = Image.open(io.BytesIO(r.content)).convert("RGBA")
        except Exception:
            LOGO_CACHE[path] = None
    return LOGO_CACHE[path]


def make_poster(rec, rank, dest, prov, mode, tag):
    r = S.get(rec["img"], timeout=30)
    r.raise_for_status()
    base = Image.open(io.BytesIO(r.content))
    logo = provider_logo(prov.get("logo")) if prov else None
    if mode == "top10":
        im = render_poster(base, rec, rank, prov, logo)
    else:
        im = render_info(base, rec, prov, logo, tag, weekday=(mode == "cinema"))
    im.save(dest, "JPEG", quality=84)


# ---------------------------------------------------------------- Catalog building
def build_catalog(catid, name, kind, ids, mode, prov, src, tag=None):
    tkind = "movie" if kind == "movie" else "tv"
    metas = []
    for rank, tid in enumerate(ids, 1):
        try:
            rec = details(tkind, tid)
            if not rec["img"]:
                continue
            fname = f"{catid}-{rank}.jpg"
            make_poster(rec, rank, POSTERS / fname, prov, mode, tag)
            WRITTEN.add(("posters", fname))
            nice_date = fmt_date(rec.get("date")).title()
            desc = " | ".join(x for x in (f"Release: {nice_date}" if nice_date else "",
                                          f"Age rating: {rec['cert']}" if rec.get("cert") else "") if x)
            metas.append({
                "id": rec["imdb"] or f"tmdb:{tid}",
                "type": kind,
                "name": rec["title"],
                "poster": f"{BASE}/posters/{fname}?v={tid}-{POSTER_VER}-{int(time.time()) // 86400}",
                "posterShape": "landscape",
                "releaseInfo": nice_date if mode == "cinema" and nice_date else rec["year"],
                "genres": [rec["genre"]] if rec["genre"] else [],
                "imdbRating": str(rec["rating"]) if rec["rating"] else None,
                "description": desc or None,
            })
        except Exception as e:
            note("item failed", catid, tid, e)
    if len(metas) < (3 if mode == "top10" else 1):
        STATUS[catid] = {"name": name, "source": src, "items": len(metas), "result": "skipped (too few items)"}
        note("skipped", name, "- only", len(metas), "items")
        return None
    (OUT / "catalog" / kind / f"{catid}.json").write_text(
        json.dumps({"metas": metas}, ensure_ascii=False), encoding="utf-8")
    WRITTEN.add((f"catalog/{kind}", f"{catid}.json"))
    STATUS[catid] = {"name": name, "source": src, "items": len(metas)}
    log(f"OK {name} [{src}] {len(metas)} items")
    return {"type": kind, "id": catid, "name": name}


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower().replace("+", "")).strip("-")


def top_ids(plat, kind, tudum, flix, jw_short):
    name = plat["name"]
    if name == "Netflix" and tudum:
        try:
            ids = resolve_many(kind, tudum_titles(tudum, COUNTRY, kind))
            if len(ids) >= 3:
                return ids, "tudum"
        except Exception as e:
            note("Tudum failed", e)
    if flix:
        try:
            ids = resolve_many(kind, flix.get(kind, []))
            if len(ids) >= 3:
                return ids, "flixpatrol"
        except Exception as e:
            note("FlixPatrol resolve failed", name, e)
    if jw_short:
        try:
            ids = resolve_many(kind, jw_popular(jw_short, kind, first=30))
            if len(ids) >= 3:
                return ids, "justwatch"
        except Exception as e:
            note("JustWatch top failed", name, e)
    if CFG.get("use_gemini") and GEMINI_KEY:
        try:
            m, s = gemini_top(name)
            ids = resolve_many(kind, m if kind == "movie" else s)
            if len(ids) >= 3:
                time.sleep(4)
                return ids, "gemini"
        except Exception as e:
            note("Gemini failed", name, e)
    return None, "tmdb"


def new_ids(kind, tk, jw_short, tm_ids):
    ids, src = [], []
    if jw_short:
        try:
            items = jw_popular(jw_short, kind, first=100, year_min=SINCE.year)
            keep = []
            for i in items:
                try:
                    d = dt.date.fromisoformat((i.get("date") or "")[:10])
                except Exception:
                    continue
                if SINCE <= d <= TODAY:
                    keep.append(i)
            ids = resolve_many(kind, keep, limit=NEW_LIMIT * 2)
            if ids:
                src.append("justwatch")
        except Exception as e:
            note("JustWatch new failed", e)
    if tm_ids:
        try:
            for tid in tmdb_provider_new(tk, tm_ids, SINCE):
                if tid not in ids:
                    ids.append(tid)
            src.append("tmdb")
        except Exception as e:
            note("TMDB new failed", e)
    keep = []
    for tid in ids:
        try:
            d = dt.date.fromisoformat(details(tk, tid)["date"])
        except Exception:
            continue
        if SINCE <= d <= TODAY:
            keep.append((d, tid))
    keep.sort(key=lambda x: x[0], reverse=True)  # newest first
    return [t for _, t in keep][:NEW_LIMIT], "+".join(src) or "none"


def main():
    if not TMDB_KEY:
        sys.exit("TMDB_API_KEY missing")
    POSTERS.mkdir(parents=True, exist_ok=True)
    for t in ("movie", "series"):
        (OUT / "catalog" / t).mkdir(parents=True, exist_ok=True)

    try:
        tudum = load_tudum()
        log("Tudum rows:", len(tudum))
    except Exception as e:
        tudum = []
        note("Tudum load failed:", e)

    try:
        tm_providers = load_tmdb_providers()
    except Exception as e:
        tm_providers = {}
        note("TMDB providers failed:", e)
    jw_packages = load_jw_packages()

    cats = []

    # ---- cinema rows (very top)
    for now in (True, False):
        try:
            ids, src = cinema_ids(now)
            lo, hi = cinema_window(now)
            if now:
                name, tag, catid = f"Now Showing in Cinema - {TODAY.strftime('%B')}", "NOW SHOWING", f"{PFX}-cinema-now"
            else:
                name, tag, catid = "Upcoming in Cinema", "COMING SOON", f"{PFX}-cinema-upcoming"
            c = build_catalog(catid, name, "movie", ids, "cinema", None, src, tag)
            if c:
                cats.append(c)
        except Exception as e:
            note("cinema row failed", e)

    # ---- platform rows
    for plat in CFG["platforms"]:
        name = plat["name"]
        slug = slugify(name)
        tm_names = match_names(tm_providers.keys(), plat["match"])
        tm_ids = [str(tm_providers[n]["provider_id"]) for n in tm_names]
        logo = tm_providers[tm_names[0]].get("logo_path") if tm_names else None
        jw_names = match_names(jw_packages.keys(), plat["match"])
        jw_short = [jw_packages[n] for n in jw_names]
        if not tm_ids:
            note("TMDB provider not found for", name)
        if not jw_short:
            note("JustWatch package not found for", name)
        prov = {"name": name, "slug": slug, "logo": logo}
        flix = None
        if plat.get("flixpatrol") and not (name == "Netflix" and tudum):
            flix = flix_fetch(plat["flixpatrol"])
            time.sleep(1)

        # new releases first, then top 10
        for section in ("new", "top"):
            for kind in ("movie", "series"):
                tk = "movie" if kind == "movie" else "tv"
                try:
                    if section == "new":
                        ids, src = new_ids(kind, tk, jw_short, tm_ids)
                        label = "New Release"
                        mode = "new"
                    else:
                        ids, src = top_ids(plat, kind, tudum, flix, jw_short)
                        if ids is None and tm_ids:
                            ids = tmdb_provider_top(tk, tm_ids)
                        label = "Top 10"
                        mode = "top10"
                    if not ids:
                        note("no data for", name, section, kind)
                        continue
                    catid = f"{PFX}-{slug}-{section}-{kind}"
                    if mode == "new":
                        title = f"{name} {label}"
                    else:
                        title = f"{name} {label} - {'Movies' if kind == 'movie' else 'Series'}"
                    c = build_catalog(catid, title, kind, ids, mode, prov, src, "NEW" if mode == "new" else None)
                    if c:
                        cats.append(c)
                except Exception as e:
                    note("row failed", name, section, kind, e)
            time.sleep(1)

    if not cats:
        sys.exit("No catalogs produced; keeping old files")

    manifest = {
        "id": "community.ott.top10",
        "version": f"1.0.{int(time.time()) // 3600}",
        "name": "OTT Top 10",
        "description": "Cinema, new releases and Top 10 per OTT platform.",
        "resources": ["catalog"],
        "types": ["movie", "series"],
        "catalogs": cats,
        "behaviorHints": {"configurable": False},
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "status.json").write_text(json.dumps(
        {"updated": NOW.strftime("%Y-%m-%d %H:%M IST"), "catalogs": STATUS, "notes": NOTES[-60:]},
        ensure_ascii=False, indent=1), encoding="utf-8")

    for sub in ("posters", "catalog/movie", "catalog/series"):
        for f in (OUT / sub).glob("*"):
            if (sub, f.name) not in WRITTEN:
                f.unlink()

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(CACHE), encoding="utf-8")
    log("Done. Catalogs:", len(cats))


if __name__ == "__main__":
    main()
