# Проверки версии 0.4.32

Локальная среда: macOS arm64, Python3.12.

- 752 automated tests / 21.167s / OK.
- 10 skill definitions validated; relative skill references exist.
- 152 installed plugin files matched source, excluding pycache/pyc.
- 4 installed CLI checks: old pilot status/export, new pilot start/export.
- Old pilot journal preserved byte-for-byte by read/export.
- Legacy connection read/update, new-name priority, old QA schema covered by tests.

Это техническая проверка разработчика. Windows, живая установка у друга,
его лекция, качество объяснения для него и удаление ещё не проверены.
Синтетические QA-события не являются ответами/отзывом пользователя.
Автоматическая диагностика не доказывает успешное распознавание речи.

Для внешнего пилота использовать $test-learning и отдельный курс.
При сбое до запуска заполнить TEST_REPORT_TEMPLATE.md вручную.
