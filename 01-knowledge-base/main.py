"""
main.py — точка входа для индексации content-brain.

Использование:
  python main.py --full     # полная индексация всех записей и постов
  python main.py --update   # только новое за последние 7 дней (по умолчанию)
"""

import argparse
import asyncio
import sys

from indexer import run_indexing


def parse_args():
    parser = argparse.ArgumentParser(
        description="content-brain knowledge base indexer"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--full",
        action="store_true",
        help="Полная индексация: все записи дневника и посты канала",
    )
    mode.add_argument(
        "--update",
        action="store_true",
        default=True,
        help="Только новое за последние 7 дней (по умолчанию)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    full = args.full  # --update или дефолт → full=False

    mode_label = "ПОЛНАЯ индексация" if full else "ОБНОВЛЕНИЕ (7 дней)"
    print(f"[content-brain] Запуск: {mode_label}")

    try:
        asyncio.run(run_indexing(full=full))
        print("[content-brain] Индексация завершена успешно.")
    except KeyboardInterrupt:
        print("\n[content-brain] Прервано пользователем.")
        sys.exit(0)
    except Exception as e:
        print(f"[content-brain] Ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
