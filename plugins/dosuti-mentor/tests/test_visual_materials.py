"""Bounded visual candidates and explicit unit-scoped image links, not semantic vision."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from PIL import Image,ImageDraw
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import course
import test_study_units as units
import test_pdf_import as resources
import pdf_fixtures


def board(stage):
    image=Image.new('RGB',(240,160),'white');draw=ImageDraw.Draw(image)
    draw.text((10,10),'Code / board',fill='black')
    for i in range(stage):draw.line((10,40+i*15,180,50+i*15),fill='blue',width=3)
    out=io.BytesIO();image.save(out,format='PNG');return out.getvalue()

class VideoVisuals(unittest.TestCase):
    def setUp(self):
        self.f=units.StudyUnits();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.folder=self.f.f.add('board',0,20,[(1,8,'Explain diagram'),(9,15,'Finished diagram')],
            [(i,board(i//2)) for i in range(10)])
        self.source=dict(kind='video',role='teacher',notes='Original board',lesson='lesson-01',start=0,end=20)
    def test_candidates_keep_final_state_deduplicate_and_do_not_autodisplay(self):
        self.f.save(self.f.card(sources=[self.source]));visuals=self.f.get()['source_contexts'][0]['visual_materials']
        candidates=visuals['candidates']
        self.assertLess(len(candidates),10);self.assertEqual(candidates[-1]['locator']['seconds'],9)
        self.assertTrue(all(not r['can_display'] for r in candidates))
        self.assertTrue(all(r['links']['speech_ids'] for r in candidates if 1<=r['locator']['seconds']<15))
    def test_reviewed_diagram_and_code_image_can_be_shown_only_in_bound_unit(self):
        self.f.save(self.f.card(sources=[self.source]));row=self.f.get()['source_contexts'][0]['visual_materials']['candidates'][-1]
        annotation=dict(id=row['id'],caption='Рукописная схема: завершающий этап',notes='Изображение просмотрено, связь с фрагментом объяснения проверена')
        self.f.save(self.f.card(sources=[self.source|dict(visuals=[annotation])]))
        value=self.f.get()['source_contexts'][0]['visual_materials']['selected'][0]
        self.assertTrue(value['can_display']);self.assertIn('![',value['markdown'])
        self.assertIn(str(self.folder),value['markdown']);self.assertFalse(value['meaning_verified'])
        self.assertEqual(value['locator']['seconds'],9)
    def test_damaged_selected_image_is_rejected_without_rewriting_unit(self):
        self.f.save(self.f.card(sources=[self.source]));row=self.f.get()['source_contexts'][0]['visual_materials']['candidates'][-1]
        self.f.save(self.f.card(sources=[self.source|dict(visuals=[dict(id=row['id'],caption='Board',notes='Inspected QA image')])]))
        before=self.f.path.read_bytes();Path(row['path']).write_bytes(b'corrupt')
        self.assertTrue(self.f.get()['stale']);self.assertEqual(self.f.path.read_bytes(),before)
    def test_context_budget_reports_omissions_and_still_keeps_final(self):
        with patch.dict(os.environ,DOSUTI_VISUAL_LIMITS=json.dumps(dict(max_candidates=2))):
            self.f.save(self.f.card(sources=[self.source]));result=self.f.get()['source_contexts'][0]['visual_materials']
        self.assertLessEqual(len(result['candidates']),2);self.assertGreater(result['omitted_count'],0)
        self.assertEqual(result['candidates'][-1]['locator']['seconds'],9)
    def test_unknown_image_annotation_does_not_publish(self):
        with self.assertRaises(ValueError):self.f.save(self.f.card(sources=[self.source|dict(visuals=[dict(id='missing',caption='Guess',notes='Not inspected')])]))
        self.assertFalse(self.f.path.exists())

    def test_context_byte_budget_is_explicit_and_read_does_not_write(self):
        self.f.save(self.f.card(sources=[self.source]));before=self.f.path.read_bytes()
        with patch.dict(os.environ,DOSUTI_VISUAL_LIMITS=json.dumps(dict(max_context_bytes=1600))):
            result=self.f.get()['source_contexts'][0]['visual_materials']
        self.assertLessEqual(len(json.dumps(result,ensure_ascii=False).encode()),1600)
        self.assertTrue(result['context_limited']);self.assertGreater(result['omitted_count'],0)
        self.assertEqual(self.f.path.read_bytes(),before)
    def test_pixel_budget_and_stale_cas_do_not_replace_unit(self):
        self.f.save(self.f.card(sources=[self.source]));unit=self.f.get();before=self.f.path.read_bytes()
        row=unit['source_contexts'][0]['visual_materials']['candidates'][-1]
        annotated=self.source|dict(visuals=[dict(id=row['id'],caption='Code',notes='Image inspected')])
        with patch.dict(os.environ,DOSUTI_VISUAL_LIMITS=json.dumps(dict(max_pixels=100))):
            with self.assertRaises(ValueError):self.f.save(self.f.card(sources=[annotated]))
        self.assertEqual(self.f.path.read_bytes(),before)
        with self.assertRaises(ValueError):self.f.save(self.f.card(sources=[annotated]),revision=0)
        self.assertEqual(self.f.path.read_bytes(),before)

    def test_code_screen_is_preserved_without_transcribing_or_inventing_text(self):
        image=Image.new('RGB',(240,160),'white');draw=ImageDraw.Draw(image)
        draw.multiline_text((8,8),'if (count < 2) {\n    count++;\n}',fill='black')
        out=io.BytesIO();image.save(out,format='PNG');raw=out.getvalue()
        folder=self.f.f.add('code',20,40,[(21,25,'Code demonstration')],[(21,raw)])
        source=self.source|dict(start=20,end=40)
        self.f.save(self.f.card(sources=[source]));row=self.f.get()['source_contexts'][0]['visual_materials']['candidates'][0]
        self.f.save(self.f.card(sources=[source|dict(visuals=[dict(id=row['id'],caption='Исходный код на экране',notes='Viewed original code screen')])]))
        shown=self.f.get()['source_contexts'][0]['visual_materials']['selected'][0]
        self.assertEqual(Path(shown['path']).read_bytes(),raw)
        self.assertFalse(shown['meaning_verified']);self.assertNotIn('ocr_text',shown)

class PDFVisuals(unittest.TestCase):
    def setUp(self):
        self.f=resources.PDFImport();self.f.setUp();self.addCleanup(self.f.doCleanups)
    def test_selected_pdf_pages_are_managed_reused_and_registered(self):
        item=self.f.attach('resources')['items'][0]
        import material_sources as ms
        args=self.f.f.args(action='extract',resource_id=item['id'],expected_revision=None,notes=None,visual_pages=[1,2])
        item=ms.resource(args)['items'][0];bundle=item['visuals']
        self.assertEqual([v['page'] for v in bundle['images']],[1,2])
        self.assertTrue(all((Path(bundle['folder'])/v['file']).is_file() for v in bundle['images']))
        before=bundle.copy()
        with patch('pdf_visuals.render',side_effect=AssertionError('Must reuse pinned page images')):
            self.assertEqual(ms.resource(args)['items'][0]['visuals'],before)
    def test_render_failure_rolls_back_only_new_files(self):
        item=self.f.attach('resources')['items'][0];before=self.f.files()
        import material_sources as ms
        with patch('pdf_visuals.render',side_effect=RuntimeError('visual fixture failure')):
            with self.assertRaises(RuntimeError):ms.resource(self.f.f.args(action='extract',resource_id=item['id'],expected_revision=None,notes=None,visual_pages=[1]))
        self.assertEqual(self.f.files(),before)
    def test_page_budget_exposes_unrendered_pages(self):
        import material_sources as ms
        item=self.f.attach('resources')['items'][0]
        with patch.dict(os.environ,DOSUTI_VISUAL_LIMITS=json.dumps(dict(max_candidates=1))):
            item=ms.resource(self.f.f.args(action='extract',resource_id=item['id'],expected_revision=None,notes=None,visual_pages=[1,2]))['items'][0]
        self.assertEqual(len(item['visuals']['images']),1)
        self.assertEqual(item['visuals']['omitted_pages'],[2])

    def prepare(self,pages=[1,2]):
        import material_sources as ms
        item=self.f.attach('resources')['items'][0]
        return ms.resource(self.f.f.args(action='extract',resource_id=item['id'],expected_revision=None,notes=None,visual_pages=pages))['items'][0]
    def source(self,item,start=1,end=None):
        import material_sources as ms
        ms.resource(self.f.f.args(action='review',resource_id=item['id'],expected_revision=item['revision'],notes='Text inspected; image topic link reviewed separately'))
        lines=Path(item['extracted_file']).read_text().splitlines()
        return dict(kind='resource',role='external',notes='PDF source',lesson='lesson-01',resource_id=item['id'],start_line=start,end_line=end or len(lines))
    def test_pdf_unit_annotation_and_real_compact_cli_preserve_visual(self):
        import subprocess
        item=self.prepare();source=self.source(item)
        f=self.f.f;f.save(f.card(sources=[source]));candidate=f.get()['source_contexts'][0]['visual_materials']['candidates'][0]
        source['visuals']=[dict(id=candidate['id'],caption='Слайд определения',notes='Viewed page1 and checked its topic')]
        f.save(f.card(sources=[source]))
        result=subprocess.run([sys.executable,'-B',str(Path(course.__file__)),'learn-unit','--course',str(f.f.path),
            '--unit-id','maps','--profile','default','--session','new-chat','--compact'],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        visual=json.loads(result.stdout)['unit']['source_contexts'][0]['visual_materials']['selected'][0]
        self.assertEqual(visual['locator']['page'],1);self.assertTrue(visual['can_display'])
        self.assertIn('Открыть изображение',visual['link'])
    def test_pdf_image_from_a_different_page_cannot_be_bound(self):
        item=self.prepare();lines=Path(item['extracted_file']).read_text().splitlines()
        end=next(i for i,s in enumerate(lines,1) if '[end PDF page 1]' in s)
        source=self.source(item,end=end)
        source['visuals']=[dict(id='page-2',caption='Wrong page',notes='Cannot prove this link')]
        with self.assertRaises(ValueError):self.f.f.save(self.f.f.card(sources=[source]))
    def test_corrupt_cached_image_refuses_reuse_and_keeps_registration(self):
        import material_sources as ms
        item=self.prepare([1]);before=self.f.files()
        image=Path(item['visuals']['folder'])/item['visuals']['images'][0]['file']
        image.chmod(0o600);image.write_bytes(b'corrupt')
        journal=(self.f.f.f.lesson/'lesson.json').read_bytes()
        with self.assertRaises(ValueError):ms.resource(self.f.f.args(action='extract',resource_id=item['id'],expected_revision=None,notes=None,visual_pages=[1]))
        self.assertEqual((self.f.f.f.lesson/'lesson.json').read_bytes(),journal)
        self.assertTrue(image.exists())
    def test_disk_limit_lists_missing_pages_and_timeout_cleans_staging(self):
        with patch.dict(os.environ,DOSUTI_VISUAL_LIMITS=json.dumps(dict(max_disk_bytes=1))):
            item=self.prepare([1])
        self.assertEqual(item['visuals']['images'],[]);self.assertEqual(item['visuals']['omitted_pages'],[1])
        import material_sources as ms,subprocess
        before=self.f.files()
        with patch('preparation_control.run',side_effect=subprocess.TimeoutExpired('render',.01)):
            with self.assertRaisesRegex(ValueError,'timed out'):ms.resource(self.f.f.args(action='extract',resource_id=item['id'],expected_revision=None,notes=None,visual_pages=[1,2]))
        self.assertEqual(self.f.files(),before)
    def test_context_only_limit_change_does_not_rerender(self):
        import material_sources as ms
        item=self.prepare([1])
        with patch.dict(os.environ,DOSUTI_VISUAL_LIMITS=json.dumps(dict(max_context_bytes=1600))),patch('pdf_visuals.render',side_effect=AssertionError('No new rendering')):
            self.assertEqual(ms.resource(self.f.f.args(action='extract',resource_id=item['id'],expected_revision=None,notes=None,visual_pages=[1]))['items'][0]['visuals'],item['visuals'])

    def test_move_preserves_selected_pdf_image_and_old_data(self):
        import shutil
        item=self.prepare([1]);source=self.source(item);f=self.f.f
        f.save(f.card(sources=[source]));row=f.get()['source_contexts'][0]['visual_materials']['candidates'][0]
        source['visuals']=[dict(id=row['id'],caption='Page1',notes='Inspected')];f.save(f.card(sources=[source]))
        moved=f.f.directory/'moved';shutil.move(f.f.path,moved);f.f.path=moved;f.f.args.course=moved
        result=f.get();self.assertFalse(result['stale'])
        visual=result['source_contexts'][0]['visual_materials']['selected'][0]
        self.assertTrue(Path(visual['path']).is_relative_to(moved));self.assertTrue(Path(visual['path']).exists())
    def test_parallel_render_publishes_once_and_cleans_loser(self):
        from concurrent.futures import ThreadPoolExecutor
        import material_sources as ms
        item=self.f.attach('resources')['items'][0]
        args=self.f.f.args(action='extract',resource_id=item['id'],expected_revision=None,notes=None,visual_pages=[1])
        def run(_):
            try:return ms.resource(args)
            except ValueError:return None
        with ThreadPoolExecutor(2) as pool:values=list(pool.map(run,range(2)))
        self.assertTrue(any(v is not None for v in values))
        current=self.f.resource('list',None)['items'][0]
        folders=list(Path(current['origin']['raw_file']).parent.glob('visuals'))
        self.assertEqual(folders,[Path(current['visuals']['folder'])])
    def test_invalid_pages_are_rejected_before_pdf_work(self):
        import material_sources as ms
        item=self.f.attach('resources')['items'][0];before=self.f.files()
        for pages in ([0],[1,1],[True],[],[201]):
            with self.subTest(pages=pages),patch.object(ms,'text_content',side_effect=AssertionError('Validate first')):
                with self.assertRaises(ValueError):ms.resource(self.f.f.args(action='extract',resource_id=item['id'],expected_revision=None,notes=None,visual_pages=pages))
        self.assertEqual(self.f.files(),before)
    def test_corrupt_receipt_is_controlled_and_not_silently_replaced(self):
        import material_sources as ms
        item=self.prepare([1]);data=course.read(self.f.f.f.lesson/'lesson.json')
        data['resources']['items'][0]['visuals']={'schema':'broken'}
        course.save(self.f.f.f.lesson/'lesson.json',data);before=self.f.files()
        with self.assertRaises(ValueError):ms.resource(self.f.f.args(action='extract',resource_id=item['id'],expected_revision=None,notes=None,visual_pages=[2]))
        self.assertEqual(self.f.files(),before)

    def test_identical_pdf_pages_share_one_file_without_losing_page_numbers(self):
        self.f.path.write_bytes(pdf_fixtures.document([pdf_fixtures.text('Same slide')]*2))
        item=self.prepare([1,2]);rows=item['visuals']['images']
        self.assertEqual([r['page'] for r in rows],[1,2])
        self.assertEqual(rows[0]['file'],rows[1]['file'])
        self.assertEqual(len(list(Path(item['visuals']['folder']).glob('*.png'))),1)

    def test_incremental_page_cache_does_not_repeat_old_render_or_leak_staging(self):
        import material_sources as ms,pdf_visuals
        item=self.prepare([1]);first=item['visuals']['images'][0];calls=[];original=pdf_visuals.render
        def render(raw,folder,pages,*a):calls.append(pages);return original(raw,folder,pages,*a)
        with patch.object(pdf_visuals,'render',side_effect=render):
            updated=ms.resource(self.f.f.args(action='extract',resource_id=item['id'],expected_revision=None,notes=None,visual_pages=[2]))['items'][0]
        self.assertEqual(calls,[[2]])
        self.assertEqual(len(updated['visuals']['images']),2)
        self.assertEqual(updated['visuals']['images'][0],first)
        parent=Path(updated['origin']['raw_file']).parent
        self.assertEqual(list(parent.glob('.visual-staging-*')),[])
        self.assertEqual(len(list((parent/'visuals').glob('*.png'))),2)

if __name__=='__main__':unittest.main()
