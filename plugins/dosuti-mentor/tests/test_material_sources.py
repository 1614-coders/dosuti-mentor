import contextlib,hashlib,io,json,shutil,sys,threading,zipfile
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course,material_sources as ms
import test_context

@contextlib.contextmanager
def windows_text_writes():
    """Exercise Windows newline translation with real file bytes on every OS."""
    original = Path.write_text
    def write(path, data, encoding=None, errors=None, newline=None):
        return original(path, data, encoding=encoding, errors=errors,
                        newline='\r\n' if newline is None else newline)
    with patch.object(Path, 'write_text', write):
        yield

class Materials(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:z.writestr('src/main.py','print(1)\n')
        cls.zip=buf.getvalue();cls.requests=0
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_GET(self):
                cls.requests+=1
                if self.path=='/redirect':self.send_response(302);self.send_header('Location','/article');self.end_headers();return
                if self.path=='/missing':self.send_error(404);return
                if self.path=='/zip':body=cls.zip;kind='application/zip'
                elif self.path=='/pdf':body=b'%PDF-1.4';kind='application/pdf'
                else:body=b'<html><h1>Task</h1><script>do not include this</script><p>Print &lt;hello&gt;</p><pre>if x:\n    print(x)</pre></html>';kind='text/html'
                self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start();cls.url='http://127.0.0.1:'+str(cls.server.server_port)
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def setUp(self):self.f=test_context.ContextRegression();self.f.setUp();self.a=self.f.args
    def tearDown(self):self.f.tearDown()
    def args(self,**kw):return NS(**(vars(self.a)|kw))
    def attach(self,kind,source):
        with contextlib.redirect_stdout(io.StringIO()):course.attach(self.args(kind=kind,source=str(source),none=False,ref=None,resource_type='article'))
        return course.read(self.f.lesson/'lesson.json')[kind]
    def resource(self,action='list',resource_id=None,**kw):return ms.resource(self.args(action=action,resource_id=resource_id,expected_revision=None,notes=None,**kw))
    def extract(self):
        item=self.attach('resources',self.url+'/article')['items'][0]
        return self.resource('extract',item['id'])['items'][0]
    def review(self,item,revision=None):return ms.resource(self.args(action='review',resource_id=item['id'],expected_revision=revision or item['revision'],notes='Read and checked task text'))
    def test_remote_zip_keeps_origin_and_inventory(self):
        code=self.attach('code',self.url+'/zip');self.assertIn('src/main.py',code['inventory']);self.assertEqual(code['origin']['source'],self.url+'/zip');self.assertTrue(Path(code['origin']['raw_file']).exists())
    def test_remote_homework_html_extracts_text(self):
        hw=self.attach('homework',self.url+'/redirect');text=Path(hw['file']).read_text();self.assertIn('Print <hello>',text);self.assertNotIn('do not include',text);self.assertIn('    print(x)',text);self.assertTrue(hw['origin']['effective_url'].endswith('/article'))
    def test_remote_code_html_rejected_without_replacing_code(self):
        with self.assertRaises(ValueError):self.attach('code',self.url+'/article')
        self.assertEqual(course.read(self.f.lesson/'lesson.json')['code']['status'],'not_asked')
    def test_404_does_not_publish(self):
        with self.assertRaises(ValueError):self.attach('homework',self.url+'/missing')
        self.assertEqual(course.read(self.f.lesson/'lesson.json')['homework']['status'],'not_asked')
    def test_pdf_not_misrepresented_as_extracted_text(self):
        with self.assertRaises(ValueError):self.attach('homework',self.url+'/pdf')
    def test_size_limit(self):
        with patch.object(ms,'MAX_BYTES',16):
            with self.assertRaises(ValueError):self.attach('homework',self.url+'/article')
    def test_extract_is_not_review(self):
        item=self.extract();self.assertEqual(item['status'],'extracted_not_reviewed');self.assertNotIn('review',item);self.assertEqual(item['text_integrity'],'verified')
    def test_review_is_revision_bound(self):
        item=self.extract()
        with self.assertRaises(ValueError):self.review(item,'sha256:old')
        self.assertTrue(self.review(item)['items'][0]['review_valid'])
    def test_text_tampering_invalidates_review(self):
        item=self.extract();self.review(item);p=Path(item['extracted_file']);p.chmod(0o600);p.write_text('modified')
        state=self.resource()['items'][0];self.assertEqual(state['status'],'needs_review');self.assertFalse(state['review_valid'])
        with self.assertRaises(ValueError):self.review(item)
    def test_raw_tampering_invalidates_review(self):
        item=self.extract();self.review(item);p=Path(item['origin']['raw_file']);p.chmod(0o600);p.write_text('modified')
        self.assertFalse(self.resource()['items'][0]['review_valid'])
        with self.assertRaises(ValueError):self.resource('extract',item['id'])
    def test_cached_extract_keeps_review_without_network(self):
        item=self.extract();self.review(item)
        with patch.object(ms,'acquire',side_effect=AssertionError('Network not expected')):
            self.assertTrue(self.resource('extract',item['id'])['items'][0]['review_valid'])
    def test_video_resource_not_treated_as_article(self):
        item=self.attach('resources',self.url+'/article')['items'][0]
        course.update_lesson(self.f.lesson,lambda d:d['resources']['items'][0].update(kind='video'))
        with self.assertRaises(ValueError):self.resource('extract',item['id'])
    def test_local_resource_pinned_at_attach(self):
        p=self.f.directory/'article.txt';p.write_text('hello');item=self.attach('resources',p)['items'][0];raw=Path(item['origin']['raw_file']);raw.chmod(0o600);raw.write_text('changed')
        with self.assertRaises(ValueError):self.resource('extract',item['id'])
    def test_plain_resource_keeps_code_whitespace_and_verified_cache(self):
        text='\n    if ready:\n        work()\n\nExplanation.\n\n'
        source=self.f.directory/'code-notes.txt';source.write_text(text)
        item=self.attach('resources',source)['items'][0]
        extracted=self.resource('extract',item['id'])['items'][0]
        self.assertEqual(Path(extracted['extracted_file']).read_text(),text)
        self.assertEqual(source.read_text(),text)
        self.review(extracted)
        reused=self.resource('extract',item['id'])['items'][0]
        self.assertEqual(reused['extracted_file'],extracted['extracted_file'])
        self.assertEqual(reused['text_sha256'],extracted['text_sha256'])
        self.assertTrue(reused['review_valid'])
    def test_text_homework_extraction_preserves_initial_tabs(self):
        text='\tif ready:\n\t\twork()\n\n'
        source=self.f.directory/'homework.txt';source.write_text(text)
        with contextlib.redirect_stdout(io.StringIO()):
            ms.attach_material(self.args(kind='homework',source=str(source),none=False,ref=None))
        item=course.read(self.f.lesson/'lesson.json')['homework']
        self.assertEqual(Path(item['file']).read_text(),text)
        self.assertEqual(source.read_text(),text)
    def test_windows_translation_preserves_existing_material_cases(self):
        with windows_text_writes():
            for test in (self.test_plain_resource_keeps_code_whitespace_and_verified_cache,
                         self.test_text_homework_extraction_preserves_initial_tabs):
                with self.subTest(case=test.__name__):test()

    def check_text_bytes(self, kind):
        cases = (
            ('lf', b'\n    if ready:\n\twork()\n\n', b'\n    if ready:\n\twork()\n\n'),
            ('crlf', b'\r\n    if ready:\r\n\twork()\r\n\r\n', b'\r\n    if ready:\r\n\twork()\r\n\n'),
            ('mixed', b'\tclass Example {\r\n    void run() {\n\twork();\r\n    }\n}\r\n',
                      b'\tclass Example {\r\n    void run() {\n\twork();\r\n    }\n}\n'),
            ('no_final_newline', b'\tif ready:\r\n\t\twork()', b'\tif ready:\r\n\t\twork()\n'),
            ('blank_lines', b'  notes\r\n\r\n\tcode\n\n\n', b'  notes\r\n\r\n\tcode\n\n\n'),
        )
        for name, raw, expected in cases:
            with self.subTest(kind=kind, endings=name):
                source=self.f.directory/(name+'.txt');source.write_bytes(raw)
                if kind=='resources':
                    item=self.attach(kind,source)
                    item=self.resource('extract',item['items'][-1]['id'])['items'][-1]
                    target=Path(item['extracted_file']);checksum=item['text_sha256']
                    self.assertEqual(item['source_integrity'],'verified')
                    self.assertEqual(item['text_integrity'],'verified')
                else:
                    with contextlib.redirect_stdout(io.StringIO()):
                        ms.attach_material(self.args(kind='homework',source=str(source),none=False,ref=None))
                    item=course.read(self.f.lesson/'lesson.json')['homework']
                    target=Path(item['file']);checksum=item['sha256']
                self.assertEqual(target.read_bytes(),expected)
                self.assertEqual(checksum,hashlib.sha256(expected).hexdigest())
                self.assertEqual(item['origin']['sha256'],hashlib.sha256(raw).hexdigest())
                self.assertEqual(Path(item['origin']['raw_file']).read_bytes(),raw)
                self.assertEqual(source.read_bytes(),raw)
                if kind=='resources':
                    self.review(item)
                    before=target.stat()
                    with patch.object(ms,'text_content',side_effect=AssertionError('Verified cache must be reused')):
                        reused=self.resource('extract',item['id'])['items'][-1]
                    self.assertEqual(reused['extracted_file'],str(target))
                    self.assertEqual(reused['text_sha256'],checksum)
                    self.assertTrue(reused['review_valid'])
                    self.assertEqual(target.read_bytes(),expected)
                    self.assertEqual(target.stat().st_mtime_ns,before.st_mtime_ns)
                else:
                    saved=course.read(self.f.lesson/'lesson.json')['homework']
                    self.assertEqual(saved['sha256'],checksum)
                    self.assertEqual(Path(saved['file']).read_bytes(),expected)

    def test_resource_line_endings_bytes_and_cache(self):self.check_text_bytes('resources')
    def test_homework_line_endings_bytes_and_hash(self):self.check_text_bytes('homework')
    def test_windows_resource_line_endings_bytes_and_cache(self):
        with windows_text_writes():self.check_text_bytes('resources')
    def test_windows_homework_line_endings_bytes_and_hash(self):
        with windows_text_writes():self.check_text_bytes('homework')
    def test_transfer_of_course_preserves_resource_paths(self):
        item=self.extract();self.review(item);new=self.f.directory/'moved';shutil.move(self.a.course,new);self.a.course=new;self.f.lesson=new/'lessons/lesson-01'
        state=self.resource()['items'][0];self.assertTrue(state['review_valid']);self.assertTrue(Path(state['extracted_file']).is_relative_to(new))
    def test_unsafe_windows_zip_path_rejected(self):
        p=self.f.directory/'bad.zip'
        with zipfile.ZipFile(p,'w') as z:z.writestr('C:\\outside.py','bad')
        with self.assertRaises(ValueError):self.attach('code',p)
    def test_concurrent_replacement_rejected(self):
        original=course.update_lesson
        def replace(directory,callback):
            original(directory,lambda d:d.update(resources={'status':'absent','items':[]}));original(directory,callback)
        item=self.attach('resources',self.url+'/article')['items'][0]
        with patch.object(course,'update_lesson',side_effect=replace):
            with self.assertRaises(ValueError):self.resource('extract',item['id'])
        self.assertEqual(self.resource()['items'],[])
