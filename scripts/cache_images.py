from __future__ import annotations

import concurrent.futures
import hashlib
import json
import mimetypes
import re
import sys
import time
from urllib.error import HTTPError
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILE = ROOT / "source-pois.json"
IMAGE_DIR = ROOT / "images" / "poi"
USER_AGENT = "AdFontesEuropa/1.0 (static image cache; Wikimedia attribution manifest)"
COMMONS_FALLBACK_MAX = 10


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
    for query in queries:
        try:
            payload = get_json(project_api_url("commons", {
                "action": "query", "format": "json", "generator": "search",
                "gsrsearch": query, "gsrnamespace": "6", "gsrlimit": "5",
                "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": "1200",
            }))
            pages = list((payload.get("query", {}).get("pages", {}) or {}).values())
            for page in pages:
                info = (page.get("imageinfo") or [{}])[0]
                image_source = info.get("thumburl") or info.get("url") or ""
                if not image_source.startswith("https://upload.wikimedia.org/"):
                    continue
                metadata = info.get("extmetadata", {}) or {}

                def value(key: str) -> str:
                    item = metadata.get(key, {}) or {}
                    return str(item.get("value") or item.get("cleanvalue") or "")

                return {
                    "title": page.get("title", "").removeprefix("File:"),
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
            continue
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
            request = Request(image_url, headers={"User-Agent": USER_AGENT})
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
            delay = float(retry_after) if retry_after.isdigit() else 3.0 * (attempt + 1)
            time.sleep(min(delay, 15.0))
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
        if not image_source and int(result.get("index", 10**9)) < COMMONS_FALLBACK_MAX:
            commons, commons_meta = find_commons_image(row)
            if commons:
                result["sourceTitle"] = commons["title"]
                result["sourcePage"] = commons["sourcePage"]
                image_source = commons["imageSource"]
                result.update(commons_meta)
        result["imageSource"] = image_source
        if not image_source:
            result["status"] = "no-image" if page else "no-wikipedia-page"
            return result
        if not image_source.startswith("https://upload.wikimedia.org/"):
            result["status"] = "non-wikimedia-image"
            return result
        digest = hashlib.sha1(result["key"].encode("utf-8")).hexdigest()[:10]
        prefix = f"poi-{int(result['index']):03d}-{digest}"
        # The Wikimedia thumbnail URL normally carries the final raster type.
        guessed_ext = extension("", image_source)
        destination = IMAGE_DIR / f"{prefix}{guessed_ext}"
        if destination.exists() and destination.stat().st_size:
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
        result.update(image_metadata(image_source))
    except Exception as error:
        result["status"] = "error"
        result["error"] = str(error)
    return result


def main() -> int:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    source = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
    rows = source["rows"]
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(process, row): row for row in rows}
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            result = future.result()
            results.append(result)
            print(f"[{index}/{len(rows)}] {result['status']} {result.get('city', '')} · {result.get('name', '')}", flush=True)
    results.sort(key=lambda row: row.get("index", 0))
    manifest = {
        "schema": "ad-fontes-europa-poi-assets/v1",
        "generatedAt": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "source": "Wikipedia/Wikimedia API (zh.wikipedia.org and upload.wikimedia.org)",
        "notes": [
            "Only images served from upload.wikimedia.org were cached.",
            "Each cached record retains its source page and image URL; license metadata is copied when exposed by the Wikimedia API.",
            "A blank or unknown license field still requires manual attribution review.",
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
