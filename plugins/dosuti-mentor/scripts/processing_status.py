"""Small per-attempt status; OS lock ownership distinguishes work from a killed worker."""
import json
import time
from pathlib import Path
from atomic_files import atomic_json
from storage_lock import file_lock

LABELS = {
    'verify': 'Проверяю источник и сохранённые этапы',
    'probe': 'Проверяю видеопоток',
    'audio': 'Извлекаю аудио',
    'frames': 'Извлекаю кадры',
    'model_load': 'Загружаю локальную модель',
    'asr': 'Распознаю речь',
    'packet': 'Собираю материал для проверки преподавателем в чате',
    'complete': 'Материал обработан; требуется проверка источников и кода',
    'prepared': 'Аудио и кадры готовы; речь ещё не распознана',
    'recognized': 'Речь распознана; сборка материала ещё не завершена',
}


class Reporter:
    def __init__(self, folder):
        self.path = Path(folder) / 'processing-status.json'
        self.started = time.monotonic()
        self.data = dict(schema='processing_status_v1', started_at=time.time())

    def set(self, stage, status='running', error=None):
        self.data.update(stage=stage, label=LABELS[stage], status=status,
                         elapsed_seconds=round(time.monotonic() - self.started, 2))
        if error is not None:
            self.data['error'] = type(error).__name__ + ': ' + str(error)[:300]
        atomic_json(self.path, self.data)
        print(self.data['label'] + f" ({self.data['elapsed_seconds']} с)", flush=True)


def describe(folder):
    """Read metadata only. Legacy jobs and missing/corrupt status stay unknown."""
    folder = Path(folder)
    path = folder / 'processing-status.json'
    lock = folder / '.processing.lock'
    def read(busy):
        data = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(data, dict) or data.get('schema') != 'processing_status_v1'
                or data.get('stage') not in LABELS
                or data.get('status') not in ('running', 'complete', 'failed', 'interrupted')):
            raise ValueError('Unknown processing status')
        if data['status'] == 'running':
            if busy:
                data['elapsed_seconds'] = round(max(0, time.time() - data['started_at']), 2)
            else:
                data['status'] = 'interrupted'
                data['label'] = 'Обработка остановлена; повторный запуск проверит и использует готовые этапы'
                data['elapsed_is_last_observed'] = True
        return data

    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            return dict(status='unavailable')
        if not lock.is_file() or lock.is_symlink():
            return dict(status='unavailable')
        try:
            with file_lock(lock, timeout=0, create_parent=False):
                return read(False)
        except TimeoutError:
            return read(True)
    except (OSError, ValueError, KeyError, TypeError):
        return dict(status='unavailable')
