"""Expanded HTML remains bounded and reaches compact CLI without silent truncation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import test_study_units as fixtures
import study_units as su
from material_sources import text_content

class HTMLLimits(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.StudyUnits();self.f.setUp();self.addCleanup(self.f.tearDown)

    def extracted(self,count):
        path=self.f.f.directory/'lesson.html'
        path.write_text('<article><h2>Topic</h2><table>'+('<tr><th scope="row">Key</th><td>Value</td></tr>'*count)+'</table></article><p>Final caveat.</p>',encoding='utf-8')
        content,_=text_content(path)
        self.f.text.write_text(content,encoding='utf-8')
        return content,path

    def card(self,content):
        value=self.f.card();value['sources'][0]['end_line']=len(content.splitlines());return value

    def test_expanded_context_survives_compact_cli_and_is_read_only(self):
        content,path=self.extracted(300)
        self.assertGreater(len(content.encode()),path.stat().st_size)
        self.f.save(self.card(content));row=self.f.get()
        before={str(p):p.read_bytes() for p in self.f.f.path.rglob('*') if p.is_file()}
        process=subprocess.run([sys.executable,'-B',str(Path(su.__file__).with_name('course.py')),
            'learn-unit','--course',str(self.f.f.path),'--unit-id','maps','--profile','default',
            '--session','new-chat','--compact'],capture_output=True,encoding='utf-8',timeout=20,
            env=dict(os.environ,PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1'))
        self.assertEqual(process.returncode,0,process.stderr)
        state=json.loads(process.stdout)
        self.assertEqual(state['output_view'],'compact_v1')
        self.assertEqual(state['unit']['source_contexts'][0]['text'],content.strip())
        self.assertEqual(row['source_contexts'][0]['text'],content.strip())
        self.assertEqual({str(p):p.read_bytes() for p in self.f.f.path.rglob('*') if p.is_file()},before)

    def test_excerpt_over_100000_characters_is_rejected_without_snapshot(self):
        content,_=self.extracted(2500)
        self.assertGreater(len(content),100000)
        with self.assertRaisesRegex(ValueError,'100000'):
            self.f.save(self.card(content))
        self.assertFalse(self.f.path.exists())
        self.assertFalse((self.f.f.path/'study-sources').exists())

    def test_text_snapshot_over_8_mib_is_rejected_without_publication(self):
        self.f.text.write_bytes(b'x'*(8*1024*1024+1))
        with self.assertRaisesRegex(ValueError,'8 MiB'):self.f.save()
        self.assertFalse(self.f.path.exists())
        self.assertFalse((self.f.f.path/'study-sources').exists())

    def test_extraction_output_limit_rolls_back_only_its_new_import(self):
        import html_content
        import material_sources as ms
        _,path=self.extracted(1)
        before={str(p):p.read_bytes() for p in self.f.f.path.rglob('*') if p.is_file()}
        with patch.object(html_content,'MAX_BYTES',path.stat().st_size+1):
            with self.assertRaisesRegex(ValueError,'output'):
                ms.attach_material(self.f.args(kind='homework',source=str(path)))
        after={str(p):p.read_bytes() for p in self.f.f.path.rglob('*') if p.is_file() and p.suffix!='.lock'}
        self.assertEqual(after,{k:v for k,v in before.items() if not k.endswith('.lock')})
        self.assertTrue(path.exists())

if __name__=='__main__':unittest.main()
