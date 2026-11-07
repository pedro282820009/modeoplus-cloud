import os
import re
import json
import time
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
TORRENTS_VAULT_DIR.mkdir(parents=True, exist_ok=True)

GITHUB_REPO_FULL = os.environ.get("GITHUB_REPO_FULL", "pedro282820009/modeoplus-cloud").strip()
MAX_PAGES_PER_CATEGORY = int(os.environ.get("MAX_PAGES_PER_CATEGORY", "35"))
MAX_NEW_POW_PER_RUN = int(os.environ.get("MAX_NEW_POW_PER_RUN", "450"))

INDEX_LOCK = threading.Lock()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
]

PUBLIC_FAST_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
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
    "https://iptv-org.github.io/iptv/countries/pe.m3u",
    "https://iptv-org.github.io/iptv/countries/cl.m3u"
]

DONTORRENT_DOMAINS = [
    "https://dontorrent.diy",
    "https://dontorrent.channel",
    "https://dontorrent.li",
    "https://dontorrent.dad",
    "https://dontorrent.org",
    "https://dontorrent.io"
]

def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def clean_category_label(cat: str) -> str:
    return re.sub(r"\s*[\(\[]\s*(?:ES|Español|Espanol)\s*[\)\]]", "", cat or "", flags=re.I).strip() or "General"

def create_resilient_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504], raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retries, pool_connections=24, pool_maxsize=24)
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
    for nonce in range(3_500_000):
        if hashlib.sha256(f"{challenge}{nonce}".encode("utf-8")).hexdigest().startswith(prefix):
            return nonce
    return 0

def fetch_torrent_bytes_in_ram(base_page_url: str, content_id: str, tabla: str) -> Optional[bytes]:
    parsed = urlparse(base_page_url)
    api_pow_url = f"{parsed.scheme}://{parsed.netloc}/api_validate_pow.php"
    try:
        HTTP_SESSION.get(base_page_url, headers=get_headers(), timeout=7)
        for tb in [tabla, "peliculas", "series", "episodios", "documentales"]:
            gen = HTTP_SESSION.post(
                api_pow_url,
                json={"action": "generate", "content_id": int(content_id), "tabla": tb},
                headers=get_headers(referer=base_page_url, is_json=True),
                timeout=7
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
                timeout=7
            )
            if val.status_code != 200:
                continue
            dl_uri = val.json().get("download_url")
            if dl_uri:
                full_t_url = ("https:" + dl_uri) if dl_uri.startswith("//") else urljoin(base_page_url, dl_uri)
                tr = HTTP_SESSION.get(full_t_url, headers=get_headers(referer=base_page_url), timeout=10)
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
    drive_index: Dict[str, dict],
    allow_live_pow: bool = True
) -> Tuple[Optional[str], bool]:
    """
    Devuelve (descriptor_url, hizo_pow_nuevo).
    Prioriza torrents_vault/ y drive_torrents_index.json para procesar miles de títulos en segundos.
    """
    vault_file = TORRENTS_VAULT_DIR / f"{item_id}.torrent"
    expected_github_url = f"https://raw.githubusercontent.com/{GITHUB_REPO_FULL}/main/torrents_vault/{item_id}.torrent"

    with INDEX_LOCK:
        if item_id in drive_index:
            saved = drive_index[item_id]
            d_url = saved.get("driveUrl", "").strip()
            mag = saved.get("magnet", "").strip()
            if vault_file.exists():
                d_url = expected_github_url
                saved["driveUrl"] = d_url
            if d_url and mag:
                return f"{d_url}||{mag}", False
            if mag:
                return mag, False
            if d_url:
                return d_url, False

    if allow_live_pow and cid and cid.isdigit() and int(cid) > 0:
        raw_bytes = fetch_torrent_bytes_in_ram(page_url, cid, tabla)
        if raw_bytes:
            github_torrent_url = save_torrent_bytes_to_github_vault(raw_bytes, item_id)
            magnet_uri = extract_exact_magnet_from_torrent_bytes(raw_bytes, title)
            combined = f"{github_torrent_url}||{magnet_uri}" if magnet_uri else github_torrent_url
            with INDEX_LOCK:
                drive_index[item_id] = {
                    "itemId": item_id,
                    "title": title,
                    "driveUrl": github_torrent_url,
                    "magnet": magnet_uri or "",
                    "updatedAt": int(time.time())
                }
            log(f"📦 Nuevo .torrent en bóveda: {item_id}.torrent ({title[:38]})")
            return combined, True

    # Si no se pudo resolver el PoW en esta pasada pero tiene content_id, guardar token #protected_dl
    # (La app de Android TV ahora sabe desbloquear #protected_dl en vivo al darle a Ver Ahora)
    if cid and cid.isdigit() and int(cid) > 0:
        return f"{page_url}#protected_dl|id={cid}|tabla={tabla}", False

    return None, False

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
                    group = clean_category_label(gm.group(1) if gm and gm.group(1) else "Entretenimiento")
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

def fetch_catalog_page_worker(args: Tuple[str, str]) -> List[tuple]:
    working_domain, pg_url = args
    found = []
    try:
        resp = HTTP_SESSION.get(pg_url, headers=get_headers(working_domain), timeout=10)
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
            genre_str = "Películas" if kind == "pelicula" else ("Documentales" if kind == "documental" else "Series")
            desc_str = f"Disponible en alta definición • {title}"
            quality_str = "1080P HD"

            if img:
                src = img.get("src") or ""
                if src:
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
                    sm_badge = ps.find("small", class_="badge")
                    if sm_badge:
                        genre_str = clean_category_label(sm_badge.get_text(strip=True))
                    all_p = ps.find_all("p")
                    if len(all_p) > 1:
                        desc_str = all_p[-1].get_text(" ", strip=True)

            if title and len(title) > 1:
                found.append((kind, cid, title, poster, year_str, genre_str, desc_str, quality_str, full_u))
    except Exception:
        pass
    return found

def hydrate_all_cached_index_items(drive_index: Dict[str, dict], movies_map: Dict[str, dict], series_map: Dict[str, dict]):
    """
    Convierte automáticamente todos los 1,800+ enlaces guardados en drive_torrents_index.json
    en entradas activas del catálogo MDX para que nunca se pierda ningún título previo.
    """
    added_from_index = 0
    for item_id, entry in drive_index.items():
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        d_url = (entry.get("driveUrl") or "").strip()
        mag = (entry.get("magnet") or "").strip()
        vault_file = TORRENTS_VAULT_DIR / f"{item_id}.torrent"
        if vault_file.exists():
            d_url = f"https://raw.githubusercontent.com/{GITHUB_REPO_FULL}/main/torrents_vault/{item_id}.torrent"
            entry["driveUrl"] = d_url

        stream_desc = f"{d_url}||{mag}" if (d_url and mag) else (d_url or mag)
        if not stream_desc:
            continue

        norm_k = re.sub(r"[^a-z0-9]", "", title.lower())
        if not norm_k:
            continue

        if item_id.startswith("mov_"):
            if norm_k not in movies_map:
                qual_tag = "4K UHD" if "[4k]" in title.lower() or "4k" in title.lower() else "1080P HD"
                movies_map[norm_k] = {
                    "id": item_id,
                    "title": re.sub(r"\s*\[.*?\]", "", title).strip() or title,
                    "releaseDate": "2025",
                    "country": "ES / LATAM",
                    "rating": "8.4",
                    "category": "Películas",
                    "poster": "",
                    "description": f"Película disponible en streaming P2P directo: {title}",
                    "qualities": [{
                        "quality": qual_tag,
                        "size": "1.9 GB",
                        "torrentUrl": stream_desc,
                        "source": "GitHubVault"
                    }]
                }
                added_from_index += 1
        elif item_id.startswith("ser_"):
            base_serie = re.sub(r"\s*[-–]?\s*\d+[ªº]?\s*Temporada.*$", "", title, flags=re.I).strip()
            s_key = re.sub(r"[^a-z0-9]", "", base_serie.lower()) or norm_k
            s_id = re.sub(r"_s\d+_e\d+.*$", "", item_id)
            if s_key not in series_map:
                series_map[s_key] = {
                    "id": s_id,
                    "title": base_serie or title,
                    "releaseDate": "2025",
                    "country": "ES / LATAM",
                    "rating": "8.6",
                    "category": "Series",
                    "poster": "",
                    "description": f"Serie completa en streaming P2P directo: {base_serie or title}",
                    "episodes": []
                }
            m_se = re.search(r"_s(\d+)_e(\d+)", item_id, re.I)
            s_num = int(m_se.group(1)) if m_se else 1
            e_num = int(m_se.group(2)) if m_se else (len(series_map[s_key]["episodes"]) + 1)
            if not any(e["episodeId"] == item_id for e in series_map[s_key]["episodes"]):
                series_map[s_key]["episodes"].append({
                    "episodeId": item_id,
                    "season": s_num,
                    "episode": e_num,
                    "title": f"Episodio {e_num} - {title[:50]}",
                    "quality": "1080P HD",
                    "size": "1.3 GB",
                    "torrentUrl": stream_desc,
                    "isDownloaded": False,
                    "localStreamUrl": ""
                })
                added_from_index += 1
    if added_from_index > 0:
        log(f"✨ Hidratados {added_from_index} títulos directamente desde drive_torrents_index.json")

def save_checkpoint(drive_index: Dict[str, dict], channels: List[dict], movies_map: Dict[str, dict], series_map: Dict[str, dict]):
    with INDEX_LOCK:
        DRIVE_INDEX_FILE.write_text(json.dumps(drive_index, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [f"HEAD|v19.0-GITHUB-FULL|{int(time.time())}"]
    for i, ch in enumerate(channels, start=1):
        lines.append(f"LIVE|live_{i}|{ch['name']}|{ch['category']}|{ch['language']}|1080p HD|{ch['poster']}|Señal en Vivo|{ch['url']}")
    for m in movies_map.values():
        q_json = json.dumps(m["qualities"], ensure_ascii=False).replace("|", "/")
        clean_t = m["title"].replace("|", " ").replace("\n", " ").strip()
        clean_d = m["description"].replace("|", " ").replace("\n", " ").strip()[:260]
        lines.append(f"MOVIE|{m['id']}|{clean_t}|{m['releaseDate']}|{m['country']}|{m['rating']}|{clean_category_label(m['category'])}|{m['poster']}|{clean_d}|{q_json}|false|")
    for s in series_map.values():
        if not s.get("episodes"):
            continue
        s["episodes"] = sorted(s["episodes"], key=lambda x: (int(x.get("season", 1)), int(x.get("episode", 1))))
        ep_json = json.dumps(s["episodes"], ensure_ascii=False).replace("|", "/")
        clean_t = s["title"].replace("|", " ").replace("\n", " ").strip()
        clean_d = s["description"].replace("|", " ").replace("\n", " ").strip()[:260]
        lines.append(f"SERIES|{s['id']}|{clean_t}|{s['releaseDate']}|{s['country']}|{s['rating']}|{clean_category_label(s['category'])}|{s['poster']}|{clean_d}|{ep_json}")

    CATALOG_TMP_FILE.write_text("\n".join(lines), encoding="utf-8")
    CATALOG_TMP_FILE.replace(CATALOG_FILE)

def build_cloud_mdx():
    log("🚀 Iniciando sincronización masiva Modeo+ Cloud 100% en GitHub...")
    drive_index: Dict[str, dict] = {}
    if DRIVE_INDEX_FILE.exists():
        try:
            drive_index = json.loads(DRIVE_INDEX_FILE.read_text(encoding="utf-8"))
            log(f"📚 Índice cargado con {len(drive_index)} torrents registrados.")
        except Exception:
            drive_index = {}

    channels = scrape_channels()
    log(f"📺 Canales M3U recolectados: {len(channels)}")
    movies_map: Dict[str, dict] = {}
    series_map: Dict[str, dict] = {}

    # 1. Cargar catálogo MDX existente si lo hay
    if CATALOG_FILE.exists():
        try:
            for line in CATALOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
                p = line.split("|")
                if p[0] == "MOVIE" and len(p) >= 10:
                    nk = re.sub(r"[^a-z0-9]", "", p[2].lower())
                    quals = json.loads(p[9])
                    if quals:
                        movies_map[nk] = {
                            "id": p[1], "title": p[2], "releaseDate": p[3], "country": p[4],
                            "rating": p[5], "category": clean_category_label(p[6]), "poster": p[7], "description": p[8],
                            "qualities": quals
                        }
                elif p[0] == "SERIES" and len(p) >= 10:
                    nk = re.sub(r"[^a-z0-9]", "", p[2].lower())
                    eps = json.loads(p[9])
                    if eps:
                        series_map[nk] = {
                            "id": p[1], "title": p[2], "releaseDate": p[3], "country": p[4],
                            "rating": p[5], "category": clean_category_label(p[6]), "poster": p[7], "description": p[8],
                            "episodes": eps
                        }
        except Exception:
            pass

    # 2. Hidratar inmediatamente todos los 1,800+ torrents que ya tienes en drive_torrents_index.json
    hydrate_all_cached_index_items(drive_index, movies_map, series_map)

    # 3. Buscar dominio activo y barrer páginas (1 a 35) + Abecedario (A-Z) en paralelo
    working_domain = None
    for d in DONTORRENT_DOMAINS:
        try:
            r = HTTP_SESSION.get(d, headers=get_headers(), timeout=8)
            if r.status_code == 200:
                pu = urlparse(r.url)
                working_domain = f"{pu.scheme}://{pu.netloc}"
                log(f"🌐 Dominio activo conectado: {working_domain}")
                break
        except Exception:
            continue

    if working_domain:
        sections = ["/ultimos", "/peliculas", "/peliculas/hd", "/peliculas/4K", "/series", "/series/hd", "/documentales"]
        page_urls = []
        for sec in sections:
            page_urls.append(urljoin(working_domain, sec))
            if sec != "/ultimos":
                for pnum in range(2, MAX_PAGES_PER_CATEGORY + 1):
                    page_urls.append(urljoin(working_domain, f"{sec}/page/{pnum}"))

        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            page_urls.append(urljoin(working_domain, f"/peliculas/letra-{letter.lower()}"))
            page_urls.append(urljoin(working_domain, f"/series/letra-{letter.lower()}"))

        log(f"🔎 Escaneando {len(page_urls)} páginas de listado y abecedario A-Z con 12 hilos en paralelo...")
        candidates = []
        seen_urls = set()

        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = [executor.submit(fetch_catalog_page_worker, (working_domain, u)) for u in page_urls]
            for future in as_completed(futures):
                for item_tuple in future.result():
                    full_u = item_tuple[8]
                    if full_u not in seen_urls:
                        seen_urls.add(full_u)
                        candidates.append(item_tuple)

        log(f"🎬 Total de candidatos únicos encontrados en la web: {len(candidates)}")
        new_pow_count = 0
        processed_counter = 0

        for kind, cid, title, poster, year_str, genre_str, desc_str, quality_str, full_u in candidates:
            norm_k = re.sub(r"[^a-z0-9]", "", title.lower())
            if not norm_k:
                continue
            is_serie = kind in ("serie", "episodio")
            allow_pow = (new_pow_count < MAX_NEW_POW_PER_RUN)

            if not is_serie:
                item_id = f"mov_{hashlib.md5(norm_k.encode()).hexdigest()[:10]}"
                cloud_url, did_pow = resolve_cloud_torrent_url(
                    item_id, title, full_u, cid, "peliculas", drive_index, allow_live_pow=allow_pow
                )
                if did_pow:
                    new_pow_count += 1
                if not cloud_url:
                    continue
                clean_title = re.sub(r"\s*\[.*?\]", "", title).strip() or title
                movies_map[norm_k] = {
                    "id": item_id,
                    "title": clean_title,
                    "releaseDate": year_str,
                    "country": "ES / LATAM",
                    "rating": "8.4",
                    "category": clean_category_label(genre_str),
                    "poster": poster or movies_map.get(norm_k, {}).get("poster", ""),
                    "description": desc_str,
                    "qualities": [{
                        "quality": quality_str,
                        "size": "1.9 GB",
                        "torrentUrl": cloud_url,
                        "source": "GitHubVault"
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
                        "category": "Series",
                        "poster": poster,
                        "description": desc_str,
                        "episodes": []
                    }
                elif poster and not series_map[s_key].get("poster"):
                    series_map[s_key]["poster"] = poster

                ep_num = len(series_map[s_key]["episodes"]) + 1
                ep_id = f"{s_id}_s1_e{ep_num}"
                cloud_url, did_pow = resolve_cloud_torrent_url(
                    ep_id, title, full_u, cid, "series", drive_index, allow_live_pow=allow_pow
                )
                if did_pow:
                    new_pow_count += 1
                if not cloud_url:
                    continue
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
            if processed_counter % 50 == 0:
                save_checkpoint(drive_index, channels, movies_map, series_map)

    save_checkpoint(drive_index, channels, movies_map, series_map)
    log(f"✅ CATÁLOGO FINALIZADO: {len(channels)} canales, {len(movies_map)} películas y {len(series_map)} series escritas en modeoplus_catalog.mdx.")

if __name__ == "__main__":
    build_cloud_mdx()
