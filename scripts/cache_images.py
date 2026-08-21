from __future__ import annotations

import concurrent.futures
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
import unicodedata
from urllib.error import HTTPError
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILE = ROOT / "source-pois.json"
IMAGE_DIR = ROOT / "images" / "poi"
IMAGE_OVERRIDES_FILE = ROOT / "image-overrides.json"
USER_AGENT = "AdFontesEuropa/1.0 (https://github.com/a15888007552-source/ad-fontes-europa-assets; static image cache; Wikimedia attribution manifest)"
COMMONS_FALLBACK_MAX = 372
BATCH_ONLY = os.environ.get("CACHE_BATCH_ONLY", "0") == "1"
BATCH_START = int(os.environ.get("CACHE_BATCH_START", "0"))
BATCH_END = int(os.environ.get("CACHE_BATCH_END", "10"))
FORCE_REDOWNLOAD = os.environ.get("CACHE_FORCE_REDOWNLOAD", "0") == "1"
BATCH_INDEXES = {
    int(value.strip())
    for value in os.environ.get("CACHE_INDEXES", "").split(",")
    if value.strip().isdigit()
}


DISPLAY_BAD_TERMS = (
    "logo", "seal", "crest", "coat of arms", "emblema", "emblem", "badge",
    "portrait", "bust", "caricature", "catalogue", "catalog", "dictionary",
    "report", "thesis", "manuscript", "inscription", "map", "drawing",
    "painting", "engraving", "scan", "document", "poster", "treatise",
    ".pdf", ".djvu", "thumbnail.png", "mummif", "medal", "organum",
    "altar", "chandelier", "cross", "tomb", "musicians", "festival",
    "performance", "actor", "actress", "concertgoer", "group photo",
)
DISPLAY_STRUCTURAL_TERMS = (
    "building", "facade", "façade", "front", "exterior", "outside", "house",
    "museum", "university", "academy", "conservatory", "school", "college",
    "campus", "faculty", "institute", "hochschule", "universitat", "université",
    "università", "music", "theatre", "theater", "opera", "hall", "church",
    "cathedral", "basilica", "palace", "castle", "walls", "square", "garden",
    "bridge", "tower", "gate", "street", "monument", "statue", "memorial",
)
DISPLAY_GENERIC_TERMS = {
    "the", "of", "and", "for", "in", "at", "de", "di", "da", "del", "la", "le",
    "der", "die", "das", "und", "von", "zu", "house", "home", "music", "school",
    "university", "college", "academy", "institute", "museum", "hall", "building",
}


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).lower()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^\w\s-]", " ", text, flags=re.UNICODE).replace("_", " ")


def text_tokens(value: str) -> set[str]:
    return {
        token for token in re.findall(r"[^\W_]+", normalize_text(value), flags=re.UNICODE)
        if len(token) > 2 and token not in DISPLAY_GENERIC_TERMS
    }


def image_label(image_source: str, source_title: str = "") -> str:
    path_name = unquote(urlparse(image_source or "").path.rsplit("/", 1)[-1])
    return f"{source_title or ''} {path_name}".lower()


def image_relevance(poi: dict, image_source: str, source_title: str = "") -> int:
    label = normalize_text(image_label(image_source, source_title))
    tokens = text_tokens(" ".join(str(poi.get(key) or "") for key in ("city", "name", "wiki")))
    score = sum(3 if token in label else 0 for token in tokens)
    score += sum(1 for term in DISPLAY_STRUCTURAL_TERMS if term in label)
    return score


def is_bad_display_image(poi: dict, image_source: str, source_title: str = "") -> bool:
    label = image_label(image_source, source_title)
    if any(term in label for term in DISPLAY_BAD_TERMS):
        return True
    # A generic university/conservatory/building entry should not silently accept
    # an unrelated place just because the search hit happened to be an image.
    tokens = text_tokens(" ".join(str(poi.get(key) or "") for key in ("city", "name", "wiki")))
    if tokens and image_relevance(poi, image_source, source_title) == 0:
        return True
    return False


def load_image_overrides() -> dict[str, dict]:
    if not IMAGE_OVERRIDES_FILE.exists():
        return {}
    try:
        payload = json.loads(IMAGE_OVERRIDES_FILE.read_text(encoding="utf-8"))
        return {
            str(row.get("index")): row
            for row in (payload.get("rows") or [])
            if row.get("index") is not None
        }
    except Exception:
        return {}


IMAGE_OVERRIDES = load_image_overrides()


def get_json(url: str, timeout: int = 35) -> dict:
    for attempt in range(4):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise
            retry_after = error.headers.get("Retry-After", "")
            delay = float(retry_after) if retry_after.isdigit() else 2.0 * (attempt + 1)
            time.sleep(min(delay, 12.0))


def project_api_url(project: str, params: dict[str, str]) -> str:
    host = "commons.wikimedia.org" if project == "commons" else f"{project}.wikipedia.org"
    return f"https://{host}/w/api.php?" + urlencode(params, doseq=True)


def api_url(params: dict[str, str]) -> str:
    return project_api_url("zh", params)


def resolve_page(poi: dict) -> tuple[dict | None, bool, str]:
    candidates = []
    for project, title in (
        ("zh", poi.get("name")), ("en", poi.get("wiki") or poi.get("name")),
    ):
        if title and (project, title) not in candidates:
            candidates.append((project, title))
    for project, title in candidates:
        try:
            payload = get_json(project_api_url(project, {
                "action": "query", "origin": "*", "format": "json", "redirects": "1",
                "prop": "coordinates|pageimages|extracts|info", "inprop": "url",
                "exintro": "1", "explaintext": "1", "pithumbsize": "1200", "titles": title,
            }))
            pages = list((payload.get("query", {}).get("pages", {}) or {}).values())
            page = pages[0] if pages else None
            if page and not page.get("missing"):
                page["_project"] = project
                return page, False, title
        except Exception:
            continue
    try:
        payload = get_json(api_url({
            "action": "query", "origin": "*", "format": "json", "list": "search",
            "srnamespace": "0", "srlimit": "3", "srsearch": f"{poi.get('city', '')} {poi.get('name', '')}",
        }))
        hits = payload.get("query", {}).get("search", []) or []
        if hits:
            title = hits[0].get("title", "")
            page_payload = get_json(api_url({
                "action": "query", "origin": "*", "format": "json", "redirects": "1",
                "prop": "coordinates|pageimages|extracts|info", "inprop": "url",
                "exintro": "1", "explaintext": "1", "pithumbsize": "1200", "titles": title,
            }))
            pages = list((page_payload.get("query", {}).get("pages", {}) or {}).values())
            page = pages[0] if pages else None
            if page and not page.get("missing"):
                page["_project"] = "zh"
                return page, True, title
    except Exception:
        pass
    return None, False, ""


def find_commons_image(poi: dict) -> tuple[dict | None, dict]:
    queries = []
    for value in (poi.get("wiki"), poi.get("name"), f"{poi.get('city', '')} {poi.get('name', '')}"):
        if value and value not in queries:
            queries.append(value)
    candidates: dict[str, tuple[int, dict, dict]] = {}
    for query in queries:
        try:
            payload = get_json(project_api_url("commons", {
                "action": "query", "format": "json", "generator": "search",
                "gsrsearch": query, "gsrnamespace": "6", "gsrlimit": "20",
                "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": "1200",
            }))
            pages = list((payload.get("query", {}).get("pages", {}) or {}).values())
            for page in pages:
                info = (page.get("imageinfo") or [{}])[0]
                image_source = info.get("thumburl") or info.get("url") or ""
                if not image_source.startswith("https://upload.wikimedia.org/"):
                    continue
                title = page.get("title", "").removeprefix("File:")
                if is_bad_display_image(poi, image_source, title):
                    continue
                score = image_relevance(poi, image_source, title)
                if score == 0 and text_tokens(" ".join(str(poi.get(key) or "") for key in ("city", "name", "wiki"))):
                    continue
                metadata = info.get("extmetadata", {}) or {}

                def value(key: str) -> str:
                    item = metadata.get(key, {}) or {}
                    return str(item.get("value") or item.get("cleanvalue") or "")

                candidate = {
                    "title": title,
                    "sourcePage": info.get("descriptionurl", ""),
                    "imageSource": image_source,
                }
                candidate_meta = {
                    "filePage": info.get("descriptionurl", ""),
                    "license": value("LicenseShortName") or value("UsageTerms"),
                    "usageTerms": value("UsageTerms"),
                    "artist": value("Artist"),
                    "credit": value("Credit"),
                }
                key = candidate["sourcePage"] or candidate["title"]
                # Keep the strongest candidate across all three queries. The old
                # implementation accepted the first search result, which is how
                # logos, scanned books and unrelated images entered the cache.
                if key not in candidates or score > candidates[key][0]:
                    candidates[key] = (score, candidate, candidate_meta)
        except Exception:
            continue
    if candidates:
        _, candidate, candidate_meta = max(candidates.values(), key=lambda item: item[0])
        return candidate, candidate_meta
    return None, {}


def find_commons_file_image(file_title: str) -> tuple[dict | None, dict]:
    """Resolve an explicitly reviewed Commons file title to its thumbnail."""
    try:
        payload = get_json(project_api_url("commons", {
            "action": "query", "format": "json", "titles": f"File:{file_title}",
            "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": "1200",
        }))
        pages = list((payload.get("query", {}).get("pages", {}) or {}).values())
        info = (pages[0].get("imageinfo") or [{}])[0] if pages else {}
        image_source = info.get("thumburl") or info.get("url") or ""
        if not image_source.startswith("https://upload.wikimedia.org/"):
            return None, {}
        metadata = info.get("extmetadata", {}) or {}

        def value(key: str) -> str:
            item = metadata.get(key, {}) or {}
            return str(item.get("value") or item.get("cleanvalue") or "")

        return {
            "title": file_title,
            "sourcePage": info.get("descriptionurl", ""),
            "imageSource": image_source,
        }, {
            "filePage": info.get("descriptionurl", ""),
            "license": value("LicenseShortName") or value("UsageTerms"),
            "usageTerms": value("UsageTerms"),
            "artist": value("Artist"),
            "credit": value("Credit"),
        }
    except Exception:
        return None, {}


def image_metadata(image_url: str) -> dict:
    try:
        parts = [part for part in urlparse(image_url).path.strip("/").split("/") if part]
        index = parts.index("wikipedia")
        project = parts[index + 1]
        host = "commons.wikimedia.org" if project == "commons" else f"{project}.wikipedia.org"
        file_name = re.sub(r"^\d+px-", "", unquote(parts[-1])).replace("_", " ")
        query = urlencode({
            "action": "query", "format": "json", "prop": "imageinfo",
            "iiprop": "url|extmetadata", "titles": f"File:{file_name}",
        })
        payload = get_json(f"https://{host}/w/api.php?{query}")
        pages = list((payload.get("query", {}).get("pages", {}) or {}).values())
        info_list = pages[0].get("imageinfo", []) if pages else []
        info = info_list[0] if info_list else {}
        meta = info.get("extmetadata", {}) or {}

        def value(key: str) -> str:
            item = meta.get(key, {}) or {}
            return str(item.get("value") or item.get("cleanvalue") or "")

        return {
            "filePage": info.get("descriptionurl", ""),
            "license": value("LicenseShortName") or value("UsageTerms"),
            "usageTerms": value("UsageTerms"),
            "artist": value("Artist"),
            "credit": value("Credit"),
        }
    except Exception:
        return {}


def extension(content_type: str, image_url: str) -> str:
    value = (content_type or "").split(";", 1)[0].lower()
    table = {
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
        "image/webp": ".webp", "image/avif": ".avif", "image/gif": ".gif",
    }
    if value in table:
        return table[value]
    guessed = Path(urlparse(image_url).path).suffix.lower()
    return guessed if guessed in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"} else ".jpg"


def download_image(image_url: str, destination: Path) -> tuple[str, int]:
    for attempt in range(4):
        try:
            if attempt:
                time.sleep(min(12.0 * attempt, 45.0))
            request = Request(image_url, headers={
                "User-Agent": USER_AGENT,
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Referer": "https://commons.wikimedia.org/",
            })
            with urlopen(request, timeout=60) as response:
                content_type = response.headers.get("Content-Type", "")
                if not content_type.lower().split(";", 1)[0].startswith("image/"):
                    raise RuntimeError(f"not an image: {content_type or 'unknown'}")
                data = response.read()
            break
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise
            retry_after = error.headers.get("Retry-After", "")
            try:
                server_delay = float(retry_after)
            except ValueError:
                server_delay = 0.0
            delay = max(server_delay, 12.0 * (attempt + 1))
            time.sleep(min(delay, 60.0))
    if not data:
        raise RuntimeError("empty image")
    destination.write_bytes(data)
    return content_type, len(data)


def process(row: dict) -> dict:
    result = dict(row)
    result["key"] = result.get("key") or f"{result.get('city', '')}|{result.get('name', '')}"
    result["asset"] = None
    result["status"] = "pending"
    try:
        page, searched, query_title = resolve_page(row)
        image_source = ""
        if page:
            result["sourceTitle"] = page.get("title") or query_title
            project = page.get("_project", "zh")
            result["sourcePage"] = page.get("fullurl") or f"https://{project}.wikipedia.org/wiki/" + quote(result["sourceTitle"])
            result["searchUsed"] = searched
            result["extract"] = page.get("extract", "")
            coordinates = (page.get("coordinates") or [None])[0]
            if coordinates:
                result["coordinates"] = {"lat": coordinates.get("lat"), "lon": coordinates.get("lon")}
            image = page.get("thumbnail") or page.get("originalimage") or {}
            image_source = image.get("source", "")

        override = IMAGE_OVERRIDES.get(str(result.get("index")))
        if override and override.get("commonsFile"):
            commons, commons_meta = find_commons_file_image(str(override["commonsFile"]))
            if commons:
                result["sourceTitle"] = commons["title"]
                result["sourcePage"] = commons["sourcePage"]
                image_source = commons["imageSource"]
                result.update(commons_meta)
                result["imageOverride"] = str(override["commonsFile"])
            else:
                result["overrideError"] = f"Commons file not resolved: {override['commonsFile']}"

        if image_source and is_bad_display_image(row, image_source, result.get("sourceTitle", "")):
            result["rejectedImage"] = {
                "sourceTitle": result.get("sourceTitle", ""),
                "imageSource": image_source,
                "reason": "not a suitable real-world landmark photo",
            }
            image_source = ""
        if not image_source and int(result.get("index", 10**9)) < COMMONS_FALLBACK_MAX:
            commons, commons_meta = find_commons_image(row)
            if commons:
                result["sourceTitle"] = commons["title"]
                result["sourcePage"] = commons["sourcePage"]
                image_source = commons["imageSource"]
                result.update(commons_meta)
        result["imageSource"] = image_source
        if not image_source:
            result["status"] = "no-suitable-image" if page else "no-wikipedia-page"
            return result
        if not image_source.startswith("https://upload.wikimedia.org/"):
            result["status"] = "non-wikimedia-image"
            return result
        digest = hashlib.sha1(result["key"].encode("utf-8")).hexdigest()[:10]
        prefix = f"poi-{int(result['index']):03d}-{digest}"
        # The Wikimedia thumbnail URL normally carries the final raster type.
        guessed_ext = extension("", image_source)
        destination = IMAGE_DIR / f"{prefix}{guessed_ext}"
        # The asset filename is derived from the POI key, not from the source
        # image URL.  When a logo/portrait/scan is replaced by a reviewed
        # Commons exterior, the destination path therefore stays the same.
        # Do not silently keep stale bytes when the workflow explicitly asks
        # for a refresh.
        if destination.exists() and destination.stat().st_size and not FORCE_REDOWNLOAD:
            content_type = mimetypes.guess_type(destination.name)[0] or "image/jpeg"
            size = destination.stat().st_size
        else:
            content_type, size = download_image(image_source, destination)
            actual = IMAGE_DIR / f"{prefix}{extension(content_type, image_source)}"
            if actual != destination:
                destination.replace(actual)
                destination = actual
        result["asset"] = destination.relative_to(ROOT).as_posix()
        result["downloadURL"] = image_source
        result["bytes"] = size
        result["contentType"] = content_type
        result["status"] = "downloaded"
        try:
            result.update(image_metadata(image_source))
        except Exception as metadata_error:
            result["metadataError"] = str(metadata_error)
    except Exception as error:
        result["status"] = "error"
        result["error"] = str(error)
    return result


def main() -> int:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    source = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
    rows = source["rows"]
    existing_manifest = {}
    existing_path = ROOT / "manifest.json"
    if existing_path.exists():
        try:
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
            existing_manifest = {row.get("key") or f"{row.get('city', '')}|{row.get('name', '')}": row for row in existing.get("rows", [])}
        except Exception:
            existing_manifest = {}
    target_rows = rows
    if BATCH_INDEXES:
        target_rows = [row for row in rows if int(row.get("index", 0)) in BATCH_INDEXES]
    elif BATCH_ONLY:
        target_rows = [row for row in rows if BATCH_START <= int(row.get("index", 0)) < BATCH_END]
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        futures = {executor.submit(process, row): row for row in target_rows}
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            result = future.result()
            results.append(result)
            print(f"[{index}/{len(target_rows)}] {result['status']} {result.get('city', '')} · {result.get('name', '')}", flush=True)
    if BATCH_ONLY:
        for result in results:
            existing_manifest[result["key"]] = result
        results = list(existing_manifest.values())
    results.sort(key=lambda row: row.get("index", 0))
    manifest = {
        "schema": "ad-fontes-europa-poi-assets/v1",
        "generatedAt": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "source": "Wikipedia/Wikimedia API (zh.wikipedia.org and upload.wikimedia.org)",
        "notes": [
            "Only images served from upload.wikimedia.org were cached.",
            "Each cached record retains its source page and image URL; license metadata is copied when exposed by the Wikimedia API.",
            "A blank or unknown license field still requires manual attribution review.",
            "Display images are filtered for obvious logos, portraits, scans, documents, objects and unrelated search results; manually reviewed Commons overrides are recorded in image-overrides.json.",
        ],
        "total": len(results),
        "downloaded": sum(result.get("status") == "downloaded" for result in results),
        "rows": results,
    }
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "total": len(results),
        "downloaded": manifest["downloaded"],
        "noPage": sum(result.get("status") == "no-wikipedia-page" for result in results),
        "noImage": sum(result.get("status") == "no-image" for result in results),
        "nonWikimedia": sum(result.get("status") == "non-wikimedia-image" for result in results),
        "errors": sum(result.get("status") == "error" for result in results),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
