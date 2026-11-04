import os
import io
import re
import json
import time
import random
import hashlib
import threading
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin, quote, urlparse

import requests
from bs4 import BeautifulSoup
from urllib3.util import Retry
from requests.adapters import HTTPAdapter

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
    HAS_GOOGLE_API = True
except ImportError:
    HAS_GOOGLE_API = False

BASE_DIR = Path(__file__).resolve().parent
CATALOG_FILE = BASE_DIR / "modeoplus_catalog.mdx"
CATALOG_TMP_FILE = BASE_DIR / "modeoplus_catalog.mdx.tmp"
DRIVE_INDEX_FILE = BASE_DIR / "drive_torrents_index.json"

GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_TORRENTS_FOLDER_ID", "").strip()
GDRIVE_SA_JSON = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()

INDEX_LOCK = threading.Lock()
THREAD_LOCAL = threading.local()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
]

PUBLIC_FAST_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce"
]

def create_resilient_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504], raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retries, pool_connections=16, pool_maxsize=16)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

HTTP_SESSION = create_resilient_session()

def get_headers(referer: Optional[str] = None, is_json: bool = False) -> dict:
    h = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "*/*" if is_json else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9"
    }
    if referer:
        h["Referer"] = referer
        parsed = urlparse(referer)
        h["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    if is_json:
        h["Content-Type"] = "application/json"
    return h

def get_thread_gdrive_service():
    if not HAS_GOOGLE_API or not GDRIVE_SA_JSON:
        return None
    if not hasattr(THREAD_LOCAL, "drive_service"):
        try:
            info = json.loads(GDRIVE_SA_JSON)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/drive"]
            )
            THREAD_LOCAL.drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
        except Exception as e:
            print(f"[GDRIVE-AUTH-ERROR] {e}")
            THREAD_LOCAL.drive_service = None
    return THREAD_LOCAL.drive_service

def upload_torrent_bytes_directly_to_drive(raw_bytes: bytes, file_name: str) -> Optional[str]:
    service = get_thread_gdrive_service()
    if service is None or not GDRIVE_FOLDER_ID:
        return None
    try:
        metadata = {"name": file_name, "parents": [GDRIVE_FOLDER_ID]}
        media = MediaIoBaseUpload(io.BytesIO(raw_bytes), mimetype="application/x-bittorrent", resumable=False)
        created = service.files().create(body=metadata, media_body=media, fields="id").execute()
        file_id = created.get("id")
        if file_id:
            return f"https://drive.google.com/uc?export=download&id={file_id}"
    except Exception as e:
        print(f"[GDRIVE-UPLOAD-WARN] {file_name}: {e}")
    return None

def _bencode_find_info_slice(data: bytes) -> Optional[bytes]:
    marker = b"4:infod"
    idx = data.find(marker)
    if idx == -1:
        return None
    start = idx + 6

    def skip_bencode_value(buf: bytes, pos: int) -> int:
        c = buf[pos:pos + 1]
        if c == b"i":
            return buf.find(b"e", pos + 1) + 1
        elif c in (b"l", b"d"):
            pos += 1
            while pos < len(buf) and buf[pos:pos + 1] != b"e":
                pos = skip_bencode_value(buf, pos)
            return pos + 1
        elif c.isdigit():
            colon = buf.find(b":", pos)
            length = int(buf[pos:colon])
            return colon + 1 + length
        raise ValueError("Invalid bencode")

    try:
        end = skip_bencode_value(data, start)
        return data[start:end]
    except Exception:
        return data[start:-1]

def extract_exact_magnet_from_torrent_bytes(raw_bytes: bytes, title: str) -> Optional[str]:
    try:
        info_slice = _bencode_find_info_slice(raw_bytes)
        if info_slice:
            info_hash = hashlib.sha1(info_slice).hexdigest()
            tr_params = "&".join(f"tr={quote(t)}" for t in PUBLIC_FAST_TRACKERS)
            return f"magnet:?xt=urn:btih:{info_hash}&dn={quote(title)}&{tr_params}"
    except Exception:
        pass
    return None

def solve_pow_nonce(challenge: str, difficulty_zeros: int = 3) -> int:
    prefix = "0" * difficulty_zeros
    for nonce in range(5_000_000):
        if hashlib.sha256(f"{challenge}{nonce}".encode("utf-8")).hexdigest().startswith(prefix):
            return nonce
    return 0

def fetch_torrent_bytes_in_ram(base_page_url: str, content_id: str, tabla: str) -> Optional[bytes]:
    parsed = urlparse(base_page_url)
    api_pow_url = f"{parsed.scheme}://{parsed.netloc}/api_validate_pow.php"
    try:
        HTTP_SESSION.get(base_page_url, headers=get_headers(), timeout=10)
        for tb in [tabla, "peliculas", "series", "episodios", "documentales"]:
            gen = HTTP_SESSION.post(
                api_pow_url,
                json={"action": "generate", "content_id": int(content_id), "tabla": tb},
                headers=get_headers(referer=base_page_url, is_json=True),
                timeout=10
            )
            if gen.status_code != 200:
                continue
            gdata = gen.json()
            if not gdata.get("success") or not gdata.get("challenge"):
                continue
            nonce = solve_pow_nonce(gdata["challenge"], 3)
            val = HTTP_SESSION.post(
                api_pow_url,
                json={"action": "validate", "challenge": gdata["challenge"], "nonce": nonce},
                headers=get_headers(referer=base_page_url, is_json=True),
                timeout=10
            )
            if val.status_code != 200:
                continue
            dl_uri = val.json().get("download_url")
            if dl_uri:
                full_t_url = ("https:" + dl_uri) if dl_uri.startswith("//") else urljoin(base_page_url, dl_uri)
                tr = HTTP_SESSION.get(full_t_url, headers=get_headers(referer=base_page_url), timeout=15)
                if tr.status_code == 200 and len(tr.content) > 50 and not tr.content.lstrip().startswith(b"<"):
                    return tr.content
    except Exception:
        pass
    return None

def resolve_cloud_torrent_url(item_id: str, title: str, page_url: str, cid: str, tabla: str, drive_index: Dict[str, dict]) -> str:
    with INDEX_LOCK:
        if item_id in drive_index:
            saved = drive_index[item_id]
            d_url = saved.get("driveUrl", "")
            mag = saved.get("magnet", "")
            if d_url and mag:
                return f"{d_url}||{mag}"
            if d_url:
                return d_url
            if mag:
                return mag

    if cid and cid.isdigit() and int(cid) > 0:
        time.sleep(random.uniform(0.2, 0.5))
        raw_bytes = fetch_torrent_bytes_in_ram(page_url, cid, tabla)
        if raw_bytes:
            clean_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', title)[:60] + f"_{item_id}.torrent"
            drive_url = upload_torrent_bytes_directly_to_drive(raw_bytes, clean_name)
            magnet_uri = extract_exact_magnet_from_torrent_bytes(raw_bytes, title)
            combined = (
                f"{drive_url}||{magnet_uri}"
                if (drive_url and magnet_uri)
                else (drive_url or magnet_uri or f"{page_url}#protected_dl|id={cid}|tabla={tabla}")
            )
            with INDEX_LOCK:
                drive_index[item_id] = {
                    "itemId": item_id,
                    "title": title,
                    "driveUrl": drive_url or "",
                    "magnet": magnet_uri or "",
                    "updatedAt": int(time.time())
                }
            return combined

    return f"{page_url}#protected_dl|id={cid}|tabla={tabla}"

SPANISH_M3U_SOURCES = [
    "https://iptv-org.github.io/iptv/languages/spa.m3u",
    "https://www.tdtchannels.com/lists/tv.m3u",
    "https://iptv-org.github.io/iptv/countries/do.m3u",
    "https://iptv-org.github.io/iptv/countries/mx.m3u",
    "https://iptv-org.github.io/iptv/countries/es.m3u",
    "https://iptv-org.github.io/iptv/countries/ar.m3u",
    "https://iptv-org.github.io/iptv/countries/co.m3u"
]

DONTORRENT_DOMAINS = [
    "https://dontorrent.diy",
    "https://dontorrent.channel",
    "https://dontorrent.li",
    "https://dontorrent.dad"
]

def scrape_channels() -> List[dict]:
    channels_by_url: Dict[str, dict] = {}
    for repo in SPANISH_M3U_SOURCES:
        try:
            r = HTTP_SESSION.get(repo, headers=get_headers(), timeout=15)
            if r.status_code != 200:
                continue
            name, group, logo = "", "Canales HD (ES)", ""
            for raw in r.text.splitlines():
                line = raw.strip()
                if line.startswith("#EXTINF:"):
                    name = line.rsplit(",", 1)[-1].strip()
                    gm = re.search(r'group-title="([^"]*)"', line, re.I)
                    group = gm.group(1) if gm and gm.group(1) else "Entretenimiento (ES)"
                    lm = re.search(r'tvg-logo="([^"]*)"', line, re.I)
                    logo = lm.group(1) if lm else ""
                elif line.startswith(("http://", "https://")) and name:
                    if line not in channels_by_url:
                        channels_by_url[line] = {
                            "name": name.replace("|", "-"),
                            "category": group.replace("|", "-"),
                            "language": "Español",
                            "poster": logo,
                            "url": line
                        }
                    name = ""
        except Exception:
            pass
    return list(channels_by_url.values())

def save_checkpoint(drive_index: Dict[str, dict], channels: List[dict], movies_map: Dict[str, dict], series_map: Dict[str, dict]):
    with INDEX_LOCK:
        DRIVE_INDEX_FILE.write_text(json.dumps(drive_index, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [f"HEAD|v17.0-PRIME-CLOUD|{int(time.time())}"]
    for i, ch in enumerate(channels, start=1):
        lines.append(f"LIVE|live_{i}|{ch['name']}|{ch['category']}|{ch['language']}|1080p HD|{ch['poster']}|Señal en Vivo|{ch['url']}")
    for m in movies_map.values():
        q_json = json.dumps(m["qualities"], ensure_ascii=False).replace("|", "/")
        lines.append(f"MOVIE|{m['id']}|{m['title']}|{m['releaseDate']}|{m['country']}|{m['rating']}|{m['category']}|{m['poster']}|{m['description']}|{q_json}|false|")
    for s in series_map.values():
        ep_json = json.dumps(s["episodes"], ensure_ascii=False).replace("|", "/")
        lines.append(f"SERIES|{s['id']}|{s['title']}|{s['releaseDate']}|{s['country']}|{s['rating']}|{s['category']}|{s['poster']}|{s['description']}|{ep_json}")

    CATALOG_TMP_FILE.write_text("\n".join(lines), encoding="utf-8")
    CATALOG_TMP_FILE.replace(CATALOG_FILE)

def build_cloud_mdx():
    drive_index: Dict[str, dict] = {}
    if DRIVE_INDEX_FILE.exists():
        try:
            drive_index = json.loads(DRIVE_INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            drive_index = {}

    channels = scrape_channels()
    movies_map: Dict[str, dict] = {}
    series_map: Dict[str, dict] = {}

    if CATALOG_FILE.exists():
        try:
            for line in CATALOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
                p = line.split("|")
                if p[0] == "MOVIE" and len(p) >= 10:
                    nk = re.sub(r"[^a-z0-9]", "", p[2].lower())
                    movies_map[nk] = {
                        "id": p[1], "title": p[2], "releaseDate": p[3], "country": p[4],
                        "rating": p[5], "category": p[6], "poster": p[7], "description": p[8],
                        "qualities": json.loads(p[9])
                    }
                elif p[0] == "SERIES" and len(p) >= 10:
                    nk = re.sub(r"[^a-z0-9]", "", p[2].lower())
                    series_map[nk] = {
                        "id": p[1], "title": p[2], "releaseDate": p[3], "country": p[4],
                        "rating": p[5], "category": p[6], "poster": p[7], "description": p[8],
                        "episodes": json.loads(p[9])
                    }
        except Exception:
            pass

    working_domain = None
    for d in DONTORRENT_DOMAINS:
        try:
            r = HTTP_SESSION.get(d, headers=get_headers(), timeout=8)
            if r.status_code == 200:
                pu = urlparse(r.url)
                working_domain = f"{pu.scheme}://{pu.netloc}"
                break
        except Exception:
            continue

    if working_domain:
        sections = ["/ultimos", "/peliculas", "/peliculas/hd", "/peliculas/4K", "/series", "/series/hd"]
        page_urls = []
        for sec in sections:
            page_urls.append(urljoin(working_domain, sec))
            if sec != "/ultimos":
                for pnum in range(2, 8):
                    page_urls.append(urljoin(working_domain, f"{sec}/page/{pnum}"))

        candidates = []
        seen_urls = set()
        for pg_url in page_urls:
            try:
                resp = HTTP_SESSION.get(pg_url, headers=get_headers(), timeout=12)
                if resp.status_code != 200:
                    continue
                soup = BeautifulSoup(resp.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    full_u = urljoin(working_domain, a["href"].strip())
                    if full_u in seen_urls:
                        continue
                    m = re.search(r"/(pelicula|serie|documental|episodio)/(\d+)(?:/(\d+))?/([^/?#]*)", full_u, re.I)
                    if not m:
                        continue
                    seen_urls.add(full_u)
                    kind = m.group(1).lower()
                    cid = m.group(3) or m.group(2)
                    slug = m.group(4).replace("-", " ").strip()
                    img = a.find("img")
                    poster = ""
                    title = a.get_text(" ", strip=True) or slug
                    year_str = "2026"
                    genre_str = "Películas Prime (ES)" if kind == "pelicula" else "Series Prime (ES)"
                    desc_str = f"Streaming Directo P2P • {title}"
                    quality_str = "1080P HD"

                    if img:
                        src = img.get("src") or ""
                        poster = ("https:" + src) if src.startswith("//") else urljoin(working_domain, src)
                        cid = img.get("data-popover-id") or cid
                        q_m = re.search(r"\[(DVDRip|HDRip|MicroHD|1080p|720p|4K|BluRay)\]", src, re.I)
                        if q_m:
                            quality_str = q_m.group(1).upper()
                        d_cont = img.get("data-content") or ""
                        if d_cont:
                            ps = BeautifulSoup(d_cont, "html.parser")
                            lead = ps.find("p", class_="lead")
                            if lead:
                                title = lead.get_text(strip=True)
                            ym = re.search(r"(\d{4})", ps.get_text())
                            if ym:
                                year_str = ym.group(1)

                    candidates.append((kind, cid, title, poster, year_str, genre_str, desc_str, quality_str, full_u))
            except Exception:
                continue

        processed_counter = 0
        for kind, cid, title, poster, year_str, genre_str, desc_str, quality_str, full_u in candidates:
            norm_k = re.sub(r"[^a-z0-9]", "", title.lower())
            if not norm_k:
                continue
            is_serie = kind in ("serie", "episodio")

            if not is_serie:
                item_id = f"mov_{hashlib.md5(norm_k.encode()).hexdigest()[:10]}"
                cloud_url = resolve_cloud_torrent_url(item_id, title, full_u, cid, "peliculas", drive_index)
                movies_map[norm_k] = {
                    "id": item_id,
                    "title": title,
                    "releaseDate": year_str,
                    "country": "ES / LATAM",
                    "rating": "8.3",
                    "category": genre_str,
                    "poster": poster,
                    "description": desc_str,
                    "qualities": [{
                        "quality": f"{quality_str} [Drive Vault]",
                        "size": "1.8 GB",
                        "torrentUrl": cloud_url,
                        "source": "GoogleDriveVault"
                    }]
                }
            else:
                base_serie = re.sub(r"\s*[-–]?\s*\d+[ªº]?\s*Temporada.*$", "", title, flags=re.I).strip()
                s_key = re.sub(r"[^a-z0-9]", "", base_serie.lower()) or norm_k
                s_id = f"ser_{hashlib.md5(s_key.encode()).hexdigest()[:10]}"
                if s_key not in series_map:
                    series_map[s_key] = {
                        "id": s_id,
                        "title": base_serie or title,
                        "releaseDate": year_str,
                        "country": "ES / LATAM",
                        "rating": "8.6",
                        "category": "Series Prime (ES)",
                        "poster": poster,
                        "description": desc_str,
                        "episodes": []
                    }
                ep_num = len(series_map[s_key]["episodes"]) + 1
                ep_id = f"{s_id}_s1_e{ep_num}"
                cloud_url = resolve_cloud_torrent_url(ep_id, title, full_u, cid, "series", drive_index)
                if not any(e["episodeId"] == ep_id for e in series_map[s_key]["episodes"]):
                    series_map[s_key]["episodes"].append({
                        "episodeId": ep_id,
                        "season": 1,
                        "episode": ep_num,
                        "title": f"Episodio {ep_num} - {title[:50]}",
                        "quality": quality_str,
                        "size": "1.4 GB",
                        "torrentUrl": cloud_url,
                        "isDownloaded": False,
                        "localStreamUrl": ""
                    })

            processed_counter += 1
            if processed_counter % 25 == 0:
                save_checkpoint(drive_index, channels, movies_map, series_map)

    save_checkpoint(drive_index, channels, movies_map, series_map)

if __name__ == "__main__":
    build_cloud_mdx()
