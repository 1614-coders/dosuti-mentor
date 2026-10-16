"""Overview navigation preserves all candidates, order and source integrity."""
import json
from pathlib import Path
import sys,tempfile,unittest
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import screen_evidence as screen
from source_integrity import file_hash

class Overviews(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.rows=[]
        for i in range(25):
            p=self.root/f'{i}.jpg';Image.new('RGB',(320,180),(i*10,0,0)).save(p)
            self.rows.append(dict(id=f'f{i:05}',file=p.name,sha256=file_hash(p),approx_seconds=i*.5,review_candidate=i!=8))
        self.receipt=dict(schema_version=1,requested_interval=[0,13],settings={'sample_every':.5,'algorithm':'screen-pixels-v5'},frames=self.rows)
        self.receipt['overviews']=screen.build_overviews(self.root,self.rows)
    def tearDown(self):self.tmp.cleanup()
    def test_groups_preserve_order_and_every_candidate(self):
        screen.verify_scan(self.root,self.receipt)
        groups=self.receipt['overviews'];self.assertEqual([len(g['frame_ids']) for g in groups],[12,12])
        self.assertEqual([x for g in groups for x in g['frame_ids']],[r['id'] for r in self.rows if r['review_candidate']])
        for r in self.rows:self.assertEqual(file_hash(self.root/r['file']),r['sha256'])
    def test_corrupt_sheet_rejected(self):
        (self.root/self.receipt['overviews'][0]['file']).write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError,'overview integrity'):screen.verify_scan(self.root,self.receipt,selected=[])
    def test_dropped_candidate_rejected(self):
        self.receipt['overviews'][0]['frame_ids'].pop()
        with self.assertRaisesRegex(ValueError,'ordered candidates'):screen.verify_scan(self.root,self.receipt)
    def test_duplicate_candidate_rejected(self):
        self.receipt['overviews'][0]['frame_ids'][0]=self.receipt['overviews'][0]['frame_ids'][1]
        with self.assertRaisesRegex(ValueError,'ordered candidates'):screen.verify_scan(self.root,self.receipt)
    def test_old_scans_remain_readable(self):
        self.receipt['settings']['algorithm']='screen-pixels-v4';del self.receipt['overviews']
        screen.verify_scan(self.root,self.receipt)
    def test_single_candidate(self):
        groups=screen.build_overviews(self.root,self.rows[:1]);self.assertEqual(groups[0]['frame_ids'],[self.rows[0]['id']])
    def test_context_scopes_ids_and_marks_partial_group(self):
        self.receipt.update(source_sha256='a'*64,sample_count=25,limitations=[])
        p=self.root/'screen.json';p.write_text(json.dumps(self.receipt))
        data=dict(video={'sha256':'a'*64},screen_scans=[dict(folder=str(self.root),requested_interval=[0,13],receipt_sha256=file_hash(p))])
        frames,sources=screen.context_scans(self.root,data,1,2)
        groups=sources[0]['overviews']
        self.assertEqual(len(groups),1)
        self.assertTrue(groups[0]['may_include_frames_outside_window'])
        self.assertEqual(groups[0]['selected_frame_ids'],[f['id'] for f in frames])
        self.assertGreater(len(groups[0]['frame_ids']),len(frames))
        self.assertTrue(Path(groups[0]['absolute_path']).is_file())
