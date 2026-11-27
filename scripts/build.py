#!/usr/bin/env python3
"""Builds static Stremio/Nuvio catalogs into ./docs for GitHub Pages.

Rows: cinema (now showing / upcoming) + per platform: new releases and top 10 (per-platform on/off in config).
Top 10 sources: per platform "top_source" in config (tudum / flixpatrol / justwatch); the others only fill gaps.
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
from concurrent.futures import ThreadPoolExecutor
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
NEW_LIMIT = int(CFG.get("new_limit", 150))
NEW_PAGES = int(CFG.get("new_pages", 15))
LOGO_MODES = set(CFG.get("title_logo_on", ["top10", "new", "cinema"]))  # top10 / new / cinema
META_ALL = bool(CFG.get("meta_for_all", True))
ID_VER = str(CFG.get("catalog_version", "2"))
IMG_LANGS = "en,null,hi,bn,ta,te,ml,kn,mr,pa,gu,ur,or,as,ko,ja,zh,th,id,es,fr,de,it,pt,ru,tr,ar"
CINEMA_LIMIT = int(CFG.get("cinema_limit", 60))
TOP_N = 10      # how many cards each Top 10 row must show
TOP_POOL = 25   # candidate titles collected so a failed title can be replaced by the next one


def cfg_date(key, default):
    try:
        return dt.date.fromisoformat(str(CFG.get(key)))
    except Exception:
        return default


_first_next = (TODAY.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
SINCE = cfg_date("since", TODAY - dt.timedelta(days=NEW_DAYS))
UPCOMING_UNTIL = cfg_date("upcoming_until",
                          (_first_next + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1))
POSTER_VER = "g4"

S = requests.Session()
S.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
S.headers["Accept-Language"] = "en-IN,en;q=0.9"

CACHE = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
WRITTEN = set()
LOGO_CACHE = {}
TLOGO = {}
META_TODO = {}
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
    last = None
    for attempt in range(4):
        try:
            r = S.get("https://api.themoviedb.org/3" + path, params=params, headers=headers, timeout=30)
            if r.status_code in (429, 500, 502, 503, 504):
                last = requests.HTTPError(f"TMDB HTTP {r.status_code}")
                try:
                    wait = float(r.headers.get("Retry-After", 0))
                except ValueError:
                    wait = 0
                time.sleep(min(max(wait, 1.5 * (attempt + 1)), 10))
                continue
            r.raise_for_status()
            return r.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


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


def ott_date(d):
    """Latest digital / TV release date in our country that is not in the future."""
    best = ""
    for c in (d.get("release_dates") or {}).get("results", []):
        if c.get("iso_3166_1") == COUNTRY:
            for x in c.get("release_dates", []):
                ds = (x.get("release_date") or "")[:10]
                if x.get("type") in (4, 6) and ds and ds <= str(TODAY) and ds > best:
                    best = ds
    return best


def best_logo(d):
    """TMDB title artwork (PNG). Original-language logo first, then English, then language-neutral."""
    logos = [l for l in (d.get("images") or {}).get("logos", [])
             if (l.get("file_path") or "").lower().endswith(".png")]
    orig = (d.get("original_language") or "").lower()

    def rank(l):
        iso = l.get("iso_639_1")
        pref = 3 if iso == orig else 2 if iso == "en" else 1 if iso is None else 0
        return (pref, l.get("vote_average") or 0, l.get("width") or 0)

    logos.sort(key=rank, reverse=True)
    return logos[0]["file_path"] if logos else None


def latest_season(d):
    best = (0, "")
    for s in d.get("seasons") or []:
        ad = s.get("air_date") or ""
        if s.get("season_number", 0) > 0 and ad and ad <= str(TODAY) and ad > best[1]:
            best = (s["season_number"], ad)
    return best


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
    key = f"v5:{kind}:{tid}"
    hit = CACHE.get(key)
    if hit and time.time() - hit["t"] < 3 * 86400:
        return hit
    extra = ("external_ids,release_dates" if kind == "movie" else "external_ids,content_ratings") + ",images"
    d = tmdb(f"/{kind}/{tid}", append_to_response=extra, language="en-US", include_image_language=IMG_LANGS)
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
    lg = best_logo(d)
    rec["logo"] = f"https://image.tmdb.org/t/p/w500{lg}" if lg else None
    if kind == "movie":
        rec["ott_date"] = ott_date(d)
    else:
        sn, sd = latest_season(d)
        rec["season"], rec["season_date"] = sn, sd
    CACHE[key] = rec
    return rec


def find_imdb(kind, imdb):
    ck = f"f1:{kind}:{imdb}"
    hit = CACHE.get(ck)
    if hit and time.time() - hit["t"] < 30 * 86400:
        return hit.get("id")
    try:
        r = tmdb(f"/find/{imdb}", external_source="imdb_id")
    except Exception:
        return None
    lst = r.get("movie_results" if kind == "movie" else "tv_results") or []
    tid = lst[0]["id"] if lst else None
    CACHE[ck] = {"t": time.time(), "id": tid}
    return tid


def resolve(kind, title, year=None, strict=False):
    nt = norm(title)
    if not nt:
        return None
    ck = f"r1:{kind}:{nt}:{year or ''}" + (":s" if strict else "")
    hit = CACHE.get(ck)
    if hit and hit.get("id") and time.time() - hit["t"] < 7 * 86400:
        return hit["id"]
    ep = "/search/movie" if kind == "movie" else "/search/tv"
    p = {"query": title, "include_adult": "false", "language": "en-US"}
    if year:
        p["year" if kind == "movie" else "first_air_date_year"] = year
    res = tmdb(ep, **p).get("results", [])
    if not res and year:
        p.pop("year", None)
        p.pop("first_air_date_year", None)
        res = tmdb(ep, **p).get("results", [])
    if not res:
        return None
    exact = [r for r in res[:8]
             if nt in (norm(r.get("title") or r.get("name")),
                       norm(r.get("original_title") or r.get("original_name")))]
    if strict and not exact:
        return None
    pick = max(exact, key=lambda r: r.get("popularity") or 0) if exact else res[0]
    CACHE[ck] = {"t": time.time(), "id": pick["id"]}
    return pick["id"]


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
        if tid is None and isinstance(it, dict) and it.get("imdb"):
            tid = find_imdb(kind, it["imdb"])
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
    return [r["id"] for r in res[:TOP_POOL]]


def tmdb_provider_new(kind, provider_ids, start):
    ep = "/discover/movie" if kind == "movie" else "/discover/tv"
    base = {"watch_region": COUNTRY, "with_watch_providers": "|".join(provider_ids),
            "with_watch_monetization_types": "flatrate|free|ads", "sort_by": "popularity.desc",
            "language": "en-US"}
    if kind == "movie":
        queries = [
            {"primary_release_date.gte": str(start), "primary_release_date.lte": str(TODAY)},
            # OTT premiere of films that released in cinemas earlier (digital / TV release in our country)
            {"region": COUNTRY, "with_release_type": "4|6",
             "release_date.gte": str(start), "release_date.lte": str(TODAY)},
        ]
    else:
        queries = [
            {"first_air_date.gte": str(start), "first_air_date.lte": str(TODAY)},   # brand-new shows
            {"air_date.gte": str(start), "air_date.lte": str(TODAY)},               # new seasons / episodes
        ]
    ids = []
    for q in queries:
        params = dict(base, **q)
        for page in range(1, NEW_PAGES + 1):
            params["page"] = page
            try:
                res = tmdb(ep, **params).get("results", [])
            except Exception as e:
                note("TMDB new page failed", kind, page, e)
                break
            for r in res:
                if r["id"] not in ids:
                    ids.append(r["id"])
            if len(res) < 20:
                break
    return ids# ---------------------------------------------------------------- Netflix Tudum
def load_tudum():
    last = None
    for attempt in range(3):
        try:
            r = S.get(TUDUM_TSV, timeout=120)
            r.raise_for_status()
            r.encoding = "utf-8"
            return list(csv.DictReader(io.StringIO(r.text), delimiter="\t"))
        except Exception as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise last


def tudum_entries(rows, cc, kind):
    """Netflix Top 10 of the latest week, in rank order; each entry = list of title candidates."""
    rows = [r for r in rows if (r.get("country_iso2") or "").upper() == cc]
    if not rows:
        return []
    latest = max(r["week"] for r in rows)
    want = "film" if kind == "movie" else "tv"
    sel = [r for r in rows if r["week"] == latest and (r.get("category") or "").lower().startswith(want)]
    sel.sort(key=lambda r: int(r["weekly_rank"]))
    out = []
    for r in sel:
        cands = []
        raws = r["show_title"].split(" // ") + [r.get("season_title") or ""]
        for raw in raws:
            base = re.sub(r":\s*(season|limited series|part|volume|chapter|series)\b.*$", "", raw.strip(), flags=re.I).strip()
            for c in (base, re.sub(r"\s*\(.*?\)\s*", " ", base).strip(), re.split(r"\s[:\-\u2013]\s", base)[0].strip()):
                if c and c.lower() != "n/a" and c not in cands:
                    cands.append(c)
        if cands and all(cands[0] != o[0] for o in out):
            out.append(cands)
    return out


def tudum_titles(rows, cc, kind):
    rows = [r for r in rows if (r.get("country_iso2") or "").upper() == cc]
    if not rows:
        return []
    latest = max(r["week"] for r in rows)
    want = "film" if kind == "movie" else "tv"
    sel = [r for r in rows if r["week"] == latest and (r.get("category") or "").lower().startswith(want)]
    sel.sort(key=lambda r: int(r["weekly_rank"]))
    out = []
    for r in sel:
        t = r["show_title"].split(" // ")[0].strip()
        t = re.sub(r":\s*(season|limited series|part|volume|chapter|series)\b.*$", "", t, flags=re.I).strip()
        if t and t not in out:
            out.append(t)
    return out


# ---------------------------------------------------------------- FlixPatrol
def flix_fetch(slugs):
    for slug in slugs:
        url = f"https://flixpatrol.com/top10/{slug}/{CNAME.lower().replace(' ', '-')}/"
        try:
            r = S.get(url, timeout=30)
            if r.status_code != 200:
                note("FlixPatrol", slug, "HTTP", r.status_code)
                continue
            movies, shows = [], []
            for typ, title in re.findall(r'href="/(movie|tv-show)/[^"]*"[^>]*>\s*([^<]+?)\s*</a>', r.text):
                title = htmllib.unescape(title).strip()
                lst = movies if typ == "movie" else shows
                if title and title not in lst:
                    lst.append(title)
            if len(movies) >= 3 or len(shows) >= 3:
                return {"movie": movies[:10], "series": shows[:10]}
            note("FlixPatrol", slug, "no titles parsed")
        except Exception as e:
            note("FlixPatrol failed", slug, e)
    return None


# ---------------------------------------------------------------- JustWatch
def jw_query(query, variables=None):
    r = S.post(JW_URL, json={"query": query, "variables": variables or {}}, timeout=60)
    try:
        j = r.json()
    except ValueError:
        r.raise_for_status()
        raise
    if j.get("errors") and not j.get("data"):
        raise RuntimeError(f"HTTP {r.status_code}: " + str(j["errors"][0].get("message", "JustWatch error"))[:200])
    r.raise_for_status()
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


JW_POP2 = """query Pop($country: Country!, $language: Language!, $first: Int!, $after: String,
  $filter: TitleFilter, $sort: PopularTitlesSorting!) {
  popularTitles(country: $country, first: $first, after: $after, filter: $filter, sortBy: $sort) {
    pageInfo { endCursor hasNextPage }
    edges { node { objectType
      content(country: $country, language: $language) {
        title originalReleaseYear originalReleaseDate externalIds { imdbId tmdbId } } } } } }"""


def jw_recent(packages, kind, year_min, pages=6):
    """All titles of this year+ on the platform (JustWatch), newest release year first, paged."""
    flt = {"packages": packages, "objectTypes": ["MOVIE" if kind == "movie" else "SHOW"],
           "releaseYear": {"min": year_min}}
    out, after = [], None
    for _ in range(pages):
        d = jw_query(JW_POP2, {"country": COUNTRY, "language": "en", "first": 100, "after": after,
                               "filter": flt, "sort": "RELEASE_YEAR"})["popularTitles"]
        for e in d["edges"]:
            c = (e.get("node") or {}).get("content") or {}
            ext = c.get("externalIds") or {}
            out.append({"title": c.get("title"), "year": c.get("originalReleaseYear"),
                        "date": c.get("originalReleaseDate"), "tmdb": ext.get("tmdbId"), "imdb": ext.get("imdbId")})
        pi = d.get("pageInfo") or {}
        if not pi.get("hasNextPage") or not pi.get("endCursor"):
            break
        after = pi["endCursor"]
    return out


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
                    "date": c.get("originalReleaseDate"), "tmdb": ext.get("tmdbId"), "imdb": ext.get("imdbId")})
    return out


# ---------------------------------------------------------------- JustWatch "new arrivals" (added to platform on date X)
ARRIVAL_DAYS = int(CFG.get("arrival_days", 30))
ARR_DATES = {}                 # (tmdb kind, tmdb id) -> arrival date (YYYY-MM-DD)
ARR_MEMO = {}
WIN = {"since": SINCE, "days": ARRIVAL_DAYS}   # per-platform "new" window (config new_days)
ARR_TYPES = {}
ARR_STATE = {"broken": False, "variant": None}

_NEW_FIELDS = """objectType
      content(country: $country, language: $language) {
        title originalReleaseYear originalReleaseDate externalIds { imdbId tmdbId } }"""
JW_NEW_VARIANTS = [
    {"paged": True, "q": """query New($country: Country!, $language: Language!, $date: Date!, $first: Int!,
  $after: String, $filter: TitleFilter) {
  newTitles(country: $country, date: $date, first: $first, after: $after, filter: $filter) {
    pageInfo { endCursor hasNextPage }
    edges { node { """ + _NEW_FIELDS + """ } } } }"""},
    {"paged": False, "q": """query New($country: Country!, $language: Language!, $date: Date!, $first: Int!,
  $filter: TitleFilter) {
  newTitles(country: $country, date: $date, first: $first, filter: $filter) {
    edges { node { """ + _NEW_FIELDS + """ } } } }"""},
]


def jw_diag():
    """Writes the real names of JustWatch's 'new' queries into status.json notes (for fixing)."""
    try:
        d = jw_query('{ __type(name: "Query") { fields { name args { name type { name ofType { name } } } } } }')
        for f in d["__type"]["fields"]:
            if "new" in f["name"].lower():
                args = ", ".join(a["name"] + ":" + str(a["type"].get("name") or (a["type"].get("ofType") or {}).get("name"))
                                 for a in f["args"])
                note("JW query:", f["name"], "(", args, ")")
    except Exception as e:
        note("JW introspection failed:", e)


def _jw_day(pkgs, day, variant, types=("MOVIE", "SHOW")):
    flt = {"packages": pkgs, "objectTypes": list(types)}
    out, after = [], None
    for _ in range(5):
        v = {"country": COUNTRY, "language": "en", "date": str(day), "first": 100, "filter": flt}
        if variant["paged"]:
            v["after"] = after
        d = jw_query(variant["q"], v)["newTitles"]
        edges = d.get("edges") if isinstance(d, dict) else d
        for e in edges or []:
            n = (e or {}).get("node") or e or {}
            c = n.get("content") or {}
            ext = c.get("externalIds") or {}
            if not c.get("title"):
                continue
            ARR_TYPES[n.get("objectType")] = ARR_TYPES.get(n.get("objectType"), 0) + 1
            out.append({"kind": "movie" if n.get("objectType") == "MOVIE" else "series",
                        "otype": n.get("objectType"), "title": c.get("title"), "year": c.get("originalReleaseYear"),
                        "tmdb": ext.get("tmdbId"), "imdb": ext.get("imdbId"), "date": str(day)})
        pi = d.get("pageInfo") if isinstance(d, dict) else None
        if not (variant["paged"] and pi and pi.get("hasNextPage") and pi.get("endCursor")):
            break
        after = pi["endCursor"]
    return out


def jw_arrivals(pkgs):
    """Titles that were ADDED to these platforms in the last ARRIVAL_DAYS days (what JustWatch 'New' shows)."""
    key = "+".join(sorted(pkgs))
    mkey = f"{key}:{WIN['days']}"
    if mkey in ARR_MEMO:
        return ARR_MEMO[mkey]
    if ARR_STATE["broken"]:
        return []
    if ARR_STATE["variant"] is None:
        errs = []
        for var in JW_NEW_VARIANTS:
            try:
                _jw_day(pkgs, TODAY - dt.timedelta(days=1), var)
                ARR_STATE["variant"] = var
                break
            except Exception as e:
                errs.append(str(e)[:200])
        if ARR_STATE["variant"] is None:
            for e in errs:
                note("JW new-arrivals failed:", e)
            jw_diag()
            ARR_STATE["broken"] = True
            return []
    var = ARR_STATE["variant"]
    start = TODAY - dt.timedelta(days=WIN["days"])
    days = [TODAY - dt.timedelta(days=i) for i in range((TODAY - start).days + 1)]
    res, todo = [], []
    for day in days:
        hit = CACHE.get(f"jwn1:{key}:{day}")
        if hit and (day < TODAY - dt.timedelta(days=3)) and time.time() - hit["t"] < 40 * 86400:
            res += hit["items"]
        else:
            todo.append(day)

    def one(day):
        try:
            items = _jw_day(pkgs, day, var)
            CACHE[f"jwn1:{key}:{day}"] = {"t": time.time(), "items": items}
            return items
        except Exception as e:
            note("JW arrivals day failed", str(day), str(e)[:120])
            return []

    with ThreadPoolExecutor(max_workers=4) as ex:
        for items in ex.map(one, todo):
            res += items
    if res and not any(i["kind"] == "series" for i in res):
        # the combined query returned only movies: ask for shows separately
        note("JW arrivals: no series in combined feed, retrying with SHOW only. types:", ARR_TYPES)

        def one_s(day):
            ck = f"jwn1s:{key}:{day}"
            hit = CACHE.get(ck)
            if hit and day < TODAY - dt.timedelta(days=3) and time.time() - hit["t"] < 40 * 86400:
                return hit["items"]
            try:
                items = _jw_day(pkgs, day, var, types=("SHOW",))
                CACHE[ck] = {"t": time.time(), "items": items}
                return items
            except Exception as e:
                note("JW arrivals SHOW day failed", str(day), str(e)[:120])
                return []

        with ThreadPoolExecutor(max_workers=4) as ex:
            for items in ex.map(one_s, days):
                res += items
        if not ARR_STATE.get("typed"):
            ARR_STATE["typed"] = True
            note("JW arrivals types after retry:", ARR_TYPES)
    ARR_MEMO[mkey] = res
    return res


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
        return TODAY - dt.timedelta(days=int(CFG.get("cinema_now_days", 30))), TODAY
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
    return [t for _, t in keep][:CINEMA_LIMIT], "+".join(src) or "none"# ---------------------------------------------------------------- Poster drawing
def font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default()


FONT_DIR = ROOT / "fonts"


def inter(size, weight="ExtraBold"):
    """Inter from ./fonts (Inter-Black.otf / Inter-ExtraBold.otf), else DejaVu Bold."""
    for p in (FONT_DIR / f"Inter-{weight}.otf", FONT_DIR / f"Inter-{weight}.ttf"):
        try:
            return ImageFont.truetype(str(p), size)
        except OSError:
            pass
    return font(size)


def spaced_width(d, text, f, sp):
    return sum(d.textlength(c, font=f) for c in text) + sp * max(len(text) - 1, 0)


def put_text(im, cx, y, text, f, fill, sp=0, center=True):
    """Text with soft shadow, optional letter spacing. cx is centre (or left edge if center=False)."""
    lay = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(lay)
    w = spaced_width(ld, text, f, sp)
    x = cx - w / 2 if center else cx
    x = max(12, x)
    sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(sh)
    xx = x
    for c in text:
        sd.text((xx + 1, y + 2), c, font=f, fill=(0, 0, 0, 200))
        xx += sd.textlength(c, font=f) + sp
    im = Image.alpha_composite(im, sh.filter(ImageFilter.GaussianBlur(3)))
    xx = x
    for c in text:
        ld.text((xx, y), c, font=f, fill=fill)
        xx += ld.textlength(c, font=f) + sp
    return Image.alpha_composite(im, lay), w


def star(im, cx, cy, r, fill):
    import math
    pts = []
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.42
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    ImageDraw.Draw(im).polygon(pts, fill=fill)


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
    s = min(max(s, 0.15), 0.55)
    r, g, b = colorsys.hsv_to_rgb(h, s, 0.93)
    return (int(r * 255), int(g * 255), int(b * 255), 255)


def overlay(im, rgb, mask, opacity=1.0):
    lay = Image.new("RGBA", im.size, rgb + (255,)) if isinstance(rgb, tuple) else rgb.copy()
    lay.putalpha(mask.point(lambda p: int(p * opacity)))
    return Image.alpha_composite(im, lay)


def frost_panel(im):
    """Left-side frosted glass panel (backdrop blur) that fades out towards the right, like the reference."""
    blur = im.filter(ImageFilter.GaussianBlur(26))
    pm = Image.new("L", im.size, 0)
    pd = ImageDraw.Draw(pm)
    for x in range(im.size[0]):
        if x < 150:
            a = 255
        elif x < 360:
            k = (x - 150) / 210
            a = int(255 * (1 - k * k * (3 - 2 * k)))
        else:
            a = 0
        pd.line([(x, 0), (x, im.size[1])], fill=a)
    im = overlay(im, blur, pm, 1.0)
    return overlay(im, (62, 64, 68), pm, 0.40)


def draw_number(im, rank):
    """Glass number on the frosted panel: soft shadow, frosted fill, bright rim, inner bevel."""
    txt = str(rank)
    size = 240 if rank < 10 else 168
    f = inter(size, "Black")
    l, t, r, b = f.getbbox(txt)
    w, h = r - l, b - t
    x = 46 if rank < 10 else 34
    top = 214 - h
    raw = Image.new("L", im.size, 0)
    ImageDraw.Draw(raw).text((x - l, top - t), txt, font=f, fill=255)
    mask = raw.filter(ImageFilter.GaussianBlur(5)).point(lambda p: 255 if p > 135 else 0).filter(ImageFilter.GaussianBlur(0.7))

    im = overlay(im, (0, 0, 0), ImageChops.offset(mask, 0, 7).filter(ImageFilter.GaussianBlur(11)), 0.32)
    # frosted fill (strong blur of what is behind) + light tint
    im = overlay(im, im.filter(ImageFilter.GaussianBlur(30)), mask, 1.0)
    im = overlay(im, (235, 235, 240), mask, 0.13)
    # darker towards the bottom of the digit
    shade = Image.new("L", im.size, 0)
    sd = ImageDraw.Draw(shade)
    for yy in range(im.size[1]):
        k = (yy - top) / max(h, 1)
        if 0 <= k <= 1:
            sd.line([(0, yy), (im.size[0], yy)], fill=int(255 * k))
    im = overlay(im, (0, 0, 0), ImageChops.multiply(mask, shade), 0.22)
    # inner bevel (bright band just inside the edge) + bright rim + top-left highlight
    inner = ImageChops.subtract(mask, mask.filter(ImageFilter.MinFilter(9))).filter(ImageFilter.GaussianBlur(2))
    im = overlay(im, (255, 255, 255), ImageChops.multiply(inner, mask), 0.20)
    rim = ImageChops.subtract(mask, mask.filter(ImageFilter.MinFilter(3)))
    im = overlay(im, (255, 255, 255), rim, 0.42)
    light = ImageChops.subtract(mask, ImageChops.offset(mask, 3, 3))
    im = overlay(im, (255, 255, 255), light, 0.30)
    dark = ImageChops.subtract(mask, ImageChops.offset(mask, -2, -2))
    im = overlay(im, (0, 0, 0), dark, 0.18)
    return im, x + w / 2


def fetch_title_logo(url):
    """Title logo PNG (transparent). Dark logos are turned white so they show on the dark card."""
    if not url:
        return None
    if url not in TLOGO:
        try:
            r = S.get(url, timeout=25)
            r.raise_for_status()
            lg = Image.open(io.BytesIO(r.content)).convert("RGBA")
            px = lg.resize((48, 48)).getdata()
            tot = sum(p[3] for p in px) or 1
            lum = sum((0.3 * p[0] + 0.59 * p[1] + 0.11 * p[2]) * p[3] for p in px) / tot
            if lum < 60:
                white = Image.new("RGBA", lg.size, (255, 255, 255, 255))
                white.putalpha(lg.split()[3])
                lg = white
            TLOGO[url] = lg
        except Exception:
            TLOGO[url] = None
    return TLOGO[url]


def draw_title_logo(im, rec):
    """Movie/series title artwork at the bottom-right (text fallback when no logo exists)."""
    lg = fetch_title_logo(rec.get("logo"))
    box_w, box_h, mr, mb = 270, 112, 24, 22
    if lg is not None:
        k = min(box_w / lg.width, box_h / lg.height)
        lg = lg.resize((max(1, int(lg.width * k)), max(1, int(lg.height * k))), Image.LANCZOS)
        x, y = im.width - mr - lg.width, im.height - mb - lg.height
        sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
        blk = Image.new("RGBA", lg.size, (0, 0, 0, 255))
        blk.putalpha(lg.split()[3].point(lambda p: int(p * 0.7)))
        sh.alpha_composite(blk, (x + 2, y + 4))
        im = Image.alpha_composite(im, sh.filter(ImageFilter.GaussianBlur(6)))
        im.alpha_composite(lg, (x, y))
        return im
    return im


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
    """Top 10 card: big glass number, genre / years / rating, platform logo, title logo."""
    im = ImageOps.fit(base.convert("RGBA"), (640, 360))
    tint = tint_color(im)
    im = frost_panel(im)
    bottom = Image.new("L", im.size, 0)
    db = ImageDraw.Draw(bottom)
    for y in range(170, 360):
        db.line([(0, y), (640, y)], fill=int(255 * (y - 170) / 190))
    im = overlay(im, (0, 0, 0), bottom, 0.55)
    im, cx = draw_number(im, rank)
    f = inter(23, "ExtraBold")
    y = 230
    for text, sp in ((short_genre(rec["genre"]).upper(), 2), (rec.get("yrange") or rec["year"], 1)):
        if text:
            im, _ = put_text(im, cx, y, text, f, tint, sp)
            y += 28
    if rec["rating"]:
        rt = str(rec["rating"])
        wd = ImageDraw.Draw(im).textlength(rt, font=f)
        total = 20 + 8 + wd
        sx = cx - total / 2
        star(im, sx + 10, y + 15, 10, tint)
        im, _ = put_text(im, sx + 28, y, rt, f, tint, 0, center=False)
    draw_provider(im, prov, logo_img, cx, 318, True)
    if "top10" in LOGO_MODES:
        im = draw_title_logo(im, rec)
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
    if ("cinema" if weekday else "new") in LOGO_MODES:
        im = draw_title_logo(im, rec)
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
    last = None
    for _ in range(3):
        try:
            r = S.get(rec["img"], timeout=30)
            r.raise_for_status()
            base = Image.open(io.BytesIO(r.content))
            base.load()
            break
        except Exception as e:
            last = e
            time.sleep(1.5)
    else:
        raise last
    logo = provider_logo(prov.get("logo")) if prov else None
    if mode == "top10":
        im = render_poster(base, rec, rank, prov, logo)
    else:
        im = render_info(base, rec, prov, logo, tag, weekday=(mode == "cinema"))
    im.save(dest, "JPEG", quality=84)# ---------------------------------------------------------------- Catalog building
def arrival_date(tk, tid):
    try:
        return dt.date.fromisoformat(ARR_DATES.get((tk, tid)) or "")
    except Exception:
        return None


def eff_date(tk, rec):
    """Date a title counts as 'new' on the platform: OTT premiere (movies) / latest season (series)
    or the original date, whichever falls in the window."""
    alt = rec.get("ott_date") if tk == "movie" else rec.get("season_date")
    for s in (alt, rec.get("date")):
        try:
            d = dt.date.fromisoformat((s or "")[:10])
        except Exception:
            continue
        if WIN["since"] <= d <= TODAY:
            return d
    return None


def build_catalog(catid, name, kind, ids, mode, prov, src, tag=None):
    tkind = "movie" if kind == "movie" else "tv"
    metas = []
    rank = 0
    for tid in ids:
        if mode == "top10" and rank >= TOP_N:
            break
        try:
            rec = details(tkind, tid)
            if not rec["img"]:
                continue
            n = rank + 1
            rv, tag_i = dict(rec), tag
            if mode == "new":
                d = eff_date(tkind, rec) or arrival_date(tkind, tid)
                if d:
                    rv["date"] = str(d)
                    if tkind == "tv" and (rec.get("season") or 1) > 1 and rec.get("season_date") == str(d):
                        tag_i = f"NEW SEASON {rec['season']}"
            fname = f"{catid}-{n}-{tid}.jpg" if mode == "top10" else f"{catid}-{tid}.jpg"
            make_poster(rv, n, POSTERS / fname, prov, mode, tag_i)
            rank = n
            WRITTEN.add(("posters", fname))
            nice_date = fmt_date(rv.get("date")).title()
            desc = " | ".join(x for x in (f"Release: {nice_date}" if nice_date else "",
                                          f"Age rating: {rec['cert']}" if rec.get("cert") else "") if x)
            mid = rec["imdb"] or f"tmdb:{tid}"
            META_TODO[(kind, mid)] = (tkind, tid, rec["imdb"])
            metas.append({
                "id": mid,
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
    if mode == "top10" and len(metas) < TOP_N:
        note(f"{name}: only {len(metas)}/{TOP_N} cards built (candidates: {len(ids)})")
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


# ---------------------------------------------------------------- meta files (so taps never say 'No addon provides meta')
def tmdb_img(path, size="w500"):
    return f"https://image.tmdb.org/t/p/{size}{path}" if path else None


def write_meta(kind, mid, tk, tid):
    common = dict(append_to_response="credits,images", language="en-US", include_image_language=IMG_LANGS)
    d = tmdb(f"/{tk}/{tid}", **common)
    lg = best_logo(d)
    cast = [c["name"] for c in (d.get("credits") or {}).get("cast", [])[:8]]
    meta = {
        "id": mid, "type": kind,
        "name": d.get("title") or d.get("name") or "",
        "poster": tmdb_img(d.get("poster_path")),
        "background": tmdb_img(d.get("backdrop_path"), "w1280"),
        "logo": tmdb_img(lg),
        "description": d.get("overview") or "",
        "releaseInfo": year_range(tk, d),
        "genres": [g["name"] for g in d.get("genres", [])],
        "cast": cast,
    }
    if d.get("vote_average"):
        meta["imdbRating"] = str(round(d["vote_average"], 1))
    rel = (d.get("release_date") or d.get("first_air_date") or "")[:10]
    if rel:
        meta["released"] = rel + "T00:00:00.000Z"
    if tk == "movie":
        if d.get("runtime"):
            meta["runtime"] = f"{d['runtime']} min"
        crew = (d.get("credits") or {}).get("crew", [])
        meta["director"] = [c["name"] for c in crew if c.get("job") == "Director"][:3]
    else:
        seasons = [s["season_number"] for s in d.get("seasons", []) if s.get("season_number", 0) > 0]
        videos = []
        for i in range(0, len(seasons), 15):
            chunk = seasons[i:i + 15]
            dd = tmdb(f"/tv/{tid}", append_to_response=",".join(f"season/{n}" for n in chunk), language="en-US")
            for n in chunk:
                for ep in (dd.get(f"season/{n}") or {}).get("episodes", []):
                    if not ep.get("air_date"):
                        continue
                    videos.append({
                        "id": f"{mid}:{n}:{ep['episode_number']}",
                        "title": ep.get("name") or f"Episode {ep['episode_number']}",
                        "season": n, "episode": ep["episode_number"],
                        "released": ep["air_date"] + "T00:00:00.000Z",
                        "overview": ep.get("overview") or "",
                        "thumbnail": tmdb_img(ep.get("still_path"), "w300"),
                    })
        meta["videos"] = videos
    sub = OUT / "meta" / kind
    sub.mkdir(parents=True, exist_ok=True)
    fname = f"{mid}.json"
    (sub / fname).write_text(json.dumps({"meta": meta}, ensure_ascii=False), encoding="utf-8")
    WRITTEN.add((f"meta/{kind}", fname))


def write_all_meta():
    n = skipped = 0
    for (kind, mid), (tk, tid, imdb) in META_TODO.items():
        if imdb and not META_ALL:
            continue
        fname = f"{mid}.json"
        key = f"meta:{kind}:{mid}"
        hit = CACHE.get(key)
        if hit and time.time() - hit["t"] < 3 * 86400 and (OUT / "meta" / kind / fname).exists():
            WRITTEN.add((f"meta/{kind}", fname))
            skipped += 1
            continue
        try:
            write_meta(kind, mid, tk, tid)
            CACHE[key] = {"t": time.time()}
            n += 1
        except Exception as e:
            note("meta failed", mid, e)
    log("Meta files written:", n, "reused:", skipped)


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower().replace("+", "")).strip("-")


def top_source(plat):
    """Which source is the main one for this platform's Top 10: tudum / flixpatrol / justwatch."""
    mode = str(plat.get("top_source") or ("tudum" if plat["name"] == "Netflix" else "flixpatrol")).lower()
    return mode if mode in ("tudum", "flixpatrol", "justwatch") else "flixpatrol"


def top_ids(plat, kind, tudum, flix, jw_short, tm_ids):
    """Returns (candidate ids, source). The platform's own source (config 'top_source') goes first;
    the rest only fill the gaps if it gives fewer than needed.
    build_catalog shows the first TOP_N candidates that work, so a title that cannot be found /
    has no image / fails to render is replaced by the next one (always 10 cards)."""
    name = plat["name"]
    tk = "movie" if kind == "movie" else "tv"
    mode = top_source(plat)
    ids, src = [], []
    need = TOP_N + 3  # keep filling until we have a small safety buffer

    def add(new, label):
        n0 = len(ids)
        for t in new:
            if t not in ids and len(ids) < TOP_POOL:
                ids.append(t)
        if len(ids) > n0:
            src.append(label)

    def from_tudum():
        if not tudum:
            note(name, ": Tudum data not loaded")
            return
        entries = tudum_entries(tudum, COUNTRY, kind)
        got, miss = [], []
        for cands in entries:
            tid = None
            for c in cands:                      # exact name match first
                try:
                    tid = resolve(kind, c, None, strict=True)
                except Exception:
                    tid = None
                if tid:
                    break
            if tid is None:                      # last resort: best search hit of the main title
                try:
                    tid = resolve(kind, cands[0], None)
                except Exception:
                    tid = None
                if tid:
                    note(f"Tudum {kind}: approx match for", cands[0])
            if tid and tid not in got:
                got.append(tid)
            elif not tid:
                miss.append(cands[0])
        log(f"Tudum {kind}: {len(entries)} titles -> {len(got)} resolved")
        if miss:
            note(f"Tudum {kind}: not found on TMDB:", miss)
        add(got, "tudum")

    def from_flix():
        if flix:
            add(resolve_many(kind, flix.get(kind, []), limit=TOP_POOL), "flixpatrol")
        else:
            note(name, ": FlixPatrol data not available")

    def from_jw():
        if jw_short:
            add(resolve_many(kind, jw_popular(jw_short, kind, first=40), limit=TOP_POOL), "justwatch-top")
        else:
            note(name, ": JustWatch package not found")

    def from_gemini():
        if len(ids) < 3 and CFG.get("use_gemini") and GEMINI_KEY:
            m, s_ = gemini_top(name)
            add(resolve_many(kind, m if kind == "movie" else s_, limit=TOP_POOL), "gemini")
            time.sleep(4)

    def from_tmdb():
        if tm_ids:
            add(tmdb_provider_top(tk, tm_ids), "tmdb-popular")

    order = {"tudum": (from_tudum, from_flix, from_jw, from_gemini, from_tmdb),
             "flixpatrol": (from_flix, from_jw, from_gemini, from_tmdb),
             "justwatch": (from_jw, from_gemini, from_tmdb)}[mode]
    for i, step in enumerate(order):
        if i > 0 and len(ids) >= need:
            break
        if i > 0 and plat.get("strict_top") and len(ids) >= 3:
            break          # e.g. Netflix: show only the official list, no filler titles
        try:
            step()
        except Exception as e:
            note(name, kind, step.__name__, "failed:", e)
    return ids, "+".join(src) or "none"


def prefetch(tk, ids):
    """Fetch TMDB details in parallel (fills the cache) so big lists stay fast."""
    todo = [t for t in ids if f"v5:{tk}:{t}" not in CACHE]

    def one(t):
        try:
            details(tk, t)
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(one, todo))


def new_ids(kind, tk, jw_short, tm_ids):
    ids, src = [], []
    if jw_short:
        try:
            arr = [i for i in jw_arrivals(jw_short) if i["kind"] == kind]
            arr.sort(key=lambda i: i["date"], reverse=True)
            for it in arr:
                got = resolve_many(kind, [it], limit=1)
                if got:
                    ARR_DATES.setdefault((tk, got[0]), it["date"])
                    if got[0] not in ids:
                        ids.append(got[0])
            if ids:
                src.append("justwatch-new")
        except Exception as e:
            note("JustWatch arrivals failed", e)
        items = []
        try:
            items += jw_recent(jw_short, kind, WIN["since"].year, pages=min(NEW_PAGES, 8))
        except Exception as e:
            note("JustWatch recent failed", e)
        try:
            items += jw_popular(jw_short, kind, first=100, year_min=WIN["since"].year)
        except Exception as e:
            note("JustWatch popular failed", e)
        if items:
            for t in resolve_many(kind, items, limit=2000):
                if t not in ids:
                    ids.append(t)
            src.append("justwatch")
    if tm_ids:
        try:
            for tid in tmdb_provider_new(tk, tm_ids, WIN["since"]):
                if tid not in ids:
                    ids.append(tid)
            src.append("tmdb")
        except Exception as e:
            note("TMDB new failed", e)
    prefetch(tk, ids)
    keep = []
    for tid in ids:
        try:
            d = arrival_date(tk, tid) or eff_date(tk, details(tk, tid))
        except Exception:
            continue
        if d:
            keep.append((d, tid))
    keep.sort(key=lambda x: x[0], reverse=True)  # newest first
    return [t for _, t in keep][:NEW_LIMIT], "+".join(src) or "none"


def main():
    if not TMDB_KEY:
        sys.exit("TMDB_API_KEY missing")
    POSTERS.mkdir(parents=True, exist_ok=True)
    for t in ("movie", "series"):
        (OUT / "catalog" / t).mkdir(parents=True, exist_ok=True)
        (OUT / "meta" / t).mkdir(parents=True, exist_ok=True)

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
    try:
        wk = max(r["week"] for r in tudum if (r.get("country_iso2") or "").upper() == COUNTRY)
        note("Netflix Tudum latest week for", COUNTRY, ":", wk)
    except Exception:
        pass

    cats = []

    # ---- cinema rows (very top)
    for now in (True, False):
        try:
            ids, src = cinema_ids(now)
            lo, hi = cinema_window(now)
            if now:
                name, tag, catid = "Now Showing in Cinema", "NOW SHOWING", f"{PFX}{ID_VER}-cinema-now"
            else:
                name, tag, catid = "Upcoming in Cinema", "COMING SOON", f"{PFX}{ID_VER}-cinema-upcoming"
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
        if plat.get("jw_packages"):
            ov = [x for x in plat["jw_packages"] if x in jw_packages.values()]
            if ov:
                jw_short = ov
            else:
                note(name, ": jw_packages override not found:", plat["jw_packages"])
        if plat.get("tmdb_providers"):
            tm_ids = [str(x) for x in plat["tmdb_providers"]]
            for pr in tm_providers.values():
                if str(pr["provider_id"]) == tm_ids[0]:
                    logo = pr.get("logo_path") or logo
        near = [f"{n}={v}" for n, v in jw_packages.items() if plat["match"][0][:6] in norm(n)]
        note(name, "| JW used:", jw_short, "| TMDB ids:", tm_ids, "| JW names like it:", near[:4])
        nd = plat.get("new_days")
        WIN["days"] = int(nd) if nd else ARRIVAL_DAYS
        WIN["since"] = min(SINCE, TODAY - dt.timedelta(days=int(nd))) if nd else SINCE
        if not tm_ids:
            note("TMDB provider not found for", name)
        if not jw_short:
            note("JustWatch package not found for", name)
        prov = {"name": name, "slug": slug, "logo": logo}
        flix = None
        if plat.get("flixpatrol") and plat.get("top10", True) and top_source(plat) != "justwatch":
            flix = flix_fetch(plat["flixpatrol"])
            time.sleep(1)

        # new releases first, then top 10
        for section in ("new", "top"):
            if section == "new" and not plat.get("new", True):
                continue
            if section == "top" and not plat.get("top10", True):
                continue
            for kind in ("movie", "series"):
                tk = "movie" if kind == "movie" else "tv"
                try:
                    if section == "new":
                        ids, src = new_ids(kind, tk, jw_short, tm_ids)
                        label = "New Release"
                        mode = "new"
                    else:
                        ids, src = top_ids(plat, kind, tudum, flix, jw_short, tm_ids)
                        label = "Top 10"
                        mode = "top10"
                    if not ids:
                        note("no data for", name, section, kind)
                        continue
                    catid = f"{PFX}{ID_VER}-{slug}-{section}-{kind}"
                    title = f"{name} {label}"   # the app adds " - Movies" / " - Series" itself
                    c = build_catalog(catid, title, kind, ids, mode, prov, src, "NEW" if mode == "new" else None)
                    if c:
                        cats.append(c)
                except Exception as e:
                    note("row failed", name, section, kind, e)
            time.sleep(1)

    if not cats:
        sys.exit("No catalogs produced; keeping old files")

    write_all_meta()
    (OUT / ".nojekyll").write_text("", encoding="utf-8")

    manifest = {
        "id": "community.ott.top10",
        "version": f"1.0.{int(time.time()) // 3600}",
        "name": "OTT Top 10",
        "description": "Cinema, new releases and Top 10 per OTT platform.",
        "resources": ["catalog", {"name": "meta", "types": ["movie", "series"],
                                  "idPrefixes": ["tt", "tmdb:"] if META_ALL else ["tmdb:"]}],
        "types": ["movie", "series"],
        "catalogs": cats,
        "behaviorHints": {"configurable": False},
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "status.json").write_text(json.dumps(
        {"updated": NOW.strftime("%Y-%m-%d %H:%M IST"), "catalogs": STATUS, "notes": NOTES[-150:]},
        ensure_ascii=False, indent=1), encoding="utf-8")

    for sub in ("posters", "catalog/movie", "catalog/series", "meta/movie", "meta/series"):
        for f in (OUT / sub).glob("*"):
            if (sub, f.name) not in WRITTEN:
                f.unlink()

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(CACHE), encoding="utf-8")
    log("Done. Catalogs:", len(cats))


if __name__ == "__main__":
    main()
    
