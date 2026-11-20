#!/usr/bin/env python3
"""Builds static Stremio/Nuvio catalogs (OTT Top 10) into ./docs for GitHub Pages.

Sources: Netflix Tudum (official) -> Gemini + Google Search -> TMDB popularity fallback.
"""
import csv
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
TMDB_KEY = os.environ.get("TMDB_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
OMDB_KEY = os.environ.get("OMDB_API_KEY", "")
BASE = os.environ.get("BASE_URL", "").rstrip("/")

OUT = ROOT / "docs"
POSTERS = OUT / "posters"
CACHE_FILE = ROOT / "data" / "cache.json"
TUDUM_TSV = "https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv"

S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (ott-top10-builder)"

CACHE = json.loads(CACHE_FILE.read_text(encoding="utf-8")) if CACHE_FILE.exists() else {}
WRITTEN = set()


def log(*a):
    print(*a, flush=True)


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


def details(kind, tid):
    key = f"{kind}:{tid}"
    hit = CACHE.get(key)
    if hit and time.time() - hit["t"] < 7 * 86400:
        return hit
    d = tmdb(f"/{'movie' if kind == 'movie' else 'tv'}/{tid}",
             append_to_response="external_ids", language="en-US")
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


def resolve_many(kind, items):
    ids = []
    for it in items:
        title, year = (it, None) if isinstance(it, str) else (it.get("title"), it.get("year"))
        if not title:
            continue
        try:
            tid = resolve(kind, title, year or None)
        except Exception:
            tid = None
        if tid and tid not in ids:
            ids.append(tid)
    return ids[:10]


def tmdb_fallback(kind, provider_id, cc):
    ep = "/discover/movie" if kind == "movie" else "/discover/tv"
    res = tmdb(ep, watch_region=cc, with_watch_providers=provider_id,
               with_watch_monetization_types="flatrate", sort_by="popularity.desc").get("results", [])
    return [r["id"] for r in res[:10]]


def get_providers(cc):
    seen = {}
    for kind in ("movie", "tv"):
        for p in tmdb(f"/watch/providers/{kind}", watch_region=cc, language="en-US").get("results", []):
            seen.setdefault(p["provider_id"], p)
    bad = [b.lower() for b in CFG.get("exclude_name_contains", [])]
    items = [p for p in seen.values() if not any(b in p["provider_name"].lower() for b in bad)]
    items.sort(key=lambda p: (p.get("display_priorities") or {}).get(cc, 999))
    return [(p["provider_id"], p["provider_name"]) for p in items[: CFG.get("max_platforms_per_country", 20)]]


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


# ---------------------------------------------------------------- Gemini
def gemini_top(platform, country):
    prompt = (
        f"Using Google Search, find the current Top 10 most-watched movies and Top 10 TV series on "
        f"{platform} in {country} this week (platform's own Top 10 or FlixPatrol). Include all languages "
        f"and origins. Reply ONLY with JSON: "
        '{"movies":[{"title":"","year":0}],"series":[{"title":"","year":0}]} '
        "in rank order, max 10 each. If you cannot find reliable data, return empty arrays. "
        "Never invent titles."
    )
    body = {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]}
    r = S.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{CFG.get('gemini_model', 'gemini-2.5-flash')}:generateContent",
        params={"key": GEMINI_KEY}, json=body, timeout=120)
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts)
    m = re.search(r"\{.*\}", text, re.S)
    j = json.loads(m.group(0))
    return j.get("movies", []), j.get("series", [])


# ---------------------------------------------------------------- Poster
def font(size):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except OSError:
        return ImageFont.load_default()


def make_poster(rec, rank, dest):
    r = S.get(rec["img"], timeout=30)
    r.raise_for_status()
    im = ImageOps.fit(Image.open(io.BytesIO(r.content)).convert("RGBA"), (640, 360))
    left = Image.new("RGBA", im.size, (0, 0, 0, 0))
    bottom = Image.new("RGBA", im.size, (0, 0, 0, 0))
    dl, db = ImageDraw.Draw(left), ImageDraw.Draw(bottom)
    for x in range(640):
        dl.line([(x, 0), (x, 360)], fill=(0, 0, 0, int(215 * max(0, 1 - x / 430))))
    for y in range(180, 360):
        db.line([(0, y), (640, y)], fill=(0, 0, 0, int(210 * (y - 180) / 180)))
    im = Image.alpha_composite(Image.alpha_composite(im, left), bottom)
    d = ImageDraw.Draw(im)
    d.text((22, 4), str(rank), font=font(200), fill=(255, 255, 255, 235),
           stroke_width=3, stroke_fill=(0, 0, 0, 255))
    lines = [rec["genre"].upper(), rec["year"], f"\u2605 {rec['rating']}" if rec["rating"] else ""]
    y = 262
    for ln in lines:
        if ln:
            d.text((26, y), ln, font=font(24), fill=(255, 215, 90, 255) if ln.startswith("\u2605") else (235, 235, 235, 255))
            y += 30
    im.convert("RGB").save(dest, "JPEG", quality=82)


# ---------------------------------------------------------------- Main
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
        log("Tudum load failed:", e)

    countries = CFG["countries"]
    multi = len(countries) > 1
    manifest_cats = []

    for c in countries:
        cc, cname = c["code"].upper(), c["name"]
        try:
            providers = get_providers(cc)
        except Exception as e:
            log("providers failed", cc, e)
            continue
        for pid, pname in providers:
            slug = re.sub(r"[^a-z0-9]+", "-", pname.lower()).strip("-")
            got = {}
            source = "tmdb"
            try:
                if pname.lower() == "netflix" and tudum:
                    for kind in ("movie", "series"):
                        got[kind] = resolve_many(kind, tudum_titles(tudum, cc, kind))
                    source = "tudum"
                elif CFG.get("use_gemini") and GEMINI_KEY:
                    movies, series = gemini_top(pname, cname)
                    got["movie"] = resolve_many("movie", movies)
                    got["series"] = resolve_many("series", series)
                    source = "gemini"
                    time.sleep(4)
            except Exception as e:
                log(f"{source} failed for {pname}/{cc}:", e)
                got = {}

            for kind in ("movie", "series"):
                ids = got.get(kind, [])
                src = source
                if len(ids) < 3:
                    try:
                        ids = tmdb_fallback("movie" if kind == "movie" else "tv", pid, cc)
                        src = "tmdb"
                    except Exception as e:
                        log("fallback failed", pname, e)
                        continue
                tkind = "movie" if kind == "movie" else "tv"
                catid = f"{cc.lower()}-{slug}-{kind}"
                metas = []
                for rank, tid in enumerate(ids[:10], 1):
                    try:
                        rec = details(tkind, tid)
                        if not rec["img"]:
                            continue
                        fname = f"{catid}-{rank}.jpg"
                        make_poster(rec, rank, POSTERS / fname)
                        WRITTEN.add(("posters", fname))
                        metas.append({
                            "id": rec["imdb"] or f"tmdb:{tid}",
                            "type": kind,
                            "name": rec["title"],
                            "poster": f"{BASE}/posters/{fname}?v={tid}",
                            "posterShape": "landscape",
                            "releaseInfo": rec["year"],
                            "genres": [rec["genre"]] if rec["genre"] else [],
                            "imdbRating": str(rec["rating"]) if rec["rating"] else None,
                        })
                    except Exception as e:
                        log("item failed", pname, tid, e)
                if len(metas) < 3:
                    continue
                (OUT / "catalog" / kind / f"{catid}.json").write_text(
                    json.dumps({"metas": metas}, ensure_ascii=False), encoding="utf-8")
                WRITTEN.add((f"catalog/{kind}", f"{catid}.json"))
                name = f"{pname} Top 10 - {'Movies' if kind == 'movie' else 'Series'}"
                if multi:
                    name += f" ({cc})"
                manifest_cats.append({"type": kind, "id": catid, "name": name})
                log(f"OK {name} [{src}] {len(metas)} items")

    if not manifest_cats:
        sys.exit("No catalogs produced; keeping old files")

    manifest = {
        "id": "community.ott.top10",
        "version": f"1.0.{int(time.time()) // 3600}",
        "name": "OTT Top 10",
        "description": "Top 10 movies and series per OTT platform and country.",
        "resources": ["catalog"],
        "types": ["movie", "series"],
        "catalogs": manifest_cats,
        "behaviorHints": {"configurable": False},
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    # remove stale files
    for sub in ("posters", "catalog/movie", "catalog/series"):
        for f in (OUT / sub).glob("*"):
            if (sub, f.name) not in WRITTEN:
                f.unlink()

    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(CACHE), encoding="utf-8")
    log("Done. Catalogs:", len(manifest_cats))


if __name__ == "__main__":
    main()
