"""Real localhost curl, controlled peer substitution, no user data."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import selectors
import signal
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
import http_resume as h


@unittest.skipUnless(os.name=='posix','HTTP GET requires native POSIX FD transport; refusal is tested separately')
class GETSafety(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.stage=self.root/'outer'/'stage';self.stage.mkdir(parents=True);self.target=self.stage/'lecture.mp4'
        self.foreign=self.root/'foreign';(self.foreign/'.git').mkdir(parents=True)
        for name,value in [('lecture.mp4.response',b'PRIVATE'),('lecture.mp4.headers',b'HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\n'),('lecture.mp4.resume.json',b'FOREIGN RECEIPT'),('.git/keep',b'FOREIGN GIT')]:
            (self.foreign/name).write_bytes(value)
        self.expected={str(p.relative_to(self.foreign)):p.read_bytes() for p in self.foreign.rglob('*') if p.is_file()}
        self.body=b'local video body';body=self.body
        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler):
                handler.send_response(200);handler.send_header('Content-Length',str(len(body)));handler.end_headers();handler.wfile.write(body)
            def log_message(*args):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start()
        def close():self.server.shutdown();self.server.server_close();thread.join(timeout=10)
        self.addCleanup(close);self.url='http://127.0.0.1:'+str(self.server.server_port)+'/lecture.mp4'

    def swap(self,path=None,symlink=False):
        path=path or self.stage;self.replacement=path
        code='from pathlib import Path;import sys;p=Path(sys.argv[1]);p.rename(p.with_name(p.name+"-owned"));f=Path(sys.argv[2]);'
        code+='p.symlink_to(f,target_is_directory=True)' if symlink else 'f.rename(p)'
        subprocess.run([sys.executable,'-B','-c',code,str(path),str(self.foreign)],check=True,capture_output=True,timeout=10)

    def race(self,after=False,symlink=False,parent=False):
        run=h.preparation_run
        def request(command,**kwargs):
            if not after:self.swap(self.stage.parent if parent else self.stage,symlink)
            result=run(command,**kwargs)
            if after:self.swap()
            return result
        error=None
        with patch.object(h,'preparation_run',request),contextlib.redirect_stderr(io.StringIO()):
            try:h.download(self.url,self.target,known_size=len(self.body))
            except (OSError,ValueError,RuntimeError) as caught:error=caught
        for name,expected in self.expected.items():self.assertEqual((self.replacement/name).read_bytes(),expected)
        self.assertIsNotNone(error,'A replaced destination must not be reported as downloaded')

    def test_swap_before_curl_preserves_foreign_response(self):self.race()
    def test_swap_after_curl_preserves_foreign_receipt(self):self.race(after=True)

    def test_symlink_swap_never_redirects_curl(self):self.race(symlink=True)
    def test_parent_swap_never_redirects_curl(self):self.race(parent=True)

    def partial_response(self,command,**kwargs):
        os.write(int(command[command.index('--dump-header')+1].rsplit('/',1)[1]),b'HTTP/1.1 200 OK\r\nContent-Length: 32\r\nETag: "v1"\r\n\r\n')
        kwargs['stdout'].write(b'abc');kwargs['stdout'].flush()
        return subprocess.CompletedProcess(command,18)

    def test_receipt_creation_after_directory_swap_preserves_foreign_receipt(self):
        from http_download_io import DownloadIO
        original=DownloadIO.write
        def write(storage,name,*args,**kwargs):
            if name==storage.receipt:self.swap()
            return original(storage,name,*args,**kwargs)
        with patch.object(h,'preparation_run',self.partial_response),patch.object(DownloadIO,'write',write),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises((ValueError,OSError)):h.download(self.url,self.target,known_size=32)
        for name,value in self.expected.items():self.assertEqual((self.replacement/name).read_bytes(),value)

    def test_existing_receipt_leaf_swap_during_retry_is_never_modified(self):
        receipt=self.target.with_name(self.target.name+'.resume.json')
        with patch.object(h,'preparation_run',self.partial_response),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):h.download(self.url,self.target,known_size=32)
        def replaced(command,**kwargs):
            result=self.partial_response(command,**kwargs)
            code='from pathlib import Path;import sys;p=Path(sys.argv[1]);p.rename(p.with_name("receipt-owned"));Path(sys.argv[2]).rename(p)'
            subprocess.run([sys.executable,'-B','-c',code,str(receipt),str(self.foreign/'lecture.mp4.resume.json')],check=True,capture_output=True,timeout=10)
            return result
        with patch.object(h,'preparation_run',replaced),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises((ValueError,OSError)):h.download(self.url,self.target,known_size=32)
        self.assertEqual(receipt.read_bytes(),b'FOREIGN RECEIPT')
        with contextlib.redirect_stderr(io.StringIO()):h.download(self.url,self.target,known_size=len(self.body))
        self.assertEqual(self.target.read_bytes(),self.body)
        self.assertEqual(receipt.read_bytes(),b'FOREIGN RECEIPT')

    def test_target_leaf_swap_during_write_never_overwrites_foreign_inode(self):
        original=os.write;swaps=[]
        foreign=self.foreign/'lecture.mp4';foreign.write_bytes(b'FOREIGN TARGET')
        def write(fd,data):
            if bytes(data)==self.body and not swaps:
                code='from pathlib import Path;import sys;p=Path(sys.argv[1]);p.rename(p.with_name("owned-video"));Path(sys.argv[2]).rename(p)'
                subprocess.run([sys.executable,'-B','-c',code,str(self.target),str(foreign)],check=True,capture_output=True,timeout=10)
                swaps.append(True)
            return original(fd,data)
        with patch('os.write',write),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises((ValueError,OSError)):h.download(self.url,self.target,known_size=len(self.body))
        self.assertEqual(len(swaps),1);self.assertEqual(self.target.read_bytes(),b'FOREIGN TARGET')
        self.assertEqual((self.foreign/'.git/keep').read_bytes(),b'FOREIGN GIT')

    def test_existing_target_is_never_replaced(self):
        self.target.write_bytes(b'EXISTING STUDENT FILE')
        with patch.object(h,'preparation_run',side_effect=AssertionError('No transfer allowed')):
            with self.assertRaisesRegex(ValueError,'already exists'):h.download(self.url,self.target)
        self.assertEqual(self.target.read_bytes(),b'EXISTING STUDENT FILE')

    def test_untrusted_partial_and_receipt_are_preserved_on_full_restart(self):
        part=self.target.with_name(self.target.name+'.part');receipt=self.target.with_name(self.target.name+'.resume.json')
        part.write_bytes(b'FOREIGN PART');receipt.write_bytes(b'[]')
        with contextlib.redirect_stderr(io.StringIO()):h.download(self.url,self.target,known_size=len(self.body))
        self.assertEqual(self.target.read_bytes(),self.body)
        self.assertEqual(part.read_bytes(),b'FOREIGN PART');self.assertEqual(receipt.read_bytes(),b'[]')

    def test_unknown_partial_is_not_overwritten_by_an_incomplete_transfer(self):
        part=self.target.with_name(self.target.name+'.part');part.write_bytes(b'UNKNOWN PREFIX')
        with patch.object(h,'preparation_run',self.partial_response),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises((ValueError,OSError)):h.download(self.url,self.target,known_size=32)
        self.assertEqual(part.read_bytes(),b'UNKNOWN PREFIX')
        self.assertFalse(self.target.exists())

    def test_receipt_symlink_never_changes_its_target(self):
        receipt=self.target.with_name(self.target.name+'.resume.json');receipt.symlink_to(self.foreign/'lecture.mp4.resume.json')
        with patch.object(h,'preparation_run',self.partial_response),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises((ValueError,OSError)):h.download(self.url,self.target,known_size=32)
        self.assertEqual((self.foreign/'lecture.mp4.resume.json').read_bytes(),b'FOREIGN RECEIPT')

    def test_hardlinked_partial_refuses_before_transport(self):
        os.link(self.foreign/'lecture.mp4.response',self.target.with_name(self.target.name+'.part'))
        with patch.object(h,'preparation_run',side_effect=AssertionError('No transfer')):
            with self.assertRaisesRegex(ValueError,'ordinary private file'):h.download(self.url,self.target)
        self.assertEqual((self.foreign/'lecture.mp4.response').read_bytes(),b'PRIVATE')

    def test_successful_native_curl_and_no_named_response_or_headers(self):
        commands=[];run=h.preparation_run
        def request(command,**kwargs):
            commands.append(command)
            self.assertEqual(command[command.index('--output')+1],'-')
            self.assertTrue(command[command.index('--dump-header')+1].startswith('/dev/fd/'))
            self.assertTrue(kwargs['pass_fds'])
            return run(command,**kwargs)
        with patch.object(h,'preparation_run',request):h.download(self.url,self.target,known_size=len(self.body))
        self.assertEqual(self.target.read_bytes(),self.body);self.assertEqual(len(commands),1)
        self.assertFalse(self.target.with_name(self.target.name+'.response').exists())
        self.assertFalse(self.target.with_name(self.target.name+'.headers').exists())

    def child(self,mode):
        code=f"""
import sys,os
from pathlib import Path
sys.path.insert(0,{str(SCRIPTS)!r})
import http_resume as h
mode=sys.argv[3];body={self.body!r}
run=h.preparation_run
if mode=='pause':
 def request(command,**kw):
  print('READY',flush=True);sys.stdin.read(1)
  return run(command,**kw)
 h.preparation_run=request
if mode=='midwrite':
 write=os.write
 def interrupted(fd,data):
  if bytes(data)==body:
   write(fd,bytes(data)[:4]);print('READY',flush=True);sys.stdin.read(1)
  return write(fd,data)
 os.write=interrupted
print('START',flush=True)
try:
 h.download(sys.argv[1],Path(sys.argv[2]),known_size=len(body));print('DONE',flush=True)
except ValueError:print('REFUSED',flush=True)
"""
        child=subprocess.Popen([sys.executable,'-B','-c',code,self.url,str(self.target),mode],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
        def close():
            if child.poll() is None:child.kill()
            child.communicate(timeout=10)
        self.addCleanup(close)
        return child

    def line(self,child):
        with selectors.DefaultSelector() as ready:
            ready.register(child.stdout,selectors.EVENT_READ);self.assertTrue(ready.select(timeout=20))
        return child.stdout.readline().strip()

    def test_two_processes_serialize_and_never_overwrite_the_winner(self):
        first=self.child('pause');self.assertEqual(self.line(first),b'START');self.assertEqual(self.line(first),b'READY')
        second=self.child('normal');self.assertEqual(self.line(second),b'START')
        first.stdin.write(b'x');first.stdin.flush()
        a,e1=first.communicate(timeout=20);b,e2=second.communicate(timeout=20)
        self.assertEqual(first.returncode,0,e1);self.assertEqual(second.returncode,0,e2)
        self.assertEqual(a.strip(),b'DONE');self.assertEqual(b.strip(),b'REFUSED')
        self.assertEqual(self.target.read_bytes(),self.body)

    def test_sigkill_during_transport_then_retry_succeeds(self):
        child=self.child('pause');self.assertEqual(self.line(child),b'START');self.assertEqual(self.line(child),b'READY')
        child.kill();child.communicate(timeout=10);self.assertEqual(child.returncode,-signal.SIGKILL)
        self.assertFalse(self.target.exists())
        h.download(self.url,self.target,known_size=len(self.body));self.assertEqual(self.target.read_bytes(),self.body)

    def test_sigkill_mid_publication_preserves_partial_and_requires_fresh_target(self):
        child=self.child('midwrite');self.assertEqual(self.line(child),b'START');self.assertEqual(self.line(child),b'READY')
        child.kill();child.communicate(timeout=10);self.assertEqual(child.returncode,-signal.SIGKILL)
        self.assertEqual(self.target.read_bytes(),self.body[:4])
        with self.assertRaisesRegex(ValueError,'fresh destination'):h.download(self.url,self.target)
        fresh=self.stage/'retry.mp4';h.download(self.url,fresh,known_size=len(self.body))
        self.assertEqual(fresh.read_bytes(),self.body);self.assertEqual(self.target.read_bytes(),self.body[:4])


class HTTPGetRefusal(unittest.TestCase):
    def test_missing_safe_primitives_refuse_before_curl_or_writes(self):
        import safe_zip_import
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(safe_zip_import,'supported',return_value=False),patch.object(h,'preparation_run',side_effect=AssertionError('No curl')):
                with self.assertRaisesRegex(ValueError,'no unsafe fallback'):h.download('https://example.invalid/v',Path(temp)/'video')
            self.assertEqual(list(Path(temp).iterdir()),[])
