"""Cheap resource advice; disk lower bound is not a total job-size estimate."""
import math
import shutil

MIB = 1024 ** 2
RESERVE = 64 * MIB  # Operational headroom, not an estimate of frame storage.
LOW_RAM = 2 * 1024 ** 3  # Advice threshold, not a model's RAM requirement.


def available_memory():
    from resource_usage import WindowsProbe
    import ctypes
    try:
        probe = WindowsProbe()
        memory = probe.SystemMemory()
        memory.size = ctypes.sizeof(memory)
        if probe.k.GlobalMemoryStatusEx(ctypes.byref(memory)):
            return int(memory.available)
    except (OSError, AttributeError):
        pass
    return None


def inspect(folder, duration, *, audio_needed, recognition_needed, pending):
    required = math.ceil(duration * 32000) + 44 if audio_needed else 0
    data = dict(schema='resource_preflight_v1', new_audio_bytes=required,
                reserve_bytes=RESERVE if pending else 0,
                scope='new_pcm_and_headroom_only', warnings=[], blocked=False)
    try:
        data['disk_free_bytes'] = shutil.disk_usage(folder).free
        data['blocked'] = pending and data['disk_free_bytes'] < required + RESERVE
    except OSError:
        data['disk_free_bytes'] = None
        data['warnings'].append('Не удалось проверить свободное место на диске.')
    data['available_ram_bytes'] = available_memory() if recognition_needed else None
    if recognition_needed and data['available_ram_bytes'] is not None and data['available_ram_bytes'] < LOW_RAM:
        data['warnings'].append('Доступно менее 2 ГиБ ОЗУ. Закрой лишние приложения или обрабатывай лекцию фрагментами. Качество распознавания не изменено.')
    return data


def enforce(data):
    for message in data['warnings']:
        print(message, flush=True)
    if data['blocked']:
        raise ValueError(f"Недостаточно места: свободно {data['disk_free_bytes'] // MIB} МиБ; "
                         f"нужно минимум {(data['new_audio_bytes'] + data['reserve_bytes'] + MIB - 1) // MIB} МиБ "
                         'для нового аудио и рабочего резерва. Кадры потребуют дополнительное место. '
                         'Освободи место или выбери меньший фрагмент; готовые этапы сохранены.')
