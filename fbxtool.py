"""
fbxtool -- バイナリ FBX の可逆パーサ / シリアライザ

FBX 7100〜7400 (32bit レコード) と 7500 以降 (64bit レコード) の両方を
透過的に扱う。無改変で parse -> serialize したとき、元ファイルとバイト単位で
完全一致することを設計目標としている (verify_roundtrip で検証可能)。

    import fbxtool
    doc = fbxtool.load("scene.fbx")
    for n in doc.top:
        print(n.name)
    doc.save("out.fbx")

ASCII 形式の FBX には対応しない (FbxAsciiError を送出)。
"""

import struct

__all__ = ['Node', 'Document', 'load', 'loads', 'verify_roundtrip',
           'FbxError', 'FbxAsciiError']

HDR = b'Kaydara FBX Binary  \x00'
MAGIC = bytes.fromhex('f85a8c6adef5d97eece90ce3758f290b')

# プロパティ型 -> (struct フォーマット, バイト数)
_SCALAR = {'Y': ('h', 2), 'C': ('?', 1), 'I': ('i', 4), 'F': ('f', 4),
           'D': ('d', 8), 'L': ('q', 8)}
_ARRAY = {'f': ('f', 4), 'd': ('d', 8), 'l': ('q', 8),
          'i': ('i', 4), 'b': ('b', 1), 'c': ('B', 1)}


class FbxError(Exception):
    pass


class FbxAsciiError(FbxError):
    pass


# --------------------------------------------------------------------------
class Node:
    """FBX のノードレコード 1 件。props は型文字を含む生バイト列で保持する。"""

    __slots__ = ('name', 'props', 'kids', 'term')

    def __init__(self, name, props=None, kids=None, term=False):
        self.name = name if isinstance(name, bytes) else name.encode()
        self.props = props if props is not None else []
        self.kids = kids if kids is not None else []
        self.term = term          # 子リストの NULL 終端レコードを持つか

    # ---- 読み出しヘルパ --------------------------------------------------
    def prop(self, i):
        """i 番目のプロパティを Python の値として返す (配列は展開しない)。"""
        p = self.props[i]
        t = chr(p[0])
        if t in _SCALAR:
            f, n = _SCALAR[t]
            return struct.unpack('<' + f, p[1:1 + n])[0]
        if t in 'SR':
            n = struct.unpack('<I', p[1:5])[0]
            return p[5:5 + n]
        if t in _ARRAY:
            return None                      # 配列は array() を使う
        raise FbxError('unknown property type %r' % t)

    def str(self, i, default=None):
        """i 番目のプロパティを bytes として返す。文字列でなければ default。"""
        try:
            v = self.prop(i)
        except (IndexError, FbxError):
            return default
        return v if isinstance(v, bytes) else default

    def text(self, i, default=''):
        """i 番目のプロパティを str として返す (デコード不能文字は置換)。"""
        v = self.str(i)
        return default if v is None else v.decode('utf-8', 'replace')

    def array(self, i):
        """i 番目の配列プロパティを tuple として展開する (zlib 展開込み)。"""
        import zlib
        p = self.props[i]
        t = chr(p[0])
        if t not in _ARRAY:
            raise FbxError('property %d is not an array (%r)' % (i, t))
        f, sz = _ARRAY[t]
        alen, enc, clen = struct.unpack('<III', p[1:13])
        raw = p[13:13 + clen]
        if enc == 1:
            raw = zlib.decompress(raw)
        return struct.unpack('<%d%s' % (alen, f), raw[:alen * sz])

    def array_len(self, i):
        """配列プロパティの要素数だけを、展開せずに取り出す。"""
        return struct.unpack('<I', self.props[i][1:5])[0]

    # ---- 探索 -----------------------------------------------------------
    def find(self, name):
        """名前が一致する最初の子を返す。無ければ None。"""
        name = name if isinstance(name, bytes) else name.encode()
        for k in self.kids:
            if k.name == name:
                return k
        return None

    def find_all(self, name):
        name = name if isinstance(name, bytes) else name.encode()
        return [k for k in self.kids if k.name == name]

    def walk(self):
        """自分と全子孫を深さ優先で列挙する。"""
        yield self
        for k in self.kids:
            for x in k.walk():
                yield x

    # ---- 書き出し --------------------------------------------------------
    def size(self, wide):
        hdr = 24 if wide else 12
        s = hdr + 1 + len(self.name)
        s += sum(len(p) for p in self.props)
        s += sum(k.size(wide) for k in self.kids)
        if self.term:
            s += hdr + 1
        return s

    def __repr__(self):
        return '<Node %s props=%d kids=%d>' % (
            self.name.decode('utf-8', 'replace'), len(self.props), len(self.kids))


# --------------------------------------------------------------------------
class Document:
    """FBX ファイル 1 本。top はトップレベルノードのリスト。"""

    def __init__(self, version, top, footer_code):
        self.version = version
        self.top = top
        self.footer_code = footer_code

    @property
    def wide(self):
        return self.version >= 7500

    # ---- よく使う入口 ----------------------------------------------------
    def section(self, name):
        name = name if isinstance(name, bytes) else name.encode()
        for n in self.top:
            if n.name == name:
                return n
        return None

    @property
    def objects(self):
        """Objects セクション直下のオブジェクトノード。"""
        s = self.section(b'Objects')
        return s.kids if s else []

    @property
    def connections(self):
        s = self.section(b'Connections')
        return s.kids if s else []

    def creator(self):
        n = self.section(b'Creator')
        return n.text(0) if n else ''

    def walk(self):
        for n in self.top:
            for x in n.walk():
                yield x

    def dumps(self, version=None):
        return _serialize(version or self.version, self.top, self.footer_code)

    def save(self, path, version=None):
        with open(path, 'wb') as f:
            f.write(self.dumps(version))


# --------------------------------------------------------------------------
def _prop_size(buf, pos):
    t = chr(buf[pos])
    if t in _SCALAR:
        return 1 + _SCALAR[t][1]
    if t in 'SR':
        return 5 + struct.unpack('<I', buf[pos + 1:pos + 5])[0]
    if t in _ARRAY:
        return 13 + struct.unpack('<I', buf[pos + 9:pos + 13])[0]
    raise FbxError('unknown property type %r at offset %d' % (t, pos))


def _parse_node(buf, pos, wide):
    if wide:
        end, nprop, plen = struct.unpack('<QQQ', buf[pos:pos + 24])
        pos += 24
    else:
        end, nprop, plen = struct.unpack('<III', buf[pos:pos + 12])
        pos += 12
    nl = buf[pos]
    pos += 1
    name = buf[pos:pos + nl]
    pos += nl
    if end == 0:
        return None, pos
    props = []
    for _ in range(nprop):
        n = _prop_size(buf, pos)
        props.append(buf[pos:pos + n])
        pos += n
    nullrec = b'\x00' * (25 if wide else 13)
    kids, term = [], False
    while pos < end:
        if buf[pos:pos + len(nullrec)] == nullrec:
            term = True
            pos += len(nullrec)
            break
        k, pos = _parse_node(buf, pos, wide)
        if k is None:
            term = True
            break
        kids.append(k)
    if pos != end:
        raise FbxError('node %r ends at %d, header says %d' % (name, pos, end))
    return Node(name, props, kids, term), end


def loads(data):
    """bytes から Document を作る。"""
    if data[:21] != HDR:
        if data.lstrip()[:1] == b';' or b'FBXHeaderExtension' in data[:4096]:
            raise FbxAsciiError('ASCII 形式の FBX には対応していません')
        raise FbxError('バイナリ FBX ではありません')
    version = struct.unpack('<I', data[23:27])[0]
    wide = version >= 7500
    nullrec = b'\x00' * (25 if wide else 13)
    pos, top = 27, []
    while True:
        if data[pos:pos + len(nullrec)] == nullrec:
            pos += len(nullrec)
            break
        n, pos = _parse_node(data, pos, wide)
        if n is None:
            break
        top.append(n)
    return Document(version, top, data[pos:pos + 16])


def load(path):
    with open(path, 'rb') as f:
        return loads(f.read())


def _write_node(out, node, offset, wide):
    end = offset + node.size(wide)
    plen = sum(len(p) for p in node.props)
    if wide:
        out.append(struct.pack('<QQQ', end, len(node.props), plen))
    else:
        out.append(struct.pack('<III', end, len(node.props), plen))
    out.append(bytes([len(node.name)]))
    out.append(node.name)
    out.extend(node.props)
    pos = offset + (24 if wide else 12) + 1 + len(node.name) + plen
    for k in node.kids:
        pos = _write_node(out, k, pos, wide)
    if node.term:
        nl = 25 if wide else 13
        out.append(b'\x00' * nl)
        pos += nl
    assert pos == end
    return pos


def _serialize(version, top, footer_code):
    wide = version >= 7500
    out = [HDR, b'\x1a\x00', struct.pack('<I', version)]
    pos = 27
    for n in top:
        pos = _write_node(out, n, pos, wide)
    nl = 25 if wide else 13
    out.append(b'\x00' * nl)
    pos += nl
    out.append(footer_code)
    pos += 16
    # 末尾: パディング + バージョン + 120 バイトのゼロ + マジック。
    # パディングはファイル全体が 16 の倍数になるよう 1〜16 バイト入れる。
    tail = 4 + 120 + 16
    pad = 16 - ((pos + tail) % 16) or 16
    out.append(b'\x00' * pad)
    out.append(struct.pack('<I', version))
    out.append(b'\x00' * 120)
    out.append(MAGIC)
    return b''.join(out)


def verify_roundtrip(path):
    """無改変の読み書きが元ファイルと一致するか調べる。(bool, 詳細) を返す。"""
    with open(path, 'rb') as f:
        src = f.read()
    out = load(path).dumps()
    if out == src:
        return True, 'identical (%d bytes)' % len(src)
    if len(out) != len(src):
        return False, 'size differs: %d -> %d' % (len(src), len(out))
    i = next(i for i, (a, b) in enumerate(zip(src, out)) if a != b)
    return False, 'first difference at byte %d' % i
