"""Run offline tests and generate a shareable Markdown report from actual results."""
from __future__ import annotations

import argparse
import hashlib
import io
import platform
import sys
import time
import unittest
from collections import Counter
from datetime import date
from pathlib import Path
from unittest.mock import patch


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = {}

    def startTest(self, test):
        super().startTest(test)
        self.outcomes[test.id()] = "RUNNING"

    def addSuccess(self, test):
        super().addSuccess(test)
        self.outcomes[test.id()] = "PASS"

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.outcomes[test.id()] = "FAIL"

    def addError(self, test, error):
        super().addError(test, error)
        self.outcomes[test.id()] = "ERROR"

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.outcomes[test.id()] = "SKIP"

    def addSubTest(self, test, subtest, error):
        super().addSubTest(test, subtest, error)
        if error is not None:
            self.outcomes[test.id()] = "FAIL"

    def addExpectedFailure(self, test, error):
        super().addExpectedFailure(test, error)
        self.outcomes[test.id()] = "EXPECTED_FAILURE"

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.outcomes[test.id()] = "UNEXPECTED_SUCCESS"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default="TEST_RESULTS.md")
    parser.add_argument("--report-date", type=date.fromisoformat, default=None,
                        help="Optional ISO date for the report in the operator's timezone")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    capture = io.StringIO()
    started = time.monotonic()
    # This encompasses discovery as well as execution. An accidental network attempt fails.
    with patch("socket.socket", side_effect=AssertionError("Network disabled in offline tests")), \
         patch("socket.create_connection", side_effect=AssertionError("Network disabled in offline tests")):
        suite = unittest.defaultTestLoader.discover(str(root / "tests"))
        result = unittest.TextTestRunner(stream=capture, verbosity=2, resultclass=RecordedResult).run(suite)
    elapsed = time.monotonic() - started
    print(capture.getvalue(), end="")
    counts = Counter(result.outcomes.values())
    source_files = sorted([*root.glob("*.py"), *root.joinpath("tests").glob("*.py")])
    hashes = [(p.relative_to(root).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest()) for p in source_files]
    fingerprint = hashlib.sha256("\n".join(f"{name}:{digest}" for name, digest in hashes).encode()).hexdigest()
    lines = ["# Результаты офлайн-тестирования", "",
             "Отчёт автоматически сформирован `run_tests.py` по результатам фактического запуска.", ""]
    if args.report_date:
        lines += [f"Дата отчёта: {args.report_date.isoformat()} (часовой пояс оператора).", ""]
    lines += [f"**Итог: {'PASS' if result.wasSuccessful() else 'FAIL'}.** "
              f"Запущено {result.testsRun} тестов; PASS: {counts['PASS']}; "
              f"FAIL/ERROR: {len(result.failures) + len(result.errors)}; SKIP: {len(result.skipped)}.", "",
              f"Среда: Python {platform.python_version()}, {platform.system()}. Время: {elapsed:.3f} с.", "",
              "## Что демонстрирует проверка", "",
              "- Локальную логику загрузки MP4 и статики JPG/PNG, смешанные пачки и Google Drive source adapter.",
              "- Формирование HTTP-параметров и multipart для поддельных Meta API-ответов.",
              "- Дедупликацию по аккаунту и содержимому, сохранение video_id/image_hash и результатов каждого задания.",
              "- Обработку таймаутов, незавершённых операций, ошибок реестра и восстановление без слепого повторного POST.",
              "- Валидацию файлов, конфигурации, разделение credentials и отсутствие токена в URL/сообщениях ошибок.", "",
              "Сетевые socket-соединения заблокированы на весь прогон. Реальные кабинеты, токены и расходы не используются.", "",
              "## Границы доказательства", "",
              "Это подтверждение локальных сценариев и контрактов на fake API, а не доказательство успешной работы с живой Meta/Google. "
              "Не проверены реальные права приложения, принятие payload текущей API-версией, кодирование видео и допустимость "
              "изображений для placements. PNG генерируется как маленькое настоящее изображение; JPEG и MP4 fixtures "
              "проверяют маркеры/заголовки и маршрутизацию, а не декодирование медиа. Полное декодирование файлов загрузчик не выполняет.", "",
              "## Воспроизведение", "", "```powershell", "python run_tests.py --report TEST_RESULTS.md", "```", "",
              "Не нужны токены, аккаунты или сторонние Python-библиотеки. Команда возвращает 0 при успехе и 1 при провале.", "",
              "## Каждый тест", "", "| Тест | Результат |", "|---|---|"]
    lines += [f"| `{name}` | {outcome} |" for name, outcome in sorted(result.outcomes.items())]
    lines += ["", "## Привязка к исходникам", "",
              "SHA-256 набора Python-файлов позволяет отличить проверенную версию от последующих изменений; "
              "это контрольная сумма, не независимая сертификация.", "", f"`{fingerprint}`", "",
              "| Файл | SHA-256 |", "|---|---|"]
    lines += [f"| `{name}` | `{digest}` |" for name, digest in hashes]
    target = Path(args.report)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Markdown report written: {target.name}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
