"""Quality summary distinguishes uncertainty, stale annotations and actual recorded differences."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from material_quality import summarize


class MaterialQuality(unittest.TestCase):
    def context(self):
        return dict(sources=[], code_stages=[], visual_notes={'uncertainties': []},
                    requested_window=[0,60], coverage={'complete':False}, gaps=[[0,60]],
                    video_integrity=None, warnings=['unprepared_intervals'])

    def test_absence_of_checks_is_not_quality_confirmation(self):
        value=self.context();before=deepcopy(value);result=summarize(value)
        self.assertFalse(result['meaning_verified'])
        self.assertEqual(result['audio']['status'],'unavailable')
        self.assertEqual(result['screen']['status'],'unverified')
        self.assertEqual(result['code']['status'],'unverified')
        self.assertEqual(result['gaps'],[[0,60]])
        self.assertEqual(value,before)

    def test_audio_diagnostics_keep_fragment_scope(self):
        value=self.context();value['sources']=[dict(fragment_id='p1',coverage={'start':0,'end':120},
            audio_quality={'warnings':['silent']},asr_quality={'warnings':['silent','repetition']})]
        result=summarize(value)['audio']
        self.assertEqual(result['status'],'requires_review')
        self.assertEqual(result['diagnostics_scope'],'whole_contributing_fragments')
        self.assertEqual(result['fragments'][0]['coverage']['end'],120)
        self.assertEqual(result['fragments'][0]['warnings'],['silent','repetition'])

    def test_stale_or_ambiguous_difference_is_not_current_difference(self):
        value=self.context();value['code_stages']=[
            dict(id='old',match='differs',stale=True,overlapping_stage_ids=[]),
            dict(id='overlap',match='differs',stale=False,overlapping_stage_ids=['other']),
            dict(id='current',match='partial',stale=False,overlapping_stage_ids=[])]
        result=summarize(value)['code']
        self.assertEqual(result['difference_ids'],[])
        self.assertEqual(result['partial_ids'],['current'])
        self.assertEqual(result['stale_ids'],['old'])
        self.assertEqual(result['ambiguous_ids'],['overlap'])

    def test_matching_annotation_does_not_prove_entire_window(self):
        value=self.context();value['code_stages']=[dict(id='one',match='matches',stale=False,overlapping_stage_ids=[])]
        self.assertEqual(summarize(value)['code']['status'],'unverified')

    def test_many_uncertainties_are_bounded_with_count(self):
        value=self.context();value['visual_notes']['uncertainties']=[
            dict(range=[i,i+1],provenance={'fragment_id':'p1'},stale=True) for i in range(100)]
        result=summarize(value)['screen']
        self.assertEqual(result['uncertainty_count'],100)
        self.assertEqual(len(result['uncertainty_refs']),20)
        self.assertTrue(result['truncated'])
        self.assertTrue(result['uncertainty_refs'][0]['stale'])
        self.assertEqual(result['readability'],'not_automatically_assessed')
