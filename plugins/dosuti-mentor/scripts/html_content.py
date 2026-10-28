"""Bounded static HTML extraction; preserve structure and ambiguous visible content."""
from html import escape
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlsplit

VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
BLOCK = {'p','div','br','li','h1','h2','h3','h4','h5','h6','pre','section','tr','article','main','blockquote','ul','ol','dl','dt','dd','table','header','footer','aside','figure','details','summary'}
HIDDEN = {'script','style','noscript','template','nav','head'}
MATH = {'math','mrow','mi','mn','mo','mtext','mfrac','msqrt','mroot','msup','msub','msubsup','mover','munder','munderover','mtable','mtr','mtd','mfenced','semantics','annotation','menclose','mspace','mpadded','mstyle','mmultiscripts','mprescripts','none','sup','sub'}
MATH_ATTRS = {'display','mathvariant','encoding','rowspan','columnspan','open','close','separators','linethickness','notation','rowalign','columnalign','stretchy','accent','accentunder','fence','separator','form','bevelled'}
MAX_BYTES = 32 * 1024 * 1024


def fence(text):
    return '`' * max(3, max((len(x)+1 for x in re.findall(r'`+',text)),default=0))


def safe_url(value):
    if not value or any(c.isspace() or ord(c)<32 for c in value):return None
    try:scheme=urlsplit(value).scheme.lower()
    except ValueError:return None
    return value if scheme in ('','http','https','mailto') else None


class ContentHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[];self.stack=[];self.ranks=set();self.size=0;self.ambiguous=False;self.text_count=0

    def emit(self,text):
        self.size+=len(text.encode('utf-8'))
        if self.size>MAX_BYTES:raise ValueError('HTML extracted output exceeds 32 MiB')
        if len(self.parts)>=500000:raise ValueError('HTML structure exceeds fragment limit')
        self.parts.append(text)

    def ancestors(self,tag):
        return [row for row in self.stack if row['tag']==tag]

    def handle_starttag(self,tag,attrs):
        # HTML permits omitted cell/row end tags. Close only inside the nearest table.
        if tag in ('tr','td','th') and not self.ancestors('pre'):
            table=next((i for i in range(len(self.stack)-1,-1,-1) if self.stack[i]['tag']=='table'),None)
            if table is not None and not self.stack[-1]['hidden']:
                ends=('tr',) if tag=='tr' else ('td','th')
                previous=next((row['tag'] for row in reversed(self.stack[table+1:]) if row['tag'] in ends),None)
                if previous:self.handle_endtag(previous)
        attrs=dict(attrs)
        if len(self.stack)>=256:raise ValueError('HTML nesting depth exceeds 256')
        tex=tag=='script' and (attrs.get('type') or '').lower() in ('math/tex','math/tex; mode=display')
        hidden=(bool(self.stack and self.stack[-1]['hidden']) or (tag in HIDDEN and not tex)
                or 'hidden' in attrs or (attrs.get('aria-hidden') or '').lower()=='true'
                or (attrs.get('role') or '').lower() in ('navigation','advertisement')
                or 'data-ad-slot' in attrs or 'data-ad-client' in attrs
                or 'adsbygoogle' in (attrs.get('class') or '').split())
        row=dict(tag=tag,hidden=hidden,start=len(self.parts),attrs=attrs,tex=tex,text_at=self.text_count)
        if not hidden:
            if 'markdown-body' in (attrs.get('class') or '').split():row['rank']=0
            elif tag=='article':row['rank']=1
            elif tag=='main' or (attrs.get('role') or '').lower()=='main':row['rank']=2
            in_pre=bool(self.ancestors('pre'))
            if in_pre:
                if tag=='code':self.ancestors('pre')[-1]['language']=next((x[9:] for x in (attrs.get('class') or '').split() if re.fullmatch(r'language-[\w+-]+',x)),'')
            elif tag in MATH:
                self.emit('<'+tag+''.join(' '+k+'="'+escape(v,quote=True)+'"' for k,v in attrs.items() if k in MATH_ATTRS and v is not None)+'>')
            else:
                if tag in BLOCK:self.emit('\n')
                if re.fullmatch('h[1-6]',tag):self.emit('#'*int(tag[1])+' ')
                if tag=='li':
                    lists=[x for x in self.stack if x['tag'] in ('ul','ol')]
                    prefix='- '
                    if lists and lists[-1]['tag']=='ol':
                        parent=lists[-1];value=attrs.get('value',str(parent['next']))
                        if re.fullmatch(r'-?\d{1,9}',value or ''):parent['next']=int(value)
                        if parent['next'] is not None:
                            prefix=str(parent['next'])+'. ';parent['next']+=parent['step']
                    self.emit('  '*max(0,len(lists)-1)+prefix)
                if tag=='ol':
                    value=attrs.get('start','1') or '1';row['next']=int(value) if re.fullmatch(r'-?\d{1,9}',value) else 1
                    row['step']=-1 if 'reversed' in attrs else 1
                    if 'reversed' in attrs and 'start' not in attrs:
                        row['next']=None;self.emit('[ordered list reversed: review original numbering]\n')
                if tag=='dd':self.emit('  ')
                if tag=='table':self.emit('[table]\n');row.update(number=0,cells=[])
                if tag=='caption':self.emit('[caption] ')
                if tex:self.emit('\n[math/tex]\n')
                tables=self.ancestors('table')
                if tables and tag=='tr':tables[-1]['number']+=1;tables[-1]['cells']=[]
                if tables and tag in ('td','th'):
                    table=tables[-1]
                    if table['cells']:self.emit('\t')
                    meta=tag+''.join(' '+k+'='+json.dumps(v,ensure_ascii=False) for k,v in attrs.items() if k in ('id','headers','scope','rowspan','colspan','abbr') and v is not None)
                    table['cells'].append(meta)
                if tag=='img':
                    url=safe_url(attrs.get('src'))
                    self.emit('[image: '+(attrs.get('alt') or 'no description')+('; URL: '+url if url else '')+'; review original]')
                    self.text_count+=1;self.ambiguous=True
                if tag=='aside' or attrs.get('role')=='complementary':self.ambiguous=True
        if tag not in VOID:self.stack.append(row)

    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in VOID:self.handle_endtag(tag)

    def finish(self,row):
        tag=row['tag']
        if row['hidden'] or self.ancestors('pre'):return
        if 'rank' in row and self.text_count>row['text_at']:self.ranks.add(row['rank'])
        if tag=='pre' or tag=='code':
            text=''.join(self.parts[row['start']:]);del self.parts[row['start']:]
            self.size-=len(text.encode('utf-8'))
            marker=fence(text) if tag=='pre' else '`'*max(1,max((len(x)+1 for x in re.findall(r'`+',text)),default=0))
            if tag=='pre':self.emit(marker+row.get('language','')+'\n'+text+('' if text.endswith('\n') else '\n')+marker+'\n')
            else:self.emit(marker+text+marker)
        elif tag in MATH:self.emit('</'+tag+'>')
        elif row['tex']:self.emit('\n[/math/tex]\n')
        elif tag=='a':
            url=safe_url(row['attrs'].get('href'))
            if url:self.emit(' (URL: '+url+')')
        elif tag=='tr':
            tables=self.ancestors('table')
            if tables:
                table=tables[-1]
                self.emit('\n[cells r'+str(table['number'])+': '+'; '.join('c'+str(i)+'='+v for i,v in enumerate(table['cells'],1))+']')
        elif tag=='table':self.emit('\n[/table]')
        if tag in BLOCK:self.emit('\n')

    def handle_endtag(self,tag):
        index=next((i for i in range(len(self.stack)-1,-1,-1) if self.stack[i]['tag']==tag),None)
        if index is None:return
        while len(self.stack)>index:
            row=self.stack.pop();self.finish(row)

    def handle_data(self,data):
        if self.stack and self.stack[-1]['hidden']:return
        if data.strip():self.text_count+=1
        if self.stack and self.stack[-1]['tex']:
            self.emit(data);return
        self.emit(escape(data,quote=False) if self.ancestors('math') and not self.ancestors('pre') else data)

    def result(self):
        self.close()
        while self.stack:
            self.finish(self.stack.pop())
        method=('html_markdown_body','html_article','html_main')[min(self.ranks)] if self.ranks else 'html_visible_text_filtered'
        if not self.text_count:return '',method
        # Unlabelled text outside article/main is not safely classifiable as site noise.
        if self.ranks or self.ambiguous:
            self.emit('\n[review: visible supplementary content retained; verify source boundaries]\n')
        return ''.join(self.parts),method


def extract(value):
    if len(value.encode('utf-8'))>MAX_BYTES:raise ValueError('HTML input exceeds 32 MiB')
    parser=ContentHTML();parser.feed(value);return parser.result()
