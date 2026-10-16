"""Conservative static content-region selection; preserve code whitespace and raw HTML."""
from html.parser import HTMLParser

VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
BLOCK = {'p','div','br','li','h1','h2','h3','h4','h5','h6','pre','section','tr','article','main','blockquote','ul','ol','table'}
HIDDEN = {'script','style','noscript','template','nav','aside'}


class ContentHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[];self.stack=[];self.regions=[]

    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        inherited=bool(self.stack and self.stack[-1]['hidden'])
        hidden=(inherited or tag in HIDDEN or 'hidden' in attrs or attrs.get('aria-hidden','').lower()=='true'
                or attrs.get('role','').lower() in ('navigation','complementary'))
        if tag in BLOCK and not hidden:self.parts.append('\n')
        rank=None
        if not hidden:
            if 'markdown-body' in (attrs.get('class') or '').split():rank=0
            elif tag=='article':rank=1
            elif tag=='main' or attrs.get('role','').lower()=='main':rank=2
        if tag not in VOID:self.stack.append(dict(tag=tag,hidden=hidden,rank=rank,start=len(self.parts)))

    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in VOID:self.handle_endtag(tag)

    def handle_endtag(self, tag):
        index=next((i for i in range(len(self.stack)-1,-1,-1) if self.stack[i]['tag']==tag),None)
        if index is not None:
            for row in self.stack[index:]:
                if row['rank'] is not None:self.regions.append((row['rank'],row['start'],len(self.parts)))
            hidden=self.stack[index]['hidden'];del self.stack[index:]
            if tag in BLOCK and not hidden:self.parts.append('\n')

    def handle_data(self, data):
        if not self.stack or not self.stack[-1]['hidden']:self.parts.append(data)

    def result(self):
        self.close()
        for row in self.stack:
            if row['rank'] is not None:self.regions.append((row['rank'],row['start'],len(self.parts)))
        candidates=[r for r in self.regions if ''.join(self.parts[r[1]:r[2]]).strip()]
        if not candidates:return ''.join(self.parts),'html_visible_text_filtered'
        rank=min(r[0] for r in candidates)
        ranges=sorted((left,right) for priority,left,right in candidates if priority==rank)
        selected=[]
        for left,right in ranges:
            if selected and left<=selected[-1][1]:selected[-1]=(selected[-1][0],max(selected[-1][1],right))
            else:selected.append((left,right))
        return '\n\n'.join(''.join(self.parts[a:b]) for a,b in selected),('html_markdown_body','html_article','html_main')[rank]


def extract(value):
    parser=ContentHTML();parser.feed(value);return parser.result()
