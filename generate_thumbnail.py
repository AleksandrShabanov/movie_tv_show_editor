#!/usr/bin/env python3
"""
Генерация YouTube-обложек через fal.ai (openai/gpt-image-2).

Зафиксированная модель по итогам сравнения (2026-08-21):
GPT Image 2 medium, 1920x1080 — $0.04/шт. См. memory
feedback_thumbnail_prompt_style.md за деталями выбора и композиционным
шаблоном (illustrated dense-cluster genre-poster style).

Использование:
    python3 generate_thumbnail.py "промпт..." --out thumbnails/my_video/v1.png
    python3 generate_thumbnail.py --prompt-file prompt.txt --out out.png
    python3 generate_thumbnail.py "промпт..." --quality low   # для черновиков, дешевле
"""
import argparse
import os
import sys
import urllib.request

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import fal_client  # noqa: E402


def generate(prompt: str, out_path: str, quality: str = "medium",
             size: str = "landscape_16_9") -> str:
    if not os.getenv("FAL_KEY"):
        sys.exit("❌  FAL_KEY не найден в .env")

    print(f"🎨  Генерация через openai/gpt-image-2 (quality={quality}, size={size})...")
    result = fal_client.subscribe(
        "openai/gpt-image-2",
        arguments={"prompt": prompt, "image_size": size, "quality": quality},
    )
    url = result["images"][0]["url"]
    print(f"     ✓ Готово: {url}")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    urllib.request.urlretrieve(url, out_path)
    print(f"     ✓ Сохранено: {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Генерация обложки через GPT Image 2 (fal.ai)")
    parser.add_argument("prompt", nargs="?", help="Текст промпта")
    parser.add_argument("--prompt-file", help="Путь к файлу с промптом (альтернатива позиционному аргументу)")
    parser.add_argument("--out", default="thumbnail.png", help="Куда сохранить PNG")
    parser.add_argument("--quality", choices=["low", "medium", "high"], default="medium",
                         help="low — дёшево для черновиков, medium — стандарт, high — дорого")
    parser.add_argument("--size", default="landscape_16_9",
                         help="landscape_16_9 (по умолчанию) или другой пресет/строка WxH")
    args = parser.parse_args()

    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as f:
            prompt = f.read().strip()
    elif args.prompt:
        prompt = args.prompt
    else:
        sys.exit("❌  Укажи промпт позиционным аргументом или через --prompt-file")

    generate(prompt, args.out, quality=args.quality, size=args.size)


if __name__ == "__main__":
    main()
