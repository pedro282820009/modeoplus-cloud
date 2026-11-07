import os
import re
import json
import time
import math
import random
import hashlib
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, quote, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from urllib3.util import Retry
from requests.adapters import HTTPAdapter

BASE_DIR = Path(__file__).resolve().parent
CATALOG_FILE = BASE_DIR / "modeoplus_catalog.mdx"
CATALOG_TMP_FILE = BASE_DIR / "modeoplus_catalog.mdx.tmp"
DRIVE_INDEX_FILE = BASE_DIR / "drive_torrents_index.json"
TORRENTS_VAULT_DIR = BASE_DIR / "torrents_vault"
SHARDS_DIR = BASE_DIR / "catalog_shards"

TORRENTS_VAULT_DIR.mkdir(parents=True, exist_ok=True)
SHARDS_DIR.mkdir(parents=True, exist_ok=True)

GITHUB_REPO_FULL = os.environ.get("GITHUB_REPO_FULL", "pedro282820009/modeoplus-cloud").strip()
MAX_PAGES_PER_SECTION = int(os.environ.get("MAX_PAGES_PER_SECTION", "35"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "20"))
MIN_SHARDS_COUNT = int(os.environ.get("MIN_SHARDS_COUNT", "20"))

INDEX_LOCK = threading.Lock()
MAP_LOCK = threading.Lock()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0"
]

PUBLIC_FAST_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.moeking.me:6969/announce",
    "udp://explodie.org:6969/announce",
    "http://tracker.opentrackr.org:1337/announce"
]

SPANISH_M3U_SOURCES = [
    "https://iptv-org.github.io/iptv/languages/spa.m3u",
    "https://www.tdtchannels.com/lists/tv.m3u",
    "https://iptv-org.github.io/iptv/countries/do.m3u",
    "https://iptv-org.github.io/iptv/countries/mx.m3u",
    "https://iptv-org.github.io/iptv/countries/es.m3u",
    "https://iptv-org.github.io/iptv/countries/ar.m3u",
    "https://iptv-org.github.io/iptv/countries/co.m3u",
    "https://iptv-org.github.io/iptv/countries/cl.m3u",
    "https://iptv-org.github.io/iptv/countries/pe.m3u",
    "https://iptv-org.github.io/iptv/countries/us.m3u"
]

DONTORRENT_DOMAINS = [
    "https://dontorrent.diy",
    "https://dontorrent.channel",
    "https://dontorrent.li",
    "https://dontorrent.dad",
    "https://dontorrent.org",
    "https://dontorrent.wf",
    "https://dontorrent.tel"
]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def create_resilient_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=4,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=64, pool_maxsize=64)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


HTTP_SESSION = create_resilient_session()


def get_headers(referer: Optional[str] = None, is_json: bool = False) -> dict:
    h = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, */*" if is_json else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Connection": "keep-alive"
    }
    if referer:
        h["Referer"] = referer
        parsed = urlparse(referer)
        if parsed.scheme and parsed.netloc:
            h["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
    if is_json:
        h["Content-Type"] = "application/json"
    return h


def save_torrent_bytes_to_github_vault(raw_bytes: bytes, item_id: str) -> str:
    filename = f"{item_id}.torrent"
    vault_path = TORRENTS_VAULT_DIR / filename
    vault_path.write_bytes(raw_bytes)
    return f"https://raw.githubusercontent.com/{GITHUB_REPO_FULL}/main/torrents_vault/{filename}"


def _bencode_find_info_slice(data: bytes) -> Optional[bytes]:
    marker = b"4:infod"
    idx = data.find(marker)
    if idx == -1:
        return None
    start = idx + 6

    def skip_bencode_value(buf: bytes, pos: int) -> int:
        c = buf[pos:pos + 1]
        if c == b"i":
            end_i = buf.find(b"e", pos + 1)
            if end_i == -1:
                raise ValueError("Unterminated integer")
            return end_i + 1
        elif c in (b"l", b"d"):
            pos += 1
            while pos < len(buf) and buf[pos:pos + 1] != b"e":
                pos = skip_bencode_value(buf, pos)
            return pos + 1
        elif c.isdigit():
            colon = buf.find(b":", pos)
            if colon == -1:
                raise ValueError("Missing colon in string")
            length = int(buf[pos:colon])
            return colon + 1 + length
        raise ValueError("Invalid bencode token")

    try:
        end = skip_bencode_value(data, start)
        return data[start:end]
    except Exception:
        return data[start:-1]


def extract_torrent_metadata(raw_bytes: bytes, title: str) -> Tuple[Optional[str], str]:
    magnet_uri = None
    size_str = "1.8 GB"
    try:
        info_slice = _bencode_find_info_slice(raw_bytes)
        if info_slice:
            info_hash = hashlib.sha1(info_slice).hexdigest()
            tr_params = "&".join(f"tr={quote(t)}" for t in PUBLIC_FAST_TRACKERS)
            magnet_uri = f"magnet:?xt=urn:btih:{info_hash}&dn={quote(title)}&{tr_params}"

            lengths = [int(m.group(1)) for m in re.finditer(rb"6:lengthi(\d+)e", info_slice)]
            if lengths:
                total_bytes = sum(lengths)
                gb = total_bytes / (1024 ** 3)
                if gb >= 0.95:
                    size_str = f"{gb:.2f} GB"
                else:
                    mb = max(50.0, total_bytes / (1024 ** 2))
                    size_str = f"{mb:.0f} MB"
    except Exception:
        pass
    return magnet_uri, size_str


def solve_pow_nonce(challenge: str, difficulty_zeros: int = 3) -> int:
    prefix = "0" * difficulty_zeros
    c_bytes = challenge.encode("utf-8")
    for nonce in range(6_000_000):
        digest = hashlib.sha256(c_bytes + str(nonce).encode("ascii")).hexdigest()
        if digest.startswith(prefix):
            return nonce
    return 0


def fetch_torrent_bytes_in_ram(base_page_url: str, content_id: str, tabla: str) -> Optional[bytes]:
    parsed = urlparse(base_page_url)
    api_pow_url = f"{parsed.scheme}://{parsed.netloc}/api_validate_pow.php"
    try:
        page_resp = HTTP_SESSION.get(base_page_url, headers=get_headers(), timeout=10)
        if page_resp.status_code == 200:
            soup = BeautifulSoup(page_resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if href.endswith(".torrent") or "/descargar/" in href:
                    direct_u = ("https:" + href) if href.startswith("//") else urljoin(base_page_url, href)
                    tr_direct = HTTP_SESSION.get(direct_u, headers=get_headers(referer=base_page_url), timeout=10)
                    if tr_direct.status_code == 200 and len(tr_direct.content) > 64 and tr_direct.content[0:1] == b"d":
                        return tr_direct.content

        tables_order = [tabla] + [t for t in ["peliculas", "series", "episodios", "documentales"] if t != tabla]
        for tb in tables_order:
            gen = HTTP_SESSION.post(
                api_pow_url,
                json={"action": "generate", "content_id": int(content_id), "tabla": tb},
                headers=get_headers(referer=base_page_url, is_json=True),
                timeout=9
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
                timeout=9
            )
            if val.status_code != 200:
                continue
            dl_uri = val.json().get("download_url")
            if dl_uri:
                full_t_url = ("https:" + dl_uri) if dl_uri.startswith("//") else urljoin(base_page_url, dl_uri)
                tr = HTTP_SESSION.get(full_t_url, headers=get_headers(referer=base_page_url), timeout=14)
                if tr.status_code == 200 and len(tr.content) > 50 and tr.content[0:1] == b"d":
                    return tr.content
    except Exception:
        pass
    return None


def resolve_cloud_torrent_url(
    item_id: str,
    title: str,
    page_url: str,
    cid: str,
    tabla: str,
    drive_index: Dict[str, dict]
) -> Tuple[Optional[str], str]:
    vault_file = TORRENTS_VAULT_DIR / f"{item_id}.torrent"

    with INDEX_LOCK:
        if item_id in drive_index:
            saved = drive_index[item_id]
            d_url = saved.get("driveUrl", "").strip()
            mag = saved.get("magnet", "").strip()
            saved_size = saved.get("size", "1.8 GB").strip() or "1.8 GB"
            if vault_file.exists():
                d_url = f"https://raw.githubusercontent.com/{GITHUB_REPO_FULL}/main/torrents_vault/{item_id}.torrent"
                saved["driveUrl"] = d_url
                if not mag:
                    mag, saved_size = extract_torrent_metadata(vault_file.read_bytes(), title)
                    saved["magnet"] = mag or ""
                    saved["size"] = saved_size
                return (f"{d_url}||{mag}" if mag else d_url), saved_size
            if d_url.startswith("https://raw.githubusercontent.com/") and mag:
                return f"{d_url}||{mag}", saved_size

    if vault_file.exists():
        raw_existing = vault_file.read_bytes()
        github_url = f"https://raw.githubusercontent.com/{GITHUB_REPO_FULL}/main/torrents_vault/{item_id}.torrent"
        mag, computed_size = extract_torrent_metadata(raw_existing, title)
        combined = f"{github_url}||{mag}" if mag else github_url
        with INDEX_LOCK:
            drive_index[item_id] = {
                "itemId": item_id,
                "title": title,
                "driveUrl": github_url,
                "magnet": mag or "",
                "size": computed_size,
                "updatedAt": int(time.time())
            }
        return combined, computed_size

    if cid and cid.isdigit() and int(cid) > 0:
        time.sleep(random.uniform(0.05, 0.20))
        raw_bytes = fetch_torrent_bytes_in_ram(page_url, cid, tabla)
        if raw_bytes:
            github_torrent_url = save_torrent_bytes_to_github_vault(raw_bytes, item_id)
            magnet_uri, computed_size = extract_torrent_metadata(raw_bytes, title)
            combined = f"{github_torrent_url}||{magnet_uri}" if magnet_uri else github_torrent_url
            with INDEX_LOCK:
                drive_index[item_id] = {
                    "itemId": item_id,
                    "title": title,
                    "driveUrl": github_torrent_url,
                    "magnet": magnet_uri or "",
                    "size": computed_size,
                    "updatedAt": int(time.time())
                }
            log(f"Vault OK: {item_id}.torrent ({computed_size}) -> {title[:42]}")
            return combined, computed_size

    with INDEX_LOCK:
        if item_id in drive_index:
            mag = drive_index[item_id].get("magnet", "").strip()
            saved_size = drive_index[item_id].get("size", "1.6 GB")
            if mag:
                return mag, saved_size

    return None, "1.6 GB"


def categorize_channel(name: str, raw_group: str) -> str:
    text = f"{name} {raw_group}".lower()
    if any(k in text for k in ["deporte", "sport", "espn", "fox sports", "dazn", "gol", "tyc", "tudn", "win", "liga", "futbol"]):
        return "Deportes en Vivo"
    if any(k in text for k in ["noticia", "news", "24h", "cnn", "rt ", "telesur", "dw", "euronews", "ntn24", "adn", "sin ", "cdn"]):
        return "Noticias 24/7"
    if any(k in text for k in ["cine", "movie", "film", "hbo", "star", "tnt", "space", "golden", "paramount", "amc", "syfy", "axn"]):
        return "Cine & Series TV"
    if any(k in text for k in ["kids", "infantil", "cartoon", "nick", "disney", "clan", "boing", "baby", "junior", "discovery kids"]):
        return "Infantil & Anime"
    if any(k in text for k in ["music", "música", "mtv", "htv", "telehit", "vh1", "radio", "urbano", "bachata", "salsa"]):
        return "Música & Conciertos"
    if any(k in text for k in ["docu", "history", "natgeo", "national geographic", "discovery", "animal", "ciencia", "odisea"]):
        return "Documentales & Cultura"
    if any(k in text for k in ["telemicro", "color vision", "telesistema", "antena 7", "teleantillas", "coral", "dominican", "rd"]):
        return "República Dominicana"
    return "Entretenimiento HD"


def scrape_channels() -> List[dict]:
    channels_by_url: Dict[str, dict] = {}
    for repo in SPANISH_M3U_SOURCES:
        try:
            r = HTTP_SESSION.get(repo, headers=get_headers(), timeout=15)
            if r.status_code != 200:
                continue
            name, group, logo = "", "Canales HD", ""
            for raw in r.text.splitlines():
                line = raw.strip()
                if line.startswith("#EXTINF:"):
                    name = line.rsplit(",", 1)[-1].strip()
                    gm = re.search(r'group-title="([^"]*)"', line, re.I)
                    group = gm.group(1) if gm and gm.group(1) else "Entretenimiento"
                    lm = re.search(r'tvg-logo="([^"]*)"', line, re.I)
                    logo = lm.group(1) if lm else ""
                elif line.startswith(("http://", "https://")) and name:
                    clean_url = line.split("|")[0].strip()
                    if clean_url not in channels_by_url and (".m3u8" in clean_url.lower() or ".ts" in clean_url.lower() or "live" in clean_url.lower()):
                        clean_name = re.sub(r"\[.*?\]|\(.*?\)", "", name).replace("|", "-").strip()
                        if len(clean_name) >= 2:
                            channels_by_url[clean_url] = {
                                "name": clean_name,
                                "category": categorize_channel(clean_name, group),
                                "language": "Español",
                                "poster": logo,
                                "url": clean_url
                            }
                    name = ""
        except Exception:
            pass
    return list(channels_by_url.values())


def parse_season_episode(title: str, url: str) -> Tuple[int, int]:
    combined = f"{title} {url}"
    m1 = re.search(r"(\d{1,2})\s*[xX]\s*(\d{1,3})", combined)
    if m1:
        return max(1, int(m1.group(1))), max(1, int(m1.group(2)))
    m2 = re.search(r"[Ss](\d{1,2})\s*[Ee](\d{1,3})", combined)
    if m2:
        return max(1, int(m2.group(1))), max(1, int(m2.group(2)))
    m3 = re.search(r"(\d{1,2})[ªº]?\s*Temporada", combined, re.I)
    if m3:
        return max(1, int(m3.group(1))), 1
    return 1, 0


def write_catalog_shards(channels: List[dict], movies_list: List[dict], series_list: List[dict]) -> int:
    """
    Genera más de 16 archivos fragmentados (shards) en catalog_shards/ dentro del servidor GitHub
    para carga paralela ultrarrápida desde Android TV sin bloqueos de memoria.
    """
    for old_file in SHARDS_DIR.glob("*.json"):
        try:
            old_file.unlink()
        except Exception:
            pass

    all_records = []
    for i, ch in enumerate(channels, start=1):
        all_records.append({"type": "LIVE", "id": f"live_{i}", "data": ch})
    for m in movies_list:
        all_records.append({"type": "MOVIE", "id": m["id"], "data": m})
    for s in series_list:
        if s.get("episodes"):
            all_records.append({"type": "SERIES", "id": s["id"], "data": s})

    total_items = len(all_records)
    target_shards = max(MIN_SHARDS_COUNT, math.ceil(total_items / 120)) if total_items > 0 else MIN_SHARDS_COUNT
    chunk_size = max(1, math.ceil(total_items / target_shards)) if total_items > 0 else 1

    shard_files_meta = []
    shard_index = 0

    for idx in range(0, max(1, total_items), chunk_size):
        chunk = all_records[idx:idx + chunk_size]
        shard_Index_name = f"shard_{shard_index:02d}.json"
        shard_path = SHARDS_DIR / shard_Index_name
        payload = {
            "shardId": shard_index,
            "count": len(chunk),
            "updatedAt": int(time.time()),
            "items": chunk
        }
        shard_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        shard_files_meta.append({
            "shardId": shard_index,
            "file": shard_Index_name,
            "url": f"https://raw.githubusercontent.com/{GITHUB_REPO_FULL}/main/catalog_shards/{shard_Index_name}",
            "count": len(chunk)
        })
        shard_index += 1

    while shard_index < MIN_SHARDS_COUNT:
        shard_Index_name = f"shard_{shard_index:02d}.json"
        shard_path = SHARDS_DIR / shard_Index_name
        payload = {
            "shardId": shard_index,
            "count": 0,
            "updatedAt": int(time.time()),
            "items": []
        }
        shard_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        shard_files_meta.append({
            "shardId": shard_index,
            "file": shard_Index_name,
            "url": f"https://raw.githubusercontent.com/{GITHUB_REPO_FULL}/main/catalog_shards/{shard_Index_name}",
            "count": 0
        })
        shard_index += 1

    manifest = {
        "version": "v19.0-GITHUB-SHARDED",
        "updatedAt": int(time.time()),
        "totalShards": len(shard_files_meta),
        "totals": {
            "channels": len(channels),
            "movies": len(movies_list),
            "series": len(series_list)
        },
        "shards": shard_files_meta
    }
    (SHARDS_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(shard_files_meta)


def save_checkpoint(
    drive_index: Dict[str, dict],
    channels: List[dict],
    movies_map: Dict[str, dict],
    series_map: Dict[str, dict]
) -> None:
    with INDEX_LOCK:
        DRIVE_INDEX_FILE.write_text(json.dumps(drive_index, ensure_ascii=False, indent=2), encoding="utf-8")

    with MAP_LOCK:
        movies_list = list(movies_map.values())
        series_list = [s for s in series_map.values() if s.get("episodes")]

    lines = [f"HEAD|v19.0-GITHUB-VAULT|{int(time.time())}"]
    for i, ch in enumerate(channels, start=1):
        lines.append(
            f"LIVE|live_{i}|{ch['name']}|{ch['category']}|{ch['language']}|1080p HD|{ch['poster']}|Señal en Vivo|{ch['url']}"
        )
    for m in movies_list:
        q_json = json.dumps(m["qualities"], ensure_ascii=False).replace("|", "/")
        lines.append(
            f"MOVIE|{m['id']}|{m['title']}|{m['releaseDate']}|{m['country']}|{m['rating']}|{m['category']}|{m['poster']}|{m['description']}|{q_json}|false|"
        )
    for s in series_list:
        ep_json = json.dumps(s["episodes"], ensure_ascii=False).replace("|", "/")
        lines.append(
            f"SERIES|{s['id']}|{s['title']}|{s['releaseDate']}|{s['country']}|{s['rating']}|{s['category']}|{s['poster']}|{s['description']}|{ep_json}"
        )

    CATALOG_TMP_FILE.write_text("\n".join(lines), encoding="utf-8")
    CATALOG_TMP_FILE.replace(CATALOG_FILE)
    shards_written = write_catalog_shards(channels, movies_list, series_list)
    log(f"Checkpoint guardado: {len(movies_list)} películas, {len(series_list)} series, {shards_written} shards en GitHub.")


def scrape_listing_page(pg_url: str, working_domain: str) -> List[tuple]:
    found = []
    try:
        resp = HTTP_SESSION.get(pg_url, headers=get_headers(referer=working_domain), timeout=10)
        if resp.status_code != 200:
            return found
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            full_u = urljoin(working_domain, a["href"].strip())
            m = re.search(r"/(pelicula|serie|documental|episodio)/(\d+)(?:/(\d+))?/([^/?#]*)", full_u, re.I)
            if not m:
                continue
            kind = m.group(1).lower()
            cid = m.group(3) or m.group(2)
            slug = m.group(4).replace("-", " ").strip()
            img = a.find("img")
            poster = ""
            title = a.get_text(" ", strip=True) or slug
            year_str = "2026"
            genre_str = "Películas Prime" if kind == "pelicula" else ("Documentales" if kind == "documental" else "Series Prime")
            quality_str = "1080P HD"

            if img:
                src = img.get("src") or ""
                poster = ("https:" + src) if src.startswith("//") else urljoin(working_domain, src)
                cid = img.get("data-popover-id") or cid
                q_m = re.search(r"\[(DVDRip|HDRip|MicroHD|1080p|720p|4K|2160p|BluRay)\]", src, re.I)
                if q_m:
                    quality_str = q_m.group(1).upper()
                d_cont = img.get("data-content") or ""
                if d_cont:
                    ps = BeautifulSoup(d_cont, "html.parser")
                    lead = ps.find("p", class_="lead")
                    if lead:
                        title = lead.get_text(strip=True)
                    ym = re.search(r"\b(19\d{2}|20\d{2})\b", ps.get_text())
                    if ym:
                        year_str = ym.group(1)

            if "4k" in pg_url.lower() or "4k" in title.lower() or "2160p" in title.lower():
                quality_str = "4K UHD"
                if kind == "pelicula":
                    genre_str = "Cine 4K UHD"

            desc_str = f"Streaming Directo P2P • {quality_str} • {title}"
            found.append((kind, cid, title, poster, year_str, genre_str, desc_str, quality_str, full_u))
    except Exception:
        pass
    return found


def process_single_candidate(
    candidate: tuple,
    drive_index: Dict[str, dict],
    movies_map: Dict[str, dict],
    series_map: Dict[str, dict]
) -> bool:
    kind, cid, title, poster, year_str, genre_str, desc_str, quality_str, full_u = candidate
    norm_k = re.sub(r"[^a-z0-9]", "", title.lower())
    if not norm_k:
        return False

    is_serie = kind in ("serie", "episodio")
    if not is_serie:
        item_id = f"mov_{hashlib.md5(norm_k.encode()).hexdigest()[:10]}"
        cloud_url, real_size = resolve_cloud_torrent_url(item_id, title, full_u, cid, "peliculas", drive_index)
        if not cloud_url:
            return False
        new_q = {
            "quality": f"{quality_str} [GitHub Vault]",
            "size": real_size,
            "torrentUrl": cloud_url,
            "source": "GitHubVault"
        }
        with MAP_LOCK:
            if norm_k in movies_map:
                existing_quals = movies_map[norm_k].get("qualities", [])
                if not any(q.get("torrentUrl") == cloud_url for q in existing_quals):
                    existing_quals.append(new_q)
                if poster and not movies_map[norm_k].get("poster"):
                    movies_map[norm_k]["poster"] = poster
            else:
                movies_map[norm_k] = {
                    "id": item_id,
                    "title": title,
                    "releaseDate": year_str,
                    "country": "ES / LATAM",
                    "rating": "8.5",
                    "category": genre_str,
                    "poster": poster,
                    "description": desc_str,
                    "qualities": [new_q]
                }
        return True
    else:
        base_serie = re.sub(r"\s*[-–]?\s*\d+[ªº]?\s*Temporada.*$", "", title, flags=re.I)
        base_serie = re.sub(r"\s*[-–]?\s*\d{1,2}[xX]\d{1,3}.*$", "", base_serie).strip()
        s_key = re.sub(r"[^a-z0-9]", "", base_serie.lower()) or norm_k
        s_id = f"ser_{hashlib.md5(s_key.encode()).hexdigest()[:10]}"
        season_num, parsed_ep = parse_season_episode(title, full_u)

        with MAP_LOCK:
            if s_key not in series_map:
                series_map[s_key] = {
                    "id": s_id,
                    "title": base_serie or title,
                    "releaseDate": year_str,
                    "country": "ES / LATAM",
                    "rating": "8.7",
                    "category": "Series Prime",
                    "poster": poster,
                    "description": desc_str,
                    "episodes": []
                }
            current_eps = series_map[s_key]["episodes"]
            ep_num = parsed_ep if parsed_ep > 0 else (len(current_eps) + 1)
            ep_id = f"{s_id}_s{season_num}_e{ep_num}"

        cloud_url, real_size = resolve_cloud_torrent_url(ep_id, title, full_u, cid, "series", drive_index)
        if not cloud_url:
            return False

        with MAP_LOCK:
            target_eps = series_map[s_key]["episodes"]
            if not any(e["episodeId"] == ep_id for e in target_eps):
                target_eps.append({
                    "episodeId": ep_id,
                    "season": season_num,
                    "episode": ep_num,
                    "title": f"T{season_num}:E{ep_num} - {title[:55]}",
                    "quality": quality_str,
                    "size": real_size,
                    "torrentUrl": cloud_url,
                    "isDownloaded": False,
                    "localStreamUrl": ""
                })
                target_eps.sort(key=lambda x: (x.get("season", 1), x.get("episode", 1)))
        return True


def build_cloud_mdx() -> None:
    log("Iniciando sincronización Modeo+ Cloud v19.0 (Multi-Thread + >16 GitHub Shards)...")
    drive_index: Dict[str, dict] = {}
    if DRIVE_INDEX_FILE.exists():
        try:
            drive_index = json.loads(DRIVE_INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            drive_index = {}

    channels = scrape_channels()
    log(f"Canales M3U recolectados: {len(channels)}")
    movies_map: Dict[str, dict] = {}
    series_map: Dict[str, dict] = {}

    if CATALOG_FILE.exists():
        try:
            for line in CATALOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
                p = line.split("|")
                if p[0] == "MOVIE" and len(p) >= 10:
                    nk = re.sub(r"[^a-z0-9]", "", p[2].lower())
                    quals = json.loads(p[9])
                    valid_quals = [q for q in quals if "#protected_dl" not in q.get("torrentUrl", "")]
                    if valid_quals:
                        movies_map[nk] = {
                            "id": p[1], "title": p[2], "releaseDate": p[3], "country": p[4],
                            "rating": p[5], "category": p[6], "poster": p[7], "description": p[8],
                            "qualities": valid_quals
                        }
                elif p[0] == "SERIES" and len(p) >= 10:
                    nk = re.sub(r"[^a-z0-9]", "", p[2].lower())
                    eps = json.loads(p[9])
                    valid_eps = [e for e in eps if "#protected_dl" not in e.get("torrentUrl", "")]
                    if valid_eps:
                        series_map[nk] = {
                            "id": p[1], "title": p[2], "releaseDate": p[3], "country": p[4],
                            "rating": p[5], "category": p[6], "poster": p[7], "description": p[8],
                            "episodes": valid_eps
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
                log(f"Dominio activo conectado: {working_domain}")
                break
        except Exception:
            continue

    if working_domain:
        sections = [
            "/ultimos",
            "/peliculas",
            "/peliculas/hd",
            "/peliculas/4K",
            "/series",
            "/series/hd",
            "/documentales"
        ]
        page_urls = []
        for sec in sections:
            page_urls.append(urljoin(working_domain, sec))
            if sec != "/ultimos":
                # Supera el límite antiguo de 16 páginas escaneando hasta MAX_PAGES_PER_SECTION (35 por defecto)
                for pnum in range(2, MAX_PAGES_PER_SECTION + 1):
                    page_urls.append(urljoin(working_domain, f"{sec}/page/{pnum}"))

        log(f"Escaneando {len(page_urls)} páginas de listado en paralelo ({MAX_WORKERS} workers)...")
        candidates = []
        seen_urls = set()

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as page_executor:
            future_to_url = {
                page_executor.submit(scrape_listing_page, pg_url, working_domain): pg_url
                for pg_url in page_urls
            }
            for fut in as_completed(future_to_url):
                for item in fut.result():
                    full_u = item[8]
                    if full_u not in seen_urls:
                        seen_urls.add(full_u)
                        candidates.append(item)

        log(f"Candidatos únicos detectados para procesar: {len(candidates)}")
        completed_count = 0

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as item_executor:
            futures = [
                item_executor.submit(process_single_candidate, cand, drive_index, movies_map, series_map)
                for cand in candidates
            ]
            for fut in as_completed(futures):
                try:
                    if fut.result():
                        completed_count += 1
                        if completed_count % 40 == 0:
                            save_checkpoint(drive_index, channels, movies_map, series_map)
                except Exception:
                    pass

    save_checkpoint(drive_index, channels, movies_map, series_map)
    log(f"Catálogo finalizado exitosamente: {len(channels)} canales, {len(movies_map)} películas y {len(series_map)} series.")


if __name__ == "__main__":
    build_cloud_mdx()
