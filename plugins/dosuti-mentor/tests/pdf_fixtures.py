"""Small real, offline PDF fixtures; standard PDF fonts, no third-party fixture builder."""
import zlib

def text(value,x=72,y=720,font='F1',size=12):
    value=value.replace('\\','\\\\').replace('(','\\(').replace(')','\\)')
    return f'BT /{font} {size} Tf {x} {y} Td ({value}) Tj ET\n'

def document(pages, *, compressed=False, catalog_extra=b''):
    objects=[b'',b'',b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
             b'<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>']
    image=zlib.compress(bytes([128]*64))
    objects.append(b'<< /Type /XObject /Subtype /Image /Width 8 /Height 8 /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode /Length '+str(len(image)).encode()+b' >>\nstream\n'+image+b'\nendstream')
    kids=[]
    for content in pages:
        data=content.encode('latin-1');data=zlib.compress(data) if compressed else data;page_id=len(objects)+1;kids.append(page_id)
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> /XObject << /Im0 5 0 R >> >> /Contents {page_id+1} 0 R >>'.encode())
        objects.append(b'<< /Length '+str(len(data)).encode()+(b' /Filter /FlateDecode' if compressed else b'')+b' >>\nstream\n'+data+b'\nendstream')
    objects[0]=b'<< /Type /Catalog /Pages 2 0 R '+catalog_extra+b' >>'
    objects[1]=('<< /Type /Pages /Count '+str(len(kids))+' /Kids ['+' '.join(f'{k} 0 R' for k in kids)+'] >>').encode()
    output=bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n');offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(output));output.extend(f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n')
    start=len(output);output.extend(f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]:output.extend(f'{offset:010} 00000 n \n'.encode())
    output.extend(f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode())
    return bytes(output)

def textbook():
    return document([text('Map basics',size=18)+text('A key identifies a value.',y=680)+
        text('1. Add a key.',y=650)+text('2. Replace its value.',y=630)+
        text('Not thread-safe.',y=580), text('Practice',size=18)+text('Explain replacement.',y=680)])

def code():
    return document([text('Python example',size=18)+text('if count < 2:',y=680,font='F2',size=10)+
                     text('    print(count)',y=667,font='F2',size=10)])

def table():
    content=text('Method table',y=750,size=18)
    for x in (72,222,422):content+=f'{x} 600 m {x} 700 l S\n'
    for y in (600,650,700):content+=f'72 {y} m 422 {y} l S\n'
    content+=text('Method',x=80,y=675)+text('Behavior',x=230,y=675)
    content+=text('put',x=80,y=625)+text('replace',x=230,y=625)
    return document([content])

def scan():return document(['q 200 0 0 200 72 500 cm /Im0 Do Q\n'])


def encrypted_empty():
    # Standard Security Handler revision 2, valid empty user password, zero-page PDF.
    import hashlib
    import struct
    from cryptography.hazmat.decrepit.ciphers.algorithms import ARC4
    from cryptography.hazmat.primitives.ciphers import Cipher
    padding=bytes.fromhex('28bf4e5e4e758a4164004e56fffa01082e2e00b6d0683e802f0ca9fe6453697a')
    def rc4(key,value):return Cipher(ARC4(key),mode=None).encryptor().update(value)
    owner=rc4(hashlib.md5(padding).digest()[:5],padding)
    identity=b'dosuti-pdf-test!'
    key=hashlib.md5(padding+owner+struct.pack('<i',-4)+identity).digest()[:5]
    user=rc4(key,padding)
    encryption=(b'/Encrypt << /Filter /Standard /V 1 /R 2 /Length 40 /P -4 /O <'+owner.hex().encode()+
                b'> /U <'+user.hex().encode()+b'> >> /ID [<'+identity.hex().encode()+b'><'+identity.hex().encode()+b'>] ')
    return document([]).replace(b'trailer\n<< ',b'trailer\n<< '+encryption)
