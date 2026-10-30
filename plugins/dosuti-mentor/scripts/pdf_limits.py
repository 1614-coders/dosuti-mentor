"""Apply limits before loading the PDF parser; fail closed if unavailable."""
import os

MEMORY_BYTES=768*1024*1024
CPU_SECONDS=20
MAX_FLATE_STREAM=16*1024*1024
MAX_FLATE_TOTAL=64*1024*1024


def limit_streams():
    """Worker-only adapter: bound Flate expansion before pdfminer allocates decoded streams.

    Invalid Flate streams fail closed, without pdfminer's unbounded recovery decoder.
    Other filters remain subject to the process memory/CPU/wall limits.
    """
    import zlib
    from types import SimpleNamespace
    from pdfminer import pdftypes
    remaining=MAX_FLATE_TOTAL
    def decompress(data):
        nonlocal remaining
        budget=min(MAX_FLATE_STREAM,remaining)
        try:
            decoder=zlib.decompressobj()
            value=decoder.decompress(data,budget+1)
        except zlib.error as error:
            raise ValueError('Invalid PDF Flate stream') from error
        if len(value)>budget or decoder.unconsumed_tail:
            raise ValueError('PDF decoded stream budget exceeded')
        if not decoder.eof:raise ValueError('Incomplete PDF Flate stream')
        remaining-=len(value)
        return value
    pdftypes.zlib=SimpleNamespace(decompress=decompress,error=zlib.error)


def check_memory():
    import sys
    if sys.platform=='darwin':
        import resource
        if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss>MEMORY_BYTES:
            raise MemoryError('PDF worker exceeded sampled RSS budget')


def apply():
    if os.name!='nt':
        import resource
        import sys
        if sys.platform=='darwin':
            # Darwin does not provide a reliable hard AS/DATA cap; sampled RSS is explicit.
            import threading
            import time
            def guard():
                while True:
                    try:check_memory()
                    except Exception:os._exit(2)
                    time.sleep(.05)
            threading.Thread(target=guard,daemon=True).start()
        else:resource.setrlimit(resource.RLIMIT_AS,(MEMORY_BYTES,MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU,(CPU_SECONDS,CPU_SECONDS))
        return None
    import ctypes as c
    from ctypes import wintypes as w
    class Basic(c.Structure):
        _fields_=[('process_time',c.c_int64),('job_time',c.c_int64),('flags',w.DWORD),
                  ('minimum',c.c_size_t),('maximum',c.c_size_t),('active',w.DWORD),
                  ('affinity',c.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
    class IO(c.Structure):
        _fields_=[(key,c.c_uint64) for key in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
    class Limits(c.Structure):
        _fields_=[('basic',Basic),('io',IO),('process_memory',c.c_size_t),('job_memory',c.c_size_t),
                  ('peak_process',c.c_size_t),('peak_job',c.c_size_t)]
    kernel=c.WinDLL('kernel32',use_last_error=True)
    kernel.CreateJobObjectW.argtypes=[c.c_void_p,w.LPCWSTR];kernel.CreateJobObjectW.restype=w.HANDLE
    kernel.SetInformationJobObject.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD]
    kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
    kernel.GetCurrentProcess.restype=w.HANDLE
    kernel.CloseHandle.argtypes=[w.HANDLE]
    job=kernel.CreateJobObjectW(None,None)
    if not job:raise c.WinError(c.get_last_error())
    value=Limits();value.basic.flags=0x100|0x2|0x2000
    value.basic.process_time=CPU_SECONDS*10_000_000;value.process_memory=MEMORY_BYTES
    if not kernel.SetInformationJobObject(job,9,c.byref(value),c.sizeof(value)):
        error=c.WinError(c.get_last_error());kernel.CloseHandle(job);raise error
    if not kernel.AssignProcessToJobObject(job,kernel.GetCurrentProcess()):
        error=c.WinError(c.get_last_error());kernel.CloseHandle(job);raise error
    return job  # Keep the handle alive until worker exit; only this worker owns the job.
