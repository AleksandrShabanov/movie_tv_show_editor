#!/usr/bin/env python3
"""
YouTube Movie Top Video Pipeline
Собирает видео-топ фильмов из:
- войсовера (VoicerAPI / ElevenLabs)
- трейлеров с YouTube (yt-dlp)
- постеров (TMDB или локальные файлы)

Использование:
  python pipeline.py movies.json --output final.mp4

Формат movies.json — см. movies_example.json
"""

import os
import sys
import json
import time
import math
import re
import random
import hashlib
import argparse
import subprocess
import tempfile
import requests
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv

load_dotenv()

def run_ffmpeg(cmd: list) -> None:
    """Запускает ffmpeg и печатает stderr при ошибке."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"❌ ffmpeg error (код {result.returncode}):")
        print(result.stderr[-3000:])
        raise subprocess.CalledProcessError(result.returncode, cmd)


def _detect_hw_encoder() -> str:
    """Возвращает h264_videotoolbox если доступен (macOS GPU), иначе libx264."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        if "h264_videotoolbox" in r.stdout:
            return "h264_videotoolbox"
    except Exception:
        pass
    return "libx264"


_HW_ENCODER = _detect_hw_encoder()


def _venc(pix_fmt: bool = True) -> list[str]:
    """Возвращает ffmpeg-аргументы кодировщика: GPU (VideoToolbox) или CPU x264.
    Пайплайн гоняет видео через много последовательных перекодировок (обрезка,
    фильтры, оверлеи) — на -q:v 50 (VideoToolbox) это накопительно размывало
    картинку, особенно заметно на больших ТВ-экранах. Явный битрейт с запасом
    даёт кодировщику стабильный бюджет бит на каждом проходе."""
    if _HW_ENCODER == "h264_videotoolbox":
        args = ["-c:v", "h264_videotoolbox", "-b:v", "10M", "-maxrate", "14M",
                 "-bufsize", "20M", "-allow_sw", "1"]
    else:
        args = ["-c:v", "libx264", "-preset", "medium", "-crf", "16"]
    if pix_fmt:
        args += ["-pix_fmt", "yuv420p"]
    return args


# ─────────────────────────────────────────────
# КОНФИГ — ключи читаются из .env
# ─────────────────────────────────────────────
LUMEAN_API_KEY = os.getenv("LUMEAN_API_KEY")
TMDB_API_KEY   = os.getenv("TMDB_API_KEY")
OMDB_API_KEY   = os.getenv("OMDB_API_KEY")
LUMEAN_BASE    = "https://api.lumean.app/api/public"
# Необязательно: заранее созданный template — если задан, шаг создания пропускается
LUMEAN_TEMPLATE_ID = os.getenv("LUMEAN_TEMPLATE_ID")

# Голос по умолчанию (ElevenLabs voice_id)
# DEFAULT_VOICE_ID = "iP95p4xoKVk53GoZ742B"  # старый голос — оставлен на всякий случай
DEFAULT_VOICE_ID = "JZ3e95uoTACVf6tXaaEi"    # новый голос

# Сколько секунд пропускать в начале трейлера (MPAA + студия)
TRAILER_SKIP_SECONDS = 15

# Сколько секунд пропускать в конце трейлера (финальные титры, Blu-ray, дата)
TRAILER_END_SKIP = 40

# Для монтажа интро/аутро — более агрессивные отступы
MONTAGE_SKIP_START = 20
MONTAGE_SKIP_END   = 50

# Настройки интро/аутро монтажа
INTRO_DURATION    = 15    # секунд
OUTRO_DURATION    = 15    # секунд
MONTAGE_CLIP_DUR  = 2.5   # длина одного клипа в монтаже

# Сколько секунд показывать постер перед трейлером (с Ken Burns)
POSTER_DURATION = 4

# Стоп-кадры (поляроид-эффект)
STILL_DURATION  = 5      # секунд на один стоп-кадр
N_STILLS        = 5      # стоп-кадров на фильм
FRAME_INNER_W   = 1250   # ширина окна рамки
FRAME_INNER_H   = 703    # высота (≈16:9)
FRAME_INNER_X   = (1920 - 1250) // 2   # = 335
FRAME_INNER_Y   = (1080 - 703)  // 2   # = 188
BG_VIDEO_PATH   = os.path.expanduser("~/12988171_3840_2160_25fps.mp4")

# Визуальные настройки
BG_COLOR    = "0x0a0f1e"   # тёмно-синий фон
TEXT_COLOR  = "white"
FONT_PATH   = "/System/Library/Fonts/Supplemental/Impact.ttf"  # Mac
# FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"  # Linux

# Фильтр на трейлерные куски. Раньше добавлял шум+резкость для маскировки от
# Content ID — сознательно убран ради качества картинки на ТВ (риск claim'ов
# принят). Пусто = никакой доп. обработки трейлерных кусков.
TRAILER_FILM_GRAIN = ""

# ─────────────────────────────────────────────
# 1. ВОРКФЛОУ ВОЙСОВЕРА
# ─────────────────────────────────────────────

# Кэш template_id по voice_id (создаём один раз за процесс)
_lumean_templates: dict[str, str] = {}


def _lumean_request(method: str, path: str, json_body: dict | None = None, retries: int = 20) -> dict:
    """Запрос к Lumean API с ретраями на 429 и сетевые сбои. Возвращает JSON."""
    url = f"{LUMEAN_BASE}{path}"
    headers = {"X-API-KEY": LUMEAN_API_KEY, "Content-Type": "application/json"}
    for attempt in range(retries):
        try:
            r = requests.request(method, url, headers=headers, json=json_body, timeout=30)
        except Exception:
            time.sleep(min(5 * (2 ** attempt), 120))
            continue
        if r.status_code == 429:
            ra = r.headers.get("Retry-After", "")
            wait = int(ra) if ra.isdigit() else min(30 * (2 ** attempt), 600)
            print(f"     ⏳ Rate limit (429), попытка {attempt+1}/{retries}, ждём {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Lumean: {method} {path} не удался после {retries} попыток")


def _lumean_template_id(voice_id: str) -> str:
    """Возвращает template_id для голоса (создаёт один раз и кэширует).
    Если задан LUMEAN_TEMPLATE_ID — используется он (шаг создания пропускается)."""
    if LUMEAN_TEMPLATE_ID:
        return LUMEAN_TEMPLATE_ID
    if voice_id in _lumean_templates:
        return _lumean_templates[voice_id]
    body = {
        "service_key": "elevenlabs",
        "name": f"movie-editor {voice_id}",
        "config": {"tts_settings": {
            "mode": "mode_v1",
            "model_id": "eleven_multilingual_v2",
            "voice_id": voice_id,
            "voice_settings": {
                # Максимальная ровность: голос иногда «проседал» ниже по тону.
                "stability": 1.0, "similarity_boost": 0.85,
                "use_speaker_boost": True, "speed": 1.0,
            },
        }},
    }
    tid = _lumean_request("POST", "/templates", body)["data"]["id"]
    _lumean_templates[voice_id] = tid
    return tid


def generate_voiceover(text: str, output_path: str, voice_id: str = DEFAULT_VOICE_ID) -> str:
    """Генерирует войсовер через Lumean: template → order → polling → download."""
    print(f"  🎙  Генерация войсовера ({len(text)} символов)...")

    template_id = _lumean_template_id(voice_id)
    order = _lumean_request("POST", "/orders",
                            {"template_id": template_id, "input_text": text})["data"]
    order_id = order["id"]
    print(f"     Order ID: {order_id} — ожидаем...")

    TERMINAL_OK  = {"completed", "result_delivered"}
    TERMINAL_BAD = {"failed", "cancelled", "compensated"}

    files = None
    last_status = None
    for _ in range(1200):  # 1200 × 3s ≈ 60 мин
        time.sleep(3)
        try:
            data = _lumean_request("GET", f"/orders/{order_id}", retries=5)["data"]
        except Exception:
            continue
        status = data.get("status", "")
        if status != last_status:
            print(f"     Статус: {status}")
            last_status = status
        if status in TERMINAL_OK:
            result = data.get("result") or {}
            files = result.get("files") or data.get("files") or []
            if not files:
                raise RuntimeError(f"Lumean: заказ завершён, но нет файлов: {data}")
            break
        if status in TERMINAL_BAD:
            raise RuntimeError(f"Lumean: заказ провален ({status}): {data}")
    else:
        raise TimeoutError("Войсовер не готов за 60 минут")

    # Ссылка на скачивание временная — получаем через /storage/url
    dl_url = _lumean_request("POST", "/storage/url", {"path": files[0]})["data"]["url"]
    audio = requests.get(dl_url, timeout=120)
    audio.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(audio.content)
    print(f"     ✓ Войсовер сохранён: {output_path}")
    return output_path


def get_audio_duration(path: str) -> float:
    """Возвращает длительность аудиофайла в секундах через ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", path
    ]
    out = subprocess.check_output(cmd)
    return float(json.loads(out)["format"]["duration"])


def _get_video_height(path: str) -> int | None:
    """Возвращает высоту видео в пикселях через ffprobe."""
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "quiet", "-select_streams", "v:0",
            "-show_entries", "stream=height", "-of", "csv=p=0", path
        ], text=True)
        return int(out.strip())
    except Exception:
        return None


# ─────────────────────────────────────────────
# 2. ТРЕЙЛЕРЫ
# ─────────────────────────────────────────────

IMDB_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
# Разрешение MP4 → числовой ранг (для выбора лучшего качества)
IMDB_RES_RANK = {"DEF_1080p": 1080, "DEF_720p": 720, "DEF_480p": 480, "DEF_SD": 360, "DEF_AUTO": 0}
# Только настоящие кадры фильма — трейлеры и клипы (не интервью/фичуретки/тизеры каналов)
IMDB_OK_TYPES = {"Trailer", "Clip"}
# Кросс-фильмовая / мета-нарезка в названии ролика — не берём (это не кадры данного фильма)
IMDB_BAD_NAME = re.compile(
    r"(?i)(guide to the films|\bvs\.?\b|versus|\bevery\b|ranking|explained|"
    r"reacts?|reaction|behind the scenes|interview|featurette)"
)


IMDB_RETRIES = 3          # число попыток на сетевой запрос
IMDB_BACKOFF = 1.5        # базовая пауза (сек), растёт экспоненциально


def _imdb_request(method: str, url: str, **kwargs) -> requests.Response:
    """HTTP-запрос к IMDB с ретраями и экспоненциальным backoff."""
    kwargs.setdefault("headers", {"User-Agent": IMDB_UA})
    kwargs.setdefault("timeout", 25)
    last = None
    for attempt in range(IMDB_RETRIES):
        try:
            resp = requests.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last = e
            if attempt < IMDB_RETRIES - 1:
                time.sleep(IMDB_BACKOFF * (2 ** attempt))
    raise last


def _imdb_graphql(query: str) -> dict:
    """Запрос к публичному GraphQL IMDB."""
    resp = _imdb_request(
        "POST", "https://api.graphql.imdb.com/",
        json={"query": query},
        headers={"User-Agent": IMDB_UA, "Content-Type": "application/json"},
    )
    return resp.json()


def _norm_title(s: str) -> str:
    """Нормализует название для сравнения: убирает регистр, диакритику и всё,
    кроме букв/цифр (так «Rocket Man» ≡ «RocketMan», «André» ≡ «Andre»)."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _title_tokens(s: str) -> set[str]:
    """Слова названия в нормализованном виде (для сравнения по подмножеству)."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return set(re.sub(r"[^a-z0-9 ]", " ", s.lower()).split())


def _imdb_find_title_id(title: str, year: int) -> str | None:
    """Находит tt-id фильма через suggestion-API IMDB.

    Делает два запроса (по названию и по «название год»), собирает кандидатов
    и выбирает по тир-логике, где год — сильный сигнал наравне с названием.
    Это устойчиво к вариантам написания:
      • слитно/раздельно и диакритика («RocketMan», «Andre»)
      • подзаголовки («Star Wars: Episode III - Revenge of the Sith»)
      • стилизация, где имя не помогает («Se7en» → IMDB «Seven», спасает год)
    """
    from urllib.parse import quote
    want = _norm_title(title)
    want_tok = _title_tokens(title)

    cands = []          # [{id, y, norm, tok, rank}]
    seen = set()
    # Ранги первого запроса (по названию) идут 0.., второго — с большим сдвигом,
    # чтобы rank 0 основной выдачи всегда побеждал.
    for qi, q in enumerate((title, f"{title} {year}")):
        url = f"https://v3.sg.media-imdb.com/suggestion/x/{quote(q)}.json?includeVideos=0"
        try:
            data = _imdb_request("GET", url).json()
        except Exception:
            continue
        for idx, r in enumerate(data.get("d", [])):
            rid = r.get("id", "")
            if not rid.startswith("tt") or rid in seen:
                continue
            seen.add(rid)
            cands.append({
                "id": rid, "y": r.get("y"), "rank": qi * 1000 + idx,
                "norm": _norm_title(r.get("l", "")),
                "tok": _title_tokens(r.get("l", "")),
            })

    def year_exact(c): return c["y"] == year
    def year_near(c):  return c["y"] is not None and abs(c["y"] - year) <= 1
    def name_exact(c): return c["norm"] == want
    def tok_match(c):  return bool(want_tok) and (want_tok <= c["tok"] or c["tok"] <= want_tok)
    # позиция в выдаче решает (настоящий фильм почти всегда rank 0),
    # число «лишних» слов — вторичный тайбрейк
    def sort_key(c):   return (c["rank"], len(want_tok ^ c["tok"]))

    tiers = [
        lambda c: name_exact(c) and year_exact(c),                  # 1
        lambda c: name_exact(c) and year_near(c),                   # 2
        lambda c: tok_match(c) and year_exact(c),                   # 3
        lambda c: tok_match(c) and year_near(c),                    # 4
        lambda c: year_exact(c) and c["rank"] <= 2,                 # 5: имя не помогло — год+топ выдачи
        name_exact,                                                 # 6: точное имя, любой год
    ]
    for match in tiers:
        picked = sorted((c for c in cands if match(c)), key=sort_key)
        if picked:
            return picked[0]["id"]
    return cands[0]["id"] if cands else None          # иначе — первый кандидат


def _imdb_best_trailer(tt: str) -> tuple[str, int] | None:
    """Выбирает лучший ролик тайтла: максимум разрешения среди трейлеров/клипов,
    при равном качестве предпочитает Trailer. Возвращает (url, height) или None.
    """
    q = ('{title(id:"%s"){primaryVideos(first:20){edges{node{name{value} '
         'contentType{displayName{value}} playbackURLs{url videoDefinition videoMimeType}}}}}}' % tt)
    edges = _imdb_graphql(q)["data"]["title"]["primaryVideos"]["edges"]
    best = None  # (res, is_trailer, url)
    for e in edges:
        node = e["node"]
        ctype = node["contentType"]["displayName"]["value"]
        name = node["name"]["value"]
        if ctype not in IMDB_OK_TYPES or IMDB_BAD_NAME.search(name):
            continue
        mp4 = {p["videoDefinition"]: p["url"] for p in node["playbackURLs"]
               if p["videoMimeType"] == "MP4"}
        if not mp4:
            continue
        res = max(IMDB_RES_RANK.get(d, 0) for d in mp4)
        url = mp4[max(mp4, key=lambda d: IMDB_RES_RANK.get(d, 0))]
        key = (res, ctype == "Trailer")
        if best is None or key > best[0]:
            best = (key, url, res)
    if best is None:
        return None
    return best[1], best[2]


# Каналы-агрегаторы с водяными знаками — исключаем из поиска на YouTube
YT_BLOCKED_UPLOADERS = r"(?i)(movieclips|fandango|clipsandtrailers|kinocheck)"
# Формат: HD h264 ≤1080p (avc1 — для совместимости с ffmpeg), затем любой ≥720p
YT_FORMAT = ("bestvideo[height>=720][height<=1080][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
             "bestvideo[height>=720][height<=1080]+bestaudio/best[height>=720]")


# Ролики-разборы/реакции/новости проходили фильтр по названию (например,
# "100 Tears Review" или "Terrifier 2 ... Release Date Confirmed" содержат
# слова из названия фильма не хуже настоящего трейлера), поэтому их нужно
# отсеивать по типу контента отдельно, а не только по названию.
YT_NON_TRAILER_TITLE = (
    r"(?i)\b(reviews?|reactions?|react(s|ing)?|rants?|recaps?|breakdowns?|"
    r"explained|analysis|commentary|podcast|interviews?|ending explained|"
    r"first look|release date|coming (soon|this week)|confirmed|top \d+|"
    r"deleted scene|behind the scenes)\b"
)


def _youtube_trailer_ids(movie_title: str, year: int) -> list[str]:
    """Ищет на YouTube кандидатов-трейлеров, отфильтрованных по каналу/длине/названию."""
    queries = [
        f"{movie_title} {year} official trailer",
        f"{movie_title} {year} trailer",
        f"{movie_title} trailer {year}",
    ]
    title_words = {w for w in re.sub(r"[^a-z0-9 ]", "", movie_title.lower()).split() if len(w) > 2}
    ids, seen = [], set()
    for query in queries:
        res = subprocess.run(
            ["yt-dlp", f"ytsearch10:{query}", "--flat-playlist",
             "--print", "%(id)s\t%(uploader)s\t%(duration)s\t%(title)s",
             "--quiet", "--no-warnings"],
            capture_output=True, text=True,
        )
        for line in res.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            vid, uploader, dur_str, vtitle = parts[0], parts[1], parts[2], parts[3].lower()
            if vid in seen or re.search(YT_BLOCKED_UPLOADERS, uploader):
                continue
            if re.search(YT_NON_TRAILER_TITLE, vtitle):
                continue
            try:
                dur = float(dur_str)
            except ValueError:
                dur = 0
            if dur and (dur < 60 or dur > 360):                     # трейлер: 1–6 минут
                continue
            vtitle_words = set(re.sub(r"[^a-z0-9 ]", "", vtitle).split())
            # Все значимые слова названия должны быть в видео — не просто одно
            # общее слово, иначе "100 Tears" ловит "100 Years" на слове "100".
            if title_words and not title_words <= vtitle_words:
                continue
            seen.add(vid)
            ids.append(vid)
        if ids:
            break
    return ids


def _clean_partial(output_path: str):
    """Удаляет незавершённые файлы yt-dlp (output.mp4, output.fNNN.mp4.part и т.п.)."""
    import glob
    stem = os.path.splitext(output_path)[0]
    for p in [output_path, *glob.glob(f"{stem}.f*"), *glob.glob(f"{output_path}*.part")]:
        try:
            os.remove(p)
        except OSError:
            pass


def _download_trailer_youtube(movie_title: str, year: int, output_path: str) -> str | None:
    """Скачивает трейлер с YouTube в HD (yt-dlp + bgutil PO-token, анонимно, без cookies)."""
    for vid in _youtube_trailer_ids(movie_title, year):
        try:
            subprocess.run(
                ["yt-dlp", vid, "--format", YT_FORMAT, "--merge-output-format", "mp4",
                 "--output", output_path, "--no-playlist", "--quiet", "--no-warnings",
                 # устойчивость к транзитным обрывам загрузки (иначе единственный
                 # HD-кандидат мог провалиться на середине и трейлер терялся)
                 "--retries", "5", "--fragment-retries", "10",
                 "--retry-sleep", "3", "--socket-timeout", "30"],
                check=True,
            )
            if os.path.exists(output_path):
                h = _get_video_height(output_path)
                if h and h < 720:
                    _clean_partial(output_path)
                    continue
                print(f"     ✓ Трейлер YouTube ({h}p): {output_path}")
                return output_path
        except subprocess.CalledProcessError:
            _clean_partial(output_path)
    return None


def _download_trailer_imdb(movie_title: str, year: int, output_path: str) -> str | None:
    """Скачивает трейлер с IMDB (self-hosted) — fallback, когда YouTube не отдал HD."""
    try:
        tt = _imdb_find_title_id(movie_title, year)
        if not tt:
            print(f"     ✗ IMDB: фильм не найден")
            return None
        best = _imdb_best_trailer(tt)
        if not best:
            print(f"     ✗ IMDB ({tt}): нет подходящих роликов")
            return None
        mp4_url, _res = best

        tmp_path = output_path + ".part"
        with _imdb_request("GET", mp4_url, stream=True, timeout=60) as r:
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
        os.replace(tmp_path, output_path)

        height = _get_video_height(output_path)
        print(f"     ✓ Трейлер IMDB ({height}p): {output_path}")
        return output_path
    except Exception as e:
        print(f"     ✗ Ошибка IMDB: {e}")
        for p in (output_path, output_path + ".part"):
            if os.path.exists(p):
                os.remove(p)
        return None


def download_trailer(movie_title: str, year: int, output_dir: str) -> str:
    """Каскад источников: YouTube HD (yt-dlp + bgutil, анонимно) → IMDB как fallback."""
    safe_name = "".join(c for c in movie_title if c.isalnum() or c in " _-").strip().replace(" ", "_")
    output_path = os.path.join(output_dir, f"{safe_name}_{year}_trailer.mp4")

    if os.path.exists(output_path):
        print(f"     ↩  Трейлер уже есть: {output_path}")
        return output_path

    print(f"  🎬  Скачиваем трейлер: {movie_title} ({year})")
    return (_download_trailer_youtube(movie_title, year, output_path)
            or _download_trailer_imdb(movie_title, year, output_path))


def trim_trailer_intro(input_path: str, output_path: str, skip: int = TRAILER_SKIP_SECONDS) -> str:
    """Обрезает начало трейлера (MPAA, логосы студий).
    Для коротких роликов (IMDB-трейлеры бывают 15-30 с) обрезку ограничиваем,
    чтобы не «съесть» весь клип.
    """
    try:
        dur = get_audio_duration(input_path)
        skip = min(skip, int(dur * 0.3))   # не срезаем больше 30% длительности
    except Exception:
        pass
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(skip),
        "-i", input_path,
        "-c", "copy",
        output_path
    ]
    run_ffmpeg(cmd)
    return output_path


# ─────────────────────────────────────────────
# 3. ВСПОМОГАТЕЛЬНЫЙ ПОИСК TMDB
# ─────────────────────────────────────────────

TMDB_RETRIES = 3
TMDB_BACKOFF = 1.5


def _tmdb_get(url: str, params: dict | None = None, timeout: int = 15) -> requests.Response:
    """GET к TMDB (API или image CDN) с ретраями и экспоненциальным backoff —
    TMDB периодически отдаёт Read timeout, из-за чего пропадали постеры/кадры."""
    last = None
    for attempt in range(TMDB_RETRIES):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            if attempt < TMDB_RETRIES - 1:
                time.sleep(TMDB_BACKOFF * (2 ** attempt))
    raise last


def _find_tmdb_movie(title: str, year: int) -> tuple[int, str] | None:
    """Ищет фильм/сериал в TMDB. Год — сильный сигнал: результат с точным годом
    из поиска по названию принимается, даже если у TMDB другое основное название
    (напр. «Hungry Wives» для «Season of the Witch» 1972). Иначе легко подцепить
    одноимённый фильм другого года (напр. версию 2011 с Кейджем).
    Возвращает (tmdb_id, media_type) или None.
    """
    import difflib
    if not TMDB_API_KEY:
        return None

    def _year_of(res: dict) -> int | None:
        date = res.get("release_date") or res.get("first_air_date") or ""
        try:
            return int(date[:4])
        except (ValueError, TypeError):
            return None

    searches = [
        ("https://api.themoviedb.org/3/search/movie", {"api_key": TMDB_API_KEY, "query": title, "year": year, "language": "en-US"}, "movie"),
        ("https://api.themoviedb.org/3/search/tv",    {"api_key": TMDB_API_KEY, "query": title, "first_air_date_year": year, "language": "en-US"}, "tv"),
        ("https://api.themoviedb.org/3/search/movie", {"api_key": TMDB_API_KEY, "query": title, "language": "en-US"}, "movie"),
        ("https://api.themoviedb.org/3/search/tv",    {"api_key": TMDB_API_KEY, "query": title, "language": "en-US"}, "tv"),
    ]
    cands = []  # (id, media_type, ratio, year)
    seen = set()
    for url, params, media_type in searches:
        try:
            results = _tmdb_get(url, params=params).json().get("results", [])
        except Exception:
            continue
        for res in results[:5]:
            key = (media_type, res.get("id"))
            if res.get("id") is None or key in seen:
                continue
            seen.add(key)
            ratio = difflib.SequenceMatcher(
                None, title.lower(), (res.get("title") or res.get("name") or "").lower()
            ).ratio()
            cands.append((res["id"], media_type, ratio, _year_of(res)))

    def ynear(c, d): return c[3] is not None and abs(c[3] - year) <= d
    # Тиры от сильного к слабому. Точное название + близкий год идёт раньше, чем
    # просто год-совпадение, — иначе одноимённый импостор того же года побьёт
    # правильный фильм, у которого TMDB-год отличается на единицу (The Witch 2015→
    # TMDB 2016). А чисто год-совпадение (без учёта названия) ловит фильмы под
    # алиасом (Season of the Witch 1972 → «Hungry Wives»).
    tiers = [
        lambda c: c[2] >= 0.9 and c[3] == year,       # точное название + точный год
        lambda c: c[2] >= 0.9 and ynear(c, 1),        # точное название + ±1 год (The Witch 2015→2016)
        lambda c: c[3] == year,                       # точный год под алиасом (Season→Hungry Wives 1972)
        lambda c: c[2] >= 0.9 and ynear(c, 2),        # точное название, ±2 года
        lambda c: ynear(c, 1),                        # ±1 год
        lambda c: c[2] >= 0.7 and ynear(c, 3),        # похожее название, близкий год
        lambda c: c[2] >= 0.85,                        # почти точное название, любой год
    ]
    for match in tiers:
        picked = [c for c in cands if match(c)]
        if picked:
            best = max(picked, key=lambda c: c[2])    # среди подходящих — лучшее название
            return (best[0], best[1])
    return None


# ─────────────────────────────────────────────
# 3. ПОСТЕРЫ
# ─────────────────────────────────────────────

def download_poster(movie_title: str, year: int, output_dir: str) -> str | None:
    """Скачивает постер: TMDB → OMDB как фолбэк. Возвращает путь или None."""
    safe_name = "".join(c for c in movie_title if c.isalnum() or c in " _-").strip().replace(" ", "_")
    poster_path = os.path.join(output_dir, f"{safe_name}_{year}_poster.jpg")

    if os.path.exists(poster_path):
        return poster_path

    # --- TMDB ---
    if TMDB_API_KEY:
        try:
            match = _find_tmdb_movie(movie_title, year)
            if match:
                tmdb_id, media_type = match
                img_r = _tmdb_get(
                    f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/images",
                    params={"api_key": TMDB_API_KEY, "include_image_language": "en,null"},
                )
                posters = img_r.json().get("posters", [])
                posters.sort(key=lambda x: x.get("vote_average", 0), reverse=True)
                poster_file_path = posters[0].get("file_path") if posters else None
                if not poster_file_path:
                    detail = _tmdb_get(
                        f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}",
                        params={"api_key": TMDB_API_KEY, "language": "en-US"},
                    ).json()
                    poster_file_path = detail.get("poster_path")
                if poster_file_path:
                    img = _tmdb_get(f"https://image.tmdb.org/t/p/w780{poster_file_path}")
                    if img.status_code == 200 and len(img.content) > 10000:
                        with open(poster_path, "wb") as f:
                            f.write(img.content)
                        print(f"     ✓ Постер (TMDB): {poster_path}")
                        return poster_path
        except Exception as e:
            print(f"     ⚠  TMDB ошибка постера: {e}")

    # --- OMDB фолбэк: movie и series ---
    if OMDB_API_KEY:
        for omdb_type in ("movie", "series"):
            try:
                r = requests.get(
                    "https://www.omdbapi.com/",
                    params={"apikey": OMDB_API_KEY, "t": movie_title, "y": year, "type": omdb_type},
                    timeout=10
                )
                data = r.json()
                poster_url = data.get("Poster", "")
                if poster_url and poster_url != "N/A":
                    img = requests.get(poster_url, timeout=10)
                    if img.status_code == 200 and len(img.content) > 10000:
                        with open(poster_path, "wb") as f:
                            f.write(img.content)
                        print(f"     ✓ Постер (OMDB): {poster_path}")
                        return poster_path
            except Exception as e:
                print(f"     ⚠  OMDB ошибка ({omdb_type}): {e}")

    print(f"     ⚠  Постер не найден для: {movie_title}")
    return None


# ─────────────────────────────────────────────
# 3b. СТОП-КАДРЫ (поляроид-эффект)
# ─────────────────────────────────────────────

def download_movie_stills(title: str, year: int, n: int, output_dir: str) -> list[str]:
    """Скачивает n backdrop-стоп-кадров из TMDB. Возвращает список путей."""
    if not TMDB_API_KEY:
        return []
    safe = "".join(c for c in title if c.isalnum() or c in " _-").strip().replace(" ", "_")
    safe = f"{safe}_{year}"

    # Проверяем кеш
    cached = [os.path.join(output_dir, f"{safe}_still_{i+1}.jpg") for i in range(n)]
    if all(os.path.exists(p) for p in cached):
        print(f"     ↩  Стоп-кадры уже есть")
        return cached

    match = _find_tmdb_movie(title, year)
    if not match:
        print(f"     ⚠  Фильм не найден в TMDB: {title}")
        return []
    movie_id, media_type = match

    try:
        r = _tmdb_get(
            f"https://api.themoviedb.org/3/{media_type}/{movie_id}/images",
            params={"api_key": TMDB_API_KEY},
        )
        backdrops = r.json().get("backdrops", [])
    except Exception as e:
        print(f"     ⚠  TMDB ошибка кадров: {e}")
        return []
    # iso_639_1 == null → чистый кадр из фильма без надписей; язык (es/it/ru/fr…) →
    # backdrop с впечатанным логотипом/названием на этом языке — такие в конец.
    def _lang_rank(bd):
        lang = bd.get("iso_639_1")
        return 0 if lang is None else (1 if lang == "en" else 2)
    backdrops.sort(key=lambda x: (_lang_rank(x), -x.get("vote_average", 0)))

    paths = []
    for i, bd in enumerate(backdrops[:n]):
        fp = bd.get("file_path", "")
        if not fp:
            continue
        out = os.path.join(output_dir, f"{safe}_still_{i+1}.jpg")
        try:
            img = _tmdb_get(f"https://image.tmdb.org/t/p/w1280{fp}")
            if len(img.content) > 5000:
                with open(out, "wb") as f:
                    f.write(img.content)
                paths.append(out)
                print(f"     ✓ Стоп-кадр {i+1}: {out}")
        except Exception:
            pass

    if not paths:
        print(f"     ⚠  Стоп-кадры не найдены: {title}")
    return paths


def generate_frame_png(output_path: str, seed: int = 42) -> str:
    """Создаёт PNG поляроид-рамки с рваным краем через Pillow."""
    rng = random.Random(seed)
    w, h = 1920, 1080
    x1, y1 = FRAME_INNER_X, FRAME_INNER_Y
    x2, y2 = x1 + FRAME_INNER_W, y1 + FRAME_INNER_H
    border, jitter, step = 28, 11, 6

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Тёмный полупрозрачный фон поверх bg-видео
    draw.rectangle([0, 0, w, h], fill=(0, 0, 0, 160))

    # Белая рамка
    draw.rectangle(
        [x1 - border, y1 - border, x2 + border, y2 + border],
        fill=(242, 237, 222, 255),
    )

    # Вырубаем прозрачное окно с рваным краем
    pts = []
    x = x1
    while x <= x2:
        pts.append((x, y1 + rng.randint(-jitter, jitter)))
        x += step
    y = y1
    while y <= y2:
        pts.append((x2 + rng.randint(-jitter, jitter), y))
        y += step
    x = x2
    while x >= x1:
        pts.append((x, y2 + rng.randint(-jitter, jitter)))
        x -= step
    y = y2
    while y >= y1:
        pts.append((x1 + rng.randint(-jitter, jitter), y))
        y -= step

    draw.polygon(pts, fill=(0, 0, 0, 0))
    img.save(output_path, "PNG")
    print(f"     ✓ Рамка: {output_path}")
    return output_path


def build_still_clip(
    still_path: str,
    duration: float,
    bg_video: str,
    frame_png: str,
    output_path: str,
    work_dir: str,
) -> str:
    """Создаёт анимированный стоп-кадр: bg-видео + Ken Burns + рамка."""
    fps = 30
    base = os.path.splitext(os.path.basename(still_path))[0]

    # Медленный пан: масштабируем кадр до 110%, двигаем crop-окно
    scaled_w = int(FRAME_INNER_W * 1.10)
    scaled_h = int(FRAME_INNER_H * 1.10)
    dx = scaled_w - FRAME_INNER_W   # пространство для горизонтального дрейфа
    dy = scaled_h - FRAME_INNER_H

    kb_path = os.path.join(work_dir, f"kb_{base}.mp4")
    run_ffmpeg([
        "ffmpeg", "-y",
        "-loop", "1", "-i", still_path,
        "-vf", (
            f"scale={scaled_w}:{scaled_h},"
            f"crop={FRAME_INNER_W}:{FRAME_INNER_H}"
            f":x='min(t/{duration:.3f}*{dx},{dx})'"
            f":y='min(t/{duration:.3f}*{dy},{dy})',"
            f"format=yuv420p,setsar=1"
        ),
        "-t", str(duration),
        *_venc(),
        "-r", str(fps), "-an",
        kb_path,
    ])

    # Финальный композит: bg + ken-burns still + рамка
    run_ffmpeg([
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", bg_video,
        "-i", kb_path,
        "-loop", "1", "-i", frame_png,
        "-t", str(duration),
        "-filter_complex", (
            f"[0:v]scale=1920:1080,format=yuv420p,setsar=1[bg];"
            f"[bg][1:v]overlay={FRAME_INNER_X}:{FRAME_INNER_Y}[v1];"
            f"[v1][2:v]overlay=0:0:format=auto[out]"
        ),
        "-map", "[out]",
        *_venc(),
        "-r", str(fps), "-an",
        output_path,
    ])
    return output_path


# ─────────────────────────────────────────────
# 4. МОНТАЖ ИНТРО / АУТРО
# ─────────────────────────────────────────────

def get_black_segments(video_path: str) -> list[tuple[float, float]]:
    """Возвращает список (start, end) чёрных сегментов через ffmpeg blackdetect."""
    cmd = [
        "ffmpeg", "-i", video_path,
        "-vf", "blackdetect=d=0.05:pix_th=0.10",
        "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    segments = []
    for line in result.stderr.split("\n"):
        if "black_start" in line:
            try:
                start = float(line.split("black_start:")[1].split()[0])
                end   = float(line.split("black_end:")[1].split()[0])
                segments.append((start, end))
            except (IndexError, ValueError):
                pass
    return segments


def _in_black(t: float, black_segs: list[tuple[float, float]], margin: float = 0.5) -> bool:
    return any(s - margin <= t <= e + margin for s, e in black_segs)


def build_montage(
    trailer_paths: list[str],
    total_duration: float,
    output_path: str,
    work_dir: str,
    clip_dur: float = MONTAGE_CLIP_DUR,
) -> str:
    """
    Нарезает короткие клипы из трейлеров и склеивает в монтаж заданной длины.
    Пропускает начало (логотипы/студии), конец (титры) и чёрные кадры.
    """
    n_clips = max(len(trailer_paths), int(total_duration / clip_dur))
    # ceil, чтобы скорее перебрать клипами, чем недобрать (иначе монтаж короче цели)
    clips_per = max(1, math.ceil(n_clips / len(trailer_paths)))

    clip_files = []
    idx = 0

    for trailer_path in trailer_paths:
        try:
            duration = get_audio_duration(trailer_path)
        except Exception:
            continue

        safe_start = MONTAGE_SKIP_START
        safe_end   = duration - MONTAGE_SKIP_END

        if safe_end - safe_start < clip_dur * 2:
            continue

        black_segs  = get_black_segments(trailer_path)
        segment_len = (safe_end - safe_start) / clips_per

        for i in range(clips_per):
            seg_s = safe_start + i * segment_len
            seg_e = seg_s + segment_len - clip_dur
            if seg_e <= seg_s:
                continue

            # Ищем момент вне чёрного кадра (до 10 попыток)
            for _ in range(10):
                t = random.uniform(seg_s, seg_e)
                if not _in_black(t, black_segs):
                    break

            clip_out = os.path.join(work_dir, f"montage_{idx:04d}.mp4")
            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{t:.3f}",
                "-i", trailer_path,
                "-t", str(clip_dur),
                "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
                       "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1",
                *_venc(),
                "-r", "30", "-an",
                clip_out,
            ]
            try:
                run_ffmpeg(cmd)
                clip_files.append(clip_out)
                idx += 1
            except subprocess.CalledProcessError:
                continue

    if not clip_files:
        raise RuntimeError("Не удалось извлечь клипы для монтажа")

    random.shuffle(clip_files)

    list_file = os.path.join(work_dir, f"montage_list_{os.path.basename(output_path)}.txt")
    with open(list_file, "w") as f:
        for c in clip_files:
            f.write(f"file '{os.path.abspath(c)}'\n")

    cmd = [
        "ffmpeg", "-y",
        # зацикливаем набор клипов, чтобы монтаж всегда был точно нужной длины
        # (доп. отрывки трейлеров вместо заморозки последнего кадра)
        "-stream_loop", "-1",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-t", str(total_duration),
        *_venc(),
        "-r", "30", "-an",
        output_path,
    ]
    run_ffmpeg(cmd)
    print(f"     ✓ Монтаж: {output_path} ({total_duration}s)")
    return output_path


# ─────────────────────────────────────────────
# 5. ХЕЛПЕРЫ ДЛЯ АНИМИРОВАННЫХ ТИТРОВ
# ─────────────────────────────────────────────

def _load_pil_font(size: int):
    for p in [
        "/System/Library/Fonts/Supplemental/Impact.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _star_polygon(cx: float, cy: float, r_outer: float, r_inner: float) -> list:
    pts = []
    for i in range(10):
        angle = math.radians(i * 36 - 90)
        r = r_outer if i % 2 == 0 else r_inner
        pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    return pts


def create_title_png(title: str, year: int, imdb_rating: float | None, output_path: str):
    """Создаёт PNG плашки с названием фильма — золотая плашка с закруглёнными углами."""
    font = _load_pil_font(44)
    stroke = 2
    title_text = f"{title.upper()}, {year}"

    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    tb = dummy.textbbox((0, 0), title_text, font=font, stroke_width=stroke)
    title_w = tb[2] - tb[0]
    title_h = tb[3] - tb[1]

    if imdb_rating is not None:
        rating_text = f"IMDB: {imdb_rating}"
        rb = dummy.textbbox((0, 0), rating_text, font=font, stroke_width=stroke)
        rating_w = rb[2] - rb[0]
        star_r = title_h // 2
        gap = 12
        total_w = title_w + gap + star_r * 2 + gap + rating_w
    else:
        rating_text = None
        rb = None
        total_w = title_w

    pad = 18
    img_w = total_w + pad * 2
    img_h = title_h + pad * 2

    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Золотая плашка с закруглёнными углами
    draw.rounded_rectangle([0, 0, img_w - 1, img_h - 1], radius=14,
                            fill=(212, 175, 55, 225))

    # Название
    draw.text((pad - tb[0], pad - tb[1]), title_text, font=font,
              fill=(255, 255, 255, 255), stroke_width=stroke, stroke_fill=(80, 40, 0, 255))

    if rating_text is not None:
        # Звезда как полигон (не зависит от поддержки юникода шрифтом)
        star_cx = pad + title_w + gap + star_r
        star_cy = img_h // 2
        draw.polygon(_star_polygon(star_cx, star_cy, star_r + 2, star_r * 0.4 + 1),
                     fill=(80, 40, 0, 255))
        draw.polygon(_star_polygon(star_cx, star_cy, star_r, star_r * 0.4),
                     fill=(255, 255, 255, 255))

        # Рейтинг
        rx = pad + title_w + gap + star_r * 2 + gap - rb[0]
        ry = pad - rb[1]
        draw.text((rx, ry), rating_text, font=font, fill=(255, 255, 255, 255),
                  stroke_width=stroke, stroke_fill=(80, 40, 0, 255))

    img.save(output_path, "PNG")


def create_number_png(number: int, output_path: str):
    """Создаёт PNG с номером на золотой медали."""
    text = str(number)
    font = _load_pil_font(100)
    stroke = 3

    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox = dummy.textbbox((0, 0), text, font=font, stroke_width=stroke)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    medal_r = max(tw, th) // 2 + 24
    size = medal_r * 2 + 14

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx, cy = size // 2, size // 2

    # Тень медали
    draw.ellipse([cx - medal_r + 5, cy - medal_r + 5, cx + medal_r + 5, cy + medal_r + 5],
                 fill=(0, 0, 0, 80))
    # Внешнее кольцо (тёмное золото)
    draw.ellipse([cx - medal_r, cy - medal_r, cx + medal_r, cy + medal_r],
                 fill=(160, 120, 10, 255))
    # Внутренний круг (яркое золото)
    inner_r = medal_r - 8
    draw.ellipse([cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r],
                 fill=(212, 175, 55, 255))

    # Цифра по центру медали
    tx = cx - (bbox[0] + bbox[2]) // 2
    ty = cy - (bbox[1] + bbox[3]) // 2
    draw.text((tx + 2, ty + 3), text, font=font, fill=(0, 0, 0, 90))   # тень
    draw.text((tx, ty), text, font=font, fill=(255, 255, 255, 255),
              stroke_width=stroke, stroke_fill=(80, 40, 0, 255))

    img.save(output_path, "PNG")


def create_corner_bug_png(number: int, title: str, year: int, output_path: str):
    """Создаёт полупрозрачную плашку с названием фильма для постоянного оверлея в углу."""
    font = _load_pil_font(26)
    text = f"#{number}  {title} ({year})"
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox = dummy.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pad_x, pad_y = 16, 10
    w = tw + pad_x * 2
    h = th + pad_y * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, w - 1, h - 1], fill=(0, 0, 0, 160))
    draw.text((pad_x - bbox[0], pad_y - bbox[1]), text, font=font, fill=(255, 255, 255, 230))
    img.save(output_path, "PNG")


def create_subscribe_png(output_path: str) -> str:
    """Создаёт PNG плашки SUBSCRIBE для оверлея на интро/аутро."""
    font_big   = _load_pil_font(32)
    font_small = _load_pil_font(15)
    stroke = 2
    main_text = "SUBSCRIBE"
    sub_text  = "& turn on notifications"

    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    mb = dummy.textbbox((0, 0), main_text, font=font_big,   stroke_width=stroke)
    sb = dummy.textbbox((0, 0), sub_text,  font=font_small)

    content_w = max(mb[2] - mb[0], sb[2] - sb[0])
    content_h = (mb[3] - mb[1]) + 8 + (sb[3] - sb[1])
    pad_x, pad_y = 20, 12

    img_w = content_w + pad_x * 2
    img_h = content_h + pad_y * 2

    img  = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Тень
    draw.rounded_rectangle([4, 4, img_w + 3, img_h + 3], radius=12, fill=(0, 0, 0, 110))
    # Красный фон
    draw.rounded_rectangle([0, 0, img_w - 1, img_h - 1], radius=12, fill=(220, 20, 20, 240))
    # Белая рамка
    draw.rounded_rectangle([2, 2, img_w - 3, img_h - 3], radius=10,
                            outline=(255, 255, 255, 160), width=2)

    cx = img_w // 2

    # Главный текст
    mw = mb[2] - mb[0]
    draw.text((cx - mw // 2 - mb[0], pad_y - mb[1]), main_text, font=font_big,
              fill=(255, 255, 255, 255), stroke_width=stroke, stroke_fill=(140, 0, 0, 255))

    # Подпись
    sw = sb[2] - sb[0]
    sub_y = pad_y + (mb[3] - mb[1]) + 10
    draw.text((cx - sw // 2 - sb[0], sub_y - sb[1]), sub_text, font=font_small,
              fill=(255, 210, 210, 220))

    img.save(output_path, "PNG")
    return output_path


def add_subscribe_banner(video_path: str, output_path: str, work_dir: str) -> str:
    """Накладывает анимированную плашку SUBSCRIBE на видео (fade in/out)."""
    sub_png = os.path.join(work_dir, "subscribe.png")
    if not os.path.exists(sub_png):
        create_subscribe_png(sub_png)

    dur = get_audio_duration(video_path)
    fade_in  = 2.0
    fade_out = max(fade_in + 2.0, dur - 2.5)

    fc = (
        f"[1]format=rgba,"
        f"fade=t=in:st={fade_in:.1f}:d=0.5:alpha=1,"
        f"fade=t=out:st={fade_out:.1f}:d=0.5:alpha=1[sub];"
        f"[0:v][sub]overlay=x=W-overlay_w-30:y=H-overlay_h-30:format=auto:eof_action=pass[out]"
    )
    run_ffmpeg([
        "ffmpeg", "-y",
        "-i", video_path,
        "-loop", "1", "-i", sub_png,
        "-filter_complex", fc,
        "-map", "[out]", "-map", "0:a",
        *_venc(), "-r", "30",
        "-c:a", "copy", "-shortest",
        output_path,
    ])
    return output_path


# ─────────────────────────────────────────────
# 5. СБОРКА СЕГМЕНТА (один фильм)
# ─────────────────────────────────────────────

def _render_poster_clip(poster_path: str | None, duration: float, output_path: str):
    if poster_path and os.path.exists(poster_path):
        fc = (
            "[0]scale=1920:1080:force_original_aspect_ratio=increase,"
            "crop=1920:1080:(iw-ow)/2:(ih-oh)/2,"
            "boxblur=20:4,eq=brightness=-0.3[bg];"
            "[0]scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1[fg];"
            "[bg][fg]overlay=0:0,setsar=1"
        )
        run_ffmpeg([
            "ffmpeg", "-y",
            "-loop", "1", "-t", str(duration), "-i", poster_path,
            "-filter_complex", fc,
            *_venc(),
            "-r", "30", "-an", output_path,
        ])
    else:
        run_ffmpeg([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s=1920x1080:d={duration}",
            *_venc(),
            "-r", "30", "-an", output_path,
        ])


def _concat_video_parts(parts: list[str], output_path: str):
    list_file = output_path + ".list.txt"
    with open(list_file, "w") as f:
        for p in parts:
            f.write(f"file '{os.path.abspath(p)}'\n")
    run_ffmpeg([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_file,
        *_venc(),
        "-r", "30", "-an", output_path,
    ])


def build_segment(
    number: int,
    title: str,
    year: int,
    voiceover_path: str,
    trailer_path: str | None,
    poster_path: str | None,
    output_path: str,
    work_dir: str,
    imdb_rating: float | None = None,
    stills: list[str] | None = None,
    frame_png: str | None = None,
    bg_video: str | None = None,
) -> str:
    import tempfile, shutil as _shutil
    stills = stills or []

    # Все крупные промежуточные файлы пишем в локальный /tmp/ чтобы избежать
    # коррупции при записи на медленный внешний диск через symlink
    tmp_dir = tempfile.mkdtemp(prefix=f"build_seg{number}_")
    try:
        return _build_segment_impl(
            number, title, year, voiceover_path, trailer_path, poster_path,
            output_path, work_dir, tmp_dir, imdb_rating, stills, frame_png, bg_video,
        )
    finally:
        _shutil.rmtree(tmp_dir, ignore_errors=True)


def _build_segment_impl(
    number: int,
    title: str,
    year: int,
    voiceover_path: str,
    trailer_path: str | None,
    poster_path: str | None,
    output_path: str,
    work_dir: str,
    tmp_dir: str,
    imdb_rating: float | None = None,
    stills: list[str] | None = None,
    frame_png: str | None = None,
    bg_video: str | None = None,
) -> str:
    stills = stills or []
    print(f"     → Длительность войсовера...")
    vo_duration = get_audio_duration(voiceover_path)
    remaining   = vo_duration - POSTER_DURATION   # секунд после постера

    parts = []

    # --- Постер ---
    print(f"     → Рендер постера...")
    poster_part = os.path.join(tmp_dir, f"seg{number}_poster.mp4")
    _render_poster_clip(poster_path, POSTER_DURATION, poster_part)
    parts.append(poster_part)

    # --- Пре-рендер стоп-кадров (кэшируются в work_dir/still_clips/) ---
    still_clips = []
    use_stills = stills and frame_png and bg_video and os.path.exists(bg_video or "")
    if use_stills:
        sc_dir = os.path.join(work_dir, "still_clips")
        os.makedirs(sc_dir, exist_ok=True)
        for i, sp in enumerate(stills):
            sc_path = os.path.join(sc_dir, f"seg{number}_still_{i+1}.mp4")
            if not os.path.exists(sc_path):
                print(f"     → Стоп-кадр {i+1}/{len(stills)}...")
                build_still_clip(sp, STILL_DURATION, bg_video, frame_png, sc_path, sc_dir)
            still_clips.append(sc_path)

    n_sc = len(still_clips)
    stills_budget = n_sc * STILL_DURATION   # секунд под стоп-кадры

    # --- Тело сегмента: трейлер + стоп-кадры ---
    if trailer_path:
        trimmed = os.path.join(tmp_dir, f"seg{number}_trailer_trimmed.mp4")
        print(f"     → Обрезка интро трейлера...")
        trim_trailer_intro(trailer_path, trimmed)
        try:
            trailer_avail = get_audio_duration(trimmed)
        except Exception:
            print(f"     ⚠  Trim повреждён, использую оригинал")
            import shutil
            shutil.copy2(trailer_path, trimmed)
            trailer_avail = get_audio_duration(trimmed)

        trailer_needed = max(remaining - stills_budget, 0.0)

        if n_sc > 0:
            if trailer_avail >= trailer_needed:
                # Трейлера хватает — делим на n_sc+1 равных кусков
                all_sc = still_clips
                n_chunks = n_sc + 1
                chunk_dur = trailer_needed / n_chunks
            else:
                # Трейлер короче нужного — добираем стоп-кадрами
                gap = remaining - trailer_avail - stills_budget
                extra = max(0, math.ceil(gap / STILL_DURATION)) if gap > 0 else 0
                all_sc = still_clips + [still_clips[i % n_sc] for i in range(extra)]
                n_chunks = len(all_sc) + 1
                chunk_dur = trailer_avail / n_chunks

            vf = ("scale=1920:1080:force_original_aspect_ratio=decrease,"
                  "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1"
                  + (f",{TRAILER_FILM_GRAIN}" if TRAILER_FILM_GRAIN else ""))
            for i in range(n_chunks):
                chunk_path = os.path.join(tmp_dir, f"seg{number}_tchunk_{i}.mp4")
                run_ffmpeg([
                    "ffmpeg", "-y",
                    "-ss", f"{i * chunk_dur:.3f}", "-i", trimmed,
                    "-t", f"{chunk_dur:.3f}",
                    "-vf", vf,
                    *_venc(),
                    "-r", "30", "-an", chunk_path,
                ])
                parts.append(chunk_path)
                if i < len(all_sc):
                    parts.append(all_sc[i])
        else:
            # Нет стоп-кадров — весь трейлер. Если трейлер короче нужного —
            # зацикливаем его (доп. отрывки), чтобы не морозить последний кадр.
            need = trailer_needed if trailer_needed > 0 else trailer_avail
            loop_args = ["-stream_loop", "-1"] if trailer_avail < need else []
            trailer_cut = os.path.join(tmp_dir, f"seg{number}_trailer_cut.mp4")
            run_ffmpeg([
                "ffmpeg", "-y",
                *loop_args,
                "-i", trimmed,
                "-t", str(need),
                "-vf", ("scale=1920:1080:force_original_aspect_ratio=decrease,"
                        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1"
                        + (f",{TRAILER_FILM_GRAIN}" if TRAILER_FILM_GRAIN else "")),
                *_venc(),
                "-r", "30", "-an", trailer_cut,
            ])
            parts.append(trailer_cut)
    else:
        # Нет трейлера
        if still_clips:
            gap = remaining - stills_budget
            extra = max(0, math.ceil(gap / STILL_DURATION)) if gap > 0 else 0
            all_sc = still_clips + [still_clips[i % n_sc] for i in range(extra)]
            parts.extend(all_sc)
        else:
            poster_full = os.path.join(tmp_dir, f"seg{number}_poster_full.mp4")
            _render_poster_clip(poster_path, remaining, poster_full)
            parts.append(poster_full)

    # --- Склейка ---
    concat_video = os.path.join(tmp_dir, f"seg{number}_concat.mp4")
    _concat_video_parts(parts, concat_video)

    # --- Анимированные титры ---
    title_png  = os.path.join(tmp_dir, f"seg{number}_title.png")
    number_png = os.path.join(tmp_dir, f"seg{number}_number.png")
    create_title_png(title, year, imdb_rating, title_png)
    create_number_png(number, number_png)

    fc = (
        "[1]format=rgba,fade=t=out:st=4.5:d=0.5:alpha=1[tf];"
        "[2]format=rgba,fade=t=out:st=9.5:d=0.5:alpha=1[nf];"
        "[0][tf]overlay="
        "x='if(lt(t,0.5),-overlay_w+(overlay_w+60)*2*t,60)':y=H-120:format=auto:eof_action=pass[v1];"
        "[v1][nf]overlay=x=W-overlay_w-40:y=25:format=auto:eof_action=pass"
    )
    overlay_video = os.path.join(tmp_dir, f"seg{number}_overlay.mp4")
    run_ffmpeg([
        "ffmpeg", "-y",
        "-i", concat_video,
        "-loop", "1", "-t", "5", "-i", title_png,
        "-loop", "1", "-t", "10", "-i", number_png,
        "-filter_complex", fc,
        *_venc(),
        "-r", "30", "-an", overlay_video,
    ])

    # --- Войсовер (tpad покрывает разницу между видео и аудио + запас 2s) ---
    corner_bug_png = os.path.join(tmp_dir, f"seg{number}_corner.png")
    create_corner_bug_png(number, title, year, corner_bug_png)

    overlay_dur = get_audio_duration(overlay_video)
    pad_needed = max(10.0, vo_duration - overlay_dur + 2.0)
    run_ffmpeg([
        "ffmpeg", "-y",
        "-i", overlay_video, "-i", voiceover_path,
        "-loop", "1", "-i", corner_bug_png,
        "-filter_complex",
        f"[0:v]tpad=stop_mode=clone:stop_duration={pad_needed:.1f}[vpad];"
        "[2:v]format=rgba,fade=t=in:st=5.0:d=0.5:alpha=1[cbug];"
        "[vpad][cbug]overlay=x=W-overlay_w-20:y=H-overlay_h-20:eof_action=repeat[v]",
        "-map", "[v]", "-map", "1:a",
        *_venc(), "-r", "30",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-shortest", output_path,
    ])

    actual_duration = get_audio_duration(output_path)
    if actual_duration < vo_duration * 0.9:
        print(f"     ⚠  Сегмент короче войсовера: {actual_duration:.1f}s vs {vo_duration:.1f}s — видео было короче аудио!")
    print(f"     ✓ Сегмент готов: {output_path} ({actual_duration:.1f}s, войсовер {vo_duration:.1f}s)")
    return output_path


# ─────────────────────────────────────────────
# 6. ФИНАЛЬНАЯ СБОРКА
# ─────────────────────────────────────────────

def assemble_final(segments: list[str], output_path: str, work_dir: str) -> str:
    """Склеивает все сегменты в финальное видео с паузой 0.5s между фильмами."""
    print(f"\n🎞  Финальная сборка ({len(segments)} сегментов)...")

    # Генерируем чёрную паузу между сегментами (один раз)
    pause_path = os.path.join(work_dir, "pause.mp4")
    if not os.path.exists(pause_path):
        run_ffmpeg([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=30:d=0.5",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", "0.5",
            *_venc(),
            "-c:a", "aac", "-b:a", "192k",
            pause_path,
        ])

    # Интро → фильм1 → пауза → фильм2 → пауза → … → фильм20 → аутро
    intro, *movies, outro = segments
    list_file = os.path.join(work_dir, "final_list.txt")
    with open(list_file, "w") as f:
        f.write(f"file '{os.path.abspath(intro)}'\n")
        for i, s in enumerate(movies):
            if i > 0:
                f.write(f"file '{os.path.abspath(pause_path)}'\n")
            f.write(f"file '{os.path.abspath(s)}'\n")
        f.write(f"file '{os.path.abspath(outro)}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy",
        "-movflags", "+faststart",
        output_path
    ]
    subprocess.run(cmd, check=True)
    print(f"✅  Готово: {output_path}")
    return output_path


# ─────────────────────────────────────────────
# 7. MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Movie Top Video Pipeline")
    parser.add_argument("movies_json", help="JSON файл со списком фильмов")
    parser.add_argument("--output", default=None, help="Путь к финальному видео (по умолчанию — из имени входного JSON)")
    parser.add_argument("--work-dir", default=None, help="Рабочая директория (по умолчанию — из имени выходного файла)")
    parser.add_argument("--skip-voiceover", action="store_true", help="Пропустить генерацию войсовера (использовать готовые MP3)")
    args = parser.parse_args()

    with open(args.movies_json, encoding="utf-8") as f:
        data = json.load(f)

    movies   = data["movies"]
    # Голос управляется централизованно в коде (DEFAULT_VOICE_ID);
    # поле voice_id в JSON игнорируется.
    voice_id = DEFAULT_VOICE_ID

    # По умолчанию имя вывода и воркдир выводятся из имени входного JSON,
    # чтобы разные сборки не затирали друг друга (общий final.mp4).
    input_stem = os.path.splitext(os.path.basename(args.movies_json))[0]
    if args.output is None:
        args.output = f"{input_stem}.mp4"
    if args.work_dir is None:
        stem = os.path.splitext(os.path.basename(args.output))[0]
        args.work_dir = f"./pipeline_work_{stem}"

    work_dir = args.work_dir
    os.makedirs(work_dir, exist_ok=True)
    for sub in ("trailers", "posters", "voiceovers", "segments", "montage", "stills", "still_clips"):
        os.makedirs(os.path.join(work_dir, sub), exist_ok=True)

    # Генерируем PNG рамки один раз
    frame_png = os.path.join(work_dir, "frame.png")
    bg_video  = BG_VIDEO_PATH if os.path.exists(BG_VIDEO_PATH) else None
    if bg_video and not os.path.exists(frame_png):
        print("\n🖼  Генерация рамки (поляроид)...")
        generate_frame_png(frame_png)
    elif not bg_video:
        print(f"\n⚠  Фоновое видео не найдено ({BG_VIDEO_PATH}), стоп-кадры отключены")
        frame_png = None

    # ── Шаг 1: скачиваем все трейлеры сразу ──────────────────────────────
    print("\n📥  Загрузка трейлеров...")
    trailer_map: dict[int, str] = {}
    for movie in movies:
        trailer = download_trailer(
            movie["title"], movie["year"], os.path.join(work_dir, "trailers")
        )
        if trailer:
            trailer_map[movie["number"]] = trailer
        else:
            print(f"     ⚠  Трейлер не найден для: {movie['title']} — фильм будет пропущен")

    # ── Шаг 2: интро ─────────────────────────────────────────────────────
    intro_path = os.path.join(work_dir, "intro.mp4")
    intro_vo_text = data.get("intro_voiceover")
    intro_vid_hash_file = intro_path + ".hash"
    intro_vid_hash = hashlib.md5((intro_vo_text or "").encode()).hexdigest()
    intro_needs_rebuild = (
        not os.path.exists(intro_path)
        or not os.path.exists(intro_vid_hash_file)
        or open(intro_vid_hash_file).read().strip() != intro_vid_hash
    )
    if intro_needs_rebuild:
        print("\n🎬  Сборка интро...")
        intro_vo = None
        intro_duration = INTRO_DURATION
        if intro_vo_text and not args.skip_voiceover:
            intro_vo = os.path.join(work_dir, "intro_vo.mp3")
            intro_hash_file = intro_vo + ".hash"
            text_hash = hashlib.md5(intro_vo_text.encode()).hexdigest()
            if (not os.path.exists(intro_vo) or not os.path.exists(intro_hash_file)
                    or open(intro_hash_file).read().strip() != text_hash):
                generate_voiceover(intro_vo_text, intro_vo, voice_id)
                open(intro_hash_file, "w").write(text_hash)
            intro_duration = get_audio_duration(intro_vo)

        intro_silent = os.path.join(work_dir, "intro_silent.mp4")
        build_montage(list(trailer_map.values()), intro_duration, intro_silent,
                      os.path.join(work_dir, "montage"))
        if intro_vo:
            vid_dur = get_audio_duration(intro_silent)
            vo_dur  = get_audio_duration(intro_vo)
            pad_sec = max(0.5, vo_dur - vid_dur + 1.0)
            run_ffmpeg([
                "ffmpeg", "-y",
                "-i", intro_silent, "-i", intro_vo,
                "-filter_complex", f"[0:v]tpad=stop_mode=clone:stop_duration={pad_sec:.1f}[v]",
                "-map", "[v]", "-map", "1:a",
                *_venc(), "-r", "30",
                "-c:a", "aac", "-b:a", "192k", "-ac", "2",
                "-shortest", intro_path,
            ])
        else:
            os.rename(intro_silent, intro_path)
        print(f"     → Плашка Subscribe (интро)...")
        sub_tmp = intro_path + ".sub.tmp.mp4"
        add_subscribe_banner(intro_path, sub_tmp, work_dir)
        os.replace(sub_tmp, intro_path)
        open(intro_vid_hash_file, "w").write(intro_vid_hash)

    # ── Шаг 3: сегменты фильмов ───────────────────────────────────────────
    segments = []
    for movie in movies:
        number      = movie["number"]
        title       = movie["title"]
        year        = movie["year"]
        script      = movie["voiceover_text"]
        imdb_rating = movie.get("imdb_rating")

        print(f"\n[{number}] {title} ({year})")

        safe_t   = "".join(c for c in title if c.isalnum() or c in " _-").strip().replace(" ", "_")
        vo_path  = os.path.join(work_dir, "voiceovers", f"{number:02d}_{safe_t}.mp3")
        seg_path = os.path.join(work_dir, "segments",   f"{number:02d}_{safe_t}.mp4")

        if os.path.exists(seg_path):
            print(f"     ↩  Сегмент уже есть, пропускаем")
            segments.append(seg_path)
            continue

        if not os.path.exists(vo_path):
            if args.skip_voiceover:
                print(f"     ⚠  Войсовер не найден и --skip-voiceover включён, пропускаем {title}")
                continue
            generate_voiceover(script, vo_path, voice_id)

        print(f"  🖼  Скачиваем постер и стоп-кадры...")
        poster  = download_poster(title, year, os.path.join(work_dir, "posters"))
        stills  = []
        if frame_png and bg_video:
            stills = download_movie_stills(title, year, N_STILLS, os.path.join(work_dir, "stills"))

        print(f"  🔧  Сборка сегмента...")
        build_segment(
            number=number,
            title=title,
            year=year,
            voiceover_path=vo_path,
            trailer_path=trailer_map.get(number),
            poster_path=poster,
            output_path=seg_path,
            work_dir=work_dir,
            imdb_rating=imdb_rating,
            stills=stills,
            frame_png=frame_png,
            bg_video=bg_video,
        )

        segments.append(seg_path)

    if not segments:
        print("❌  Нет готовых сегментов для сборки")
        sys.exit(1)

    # ── Шаг 4: аутро ─────────────────────────────────────────────────────
    outro_path = os.path.join(work_dir, "outro.mp4")
    outro_vo_text = data.get("outro_voiceover")
    outro_vid_hash_file = outro_path + ".hash"
    outro_vid_hash = hashlib.md5((outro_vo_text or "").encode()).hexdigest()
    outro_needs_rebuild = (
        not os.path.exists(outro_path)
        or not os.path.exists(outro_vid_hash_file)
        or open(outro_vid_hash_file).read().strip() != outro_vid_hash
    )
    if outro_needs_rebuild:
        print("\n🎬  Сборка аутро...")
        outro_vo = None
        outro_duration = OUTRO_DURATION
        if outro_vo_text and not args.skip_voiceover:
            outro_vo = os.path.join(work_dir, "outro_vo.mp3")
            outro_hash_file = outro_vo + ".hash"
            text_hash = hashlib.md5(outro_vo_text.encode()).hexdigest()
            if (not os.path.exists(outro_vo) or not os.path.exists(outro_hash_file)
                    or open(outro_hash_file).read().strip() != text_hash):
                generate_voiceover(outro_vo_text, outro_vo, voice_id)
                open(outro_hash_file, "w").write(text_hash)
            outro_duration = get_audio_duration(outro_vo)

        outro_silent = os.path.join(work_dir, "outro_silent.mp4")
        build_montage(list(trailer_map.values()), outro_duration, outro_silent,
                      os.path.join(work_dir, "montage"))
        if outro_vo:
            vid_dur = get_audio_duration(outro_silent)
            vo_dur  = get_audio_duration(outro_vo)
            pad_sec = max(0.5, vo_dur - vid_dur + 1.0)
            run_ffmpeg([
                "ffmpeg", "-y",
                "-i", outro_silent, "-i", outro_vo,
                "-filter_complex", f"[0:v]tpad=stop_mode=clone:stop_duration={pad_sec:.1f}[v]",
                "-map", "[v]", "-map", "1:a",
                *_venc(), "-r", "30",
                "-c:a", "aac", "-b:a", "192k", "-ac", "2",
                "-shortest", outro_path,
            ])
        else:
            os.rename(outro_silent, outro_path)
        print(f"     → Плашка Subscribe (аутро)...")
        sub_tmp = outro_path + ".sub.tmp.mp4"
        add_subscribe_banner(outro_path, sub_tmp, work_dir)
        os.replace(sub_tmp, outro_path)
        open(outro_vid_hash_file, "w").write(outro_vid_hash)

    # ── Шаг 5: финальная сборка ───────────────────────────────────────────
    assemble_final([intro_path] + segments + [outro_path], args.output, work_dir)

    # ── Шаг 6: очистка промежуточных файлов ──────────────────────────────
    _cleanup_work_dir(work_dir)


def _cleanup_work_dir(work_dir: str):
    """Удаляет крупные промежуточные файлы после успешной сборки."""
    import shutil
    dirs_to_remove = ["trailers", "stills", "still_clips", "montage"]
    patterns_to_remove = ["seg*_concat.mp4", "seg*_overlay.mp4", "seg*_poster.mp4",
                          "seg*_still*.mp4", "seg*_tchunk*.mp4", "seg*_list.txt",
                          "seg*_title.png", "seg*_number.png", "final_list.txt"]
    freed = 0
    for d in dirs_to_remove:
        path = os.path.join(work_dir, d)
        if os.path.isdir(path):
            freed += sum(
                os.path.getsize(os.path.join(r, f))
                for r, _, files in os.walk(path) for f in files
            )
            shutil.rmtree(path, ignore_errors=True)
    import glob
    for pattern in patterns_to_remove:
        for f in glob.glob(os.path.join(work_dir, pattern)):
            freed += os.path.getsize(f)
            os.remove(f)
    print(f"🧹  Очистка: освобождено {freed // 1024 // 1024} МБ")


if __name__ == "__main__":
    main()
