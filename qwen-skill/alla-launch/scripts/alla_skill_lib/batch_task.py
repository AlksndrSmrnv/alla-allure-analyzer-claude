"""Задание субагенту на пакет кластеров: ``batches/N.md``.

Когда кластеров много, основной агент раздаёт их пакетами субагентам Qwen Code
(инструмент ``agent``). Субагент стартует с чистым контекстом и видит только
этот файл, поэтому он самодостаточен: правила, список «кластер → задание →
файл разбора», формат ответа и команда проверки ``verify``. Сами правила
анализа лежат в ``clusters/NN.md`` — здесь только то, что относится к работе
субагента.
"""

from __future__ import annotations

from typing import Any

from alla_skill_lib import workspace as ws
from alla_skill_lib.analysis_format import EXPECTED_FORMAT
from alla_skill_lib.cluster_task import UNTRUSTED_NOTE

MAX_VERIFY_ROUNDS = 3


def verify_command(paths: ws.RunPaths, file_ids: list[str]) -> str:
    return ws.skill_command("verify", *file_ids, "--run", str(paths.root))


def render_batch_task(
    paths: ws.RunPaths,
    number: int,
    entries: list[dict[str, Any]],
    launch_id: int,
) -> str:
    """Текст ``batches/N.md`` для пакета ``entries`` (записи ``run.json``)."""
    ids = [entry["file_id"] for entry in entries]
    lines = [
        f"# Пакет {number}: кластеры {', '.join(ids)} · прогон #{launch_id}",
        "",
        "Ты — субагент: разбираешь только кластеры этого пакета, остальные разбирают другие. "
        "Основной агент ждёт твоего ответа. Пиши по-русски, коротко и простым языком.",
        "",
        UNTRUSTED_NOTE,
        "",
        "## Что делать",
        "Для каждого кластера из списка по порядку:",
        "1. Прочитай его задание (read_file). Правила и формат ответа — в самом задании. "
        "Строку «Затем выполни: …» в его начале пропусти: команды скилла, кроме проверки "
        "ниже, выполняет только основной агент.",
        "2. Если нужно понять, что проверяет тест, открой не больше 3 файлов проекта из "
        "раздела «Где искать код автотеста» или из кадров стека. Только чтение.",
        "3. Запиши разбор в указанный файл инструментом write_file.",
        "",
        "Кластеры пакета:",
    ]
    for entry in entries:
        label = " ".join(str(entry["label"]).split())
        if len(label) > 100:
            label = label[:99] + "…"
        lines += [
            f"- {entry['file_id']} · {label} ({entry['member_count']} тест.)",
            f"  задание: {paths.cluster_task(entry['file_id'])}",
            f"  разбор:  {paths.analysis(entry['file_id'])}",
        ]
    lines += [
        "",
        "## Проверка",
        "Когда записал все разборы, выполни одну команду (shell):",
        "",
        f"    {verify_command(paths, ids)}",
        "",
        "`STATUS: ok` — всё принято. `STATUS: fix` — исправь названные файлы (write_file) и "
        f"повтори проверку; не больше {MAX_VERIFY_ROUNDS} раз на кластер. Если разбор так и не "
        "прошёл — оставь как есть и назови номер в ответе.",
        "",
        "## Формат разбора",
        EXPECTED_FORMAT,
        "",
        "## Нельзя",
        "- Выполнять команды скилла, кроме проверки выше: next, skip, apply, revert, "
        "remember, reject, prepare — они не для тебя.",
        "- Запускать других субагентов, тесты, сборку, git, curl; читать .env, run.json, "
        "evidence/ и сырые логи.",
        "- Менять что-либо, кроме файлов «разбор» из списка; писать их только через write_file "
        "(не через echo, cat или heredoc).",
        "- Разбирать кластеры не из этого пакета.",
        "",
        "## Ответ основному агенту",
        "Одной строкой, без пересказа разборов: «Готово: 03, 04.» или "
        "«Готово: 03, 04. Не прошли проверку: 05 — причина.»",
    ]
    return "\n".join(lines) + "\n"
