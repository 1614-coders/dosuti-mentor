"""Real PDF text-layer oracles, not live Codex or OCR tests."""
import json
import hashlib
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import pdf_fixtures as fixtures
from material_sources import text_content
import pdf_content as pdf

class PDFContent(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='dosuti-pdf-');self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name).resolve()/'учебный документ.pdf'
    def read(self,data):
        self.path.write_bytes(data);return text_content(self.path,'application/pdf')
    def test_textbook_pages_and_caveat(self):
        value,method=self.read(fixtures.textbook())
        self.assertEqual(method,'pdf_text_layer_v1')
        for token in ('PDF page 1','PDF page 2','Not thread-safe.','1. Add a key.','Practice'):
            self.assertIn(token,value)
        self.assertEqual(len(re.findall(r'Map\s+basics',value)),1)
    def test_table_rows_and_page_locator(self):
        value,_=self.read(fixtures.table())
        self.assertIn('[PDF page 1 table 1',value)
        self.assertRegex(value,r'Method\tBehavior')
        self.assertRegex(value,r'put\treplace')
        self.assertIn('headers unverified',value)
    def test_code_preserves_relative_indentation(self):
        value,_=self.read(fixtures.code());lines=value.splitlines()
        first=next(s for s in lines if 'if count' in s);second=next(s for s in lines if 'print(count)' in s)
        self.assertEqual((len(second)-len(second.lstrip()))-(len(first)-len(first.lstrip())),4)
    def test_scan_and_blank_have_no_invented_text(self):
        value,_=self.read(fixtures.scan());self.assertIn('requires_ocr',value)
        value,_=self.read(fixtures.document(['']));self.assertIn('no_text_layer',value)
    def test_zero_page_document(self):
        value,_=self.read(fixtures.document([]));self.assertIn('no pages',value)
    def test_corrupt_pdf_is_controlled_and_preserved(self):
        data=b'%PDF-1.4\nbroken';self.path.write_bytes(data)
        with self.assertRaises(ValueError):text_content(self.path)
        self.assertEqual(self.path.read_bytes(),data)

    def test_page_and_output_limits_reject_without_partial_result(self):
        self.path.write_bytes(fixtures.textbook());sha=hashlib.sha256(self.path.read_bytes()).hexdigest()
        for name,limit,pattern in [('MAX_PAGES',1,'page limit'),('MAX_OUTPUT',30,'output')]:
            with self.subTest(limit=name),patch.object(pdf,name,limit):
                with self.assertRaisesRegex(ValueError,pattern):pdf.read_pdf(self.path,sha)

    def test_input_size_is_checked_before_worker_and_parsing(self):
        self.path.write_bytes(fixtures.textbook());raw=self.path.read_bytes()
        with patch.object(pdf,'MAX_INPUT',8):
            with self.assertRaisesRegex(ValueError,'32 MiB'):pdf.extract(self.path)
            with self.assertRaisesRegex(ValueError,'32 MiB'):pdf.read_pdf(self.path,hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.path.read_bytes(),raw)

    def test_cleanup_does_not_eagerly_reload_all_pages(self):
        import pdfplumber
        self.path.write_bytes(fixtures.textbook());sha=hashlib.sha256(self.path.read_bytes()).hexdigest()
        def forbidden(_):raise AssertionError('Eager page list must not be created, including cleanup')
        with patch.object(pdfplumber.pdf.PDF,'pages',property(forbidden)):
            self.assertIn('PDF page 2',pdf.read_pdf(self.path,sha)['text'])

    def test_timeout_returns_no_text_and_keeps_source(self):
        data=fixtures.textbook();self.path.write_bytes(data)
        with patch.object(pdf,'WALL_SECONDS',.001):
            with self.assertRaisesRegex(ValueError,'timed out'):pdf.extract(self.path)
        self.assertEqual(self.path.read_bytes(),data)

    def test_worker_memory_guard_rejects_large_allocation(self):
        code='import pdf_limits; pdf_limits.MEMORY_BYTES=64*1024*1024; job=pdf_limits.apply(); print("GUARD_READY",flush=True); x=bytearray(128*1024*1024); pdf_limits.check_memory()'
        result=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,timeout=10,
            env=dict(os.environ,PYTHONPATH=str(Path(pdf.__file__).parent)))
        self.assertNotEqual(result.returncode,0)
        self.assertIn(b'GUARD_READY',result.stdout)

    def test_worker_result_and_changed_source_are_rejected(self):
        self.path.write_bytes(fixtures.textbook());sha=hashlib.sha256(self.path.read_bytes()).hexdigest()
        from preparation_control import subprocess as processes
        result=processes.CompletedProcess([],0,json.dumps(dict(source_sha256='0'*64,text='invented')),'')
        with patch('preparation_control.run',return_value=result):
            with self.assertRaisesRegex(ValueError,'worker result'):pdf.extract(self.path)
        def changed(*a,**kw):
            self.path.write_bytes(fixtures.code())
            return processes.CompletedProcess([],0,json.dumps(dict(source_sha256=sha,text='text')),'')
        with patch('preparation_control.run',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'source changed'):pdf.extract(self.path)

    def test_compressed_stream_expansion_is_bounded(self):
        # Small on disk, over 16 MiB after decoding; no character/output limit catches whitespace.
        data=fixtures.document([' '*(17*1024*1024)],compressed=True)
        self.assertLess(len(data),32*1024)
        self.path.write_bytes(data)
        with self.assertRaisesRegex(ValueError,'resource limits'):pdf.extract(self.path)
        self.assertEqual(self.path.read_bytes(),data)

    def test_normal_compressed_multipage_document(self):
        value,_=self.read(fixtures.document([fixtures.text('First page'),fixtures.text('Second page')],compressed=True))
        self.assertIn('First page',value);self.assertIn('Second page',value)
        self.assertIn('PDF page 2',value)

    def test_encrypted_pdf_even_with_empty_password_is_refused(self):
        import pdfplumber
        data=fixtures.encrypted_empty();self.path.write_bytes(data)
        with pdfplumber.open(self.path) as source:self.assertTrue(source.doc.encryption)
        with self.assertRaisesRegex(ValueError,'encrypted'):pdf.extract(self.path)
        self.assertEqual(self.path.read_bytes(),data)

    def test_pdf_actions_are_not_executed(self):
        action=b'/OpenAction << /S /JavaScript /JS (throw new Error("DO_NOT_EXECUTE")) >>'
        value,_=self.read(fixtures.document([fixtures.text('Actual lesson')],catalog_extra=action))
        self.assertIn('Actual lesson',value);self.assertNotIn('DO_NOT_EXECUTE',value)

    def test_flate_limits_cumulative_budget_and_corrupt_stream(self):
        code="import zlib\nimport pdf_limits\nfrom pdfminer.pdftypes import PDFStream\nfrom pdfminer.psparser import LIT\npdf_limits.MAX_FLATE_STREAM=512\npdf_limits.MAX_FLATE_TOTAL=1024\npdf_limits.limit_streams()\ndef decode(value):return PDFStream({'Filter':LIT('FlateDecode')},value).get_data()\nassert decode(zlib.compress(b'a'*400))==b'a'*400\nassert decode(zlib.compress(b'b'*400))==b'b'*400\nfor value in (zlib.compress(b'c'*400),b'broken',zlib.compress(b'c')[:-2]):\n    try:decode(value)\n    except ValueError:pass\n    else:raise AssertionError('Invalid/oversized stream accepted')\nassert zlib.decompress(zlib.compress(b'x'*2000))==b'x'*2000\nprint('BOUNDED_AND_SCOPED')\n"
        result=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,timeout=10,
            env=dict(os.environ,PYTHONPATH=str(Path(pdf.__file__).parent)))
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn(b'BOUNDED_AND_SCOPED',result.stdout)

    def test_worker_cpu_budget_and_real_page_limit(self):
        code='import pdf_limits; pdf_limits.CPU_SECONDS=1; job=pdf_limits.apply(); print("GUARD_READY",flush=True)\nwhile True: pass'
        result=subprocess.run([sys.executable,'-B','-c',code],capture_output=True,timeout=10,
            env=dict(os.environ,PYTHONPATH=str(Path(pdf.__file__).parent)))
        self.assertIn(b'GUARD_READY',result.stdout);self.assertNotEqual(result.returncode,0)
        self.path.write_bytes(fixtures.document(['']*201))
        with self.assertRaisesRegex(ValueError,'resource limits'):pdf.extract(self.path)

    def test_symlink_is_not_accepted(self):
        original=self.path.with_name('original.pdf');original.write_bytes(fixtures.textbook())
        try:self.path.symlink_to(original)
        except OSError as error:
            if os.name=='nt':self.skipTest('Symlink privilege unavailable: '+str(error))
            raise
        with self.assertRaises(ValueError):pdf.extract(self.path)

if __name__=='__main__':unittest.main()
