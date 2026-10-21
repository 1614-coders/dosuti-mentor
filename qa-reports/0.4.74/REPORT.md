# DoSuti Mentor 0.4.74 — P1/P2: граница доверия к alignment

Дата: 2026-10-21. Исправление локальное, без commit/push/PR/публикации.

## Исходное состояние и изоляция

В начале и при повторной проверке удалённый main:
`978d6a4d026a66c2fe26ead37dcbba2392ee526c`, plugin.json — **0.4.73**.
Новых коммитов относительно ожидаемого SHA нет.

Создан отдельный clone:
`/Users/fadeevtimofey/Documents/Codex/2026-10-12/new-chat/work/dosuti-alignment-0474`.
Его HEAD остаётся исходным SHA; исправления находятся в рабочем дереве.
Тесты используют TemporaryDirectory и QA-профили внутри временных курсов.
Пользовательские курсы, установленный плагин и прежняя рабочая копия не изменялись.
Использован существующий отдельный venv; новые зависимости не устанавливались.

## Воспроизведение на исходной 0.4.73

Исходная проверка полностью совпала с предоставленными числами:

| Запуск | Total | Pass | Fail | Errors | Skip |
| --- | ---: | ---: | ---: | ---: | ---: |
| Семь обязательных модулей до исправления | 83 | 83 | 0 | 0 | 0 |
| Полный набор до исправления | 1111 | 1096 | 0 | 0 | 15 |
| Первые пять новых проверок до изменения кода | 5 | 1 | 3 | 1 | 0 |

Логи: [baseline-targeted.log](baseline-targeted.log), [baseline-full.log](baseline-full.log),
[red-new-tests.log](red-new-tests.log). Внешние дополнительные 8 pass / 2 fail
из задания не выдаются за наш запуск: их исходник не предоставлен. Здесь выполнены
эквивалентные обязательные воспроизведения P1 и P2.

**P1.** В тесте создан пробел `Java synchronized visibility and happens-before`.
Ответ: «It only excludes other threads; it gives no visibility guarantee»,
evidence=unknown. Target ссылается на HashMap unit и duplicate-key criterion.
Review=supported имеет ту же competence и diagnostic_event_revisions, но настоящая
цитата и объяснение описывают только HashMap. После принятия плана и правильного
самостоятельного ответа про два put исходная версия возвращает completed_steps=[1].
Новый тест падает на сравнении [1] с [].

**P2.** В положительном Map review полная цитата заменена на `put`. Исходная
версия записывает предложение; новый тест падает: ожидаемый ValueError не возник.
Причина — [поиск подстроки в 0.4.73](https://github.com/1614-coders/dosuti-mentor/blob/978d6a4d026a66c2fe26ead37dcbba2392ee526c/plugins/dosuti-mentor/scripts/learning_intent.py#L173).

Третье падение RED — прежний автоматический зачёт в положительном Map-примере;
он также противоречит новому консервативному контракту. Один ERROR — отсутствие
нового поля evidence_valid у короткого полного фрагмента. Это изменения контракта
новых тестов, а не ещё два заявленных независимых дефекта.

## Защита P1: прекращение автоматического семантического зачёта

Причина не в недостаточном количестве полей. Один и тот же ассистент создаёт
competence, rationale, explanation и verdict. Его полностью согласованная запись
не является независимым доказательством его же содержательного вывода.
Согласие пользователя с планом — согласие на задания, а не доказательство знаний.

Новый контракт:

- Полный supported review сохраняется для аудита, но alignment.status=unverified.
  review_verdict=supported и evidence_valid=true означают только целостность
  цитируемого свидетельства. Независимого семантического verifier здесь нет.
- Для targets автоматическое completed_steps не заполняется по model-review.
  Даже ошибочный supported с согласованными полями не может дать зачёт concurrency.
- Реальные сохранённые оценки конкретных unit-критериев остаются видимыми:
  demonstrated_units, criteria, критерии/история ответов не стираются. Это записи
  оценки конкретных ответов, не математическая гарантия правильности оценки модели.
- Выполнение выбранных заданий отражается в criterion_completed_steps и
  criteria_demonstrated, отдельно от competence_status=unverified. Зависимости и
  следующий учебный шаг используют выполнение заданий, чтобы не заставлять
  повторять уже решённую практику. Это не доказательство обобщённых предпосылок.
- Учебная ссылка и успех unit не удаляют исторический диагностический пробел.
  Все исходные unknown остаются в uncovered_diagnostic_gaps до новой диагностики;
  сама диагностика не переписывается при чтении или успехе unit.

Следствие: **валидный Map-пример проходит предложение, принятие и сохранение
успешной практики**, но тоже не получает автоматическую аттестацию общей
компетенции. Это намеренный безопасный fallback из задания, а не попытка
сымитировать семантическую проверку дополнительным флагом.

Код:
[target_alignment:152](/Users/fadeevtimofey/Documents/Codex/2026-10-12/new-chat/work/dosuti-alignment-0474/plugins/dosuti-mentor/scripts/learning_intent.py:152),
[отключение автоматического зачёта:354](/Users/fadeevtimofey/Documents/Codex/2026-10-12/new-chat/work/dosuti-alignment-0474/plugins/dosuti-mentor/scripts/learning_intent.py:354),
[раздельный прогресс:366](/Users/fadeevtimofey/Documents/Codex/2026-10-12/new-chat/work/dosuti-alignment-0474/plugins/dosuti-mentor/scripts/learning_intent.py:366),
[непроверенный статус и сохранение пробелов:124](/Users/fadeevtimofey/Documents/Codex/2026-10-12/new-chat/work/dosuti-alignment-0474/plugins/dosuti-mentor/scripts/adaptive_learning.py:124).

## Защита P2: целостный адресуемый фрагмент

Цитата должна совпадать с полными последовательными строками выбранного источника
либо целым однострочным речевым фрагментом. Вырезанное слово put внутри строки о
замене значения больше не является допустимым фрагментом: proposal отклоняется
до записи. Нет порога длины, классификации по ключевым словам или догадок по
пунктуации. Полная исходная строка Map и короткая полная строка кода `x++;` проходят.

Это **проверка целостности цитирования**, не её смысловой достаточности. Если
источник сам состоит из одного слова, его целостное цитирование не доказывает
компетенцию: статус всё равно unverified, семантического зачёта нет. Длинная,
но неподходящая цитата также не даёт зачёта. Не заявляется автоматическое
понимание Java, содержания источников или правильности explanation.

Код: [границы цитаты:173](/Users/fadeevtimofey/Documents/Codex/2026-10-12/new-chat/work/dosuti-alignment-0474/plugins/dosuti-mentor/scripts/learning_intent.py:173).
Полный валидный Map проверяется отдельно от короткого фрагмента.

## Регрессии и совместимость

В [test_alignment_trust.py](/Users/fadeevtimofey/Documents/Codex/2026-10-12/new-chat/work/dosuti-alignment-0474/plugins/dosuti-mentor/tests/test_alignment_trust.py)
добавлены восемь тестовых методов:

1. Ошибочный полностью согласованный supported HashMap→concurrency; no completed_steps,
   сохранённый unit result, непроверенная компетенция и оставшийся диагностический пробел.
2. Обрезанная цитата put отклоняется без записи.
3. Полный Map сохраняет unit-результат и выполнение критериев без семантического зачёта.
4. Отсутствующий/пустой verdict, пустая и выдуманная цитата не записываются.
5. Короткая, но полная строка кода проходит как цитата без аттестации компетенции.
6. **Восемь реальных CLI-процессов**: четыре команды для двух вариантов плохого
   intent — повреждённый JSON и будущая схема. Exit=0, unit progress сохраняется,
   learning_progress.valid=false, stopped_at=null; ни один файл курса не меняется.
7. Существующий v6 с короткой цитатой читается с plan.needs_review без миграции,
   переписывания истории или потери unit-результата.
8. Профили с заголовками v1–v6 читаются без изменений файлов и потери независимых
   результатов; расширенные career/context/bases-пути остаются в полном старом наборе.

Существующие проверки переноса между competence/revisions, неверных критериев,
булевых ревизий, неподтверждённых unit, CAS и устаревшего proposal сохранены.
Ни один тест не отключён. Прежние положительные assertions о выполнении задач
перенесены на criterion_completed_steps и дополнены запретом completed_steps.
Сохранены проверки следующего шага, зависимостей, assisted/error/recheck/skip,
отсутствия записи при чтении, истории и отдельного unit progress.
В educational fixture цитата частот расширена до полной существующей строки,
чтобы соответствовать новому контракту P2, а не ослаблять его.

Дополнительно выполнены две мутации **только в памяти**: возврат зачёта
completed=criteria_completed и возврат поиска подстроки. Соответствующие P1/P2
тесты падают; оригинальные функции восстановлены, исходные файлы не менялись.

## Команды и фактические результаты

Рабочий каталог команд — `plugins/dosuti-mentor/tests` новой копии.

```sh
PYTHONDONTWRITEBYTECODE=1 ../../../../venv/bin/python -B -m unittest \
  test_target_alignment test_roadmap_adaptation test_educational_acceptance \
  test_incomplete_intent test_learning_progress test_diagnostic_learning test_learning_journey

PYTHONDONTWRITEBYTECODE=1 ../../../../venv/bin/python -B -m unittest test_alignment_trust

PYTHONDONTWRITEBYTECODE=1 ../../../../venv/bin/python -B -m unittest \
  test_alignment_trust test_target_alignment test_roadmap_adaptation test_educational_acceptance \
  test_incomplete_intent test_learning_progress test_diagnostic_learning test_learning_journey

PYTHONDONTWRITEBYTECODE=1 ../../../../venv/bin/python -B -m unittest discover -s . -p 'test_*.py'
```

Полный запуск выполнен с существующими зависимостями и разрешённым localhost.

| После исправления | Total | Pass | Fail | Errors | Skip | Лог |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Семь обязательных модулей | 83 | 83 | 0 | 0 | 0 | [final-required.log](final-required.log) |
| Новые проверки | 8 | 8 | 0 | 0 | 0 | [final-new-tests.log](final-new-tests.log) |
| Обязательные + новые | 91 | 91 | 0 | 0 | 0 | [final-extended.log](final-extended.log) |
| Финальный полный запуск | 1119 | 1104 | 0 | 0 | 15 | [final-full.log](final-full.log) |

Разница с 1111/1096 — ровно восемь новых тестовых методов. Все 15 пропусков
по-прежнему относятся к PowerShell/Windows; новых skip/xfail нет. Проверка
Python AST, заголовков навыков и git diff --check также прошла.

## Схема и версия

Локальная версия повышена до **0.4.74**, однако защита — изменение кода, а не номер.
Новые журналы/схемы не введены: v6 и события остаются прежними. Миграция не нужна,
потому что меняется вычисляемая сводка, а не сохранённые ответы/планы.
Исторический supported больше не превращается при чтении в аттестацию компетенции.

Это намеренное изменение выходного контракта для targets: клиентам следует
использовать criterion_completed_steps для результатов заданий, а не старый
completed_steps как универсальную оценку знаний. Legacy-планы без targets
сохраняют прежний unit-based completed_steps как совместимый результат задач,
не как доказательство смысла произвольного заголовка. Scripts и skills следует
обновлять совместно. При необходимости независимого семантического verifier
потребуется отдельное проектирование, а не ещё один записываемый моделью флаг.

## Изменённые файлы

- scripts: plugins/dosuti-mentor/scripts/learning_intent.py, adaptive_learning.py.
- tests: plugins/dosuti-mentor/tests/test_alignment_trust.py (новый),
  test_target_alignment.py, test_roadmap_adaptation.py, test_educational_acceptance.py,
  test_incomplete_intent.py.
- skills: plugins/dosuti-mentor/skills/plan-career/SKILL.md,
  review-learning/SKILL.md, teach-unit/SKILL.md.
- references: plugins/dosuti-mentor/references/learning-intent.md.
- plugins/dosuti-mentor/plugin.json, CHANGELOG.md.
- qa-reports/0.4.74/REPORT.md и семь исходных логов запусков.

## Статусы дефектов и этапов

| Объект | Решение |
| --- | --- |
| P1, ложный зачёт по model-supported | Исправлен безопасным отказом от автоматической семантической аттестации |
| P2, put как подстрока доказательства | Отклоняется как неполный фрагмент; полноценные цитаты и unit-практика проходят |
| Повреждённый/неподдерживаемый intent | Существующая защита сохранена, подтверждена API и реальными CLI |
| Этап 2 | Консервативный режим готов к независимой приёмке; автоматическая содержательная верификация не реализована и не заявлена |
| Этап 3, программные компоненты | Сквозные проверки проходят |
| Этап 3, живой диалог Codex UI | Не проверен; этап полностью не закрыт |
| Публикация | Не выполнена и не считается разрешённой результатами тестов |

Python fixtures используют сценарные ответы и синтетическое медиа. CLI subprocess
доказывает восстановление из файлов в другом процессе, но не поведение модели.
В Codex UI не проводилось взаимодействие, запрет доступа к com.openai.codex не
обходился. Нативные Windows/Linux, качество настоящего ASR/OCR и педагогическая
правильность ответов модели этим исправлением не подтверждены.

**Решение: передавать 0.4.74 на независимую приёмку P1/P2 в явно непроверенном
семантическом режиме; не объявлять автоматическую аттестацию или живую приёмку
готовыми.** Удалённый main и plugin.json при повторной проверке не изменились.
