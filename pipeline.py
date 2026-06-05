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
import random
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


# ─────────────────────────────────────────────
# КОНФИГ — ключи читаются из .env
# ─────────────────────────────────────────────
VOICER_API_KEY = os.getenv("VOICER_API_KEY")
TMDB_API_KEY   = os.getenv("TMDB_API_KEY")
OMDB_API_KEY   = os.getenv("OMDB_API_KEY")
VOICER_BASE    = "https://voiceapiru.csv666.ru"

# Голос по умолчанию (ElevenLabs voice_id)
DEFAULT_VOICE_ID = "iP95p4xoKVk53GoZ742B"

# Сколько секунд пропускать в начале трейлера (MPAA + студия)
TRAILER_SKIP_SECONDS = 8

# Сколько секунд пропускать в конце трейлера (финальные титры)
TRAILER_END_SKIP = 20

# Настройки интро/аутро монтажа
INTRO_DURATION    = 15    # секунд
OUTRO_DURATION    = 15    # секунд
MONTAGE_CLIP_DUR  = 2.5   # длина одного клипа в монтаже

# Сколько секунд показывать постер перед трейлером (с Ken Burns)
POSTER_DURATION = 4

# Визуальные настройки
BG_COLOR    = "0x0a0f1e"   # тёмно-синий фон
TEXT_COLOR  = "white"
FONT_PATH   = "/System/Library/Fonts/Supplemental/Impact.ttf"  # Mac
# FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"  # Linux

# ─────────────────────────────────────────────
# 1. ВОРКФЛОУ ВОЙСОВЕРА
# ─────────────────────────────────────────────

def generate_voiceover(text: str, output_path: str, voice_id: str = DEFAULT_VOICE_ID) -> str:
    """Отправляет текст в VoicerAPI, ждёт готовности, скачивает MP3."""
    print(f"  🎙  Генерация войсовера ({len(text)} символов)...")

    headers = {"X-API-Key": VOICER_API_KEY, "Content-Type": "application/json"}
    payload = {
        "text": text,
        "template": {
            "model_id": "eleven_multilingual_v2",
            "voice_id": voice_id,
            "voice_settings": {"stability": 0.85, "similarity_boost": 0.75, "speed": 1.0}
        }
    }

    r = requests.post(f"{VOICER_BASE}/tasks", json=payload, headers=headers)
    r.raise_for_status()
    task_id = r.json()["task_id"]
    print(f"     Task ID: {task_id} — ожидаем...")

    # Поллинг статуса
    for _ in range(120):
        time.sleep(3)
        s = requests.get(f"{VOICER_BASE}/tasks/{task_id}/status", headers=headers)
        status = s.json()["status"]
        if status == "ending":
            break
        elif status in ("error", "error_handled"):
            raise RuntimeError(f"Ошибка генерации войсовера: {s.json()}")
    else:
        raise TimeoutError("Войсовер не готов за 6 минут")

    # Скачиваем результат
    res = requests.get(f"{VOICER_BASE}/tasks/{task_id}/result", headers=headers)
    res.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(res.content)
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


# ─────────────────────────────────────────────
# 2. ТРЕЙЛЕРЫ
# ─────────────────────────────────────────────

def download_trailer(movie_title: str, year: int, output_dir: str) -> str:
    """Ищет трейлер на YouTube через yt-dlp и скачивает его."""
    safe_name = "".join(c for c in movie_title if c.isalnum() or c in " _-").strip().replace(" ", "_")
    output_path = os.path.join(output_dir, f"{safe_name}_trailer.mp4")

    if os.path.exists(output_path):
        print(f"     ↩  Трейлер уже есть: {output_path}")
        return output_path

    query = f"{movie_title} {year} official trailer"
    print(f"  🎬  Скачиваем трейлер: {query}")

    cmd = [
        "yt-dlp",
        f"ytsearch1:{query}",
        "--format", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--output", output_path,
        "--no-playlist",
        "--quiet",
        "--no-warnings",
    ]
    subprocess.run(cmd, check=True)
    print(f"     ✓ Трейлер: {output_path}")
    return output_path


def trim_trailer_intro(input_path: str, output_path: str, skip: int = TRAILER_SKIP_SECONDS) -> str:
    """Обрезает начало трейлера (MPAA, логосы студий)."""
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
# 3. ПОСТЕРЫ
# ─────────────────────────────────────────────

def download_poster(movie_title: str, year: int, output_dir: str) -> str | None:
    """Скачивает постер: TMDB → OMDB как фолбэк. Возвращает путь или None."""
    safe_name = "".join(c for c in movie_title if c.isalnum() or c in " _-").strip().replace(" ", "_")
    poster_path = os.path.join(output_dir, f"{safe_name}_poster.jpg")

    if os.path.exists(poster_path):
        return poster_path

    # --- TMDB ---
    if TMDB_API_KEY:
        url = "https://api.themoviedb.org/3/search/movie"
        params = {"api_key": TMDB_API_KEY, "query": movie_title, "year": year, "language": "en-US"}
        r = requests.get(url, params=params, timeout=10)
        results = r.json().get("results", [])

        # Ищем постер с наилучшим рейтингом
        for result in results[:3]:
            if result.get("poster_path"):
                poster_url = f"https://image.tmdb.org/t/p/w780{result['poster_path']}"
                img = requests.get(poster_url, timeout=10)
                if img.status_code == 200 and len(img.content) > 10000:
                    with open(poster_path, "wb") as f:
                        f.write(img.content)
                    print(f"     ✓ Постер (TMDB): {poster_path}")
                    return poster_path

    # --- OMDB фолбэк ---
    if OMDB_API_KEY:
        r = requests.get(
            "https://www.omdbapi.com/",
            params={"apikey": OMDB_API_KEY, "t": movie_title, "y": year, "type": "movie"},
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

    print(f"     ⚠  Постер не найден для: {movie_title}")
    return None


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
    clips_per = max(1, round(n_clips / len(trailer_paths)))

    clip_files = []
    idx = 0

    for trailer_path in trailer_paths:
        try:
            duration = get_audio_duration(trailer_path)
        except Exception:
            continue

        safe_start = TRAILER_SKIP_SECONDS
        safe_end   = duration - TRAILER_END_SKIP

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
                "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
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
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-t", str(total_duration),
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-r", "30", "-an",
        output_path,
    ]
    run_ffmpeg(cmd)
    print(f"     ✓ Монтаж: {output_path} ({total_duration}s)")
    return output_path


# ─────────────────────────────────────────────
# 5. ХЕЛПЕРЫ ДЛЯ АНИМИРОВАННЫХ ТИТРОВ
# ─────────────────────────────────────────────

def _find_font() -> str:
    for p in [
        "/System/Library/Fonts/Supplemental/Impact.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]:
        if os.path.exists(p):
            return p
    return ""


def _dt_escape(text: str) -> str:
    """Экранирует спецсимволы для ffmpeg drawtext text=."""
    return (text
            .replace("\\", "\\\\")
            .replace("'",  "\\'")
            .replace(":",  "\\:")
            .replace("%",  "\\%"))


# ─────────────────────────────────────────────
# 5. СБОРКА СЕГМЕНТА (один фильм)
# ─────────────────────────────────────────────

def build_segment(
    number: int,
    title: str,
    year: int,
    voiceover_path: str,
    trailer_path: str,
    poster_path: str | None,
    output_path: str,
    work_dir: str,
    imdb_rating: float | None = None,
) -> str:
    """
    Собирает сегмент для одного фильма:
    - Постер с Ken Burns (POSTER_DURATION сек)
    - Трейлер (оставшееся время до конца войсовера)
    - Войсовер поверх всего
    - Плашка с номером и названием
    """
    print(f"     → Длительность войсовера...")
    vo_duration = get_audio_duration(voiceover_path)
    trailer_duration = max(vo_duration - POSTER_DURATION, 3.0)
    print(f"     → Войсовер: {vo_duration:.1f}s, трейлер будет: {trailer_duration:.1f}s")

    # Обрезаем трейлер до нужной длины
    trimmed_trailer = os.path.join(work_dir, f"seg{number}_trailer_trimmed.mp4")
    print(f"     → Обрезка интро трейлера...")
    trim_trailer_intro(trailer_path, trimmed_trailer)

    parts = []

    # --- Часть 1: Постер (blur background + медленный пан) ---
    print(f"     → Рендер постера...")
    poster_part = os.path.join(work_dir, f"seg{number}_poster.mp4")
    if poster_path and os.path.exists(poster_path):
        # Фон: постер растянут на весь экран с паном, размыт и затемнён
        # Передний план: постер в оригинальных пропорциях по центру
        fc = (
            "[0]scale=2160:1215:force_original_aspect_ratio=increase,"
            "crop=2160:1215:'(iw-2160)/2':'(ih-1215)/2'[src];"
            f"[src]crop=1920:1080:'(iw-ow)*t/{POSTER_DURATION}':'(ih-oh)/2',"
            "boxblur=22:4,eq=brightness=-0.2[bg];"
            "[0]scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1[fg];"
            "[bg][fg]overlay=0:0,setsar=1"
        )
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-t", str(POSTER_DURATION), "-i", poster_path,
            "-filter_complex", fc,
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-r", "30", "-an",
            poster_part,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s=1920x1080:d={POSTER_DURATION}",
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p", "-an",
            poster_part,
        ]
    run_ffmpeg(cmd)
    parts.append(poster_part)

    # --- Часть 2: Трейлер ---
    trailer_cut = os.path.join(work_dir, f"seg{number}_trailer_cut.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-i", trimmed_trailer,
        "-t", str(trailer_duration),
        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1",
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-r", "30", "-an",
        trailer_cut
    ]
    run_ffmpeg(cmd)
    parts.append(trailer_cut)

    # --- Конкатенируем части ---
    concat_video = os.path.join(work_dir, f"seg{number}_concat.mp4")
    list_file = os.path.join(work_dir, f"seg{number}_list.txt")
    with open(list_file, "w") as f:
        for p in parts:
            f.write(f"file '{os.path.abspath(p)}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-r", "30", "-an",
        concat_video
    ]
    run_ffmpeg(cmd)

    # --- Анимированные титры через drawtext ---
    # Название: выезжает слева за 0.5s, держится, угасает за 0.5s (t=4.5→5.0)
    # Номер: появляется сразу, то же угасание; белый с красным контуром, правый верхний угол
    font     = _find_font()
    font_opt = f"fontfile='{font}':" if font else ""
    rating_str = f"  ★ IMDB\\: {imdb_rating}" if imdb_rating is not None else ""
    txt      = _dt_escape(f"{title.upper()}, {year}{rating_str}")

    title_filter = (
        f"drawtext={font_opt}text='{txt}'"
        ":fontsize=44:fontcolor=white"
        ":bordercolor=red:borderw=2"
        ":box=1:boxcolor=0x000000@0.65:boxborderw=18"
        ":x='if(lt(t,0.5),-tw+(tw+80)*t/0.5,60)'"
        ":y=h-120"
        ":alpha='if(lt(t,4.5),1,if(lt(t,5.0),1-(t-4.5)/0.5,0))'"
        ":enable='between(t,0,5.0)'"
    )
    number_filter = (
        f"drawtext={font_opt}text='{number}'"
        ":fontsize=120:fontcolor=white"
        ":bordercolor=red:borderw=4"
        ":shadowcolor=black:shadowx=3:shadowy=3"
        ":x=w-tw-40:y=25"
        ":alpha='if(lt(t,4.5),1,if(lt(t,5.0),1-(t-4.5)/0.5,0))'"
        ":enable='between(t,0,5.0)'"
    )
    overlay_video = os.path.join(work_dir, f"seg{number}_overlay.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-i", concat_video,
        "-vf", f"{title_filter},{number_filter}",
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-r", "30", "-an",
        overlay_video,
    ]
    run_ffmpeg(cmd)

    # --- Добавляем войсовер ---
    cmd = [
        "ffmpeg", "-y",
        "-i", overlay_video,
        "-i", voiceover_path,
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        output_path
    ]
    run_ffmpeg(cmd)

    print(f"     ✓ Сегмент готов: {output_path} ({vo_duration:.1f}s)")
    return output_path


# ─────────────────────────────────────────────
# 6. ФИНАЛЬНАЯ СБОРКА
# ─────────────────────────────────────────────

def assemble_final(segments: list[str], output_path: str, work_dir: str) -> str:
    """Склеивает все сегменты в финальное видео."""
    print(f"\n🎞  Финальная сборка ({len(segments)} сегментов)...")

    list_file = os.path.join(work_dir, "final_list.txt")
    with open(list_file, "w") as f:
        for s in segments:
            f.write(f"file '{os.path.abspath(s)}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_file,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
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
    parser.add_argument("--output", default="final.mp4", help="Путь к финальному видео")
    parser.add_argument("--work-dir", default="./pipeline_work", help="Рабочая директория")
    parser.add_argument("--skip-voiceover", action="store_true", help="Пропустить генерацию войсовера (использовать готовые MP3)")
    args = parser.parse_args()

    with open(args.movies_json, encoding="utf-8") as f:
        data = json.load(f)

    movies   = data["movies"]
    voice_id = data.get("voice_id", DEFAULT_VOICE_ID)

    work_dir = args.work_dir
    os.makedirs(work_dir, exist_ok=True)
    for sub in ("trailers", "posters", "voiceovers", "segments", "montage"):
        os.makedirs(os.path.join(work_dir, sub), exist_ok=True)

    # ── Шаг 1: скачиваем все трейлеры сразу ──────────────────────────────
    print("\n📥  Загрузка трейлеров...")
    trailer_map: dict[int, str] = {}
    for movie in movies:
        trailer_map[movie["number"]] = download_trailer(
            movie["title"], movie["year"], os.path.join(work_dir, "trailers")
        )

    # ── Шаг 2: интро ─────────────────────────────────────────────────────
    intro_path = os.path.join(work_dir, "intro.mp4")
    intro_vo_text = data.get("intro_voiceover")
    if not os.path.exists(intro_path):
        print("\n🎬  Сборка интро...")
        intro_silent = os.path.join(work_dir, "intro_silent.mp4")
        build_montage(list(trailer_map.values()), INTRO_DURATION, intro_silent,
                      os.path.join(work_dir, "montage"))
        if intro_vo_text and not args.skip_voiceover:
            intro_vo = os.path.join(work_dir, "intro_vo.mp3")
            generate_voiceover(intro_vo_text, intro_vo, voice_id)
            run_ffmpeg([
                "ffmpeg", "-y",
                "-i", intro_silent, "-i", intro_vo,
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                intro_path,
            ])
        else:
            os.rename(intro_silent, intro_path)

    # ── Шаг 3: сегменты фильмов ───────────────────────────────────────────
    segments = []
    for movie in movies:
        number      = movie["number"]
        title       = movie["title"]
        year        = movie["year"]
        script      = movie["voiceover_text"]
        imdb_rating = movie.get("imdb_rating")

        print(f"\n[{number}] {title} ({year})")

        vo_path  = os.path.join(work_dir, "voiceovers", f"{number:02d}_{title.replace(' ', '_')}.mp3")
        seg_path = os.path.join(work_dir, "segments",   f"{number:02d}_{title.replace(' ', '_')}.mp4")

        if os.path.exists(seg_path):
            print(f"     ↩  Сегмент уже есть, пропускаем")
            segments.append(seg_path)
            continue

        if not os.path.exists(vo_path):
            if args.skip_voiceover:
                print(f"     ⚠  Войсовер не найден и --skip-voiceover включён, пропускаем {title}")
                continue
            generate_voiceover(script, vo_path, voice_id)

        print(f"  🖼  Скачиваем постер...")
        poster = download_poster(title, year, os.path.join(work_dir, "posters"))

        print(f"  🔧  Сборка сегмента...")
        build_segment(
            number=number,
            title=title,
            year=year,
            voiceover_path=vo_path,
            trailer_path=trailer_map[number],
            poster_path=poster,
            output_path=seg_path,
            work_dir=work_dir,
            imdb_rating=imdb_rating,
        )
        segments.append(seg_path)

    if not segments:
        print("❌  Нет готовых сегментов для сборки")
        sys.exit(1)

    # ── Шаг 4: аутро ─────────────────────────────────────────────────────
    outro_path = os.path.join(work_dir, "outro.mp4")
    outro_vo_text = data.get("outro_voiceover")
    if not os.path.exists(outro_path):
        print("\n🎬  Сборка аутро...")
        outro_silent = os.path.join(work_dir, "outro_silent.mp4")
        build_montage(list(trailer_map.values()), OUTRO_DURATION, outro_silent,
                      os.path.join(work_dir, "montage"))
        if outro_vo_text and not args.skip_voiceover:
            outro_vo = os.path.join(work_dir, "outro_vo.mp3")
            generate_voiceover(outro_vo_text, outro_vo, voice_id)
            run_ffmpeg([
                "ffmpeg", "-y",
                "-i", outro_silent, "-i", outro_vo,
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                outro_path,
            ])
        else:
            os.rename(outro_silent, outro_path)

    # ── Шаг 5: финальная сборка ───────────────────────────────────────────
    assemble_final([intro_path] + segments + [outro_path], args.output, work_dir)


if __name__ == "__main__":
    main()
