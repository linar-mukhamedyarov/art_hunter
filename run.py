#!/usr/bin/env python3
"""
Art Hunter — Japan Gallery Report Pipeline
Step 1 (Haiku):  web-search galleries in each city
Step 2 (Sonnet): validate via WebFetch
Step 3 (Opus):   generate Russian-language report
"""

import json
import sys
from datetime import datetime
from pathlib import Path

# Force UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from cli import claude_call

CONFIG_FILE = PROJECT_ROOT / "config" / "trip.json"
PROMPTS_DIR = PROJECT_ROOT / "prompts"
OUTPUT_DIR = PROJECT_ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

MODEL_SEARCH = "claude-haiku-4-5-20251001"  # fast, cheap — web search
MODEL_VALIDATE = "claude-sonnet-4-6"  # balanced — verification
MODEL_REPORT = "claude-opus-4-7"  # best reasoning — final report


# ── helpers ──────────────────────────────────────────────────────────────────


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def _days_word(n: int) -> str:
    if n == 1:
        return "день"
    if n in (2, 3, 4):
        return "дня"
    return "дней"


# ── pipeline steps ────────────────────────────────────────────────────────────


def search_city(city: dict, categories: list) -> str:
    print(f"\n[SEARCH/Haiku] {city['name']} ({city['name_ja']})…")

    cats = "\n".join(
        f"  - {c['name_ru']} ({c['name_en']}, {c.get('name_ja', '')})"
        + (f"  — периоды: {', '.join(c['periods'])}" if c.get("periods") else "")
        for c in categories
    )

    user_msg = f"""Город: {city["name"]} ({city["name_ja"]})
Дней в городе: {city["days"]} {_days_word(city["days"])}

Категории поиска:
{cats}

Поисковые запросы на японском (используй WebSearch):
  日本刀 販売 {city["name_ja"]}
  刀剣 ギャラリー {city["name_ja"]}
  甲冑 専門店 {city["name_ja"]}
  鎌 古武器 {city["name_ja"]}
  NBTHK 加盟 刀剣商 {city["name_ja"]}

Поисковые запросы на английском (используй WebSearch):
  nihonto dealer {city["name"]}
  samurai sword gallery {city["name"]}
  antique japanese armor {city["name"]}
  NBTHK member dealer {city["name"]}
"""

    result = claude_call(
        prompt=user_msg,
        system_prompt=load_prompt("search"),
        model=MODEL_SEARCH,
        web_search=True,
        step_name=f"search_{city['name'].lower()}",
    )
    print(f"  → {len(result)} chars")
    return result


def validate_all(search_results: dict) -> str:
    print(f"\n[VALIDATE/Sonnet] Проверяю {len(search_results)} городов…")

    blocks = "\n\n".join(
        f"=== {city} ===\n{text}" for city, text in search_results.items()
    )
    user_msg = f"Проверь следующие галереи:\n\n{blocks}"

    result = claude_call(
        prompt=user_msg,
        system_prompt=load_prompt("validate"),
        model=MODEL_VALIDATE,
        web_search=True,
        step_name="validate_all",
    )
    print(f"  → {len(result)} chars")
    return result


def generate_report(validated: str, config: dict) -> str:
    print("\n[REPORT/Opus] Генерирую отчёт…")

    route = " → ".join(
        f"{c['name']} ({c['days']} {_days_word(c['days'])})"
        for c in sorted(config["cities"], key=lambda x: x["order"])
    )
    cats = ", ".join(c["name_ru"] for c in config["categories"])

    user_msg = f"""Маршрут: {route}
Даты: с {config["date_range"]["start"]}, {config["date_range"]["duration_days"]} дней
Категории: {cats}

--- ПРОВЕРЕННЫЕ ДАННЫЕ ГАЛЕРЕЙ ---
{validated}
"""

    result = claude_call(
        prompt=user_msg,
        system_prompt=load_prompt("report"),
        model=MODEL_REPORT,
        web_search=False,
        step_name="generate_report",
    )
    print(f"  → {len(result)} chars")
    return result


def save_report(report: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUTPUT_DIR / f"japan_galleries_{ts}.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n[OUTPUT] Сохранён: {out}")
    return out


def print_cost_summary():
    logs_dir = PROJECT_ROOT / "logs"
    today = datetime.now().strftime("%Y%m%d")
    log_file = logs_dir / f"calls_{today}.jsonl"
    if not log_file.exists():
        return

    calls = []
    with open(log_file, encoding="utf-8") as f:
        for line in f:
            try:
                calls.append(json.loads(line))
            except Exception:
                pass

    print("\n" + "─" * 47)
    print(f"{'Шаг':<32} {'Модель':<10} {'Время':>4}")
    print("─" * 47)
    for c in calls:
        step = c.get("step", "?")[:31]
        model = c.get("model", "?").split("-")[1] if "-" in c.get("model", "") else "?"
        dur = c.get("duration_ms", 0) / 1000
        print(f"{step:<32} {model:<10} {dur:>4.1f}s")
    print("─" * 47)


# ── main ──────────────────────────────────────────────────────────────────────


def main():
    print("=" * 55)
    print("  ART HUNTER — Японский отчёт")
    print("  Haiku -> Sonnet -> Opus")
    print("=" * 55)

    config = load_config()
    cities = sorted(config["cities"], key=lambda x: x["order"])
    cats = config["categories"]
    print(f"Маршрут: {' → '.join(c['name'] for c in cities)}")
    print(f"Категории: {', '.join(c['name_ru'] for c in cats)}\n")

    # ── Step 1: search each city ──────────────────────────────────────────────
    search_results: dict[str, str] = {}
    for city in cities:
        try:
            search_results[city["name"]] = search_city(city, cats)
        except RuntimeError as e:
            print(f"  [ERROR] {e}")
            search_results[city["name"]] = f"SEARCH_FAILED: {e}"

    good = {
        k: v for k, v in search_results.items() if not v.startswith("SEARCH_FAILED")
    }
    if not good:
        print("\n[FATAL] Поиск не дал результатов. Проверь интернет и claude CLI.")
        sys.exit(1)
    print(f"\n[OK] Поиск завершён: {len(good)}/{len(cities)} городов")

    # ── Step 2: validate ──────────────────────────────────────────────────────
    try:
        validated = validate_all(search_results)
    except RuntimeError as e:
        print(f"  [WARN] Валидация не удалась: {e}")
        print("  Использую сырые результаты поиска…")
        validated = "\n\n".join(
            f"=== {k} (NOT VALIDATED) ===\n{v}" for k, v in good.items()
        )

    # ── Step 3: generate report ───────────────────────────────────────────────
    try:
        report = generate_report(validated, config)
        out_path = save_report(report)
    except RuntimeError as e:
        print(f"  [ERROR] Генерация отчёта не удалась: {e}")
        sys.exit(1)

    # ── Summary ───────────────────────────────────────────────────────────────
    print_cost_summary()
    print(f"\n[DONE] Отчёт: {out_path}")


if __name__ == "__main__":
    main()
