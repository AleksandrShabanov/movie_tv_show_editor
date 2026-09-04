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
from PIL import Image, ImageDraw, ImageFont, ImageFilter
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
    даёт кодировщику стабильный бюджет бит на каждом проходе.
    Битрейт рассчитан под 4K (см. OUT_W/OUT_H) — YouTube рекомендует
    ~35-45 Mbps для 2160p30, берём с запасом на множественные перекодировки."""
    if _HW_ENCODER == "h264_videotoolbox":
        args = ["-c:v", "h264_videotoolbox", "-b:v", "45M", "-maxrate", "60M",
                 "-bufsize", "80M", "-allow_sw", "1"]
    else:
        args = ["-c:v", "libx264", "-preset", "medium", "-crf", "16"]
    if pix_fmt:
        args += ["-pix_fmt", "yuv420p"]
    return args


# ─────────────────────────────────────────────
# КОНФИГ — ключи читаются из .env
# ─────────────────────────────────────────────

# Выходное разрешение ролика. Было 1920x1080 — переход на 4K по запросу
# пользователя (2026-08-04). Все scale/pad-фильтры и координаты оверлеев
# ниже выражены через эти константы (или ×2 от прежних 1080p-значений),
# чтобы при откате на 1080p достаточно было поменять только эти две строки.
OUT_W, OUT_H = 3840, 2160
LUMEAN_API_KEY = os.getenv("LUMEAN_API_KEY")
TMDB_API_KEY   = os.getenv("TMDB_API_KEY")
OMDB_API_KEY   = os.getenv("OMDB_API_KEY")
LUMEAN_BASE    = "https://api.lumean.app/api/public"
# Необязательно: заранее созданный template — если задан, шаг создания пропускается
LUMEAN_TEMPLATE_ID = os.getenv("LUMEAN_TEMPLATE_ID")

# Голос по умолчанию (ElevenLabs voice_id)
# DEFAULT_VOICE_ID = "iP95p4xoKVk53GoZ742B"  # старый голос — оставлен на всякий случай
# DEFAULT_VOICE_ID = "JZ3e95uoTACVf6tXaaEi"    # предыдущий голос
# DEFAULT_VOICE_ID = "q5MmJjJQLLpmdheE5NxW"    # предыдущий голос — не понравился
DEFAULT_VOICE_ID = "65dhNaIr3Y4ovumVtdy0"    # новый голос — тест

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
TEASER_CLIP_DUR   = 1.0   # секунд на вспышку тизера #1/#2 в начале интро

# Сколько секунд показывать постер перед трейлером (с Ken Burns)
POSTER_DURATION = 4

# Стоп-кадры (поляроид-эффект)
STILL_DURATION  = 6      # секунд на один стоп-кадр
N_STILLS        = 7      # стоп-кадров на фильм

# ЭКСПЕРИМЕНТ (отключено): тело сегмента — только нарезка трейлера на клипы
# ≤4с подряд, без стоп-кадров TMDB и без длинных непрерывных кусков трейлера.
# Гипотеза была, что конкурент так убирает вопросы по авторским правам —
# не подтвердилась: YouTube заблокировал монетизацию на видео, собранном
# по этой схеме (top20_mummy_movies). Вернулись на старую схему (трейлер
# длинными кусками + стоп-кадры), код эксперимента оставлен ниже под `if`,
# ничего не удалено — переключается этим флагом.
RAPID_TRAILER_CUTS = False
RAPID_CUT_DURATION = 4.0  # секунд, максимальная длина одного клипа трейлера
FRAME_INNER_W   = 2500   # ширина окна рамки (было 1250 под 1080p, ×2 под 4K)
FRAME_INNER_H   = 1406   # высота (было 703, ×2)
FRAME_INNER_X   = (OUT_W - FRAME_INNER_W) // 2
FRAME_INNER_Y   = (OUT_H - FRAME_INNER_H) // 2
BG_VIDEO_PATH   = os.path.expanduser("~/12988171_3840_2160_25fps.mp4")  # уже нативно 4K

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
# ВИЗУАЛЬНЫЕ СТИЛИ ВИДЕО (чередование ради разнообразия для зрителя/алгоритма)
# ─────────────────────────────────────────────
# Каждое новое видео получает следующий стиль по кругу — см. _get_visual_style().
# "classic": рамка-поляроид на стоп-кадрах + переход «прогорание плёнки».
# "fullscreen": стоп-кадр во весь экран с Ken Burns зумом + переход VHS-глитч.
VISUAL_STYLES = [
    {"name": "classic",    "still_style": "framed",   "transition": "burn"},
    {"name": "fullscreen", "still_style": "fullbleed", "transition": "vhs_glitch"},
]
# Глобальный счётчик (какое по счёту видео) — лежит рядом с pipeline.py,
# не в work_dir, чтобы переживать разные видео с разными work_dir.
STYLE_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".style_counter.json")


def _get_visual_style(work_dir: str) -> dict:
    """Возвращает визуальный стиль для этого видео, кэшируя выбор в
    work_dir/visual_style.json — чтобы повторные запуски/резюме сборки ОДНОГО
    и того же видео не меняли стиль на середине пути. Новый work_dir получает
    следующий стиль по кругу через общий счётчик STYLE_STATE_FILE."""
    cache_file = os.path.join(work_dir, "visual_style.json")
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            return json.load(f)

    counter = 0
    if os.path.exists(STYLE_STATE_FILE):
        try:
            with open(STYLE_STATE_FILE) as f:
                counter = json.load(f).get("counter", 0)
        except Exception:
            counter = 0

    style = VISUAL_STYLES[counter % len(VISUAL_STYLES)]

    with open(STYLE_STATE_FILE, "w") as f:
        json.dump({"counter": counter + 1}, f)
    os.makedirs(work_dir, exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump(style, f)

    print(f"\n🎨  Визуальный стиль этого видео: {style['name']}"
          f" (стоп-кадры: {style['still_style']}, переход: {style['transition']})")
    return style


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
                # similarity_boost понижен ниже рекомендованных Lumean/ElevenLabs
                # 0.75 (эксперимент, было 0.80 → 0.75 → 0.70) — по их документации
                # завышенная схожесть сама может провоцировать артефакты, что
                # похоже на природу остаточного дрейфа тона между сегментами.
                # style обнулён по той же логике; stability уже на максимуме.
                "stability": 1.0, "similarity_boost": 0.70, "style": 0.0,
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


def _generate_batched_voiceovers(
    segments_spec: list[tuple[str, str]],
    work_dir: str,
    voice_id: str = DEFAULT_VOICE_ID,
) -> None:
    """Генерирует войсоверы для интро + всех фильмов + аутро ОДНИМ TTS-заказом
    и режет результат по alignment-таймингам на отдельные mp3.

    Отдельные /orders-заказы у Lumean НЕ разделяют непрерывность голоса между
    собой (в отличие от чанков ВНУТРИ одного заказа, которые Lumean сшивает
    гладко — проверено вживую: ни щелчков, ни провалов в тишину на границах
    чанков). Поэтому один большой заказ на весь ролик даёт заметно более
    ровный тон по всему видео, ценой того, что правка текста любого одного
    фильма требует перегенерации всей озвучки целиком.

    segments_spec: список (output_path, text) в порядке появления в ролике.
    """
    if not segments_spec:
        return

    combined_text = " ".join(text for _, text in segments_spec)
    combined_hash = hashlib.md5(combined_text.encode()).hexdigest()
    hash_file = os.path.join(work_dir, "combined_vo.hash")

    all_exist = all(os.path.exists(p) for p, _ in segments_spec)
    if (all_exist and os.path.exists(hash_file)
            and open(hash_file).read().strip() == combined_hash):
        print(f"  ↩  Озвучка (батч, {len(segments_spec)} блоков) уже готова, пропускаем")
        return

    print(f"  🎙  Батч-генерация озвучки ({len(segments_spec)} блоков, "
          f"{len(combined_text)} символов)...")

    # Смещения символов каждого блока в объединённом тексте (для нарезки)
    offsets = []
    pos = 0
    for i, (_, text) in enumerate(segments_spec):
        offsets.append(pos)
        pos += len(text)
        if i < len(segments_spec) - 1:
            pos += 1  # разделяющий пробел

    template_id = _lumean_template_id(voice_id)
    order = _lumean_request("POST", "/orders",
                            {"template_id": template_id, "input_text": combined_text})["data"]
    order_id = order["id"]
    print(f"     Order ID: {order_id} — ожидаем...")

    TERMINAL_OK  = {"completed", "result_delivered"}
    TERMINAL_BAD = {"failed", "cancelled", "compensated"}
    files, service_files = None, None
    last_status = None
    for _ in range(1200):  # 1200 × 3s = 60 мин
        time.sleep(3)
        try:
            d = _lumean_request("GET", f"/orders/{order_id}", retries=5)["data"]
        except Exception:
            continue
        status = d.get("status", "")
        if status != last_status:
            cc, tc = d.get("completed_chunks"), d.get("total_chunks")
            suffix = f" (чанки {cc}/{tc})" if tc else ""
            print(f"     Статус: {status}{suffix}")
            last_status = status
        if status in TERMINAL_OK:
            result = d.get("result") or {}
            files = result.get("files") or []
            service_files = result.get("service_files") or []
            if not files:
                raise RuntimeError(f"Lumean: батч завершён, но нет аудиофайла: {d}")
            break
        if status in TERMINAL_BAD:
            raise RuntimeError(f"Lumean: батч-заказ провален ({status}): {d}")
    else:
        raise TimeoutError("Батч-озвучка не готова за 60 минут")

    align_path = next((p for p in service_files if p.endswith("result.json")), None)
    if not align_path:
        raise RuntimeError(f"Lumean: нет alignment result.json в service_files: {service_files}")

    combined_mp3 = os.path.join(work_dir, "combined_vo.mp3")
    dl_url = _lumean_request("POST", "/storage/url", {"path": files[0]})["data"]["url"]
    audio = requests.get(dl_url, timeout=300)
    audio.raise_for_status()
    with open(combined_mp3, "wb") as f:
        f.write(audio.content)

    align_url = _lumean_request("POST", "/storage/url", {"path": align_path})["data"]["url"]
    align_resp = requests.get(align_url, timeout=120)
    align_resp.raise_for_status()
    align_data = align_resp.json()
    starts = align_data["alignment"]["character_start_times_seconds"]
    total_duration = align_data.get("duration_seconds") or get_audio_duration(combined_mp3)

    # Нормализуем громкость ОДИН раз на весь непрерывный файл, а не по кускам
    # после нарезки: однопроходный loudnorm "разгоняется" первые секунды
    # каждого запуска, и 22 независимых запуска (интро+фильмы+аутро) давали
    # слышимый/видимый на волновой форме скачок ровно на каждой склейке.
    normalized_mp3 = os.path.join(work_dir, "combined_vo_normalized.mp3")
    _loudnorm_whole_file(combined_mp3, normalized_mp3)

    print(f"     → Нарезка на {len(segments_spec)} файлов по alignment...")
    FADE = 0.15  # секунд
    for i, (out_path, _) in enumerate(segments_spec):
        start = starts[offsets[i]] if offsets[i] > 0 else 0.0
        end = starts[offsets[i + 1]] if i + 1 < len(segments_spec) else total_duration
        dur = end - start
        fade = min(FADE, dur / 3)
        run_ffmpeg([
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
            "-i", normalized_mp3,
            # alignment по символам не идеально точен — рез иногда попадает
            # в хвост согласного соседнего слова ("мм"/"нн" на стыке фильмов).
            # Короткий fade на краях глушит этот призвук, не обрезая контент.
            "-af", f"afade=t=in:st=0:d={fade:.3f},afade=t=out:st={dur - fade:.3f}:d={fade:.3f}",
            "-c:a", "libmp3lame", "-q:a", "2",
            out_path,
        ])

    os.remove(combined_mp3)
    os.remove(normalized_mp3)
    open(hash_file, "w").write(combined_hash)
    print(f"     ✓ Батч-озвучка готова ({len(segments_spec)} файлов)")


def _loudnorm_whole_file(src_path: str, dst_path: str) -> None:
    """Двухпроходная нормализация громкости (EBU R128) всего файла целиком.

    Двухпроходный режим (measure → linear=true с измеренными параметрами)
    точнее однопроходного и не имеет "разгонного" переходного процесса —
    важно, т.к. это единственная нормализация на весь ролик (см. вызов
    в _generate_batched_voiceovers)."""
    target = "I=-16:TP=-1.5:LRA=11"
    measure = subprocess.run(
        ["ffmpeg", "-y", "-i", src_path,
         "-af", f"loudnorm={target}:print_format=json",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    stats = None
    try:
        json_text = measure.stderr[measure.stderr.rindex("{"):measure.stderr.rindex("}") + 1]
        stats = json.loads(json_text)
    except (ValueError, json.JSONDecodeError):
        pass

    if stats:
        run_ffmpeg([
            "ffmpeg", "-y", "-i", src_path,
            "-af", (
                f"loudnorm={target}:"
                f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
                f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:"
                f"offset={stats['target_offset']}:linear=true"
            ),
            "-ar", "44100",
            dst_path,
        ])
    else:
        # Не удалось измерить — откатываемся на однопроходный режим,
        # это лучше, чем оставить звук вовсе без нормализации.
        run_ffmpeg([
            "ffmpeg", "-y", "-i", src_path,
            "-af", f"loudnorm={target}",
            dst_path,
        ])


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
# 1.5 ФОНОВАЯ МУЗЫКА (ElevenLabs Music через Lumean)
# ─────────────────────────────────────────────
# Библиотека генерируется ОДИН РАЗ (build_music_library), треки живут в
# assets/music/ и переиспользуются во всех будущих сборках — как и
# CTA-ролик (см. subscribe-cta-feature), это не per-video контент.

MUSIC_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "music")

# Каждый вызов Lumean/ElevenLabs Music отдаёт ровно 30 секунд — жёсткое
# ограничение API, не настройка.
MUSIC_CLIP_DURATION_S = 30.0

MUSIC_MOODS: dict[str, str] = {
    "tension_horror": "dark moody cinematic horror tension, orchestral strings, "
                       "slow building dread, atmospheric, no vocals, no lyrics",
    "action":         "high energy cinematic action orchestral score, driving percussion, "
                       "intense and propulsive, no vocals, no lyrics",
    "campy":          "quirky playful campy b-movie score, retro synth and orchestral mix, "
                       "cheeky mischievous tone, no vocals, no lyrics",
    "drama":          "elegiac cinematic drama score, emotional strings and piano, "
                       "reflective and cinematic, no vocals, no lyrics",
    "eerie_mystery":  "eerie supernatural mystery score, unsettling ambient textures, "
                       "sparse and haunting, no vocals, no lyrics",
    "epic_adventure": "epic adventure orchestral score, sweeping heroic themes, "
                       "grand and cinematic, no vocals, no lyrics",
}

def _lumean_music_template_id(prompt: str) -> str:
    """Создаёт одноразовый Lumean-шаблон для музыкального промпта (в отличие от
    TTS-шаблонов голоса, не кэшируется — каждый вызов сам по себе даёт новую
    вариацию, кэшировать template_id по промпту бессмысленно)."""
    body = {
        "service_key": "music",
        "name": f"movie-editor music {abs(hash(prompt)) % 10**8}",
        "config": {"prompt": prompt},
    }
    return _lumean_request("POST", "/templates", body)["data"]["id"]


def generate_music_clip(prompt: str, output_path: str) -> str:
    """Генерирует один ~30-сек клип фоновой музыки через Lumean (11L Music
    Generation / ElevenLabs Music) и скачивает его в output_path."""
    template_id = _lumean_music_template_id(prompt)
    order = _lumean_request("POST", "/orders", {"template_id": template_id})["data"]
    order_id = order["id"]

    TERMINAL_OK  = {"completed", "result_delivered"}
    TERMINAL_BAD = {"failed", "cancelled", "compensated"}

    files = None
    for _ in range(200):  # 200 × 3s = 10 мин запаса
        time.sleep(3)
        data = _lumean_request("GET", f"/orders/{order_id}", retries=5)["data"]
        if data["status"] in TERMINAL_OK:
            files = (data.get("result") or {}).get("files")
            break
        if data["status"] in TERMINAL_BAD:
            raise RuntimeError(f"Lumean music order {order_id} завершился с ошибкой: {data}")
    if not files:
        raise RuntimeError(f"Lumean music order {order_id}: таймаут ожидания результата")

    dl_url = _lumean_request("POST", "/storage/url", {"path": files[0]})["data"]["url"]
    audio = requests.get(dl_url, timeout=60)
    audio.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(audio.content)

    try:
        requests.delete(f"{LUMEAN_BASE}/templates/{template_id}",
                        headers={"X-API-KEY": LUMEAN_API_KEY}, timeout=15)
    except Exception:
        pass  # шаблон одноразовый, не страшно если удаление не прошло

    return output_path


def _crossfade_chain(clips: list[str], crossfade_dur: float, output_path: str) -> str:
    """Склеивает список аудио-клипов в один трек через цепочку acrossfade —
    даёт плавные переходы вместо жёстких склеек между отдельными генерациями."""
    if len(clips) == 1:
        import shutil as _shutil
        _shutil.copy2(clips[0], output_path)
        return output_path

    inputs = []
    for c in clips:
        inputs += ["-i", c]

    filter_parts = []
    prev = "0:a"
    for i in range(1, len(clips)):
        label = f"cf{i}"
        filter_parts.append(f"[{prev}][{i}:a]acrossfade=d={crossfade_dur}:c1=tri:c2=tri[{label}]")
        prev = label
    fc = ";".join(filter_parts)

    run_ffmpeg([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", fc,
        "-map", f"[{prev}]",
        "-c:a", "libmp3lame", "-q:a", "2",
        output_path,
    ])
    return output_path


MUSIC_BED_TARGET_I = "I=-23:TP=-2:LRA=11"  # цель нормализации подложки — тише голоса (-16 LUFS)


def _normalize_music_track(src_path: str, dst_path: str) -> None:
    """Двухпроходная EBU R128 нормализация muud-трека к единому опорному
    уровню (-23 LUFS) — чтобы разные генерации не звучали громче/тише друг
    друга при подмешивании (см. add_background_music)."""
    measure = subprocess.run(
        ["ffmpeg", "-y", "-i", src_path,
         "-af", f"loudnorm={MUSIC_BED_TARGET_I}:print_format=json",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    try:
        json_text = measure.stderr[measure.stderr.rindex("{"):measure.stderr.rindex("}") + 1]
        stats = json.loads(json_text)
    except (ValueError, json.JSONDecodeError):
        stats = None

    if stats:
        run_ffmpeg([
            "ffmpeg", "-y", "-i", src_path,
            "-af", (
                f"loudnorm={MUSIC_BED_TARGET_I}:"
                f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
                f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:"
                f"offset={stats['target_offset']}:linear=true"
            ),
            "-ar", "44100",
            dst_path,
        ])
    else:
        run_ffmpeg(["ffmpeg", "-y", "-i", src_path, "-af", f"loudnorm={MUSIC_BED_TARGET_I}", dst_path])


def build_music_library(assets_dir: str = MUSIC_ASSETS_DIR,
                        n_clips_per_mood: int = 9,
                        crossfade: float = 1.5) -> None:
    """Разовая генерация библиотеки фоновой музыки: по одному ~4-5-минутному
    треку на настроение (склейка n_clips_per_mood 30-сек генераций через
    кроссфейд), нормализованному к единому опорному уровню громкости.
    Пропускает уже существующие файлы — безопасно перезапускать."""
    os.makedirs(assets_dir, exist_ok=True)

    for mood, prompt in MUSIC_MOODS.items():
        out_path = os.path.join(assets_dir, f"mood_{mood}.mp3")
        if os.path.exists(out_path):
            print(f"  ↩  Muud-трек уже есть: {out_path}")
            continue
        print(f"  🎵  Генерация muud-трека «{mood}» ({n_clips_per_mood} клипов)...")
        clips = []
        for i in range(n_clips_per_mood):
            clip_path = os.path.join(assets_dir, f"_tmp_{mood}_{i}.mp3")
            print(f"     → клип {i+1}/{n_clips_per_mood}...")
            generate_music_clip(prompt, clip_path)
            clips.append(clip_path)
        raw_path = os.path.join(assets_dir, f"_raw_{mood}.mp3")
        _crossfade_chain(clips, crossfade, raw_path)
        _normalize_music_track(raw_path, out_path)
        for c in clips:
            os.remove(c)
        os.remove(raw_path)
        print(f"     ✓ {out_path}")


def add_background_music(video_path: str, mood: str, output_path: str,
                         relative_db: float = -14.0) -> str:
    """Подмешивает жанровый muud-трек фоном под уже собранный финальный
    ролик — один непрерывный трек на интро+сегменты+аутро (зацикливается
    через -stream_loop, тот же приём, что и в build_montage для трейлеров).
    normalize=0 в amix обязателен — иначе весь микс проседает по громкости
    целиком, даже там, где музыка почти не слышна (тот же баг, что был у
    subscribe CTA, см. add_subscribe_cta)."""
    track_path = os.path.join(MUSIC_ASSETS_DIR, f"mood_{mood}.mp3")
    if not os.path.exists(track_path):
        raise FileNotFoundError(f"Muud-трек не найден: {track_path} "
                                f"(доступные: {', '.join(MUSIC_MOODS)})")

    duration = get_audio_duration(video_path)
    fade_out_start = max(duration - 2.0, 0.0)

    filter_complex = (
        f"[1:a]volume={relative_db}dB,afade=t=out:st={fade_out_start:.3f}:d=2[music];"
        f"[0:a][music]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
    )
    run_ffmpeg([
        "ffmpeg", "-y",
        "-i", video_path,
        "-stream_loop", "-1", "-i", track_path,
        "-filter_complex", filter_complex,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-movflags", "+faststart",
        "-shortest",
        output_path,
    ])
    print(f"     ✓ Фоновая музыка ({mood}) подмешана: {output_path}")
    return output_path


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


# Каналы-агрегаторы с водяными знаками — исключаем из поиска на YouTube.
# 21 августа 2026: "Rotten Tomatoes Classic Trailers"/"Rotten Tomatoes Trailers"
# добавлены после того, как их аплоад Pearl Harbor (2001) оказался несущим ту же
# вотермарку MOVIECLIPS.com и фирменную концовку "CLICK TO SUBSCRIBE" — Rotten
# Tomatoes и MovieClips коммерчески аффилированы через Fandango, и их "трейлеры"
# могут быть теми же мастер-файлами. Уполномоченного загрузчика недостаточно
# проверить по имени — при любых новых жалобах на вотермарки в первую очередь
# смотреть, не входит ли канал в семью Fandango/MovieClips/Rotten Tomatoes.
# 1 сентября 2026: "Cultpix" добавлен после того, как их клип Castle of Blood
# (1964) для видео Ghost Movies (50) оказался промо-нарезкой их сервиса —
# лого CULTPIX на весь экран, "31 Nights of Halloween on CULTPIX", "Join us
# at cultpix.com" и вшитые субтитры-CTA прямо в кадре. Это не трейлер фильма,
# а промо самого стримингового сервиса — не использовать независимо от того,
# насколько релевантным выглядит заголовок видео на YouTube.
YT_BLOCKED_UPLOADERS = r"(?i)(movie\s*clips|fandango|clipsandtrailers|kinocheck|rotten\s*tomatoes|cultpix)"

# 1 сентября 2026: жёстко заданный "chrome" (профиль по умолчанию) стал ловить
# "no such table: meta" — куки-БД профиля Default сбросилась (пустой файл,
# 4KB, ни одной таблицы). Реальные рабочие куки лежат в другом профиле
# Chrome — указываем его явно через "chrome:<profile>", а не голый "chrome".
# Если это снова сломается — сначала проверить `ls -la ~/Library/Application\
# Support/Google/Chrome/*/Cookies` и найти профиль с ненулевым размером файла.
YT_COOKIES_FROM_BROWSER = "chrome:Profile 2"
# Формат: берём максимум вплоть до 4K, если у трейлера он есть (avc1 —
# для совместимости с ffmpeg), с постепенным откатом вниз. У большинства
# трейлеров (особенно старых фильмов) 4K физически не существует — тогда
# откатываемся ниже вплоть до прежнего минимума ≥720p, без изменений.
YT_FORMAT = ("bestvideo[height>=720][height<=2160][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
             "bestvideo[height>=720][height<=2160]+bestaudio/best[height>=720]")


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
             "--quiet", "--no-warnings", "--cookies-from-browser", YT_COOKIES_FROM_BROWSER],
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


YT_PROBE_LIMIT = 6  # сколько кандидатов дёшево проверяем на разрешение перед скачиванием


def _probe_youtube_height(vid: str) -> int | None:
    """Дёшево (без скачивания) узнаёт высоту лучшего доступного видеопотока —
    для выбора кандидата с лучшим качеством перед реальной загрузкой."""
    try:
        r = subprocess.run(
            ["yt-dlp", "--simulate", "--print", "%(height)s",
             "-f", "bestvideo[height<=2160][vcodec^=avc1]/bestvideo[height<=2160]",
             f"https://www.youtube.com/watch?v={vid}",
             "--quiet", "--no-warnings", "--cookies-from-browser", YT_COOKIES_FROM_BROWSER],
            capture_output=True, text=True, timeout=20,
        )
        out = r.stdout.strip()
        return int(out) if out.isdigit() else None
    except Exception:
        return None


def _download_trailer_youtube(movie_title: str, year: int, output_path: str) -> str | None:
    """Скачивает трейлер с YouTube в HD (yt-dlp + bgutil PO-token + cookies-from-browser
    chrome). 21 августа 2026: анонимный режим без cookies начал систематически ловить
    403/"Requested format is not available" от YouTube (см. sports20_build.log) —
    вернули cookies-from-browser как обязательный слой поверх bgutil, что чинит
    загрузку до 1080p.

    Сначала дёшево (без скачивания) проверяет разрешение первых YT_PROBE_LIMIT
    кандидатов и качает лучший по качеству — а не первого попавшегося, который
    просто прошёл порог ≥720p. На практике у одного и того же трейлера почти
    всегда рядом лежит несколько загрузок разного качества (разные каналы), и
    без этой разведки пайплайн молча довольствовался первой 720p-загрузкой,
    даже когда парой кандидатов ниже лежал 1080p того же ролика."""
    candidates = _youtube_trailer_ids(movie_title, year)
    if not candidates:
        return None

    probed = []
    for vid in candidates[:YT_PROBE_LIMIT]:
        h = _probe_youtube_height(vid)
        if h:
            probed.append((h, vid))

    # Лучшие по разрешению — первыми; кандидаты без данных разведки (сеть
    # моргнула) или за пределами YT_PROBE_LIMIT — в хвост как запасной вариант.
    ordered = [vid for _, vid in sorted(probed, key=lambda x: -x[0])]
    ordered += [vid for vid in candidates if vid not in ordered]

    for vid in ordered:
        try:
            # Полный URL, а не голый id: id может начинаться с "-" (валидный
            # символ в YouTube video id), и такой позиционный аргумент
            # yt-dlp по ошибке парсит как флаг (напр. "no such option: -A").
            subprocess.run(
                ["yt-dlp", f"https://www.youtube.com/watch?v={vid}",
                 "--format", YT_FORMAT, "--merge-output-format", "mp4",
                 "--output", output_path, "--no-playlist", "--quiet", "--no-warnings",
                 "--cookies-from-browser", YT_COOKIES_FROM_BROWSER,
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
    """Каскад источников: YouTube HD (yt-dlp + bgutil + cookies-from-browser chrome) → IMDB как fallback."""
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
                    # original вместо w780 — под 4K-канвас (см. OUT_W/OUT_H)
                    img = _tmdb_get(f"https://image.tmdb.org/t/p/original{poster_file_path}")
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


def _tmdb_credits(title: str, year: int) -> dict | None:
    """Режиссёр и первый по билингу актёр из TMDB — для lower-third оверлея
    (см. анализ конкурентного видео: подписанные имена людей в кадре
    добавляют познавательности и держат внимание)."""
    found = _find_tmdb_movie(title, year)
    if not found:
        return None
    tmdb_id, media_type = found
    credits_type = "movie" if media_type == "movie" else "tv"
    try:
        data = _tmdb_get(
            f"https://api.themoviedb.org/3/{credits_type}/{tmdb_id}/credits",
            params={"api_key": TMDB_API_KEY},
        ).json()
    except Exception:
        return None

    director = None
    for c in data.get("crew", []):
        if c.get("job") == "Director":
            director = c.get("name")
            break
    cast = data.get("cast", [])
    actor = cast[0].get("name") if cast else None

    if not director and not actor:
        return None
    return {"director": director, "actor": actor}


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
            # original вместо w1280 — под 4K-канвас (см. OUT_W/OUT_H)
            img = _tmdb_get(f"https://image.tmdb.org/t/p/original{fp}")
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
    w, h = OUT_W, OUT_H
    x1, y1 = FRAME_INNER_X, FRAME_INNER_Y
    x2, y2 = x1 + FRAME_INNER_W, y1 + FRAME_INNER_H
    border, jitter, step = 56, 22, 12  # ×2 от 1080p-версии (28, 11, 6)

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
            f"[0:v]scale={OUT_W}:{OUT_H},format=yuv420p,setsar=1[bg];"
            f"[bg][1:v]overlay={FRAME_INNER_X}:{FRAME_INNER_Y}[v1];"
            f"[v1][2:v]overlay=0:0:format=auto[out]"
        ),
        "-map", "[out]",
        *_venc(),
        "-r", str(fps), "-an",
        output_path,
    ])
    return output_path


def build_still_clip_fullscreen(
    still_path: str,
    duration: float,
    output_path: str,
) -> str:
    """Альтернативный стиль стоп-кадра («fullscreen»): без рамки-поляроида и
    без размытого bg-видео — картинка занимает весь кадр 3840x2160 с плавным
    Ken Burns зумом. Направление (приближение/отдаление) чередуется
    детерминированно по хэшу имени файла, чтобы пересборка того же кадра
    давала то же движение. См. build_still_clip() — «классический» вариант
    с рамкой; выбор между ними на уровне всего видео см. _get_visual_style()."""
    fps = 30
    n_frames = max(1, round(duration * fps))
    zoom_in = (hash(os.path.basename(still_path)) % 2 == 0)
    zoom_end = 1.15
    step = (zoom_end - 1.0) / n_frames

    if zoom_in:
        zoom_expr = f"min(zoom+{step:.6f},{zoom_end})"
    else:
        zoom_expr = f"if(eq(on,0),{zoom_end},max(zoom-{step:.6f},1.0))"

    run_ffmpeg([
        "ffmpeg", "-y",
        "-loop", "1", "-i", still_path,
        "-vf", (
            f"scale={OUT_W * 2}:{OUT_H * 2}:force_original_aspect_ratio=increase,"
            f"crop={OUT_W * 2}:{OUT_H * 2},"
            f"zoompan=z='{zoom_expr}':d={n_frames}:s={OUT_W}x{OUT_H}:fps={fps}"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)',"
            "format=yuv420p,setsar=1"
        ),
        "-t", str(duration),
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
                "-vf", f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
                       f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,setsar=1",
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


def _build_intro_teaser(trailer_map: dict[int, str], work_dir: str) -> str | None:
    """Пара быстрых вспышек из трейлеров #2 и #1 (лучших мест в countdown) —
    в самое начало интро, до основного монтажа. Намёк «главное — впереди»
    (см. анализ видео конкурента: тизер топовых позиций в первые секунды
    держит зрителя до конца ролика). Если трейлеров #1/#2 нет — просто
    ничего не добавляем, интро остаётся как раньше."""
    top_numbers = [n for n in (2, 1) if trailer_map.get(n)]
    if not top_numbers:
        return None

    vf = (f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
          f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    clips = []
    for i, number in enumerate(top_numbers):
        trailer_path = trailer_map[number]
        try:
            duration = get_audio_duration(trailer_path)
        except Exception:
            continue
        safe_start = MONTAGE_SKIP_START
        safe_end   = duration - MONTAGE_SKIP_END
        if safe_end - safe_start < TEASER_CLIP_DUR:
            continue
        t = random.uniform(safe_start, safe_end - TEASER_CLIP_DUR)
        clip_out = os.path.join(work_dir, f"teaser_{i}.mp4")
        try:
            run_ffmpeg([
                "ffmpeg", "-y",
                "-ss", f"{t:.3f}", "-i", trailer_path,
                "-t", str(TEASER_CLIP_DUR),
                "-vf", vf,
                *_venc(), "-r", "30", "-an",
                clip_out,
            ])
            clips.append(clip_out)
        except subprocess.CalledProcessError:
            continue

    if not clips:
        return None

    teaser_path = os.path.join(work_dir, "intro_teaser.mp4")
    _concat_video_parts(clips, teaser_path)
    print(f"     ✓ Тизер #1/#2: {teaser_path}")
    return teaser_path


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
    font = _load_pil_font(88)  # ×2 от 1080p-версии (44) — под 4K-канвас
    stroke = 4
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
        gap = 24
        total_w = title_w + gap + star_r * 2 + gap + rating_w
    else:
        rating_text = None
        rb = None
        total_w = title_w

    pad = 36
    img_w = total_w + pad * 2
    img_h = title_h + pad * 2

    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Золотая плашка с закруглёнными углами
    draw.rounded_rectangle([0, 0, img_w - 1, img_h - 1], radius=28,
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
    font = _load_pil_font(200)  # ×2 от 1080p-версии (100)
    stroke = 6

    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox = dummy.textbbox((0, 0), text, font=font, stroke_width=stroke)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    medal_r = max(tw, th) // 2 + 48
    size = medal_r * 2 + 28

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx, cy = size // 2, size // 2

    # Тень медали
    draw.ellipse([cx - medal_r + 10, cy - medal_r + 10, cx + medal_r + 10, cy + medal_r + 10],
                 fill=(0, 0, 0, 80))
    # Внешнее кольцо (тёмное золото)
    draw.ellipse([cx - medal_r, cy - medal_r, cx + medal_r, cy + medal_r],
                 fill=(160, 120, 10, 255))
    # Внутренний круг (яркое золото)
    inner_r = medal_r - 16
    draw.ellipse([cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r],
                 fill=(212, 175, 55, 255))

    # Цифра по центру медали
    tx = cx - (bbox[0] + bbox[2]) // 2
    ty = cy - (bbox[1] + bbox[3]) // 2
    draw.text((tx + 4, ty + 6), text, font=font, fill=(0, 0, 0, 90))   # тень
    draw.text((tx, ty), text, font=font, fill=(255, 255, 255, 255),
              stroke_width=stroke, stroke_fill=(80, 40, 0, 255))

    img.save(output_path, "PNG")


def create_corner_bug_png(number: int, title: str, year: int, output_path: str):
    """Создаёт полупрозрачную плашку с названием фильма для постоянного оверлея в углу."""
    font = _load_pil_font(52)  # ×2 от 1080p-версии (26)
    text = f"#{number}  {title} ({year})"
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox = dummy.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pad_x, pad_y = 32, 20
    w = tw + pad_x * 2
    h = th + pad_y * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, w - 1, h - 1], fill=(0, 0, 0, 160))
    draw.text((pad_x - bbox[0], pad_y - bbox[1]), text, font=font, fill=(255, 255, 255, 230))
    img.save(output_path, "PNG")


def create_filmstrip_divider_png(length: int, output_path: str, strip_w: int = 64,
                                  horizontal: bool = False) -> str:
    """PNG-полоса в виде киноплёнки (тёмная лента + перфорация по центру) —
    разделитель между панелями split-screen кадра вместо простой линии.
    По умолчанию вертикальная полоса высотой `length`; при horizontal=True —
    горизонтальная полоса шириной `length` (для раскладок 3/4 панели)."""
    hole_w, hole_h = 26, 36
    gap = 100  # шаг между отверстиями перфорации
    img = Image.new("RGBA", (strip_w, length), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Тёмная лента плёнки
    draw.rectangle([0, 0, strip_w - 1, length - 1], fill=(12, 12, 12, 235))
    # Светлые кромки по краям ленты
    draw.rectangle([0, 0, 3, length - 1], fill=(225, 215, 195, 255))
    draw.rectangle([strip_w - 4, 0, strip_w - 1, length - 1], fill=(225, 215, 195, 255))
    # Перфорация — колонка отверстий по центру ленты
    hx = (strip_w - hole_w) // 2
    y = gap // 2
    while y < length:
        draw.rounded_rectangle([hx, y, hx + hole_w, y + hole_h], radius=6,
                                fill=(230, 224, 208, 255))
        y += gap
    if horizontal:
        img = img.rotate(90, expand=True)
    img.save(output_path, "PNG")
    return output_path


def create_clean_divider_png(length: int, output_path: str, thickness: int = 6,
                              horizontal: bool = False) -> str:
    """Тонкая аккуратная линия-разделитель между панелями split-screen —
    светлая линия с мягкой тенью, без декоративной перфорации."""
    pad = 24
    w = thickness + pad * 2
    shadow = Image.new("RGBA", (w, length), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rectangle([pad, 0, pad + thickness, length - 1], fill=(0, 0, 0, 170))
    shadow = shadow.filter(ImageFilter.GaussianBlur(9))
    line = Image.new("RGBA", (w, length), (0, 0, 0, 0))
    ld = ImageDraw.Draw(line)
    ld.rectangle([pad, 0, pad + thickness, length - 1], fill=(255, 255, 255, 255))
    img = Image.alpha_composite(shadow, line)
    if horizontal:
        img = img.rotate(90, expand=True)
    img.save(output_path, "PNG")
    return output_path


def create_lower_third_png(name: str, role: str, output_path: str):
    """Lower-third плашка с именем режиссёра/актёра — тёмная полупрозрачная
    панель с золотой полосой слева, в стиле остальных титров ролика."""
    font_name = _load_pil_font(60)  # ×2 от 1080p-версии (30)
    font_role = _load_pil_font(36)  # ×2 от 1080p-версии (18)

    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    nb = dummy.textbbox((0, 0), name, font=font_name)
    rb = dummy.textbbox((0, 0), role.upper(), font=font_role)
    name_h = nb[3] - nb[1]
    text_w = max(nb[2] - nb[0], rb[2] - rb[0])
    text_h = name_h + 8 + (rb[3] - rb[1])

    bar_w = 10
    pad_x, pad_y = 32, 20
    img_w = bar_w + pad_x * 2 + text_w
    img_h = pad_y * 2 + text_h

    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, img_w - 1, img_h - 1], fill=(0, 0, 0, 175))
    draw.rectangle([0, 0, bar_w - 1, img_h - 1], fill=(212, 175, 55, 255))
    draw.text((bar_w + pad_x - nb[0], pad_y - nb[1]), name, font=font_name,
              fill=(255, 255, 255, 255))
    draw.text((bar_w + pad_x - rb[0], pad_y + name_h + 8 - rb[1]), role.upper(),
              font=font_role, fill=(212, 175, 55, 255))
    img.save(output_path, "PNG")


def create_subscribe_png(output_path: str) -> str:
    """Создаёт PNG плашки SUBSCRIBE для оверлея на интро/аутро."""
    font_big   = _load_pil_font(64)  # ×2 от 1080p-версии (32)
    font_small = _load_pil_font(30)  # ×2 от 1080p-версии (15)
    stroke = 4
    main_text = "SUBSCRIBE"
    sub_text  = "& turn on notifications"

    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    mb = dummy.textbbox((0, 0), main_text, font=font_big,   stroke_width=stroke)
    sb = dummy.textbbox((0, 0), sub_text,  font=font_small)

    content_w = max(mb[2] - mb[0], sb[2] - sb[0])
    content_h = (mb[3] - mb[1]) + 16 + (sb[3] - sb[1])
    pad_x, pad_y = 40, 24

    img_w = content_w + pad_x * 2
    img_h = content_h + pad_y * 2

    img  = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Тень
    draw.rounded_rectangle([8, 8, img_w + 6, img_h + 6], radius=24, fill=(0, 0, 0, 110))
    # Красный фон
    draw.rounded_rectangle([0, 0, img_w - 1, img_h - 1], radius=24, fill=(220, 20, 20, 240))
    # Белая рамка
    draw.rounded_rectangle([4, 4, img_w - 5, img_h - 5], radius=20,
                            outline=(255, 255, 255, 160), width=4)

    cx = img_w // 2

    # Главный текст
    mw = mb[2] - mb[0]
    draw.text((cx - mw // 2 - mb[0], pad_y - mb[1]), main_text, font=font_big,
              fill=(255, 255, 255, 255), stroke_width=stroke, stroke_fill=(140, 0, 0, 255))

    # Подпись
    sw = sb[2] - sb[0]
    sub_y = pad_y + (mb[3] - mb[1]) + 20
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
        f"[0:v][sub]overlay=x=W-overlay_w-60:y=H-overlay_h-60:format=auto:eof_action=pass[out]"
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
# 4.5 ИНТЕРАКТИВНЫЙ ПРИЗЫВ К ПОДПИСКЕ (конец интро)
# ─────────────────────────────────────────────

CTA_GREENSCREEN_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "114691-701830292.mp4")
# Область с контентом (лайк / SUBSCRIBE / колокольчик) внутри кадра
# исходника 1920x1080 — сам ролик почти весь пустой/зелёный, кроме нижней
# трети; найдено сканированием кадров на не-зелёные пиксели.
CTA_CROP = (1000, 390, 460, 690)   # w, h, x, y
CTA_GREEN_COLOR = "0x72F312"


def add_subscribe_cta(video_path: str, output_path: str, work_dir: str) -> str:
    """Накладывает готовую анимацию «лайк → SUBSCRIBE → колокольчик»
    (хромакей поверх бесплатного стокового ролика без вотермарки, со своим
    звуком клика) в последние секунды видео, по центру экрана."""
    if not os.path.exists(CTA_GREENSCREEN_PATH):
        print(f"     ⚠  CTA-ролик не найден ({CTA_GREENSCREEN_PATH}), пропускаем")
        import shutil
        shutil.copy(video_path, output_path)
        return output_path

    cta_dur  = get_audio_duration(CTA_GREENSCREEN_PATH)
    duration = get_audio_duration(video_path)
    cta_start = max(0.0, duration - cta_dur)

    crop_w, crop_h, crop_x, crop_y = CTA_CROP
    scale = 2.5  # было 1.25 под 1080p — ×2 под 4K (CTA_CROP сам не меняется,
                 # это координаты внутри исходного greenscreen-ролика)
    disp_w, disp_h = int(crop_w * scale), int(crop_h * scale)
    pos_x, pos_y = (OUT_W - disp_w) // 2, (OUT_H - disp_h) // 2

    delay_ms = int(round(cta_start * 1000))

    # Явный сдвиг ВНУТРИ filter_complex — по отдельности для видео (setpts)
    # и аудио (adelay) — вместо -itsoffset на входе. -itsoffset сдвигает PTS
    # всего потока разом, и overlay для видео это честно уважает (проверено
    # на кадрах), но amix для аудио, похоже, НЕ ждёт сдвинутый PTS и подмешивает
    # звук CTA-ролика (колокольчик/клик) прямо с t=0 вместо cta_start — отсюда
    # посторонние звуки и просадка громкости в начале интро. setpts+adelay
    # каждый по отдельности уже проверялись в этой сессии и точно работают.
    filter_complex = (
        f"[1:v]crop={crop_w}:{crop_h}:{crop_x}:{crop_y},scale={disp_w}:{disp_h},"
        f"chromakey={CTA_GREEN_COLOR}:0.12:0.08,despill=type=green,"
        f"setpts=PTS+{cta_start:.3f}/TB[cta];"
        f"[0:v][cta]overlay=x={pos_x}:y={pos_y}:eof_action=pass[vout];"
        f"[1:a]adelay={delay_ms}|{delay_ms}[cta_a];"
        # normalize=0 — иначе amix по умолчанию занижает громкость ВСЕГО
        # микса (не только момента наложения) при объединении двух дорожек,
        # даже когда вторая (CTA) почти всё время молчит — из-за этого
        # тише становится весь войсовер интро, а не только звук клика.
        f"[0:a][cta_a]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
    )

    run_ffmpeg([
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", CTA_GREENSCREEN_PATH,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        *_venc(), "-r", "30",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-shortest", output_path,
    ])
    return output_path


# ─────────────────────────────────────────────
# 5. СБОРКА СЕГМЕНТА (один фильм)
# ─────────────────────────────────────────────

def _render_poster_clip(poster_path: str | None, duration: float, output_path: str):
    if poster_path and os.path.exists(poster_path):
        fc = (
            f"[0]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
            f"crop={OUT_W}:{OUT_H}:(iw-ow)/2:(ih-oh)/2,"
            "boxblur=40:4,eq=brightness=-0.3[bg];"
            f"[0]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
            f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,setsar=1[fg];"
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
            "-f", "lavfi", "-i", f"color=c=black:s={OUT_W}x{OUT_H}:d={duration}",
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
    still_style: str = "framed",
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
            still_style,
        )
    finally:
        _shutil.rmtree(tmp_dir, ignore_errors=True)


def _build_rapid_trailer_cuts(
    trimmed_trailer: str,
    needed: float,
    trailer_avail: float,
    tmp_dir: str,
    number: int,
) -> list[str]:
    """ЭКСПЕРИМЕНТ: режет обрезанный трейлер на клипы по RAPID_CUT_DURATION
    секунд. Если трейлера хватает на needed — берёт точки РАВНОМЕРНО по
    всей его длине (начало/середина/конец, а не только вступление, чтобы
    захватывать и экшн-сцены ближе к концу трейлера). Если трейлер короче
    needed — идёт последовательно от начала с зацикливанием заново, там
    равномерная выборка не имеет смысла (сырья физически не хватает)."""
    vf = (f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
          f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,setsar=1"
          + (f",{TRAILER_FILM_GRAIN}" if TRAILER_FILM_GRAIN else ""))
    n_chunks = max(1, math.ceil(needed / RAPID_CUT_DURATION))
    parts = []

    enough = trailer_avail >= needed
    span = max(trailer_avail - RAPID_CUT_DURATION, 0.0)
    step = span / (n_chunks - 1) if n_chunks > 1 else 0.0
    pos = 0.0

    left = needed
    for i in range(n_chunks):
        dur = min(RAPID_CUT_DURATION, left)
        if dur <= 0:
            break
        if enough:
            start = i * step  # равномерно по всей длине трейлера
        else:
            if pos + dur > trailer_avail:
                pos = 0.0  # трейлер короче нужного — начинаем заново с начала
            start = pos
        chunk_path = os.path.join(tmp_dir, f"seg{number}_rcut_{i}.mp4")
        run_ffmpeg([
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}", "-i", trimmed_trailer,
            "-t", f"{dur:.3f}",
            "-vf", vf,
            *_venc(),
            "-r", "30", "-an", chunk_path,
        ])
        parts.append(chunk_path)
        left -= dur
        pos += dur
    return parts


MULTIPANEL_CYCLE = [2, 3, 4]  # количество панелей чередуется от фильма к фильму


def _frame_is_title_card(video_path: str, t: float) -> bool:
    """Грубая эвристика: кадр похож на титульную карточку/заставку (текст
    на почти чёрном фоне — имя актёра, название студии/фильма), а не на
    сцену из фильма — доля почти чёрных пикселей аномально высока."""
    tmp = video_path + f"._probe_{int(t * 1000)}.png"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", video_path,
             "-frames:v", "1", "-vf", "scale=160:-1", tmp],
            check=True, capture_output=True,
        )
        img = Image.open(tmp).convert("L")
        pixels = list(img.getdata())
        dark = sum(1 for px in pixels if px < 24) / len(pixels)
        return dark > 0.6
    except Exception:
        return False
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _pick_multipanel_timestamps(trailer_path: str, trailer_avail: float, dur: float,
                                 n: int) -> list[float]:
    """N разных моментов трейлера, разнесённых по его "основной" части, для
    одновременного показа в N панелях. Начало и конец трейлера отрезаются с
    запасом (там чаще всего логотипы студий/название фильма), а для каждой
    точки дополнительно отбраковываются кадры, похожие на титульные карточки
    (см. _frame_is_title_card)."""
    margin = trailer_avail * 0.12
    lo = margin
    hi = max(trailer_avail - dur - margin, lo)
    span = hi - lo
    if span <= 0:
        return [min(lo, max(trailer_avail - dur, 0.0))] * n

    def _sample(base: float) -> float:
        t = base
        for attempt in range(5):
            spread = span * 0.05 * (attempt + 1)
            t = min(max(base + random.uniform(-spread, spread), lo), lo + span)
            # Проверяем не только стартовый кадр, но и середину/конец окна
            # показа — сцена может уйти в затемнение уже после старта.
            check_points = [t,
                             min(t + dur * 0.5, trailer_avail - 0.1),
                             min(t + dur * 0.85, trailer_avail - 0.1)]
            if not any(_frame_is_title_card(trailer_path, cp) for cp in check_points):
                return t
        return t  # сдаёмся после 5 попыток — берём что есть

    if n == 1:
        return [_sample(lo + span / 2)]
    step = span / (n - 1)
    return [_sample(lo + i * step) for i in range(n)]


def _panel_vf(w: int, h: int, max_stretch: float = 1.5, source_aspect: float = 16 / 9) -> str:
    """Заполняет панель без обрезки сильнее max_stretch относительно
    типового соотношения сторон трейлера (16:9) — на сильно вытянутых
    панелях (широкая верхняя полоса 3-раскладки, узкие половины
    2-раскладки) обычный "cover" обрезает слишком много по краям кадра
    (обрезанные лица/объекты). Вместо этого ограничиваем степень cover и
    дополняем остаток панели чёрными полями."""
    panel_aspect = w / h
    ratio = panel_aspect / source_aspect
    if 1 / max_stretch <= ratio <= max_stretch:
        return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
    if ratio > max_stretch:
        # панель существенно шире источника — не растягиваем cover по
        # ширине сильнее допустимого, дополняем чёрными полями по бокам
        fill_w = round(h * source_aspect * max_stretch)
        return (f"scale={fill_w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={fill_w}:{h},"
                f"pad={w}:{h}:(ow-iw)/2:0:color=black,setsar=1")
    # панель существенно выше источника — не растягиваем cover по высоте
    # сильнее допустимого, дополняем чёрными полями сверху/снизу
    fill_h = round(w / (source_aspect / max_stretch))
    return (f"scale={w}:{fill_h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{fill_h},"
            f"pad={w}:{h}:0:(oh-ih)/2:color=black,setsar=1")


def _build_multipanel_chunk(trimmed_trailer: str, timestamps: list[float], dur: float,
                            panel_count: int, output_path: str) -> str:
    """Один кусок трейлера, показанный сразу в 2/3/4 панелях (разные моменты
    трейлера одновременно) — раз в фильм, с чередованием количества панелей
    2→3→4→2→3→4... по ходу ролика (см. анализ видео конкурента). Панели
    разделены тонкой аккуратной линией."""
    half_w, half_h = OUT_W // 2, OUT_H // 2
    grain = f",{TRAILER_FILM_GRAIN}" if TRAILER_FILM_GRAIN else ""
    tmp_dir = os.path.dirname(output_path)
    _vf = _panel_vf

    cmd = ["ffmpeg", "-y"]
    for t in timestamps:
        cmd += ["-ss", f"{t:.3f}", "-t", f"{dur:.3f}", "-i", trimmed_trailer]

    if panel_count == 2:
        v_div = os.path.join(tmp_dir, "_div_v_full.png")
        create_clean_divider_png(OUT_H, v_div)
        cmd += ["-t", f"{dur:.3f}", "-loop", "1", "-i", v_div]
        fc = (
            f"[0:v]{_vf(half_w, OUT_H)}[a];"
            f"[1:v]{_vf(half_w, OUT_H)}[b];"
            f"[a][b]hstack=inputs=2[stacked];"
            f"[stacked][2:v]overlay=(W-w)/2:0{grain}[v]"
        )
    elif panel_count == 3:
        # Один большой кадр сверху на всю ширину + два поменьше снизу
        h_div = os.path.join(tmp_dir, "_div_h_full.png")
        v_div = os.path.join(tmp_dir, "_div_v_half.png")
        create_clean_divider_png(OUT_W, h_div, horizontal=True)
        create_clean_divider_png(half_h, v_div)
        cmd += ["-t", f"{dur:.3f}", "-loop", "1", "-i", h_div,
                "-t", f"{dur:.3f}", "-loop", "1", "-i", v_div]
        fc = (
            f"[0:v]{_vf(OUT_W, half_h)}[top];"
            f"[1:v]{_vf(half_w, half_h)}[bl];"
            f"[2:v]{_vf(half_w, half_h)}[br];"
            f"[bl][br]hstack=inputs=2[bottom];"
            f"[top][bottom]vstack=inputs=2[stacked];"
            f"[stacked][3:v]overlay=0:(H-h)/2[s2];"
            f"[s2][4:v]overlay=(W-w)/2:H/2{grain}[v]"
        )
    elif panel_count == 4:
        # Сетка 2×2
        v_div = os.path.join(tmp_dir, "_div_v_full.png")
        h_div = os.path.join(tmp_dir, "_div_h_full.png")
        create_clean_divider_png(OUT_H, v_div)
        create_clean_divider_png(OUT_W, h_div, horizontal=True)
        cmd += ["-t", f"{dur:.3f}", "-loop", "1", "-i", v_div,
                "-t", f"{dur:.3f}", "-loop", "1", "-i", h_div]
        fc = (
            f"[0:v]{_vf(half_w, half_h)}[tl];"
            f"[1:v]{_vf(half_w, half_h)}[tr];"
            f"[2:v]{_vf(half_w, half_h)}[bl];"
            f"[3:v]{_vf(half_w, half_h)}[br];"
            f"[tl][tr]hstack=inputs=2[top];"
            f"[bl][br]hstack=inputs=2[bottom];"
            f"[top][bottom]vstack=inputs=2[stacked];"
            f"[stacked][4:v]overlay=(W-w)/2:0[s2];"
            f"[s2][5:v]overlay=0:(H-h)/2{grain}[v]"
        )
    else:
        raise ValueError(f"unsupported panel_count: {panel_count}")

    cmd += [
        "-filter_complex", fc,
        "-map", "[v]",
        *_venc(), "-r", "30", "-an",
        output_path,
    ]
    run_ffmpeg(cmd)
    return output_path


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
    still_style: str = "framed",
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
    # "fullbleed" не требует bg_video/frame_png (нет рамки/размытого фона),
    # "framed" (по умолчанию) требует их как раньше.
    still_clips = []
    use_stills = stills and not RAPID_TRAILER_CUTS and (
        still_style == "fullbleed"
        or (frame_png and bg_video and os.path.exists(bg_video or ""))
    )
    if use_stills:
        sc_dir = os.path.join(work_dir, "still_clips")
        os.makedirs(sc_dir, exist_ok=True)
        for i, sp in enumerate(stills):
            sc_path = os.path.join(sc_dir, f"seg{number}_still_{i+1}.mp4")
            if not os.path.exists(sc_path):
                print(f"     → Стоп-кадр {i+1}/{len(stills)}...")
                if still_style == "fullbleed":
                    build_still_clip_fullscreen(sp, STILL_DURATION, sc_path)
                else:
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

        if RAPID_TRAILER_CUTS:
            # --- ЭКСПЕРИМЕНТ: только трейлер, нарезанный на клипы ≤4с ---
            need = trailer_needed if trailer_needed > 0 else trailer_avail
            parts.extend(_build_rapid_trailer_cuts(trimmed, need, trailer_avail, tmp_dir, number))
        elif n_sc > 0:
            # --- Старая схема: длинные куски трейлера + стоп-кадры ---
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

            vf = (f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
                  f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,setsar=1"
                  + (f",{TRAILER_FILM_GRAIN}" if TRAILER_FILM_GRAIN else ""))
            # Один многопанельный момент на сегмент (не первый и не последний
            # кусок, чтобы не портить открывающий/закрывающий кадр сегмента) —
            # количество панелей чередуется 2→3→4→2→3→4... по номеру фильма.
            split_pool = list(range(1, max(n_chunks - 1, 1)))
            split_index = random.choice(split_pool) if split_pool else None
            panel_count = MULTIPANEL_CYCLE[number % 3]
            for i in range(n_chunks):
                chunk_path = os.path.join(tmp_dir, f"seg{number}_tchunk_{i}.mp4")
                if i == split_index:
                    timestamps = _pick_multipanel_timestamps(trimmed, trailer_avail, chunk_dur, panel_count)
                    _build_multipanel_chunk(trimmed, timestamps, chunk_dur, panel_count, chunk_path)
                else:
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
            # --- Старая схема: нет стоп-кадров — весь трейлер одним куском.
            # Если трейлер короче нужного — зацикливаем его (доп. отрывки),
            # чтобы не морозить последний кадр. ---
            need = trailer_needed if trailer_needed > 0 else trailer_avail
            loop_args = ["-stream_loop", "-1"] if trailer_avail < need else []
            trailer_cut = os.path.join(tmp_dir, f"seg{number}_trailer_cut.mp4")
            run_ffmpeg([
                "ffmpeg", "-y",
                *loop_args,
                "-i", trimmed,
                "-t", str(need),
                "-vf", (f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease,"
                        f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,setsar=1"
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

    # --- Lower-third: режиссёр/актёр из TMDB (см. анализ конкурента) ---
    credits = _tmdb_credits(title, year)
    lower_third_png = None
    if credits:
        lt_name = credits.get("director") or credits.get("actor")
        lt_role = "Director" if credits.get("director") else "Starring"
        if lt_name:
            lower_third_png = os.path.join(tmp_dir, f"seg{number}_lower3rd.png")
            create_lower_third_png(lt_name, lt_role, lower_third_png)

    overlay_inputs = [
        "-i", concat_video,
        "-loop", "1", "-t", "5", "-i", title_png,
        "-loop", "1", "-t", "10", "-i", number_png,
    ]
    fc = (
        "[1]format=rgba,fade=t=out:st=4.5:d=0.5:alpha=1[tf];"
        "[2]format=rgba,fade=t=out:st=9.5:d=0.5:alpha=1[nf];"
        "[0][tf]overlay="
        "x='if(lt(t,0.5),-overlay_w+(overlay_w+120)*2*t,120)':y=H-240:format=auto:eof_action=pass[v1];"
        "[v1][nf]overlay=x=W-overlay_w-80:y=50:format=auto:eof_action=pass"
    )
    if lower_third_png:
        # Появляется у того же угла (низ-лево), что и заголовок, но уже
        # после того, как заголовок исчез (5.0с) — конфликтов по месту нет.
        overlay_inputs += ["-loop", "1", "-t", "9", "-i", lower_third_png]
        fc = (
            "[1]format=rgba,fade=t=out:st=4.5:d=0.5:alpha=1[tf];"
            "[2]format=rgba,fade=t=out:st=9.5:d=0.5:alpha=1[nf];"
            "[3]format=rgba,fade=t=in:st=5.0:d=0.4:alpha=1,fade=t=out:st=8.6:d=0.4:alpha=1[lt3];"
            "[0][tf]overlay="
            "x='if(lt(t,0.5),-overlay_w+(overlay_w+120)*2*t,120)':y=H-240:format=auto:eof_action=pass[v1];"
            "[v1][nf]overlay=x=W-overlay_w-80:y=50:format=auto:eof_action=pass[v2];"
            "[v2][lt3]overlay=x=120:y=H-240:format=auto:eof_action=pass"
        )
    overlay_video = os.path.join(tmp_dir, f"seg{number}_overlay.mp4")
    run_ffmpeg([
        "ffmpeg", "-y",
        *overlay_inputs,
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
        "[vpad][cbug]overlay=x=W-overlay_w-40:y=H-overlay_h-40:eof_action=repeat[v]",
        "-map", "[v]", "-map", "1:a",
        # Громкость уже нормализована один раз на весь ролик целиком
        # в _generate_batched_voiceovers (см. _loudnorm_whole_file) —
        # повторная нормализация по кускам здесь не нужна и вредна
        # (создавала скачок на каждой склейке).
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

def _build_vhs_glitch_transition(output_path: str, work_dir: str) -> str:
    """Создаёт короткий переход «VHS-глитч»: резкий скачок в цветные
    телепомехи с цветными полосами рассинхрона и хроматическим сдвигом,
    лёгкое вертикальное дрожание (сбой трекинга) → короткий шумный хвост →
    чёрное. Альтернатива _build_burn_transition."""
    import shutil
    tmp_dir = os.path.join(work_dir, "vhs_tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    glitch_h = OUT_H + 160  # запас под вертикальное дрожание (было 1160 = 1080+80)
    glitch = os.path.join(tmp_dir, "glitch.mp4")
    run_ffmpeg([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=gray:s={OUT_W}x{glitch_h}:r=30:d=0.45",
        "-vf", (
            "noise=alls=60:allf=t+u,"
            f"drawbox=x=0:y=360:w={OUT_W}:h=100:color=cyan@0.8:t=fill:"
            "enable='lt(mod(t*7\\,1),0.3)',"
            f"drawbox=x=0:y=1120:w={OUT_W}:h=70:color=magenta@0.8:t=fill:"
            "enable='lt(mod(t*5+0.4\\,1),0.25)',"
            f"drawbox=x=0:y=1740:w={OUT_W}:h=120:color=0x33FF66@0.7:t=fill:"
            "enable='lt(mod(t*9+0.15\\,1),0.2)',"
            "geq=lum='lum(X,Y)*(0.55+0.45*mod(Y\\,4)/3)':cb='cb(X,Y)':cr='cr(X,Y)',"
            "rgbashift=rh=-36:bh=36,"
            f"crop={OUT_W}:{OUT_H}:0:'80+60*sin(2*PI*t*11)'"
        ),
        *_venc(), "-an", glitch,
    ])

    tail = os.path.join(tmp_dir, "tail.mp4")
    run_ffmpeg([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=black:s={OUT_W}x{OUT_H}:r=30:d=0.1",
        "-vf", "noise=alls=15:allf=t+u",
        *_venc(), "-an", tail,
    ])

    black = os.path.join(tmp_dir, "black.mp4")
    run_ffmpeg([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=black:s={OUT_W}x{OUT_H}:r=30:d=0.1",
        *_venc(), "-an", black,
    ])

    video_only = os.path.join(tmp_dir, "vhs_video.mp4")
    _concat_video_parts([glitch, tail, black], video_only)

    duration = get_audio_duration(video_only)
    run_ffmpeg([
        "ffmpeg", "-y",
        "-i", video_only,
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", f"{duration:.3f}",
        *_venc(),
        "-c:a", "aac", "-b:a", "192k",
        output_path,
    ])
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return output_path


def _build_burn_transition(output_path: str, work_dir: str) -> str:
    """Создаёт короткий переход «прогорание плёнки»: искра в углу кадра →
    раскалённое бело-оранжевое пятно расползается, тая в тёмно-красный по
    краю → вспышка почти в белое → чёрное. Используется вместо простой
    чёрной паузы после интро, между фильмами и перед аутро."""
    import shutil
    tmp_dir = os.path.join(work_dir, "burn_tmp")
    os.makedirs(tmp_dir, exist_ok=True)

    cx, cy = 2764, 604  # точка возгорания — верхний правый квадрант кадра (×2 от 1080p)
    radial = f"1-hypot(X-{cx},Y-{cy})/max((4800*pow(clip((T-0.12)/0.5,0,1),0.6)),1)"

    bloom = os.path.join(tmp_dir, "bloom.mp4")
    run_ffmpeg([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=black:s={OUT_W}x{OUT_H}:r=30:d=0.5",
        "-vf", (
            f"geq=r='255*clip(({radial})*1.4,0,1)':"
            f"g='255*clip(({radial}-0.12)*1.25,0,1)':"
            f"b='255*clip(({radial}-0.55)*2.2,0,1)',"
            "noise=alls=14:allf=t+u,eq=brightness=0.02*sin(2*PI*t*23)"
        ),
        *_venc(), "-an", bloom,
    ])

    flash = os.path.join(tmp_dir, "flash.mp4")
    run_ffmpeg([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=0xFFF3C4:s={OUT_W}x{OUT_H}:r=30:d=0.1",
        "-vf", "noise=alls=20:allf=t+u,eq=brightness=0.05*sin(2*PI*t*40)",
        *_venc(), "-an", flash,
    ])

    black = os.path.join(tmp_dir, "black.mp4")
    run_ffmpeg([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=black:s={OUT_W}x{OUT_H}:r=30:d=0.15",
        *_venc(), "-an", black,
    ])

    video_only = os.path.join(tmp_dir, "burn_video.mp4")
    _concat_video_parts([bloom, flash, black], video_only)

    duration = get_audio_duration(video_only)
    run_ffmpeg([
        "ffmpeg", "-y",
        "-i", video_only,
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", f"{duration:.3f}",
        *_venc(),
        "-c:a", "aac", "-b:a", "192k",
        output_path,
    ])
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return output_path


def assemble_final(segments: list[str], output_path: str, work_dir: str,
                    transition_style: str = "burn") -> str:
    """Склеивает все сегменты в финальное видео с переходом (по умолчанию
    «прогорание плёнки», альтернатива — VHS-глитч, см. VISUAL_STYLES) после
    интро, между фильмами и перед аутро."""
    print(f"\n🎞  Финальная сборка ({len(segments)} сегментов)...")

    # Генерируем переход один раз, переиспользуем во всех точках склейки
    if transition_style == "vhs_glitch":
        burn_path = os.path.join(work_dir, "vhs_transition.mp4")
        if not os.path.exists(burn_path):
            print("     → Генерация перехода (VHS-глитч)...")
            _build_vhs_glitch_transition(burn_path, work_dir)
    else:
        burn_path = os.path.join(work_dir, "burn_transition.mp4")
        if not os.path.exists(burn_path):
            print("     → Генерация перехода (прогорание плёнки)...")
            _build_burn_transition(burn_path, work_dir)

    # Интро → переход → фильм1 → переход → фильм2 → … → фильмN → переход → аутро
    intro, *movies, outro = segments
    list_file = os.path.join(work_dir, "final_list.txt")
    with open(list_file, "w") as f:
        f.write(f"file '{os.path.abspath(intro)}'\n")
        f.write(f"file '{os.path.abspath(burn_path)}'\n")
        for i, s in enumerate(movies):
            if i > 0:
                f.write(f"file '{os.path.abspath(burn_path)}'\n")
            f.write(f"file '{os.path.abspath(s)}'\n")
        f.write(f"file '{os.path.abspath(burn_path)}'\n")
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
    parser.add_argument("--music-mood", choices=list(MUSIC_MOODS.keys()), default=None,
                        help="Жанровый muud-трек фоном на весь ролик (assets/music/mood_<mood>.mp3), опционально")
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

    # Визуальный стиль этого видео (чередуется по кругу между видео,
    # см. VISUAL_STYLES) — влияет на оформление стоп-кадров и переход.
    visual_style = _get_visual_style(work_dir)

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
        trailer_out = os.path.join(work_dir, "trailers")
        safe_check = "".join(c for c in movie["title"] if c.isalnum() or c in " _-").strip().replace(" ", "_")
        already_cached = os.path.exists(os.path.join(trailer_out, f"{safe_check}_{movie['year']}_trailer.mp4"))
        trailer = download_trailer(movie["title"], movie["year"], trailer_out)
        if trailer:
            trailer_map[movie["number"]] = trailer
        else:
            print(f"     ⚠  Трейлер не найден для: {movie['title']} — фильм будет пропущен")
        # Пауза между реальными (не кэшированными) запросами — большие пакеты
        # (50-100 фильмов подряд без пауз) провоцируют временный бан-блок
        # YouTube ("Sign in to confirm you're not a bot"), даже с валидными
        # cookies. Зафиксировано 3 сентября 2026 на сборке Top 100 Movies —
        # 99 из 100 трейлеров упали с бан-блоком подряд, а те же ссылки
        # скачивались вручную мгновенно уже через несколько минут после.
        if not already_cached:
            time.sleep(4)

    # ── Шаг 1.5: озвучка (интро + все фильмы + аутро) по отдельности ─────
    # Раньше генерировали одним батч-заказом на весь ролик (см.
    # _generate_batched_voiceovers) — это убрало скачки громкости на
    # стыках, но не до конца убрало дрейф тона и добавило новый артефакт:
    # нарезка по alignment-таймингам иногда попадала в хвост согласного
    # звука соседнего блока ("мм"/"нн" на стыках фильмов). Вернулись к
    # отдельному TTS-вызову на каждый блок; громкость по-прежнему
    # выравниваем — но двухпроходным loudnorm на каждый файл сразу после
    # генерации (не по кускам после общей нарезки).
    intro_vo_text = data.get("intro_voiceover")
    outro_vo_text = data.get("outro_voiceover")
    intro_vo_path = os.path.join(work_dir, "intro_vo.mp3")
    outro_vo_path = os.path.join(work_dir, "outro_vo.mp3")
    vo_paths: dict[int, str] = {}
    for movie in movies:
        safe_t = "".join(c for c in movie["title"] if c.isalnum() or c in " _-").strip().replace(" ", "_")
        vo_paths[movie["number"]] = os.path.join(work_dir, "voiceovers", f"{movie['number']:02d}_{safe_t}.mp3")

    if not args.skip_voiceover:
        blocks: list[tuple[str, str]] = []
        if intro_vo_text:
            blocks.append((intro_vo_path, intro_vo_text))
        for movie in movies:
            blocks.append((vo_paths[movie["number"]], movie["voiceover_text"]))
        if outro_vo_text:
            blocks.append((outro_vo_path, outro_vo_text))
        for out_path, text in blocks:
            if os.path.exists(out_path):
                print(f"  ↩  Озвучка уже есть: {out_path}")
                continue
            raw_path = out_path + ".raw.mp3"
            try:
                generate_voiceover(text, raw_path, voice_id)
                _loudnorm_whole_file(raw_path, out_path)
                os.remove(raw_path)
            except Exception as e:
                # Не валим весь прогон из-за одного повреждённого пути на
                # диске (напр. битая запись в таблице NTFS) — если для этого
                # блока уже есть готовый сегмент дальше по пайплайну, эта
                # войсовер-регенерация всё равно не нужна; иначе сегмент
                # просто не соберётся и будет пропущен на Шаге 3.
                print(f"  ⚠  Не удалось сгенерировать/записать войсовер {out_path}: {e} — пропускаю")

    # ── Шаг 2: интро ─────────────────────────────────────────────────────
    intro_path = os.path.join(work_dir, "intro.mp4")
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
            intro_vo = intro_vo_path
            intro_duration = get_audio_duration(intro_vo)

        intro_silent = os.path.join(work_dir, "intro_silent.mp4")
        montage_dir  = os.path.join(work_dir, "montage")
        teaser = _build_intro_teaser(trailer_map, montage_dir)
        if teaser:
            teaser_dur   = get_audio_duration(teaser)
            montage_body = os.path.join(work_dir, "intro_montage_body.mp4")
            build_montage(list(trailer_map.values()), max(intro_duration - teaser_dur, 1.0),
                          montage_body, montage_dir)
            _concat_video_parts([teaser, montage_body], intro_silent)
        else:
            build_montage(list(trailer_map.values()), intro_duration, intro_silent, montage_dir)
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
        print(f"     → Призыв к подписке (интерактивный, конец интро)...")
        cta_tmp = intro_path + ".cta.tmp.mp4"
        add_subscribe_cta(intro_path, cta_tmp, work_dir)
        os.replace(cta_tmp, intro_path)
        open(intro_vid_hash_file, "w").write(intro_vid_hash)

    # ── Шаг 3: сегменты фильмов ───────────────────────────────────────────
    segments = []
    for movie in movies:
        number      = movie["number"]
        title       = movie["title"]
        year        = movie["year"]
        imdb_rating = movie.get("imdb_rating")

        print(f"\n[{number}] {title} ({year})")

        safe_t   = "".join(c for c in title if c.isalnum() or c in " _-").strip().replace(" ", "_")
        vo_path  = vo_paths[number]
        seg_path = os.path.join(work_dir, "segments", f"{number:02d}_{safe_t}.mp4")

        if os.path.exists(seg_path):
            print(f"     ↩  Сегмент уже есть, пропускаем")
            segments.append(seg_path)
            continue

        if not os.path.exists(vo_path):
            print(f"     ⚠  Войсовер не найден, пропускаем {title}")
            continue

        print(f"  🖼  Скачиваем постер{'' if RAPID_TRAILER_CUTS else ' и стоп-кадры'}...")
        poster  = download_poster(title, year, os.path.join(work_dir, "posters"))
        stills  = []
        if not RAPID_TRAILER_CUTS and (
            visual_style["still_style"] == "fullbleed" or (frame_png and bg_video)
        ):
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
            still_style=visual_style["still_style"],
        )

        segments.append(seg_path)

    if not segments:
        print("❌  Нет готовых сегментов для сборки")
        sys.exit(1)

    # ── Шаг 4: аутро ─────────────────────────────────────────────────────
    outro_path = os.path.join(work_dir, "outro.mp4")
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
            outro_vo = outro_vo_path
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
    assemble_final([intro_path] + segments + [outro_path], args.output, work_dir,
                    transition_style=visual_style["transition"])

    # ── Шаг 5.5: фоновая музыка (один muud-трек на весь ролик) ───────────
    if args.music_mood:
        print(f"\n🎵  Подмешиваем фоновую музыку ({args.music_mood})...")
        with_music = args.output + ".music.tmp.mp4"
        add_background_music(args.output, args.music_mood, with_music)
        os.replace(with_music, args.output)

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
