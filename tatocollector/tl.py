"""Bounded decoder for explicit, statically recovered TL declarations.

No evaluation or dynamic code generation. Unknown constructors stop decoding;
this module never guesses how many bytes an unknown object occupies.
"""

from dataclasses import dataclass
from pathlib import Path
import re
import struct


class TLDecodeError(ValueError):
    def __init__(self, reason, offset, constructor=None):
        self.reason, self.offset, self.constructor = reason, offset, constructor
        suffix = f' at offset {offset}'
        if constructor is not None:
            suffix += f', constructor {constructor:08x}'
        super().__init__(reason + suffix)


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    flag: str | None = None
    bit: int | None = None


@dataclass(frozen=True)
class Definition:
    name: str
    constructor: int
    fields: tuple[Field, ...]
    result: str


class Schema:
    def __init__(self, text=''):
        self.definitions = {}
        self.add('boolFalse#bc799737 = Bool; boolTrue#997275b5 = Bool;')
        self.add(text)

    def add(self, text):
        text = text.split('---functions---', 1)[0]
        text = re.sub(r'//[^\n]*', '', text)
        text = text.replace('---types---', '')
        declarations = re.findall(r'([A-Za-z_][\w.]*)#([0-9a-fA-F]{1,8})\s*(.*?)\s*=\s*([\w.]+)\s*;', text, re.S)
        pending = {}
        for name, constructor, body, result in declarations:
            flags, fields = set(), []
            for token in body.split():
                if ':' not in token:
                    raise ValueError('unsupported TL declaration token')
                field_name, kind = token.split(':', 1)
                owner, bit = None, None
                if '?' in kind:
                    condition, kind = kind.split('?', 1)
                    owner, bit_text = condition.rsplit('.', 1)
                    if owner not in flags or not bit_text.isdigit() or not 0 <= int(bit_text) <= 31:
                        raise ValueError('unresolved TL flag reference')
                    bit = int(bit_text)
                if kind == '#':
                    flags.add(field_name)
                fields.append(Field(field_name, kind, owner, bit))
            definition = Definition(name, int(constructor, 16), tuple(fields), result)
            existing = pending.get(definition.constructor, self.definitions.get(definition.constructor))
            if existing is not None and existing != definition:
                raise ValueError(f'conflicting constructor {definition.constructor:08x}')
            pending[definition.constructor] = definition
        self.definitions.update(pending)

    @classmethod
    def from_files(cls, paths):
        schema = cls()
        for path in paths:
            schema.add(Path(path).read_text(encoding='utf-8'))
        return schema

    def decode_prefix(self, data, expected_type=None):
        reader = _Reader(data, self)
        result = reader.object(expected_type, 0)
        return result, reader.offset

    def decode(self, data, expected_type=None):
        result, consumed = self.decode_prefix(data, expected_type)
        if consumed != len(data):
            raise TLDecodeError('trailing_data', consumed)
        return result


class _Reader:
    def __init__(self, data, schema):
        if not isinstance(data, bytes) or len(data) > 1_048_576:
            raise TLDecodeError('input_limit', 0)
        self.data, self.schema, self.offset, self.nodes = data, schema, 0, 0

    def read(self, size):
        end = self.offset + size
        if size < 0 or end > len(self.data):
            raise TLDecodeError('truncated', self.offset)
        value = self.data[self.offset:end]
        self.offset = end
        return value

    def uint(self):
        return struct.unpack('<I', self.read(4))[0]

    def blob(self):
        size = self.read(1)[0]
        prefix = 1
        if size == 254:
            size = int.from_bytes(self.read(3), 'little')
            prefix = 4
            if size < 254:
                raise TLDecodeError('noncanonical_string', self.offset)
        elif size == 255:
            raise TLDecodeError('invalid_string_prefix', self.offset)
        if size > 65536:
            raise TLDecodeError('string_limit', self.offset)
        data = self.read(size)
        if any(self.read(-(prefix + size) % 4)):
            raise TLDecodeError('string_padding', self.offset)
        return data

    def value(self, kind, depth):
        if kind == 'int':
            return struct.unpack('<i', self.read(4))[0]
        if kind == '#':
            return self.uint()
        if kind == 'long':
            return struct.unpack('<q', self.read(8))[0]
        if kind == 'double':
            return struct.unpack('<d', self.read(8))[0]
        if kind in ('int128', 'int256'):
            return self.read(16 if kind == 'int128' else 32)
        if kind in ('string', 'bytes'):
            data = self.blob()
            if kind == 'bytes':
                return data
            try:
                return data.decode('utf-8')
            except UnicodeDecodeError:
                raise TLDecodeError('invalid_utf8', self.offset) from None
        if kind == 'true':
            return True
        if kind.startswith('Vector<') and kind.endswith('>'):
            if self.uint() != 0x1CB5C415:
                raise TLDecodeError('vector_marker', self.offset - 4)
            count = self.uint()
            if count > 4096:
                raise TLDecodeError('vector_limit', self.offset - 4)
            return [self.value(kind[7:-1], depth + 1) for _ in range(count)]
        return self.object(kind, depth + 1)

    def object(self, expected_type, depth):
        self.nodes += 1
        if depth > 64 or self.nodes > 10000:
            raise TLDecodeError('complexity_limit', self.offset)
        position = self.offset
        constructor = self.uint()
        definition = self.schema.definitions.get(constructor)
        if definition is None:
            raise TLDecodeError('unknown_constructor', position, constructor)
        if expected_type and definition.result != expected_type:
            raise TLDecodeError('unexpected_union_type', position, constructor)
        values = {'_': definition.name}
        for field in definition.fields:
            if field.flag and not values[field.flag] & (1 << field.bit):
                if field.type == 'true':
                    values[field.name] = False
                continue
            values[field.name] = self.value(field.type, depth)
        return values
