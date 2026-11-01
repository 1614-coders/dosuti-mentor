import sys,unittest,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from html_content import extract
from material_sources import text_content

class PlainTextWhitespace(unittest.TestCase):
    def check_text(self, value, expected=None):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'material.txt';raw=value.encode('utf-8');path.write_bytes(raw)
            self.assertEqual(text_content(path),(value if expected is None else expected,'utf8_text'))
            self.assertEqual(path.read_bytes(),raw)

    def test_python_first_line_and_nested_indentation(self):
        self.check_text('    if ready:\n        for item in items:\n            process(item)\n')

    def test_tabs_and_mixed_indentation_are_not_expanded(self):
        self.check_text('\tif ready:\n\t\tprocess()\n\t    # original mixed indentation\n')

    def test_java_indented_method_and_nested_blocks(self):
        self.check_text('    void run() {\n        if (ready) {\n            work();\n        }\n    }\n')

    def test_mixed_prose_fenced_code_and_paragraphs(self):
        self.check_text('\n  Example:\n\n```python\n    if ready:\n        work()\n```\n\nNext paragraph.\n\n')

    def test_blank_lines_and_horizontal_whitespace_are_preserved(self):
        self.check_text('\n\n    first()  \n\n\tsecond()\t\n\n\n')

    def test_terminal_newline_normalization_keeps_other_line_endings(self):
        for value,expected in [('  code','  code\n'),('  code  ','  code  \n'),
            ('\tfirst\r\n\r\n\tsecond\r\n','\tfirst\r\n\r\n\tsecond\n'),
            ('  code\r','  code\n')]:
            with self.subTest(value=value):self.check_text(value,expected)

    def test_utf8_bom_does_not_remove_following_indentation(self):
        self.check_text('\ufeff    first()\n    second()\n','    first()\n    second()\n')

    def test_empty_and_whitespace_only_material_is_still_rejected(self):
        for value in ('',' ','\n\t \r\n'):
            with self.subTest(value=value),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'empty.txt';path.write_text(value)
                raw=path.read_bytes()
                with self.assertRaisesRegex(ValueError,'No readable text'):text_content(path)
                self.assertEqual(path.read_bytes(),raw)

class HTMLContent(unittest.TestCase):
    def test_readme_over_main_and_navigation(self):
        text,method=extract('<nav>Sign in</nav><main><nav>Repository menu</nav><article class="markdown-body"><h2>Java</h2><pre>if (x) {\n    call();\n}</pre></article>Footer</main>')
        self.assertEqual(method,'html_markdown_body');self.assertNotIn('menu',text);self.assertNotIn('Sign in',text);self.assertIn('    call();',text)
    def test_article_over_main(self):
        text,method=extract('<header>Site</header><main>Menu<article><header><h1>Topic</h1></header><p>Lesson</p></article>Other</main>')
        self.assertEqual(method,'html_article');self.assertIn('Topic',text);self.assertIn('Other',text);self.assertIn('review',text)
    def test_main_fallback(self):
        text,method=extract('<header>Site</header><main><nav>Links</nav><p>Task</p></main><footer>Site footer</footer>')
        self.assertEqual(method,'html_main');self.assertIn('Task',text);self.assertNotIn('Links',text);self.assertIn('Site footer',text);self.assertIn('review',text)
    def test_sibling_articles_not_silently_discarded(self):
        text,method=extract('<article>Part one</article><article>Part two</article>');self.assertIn('Part one',text);self.assertIn('Part two',text)
    def test_nested_regions_no_duplicate(self):
        text,_=extract('<article>A<article>B</article>C</article>');self.assertEqual(text.count('B'),1)
    def test_hidden_regions_and_void_nodes(self):
        text,_=extract('<article>One<br/>Two<input hidden><span aria-hidden="true">Hidden</span><script>evil()</script><aside>Sidebar</aside><p>Three &amp; four</p></article>')
        self.assertIn('Three & four',text);self.assertNotIn('Hidden',text);self.assertIn('Sidebar',text);self.assertIn('review',text);self.assertNotIn('evil',text)
    def test_unclosed_article(self):
        self.assertIn('Text',extract('<article><p>Text')[0])
    def test_plain_page_fallback(self):
        text,method=extract('<nav>Menu</nav><p>Useful</p>');self.assertEqual(method,'html_visible_text_filtered');self.assertNotIn('Menu',text)
    def test_empty_preferred_region_falls_back(self):
        text,method=extract('<article></article><main>Actual content</main>');self.assertEqual(method,'html_main');self.assertIn('Actual',text)
    def test_markdown_code_html_entities_preserved(self):
        text,_=extract('<article class="markdown-body"><pre>Map&lt;String, Integer&gt; map;\n  a &lt; b &amp;&amp; c &gt; d;</pre></article>');self.assertIn('Map<String, Integer>',text);self.assertIn('  a < b && c > d;',text)
    def test_text_file_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'x.txt';p.write_bytes(b'Line\n    code\n');self.assertEqual(text_content(p),('Line\n    code\n','utf8_text'))

    def test_text_file_crlf_interior_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'x.txt';raw=b'Line\r\n    code\r\n';p.write_bytes(raw)
            self.assertEqual(text_content(p),('Line\r\n    code\n','utf8_text'))
            self.assertEqual(p.read_bytes(),raw)
