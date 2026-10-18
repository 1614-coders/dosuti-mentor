"""Bounded local resource observations; no uploads, process names or learner data."""
import ctypes as c
from ctypes import wintypes as w
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import threading
import time


class WindowsProbe:
    """Win32 counters work before any optional Python packages are installed."""
    def __init__(self):
        if os.name != 'nt': raise OSError('Windows counters unavailable on this platform')
        self.k = c.WinDLL('kernel32', use_last_error=True)
        self.p = c.WinDLL('psapi', use_last_error=True)
        class Entry(c.Structure):
            _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                        ('heap', c.c_size_t), ('module', w.DWORD), ('threads', w.DWORD),
                        ('parent', w.DWORD), ('priority', w.LONG), ('flags', w.DWORD),
                        ('exe', w.WCHAR * 260)]
        class Memory(c.Structure):
            _fields_ = [('size', w.DWORD), ('faults', w.DWORD)] + [
                (name, c.c_size_t) for name in ('peak', 'rss', 'peak_pool', 'pool',
                'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_pagefile', 'private')]
        class SystemMemory(c.Structure):
            _fields_ = [('size', w.DWORD), ('load', w.DWORD)] + [
                (name, c.c_ulonglong) for name in ('total', 'available', 'page_total',
                'page_available', 'virtual_total', 'virtual_available', 'extended')]
        self.Entry, self.Memory, self.SystemMemory = Entry, Memory, SystemMemory
        for dll, name, args, result in [
            (self.k, 'CreateToolhelp32Snapshot', [w.DWORD,w.DWORD], w.HANDLE),
            (self.k, 'Process32FirstW', [w.HANDLE,c.POINTER(Entry)], w.BOOL),
            (self.k, 'Process32NextW', [w.HANDLE,c.POINTER(Entry)], w.BOOL),
            (self.k, 'OpenProcess', [w.DWORD,w.BOOL,w.DWORD], w.HANDLE),
            (self.k, 'CloseHandle', [w.HANDLE], w.BOOL),
            (self.k, 'GetProcessTimes', [w.HANDLE]+[c.POINTER(w.FILETIME)]*4, w.BOOL),
            (self.k, 'GetSystemTimes', [c.POINTER(w.FILETIME)]*3, w.BOOL),
            (self.k, 'GlobalMemoryStatusEx', [c.POINTER(SystemMemory)], w.BOOL),
            (self.p, 'GetProcessMemoryInfo', [w.HANDLE,c.POINTER(Memory),w.DWORD], w.BOOL)]:
            fn=getattr(dll,name);fn.argtypes=args;fn.restype=result

    @staticmethod
    def ticks(value): return (value.dwHighDateTime << 32) | value.dwLowDateTime

    def sample(self):
        handle=self.k.CreateToolhelp32Snapshot(2,0)
        if handle == c.c_void_p(-1).value: raise c.WinError(c.get_last_error())
        parents={}
        try:
            entry=self.Entry();entry.size=c.sizeof(entry)
            ok=self.k.Process32FirstW(handle,c.byref(entry))
            if not ok: raise c.WinError(c.get_last_error())
            while ok:
                parents[int(entry.pid)]=int(entry.parent)
                ok=self.k.Process32NextW(handle,c.byref(entry))
        finally: self.k.CloseHandle(handle)
        selected={os.getpid()}
        while True:
            extra={pid for pid,parent in parents.items() if parent in selected}
            if extra <= selected: break
            selected |= extra
        processes={};missing=0
        for pid in selected:
            handle=self.k.OpenProcess(0x410,False,pid)
            if not handle:
                missing+=1;continue
            try:
                created,exited,kernel,user=(w.FILETIME() for _ in range(4))
                mem=self.Memory();mem.size=c.sizeof(mem)
                if not (self.k.GetProcessTimes(handle,*map(c.byref,(created,exited,kernel,user)))
                        and self.p.GetProcessMemoryInfo(handle,c.byref(mem),c.sizeof(mem))):
                    missing+=1;continue
                processes[(pid,self.ticks(created))]=(int(mem.rss),
                    (self.ticks(kernel)+self.ticks(user))/10_000_000)
            finally: self.k.CloseHandle(handle)
        idle,kernel,user=(w.FILETIME() for _ in range(3))
        memory=self.SystemMemory();memory.size=c.sizeof(memory)
        if not self.k.GetSystemTimes(*map(c.byref,(idle,kernel,user))): raise c.WinError(c.get_last_error())
        if not self.k.GlobalMemoryStatusEx(c.byref(memory)): raise c.WinError(c.get_last_error())
        return dict(processes=processes,missing=missing,system_used=memory.total-memory.available,
                    system_total=memory.total,system_cpu=(self.ticks(idle),self.ticks(kernel)+self.ticks(user)))


def folder_size(root, limit=20000, budget=.25):
    """Logical bytes, bounded work; never follow links/reparse points."""
    total=count=skipped=0;deadline=time.monotonic()+budget;pending=[Path(root)]
    try:
        while pending:
            folder=pending.pop()
            info=folder.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
                skipped+=1;continue
            with os.scandir(folder) as entries:
                for entry in entries:
                    count+=1
                    if count>limit or time.monotonic()>deadline:
                        return dict(bytes=total,complete=False,reason='scan_budget',links_skipped=skipped)
                    info=entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400: skipped+=1
                    elif stat.S_ISDIR(info.st_mode): pending.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode): total+=info.st_size
        return dict(bytes=total,complete=True,links_skipped=skipped)
    except OSError:
        return dict(bytes=total,complete=False,reason='files_changed_or_unavailable',links_skipped=skipped)


class Monitor:
    def __init__(self, root, publish, interval=2, probe_factory=WindowsProbe):
        self.root=Path(root);self.publish=publish;self.interval=interval
        self.data=dict(schema='resource_usage_v1',state='running',interval_seconds=interval,
                       samples=0,errors=0,missing_process_samples=0,logical_cpus=os.cpu_count(),
                       scope='python_process_and_live_descendants',cpu_scope='observed_process_intervals')
        self.data['system_cpu_scope']='primary_processor_group' if (os.cpu_count() or 1)>64 else 'system'
        self.previous=None;self.cpu_seconds=0;self.cpu_wall=0;self.stop=threading.Event()
        try: self.probe=probe_factory()
        except (OSError,AttributeError): self.probe=None;self.data['platform_counters']='unavailable'
        self.data['folder_start']=folder_size(root)
        self.thread=None

    def emit(self):
        try: self.publish(dict(self.data))
        except Exception: self.data['errors']+=1  # Measurement must not fail the lesson.

    def peak(self,key,value):
        if value is not None: self.data[key]=max(value,self.data.get(key,value))

    def sample(self):
        try:
            disk=shutil.disk_usage(self.root);free=disk.free
            self.data['disk_total_bytes']=disk.total
            self.data.setdefault('disk_used_start_bytes',disk.used)
            self.data['disk_used_end_bytes']=disk.used
            self.data.setdefault('disk_free_start_bytes',free)
            self.data['disk_free_end_bytes']=free
            self.data['disk_free_min_bytes']=min(free,self.data.get('disk_free_min_bytes',free))
            if self.probe is None: return
            current=self.probe.sample();now=time.monotonic()
            self.data['samples']+=1
            self.data['missing_process_samples']+=current['missing']
            rss=sum(p[0] for p in current['processes'].values())
            if current['processes']:
                self.data.setdefault('rss_start_bytes',rss);self.data['rss_end_bytes']=rss
                self.peak('rss_sampled_peak_bytes',rss)
            self.data.setdefault('system_ram_start_bytes',current['system_used'])
            self.data['system_ram_end_bytes']=current['system_used']
            self.data['system_ram_total_bytes']=current['system_total']
            self.peak('system_ram_sampled_peak_bytes',current['system_used'])
            if self.previous:
                then,prior=self.previous;elapsed=now-then
                delta=sum(max(0,cpu-prior['processes'][key][1]) for key,(_,cpu) in current['processes'].items()
                          if key in prior['processes'])
                if elapsed>0 and current['processes'] and prior['processes']:
                    self.cpu_seconds+=delta;self.cpu_wall+=elapsed
                    self.data['cpu_average_cores']=self.cpu_seconds/self.cpu_wall
                    self.peak('cpu_sampled_peak_cores',delta/elapsed)
                idle,total=current['system_cpu'];old_idle,old_total=prior['system_cpu']
                if total>old_total:
                    self.peak('system_cpu_sampled_peak_percent',max(0,min(100,100*(1-(idle-old_idle)/(total-old_total)))))
            self.previous=now,current
        except Exception: self.data['errors']+=1

    def __enter__(self):
        self.sample();self.emit()
        def loop():
            last=time.monotonic()
            while not self.stop.wait(self.interval):
                self.sample()
                if time.monotonic()-last>=30:
                    self.emit();last=time.monotonic()
        self.thread=threading.Thread(target=loop,daemon=True,name='dosuti-resource-monitor');self.thread.start()
        return self

    def __exit__(self,kind,error,tb):
        self.stop.set();self.thread.join()
        self.sample();self.data['folder_end']=folder_size(self.root)
        self.data['state']='interrupted' if kind else 'finished'
        self.emit()


@contextmanager
def measure(root,publish):
    # Nested activity timers in this process share the outer monitor.
    # Child processes inherit this flag, so the parent counts them only once.
    key='DOSUTI_RESOURCE_MONITOR'
    if os.environ.get(key):
        yield;return
    from managed_storage import find_home
    home=find_home(root)
    storage_root=home if home is not None and Path(root).resolve().is_relative_to(home) else root
    os.environ[key]='1'
    try:
        with Monitor(storage_root,publish): yield
    finally: os.environ.pop(key,None)


def markdown(data):
    """Readable summary; full values remain in the journal export."""
    if not isinstance(data,dict): return ['Ресурсы: замеры не записаны.']
    def mib(key):
        value=data.get(key)
        return f'{value/1024**2:.1f} МиБ' if type(value) in (int,float) else 'нет замера'
    cpu=data.get('cpu_average_cores');cpu_text=f'{cpu:.2f} ядра' if type(cpu) in (int,float) else 'нет замера'
    peak=data.get('system_cpu_sampled_peak_percent');peak_text=f'{peak:.1f}%' if type(peak) in (int,float) else 'нет замера'
    lines=[f"Ресурсы: {data.get('state','unknown')}; выборок: {data.get('samples',0)}; ошибок: {data.get('errors',0)}; недоступных процессов в выборках: {data.get('missing_process_samples',0)}.",
           f"ОЗУ обработчика и дочерних процессов, наблюдавшийся пик: {mib('rss_sampled_peak_bytes')}.",
           f"ОЗУ всей системы: начало {mib('system_ram_start_bytes')}, пик {mib('system_ram_sampled_peak_bytes')}.",
           f"CPU обработчика, средняя занятость: {cpu_text}; пик CPU всей системы: {peak_text}.",
           f"Занято на всём диске: начало {mib('disk_used_start_bytes')}, конец {mib('disk_used_end_bytes')}.",
           f"Свободно на диске: начало {mib('disk_free_start_bytes')}, минимум {mib('disk_free_min_bytes')}, конец {mib('disk_free_end_bytes')}."]
    for label,key in [('начало','folder_start'),('конец','folder_end')]:
        size=data.get(key)
        if isinstance(size,dict): lines.append(f"Папка данных, {label}: {size.get('bytes',0)/1024**2:.1f} МиБ (логический размер; {'полный обход' if size.get('complete') else 'частичный обход'}).")
    if data.get('system_cpu_scope')=='primary_processor_group':
        lines.append('CPU системы ограничен основной группой процессоров Windows (>64 логических CPU).')
    return lines
