import json
from pathlib import Path
import subprocess,sys,tempfile,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import http_resume as h

class HTTPResume(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.target=Path(self.tmp.name)/'video.mp4';self.calls=[]
    def tearDown(self):self.tmp.cleanup()
    def response(self,status,body,headers,code=0):
        def run(cmd,**kw):
            self.calls.append(cmd)
            Path(cmd[cmd.index('--dump-header')+1]).write_text('HTTP/1.1 '+str(status)+' Test\r\n'+''.join(k+': '+v+'\r\n' for k,v in headers.items())+'\r\n')
            Path(cmd[cmd.index('--output')+1]).write_bytes(body)
            return subprocess.CompletedProcess(cmd,code,b'',b'')
        return run
    def call(self,status,body,headers,code=0):
        with patch.object(h.subprocess,'run',self.response(status,body,headers,code)):h.download('https://example.org/video',self.target)
    def partial(self,etag='"v1"'):
        with self.assertRaises(RuntimeError):self.call(200,b'abc',{'Content-Length':'6','ETag':etag},18)
        self.assertFalse(self.target.exists())
    def test_resume_exact_bytes(self):
        self.partial();self.call(206,b'def',{'Content-Range':'bytes 3-5/6','Content-Length':'3','ETag':'"v1"'})
        self.assertEqual(self.target.read_bytes(),b'abcdef');self.assertIn('3-',self.calls[-1]);self.assertIn('If-Range: "v1"',self.calls[-1])
    def test_ignored_range_replaces_not_appends(self):
        self.partial();self.call(200,b'UVWXYZ',{'Content-Length':'6','ETag':'"v2"'})
        self.assertEqual(self.target.read_bytes(),b'UVWXYZ')
    def test_wrong_range_preserves_prefix(self):
        self.partial()
        with self.assertRaises(ValueError):self.call(206,b'def',{'Content-Range':'bytes 2-4/6','ETag':'"v1"'})
        self.assertEqual(self.target.with_name('video.mp4.part').read_bytes(),b'abc')
    def test_changed_etag_not_appended(self):
        self.partial()
        with self.assertRaises(ValueError):self.call(206,b'def',{'Content-Range':'bytes 3-5/6','ETag':'"v2"'})
        self.assertFalse(self.target.exists())
    def test_weak_validator_restarts(self):
        self.partial('W/"v1"');self.call(200,b'abcdef',{'Content-Length':'6'})
        self.assertNotIn('--range',self.calls[-1])
    def test_tampered_prefix_restarts(self):
        self.partial();self.target.with_name('video.mp4.part').write_bytes(b'BAD')
        self.call(200,b'abcdef',{'Content-Length':'6','ETag':'"v1"'});self.assertNotIn('--range',self.calls[-1])
    def test_partial_range_can_resume_again(self):
        self.partial()
        with self.assertRaises(RuntimeError):self.call(206,b'd',{'Content-Range':'bytes 3-5/6','ETag':'"v1"'},28)
        self.call(206,b'ef',{'Content-Range':'bytes 4-5/6','ETag':'"v1"'})
        self.assertEqual(self.target.read_bytes(),b'abcdef')
    def test_short_success_rejected(self):
        with self.assertRaises(ValueError):self.call(200,b'abc',{'Content-Length':'6'})
        self.assertFalse(self.target.exists())
    def test_range_416_does_not_publish(self):
        self.partial()
        with self.assertRaises(ValueError):self.call(416,b'',{},22)
        self.assertFalse(self.target.exists())
    def test_redirect_headers_use_final_response(self):
        p=Path(self.tmp.name)/'headers';p.write_text('HTTP/1.1 302 Found\nLocation: /next\n\nHTTP/2 200 OK\nETag: "v1"\nContent-Length: 6\n\n')
        self.assertEqual(h.response_headers(p),(200,{'etag':'"v1"','content-length':'6'}))
    def test_encoded_response_rejected(self):
        with self.assertRaises(ValueError):self.call(200,b'abc',{'Content-Encoding':'gzip'})

    def test_repeated_cookies_do_not_block_download(self):
        self.call(200,b'abc',{'Content-Length':'3','Set-Cookie':'a=1\r\nSet-Cookie: b=2'})
        self.assertEqual(self.target.read_bytes(),b'abc')
    def test_repeated_cookies_preserve_server_error(self):
        with self.assertRaisesRegex(ValueError,'HTTP 500'):
            self.call(500,b'',{'Set-Cookie':'a=1\r\nSet-Cookie: b=2'},22)
        self.assertFalse(self.target.exists())
    def test_conflicting_validation_headers_still_rejected(self):
        for key in ['Content-Length','Content-Range','Content-Encoding','ETag']:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError,'Conflicting'):
                self.call(200,b'abc',{key:'a\r\n'+key.lower()+': b'})
        self.assertFalse(self.target.exists())
