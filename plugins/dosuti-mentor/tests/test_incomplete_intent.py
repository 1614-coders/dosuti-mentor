"""An unreadable route must not crash readers or erase independent learning evidence."""
import contextlib
import io
import json
import unittest
import test_target_alignment
import adaptive_learning
import course
import learning_intent as intent
import learning_tools as learning
import unit_learning


class IncompleteIntent(unittest.TestCase):
    def setUp(self):
        self.f=test_target_alignment.TargetAlignment();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.accept(self.f.r.target(alignment_review=self.f.review()));self.f.solve_map()
        self.f.f.f.save(dict(recap='Map solved',next_step='Review later',pending_question=None),checkpoint=True)

    def test_corrupt_intent_preserves_results_and_continuations_without_automatic_selection(self):
        path=self.f.f.path;original=path.read_bytes()
        for bad in ('{broken',json.dumps(dict(schema='future',revision=0,events=[]))):
            with self.subTest(bad=bad):
                path.write_text(bad);before=self.f.f.files()
                value=intent.view(self.f.f.args())
                self.assertFalse(value['valid'])
                progress=value['learning_progress']
                self.assertFalse(progress['valid']);self.assertIn('intent',progress['reason'])
                self.assertEqual(progress['demonstrated_units'],['maps'])
                self.assertEqual(len(progress['continuations']),1)
                self.assertIsNone(progress['stopped_at']);self.assertIsNone(progress['next_step'])
                self.assertEqual(progress['completed_steps'],[])
                unit=unit_learning.learn_unit(self.f.f.f.args())
                self.assertTrue(unit['learning']['all_criteria_demonstrated'])
                self.assertEqual(unit['learning_progress'],progress)
                self.assertEqual(learning.resume(self.f.f.args())['learning_progress'],progress)
                advice=adaptive_learning.study_next(self.f.f.args(limit=5))
                self.assertFalse(advice['valid']);self.assertEqual(advice['recommendations'],[])
                self.assertEqual(advice['roadmap']['status'],'incomplete')
                self.assertEqual(before,self.f.f.files())
        path.write_bytes(original)
        self.assertEqual(intent.view(self.f.f.args())['learning_progress']['completed_steps'],[1])

    def test_status_and_failed_intent_write_leave_corrupt_file_and_unit_answers_intact(self):
        self.f.f.path.write_text('{broken');before=self.f.f.files()
        with contextlib.redirect_stdout(io.StringIO()) as output:course.status(self.f.f.args())
        self.assertFalse(json.loads(output.getvalue())['learning_progress']['valid'])
        with self.assertRaises(ValueError):
            intent.commit(self.f.f.args(expected_revision=0,reason='QA must not overwrite corrupt route'),
                          dict(goal=dict(description='Do not replace unreadable history')))
        self.assertEqual(before,self.f.f.files())


if __name__=='__main__':unittest.main()
