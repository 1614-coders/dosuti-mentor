# Windows 1.0.0 — experimental / limited support

Это не Windows Stable. Не используйте неподтверждённые операции над единственной
копией учебных данных. Сначала сохраните данные и заведите отдельный QA-курс.

## Контрольная база

База `4c3afda4034579ec470ea9078fac7d35a8fd551b`.
[Tests run 42473432548](https://github.com/1614-coders/dosuti-mentor/actions/runs/42473432548):
Linux job — success; Windows job — failure. PS7 и PS5.1: **7 failures, 41 errors,
85 skipped на профиль** (911 PASS по исходной сводке). Это не результат patch 1.0.0.
Первичная установка с гонками отдельно PASS; общий красный статус не отменён.

## Открытые группы

| Группа | Ограничение / следующий шаг на Windows |
|---|---|
| H01: HomeSession HANDLE lifecycle | Возможны потеря ownership и stderr-only secondary close error при fault cleanup. Отдельный patch не включён в 1.0.0; нужны аудит и native faults, без blind retry NtClose |
| WorkerIO | POSIX-only примитивы не обеспечивают весь Windows media pipeline. Отказ платформы нельзя обходить pathname fallback |
| Job Publication | Подготовленный job snapshot пока нельзя считать полноценно публикуемым Windows-сценарием; девять PublicationValidation падают на раннем guard. Черновой backend BLOCKED, не включён |
| Extraction/prepared | UTF-8 extraction имеет отдельный backend; PDF extraction, remote HTML/HTTP, prepared/tree import и связанные пути не заявлены как принятые Windows-возможности |
| Concurrency/media assertions | Оставшиеся реальные операции требуют разборов на нативной машине; отказы нельзя объявлять безопасностью лишь по числу тестов |

H01 — подтверждённый reliability blocker для Windows Stable, не доказательство
перезаписи чужого файла в обычном успешном сценарии. При неизвестном исходе
сохраняйте данные/остатки и остановите зависимую операцию. Пакет не обещает исправленного
cleanup: номер 1.0.0 и статус experimental не устраняют дефект.

## Что уже проходило нативные проверки

До release patch отдельные наборы подтверждали: SingleFile local text/code — 13/13,
ResourceText UTF-8 extraction — 11/11, PDF raw intake — 13/13,
Homework local UTF-8 — 13/13, Registry Update — 28/28,
installation races — 6/6 на PS7 и PS5.1. Это компонентное покрытие прежней базы,
не доказательство всех end-to-end сценариев или новой зависимости Pillow.
Пропущенные security tests не засчитываются как PASS.

## Установка на отдельном Windows-компьютере после публикации

1. Используйте Windows x64 с локальным NTFS, Codex и сетью. Создайте копию данных;
   не переносите venv с macOS/Linux. Системные политики и права не отключайте.
2. Подключите Git marketplace `1614-coders/dosuti-mentor`, ref main, установите
   `dosuti-mentor@dosuti-mentor-local`, проверьте фактическую версию.
3. Запустите setup-dosuti для локального runtime. Успешный installer не подтверждает
   WorkerIO/publication/extraction. При ошибке сохраните stderr и результат проверки.
4. В новом чате проверьте знакомство, выбор профиля/курса и продолжение QA-курса;
   материал проверяйте только в принятых локальных сценариях и с копиями файлов.
5. В PS7 и PS5.1 отдельно выполните штатный windows_test_suite и installation races,
   сохраните полные stdout/stderr, имена failures/errors и NTFS/архитектуру. Исправления
   делайте отдельными пакетами, не отключая suite и не ослабляя assertions.

[Полная приёмка установки](INSTALLATION_ACCEPTANCE.md) · [Настройка Windows runtime](plugins/dosuti-mentor/references/installation.md#локальные-компоненты).

## Семь failures в каждом профиле контрольного CI

Имена сохранены для сравнения причин; assertions не изменяются:

- `test_concurrent_replacement_rejected (test_material_sources.Materials.test_concurrent_replacement_rejected)`
- `test_snapshot_limit_refuses_without_modifying_progress (test_processing_job_safety.WorkerRefusal.test_snapshot_limit_refuses_without_modifying_progress)`
- `test_extraction_output_limit_preserves_existing_files_and_registry (test_html_limits.HTMLLimits.test_extraction_output_limit_preserves_existing_files_and_registry)`
- `test_concurrent_extraction_keeps_one_registered_text (test_pdf_import.PDFImport.test_concurrent_extraction_keeps_one_registered_text)`
- `test_pdf_cannot_be_smuggled_as_remote_code (test_pdf_import.PDFImport.test_pdf_cannot_be_smuggled_as_remote_code)`
- `test_audio_only_preparation_has_no_fictitious_frames (test_media_quality.MediaSamples.test_audio_only_preparation_has_no_fictitious_frames)`
- `test_parallel_render_publishes_once_and_cleans_loser (test_visual_materials.PDFVisuals.test_parallel_render_publishes_once_and_cleans_loser)`
