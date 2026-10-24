"""Freeze manually reviewed examples; this does not grade question semantics."""
import json
from pathlib import Path
import unittest
import test_diagnostic_assessment as fixtures

EXPECTED=[
    'Map<String,Integer> m = new HashMap<>(); m.put("a",2); m.put("a",3); Какие значения вернут m.get("a") и m.size()? Объясни ход выполнения.',
    'Map<String,Integer> m = new HashMap<>(); m.put("a",2); m.put("b",3); Какие ключи и значения находятся в m после этих операций?',
    'После изменения часов работы библиотеки число посещений выросло со 100 до 140 в неделю. Какие выводы можно обосновать этими данными и что ещё ты хотел бы выяснить?',
]


class NeutralDiagnosticPrompts(unittest.TestCase):
    def test_reviewed_example_is_the_prompt_not_the_hidden_rubric(self):
        f=fixtures.DiagnosticAssessment();f.setUp();self.addCleanup(f.doCleanups)
        text=(Path(__file__).parents[1]/'references/diagnostic-neutral-prompts.md').read_text()
        examples=[json.loads(part.split('\n```',1)[0]) for part in text.split('```json\n')[1:]]
        self.assertEqual([e['prompt'] for e in examples],EXPECTED)
        for example in examples:
            f.d.save(dict(action='complete'))
            f.d.save(dict(action='accept',competences=['QA example'],question_policy='fixed_15_v1',
                selection_policy='adaptive_v2',competence_prerequisites={},next_difficulty=example['difficulty']))
            state=f.d.save(dict(action='prompt',competence='QA example',**example,
                rubric=dict(action='Explain the example',required=['Justify the stated result'],critical_errors=[])))
            self.assertEqual(state['current']['pending_prompt']['prompt'],example['prompt'])
            self.assertEqual(state['current']['answers'],[])


if __name__=='__main__':unittest.main()
