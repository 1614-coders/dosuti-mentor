# Краткий CLI-контекст наставника

Обычные действия знакомства, диагностики и обучения выполняй с `--compact`.
Поддержаны `settings`, `diagnostic`, `learn-unit`, `context`, `resume`, `intent`,
`study-next`, `progress`. Это формат ответа `output_view=compact_v1`, а не новый
профиль, журнал или оценивание. Без флага полностью сохранён прежний CLI-ответ.

## Что использовать

- Знакомство: `onboarding_message`, `onboarding_next`, все `onboarding_answers`,
  `effective` и `checks_source`. Запись ответа уже возвращает следующий вопрос;
  не перечитывай settings только ради получения того же текста. После паузы или
  при изменении другим чатом прочитай актуальное состояние.
- Диагностика: `current.pending_prompt` с rubric, `selection_guidance` с последним
  ответом, помощью, обоснованием, использованными вопросами и бюджетом, вся summary
  с критериями, противоречиями, ревизиями доказательств и ограничениями. Старые
  attempts и повтор answers/skips исключены из обычного ответа. Их данные не удалены.
- Урок: источники и их контекст, критерии, adaptive, практика с полной историей
  помощи и последней попыткой, выбранный lesson_context/state/checkpoint. Остальные
  занятия представлены в `learning.lesson_choices`, все продолжения сохранены.
  `learning_progress` находится на верхнем уровне, не дублируется внутри intent.
  Никакой «понятно» или completed не становится доказательством освоения.

## Когда нужны подробности

`evidence` содержит исходные оценки текущих критериев из уже загруженного журнала,
в том числе старше последних 100 событий. При непустом `details_required.event_revisions` запроси эти события
**до объяснения прежней ошибки или вывода о самостоятельности**, не угадывай
содержание по краткому статусу. Сверяй unit_sha256, критерий и границы оценки.
Если summary диагностики или последний ответ недостаточны для содержательного
решения, запроси исходный ответ по answer_revision/prompt_revision. Не показывай
ученику rubric/готовое решение до самостоятельной попытки.

```sh
PYTHON PLUGIN_ROOT/scripts/course.py learn-unit --course COURSE --unit-id UNIT --compact --event-revision REV
PYTHON PLUGIN_ROOT/scripts/course.py diagnostic --course COURSE --profile USER --compact --event-revision REV
```

`--event-revision` можно повторить до 100 раз. Это read-only запрос из полностью
проверенного существующего журнала; чужая unit или отсутствующая ревизия отклоняется.
Не совмещай запрос доказательств/старой страницы с записью ответа.

Обычный полный ответ: повтори команду без `--compact`. `--history-limit 1..100`
возвращает последнюю страницу. Для старой истории используй исключающую верхнюю
границу `--history-before`; в ответе `history_page.next_before_revision` — граница
следующей страницы или null в конце. Для первой страницы можно указать текущую
ревизию журнала + 1. В learn-unit история относится к выбранной unit.

```sh
PYTHON PLUGIN_ROOT/scripts/course.py learn-unit --course COURSE --unit-id UNIT --compact --history-limit 50 --history-before REV_PLUS_ONE
PYTHON PLUGIN_ROOT/scripts/course.py diagnostic --course COURSE --profile USER --compact --history-limit 50 --history-before REV_PLUS_ONE
```

Компактная выдача не меняет правила CAS, ревизии и обязательные согласия.
Используй возвращённые revision и unit.content_sha256; после конфликта прочитай
заново. valid=false, stale, needs_review, причины и варианты продолжения нельзя
игнорировать. Полный текст нужного материала, критериев и помощи не обрезается
ради размера: сложная тема может иметь большой даже компактный ответ.
