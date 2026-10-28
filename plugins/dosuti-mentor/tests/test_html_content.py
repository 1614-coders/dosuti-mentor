import sys,unittest,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from html_content import extract
from material_sources import text_content

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
