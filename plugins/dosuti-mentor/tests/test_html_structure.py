"""Extraction oracles: preserve meaning/structure, never execute or infer missing content."""
import sys
import unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from html_content import extract
import html_content

class HTMLStructure(unittest.TestCase):
    def test_optional_table_end_tags_keep_separate_row_metadata(self):
        text,_=extract('<table><tr><th>A<th>B<tr><td>C<td>D</table>')
        self.assertIn('[cells r1: c1=th; c2=th]',text)
        self.assertIn('[cells r2: c1=td; c2=td]',text)
        self.assertEqual(text.count('[cells r2:'),1)
        self.assertRegex(text,r'A\s+B');self.assertRegex(text,r'C\s+D')

    def test_table_boundaries_headers_and_spans(self):
        value='<table><caption>API</caption><tr><th id="op" scope="col">Method</th><th scope="col">Behavior</th></tr><tr><td headers="op" rowspan="2">put</td><td>replace</td></tr><tr><td>update</td></tr></table>'
        text,_=extract(value)
        self.assertRegex(text,r'Method\s+Behavior')
        self.assertRegex(text,r'put\s+replace')
        for token in ('API','scope="col"','headers="op"','rowspan="2"','update'):
            self.assertIn(token,text)

    def test_caveats_outside_article_and_ambiguous_aside_are_preserved(self):
        text,_=extract('<main><p>Definition before</p><article>Lesson</article><p>Not thread-safe</p></main><aside role="note">Only for version 17</aside>Unlabelled caution')
        for token in ('Definition before','Lesson','Not thread-safe','Only for version 17','Unlabelled caution'):
            self.assertIn(token,text)
        self.assertIn('review',text.lower())
        self.assertLess(text.index('Definition before'),text.index('Lesson'))

    def test_headings_lists_links_and_code(self):
        text,_=extract('<h2>Topic</h2><ol start="3"><li>First<ul><li>Nested</li></ul></li><li value="7">Second</li></ol><p><a href="/docs?a=1&amp;b=2">Source</a></p><pre><code class="language-python">if x &lt; 2:\n    print("```")\n</code></pre>')
        for token in ('## Topic','3. First','7. Second','  - Nested','/docs?a=1&b=2','if x < 2:\n    print("```")\n'):
            self.assertIn(token,text)
        self.assertIn('````python',text)

    def test_math_structure_tex_and_image_uncertainty(self):
        text,_=extract('<math><mfrac><mi>a</mi><mi>b</mi></mfrac></math><script type="math/tex">x^2 + y^2</script><img alt="Graph: rising line"><img src="/unknown.png">')
        self.assertIn('<mfrac><mi>a</mi><mi>b</mi></mfrac>',text)
        self.assertIn('x^2 + y^2',text)
        self.assertIn('Graph: rising line',text)
        self.assertIn('review',text.lower())

    def test_explicit_noise_hidden_and_unsafe_urls(self):
        text,_=extract('<nav>MENU</nav><div data-ad-slot="42">ADVERT</div><div hidden>HIDDEN</div><script type>EVIL()</script><p role aria-hidden><a href="javascript:EVIL()">Keep label</a><a href="https://example.test/ref">Safe</a></p>')
        for token in ('MENU','ADVERT','HIDDEN','EVIL()','javascript:'):
            self.assertNotIn(token,text)
        self.assertIn('Keep label',text);self.assertIn('https://example.test/ref',text)

    def test_nested_tables_and_missing_end_tags_do_not_duplicate(self):
        text,_=extract('<table><tr><td>Outer<table><tr><th>Inner</th></tr></table></td></tr></table><h3>Open heading')
        self.assertEqual(text.count('Outer'),1);self.assertEqual(text.count('Inner'),1)
        self.assertIn('### Open heading',text)

    def test_code_whitespace_and_inline_backticks(self):
        text,_=extract('<p>Use <code>a`b</code>.</p><pre>\n  x\t= 1\n\n</pre>')
        self.assertIn('``a`b``',text)
        self.assertIn('\n  x\t= 1\n\n',text)

    def test_deep_input_rejected_without_recursion_or_silent_truncation(self):
        with self.assertRaisesRegex(ValueError,'depth'):
            extract('<div>'*300+'fact'+'</div>'*300)

    def test_html_sup_sub_and_unsafe_links_preserve_labels(self):
        text,_=extract('<p>x<sup>2</sup> + a<sub>n</sub></p><a href="data:text/html,evil">Definition</a>')
        self.assertIn('x<sup>2</sup>',text);self.assertIn('a<sub>n</sub>',text)
        self.assertIn('Definition',text);self.assertNotIn('data:text',text)

    def test_input_and_output_bounds_fail_explicitly(self):
        with patch.object(html_content,'MAX_BYTES',100):
            with self.assertRaisesRegex(ValueError,'input'):extract('я'*51)
            with self.assertRaisesRegex(ValueError,'output'):extract('<article>'+'x'*75+'</article>')

    def test_declared_span_is_not_expanded_into_invented_values(self):
        text,_=extract('<table><tr><th colspan="2">Total</th></tr><tr><td>A</td><td>B</td></tr></table>')
        self.assertEqual(text.count('Total'),1);self.assertIn('colspan="2"',text)
        self.assertRegex(text,r'A\s+B')

    def test_empty_markup_is_not_invented_readable_content(self):
        self.assertEqual(extract('<article><h2></h2><table></table><pre></pre></article>')[0],'')
        text,_=extract('<img src="/diagram.png">')
        self.assertIn('/diagram.png',text);self.assertIn('review original',text)

    def test_math_fences_and_fraction_attributes_survive(self):
        text,_=extract('<math><mfenced open="[" close="]" separators=";"><mfrac linethickness="0"><mi>a</mi><mi>b</mi></mfrac></mfenced></math>')
        for token in ('open="["','close="]"','separators=";"','linethickness="0"'):
            self.assertIn(token,text)

    def test_definition_and_reversed_lists_do_not_invent_order(self):
        text,_=extract('<dl><dt>Term</dt><dd>Definition</dd></dl><ol reversed><li>A</li><li>B</li></ol><ol reversed start="5"><li>C</li><li>D</li></ol>')
        self.assertRegex(text,r'Term\s+Definition')
        self.assertIn('review original numbering',text);self.assertNotIn('1. A',text)
        self.assertIn('5. C',text);self.assertIn('4. D',text)

if __name__=='__main__':unittest.main()
