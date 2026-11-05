"""Pinned screen publication and anonymous HTTP preflight; controlled peer races."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import signal
import selectors
import sys
import tempfile
import unittest
from unittest.mock import patch
SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
import screen_evidence as screens
import video_sources as videos
import http_resume
from source_integrity import file_hash


class RaceFixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.source=self.root/'input.mp4'
        self.source.write_bytes(b'isolated decoder input')
        self.foreign=self.root/'foreign';(self.foreign/'.git').mkdir(parents=True)
        for name,value in [('screen.json',b'FOREIGN SCREEN'),('completion.json',b'FOREIGN COMPLETION'),('.git/keep',b'FOREIGN GIT')]:
            (self.foreign/name).write_bytes(value)
        self.replacement=None

    def swap(self,path,symlink=False):
        self.replacement=Path(path)
        code='from pathlib import Path;import sys;p=Path(sys.argv[1]);f=Path(sys.argv[2]);p.rename(p.with_name(p.name+"-owned"));'
        code+='p.symlink_to(f,target_is_directory=True)' if symlink else 'f.rename(p)'
        subprocess.run([sys.executable,'-B','-c',code,str(path),str(self.foreign)],check=True,capture_output=True,timeout=10)

    def preserved(self):
        self.assertIsNotNone(self.replacement)
        for name,value in [('screen.json',b'FOREIGN SCREEN'),('completion.json',b'FOREIGN COMPLETION'),('.git/keep',b'FOREIGN GIT')]:
            self.assertEqual((self.replacement/name).read_bytes(),value)

    def frames(self,source,folder,*args,**kwargs):
        from PIL import Image
        # Fake decoding still uses the real pinned output writer when available.
        storage=kwargs.get('storage')
        image=Image.new('RGB',(32,32),'white')
        if storage:
            with storage.output(folder,'00001.jpg') as stream:image.save(stream,format='JPEG')
        else:
            folder.mkdir();image.save(folder/'00001.jpg')
        return [dict(file='00001.jpg',approx_seconds=0.0)]

    def scan(self):return screens.scan(self.source,self.root/'screens',0,2,.5,file_hash(self.source))


@unittest.skipUnless(os.name=='posix', 'Safe screen writes require native POSIX primitives')
class ScreenPublicationSafety(RaceFixture):
    def test_replaced_staging_cannot_overwrite_foreign_json(self):
        def overviews(folder,rows,**kwargs):self.swap(folder);return []
        error=None
        with patch.object(screens,'extract_frames',self.frames),patch.object(screens,'build_overviews',overviews),contextlib.redirect_stderr(io.StringIO()):
            try:
                returned=self.scan()
                if not self.replacement.exists():self.replacement=returned
            except (OSError,ValueError) as caught:error=caught
        self.preserved()
        self.assertIsNotNone(error,'A replaced scan must not be reported as published')

    def fail_overviews_after_swap(self,symlink=False,parent=False):
        original=screens.build_overviews
        def changed(folder,rows,**kwargs):
            self.swap(folder.parent if parent else folder,symlink)
            return original(folder,rows,**kwargs)
        with patch.object(screens,'extract_frames',self.frames),patch.object(screens,'build_overviews',changed),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises((OSError,ValueError)):self.scan()
        self.preserved()

    def test_symlink_at_overview_write_is_refused(self):self.fail_overviews_after_swap(symlink=True)

    def test_parent_replaced_at_overview_write_is_refused(self):self.fail_overviews_after_swap(parent=True)

    def test_swap_before_completion_preserves_foreign_json(self):
        from screen_storage import ScreenStorage
        original=ScreenStorage.metadata
        def metadata(storage,folder,name,value):
            if name=='completion.json':self.swap(folder)
            return original(storage,folder,name,value)
        with patch.object(screens,'extract_frames',self.frames),patch.object(ScreenStorage,'metadata',metadata),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises((OSError,ValueError)):self.scan()
        self.preserved()

    def test_leaf_replaced_after_exclusive_open_never_overwrites_foreign_inode(self):
        import safe_zip_import
        original=safe_zip_import.new_file;replaced=[];leaf=[]
        def opened(parent,name):
            fd=original(parent,name)
            if name=='screen.json' and not replaced:
                target=next((self.root/'screens').glob('scan-*'))/'screen.json'
                code='from pathlib import Path;import sys;p=Path(sys.argv[1]);p.rename(p.with_name("owned.json"));Path(sys.argv[2]).rename(p)'
                subprocess.run([sys.executable,'-B','-c',code,str(target),str(self.foreign/'screen.json')],check=True,capture_output=True,timeout=10)
                replaced.append(True);leaf.append(target)
            return fd
        with patch.object(screens,'extract_frames',self.frames),patch.object(safe_zip_import,'new_file',opened),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises((OSError,ValueError)):self.scan()
        self.assertEqual(len(replaced),1)
        self.assertEqual(leaf[0].read_bytes(),b'FOREIGN SCREEN')
        self.assertEqual((self.foreign/'.git/keep').read_bytes(),b'FOREIGN GIT')
        self.assertFalse((leaf[0].parent/'completion.json').exists())

    def test_existing_foreign_target_is_not_repaired_or_overwritten(self):
        _,key=screens.scan_settings(file_hash(self.source),0,2,.5)
        (self.root/'screens').mkdir();self.replacement=self.root/'screens'/('scan-'+key[:24]);self.foreign.rename(self.replacement)
        with patch.object(screens,'extract_frames',self.frames),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(ValueError):self.scan()
        self.preserved()

    def test_nested_preferred_incomplete_scan_retries_in_its_pinned_parent(self):
        nested=self.root/'screens'/'nested';incomplete=nested/'previous';incomplete.mkdir(parents=True)
        with patch.object(screens,'extract_frames',self.frames),contextlib.redirect_stderr(io.StringIO()):
            result=screens.scan(self.source,self.root/'screens',0,2,.5,file_hash(self.source),preferred=incomplete)
        self.assertEqual(result.parent,nested)
        self.assertEqual(list(incomplete.iterdir()),[])
        self.assertTrue((result/'completion.json').is_file())

    def test_preferred_output_root_is_refused_without_writing_a_sibling(self):
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaisesRegex(ValueError,'within its storage'):
            screens.scan(self.source,self.root/'screens',0,2,.5,file_hash(self.source),preferred=self.root/'screens')
        self.assertFalse(list(self.root.glob('screens-*')))

    def child_code(self):
        return f"""
from pathlib import Path
import sys
sys.path[:0]=[{str(SCRIPTS)!r},{str(Path(__file__).parent)!r}]
import screen_evidence as screens
from screen_storage import ScreenStorage
from source_integrity import file_hash
from PIL import Image
root=Path(sys.argv[1]);mode=sys.argv[2]
def frames(source,folder,*a,**kw):
 with kw['storage'].output(folder,'00001.jpg') as stream:Image.new('RGB',(32,32),'white').save(stream,format='JPEG')
 return [dict(file='00001.jpg',approx_seconds=0.0)]
screens.extract_frames=frames
original=ScreenStorage.metadata
def metadata(storage,folder,name,value):
 if mode=='pause' and name=='completion.json':
  print('READY '+str(folder),flush=True);sys.stdin.read(1)
 return original(storage,folder,name,value)
ScreenStorage.metadata=metadata
print('START',flush=True)
folder=screens.scan(root/'input.mp4',root/'screens',0,2,.5,file_hash(root/'input.mp4'))
print('DONE '+str(folder),flush=True)
"""

    def child(self,mode):
        process=subprocess.Popen([sys.executable,'-B','-c',self.child_code(),str(self.root),mode],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
        def finish():
            if process.poll() is None:process.kill()
            process.communicate(timeout=10)
        self.addCleanup(finish)
        return process

    def line(self,process):
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout,selectors.EVENT_READ)
            self.assertTrue(ready.select(timeout=20),'Child must reach controlled publication barrier')
        return process.stdout.readline().decode().strip()

    def test_sigkill_during_publication_retains_incomplete_then_retry_succeeds(self):
        child=self.child('pause');self.assertEqual(self.line(child),'START');paused=self.line(child)
        self.assertTrue(paused.startswith('READY '));incomplete=Path(paused[6:])
        child.kill();child.communicate(timeout=10);self.assertEqual(child.returncode,-signal.SIGKILL)
        self.assertTrue((incomplete/'screen.json').exists());self.assertFalse((incomplete/'completion.json').exists())
        with patch.object(screens,'extract_frames',self.frames),contextlib.redirect_stderr(io.StringIO()):
            result=self.scan()
        self.assertNotEqual(result,incomplete)
        self.assertTrue((result/'completion.json').exists());self.assertTrue((incomplete/'screen.json').exists())
        with patch.object(screens,'extract_frames',side_effect=AssertionError('Verified result must be reused')):
            self.assertEqual(screens.scan(self.source,self.root/'screens',0,2,.5,file_hash(self.source),preferred=result),result)

    def test_two_independent_scans_share_one_committed_result(self):
        first=self.child('pause');self.assertEqual(self.line(first),'START');self.assertTrue(self.line(first).startswith('READY '))
        second=self.child('normal');self.assertEqual(self.line(second),'START')
        first.stdin.write(b'x');first.stdin.flush()
        out1,err1=first.communicate(timeout=20);out2,err2=second.communicate(timeout=20)
        self.assertEqual(first.returncode,0,err1);self.assertEqual(second.returncode,0,err2)
        self.assertEqual(out1.strip(),out2.strip());self.assertTrue(out1.startswith(b'DONE '))
        self.assertEqual(len(list((self.root/'screens').glob('scan-*/completion.json'))),1)


class HTTPPreflightSafety(RaceFixture):
    def test_replaced_http_temporary_cannot_delete_foreign_git(self):
        def request(command,**kwargs):
            if '--head' in command:
                name=command[command.index('--dump-header')+1]
                folder=Path(name).parent if name!='-' else next((self.root/'video').glob('.fetch-*'))
                self.swap(folder)
            raise OSError('controlled HTTP interruption before network')
        with patch.object(http_resume,'preparation_run',request),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(OSError,'controlled HTTP interruption'):
                videos.acquire(self.root,dict(source='https://example.invalid/input.mp4'))
        self.preserved()

    def valid_head(self,command,**kwargs):
        self.assertEqual(command[command.index('--dump-header')+1],'-')
        self.assertEqual(command[command.index('--output')+1],os.devnull)
        kwargs['stdout'].write(b'HTTP/1.1 200 OK\r\nContent-Length: 42\r\n\r\n')
        kwargs['stdout'].flush()
        return subprocess.CompletedProcess(command,0)

    def head_race(self,symlink=False,parent=False):
        folder=self.root/'outer'/'http';folder.mkdir(parents=True)
        def request(command,**kwargs):
            self.swap(folder.parent if parent else folder,symlink)
            return self.valid_head(command,**kwargs)
        with patch.object(http_resume,'preparation_run',request),patch('download_space.ensure') as ensure:
            http_resume.preflight('https://example.invalid/input.mp4',folder)
        ensure.assert_called_once_with(folder,42)
        self.preserved()

    def test_head_directory_replaced_while_curl_writes_stays_read_only(self):self.head_race()

    @unittest.skipUnless(os.name=='posix','Native POSIX symlink substitution')
    def test_head_symlink_replacement_stays_read_only(self):self.head_race(symlink=True)

    def test_head_parent_replacement_stays_read_only(self):self.head_race(parent=True)

    def test_timeout_keeps_unknown_size_and_closes_anonymous_file(self):
        streams=[]
        def request(command,**kwargs):
            streams.append(kwargs['stdout']);raise subprocess.TimeoutExpired(command,25)
        with patch.object(http_resume,'preparation_run',request),patch('download_space.ensure') as ensure:
            http_resume.preflight('https://example.invalid/input.mp4',self.root)
        self.assertTrue(streams[0].closed);ensure.assert_called_once_with(self.root,None)

    def test_interrupt_closes_stream_without_touching_sources(self):
        streams=[]
        def request(command,**kwargs):
            streams.append(kwargs['stdout']);raise KeyboardInterrupt()
        with patch.object(http_resume,'preparation_run',request):
            with self.assertRaises(KeyboardInterrupt):http_resume.preflight('https://example.invalid/input.mp4',self.root)
        self.assertTrue(streams[0].closed)
        self.assertEqual(self.source.read_bytes(),b'isolated decoder input')
        self.assertEqual((self.foreign/'.git/keep').read_bytes(),b'FOREIGN GIT')

    def test_large_headers_do_not_supply_an_invented_size(self):
        def request(command,**kwargs):
            kwargs['stdout'].write(b'x'*(1024*1024+1));kwargs['stdout'].flush()
            return subprocess.CompletedProcess(command,0)
        with patch.object(http_resume,'preparation_run',request),patch('download_space.ensure') as ensure:
            http_resume.preflight('https://example.invalid/input.mp4',self.root)
        ensure.assert_called_once_with(self.root,None)

    def test_real_curl_head_uses_anonymous_output_and_leaves_no_files(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(handler):
                handler.send_response(200);handler.send_header('Content-Length','42');handler.end_headers()
            def log_message(*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        before=sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*'))
        try:
            with patch('download_space.ensure') as ensure:
                http_resume.preflight('http://127.0.0.1:'+str(server.server_port)+'/video.mp4',self.root)
            ensure.assert_called_once_with(self.root,42)
        finally:server.shutdown();server.server_close();thread.join(timeout=10)
        self.assertEqual(sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*')),before)

    def head_child(self,mode):
        code=f"""
import sys
from pathlib import Path
sys.path.insert(0,{str(SCRIPTS)!r})
import http_resume
from types import SimpleNamespace
def request(command,**kw):
 if sys.argv[2]=='pause':
  kw['stdout'].write(b'HTTP partial');kw['stdout'].flush()
  print('READY',flush=True);sys.stdin.read(1)
 kw['stdout'].write(b'HTTP/1.1 200 OK\\r\\nContent-Length: 42\\r\\n\\r\\n');kw['stdout'].flush()
 return SimpleNamespace(returncode=0)
http_resume.preparation_run=request
http_resume.preflight('https://example.invalid/video.mp4',Path(sys.argv[1]))
print('DONE',flush=True)
"""
        child=subprocess.Popen([sys.executable,'-B','-c',code,str(self.root),mode],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
        def finish():
            if child.poll() is None:child.kill()
            child.communicate(timeout=10)
        self.addCleanup(finish)
        return child

    def test_two_independent_head_processes_leave_sources_untouched(self):
        before=sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*'))
        children=[self.head_child('normal') for _ in range(2)]
        for child in children:
            output,error=child.communicate(timeout=20)
            self.assertEqual(child.returncode,0,error);self.assertEqual(output.strip(),b'DONE')
        self.assertEqual(sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob('*')),before)
        self.assertEqual((self.foreign/'.git/keep').read_bytes(),b'FOREIGN GIT')

    @unittest.skipUnless(os.name=='posix','SIGKILL requires native POSIX')
    def test_sigkill_during_head_then_retry_never_runs_path_cleanup(self):
        child=self.head_child('pause')
        with selectors.DefaultSelector() as ready:
            ready.register(child.stdout,selectors.EVENT_READ);self.assertTrue(ready.select(timeout=20))
        self.assertEqual(child.stdout.readline().strip(),b'READY')
        child.kill();child.communicate(timeout=10);self.assertEqual(child.returncode,-signal.SIGKILL)
        with patch.object(http_resume,'preparation_run',self.valid_head),patch('download_space.ensure') as ensure:
            http_resume.preflight('https://example.invalid/input.mp4',self.root)
        ensure.assert_called_once_with(self.root,42)
        self.assertEqual((self.foreign/'.git/keep').read_bytes(),b'FOREIGN GIT')


class ScreenPlatformRefusal(unittest.TestCase):
    def test_missing_primitives_refuse_before_any_output_write(self):
        import safe_zip_import
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'uncreated'
            with patch.object(safe_zip_import,'supported',return_value=False):
                with self.assertRaisesRegex(ValueError,'no unsafe fallback'):
                    screens.scan(Path(temp)/'input',output,0,2,.5,'0'*64)
            self.assertFalse(output.exists())
