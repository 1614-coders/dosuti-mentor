"""Memory-bounded assembly must preserve exact UTF-8 and all integrity checks."""
import hashlib,json,sys,tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import document_store as ds
from atomic_files import atomic_bytes,atomic_json

class Reads(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'state.json'
    def write(self,value):
        with patch.object(ds,'INLINE_BYTES',1),patch.object(ds,'PART_BYTES',31):ds.write(self.path,value,'qa')
    def fingerprint(self):
        return {str(p):p.read_bytes() for p in self.path.parent.rglob('*') if p.is_file()}
    def test_unicode_codepoints_across_legacy_part_boundaries(self):
        data=dict(schema='qa',text='абв漢字🧠'*80,values=[1,True,None,{'id':'x'}])
        self.write(data);manifest=json.loads(self.path.read_text())
        self.assertTrue(any((ds.folder(self.path)/(p['sha256']+'.part')).read_bytes()[-1]&0xc0==0x80 for p in manifest['parts']))
        before=self.fingerprint()
        self.assertEqual(ds.read(self.path,None,'qa'),data)
        self.assertEqual(before,self.fingerprint())
    def test_bad_total_refused_before_decoding_parts(self):
        self.write(dict(schema='qa',text='x'*100))
        manifest=json.loads(self.path.read_text());manifest['bytes']+=1
        atomic_json(self.path,manifest);before=self.fingerprint()
        with patch('document_store.codecs.getincrementaldecoder',side_effect=AssertionError('No decoding')):
            with self.assertRaisesRegex(ValueError,'size changed'):ds.read(self.path,None,'qa')
        self.assertEqual(before,self.fingerprint())
    def test_wrong_aggregate_hash_does_not_repair_parts(self):
        self.write(dict(schema='qa',text='x'*100));manifest=json.loads(self.path.read_text())
        manifest['sha256']='0'*64;atomic_json(self.path,manifest);before=self.fingerprint()
        with self.assertRaisesRegex(ValueError,'checksum'):ds.read(self.path,None,'qa')
        self.assertEqual(before,self.fingerprint())
    def test_reordered_verified_parts_are_not_a_valid_document(self):
        self.write(dict(schema='qa',text='abcde'*100));manifest=json.loads(self.path.read_text())
        manifest['parts'].reverse();atomic_json(self.path,manifest)
        with self.assertRaisesRegex(ValueError,'checksum'):ds.read(self.path,None,'qa')
    def raw_manifest(self,raw):
        sha=hashlib.sha256(raw).hexdigest();folder=ds.folder(self.path);folder.mkdir()
        atomic_bytes(folder/(sha+'.part'),raw)
        atomic_json(self.path,dict(schema=ds.SCHEMA,kind='qa',bytes=len(raw),sha256=sha,parts=[dict(sha256=sha,bytes=len(raw))]))
    def test_valid_checksum_does_not_make_invalid_utf8_acceptable(self):
        self.raw_manifest(b'{"schema":"qa","value":"\xff"}');before=self.fingerprint()
        with self.assertRaises(ValueError):ds.read(self.path,None,'qa')
        self.assertEqual(before,self.fingerprint())
    def test_valid_checksum_does_not_make_nested_manifest_acceptable(self):
        self.raw_manifest(json.dumps(dict(schema=ds.SCHEMA)).encode())
        with self.assertRaisesRegex(ValueError,'logical'):ds.read(self.path,None,'qa')
    def test_last_part_corruption_is_checked(self):
        self.write(dict(schema='qa',text='abcde'*100));manifest=json.loads(self.path.read_text())
        item=manifest['parts'][-1];path=ds.folder(self.path)/(item['sha256']+'.part')
        data=path.read_bytes();path.write_bytes(b'!'*len(data));before=self.fingerprint()
        with self.assertRaisesRegex(ValueError,'checksum'):ds.read(self.path,None,'qa')
        self.assertEqual(before,self.fingerprint())
