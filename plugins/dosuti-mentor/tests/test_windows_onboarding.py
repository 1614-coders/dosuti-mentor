"""Real local CLI onboarding persistence; this is not a model-dialogue test."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import test_unit_learning
import course

ROOT=Path(__file__).resolve().parents[3]
PWSH=os.environ.get('DOSUTI_TEST_PWSH') or shutil.which('pwsh') or shutil.which('powershell')
ANSWERS=dict(outcome=['profession'],subject='Java backend',self_assessment='basic_experience',
    learning_format='step_by_step',assistant_role='mentor',check_frequency='each_block',
    explanation_detail='balanced',constraints=dict(selected=['study_time'],details='3–5 часов в неделю'))


class InstallerSafety(unittest.TestCase):
    def test_installer_does_not_assign_powershell_automatic_home(self):
        script=(ROOT/'Install-DoSuti.ps1').read_text()
        # PowerShell names are case-insensitive. Catch the Windows failure even
        # on hosts without PowerShell; native script execution is a separate test.
        self.assertIsNone(re.search(r'(?im)^\s*\$home\s*=',script),
                          'Installer overwrites the read-only PowerShell $HOME variable')

    @unittest.skipUnless(PWSH,'PowerShell unavailable; Windows installer execution unverified')
    def test_installer_forwards_real_parameters_without_downloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);shutil.copy(ROOT/'Install-DoSuti.ps1',root/'Install-DoSuti.ps1')
            spec=root/'plugins/dosuti-mentor/references/windows-python.json'
            spec.parent.mkdir(parents=True);spec.write_text('{}')
            (root/'Windows-Runtime.ps1').write_text('''
function Initialize-DoSutiHome([string]$HomePath) { return $HomePath }
function Resolve-DoSutiPython([string]$HomePath,[string]$Explicit,$Spec,[switch]$Offline) {
 if($Explicit -ne 'fixture-python' -or -not $Offline) {throw 'parameters not forwarded'}
 return [pscustomobject]@{executable='Invoke-QAPython'}
}
function Invoke-QAPython {
 $args | ConvertTo-Json -Compress
 $global:LASTEXITCODE=0
}
''')
            literal=lambda p:"'"+str(p).replace("'","''")+"'"
            code="[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $env:PROCESSOR_ARCHITECTURE='AMD64'; & "+literal(root/'Install-DoSuti.ps1')+" -HomePath "+literal(root/'данные с пробелами')+" -Python fixture-python -Offline"
            result=subprocess.run([PWSH,'-NoProfile','-NonInteractive','-Command',code],capture_output=True,encoding='utf-8',timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            args=json.loads(result.stdout.strip());self.assertEqual(args[-2:],['--home',str(root/'данные с пробелами')])
            self.assertEqual(args[0],'-B')
            invalid=subprocess.run([PWSH,'-NoProfile','-NonInteractive','-Command',code+' -Check'],
                                   capture_output=True,encoding='utf-8',timeout=20)
            self.assertNotEqual(invalid.returncode,0,'Unknown installer flag must fail before setup')


class WindowsOnboarding(unittest.TestCase):
    def setUp(self):
        self.f=test_unit_learning.UnitLearning();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.script=Path(course.__file__)

    def cli(self,command,*options,session='chat-a'):
        argv=[sys.executable,'-B',str(self.script),command,'--course',str(self.f.f.f.path),'--profile','pilot']
        if command!='settings':argv+=['--session',session]
        result=subprocess.run(argv+list(map(str,options)),capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        return json.loads(result.stdout)

    def answer(self,name,value):
        path=self.f.f.f.directory/'onboarding-turn.json'
        path.write_text(json.dumps(dict(answers={name:value}),ensure_ascii=False),encoding='utf-8')
        return self.cli('settings','--onboarding-file',path)

    def complete(self):
        for name,value in ANSWERS.items():self.answer(name,value)

    def diagnostic_choice(self,value):
        path=self.f.f.f.directory/'diagnostic-choice.json';path.write_text(json.dumps(value))
        return self.cli('diagnostic','--update-file',path,'--expected-revision','0','--reason','Explicit QA learner choice')

    def test_empty_project_can_connect_new_course_before_first_answer(self):
        project=self.f.f.f.directory/'новое обучение';project.mkdir()
        root=project/'.dosuti-course';connection=self.script.with_name('connection.py')
        commands=[
            [str(self.script),'init','--course',str(root),'--title','Моё обучение'],
            [str(connection),'--project',str(project),'--action','connect','--scope','project',
             '--course',str(root),'--profile','default','--expected-revision','0'],
            [str(self.script),'settings','--course',str(root),'--profile','default']]
        old={str(p):p.read_bytes() for p in self.f.f.f.path.rglob('*') if p.is_file()}
        for args in commands:
            result=subprocess.run([sys.executable,'-B']+args,capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
        prefs=json.loads(result.stdout);self.assertEqual(prefs['onboarding_next'],'outcome')
        self.assertEqual(prefs['onboarding'],'not_asked')
        self.assertEqual(old,{str(p):p.read_bytes() for p in self.f.f.f.path.rglob('*') if p.is_file()})

    def test_eight_answers_saved_one_by_one_and_read_in_fresh_processes(self):
        state=self.cli('settings');self.assertEqual(state['onboarding'],'not_asked')
        state=self.cli('settings','--onboarding','in_progress')
        self.assertEqual(state['onboarding_answers'],{})
        for number,(name,value) in enumerate(ANSWERS.items(),1):
            self.assertEqual(state['onboarding_next'],name)
            written=self.answer(name,value)
            state=self.cli('settings')  # A new interpreter, no inherited chat memory.
            self.assertEqual(state,written)
            self.assertEqual(state['onboarding_answers'][name],dict(status='answered',value=value))
            self.assertEqual(len(state['onboarding_answers']),number)
        self.assertEqual(state['onboarding'],'complete');self.assertIsNone(state['onboarding_next'])
        self.assertEqual(self.cli('diagnostic')['status'],'not_started')
        self.assertEqual(state['effective'],__import__('learning_tools').DEFAULTS)

    def test_skip_pause_new_chat_and_edit_do_not_repeat_questions(self):
        self.answer('outcome',['course']);self.cli('settings','--onboarding-skip','subject')
        path=self.f.f.f.path/'learners/pilot/preferences.json';before=path.read_bytes()
        state=self.cli('settings');self.assertEqual(before,path.read_bytes())
        self.assertEqual(state['onboarding'],'in_progress');self.assertEqual(state['onboarding_next'],'self_assessment')
        # A new chat resolves the same explicitly connected project course.
        project=self.f.f.f.directory/'project';project.mkdir()
        connection=self.script.with_name('connection.py')
        for action in ('connect','status'):
            argv=[sys.executable,'-B',str(connection),'--project',str(project),'--action',action]
            if action=='connect':argv+=['--scope','project','--course',str(self.f.f.f.path),'--profile','pilot','--expected-revision','0']
            else:argv+=['--chat','chat-b']
            result=subprocess.run(argv,capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            bound=json.loads(result.stdout)['binding'];self.assertEqual(bound['profile'],'pilot')
        edited=self.answer('subject','Java')
        self.assertEqual(edited['onboarding_next'],'self_assessment')
        self.assertEqual(edited['onboarding_answers']['outcome']['value'],['course'])

    def test_completion_only_offers_diagnostic_until_explicit_acceptance(self):
        self.complete();path=self.f.f.f.path/'learners/pilot/diagnostic-learning.json'
        self.assertFalse(path.exists());self.cli('settings');self.assertFalse(path.exists())
        state=self.diagnostic_choice(dict(action='accept',competences=['Map keys'],next_difficulty='basic'))
        self.assertEqual(state['status'],'accepted');self.assertEqual(state['current']['answers'],[])

    def test_declining_diagnostic_keeps_onboarding_and_allows_learning(self):
        self.complete();state=self.diagnostic_choice(dict(action='decline'))
        self.assertEqual(state['status'],'declined')
        self.assertEqual(self.cli('diagnostic',session='chat-b')['status'],'declined')
        prefs=self.cli('settings');self.assertEqual(prefs['onboarding'],'complete');self.assertIsNone(prefs['onboarding_next'])
        lesson=self.cli('learn-unit','--unit-id','maps',session='chat-b')
        self.assertTrue(lesson['learning']['valid'])

    def test_legacy_completed_and_whole_skip_do_not_restart(self):
        path=self.f.f.f.path/'learners/pilot/preferences.json';path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(dict(values={},onboarding='complete')));before=path.read_bytes()
        state=self.cli('settings');self.assertEqual(state['onboarding_pending'],[]);self.assertEqual(before,path.read_bytes())
        path.unlink();self.answer('outcome',['topics']);state=self.cli('settings','--onboarding','skipped')
        self.assertEqual(state['onboarding_pending'],[]);self.assertIsNone(state['onboarding_next'])
        self.assertEqual(state['onboarding_answers']['outcome']['value'],['topics'])

    def test_editing_legacy_finished_answer_does_not_reopen_other_questions(self):
        path=self.f.f.f.path/'learners/pilot/preferences.json';path.parent.mkdir(parents=True,exist_ok=True)
        for state in ('complete','skipped'):
            with self.subTest(state=state):
                path.write_text(json.dumps(dict(values={},onboarding=state)))
                result=self.answer('subject','Java')
                self.assertEqual(result['onboarding'],state)
                self.assertEqual(result['onboarding_pending'],[]);self.assertIsNone(result['onboarding_next'])
                self.assertEqual(set(result['onboarding_answers']),{'subject'})
                self.assertEqual(self.cli('settings'),result)


if __name__=='__main__':unittest.main()
