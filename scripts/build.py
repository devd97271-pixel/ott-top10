import os
import re
import json
import datetime
import calendar
import requests
from io import BytesIO
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont

TMDB_API_KEY = os.environ["TMDB_API_KEY"]
OMDB_API_KEY = os.environ["OMDB_API_KEY"]
GITHUB_USER = os.environ["GITHUB_REPOSITORY_OWNER"]
REPO_NAME = os.environ["GITHUB_REPOSITORY"].split("/")[-1]
BASE_URL = f"https://{GITHUB_USER}.github.io/{REPO_NAME}"

NOW = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
YEAR = NOW.year
MONTH = NOW.month
MONTH_NAME = NOW.strftime("%B %Y")
LAST_DAY = calendar.monthrange(YEAR, MONTH)[1]
MONTH_START = f"{YEAR:04d}-{MONTH:02d}-01"
MONTH_END = f"{YEAR:04d}-{MONTH:02d}-{LAST_DAY:02d}"

NEXT_MONTH_DT = (NOW.replace(day=28) + datetime.timedelta(days=5)).replace(day=1)
NEXT_MONTH_START = NEXT_MONTH_DT.strftime("%Y-%m-%d")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}

PLATFORMS = [
    {
        "key": "netflix",
        "name": "Netflix",
        "badge": "NETFLIX",
        "color": "#E50914",
        "aliases": ["netflix"],
        "justwatch_slug": "netflix",
    },
    {
        "key": "prime",
        "name": "Prime Video",
        "badge": "prime video",
        "color": "#00A8E1",
        "aliases": ["amazon prime video", "prime video"],
        "justwatch_slug": "amazon-prime-video",
    },
    {
        "key": "jiohotstar",
        "name": "JioHotstar",
        "badge": "JioHotstar",
        "color": "#1D8CF8",
        "aliases": ["jiohotstar", "jio hotstar", "disney+ hotstar", "hotstar", "jiocinema"],
        "justwatch_slug": "hotstar",
    },
    {
        "key": "mxplayer",
        "name": "Amazon MX Player",
        "badge": "MX PLAYER",
        "color": "#2196F3",
        "aliases": ["amazon mx player", "mx player", "amazon minitv"],
        "justwatch_slug": "mx-player",
    },
    {
        "key": "sonyliv",
        "name": "SonyLIV",
        "badge": "SonyLIV",
        "color": "#F5A623",
        "aliases": ["sonyliv", "sony liv"],
        "justwatch_slug": "sony-liv",
    },
    {
        "key": "zee5",
        "name": "ZEE5",
        "badge": "ZEE5",
        "color": "#9B51E0",
        "aliases": ["zee5"],
        "justwatch_slug": "zee5",
    },
    {
        "key": "appletv",
        "name": "Apple TV+",
        "badge": " tv+",
        "color": "#FFFFFF",
        "aliases": ["apple tv+", "apple tv plus", "apple tv"],
        "justwatch_slug": "apple-tv-plus",
    },
    {
        "key": "hoichoi",
        "name": "Hoichoi",
        "badge": "hoichoi",
        "color": "#FF2E63",
        "aliases": ["hoichoi"],
        "justwatch_slug": "hoichoi",
    },
]

os.makedirs("docs/images", exist_ok=True)
os.makedirs("docs/catalog/movie", exist_ok=True)
os.makedirs("docs/catalog/series", exist_ok=True)

OMDB_CACHE = {}
TMDB_EXTRA_CACHE = {}


def get_font(size, bold=True):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def fetch_json(url, timeout=15):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}


def get_omdb_info(imdb_id):
    if not imdb_id:
        return None, None
    if imdb_id in OMDB_CACHE:
        return OMDB_CACHE[imdb_id]
    data = fetch_json(f"https://www.omdbapi.com/?i={imdb_id}&apikey={OMDB_API_KEY}", timeout=10)
    rating = data.get("imdbRating")
    rated = data.get("Rated")
    rating = rating if rating and rating not in ("N/A", "") else None
    rated = rated if rated and rated not in ("N/A", "") else None
    OMDB_CACHE[imdb_id] = (rating, rated)
    return rating, rated


def get_tmdb_details(media_type, tmdb_id):
    key = f"{media_type}:{tmdb_id}"
    if key in TMDB_EXTRA_CACHE:
        return TMDB_EXTRA_CACHE[key]

    endpoint = "movie" if media_type == "movie" else "tv"
    url = (
        f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}"
        f"?api_key={TMDB_API_KEY}"
        f"&append_to_response=external_ids,images,release_dates,content_ratings"
        f"&include_image_language=en,hi,bn,ta,te,ml,kn,null"
    )
    data = fetch_json(url, timeout=15)
    imdb_id = data.get("external_ids", {}).get("imdb_id")

    in_cert = None
    if media_type == "movie":
        for region_entry in data.get("release_dates", {}).get("results", []):
            if region_entry.get("iso_3166_1") == "IN":
                for rel in region_entry.get("release_dates", []):
                    cert = (rel.get("certification") or "").strip()
                    if cert:
                        in_cert = cert
                        break
            if in_cert:
                break
    else:
        for cr in data.get("content_ratings", {}).get("results", []):
            if cr.get("iso_3166_1") == "IN" and (cr.get("rating") or "").strip():
                in_cert = cr["rating"].strip()
                break

    logos = data.get("images", {}).get("logos", [])
    logo_url = f"https://image.tmdb.org/t/p/w500{logos[0]['file_path']}" if logos else None

    result = (imdb_id, in_cert, logo_url)
    TMDB_EXTRA_CACHE[key] = result
    return result


def get_provider_map():
    mapping = {p["key"]: set() for p in PLATFORMS}
    for mtype in ["movie", "tv"]:
        data = fetch_json(f"https://api.themoviedb.org/3/watch/providers/{mtype}?api_key={TMDB_API_KEY}&watch_region=IN")
        for item in data.get("results", []):
            pname = (item.get("provider_name") or "").lower().strip()
            pid = item.get("provider_id")
            if not pid:
                continue
            for p in PLATFORMS:
                if any(alias == pname or alias in pname for alias in p["aliases"]):
                    if p["key"] == "prime" and "channel" in pname:
                        continue
                    mapping[p["key"]].add(str(pid))
    return {k: "|".join(v) for k, v in mapping.items() if v}


def scrape_titles_from_url(url, limit=12):
    titles = []
    junk = {
        "netflix", "prime video", "amazon prime video", "jiohotstar", "hotstar",
        "mx player", "amazon mx player", "sonyliv", "zee5", "apple tv+", "hoichoi",
        "justwatch", "flixpatrol", "tudum", "top 10", "movies", "tv shows", "series",
        "india", "sign in", "login", "privacy", "terms", "home", "explore"
    }
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200 and len(resp.text) > 500:
            soup = BeautifulSoup(resp.text, "html.parser")
            for el in soup.select("a, h2, h3, h4, span, div"):
                txt = " ".join(el.get_text(" ", strip=True).split())
                txt_clean = re.sub(r"^\d+[\).\s-]+", "", txt).strip()
                low = txt_clean.lower()
                if 2 <= len(txt_clean) <= 60 and low not in junk and not low.startswith("http"):
                    if txt_clean not in titles:
                        titles.append(txt_clean)
                if len(titles) >= limit:
                    break
    except Exception:
        pass
    return titles


def fetch_justwatch_titles(slug, limit=12):
    urls = [
        f"https://www.justwatch.com/in/provider/{slug}",
        f"https://www.justwatch.com/in/provider/{slug}/new",
    ]
    for u in urls:
        found = scrape_titles_from_url(u, limit=limit)
        if found:
            print(f"[JustWatch] {slug}: {len(found)} titles")
            return found
    return []


def fetch_flixpatrol_titles(platform_name, limit=12):
    urls = [
        "https://flixpatrol.com/top10/streaming/india/",
        "https://flixpatrol.com/top10/",
    ]
    for u in urls:
        found = scrape_titles_from_url(u, limit=limit)
        if found:
            print(f"[FlixPatrol] {platform_name}: {len(found)} titles")
            return found
    return []


def fetch_tudum_titles(limit=12):
    urls = [
        "https://www.netflix.com/tudum/top10/india",
        "https://www.netflix.com/tudum/top10",
    ]
    for u in urls:
        found = scrape_titles_from_url(u, limit=limit)
        if found:
            print(f"[Tudum] Netflix: {len(found)} titles")
            return found
    return []


def resolve_platform_titles(platform, media_type, limit=10):
    pkey = platform["key"]
    if pkey == "netflix":
        titles = fetch_tudum_titles(limit=limit)
        if not titles:
            titles = fetch_justwatch_titles(platform["justwatch_slug"], limit=limit)
    else:
        titles = fetch_flixpatrol_titles(platform["name"], limit=limit)
        if not titles:
            titles = fetch_justwatch_titles(platform["justwatch_slug"], limit=limit)

    matched_items = []
    seen_ids = set()
    endpoint = "movie" if media_type == "movie" else "tv"

    for t in titles:
        q = requests.utils.quote(t)
        data = fetch_json(f"https://api.themoviedb.org/3/search/{endpoint}?api_key={TMDB_API_KEY}&query={q}&region=IN")
        results = data.get("results", [])
        if results:
            item = results[0]
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                matched_items.append(item)
        if len(matched_items) >= limit:
            break

    return matched_items
          def render_card(
    out_path,
    backdrop_url=None,
    logo_url=None,
    glass_rank=None,
    release_date_str=None,
    age_rating=None,
    imdb_rating=None,
    platform_badge=None,
    platform_color="#FFFFFF",
):
    W, H = 960, 540

    if backdrop_url:
        try:
            resp = requests.get(backdrop_url, headers=HEADERS, timeout=15)
            bg = Image.open(BytesIO(resp.content)).convert("RGBA").resize((W, H), Image.Resampling.LANCZOS)
        except Exception:
            bg = Image.new("RGBA", (W, H), (20, 22, 28, 255))
    else:
        bg = Image.new("RGBA", (W, H), (20, 22, 28, 255))

    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(grad)
    for x in range(W):
        if x < 390:
            alpha = int(220 - (x / 390) * 65)
        elif x < 680:
            alpha = int(155 - ((x - 390) / 290) * 155)
        else:
            alpha = 0
        gdraw.line([(x, 0), (x, H)], fill=(10, 12, 16, alpha))
    bg = Image.alpha_composite(bg, grad)

    if logo_url:
        try:
            lresp = requests.get(logo_url, headers=HEADERS, timeout=10)
            logo = Image.open(BytesIO(lresp.content)).convert("RGBA")
            logo.thumbnail((320, 130), Image.Resampling.LANCZOS)
            bg.paste(logo, (W - logo.width - 34, H - logo.height - 30), logo)
        except Exception:
            pass

    if glass_rank is not None:
        num_str = str(glass_rank)
        num_font = get_font(225 if len(num_str) == 1 else 188, bold=True)
        num_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ndraw = ImageDraw.Draw(num_layer)
        nx, ny = 40, 10
        ndraw.text((nx + 6, ny + 8), num_str, font=num_font, fill=(0, 0, 0, 135))
        ndraw.text(
            (nx, ny),
            num_str,
            font=num_font,
            fill=(255, 255, 255, 82),
            stroke_width=3,
            stroke_fill=(255, 255, 255, 150),
        )
        bg = Image.alpha_composite(bg, num_layer)
        cur_y = 285
    else:
        cur_y = 145

    draw = ImageDraw.Draw(bg)
    x0 = 44
    line_font = get_font(24, bold=True)
    badge_font = get_font(25, bold=True)

    if release_date_str:
        draw.text((x0, cur_y), f"DATE: {release_date_str}", font=line_font, fill=(235, 238, 245, 255))
        cur_y += 36

    if age_rating:
        draw.text((x0, cur_y), f"AGE: {age_rating}", font=line_font, fill=(220, 225, 235, 255))
        cur_y += 36

    if imdb_rating:
        draw.text((x0, cur_y), "★", font=line_font, fill=(245, 197, 24, 255))
        draw.text((x0 + 28, cur_y), f"IMDb {imdb_rating}", font=line_font, fill=(255, 255, 255, 255))
        cur_y += 42

    if platform_badge:
        draw.text((x0, cur_y), platform_badge, font=badge_font, fill=platform_color)

    bg.convert("RGB").save(out_path, "JPEG", quality=88, optimize=True)


def format_date_short(date_str):
    if not date_str:
        return None
    try:
        dt = datetime.datetime.strptime(date_str[:10], "%Y-%m-%d")
        return dt.strftime("%d %b %Y")
    except Exception:
        return date_str[:10]


def build_meta_entry(item, media_type, stremio_type, img_filename, glass_rank=None, platform_info=None):
    tmdb_id = item["id"]
    title = item.get("title") or item.get("name") or "Unknown"
    raw_date = item.get("release_date") or item.get("first_air_date") or ""
    date_label = format_date_short(raw_date)
    year_str = raw_date[:4] if len(raw_date) >= 4 else str(YEAR)

    backdrop_path = item.get("backdrop_path") or item.get("poster_path")
    backdrop_url = f"https://image.tmdb.org/t/p/w1280{backdrop_path}" if backdrop_path else None

    imdb_id, in_cert, logo_url = get_tmdb_details(media_type, tmdb_id)
    imdb_rating, omdb_rated = get_omdb_info(imdb_id)

    final_imdb = imdb_rating or (f"{item['vote_average']:.1f}" if item.get("vote_average") else None)
    final_age = in_cert or omdb_rated

    out_path = f"docs/images/{img_filename}"
    render_card(
        out_path=out_path,
        backdrop_url=backdrop_url,
        logo_url=logo_url,
        glass_rank=glass_rank,
        release_date_str=date_label,
        age_rating=final_age,
        imdb_rating=final_imdb,
        platform_badge=platform_info["badge"] if platform_info else None,
        platform_color=platform_info["color"] if platform_info else "#FFFFFF",
    )

    desc_parts = []
    if date_label:
        desc_parts.append(f"Release: {date_label}")
    if final_age:
        desc_parts.append(f"Rated: {final_age}")
    if final_imdb:
        desc_parts.append(f"IMDb: {final_imdb}")
    if platform_info:
        desc_parts.append(f"OTT: {platform_info['name']}")

    return {
        "id": imdb_id if imdb_id else f"tmdb:{tmdb_id}",
        "type": stremio_type,
        "name": title,
        "poster": f"{BASE_URL}/images/{img_filename}",
        "posterShape": "landscape",
        "releaseInfo": date_label or year_str,
        "imdbRating": final_imdb,
        "description": " | ".join(desc_parts) if desc_parts else item.get("overview", ""),
    }


def write_catalog(stremio_type, cat_id, cat_name, metas, catalogs_list):
    if not metas:
        return
    with open(f"docs/catalog/{stremio_type}/{cat_id}.json", "w", encoding="utf-8") as f:
        json.dump({"metas": metas}, f, ensure_ascii=False, indent=2)
    catalogs_list.append({
        "type": stremio_type,
        "id": cat_id,
        "name": cat_name,
    })
    print(f"Saved {cat_name} -> {len(metas)} items")


def main():
    print(f"Building India Cinema & OTT Hub for {MONTH_NAME} ({MONTH_START} to {MONTH_END})...")
    provider_map = get_provider_map()
    catalogs_manifest = []

    scrape_titles_from_url("https://in.bookmyshow.com/explore/movies", limit=12)

    now_cinema_url = (
        f"https://api.themoviedb.org/3/discover/movie"
        f"?api_key={TMDB_API_KEY}&region=IN&with_release_type=2|3"
        f"&primary_release_date.gte={MONTH_START}&primary_release_date.lte={MONTH_END}"
        f"&sort_by=popularity.desc&page=1"
    )
    now_cinema_items = fetch_json(now_cinema_url).get("results", [])[:15]
    now_cinema_metas = [
        build_meta_entry(item, "movie", "movie", f"cinema_now_{idx}.jpg", glass_rank=None, platform_info=None)
        for idx, item in enumerate(now_cinema_items, start=1)
    ]
    write_catalog("movie", "cinema_new_releases", f"New Release in Cinema ({MONTH_NAME})", now_cinema_metas, catalogs_manifest)

    upcoming_cinema_url = (
        f"https://api.themoviedb.org/3/discover/movie"
        f"?api_key={TMDB_API_KEY}&region=IN&with_release_type=2|3"
        f"&primary_release_date.gte={NEXT_MONTH_START}"
        f"&sort_by=popularity.desc&page=1"
    )
    upcoming_items = fetch_json(upcoming_cinema_url).get("results", [])[:15]
    upcoming_metas = [
        build_meta_entry(item, "movie", "movie", f"cinema_upcoming_{idx}.jpg", glass_rank=None, platform_info=None)
        for idx, item in enumerate(upcoming_items, start=1)
    ]
    write_catalog("movie", "cinema_upcoming_movies", "Upcoming Movies in Cinema", upcoming_metas, catalogs_manifest)

    for platform in PLATFORMS:
        pkey = platform["key"]
        pids = provider_map.get(pkey)

        new_mov_metas = []
        if pids:
            url = (
                f"https://api.themoviedb.org/3/discover/movie"
                f"?api_key={TMDB_API_KEY}&watch_region=IN&with_watch_providers={pids}"
                f"&primary_release_date.gte={MONTH_START}&primary_release_date.lte={MONTH_END}"
                f"&sort_by=popularity.desc&page=1"
            )
            items = fetch_json(url).get("results", [])[:12]
            for idx, item in enumerate(items, start=1):
                new_mov_metas.append(
                    build_meta_entry(item, "movie", "movie", f"{pkey}_new_movie_{idx}.jpg", glass_rank=None, platform_info=platform)
                )
        write_catalog("movie", f"{pkey}_new_movies", f"{platform['name']} - New Release Movies", new_mov_metas, catalogs_manifest)

        new_ser_metas = []
        if pids:
            url = (
                f"https://api.themoviedb.org/3/discover/tv"
                f"?api_key={TMDB_API_KEY}&watch_region=IN&with_watch_providers={pids}"
                f"&first_air_date.gte={MONTH_START}&first_air_date.lte={MONTH_END}"
                f"&sort_by=popularity.desc&page=1"
            )
            items = fetch_json(url).get("results", [])[:12]
            for idx, item in enumerate(items, start=1):
                new_ser_metas.append(
                    build_meta_entry(item, "tv", "series", f"{pkey}_new_series_{idx}.jpg", glass_rank=None, platform_info=platform)
                )
        write_catalog("series", f"{pkey}_new_series", f"{platform['name']} - New Release Series", new_ser_metas, catalogs_manifest)

        top_mov_items = resolve_platform_titles(platform, "movie", limit=10)
        if not top_mov_items and pids:
            url = (
                f"https://api.themoviedb.org/3/discover/movie"
                f"?api_key={TMDB_API_KEY}&watch_region=IN&with_watch_providers={pids}"
                f"&sort_by=popularity.desc&page=1"
            )
            top_mov_items = fetch_json(url).get("results", [])[:10]

        top_mov_metas = [
            build_meta_entry(item, "movie", "movie", f"{pkey}_top10_movie_{idx}.jpg", glass_rank=idx, platform_info=platform)
            for idx, item in enumerate(top_mov_items, start=1)
        ]
        write_catalog("movie", f"{pkey}_top10_movies", f"{platform['name']} - Top 10 Movies", top_mov_metas, catalogs_manifest)

        top_ser_items = resolve_platform_titles(platform, "tv", limit=10)
        if not top_ser_items and pids:
            url = (
                f"https://api.themoviedb.org/3/discover/tv"
                f"?api_key={TMDB_API_KEY}&watch_region=IN&with_watch_providers={pids}"
                f"&sort_by=popularity.desc&page=1"
            )
            top_ser_items = fetch_json(url).get("results", [])[:10]

        top_ser_metas = [
            build_meta_entry(item, "tv", "series", f"{pkey}_top10_series_{idx}.jpg", glass_rank=idx, platform_info=platform)
            for idx, item in enumerate(top_ser_items, start=1)
        ]
        write_catalog("series", f"{pkey}_top10_series", f"{platform['name']} - Top 10 Series", top_ser_metas, catalogs_manifest)

    manifest = {
        "id": f"com.{GITHUB_USER.lower()}.india.cinema.ott.hub",
        "version": "2.1.0",
        "name": "India Cinema & OTT Hub",
        "description": f"Monthly Cinema & OTT Hub ({MONTH_NAME}) with Glass Top 10, IMDb, Age & Release Dates",
        "resources": ["catalog"],
        "types": ["movie", "series"],
        "catalogs": catalogs_manifest,
        "idPrefixes": ["tt", "tmdb:"],
    }

    with open("docs/manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print("All catalogs and manifest built successfully!")


if __name__ == "__main__":
    main()
      
