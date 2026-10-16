import hashlib,json,os,sys,tempfile
from pathlib import Path
from unittest.mock import patch
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import storage_report as report

class StorageReport(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
    def tearDown(self):self.temp.cleanup()
    def write(self,path,data):
        p=self.root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);return p
    def test_groups_and_totals(self):
        for p,n in [('course.json',2),('lessons/one/video/.fetch-x/part',10),('lessons/one/video/file.mp4',20),('lessons/one/.jobs/job/audio.wav',30),('lessons/one/prepared/x/frame.jpg',40),('learners/alice/cursor.json',3)]:self.write(p,b'x'*n)
        r=report.inventory(self.root);self.assertEqual(r['totals']['logical_bytes'],105);self.assertEqual(r['totals']['file_paths'],6)
        self.assertEqual({x['category'] for x in r['groups']},{'course_metadata_or_other','download_staging','video_cache','processing_work','prepared_evidence','learner_data'});self.assertTrue(r['scan_complete']);self.assertIsNone(r['reclaimable_bytes'])
    def test_symlink_targets_not_counted(self):
        p=self.write('outside/large',b'x'*100);course=self.root/'course';course.mkdir();(course/'link').symlink_to(p);(course/'dir-link').symlink_to(p.parent,target_is_directory=True)
        r=report.inventory(course);self.assertEqual(r['totals']['logical_bytes'],0);self.assertEqual(r['skipped']['symbolic_links'],2)
    def test_hardlink_logical_vs_unique(self):
        p=self.write('a',b'abc');os.link(p,self.root/'b');r=report.inventory(self.root)
        self.assertEqual(r['totals']['logical_bytes'],6);self.assertEqual(r['totals']['file_paths'],2);self.assertEqual(r['totals']['unique_file_ids'],1)
        if hasattr(p.stat(),'st_blocks'):self.assertEqual(r['totals']['allocated_file_bytes'],p.stat().st_blocks*512)
    def test_limit_marks_partial(self):
        for i in range(10):self.write(str(i),b'x')
        r=report.inventory(self.root,3);self.assertTrue(r['limit_reached']);self.assertFalse(r['scan_complete']);self.assertEqual(r['entries_visited'],3)
    def test_largest_bounded(self):
        for i in range(25):self.write(str(i),b'x'*i)
        r=report.inventory(self.root);self.assertEqual(len(r['largest_files']),20);self.assertEqual(r['largest_files'][0]['logical_bytes'],24)
    def test_no_content_or_progress_changes(self):
        p=self.write('learners/alice/cursor.json',b'{"revision":2}');before=(p.read_bytes(),p.stat().st_mtime_ns)
        report.inventory(self.root);self.assertEqual(before,(p.read_bytes(),p.stat().st_mtime_ns))
    def test_special_file_not_opened(self):
        os.mkfifo(self.root/'pipe');r=report.inventory(self.root);self.assertEqual(r['skipped']['special_files'],1)
    def test_unreadable_directory_reported(self):
        with patch.object(report.os,'scandir',side_effect=PermissionError):r=report.inventory(self.root)
        self.assertFalse(r['scan_complete']);self.assertEqual(r['skipped']['error_count'],1)
    def test_invalid_limit(self):
        for value in (0,-1,1000001,True,'10'):
            with self.assertRaises(ValueError):report.inventory(self.root,value)
    def test_empty_course(self):
        r=report.inventory(self.root);self.assertEqual(r['totals']['logical_bytes'],0);self.assertTrue(r['scan_complete'])
