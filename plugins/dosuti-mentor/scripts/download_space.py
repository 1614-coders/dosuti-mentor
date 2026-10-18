"""Pre-transfer disk lower bound, with explicit unknown-size diagnostics."""
import shutil
from resource_preflight import RESERVE, MIB


def ensure(folder, size=None, *, merge=False):
    if type(size) is not int or size <= 0: size = None
    try: free = shutil.disk_usage(folder).free
    except OSError: free = None
    required = None if size is None else size * (2 if merge else 1) + RESERVE
    warnings = []
    if size is None: warnings.append('Размер скачивания неизвестен; места для всего видео гарантировать нельзя.')
    if free is None: warnings.append('Не удалось проверить свободное место перед скачиванием.')
    data = dict(estimated_download_bytes=size, disk_free_bytes=free, minimum_free_bytes=required,
                merge_copy_reserved=merge, warnings=warnings,
                scope='transfer_and_headroom_only_not_total_preparation')
    from preparation_status import phase
    phase('download', download_space=data)
    for warning in warnings: print(warning, flush=True)
    if free is not None and free < (required if required is not None else RESERVE):
        raise ValueError(f'Недостаточно места перед скачиванием: свободно {free // MIB} МиБ; '
                         f'нужно минимум {(required if required is not None else RESERVE) // MIB} МиБ. '
                         'Освободи место и повтори; аудио и кадрам потребуется дополнительное место.')
    return data
