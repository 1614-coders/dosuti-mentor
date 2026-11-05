"""Moved reference pages must remain reachable from the user guide and packaged skills."""
from pathlib import Path
import re
import unittest
from urllib.parse import unquote, urlsplit

REPO = Path(__file__).resolve().parents[3]


def anchors(path):
    text = path.read_text(encoding='utf-8')
    result = set(re.findall(r'(?:id|name)=["\']([^"\']+)', text))
    repeated = {}
    for heading in re.findall(r'^#{1,6}\s+(.+?)\s*#*$', text, re.M):
        slug = re.sub(r'[^\w\- ]', '', re.sub(r'[*`~]', '', heading).lower()).replace(' ', '-')
        count = repeated.get(slug, 0)
        result.add(slug + ('-' + str(count) if count else ''))
        repeated[slug] = count + 1
    return result


class DocumentationLinks(unittest.TestCase):
    def test_local_documents_and_fragments_resolve(self):
        pages = list(REPO.glob('*.md'))
        for folder in ('docs', 'plugins/dosuti-mentor', 'qa-reports'):
            pages.extend((REPO / folder).rglob('*.md'))
        checked = 0
        for page in pages:
            fenced = False
            for number, line in enumerate(page.read_text(encoding='utf-8').splitlines(), 1):
                if line.lstrip().startswith(('```', '~~~')):
                    fenced = not fenced
                    continue
                if fenced:
                    continue
                for match in re.finditer(r'!?\[[^\]\n]*\]\(([^)\n]+)\)', line):
                    target = match.group(1).strip().strip('<>')
                    url = urlsplit(target)
                    if url.scheme or target.startswith('//'):
                        continue
                    with self.subTest(page=str(page.relative_to(REPO)), line=number, target=target):
                        path = (page.parent / unquote(url.path)).resolve() if url.path else page
                        self.assertTrue(path.is_relative_to(REPO), 'Local link must be portable within the repository')
                        self.assertTrue(path.exists(), 'Linked document is missing')
                        if url.fragment and path.suffix == '.md':
                            self.assertIn(unquote(url.fragment), anchors(path), 'Linked section is missing')
                    checked += 1
        self.assertGreater(checked, 0)


if __name__ == '__main__':
    unittest.main()
