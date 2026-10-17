"""Visit a root array item by item, retaining only other root fields.

The standard JSON decoder handles nested values. Large individual values still
need a contiguous buffer; this is projection, not a general JSON database.
"""
import json


class Reader:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.buffer = ''
        self.pos = 0
        self.ended = False
        self.decoder = json.JSONDecoder()

    def fill(self):
        if self.ended:
            return False
        try:
            chunk = next(self.chunks)
        except StopIteration:
            self.ended = True
            return False
        self.buffer = self.buffer[self.pos:] + chunk
        self.pos = 0
        return True

    def char(self):
        while True:
            while self.pos < len(self.buffer) and self.buffer[self.pos] in ' \t\r\n':
                self.pos += 1
            if self.pos < len(self.buffer):
                return self.buffer[self.pos]
            if not self.fill():
                return ''

    def take(self, char):
        if self.char() != char:
            raise ValueError('Invalid projected JSON structure')
        self.pos += 1

    def value(self):
        self.char()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.pos)
            except ValueError:
                if self.fill():
                    continue
                raise
            # A number or literal can continue in the next chunk. Do not accept
            # a valid prefix until a delimiter or actual EOF has been observed.
            if end == len(self.buffer) and not self.ended:
                if self.fill():
                    continue
            elif end < len(self.buffer) and self.buffer[end] not in ' \t\r\n,:]}':
                if self.fill():
                    continue
                raise ValueError('Invalid projected JSON token')
            self.pos = end
            return value


def visit_array_chunks(chunks, field, visitor):
    reader = Reader(chunks)
    reader.take('{')
    result = {}
    count = 0
    if reader.char() != '}':
        while True:
            key = reader.value()
            if not isinstance(key, str) or key in result:
                raise ValueError('Invalid or duplicate projected JSON key')
            reader.take(':')
            if key == field:
                reader.take('[')
                if reader.char() != ']':
                    while True:
                        item = reader.value()
                        visitor(item)
                        count += 1
                        del item
                        if reader.char() == ']':
                            break
                        reader.take(',')
                reader.take(']')
                result[key] = []
            else:
                result[key] = reader.value()
            if reader.char() == '}':
                break
            reader.take(',')
    reader.take('}')
    if reader.char():
        raise ValueError('Trailing projected JSON content')
    return result, count


def visit_array(text, field, visitor):
    return visit_array_chunks([text], field, visitor)
