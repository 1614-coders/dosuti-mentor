"""Offline consent/roadmap contracts; fixtures are not real vacancy research."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import career_route
import course
import test_career_evidence


class CareerMarket(unittest.TestCase):
    def setUp(self):
        self.fixture = test_career_evidence.Career()
        self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.input = self.root.parent / 'market-input.json'

    def args(self, **kw): return self.fixture.args(**kw)
    def read(self, **kw): return career_route.route(self.args(**kw))['market_research']
    def save(self, event, revision=None):
        if revision is None: revision = self.read()['revision']
        self.input.write_text(json.dumps(event), encoding='utf-8')
        return self.read(market_file=self.input, expected_market_revision=revision)
    def context(self, **kw):
        return dict(action='context', professional=True, role='Java backend',
                    region='QA region', level='intern') | kw
    def consent(self, decision='granted'):
        return self.save(dict(action='decision', decision=decision,
                              statement='Synthetic QA learner decision, not real consent'))

    def test_old_course_read_does_not_create_consent_or_allow_search(self):
        value = self.read()
        self.assertEqual(value['revision'], 0)
        self.assertFalse(value['may_search'])
        self.assertFalse((self.root / 'learners').exists())

    def test_real_cli_saves_context_and_reports_stale_revision_without_traceback(self):
        self.input.write_text(json.dumps(self.context()), encoding='utf-8')
        command = [sys.executable, '-B', course.__file__, 'career-route', '--course', str(self.root),
                   '--profile', 'default', '--market-file', str(self.input), '--expected-market-revision', '0']
        done = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)['market_research']['revision'], 1)
        before = (self.root / 'learners/default/career-market.json').read_bytes()
        stale = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(stale.returncode, 1)
        self.assertIn('Market decision changed', stale.stderr)
        self.assertNotIn('Traceback', stale.stderr)
        self.assertEqual((self.root / 'learners/default/career-market.json').read_bytes(), before)

    def test_market_decision_and_route_cannot_be_written_together(self):
        self.input.write_text(json.dumps(self.context()), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'separate operations'):
            self.read(market_file=self.input, expected_market_revision=0, route_file=self.input)
        self.assertEqual(self.read()['revision'], 0)

    def test_profession_requires_consent_and_known_scope_before_search(self):
        value = self.save(self.context(region=None, level=None))
        self.assertEqual(value['next_action'], 'offer_market_research')
        self.assertFalse(value['may_search'])
        value = self.consent()
        self.assertEqual(value['next_action'], 'clarify_market_scope')
        self.assertFalse(value['may_search'])
        value = self.save(self.context())
        self.assertTrue(value['may_search'])
        self.assertEqual(value['next_action'], 'search_with_available_tools')

    def test_decline_persists_across_processes_without_repeated_offer(self):
        self.save(self.context()); self.consent('declined')
        code = ('import json,sys; from types import SimpleNamespace as N; from pathlib import Path; '
                'from career_route import route; print(json.dumps(route(N(course=Path(sys.argv[1]),'
                'profile="default"))["market_research"]))')
        result = subprocess.run([sys.executable, '-B', '-c', code, str(self.root)],
                                capture_output=True, text=True, timeout=15,
                                env=os.environ | {'PYTHONPATH': str(Path(course.__file__).parent)})
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['consent']['status'], 'declined')
        self.assertEqual(value['next_action'], 'ordinary_roadmap')
        self.assertFalse(value['should_offer']); self.assertFalse(value['may_search'])

    def test_nonprofessional_topic_does_not_offer_or_accept_search(self):
        value = self.save(self.context(professional=False, role='Learn fractions'))
        self.assertFalse(value['should_offer'])
        self.assertEqual(value['next_action'], 'ordinary_roadmap')
        with self.assertRaises(ValueError): self.consent()

    def test_offline_and_insufficient_results_do_not_invent_requirements(self):
        for status in ('unavailable', 'insufficient'):
            with self.subTest(status=status):
                self.save(self.context()); self.consent()
                value = self.save(dict(action='result', status=status,
                    note='QA search unavailable or too few sources', sample_id=None))
                self.assertEqual(value['next_action'], 'ordinary_roadmap')
                self.assertFalse(value['may_search'])
                self.assertEqual(value['requirement_groups'], {'frequent': [], 'additional': []})
                self.assertEqual(self.read()['result']['status'], status)

    def test_results_cannot_be_recorded_without_consent_or_a_real_saved_sample(self):
        self.save(self.context())
        event = dict(action='result', status='recorded', note='QA', sample_id='missing')
        with self.assertRaises(ValueError): self.save(event)
        self.consent()
        with self.assertRaises(ValueError): self.save(event)
        self.assertIsNone(self.read()['result'])

    def test_course_and_profile_isolation_and_scope_change(self):
        self.save(self.context()); self.consent()
        self.assertEqual(self.read(profile='bob')['revision'], 0)
        other = self.root.parent / 'other-course'; other.mkdir()
        (other / 'course.json').write_bytes((self.root / 'course.json').read_bytes())
        from types import SimpleNamespace
        other_args = SimpleNamespace(**(vars(self.args()) | {'course': other}))
        self.assertEqual(career_route.route(other_args)['market_research']['revision'], 0)
        value = self.save(self.context(region='Other region'))
        self.assertEqual(value['consent']['status'], 'not_asked')
        self.assertFalse(value['may_search'])

    def test_stale_cas_and_corruption_are_refused(self):
        self.save(self.context())
        path = self.root / 'learners/default/career-market.json'
        before = path.read_bytes()
        with self.assertRaises(ValueError): self.save(self.context(role='QA'), revision=0)
        self.assertEqual(path.read_bytes(), before)
        path.write_bytes(b'[]')
        with self.assertRaises(ValueError): self.read()
        self.assertEqual(path.read_bytes(), b'[]')

    def test_concurrent_decisions_have_one_cas_winner(self):
        self.save(self.context())
        from types import SimpleNamespace
        calls = []
        for decision in ('granted', 'declined'):
            path = self.root.parent / (decision + '.json')
            path.write_text(json.dumps(dict(action='decision', decision=decision, statement='QA explicit decision')))
            calls.append(SimpleNamespace(**(vars(self.args()) | dict(market_file=path, expected_market_revision=1))))
        def write(args):
            try: career_route.route(args); return 'saved'
            except ValueError as error:
                self.assertIn('changed', str(error)); return 'conflict'
        with ThreadPoolExecutor(2) as pool: results = list(pool.map(write, calls))
        self.assertCountEqual(results, ['saved', 'conflict'])
        self.assertEqual(self.read()['revision'], 2)

    def test_symbolic_link_decision_file_is_refused(self):
        self.save(self.context())
        path = self.root / 'learners/default/career-market.json'
        original = path.read_bytes(); target = path.with_name('original.json')
        target.write_bytes(original); path.unlink()
        try: path.symlink_to(target)
        except OSError:
            if os.name == 'nt': self.skipTest('Windows symlink privilege is unavailable')
            raise
        with self.assertRaises(ValueError): self.read()
        with self.assertRaises(ValueError): self.consent()
        self.assertEqual(target.read_bytes(), original)

    def test_known_answers_are_reused_and_refusal_is_not_reset(self):
        for role in ('Java Backend Developer', 'QA Engineer', 'DevOps'):
            with self.subTest(role=role):
                self.save(self.context(role=role)); self.consent('declined')
                result = self.save(self.context(role=role))
                self.assertFalse(result['should_offer']); self.assertFalse(result['may_search'])
                self.assertEqual(result['consent']['status'], 'declined')

    def sample(self):
        f = self.fixture; today = course.now()[:10]
        f.source('two', 'https://jobs.example/other/456',
                 'QA alternate position\nRequired: Java\nPreferred: SQL\n')
        second = f.posting('two', employer='Other QA employer', employer_job_id='456', observed_on=today)
        second['source']['source_id'] = 'two'
        card = f.card(as_of=today, postings=[f.posting(observed_on=today), second])
        card['requirements'][0]['evidence'].append(dict(card['requirements'][0]['evidence'][0], posting='two'))
        self.sample_card = card
        return f.save(card)

    def test_saved_evidence_repeated_requirements_and_freshness_are_preserved(self):
        sample = self.sample(); self.save(self.context()); self.consent()
        value = self.save(dict(action='result', status='recorded', note='Synthetic two-posting sample', sample_id='java'))
        self.assertTrue(value['basis_valid'])
        self.assertEqual(value['requirement_groups']['frequent'][0]['id'], 'java')
        self.assertEqual(value['sample_as_of'], sample['sample']['as_of'])
        self.assertTrue(value['requirement_groups']['frequent'][0]['sources'][0]['url'])
        from unittest.mock import patch
        with patch('course.now', return_value='2090-01-01T00:00:00Z'):
            old = self.read()
        self.assertFalse(old['basis_valid'])
        self.assertEqual(old['next_action'], 'offer_refresh_or_ordinary')
        self.assertEqual(old['requirement_groups']['frequent'], [])
        self.assertEqual(old['result']['sample_sha256'], value['result']['sample_sha256'])

    def test_preferred_skills_stay_additional_and_empty_evidence_is_insufficient(self):
        self.sample(); f = self.fixture
        card = self.sample_card
        card['requirements'] = card['requirements'] + [dict(id='sql', title='SQL', interpretation='QA optional skill',
            evidence=[dict(posting=p['id'], classification='preferred', start_line=3, end_line=3,
                           explanation='Explicit optional line in synthetic source') for p in card['postings']])]
        f.save(card); self.save(self.context()); self.consent()
        value = self.save(dict(action='result', status='recorded', note='QA optional skill', sample_id='java'))
        self.assertEqual([r['id'] for r in value['requirement_groups']['frequent']], ['java'])
        self.assertEqual([r['id'] for r in value['requirement_groups']['additional']], ['sql'])
        self.consent(); card['requirements'] = []; f.save(card)
        with self.assertRaisesRegex(ValueError, 'recurring'):
            self.save(dict(action='result', status='recorded', note='No supported skills', sample_id='java'))

    def test_changed_or_single_posting_sample_cannot_claim_recurring_demand(self):
        f = self.fixture; today = course.now()[:10]
        f.save(f.card(as_of=today, postings=[f.posting(observed_on=today)]))
        self.save(self.context()); self.consent()
        with self.assertRaises(ValueError):
            self.save(dict(action='result', status='recorded', note='QA single posting', sample_id='java'))
        self.sample()
        self.save(dict(action='result', status='recorded', note='QA', sample_id='java'))
        f.save(f.card(title='Changed basis', as_of=today, postings=[f.posting(observed_on=today)]))
        self.assertFalse(self.read()['basis_valid'])

    def test_sample_scope_must_match_and_denial_never_changes_existing_plan(self):
        self.sample(); self.save(self.context(region='Other region')); self.consent()
        before = (self.root / 'learners/default/career-market.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'scope'):
            self.save(dict(action='result', status='recorded', note='QA', sample_id='java'))
        self.assertEqual((self.root / 'learners/default/career-market.json').read_bytes(), before)
        import learning_intent
        def change(value):
            revision = learning_intent.view(self.args())['revision']
            learning_intent.commit(self.args(expected_revision=revision, reason='QA actual choice'), value)
        change(dict(goal=dict(description='QA career goal')))
        change(dict(proposal=dict(summary='Existing ordinary plan', steps=['Basics'])))
        change(dict(accept_proposal=True))
        intent_path = learning_intent.path_for(self.args()); old = intent_path.read_bytes()
        self.consent('declined')
        card = dict(summary='New proposal', sample_id='java',
                    learner_context=dict(self_report='Beginner', available_time=None, timeframe=None, unknowns=['Actual skills']),
                    steps=[dict(id='base', title='Basics', rationale='Prerequisite, not market inference',
                                kind='foundation', requirements=[], prerequisites=[], units=[])])
        with self.assertRaisesRegex(ValueError, 'agreed current market basis'):
            career_route.compile_route(self.args(), card)
        card['sample_id'] = None
        proposal = career_route.compile_route(self.args(), card)
        self.assertEqual(proposal['career']['steps'][0]['kind'], 'foundation')
        self.assertEqual(intent_path.read_bytes(), old)

    def test_search_result_is_pinned_across_process_restart(self):
        self.sample(); self.save(self.context()); self.consent()
        result = self.save(dict(action='result', status='recorded', note='QA two sources', sample_id='java'))
        code = ('import sys,json; from pathlib import Path; from types import SimpleNamespace as N; '
                'from career_route import route; print(json.dumps(route(N(course=Path(sys.argv[1]),'
                'profile="default"))["market_research"]))')
        done = subprocess.run([sys.executable, '-B', '-c', code, str(self.root)], timeout=15,
            capture_output=True, text=True, env=os.environ | {'PYTHONPATH': str(Path(course.__file__).parent)})
        self.assertEqual(done.returncode, 0, done.stderr)
        reloaded = json.loads(done.stdout)
        self.assertEqual(reloaded['result'], result['result'])
        self.assertTrue(reloaded['basis_valid']); self.assertFalse(reloaded['may_search'])
        self.assertEqual(reloaded['requirement_groups'], result['requirement_groups'])

    def test_market_decisions_never_change_goals_roadmap_or_progress(self):
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.save(self.context()); self.consent('declined')
        for name, raw in before.items(): self.assertEqual((self.root / name).read_bytes(), raw)
        self.assertFalse((self.root / 'learners/default/learning-intent.json').exists())
        self.assertFalse((self.root / 'learners/default/progress.sqlite3').exists())
