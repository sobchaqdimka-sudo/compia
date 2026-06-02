"""CLI-точка входа тестера.

Usage:
    COMPIA_TEST_MODE=1 python -m tester
    COMPIA_TEST_MODE=1 python -m tester --scenario onboarding_to_mira_photo
    COMPIA_TEST_MODE=1 python -m tester --keep-db
    COMPIA_TEST_MODE=1 python -m tester --no-judge   # только прогон без оценки
"""

import argparse
import asyncio
import logging
import os
import sys
from typing import Optional

from .scenarios import SCENARIOS
from .simulator import dump_report, run_scenario


def _setup_test_mode():
    """Включить TEST_MODE заранее (до импорта config). Подставить корректный по
    формату fake-токен (aiogram валидирует <int>:<hash> при инициализации Bot)."""
    os.environ["COMPIA_TEST_MODE"] = "1"
    os.environ.setdefault("TELEGRAM_TOKEN", "123456789:AAEtestplaceholderAAAAAAAAAAAAAAAAAAA")


def _print_report(report: dict, judge_result):  # type: Optional[dict]
    print()
    print("=" * 70)
    print(f"SCENARIO:  {report['scenario']}")
    print(f"PROFILE:   {report['profile']}")
    print(f"TURNS:     {sum(1 for t in report['turns'] if t['role'] == 'user')}")
    print(f"ELAPSED:   {report['elapsed_sec']}s")
    print("=" * 70)
    print()

    print("MILESTONES:")
    all_expected = set(report["milestones_hit"] + report["milestones_missed"])
    for m in sorted(all_expected):
        mark = "+" if m in report["milestones_hit"] else "-"
        print(f"  [{mark}] {m}")
    print()

    if judge_result is None:
        print("(judge skipped)")
        return

    findings = judge_result.get("findings", [])
    print(f"JUDGE: {len(findings)} finding(s)")
    if not findings:
        print("  (нарушений не найдено)")
    else:
        for f in findings:
            sev = f.get("severity", "?").upper()
            pid = f.get("pattern_id", "?")
            turn = f.get("turn", "?")
            owner = f.get("fix_owner", "?")
            quote = f.get("quote", "")
            expl = f.get("explanation", "")
            print(f"  [{sev:6s}] {pid}  (ход {turn}, fix: {owner})")
            if quote:
                print(f"          цитата: «{quote}»")
            if expl:
                print(f"          {expl}")
    print()
    print(f"SUMMARY: {judge_result.get('summary', '')}")
    if "_raw" in judge_result:
        print(f"(сырой ответ судьи: {judge_result['_raw'][:200]}...)")


async def _amain(args):
    scenarios_to_run = [args.scenario] if args.scenario else list(SCENARIOS.keys())

    overall_findings = 0
    for sname in scenarios_to_run:
        if sname not in SCENARIOS:
            print(f"Неизвестный сценарий: {sname}", file=sys.stderr)
            print(f"Доступные: {', '.join(SCENARIOS.keys())}", file=sys.stderr)
            return 2

        report = await run_scenario(sname, keep_db=args.keep_db)
        path = dump_report(report)
        print(f"\nТранскрипт сохранён: {path}")

        judge_result = None
        if not args.no_judge:
            from .judge import judge as run_judge
            print("Прогоняю транскрипт через судью...")
            judge_result = run_judge(report)
            # Сохраняем рядом
            judge_path = path.replace(".json", "_judge.json")
            import json
            with open(judge_path, "w", encoding="utf-8") as f:
                json.dump(judge_result, f, ensure_ascii=False, indent=2)

        _print_report(report, judge_result)
        if judge_result:
            overall_findings += len(judge_result.get("findings", []))

    # Exit code: 0 если judge ничего не нашёл, 1 если есть findings.
    return 1 if overall_findings else 0


def main():
    _setup_test_mode()

    parser = argparse.ArgumentParser(description="Compia E2E AI-тестер")
    parser.add_argument(
        "--scenario", help="прогнать только один сценарий", default=None,
    )
    parser.add_argument(
        "--keep-db", action="store_true",
        help="не удалять tester_companion.db после прогона (для дебага)",
    )
    parser.add_argument(
        "--no-judge", action="store_true",
        help="только прогнать сценарий, не вызывать судью",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="DEBUG-логи",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Anthropic SDK тихий, иначе шумит retries.
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    exit_code = asyncio.run(_amain(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
