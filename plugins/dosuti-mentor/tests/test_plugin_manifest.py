"""Portable package and Codex fallback must expose the same release and setup."""
import json
from pathlib import Path, PurePosixPath
import re
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
REPO = PLUGIN.parents[1]
MANIFESTS = ('plugin.json', '.codex-plugin/plugin.json')
COMMANDS = {'compact-lecture', 'plan-career', 'prepare-lecture', 'research-topic',
            'review-homework', 'review-learning', 'setup-dosuti', 'start-learning',
            'teach-lecture', 'teach-project', 'teach-unit'}


def read(relative):
    return json.loads((PLUGIN / relative).read_text(encoding='utf-8'))


def skill_name(path):
    match = re.search(r'^name:\s*([^\n]+)$', path.read_text(encoding='utf-8'), re.M)
    return match.group(1).strip() if match else None


class PluginManifest(unittest.TestCase):
    def test_portable_manifest_keeps_schema_and_identity(self):
        manifest = read('plugin.json')
        self.assertEqual(manifest['$schema'], 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json')
        self.assertEqual(manifest['name'], 'dosuti-mentor')
        self.assertRegex(manifest['version'], r'^\d+\.\d+\.\d+$')
        self.assertTrue(manifest['description'].strip())

    def test_codex_fallback_agrees_with_portable_release_identity(self):
        portable, fallback = map(read, MANIFESTS)
        for key in ('name', 'version'):
            with self.subTest(field=key): self.assertEqual(portable[key], fallback[key])

    def test_both_installation_entrypoints_are_technical_setup(self):
        for name in MANIFESTS:
            with self.subTest(manifest=name):
                self.assertEqual(read(name)['extensions']['com.openai']['onboardingSkill'],
                                 './skills/setup-dosuti/SKILL.md')

    def test_entrypoints_resolve_from_plugin_root_and_are_packaged_skills(self):
        for name in MANIFESTS:
            relative = read(name)['extensions']['com.openai']['onboardingSkill']
            self.assertTrue(relative.startswith('./'))
            self.assertNotIn('..', PurePosixPath(relative).parts)
            path = (PLUGIN / relative).resolve()
            self.assertTrue(path.is_relative_to(PLUGIN.resolve()))
            self.assertEqual(path.name, 'SKILL.md')
            self.assertEqual(skill_name(path), path.parent.name)

    def test_compatibility_skill_directory_preserves_existing_commands(self):
        self.assertEqual(read('.codex-plugin/plugin.json')['skills'], './skills/')
        names = [skill_name(p) for p in (PLUGIN / 'skills').glob('*/SKILL.md')]
        self.assertEqual(len(names), len(set(names)))
        self.assertLessEqual(COMMANDS, set(names))

    def test_setup_and_educational_start_remain_separate_skills(self):
        setup = PLUGIN / 'skills/setup-dosuti/SKILL.md'
        start = PLUGIN / 'skills/start-learning/SKILL.md'
        self.assertEqual(skill_name(setup), 'setup-dosuti')
        self.assertEqual(skill_name(start), 'start-learning')
        self.assertIn('Не вызывай start-learning', setup.read_text(encoding='utf-8'))
        self.assertIn('$start-learning', start.read_text(encoding='utf-8'))
        for name in ('bootstrap.py', 'setup_local.py', 'Install-DoSuti.ps1', 'Windows-Runtime.ps1'):
            self.assertTrue((PLUGIN / 'scripts' / name).is_file(), name)

    def test_marketplace_keeps_identity_and_points_to_package_root(self):
        marketplace = json.loads((REPO / '.agents/plugins/marketplace.json').read_text(encoding='utf-8'))
        self.assertEqual(marketplace['name'], 'dosuti-mentor-local')
        entry = next(row for row in marketplace['plugins'] if row['name'] == read('plugin.json')['name'])
        self.assertEqual(entry['source']['source'], 'local')
        self.assertEqual((REPO / entry['source']['path']).resolve(), PLUGIN.resolve())

    def test_release_headings_match_manifest_version(self):
        version = read('plugin.json')['version']
        readme = (REPO / 'README.md').read_text(encoding='utf-8')
        changelog = (REPO / 'CHANGELOG.md').read_text(encoding='utf-8')
        self.assertIn('## Подготовка версии ' + version + ':', readme)
        first = re.search(r'^## (\d+\.\d+\.\d+)\b', changelog, re.M)
        self.assertIsNotNone(first)
        self.assertEqual(first.group(1), version)


if __name__ == '__main__': unittest.main()
