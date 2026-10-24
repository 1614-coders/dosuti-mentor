"""Subject refinements remain question two; interpretation belongs to the mentor."""
import unittest
import test_onboarding_dialog as fixtures
import learning_tools as lt


class OnboardingHierarchy(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.OnboardingDialog();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.save({'outcome':['profession']})

    def test_programming_then_backend_then_language_preserves_all_levels(self):
        state=self.f.save({'subject':dict(value='Программирование и разработка.',area='Программирование и разработка.',text='1',pending=True)})
        self.assertIn('Backend',state['onboarding_message']);self.assertNotIn('Java',state['onboarding_message'])
        state=self.f.save({'subject':dict(value='Backend',direction='Backend',text='Backend',pending=True)})
        self.assertEqual(state['onboarding_next'],'subject');self.assertIn('Java',state['onboarding_message'])
        self.assertIn('без выбранного языка',state['onboarding_message'])
        state=self.f.save({'subject':dict(value='Java Backend',focus='Java',text='Java')})
        record=state['onboarding_answers']['subject']
        self.assertEqual(record['area'],'Программирование и разработка.')
        self.assertEqual(record['direction'],'Backend');self.assertEqual(record['focus'],'Java')
        self.assertEqual(record['text'],'1\nBackend\nJava');self.assertEqual(state['onboarding_next'],'self_assessment')

    def test_general_profile_and_concrete_free_answer_do_not_force_language(self):
        for value in (dict(value='Backend без языка',area='Программирование и разработка.',direction='Backend',focus='Общий профиль',text='Пока без языка'),
                      dict(value='Java Backend',text='Java Backend')):
            state=self.f.save({'subject':value})
            self.assertEqual(state['onboarding_next'],'self_assessment')
            self.assertEqual(state['onboarding_answers']['subject']['text'],value['text'])

    def test_design_and_data_refine_specialization_before_tools(self):
        for area,direction,tool in [('Дизайн и творчество.','UI/UX','Figma'),('Аналитика и работа с данными.','Продуктовая аналитика','SQL')]:
            state=self.f.save({'subject':dict(value=area,area=area,text=area,pending=True)})
            self.assertNotIn(tool,state['onboarding_message'])
            state=self.f.save({'subject':dict(value=direction,direction=direction,text=direction,pending=True)})
            self.assertIn(tool,state['onboarding_message'])
            state=self.f.save({'subject':dict(value=direction+' '+tool,focus=tool,text=tool)})
            self.assertEqual(state['onboarding_answers']['subject']['direction'],direction)

    def test_pending_specialization_restores_in_new_chat_without_read_write(self):
        self.f.save({'subject':dict(value='Публичные выступления',area='Коммуникация',direction='Аргументация',text='Хочу убеждать',pending=True)})
        path=lt.pref_file(self.f.f.a,'profile');before=path.read_bytes()
        state=lt.preferences(self.f.f.args(session='another-chat'))
        self.assertIn('Аргументация',state['onboarding_message']);self.assertEqual(state['onboarding_next'],'subject')
        self.assertEqual(path.read_bytes(),before)

    def test_changing_area_does_not_keep_an_unrelated_pending_direction(self):
        self.f.save({'subject':dict(value='Backend',area='Программирование и разработка.',direction='Backend',text='Backend',pending=True)})
        state=self.f.save({'subject':dict(value='Дизайн и творчество.',area='Дизайн и творчество.',text='Хочу дизайн',pending=True)})
        self.assertNotIn('direction',state['onboarding_answers']['subject'])
        self.assertIn('UI/UX',state['onboarding_message']);self.assertNotIn('Java',state['onboarding_message'])


if __name__=='__main__':unittest.main()
