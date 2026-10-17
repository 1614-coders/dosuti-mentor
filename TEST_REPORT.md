# Проверки версии 0.4.43

- macOS arm64 / Python 3.12: 904 теста, 25.858 s, OK. 15 новых регрессий.
- Исправления проверены на временных QA-папках: 501 событие SQLite и полная
  сегментированная история сохраняются в отчёте; недоступный сегмент/повреждение/
  превышение бюджета запрещают очистку. Старый план без coverage отклоняется.
- Pending очистки setup/run блокирует новые записи. Отдельный ожидающий процесс
  повторно проверяет marker после получения lock; два apply сериализованы.
- Живой автоматический timer нельзя закрыть восстановлением. Реальный дочерний
  процесс убит, его запись восстановлена через CLI как interrupted с неизвестным
  временем; legacy требует явного признания неизвестного владельца.
- Ошибки identity содержат конкретный файл и изменения inode/size/mtime/ctime.
  Подмена файла обнаруживается. Проверка целостности не ослаблялась.
- Два новых процесса читают независимые unit-checkpoints своих session без
  записи оценок. Связи QA и обычных попыток проверяются read-only, с отказом
  для другого профиля/ответа/версии источника и с сохранением старых журналов.
- Реальный локальный 12-секундный фрагмент обработан на CPU; повтор использовал
  checkpoint без нового ASR. Отдельная offline-подготовка окружения прошла.
  Историческое разовое Source changed during hashing в этом прогоне не повторилось;
  его первопричина остаётся неизвестной. Теперь диагностика содержит контекст.

Реальные ответы ученика и успех Windows-прогона не создавались. Плагин, история
чатов и приложение Codex не удалялись. Windows/PowerShell, обучение друга и ручное
удаление плагина/чатов проверяются на его машине. Частичный экспорт остаётся
доступен для диагностики, но не разрешает удаление данных.

---

# Проверки версии 0.4.42

- macOS arm64 / Python 3.12: 889 тестов, 25.058 s, OK. Из них 24 новых
  сценария времени, источников, ответов, кэша, конкурирующих записей и очистки.
- Реальный локальный 12-секундный фрагмент: CPU ASR + аудио + кадры + пакет,
  5 отдельных замеров; повтор использовал checkpoint без повторного ASR.
- Реальная offline-подготовка runtime: две попытки сохранены в одном отчёте.
  Первая остановилась на проверке изменения файла во время хеширования;
  повтор прошёл. Причина первого срабатывания не установлена, проверка целостности
  не отключалась. Ошибка и числовые длительности сохранены.
- Скачать файл с локального HTTP-сервера и повторно использовать кэш — проверено.
- Очистка проверена только на собственных временных фикстурах: изменение файлов,
  занятый lock, открытый таймер, внешняя ссылка и прерывание не удаляют чужие данные.
- Реальные ответы ученика не создавались. Подмена тестов усвоением отсутствует.

Windows, живое обучение друга, удаление плагина/истории средствами его Codex и
качество распознавания полной лекции этим прогоном не подтверждены.
Ручные таймеры включают паузы между командами. Записанная оценка ответа не является
автоматическим доказательством освоения. Старые курсы сохраняют прежние журналы.

---

# Проверки версии0.4.41

macOS arm64/Python3.12:865автоматических тестов /24.353s /OK,2новых регрессии.
174установленных файла сверены,11skills валидны,8installedCLI-проверок.

Живой ограниченный журнал доступен до завершения команды; ошибка его записи
останавливает управляемый дочерний процесс. Маскирование секретов частичное.

Новая отдельная среда без system site-packages: реальные pip install, загрузка
модели tiny с проверкой37частей и полного SHA256, FFmpeg/CPU int8 и20sобработка.
2ASR/2frames. Первая попытка намеренно остановлена тестировщиком во время медленной
загрузки, это не ошибка совместимости pip. История сохранена; повтор online прошёл.
Затем offline-повтор в новой среде прошёл без установки/загрузки модели. Исходное
видео неизменно; новый процесс находит QA-курс через проект. Ответы/оценки ученика
не созданы, resume в настоящем новом чате остаётся not_checked.

Версии новой среды: faster-whisper1.2.1,ctranslate2 4.8.3,av19.0.1,Pillow12.3.0,
imageio-ffmpeg0.6.0,onnxruntime1.31.0,yt-dlp2026.8.19,numpy2.5.4.
Это результат macOS, не подтверждение чистой установки Windows/PowerShell.
Качество распознавания и преподавания требует внешней проверки.

## Предыдущие проверки

# Проверки версии0.4.40

macOS arm64/Python3.12:863автоматических теста /22.651s /OK,22новых регрессии.
174установленных файла сверены,11skills валидны,8installedCLI-проверок.

Реальная CPU/offline-подготовка: ошибка до курса →2успешных обработки20s tiny,
2ASR/2frames на прогон; источник неизменён, история ошибки/курс сохранены.
Свежий процесс находит QA-курс по выбранному проекту без --pilot. Отключённые
чаты и чужие привязки не заменяются; параллельные владельцы и сбои проверены.
Нет выдуманных ответов/оценок: настоящие onboarding/teaching/resume not_checked.

Windows/PowerShell ещё не выполнялись на целевой системе. Online install/download
проверены оркестрационными тестами; реальный новый сценарий использовал готовую
среду/offline. Качество распознавания и обучения требуют внешней проверки.

## Предыдущие проверки

# Проверки версии 0.4.39

Локальная среда: macOS arm64, Python3.12.

- 841 automated tests; 15 regressions for compact full-lecture QA findings.
- Full6501.74s lecture on an isolated copy: 38windows,2444ASR,651frames preserved.
- Source lesson unchanged;0learner attempts created;2session checkpoints/journal preserved.
- Reviewed verbal homework works after v1 compaction without rewriting sealed lesson files.
- Narrow windows withhold unscoped topic prose and late code; full-topic access preserved.
- Structured v2 review scope; v1 explicitly unknown; no invented audio verification.
- Homework origin/text/revision corruption, stale grades, CAS, priority, relocation,
  dependencies/unit provenance and changed compact plans covered.
- 11skills validated;171installed plugin files match source;6fresh installed CLI checks.

Это техническая самопроверка. Качество всего ASR/каждого символа кода и личная
пригодность объяснений не доказаны. Семантическая выборка:23/651кадра (6крупно),
весь сохранённый ASR/текущий код; независимой аудиопроверки нет.
Windows и независимый внешний пилот остаются открытыми.

## История предыдущих проверок

### Версия 0.4.38

Локальная среда: macOS arm64, Python3.12.

- 826 automated tests / 21.953s / OK.
- 11 skill definitions validated; relative skill references exist.
- 168 installed plugin files matched source; 5 compact/context CLI checks passed on isolated real-media copy.
- Installed cleanup plan/apply/plan on isolated synthetic cache: old build removed, current build and lock inode preserved.
- Actual host plan: absent, no real-host deletion.
- Prior0.4.34 reader checks: old-writer inline/segmented documents match logical content, no writes.
- Document-read benchmark: 3 cases, 3 fresh-process runs per implementation; Python peak allocation down26–41%, not RSS; full document still restored.
- Prior0.4.33 check —4real OS processes: 1 synthetic OCR call, 4 identical results; this does not measure recognition accuracy.
- Invalid observations rejected before publication; failure retry, changed engine/source, lock refusal tested.
- Old pilot journal preserved byte-for-byte by read/export.
- Legacy connection read/update, new-name priority, old QA schema covered by tests.

Это техническая проверка разработчика. Windows, живая установка у друга,
его лекция, качество объяснения для него и удаление ещё не проверены.
Синтетические QA-события не являются ответами/отзывом пользователя.
Автоматическая диагностика не доказывает успешное распознавание речи.

Для внешнего пилота использовать $test-learning и отдельный курс.
При сбое до запуска заполнить TEST_REPORT_TEMPLATE.md вручную.

0.4.35:14new cleanup tests; active/current/changed/unknown/linked entries preserved,
interrupted pruning resumed, Windows unsupported without host mutation.
Real OS-process lock acceptance used synthetic executable bytes, never executed.

0.4.36:11new document-report tests; segmented/inline compatibility, orphan candidates,
unknown/linked entries, integrity failures, scan/sample limits and observed changes.
Report supports no deletion and makes no logical teaching/content correctness claim.

0.4.37:14new registry-projection/parser tests; all historical semantic hashes and
current bytes verified, global revision/DAG retained, sessions/answers unchanged.
Two synthetic registries2.96/8.80MB,3fresh-process runs per mode: Python peak
allocation down31–45%, time about6% slower. Not RSS, not a speedup claim.
Installed QA: two session checkpoints, source damage invalidation, historical
corruption refusal. Every CLI read preserved fixture bytes. Real courses untouched.


0.4.38:20compact-mode tests; retained evidence/text/code/progress, managed-download
and audio deletion, external-original protection, interrupted resume, changed assets,
active locks, cross-lesson references, relocation, video-unit attempt/checkpoint.
Real-media smoke:24.16s imported evidence, actual24.123s clip,3frames/2ASR segments,
local ffmpeg extraction.5fresh installed CLI commands, no ASR rerun or network.
Deleted2286310logical bytes only in a disposable QA copy; original files unchanged.
QA review flags do not establish semantic completeness or teaching quality.
