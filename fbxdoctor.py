#!/usr/bin/env python3
"""
fbxdoctor -- FBX の整合性検査と修復

    python3 fbxdoctor.py inspect  scene.fbx
    python3 fbxdoctor.py audit    scene.fbx
    python3 fbxdoctor.py audit    "exports/*.fbx"
    python3 fbxdoctor.py fix      "exports/*.fbx" -o fixed/

MotionBuilder や他の DCC がファイルを開けない / 読み込み中に固まるとき、
原因になりやすい構造上の破綻を洗い出して修復する。
"""

import argparse
import glob
import math
import os
import struct
import sys
from collections import Counter, defaultdict

import fbxtool
from fbxtool import FbxError, FbxAsciiError

# Definitions の ObjectType 名は Objects のノード名と一致するが、
# FbxCharacter だけはノード名 Constraint / クラス名 Character になる。
CLASSMAP = {b'Constraint': b'Character'}

SEVERITY = {'fatal': 0, 'warn': 1, 'info': 2}


class Issue:
    def __init__(self, code, severity, target, detail, fixable=False):
        self.code, self.severity = code, severity
        self.target, self.detail, self.fixable = target, detail, fixable

    def __str__(self):
        mark = {'fatal': '!!', 'warn': ' !', 'info': '  '}[self.severity]
        fix = ' [修復可]' if self.fixable else ''
        return '%s %-18s %-38s %s%s' % (
            mark, self.code, self.target[:38], self.detail, fix)


# --------------------------------------------------------------------------
def obj_name(o):
    v = o.str(1) or b''
    return v.split(b'\x00\x01')[0].decode('utf-8', 'replace')


def obj_subtype(o):
    return o.str(2) or b''


def obj_id(o):
    return o.prop(0)


def is_mesh(o):
    return o.name == b'Geometry' and obj_subtype(o) == b'Mesh'


def layer_elements(geo):
    """{(型名, TypedIndex): ノード} を返す。"""
    out = {}
    for k in geo.kids:
        if not k.name.startswith(b'LayerElement') or k.name == b'LayerElement':
            continue
        ti = k.find(b'TypedIndex')
        out[(k.name.decode()[12:], ti.prop(0) if ti else 0)] = k
    return out


def mapping_of(le):
    m = le.find(b'MappingInformationType')
    r = le.find(b'ReferenceInformationType')
    return (m.text(0) if m else '', r.text(0) if r else '')


# --------------------------------------------------------------------------
def audit(doc):
    issues = []
    objs = doc.objects
    ids = {obj_id(o) for o in objs if o.props}

    # --- 1. 面を 1 枚も持たないメッシュ ---------------------------------
    # MotionBuilder はこれをマージ中に掴むと停止することがある。
    for o in objs:
        if not is_mesh(o):
            continue
        v = o.find(b'Vertices')
        if v is not None and o.find(b'PolygonVertexIndex') is None:
            issues.append(Issue(
                'no_polygons', 'fatal', obj_name(o),
                '頂点 %d 個だがポリゴンが皆無' % (v.array_len(0) // 3), True))

    # --- 2. Layer が存在しない LayerElement を指している ------------------
    for o in objs:
        if o.name != b'Geometry':
            continue
        avail = set(layer_elements(o))
        for lay in o.find_all(b'Layer'):
            for le in lay.find_all(b'LayerElement'):
                t = le.find(b'Type')
                ti = le.find(b'TypedIndex')
                if t is None or ti is None:
                    continue
                key = (t.text(0)[12:], ti.prop(0))
                if key not in avail:
                    issues.append(Issue(
                        'dangling_layer', 'warn', obj_name(o),
                        '存在しない %s[%d] を参照' % key, True))

    # --- 3. レイヤ要素の配列長 / 索引範囲 ---------------------------------
    for o in objs:
        if not is_mesh(o):
            continue
        pvi = o.find(b'PolygonVertexIndex')
        vt = o.find(b'Vertices')
        if pvi is None or vt is None:
            continue
        nv = vt.array_len(0) // 3
        idx = pvi.array(0)
        npv, npoly = len(idx), sum(1 for x in idx if x < 0)
        top = max((x if x >= 0 else -x - 1) for x in idx) if idx else -1
        if top >= nv:
            issues.append(Issue('index_range', 'fatal', obj_name(o),
                                '頂点索引 %d >= 頂点数 %d' % (top, nv)))
        sizes = Counter()
        run = 0
        for x in idx:
            run += 1
            if x < 0:
                sizes[run] += 1
                run = 0
        if run:
            issues.append(Issue('unclosed_poly', 'fatal', obj_name(o),
                                '最終ポリゴンが閉じていない'))
        if sizes[1] or sizes[2]:
            issues.append(Issue('degenerate_poly', 'warn', obj_name(o),
                                '退化ポリゴン %d 面' % (sizes[1] + sizes[2])))

        expect = {'ByPolygonVertex': npv, 'ByPolygon': npoly, 'ByVertice': nv,
                  'ByControlPoint': nv, 'AllSame': 1}
        for (typ, ti), le in layer_elements(o).items():
            m, r = mapping_of(le)
            exp = expect.get(m)
            if exp is None:
                continue
            spec = {'Normal': (b'Normals', 3, None),
                    'UV': (b'UV', 2, b'UVIndex'),
                    'Color': (b'Colors', 4, b'ColorIndex'),
                    'Material': (b'Materials', 1, None)}.get(typ)
            if spec is None:
                continue
            dname, comp, iname = spec
            dnode = le.find(dname)
            if dnode is None:
                continue
            ndata = dnode.array_len(0) // comp
            if r == 'IndexToDirect' and iname:
                inode = le.find(iname)
                if inode is None:
                    issues.append(Issue('layer_index', 'warn', obj_name(o),
                                        '%s が IndexToDirect だが索引配列なし' % typ))
                    continue
                arr = inode.array(0)
                if len(arr) != exp:
                    issues.append(Issue('layer_length', 'warn', obj_name(o),
                                        '%s 索引 %d 個 / 期待 %d' % (typ, len(arr), exp)))
                if arr and max(arr) >= ndata:
                    issues.append(Issue('layer_index', 'fatal', obj_name(o),
                                        '%s 索引最大 %d >= 実体数 %d' % (typ, max(arr), ndata)))
            elif ndata != exp:
                issues.append(Issue('layer_length', 'warn', obj_name(o),
                                    '%s (%s) %d 個 / 期待 %d' % (typ, m, ndata, exp)))

    # --- 4. マテリアル索引と接続マテリアル数の食い違い ---------------------
    mat_of, geo_of = defaultdict(list), defaultdict(list)
    kind = {obj_id(o): o.name for o in objs if o.props}
    for c in doc.connections:
        if c.str(0) != b'OO':
            continue
        s, d = c.prop(1), c.prop(2)
        if kind.get(s) == b'Material':
            mat_of[d].append(s)
        elif kind.get(s) == b'Geometry':
            geo_of[d].append(s)
    for o in objs:
        if not is_mesh(o):
            continue
        gid = obj_id(o)
        models = [m for m, gs in geo_of.items() if gid in gs]
        nmat = sum(len(mat_of.get(m, [])) for m in models)
        for (typ, _), le in layer_elements(o).items():
            if typ != 'Material':
                continue
            d = le.find(b'Materials')
            if d is None:
                continue
            arr = d.array(0)
            if arr and (max(arr) >= nmat or min(arr) < 0):
                issues.append(Issue('material_index', 'fatal', obj_name(o),
                                    'マテリアル索引 %d..%d / 接続 %d 個'
                                    % (min(arr), max(arr), nmat)))

    # --- 5. 中身が空のスキンクラスタ ---------------------------------------
    empty = [o for o in objs
             if o.name == b'Deformer' and obj_subtype(o) == b'Cluster'
             and not any(k.name == b'Indexes' and k.array_len(0) > 0
                         for k in o.kids)]
    if empty:
        issues.append(Issue('empty_cluster', 'info', '(scene)',
                            '空のスキンクラスタ %d 個 / 全 %d 個'
                            % (len(empty), sum(1 for o in objs
                                               if o.name == b'Deformer'
                                               and obj_subtype(o) == b'Cluster')),
                            True))

    # --- 6. 参照切れの接続 --------------------------------------------------
    dang = sum(1 for c in doc.connections for i in (1, 2)
               if isinstance(c.prop(i), int) and c.prop(i) and c.prop(i) not in ids)
    if dang:
        issues.append(Issue('dangling_conn', 'fatal', '(scene)',
                            '存在しないオブジェクトを指す接続 %d 本' % dang, True))

    # --- 7. 非 ASCII のプロパティ名 / エクスポータの残骸 --------------------
    nonascii, junk = Counter(), Counter()
    for n in doc.walk():
        if n.name != b'Properties70':
            continue
        for p in n.find_all(b'P'):
            nm = p.str(0)
            if nm is None:
                continue
            if any(b > 127 for b in nm):
                nonascii[nm] += 1
            for i in range(3, len(p.props)):
                v = p.str(i)
                if v and v.startswith(b'<bpy id prop:'):
                    junk[nm] += 1
                    break
    if nonascii:
        issues.append(Issue('nonascii_prop', 'info', '(scene)',
                            '非 ASCII 名のプロパティ %d 個 (%s ほか)'
                            % (sum(nonascii.values()),
                               list(nonascii)[0].decode('utf-8', 'replace')), True))
    if junk:
        issues.append(Issue('exporter_junk', 'info', '(scene)',
                            'Blender の残骸プロパティ %d 個' % sum(junk.values()), True))

    # --- 8. 非有限値 --------------------------------------------------------
    nan = 0
    for n in doc.walk():
        for i, p in enumerate(n.props):
            t = chr(p[0])
            if t in 'FD':
                v = struct.unpack('<' + ('f' if t == 'F' else 'd'),
                                  p[1:1 + (4 if t == 'F' else 8)])[0]
                if not math.isfinite(v):
                    nan += 1
            elif t in 'fd':
                try:
                    nan += sum(1 for v in n.array(i) if not math.isfinite(v))
                except Exception:
                    pass
    if nan:
        issues.append(Issue('non_finite', 'fatal', '(scene)',
                            'NaN / Inf の値 %d 個' % nan))

    issues.sort(key=lambda x: SEVERITY[x.severity])
    return issues


# --------------------------------------------------------------------------
def drop_objects(doc, pred):
    """条件に合うオブジェクトと、それに触れる接続をまとめて削除する。"""
    sec = doc.section(b'Objects')
    dead, keep = set(), []
    for o in sec.kids:
        if o.props and pred(o):
            dead.add(obj_id(o))
        else:
            keep.append(o)
    sec.kids = keep
    cs = doc.section(b'Connections')
    if cs:
        cs.kids = [c for c in cs.kids
                   if not any(c.prop(i) in dead for i in (1, 2))]
    return len(dead)


def prune_connections(doc):
    ids = {obj_id(o) for o in doc.objects if o.props}
    cs = doc.section(b'Connections')
    if not cs:
        return 0
    before = len(cs.kids)
    cs.kids = [c for c in cs.kids
               if all(not (isinstance(c.prop(i), int) and c.prop(i)
                           and c.prop(i) not in ids) for i in (1, 2))]
    return before - len(cs.kids)


def fix_definitions(doc):
    """残ったオブジェクトから Definitions の宣言数を再計算する。"""
    cnt = Counter(CLASSMAP.get(o.name, o.name) for o in doc.objects if o.props)
    sec = doc.section(b'Definitions')
    if sec is None:
        return
    keep = []
    for d in sec.kids:
        if d.name == b'Count':
            d.props[0] = b'I' + struct.pack('<i', sum(cnt.values()) + 1)
            keep.append(d)
        elif d.name == b'ObjectType':
            cls = d.str(0)
            if cls == b'GlobalSettings':
                keep.append(d)
            elif cnt.get(cls, 0):
                c = d.find(b'Count')
                if c is not None:
                    c.props[0] = b'I' + struct.pack('<i', cnt[cls])
                keep.append(d)
        else:
            keep.append(d)
    sec.kids = keep


def repair(doc, opts):
    log = []

    if opts.no_polygons:
        n = drop_objects(doc, lambda o: (is_mesh(o) and o.find(b'Vertices') is not None
                                         and o.find(b'PolygonVertexIndex') is None))
        if n:
            log.append('面なしメッシュ %d 個を除去' % n)

    if opts.dangling_layer:
        n = 0
        for o in doc.objects:
            if o.name != b'Geometry':
                continue
            avail = set(layer_elements(o))
            for lay in o.find_all(b'Layer'):
                keep = []
                for le in lay.kids:
                    if le.name != b'LayerElement':
                        keep.append(le)
                        continue
                    t, ti = le.find(b'Type'), le.find(b'TypedIndex')
                    if t is not None and ti is not None and \
                            (t.text(0)[12:], ti.prop(0)) not in avail:
                        n += 1
                    else:
                        keep.append(le)
                lay.kids = keep
        if n:
            log.append('壊れたレイヤ参照 %d 件を除去' % n)

    if opts.empty_clusters:
        n = drop_objects(doc, lambda o: (
            o.name == b'Deformer' and obj_subtype(o) == b'Cluster'
            and not any(k.name == b'Indexes' and k.array_len(0) > 0 for k in o.kids)))
        if n:
            log.append('空のスキンクラスタ %d 個を除去' % n)

    if opts.junk_props:
        n = 0
        for node in doc.walk():
            if node.name != b'Properties70':
                continue
            keep = []
            for p in node.kids:
                nm = p.str(0) if p.name == b'P' else None
                bad = False
                if nm is not None:
                    if any(b > 127 for b in nm):
                        bad = True
                    else:
                        for i in range(3, len(p.props)):
                            v = p.str(i)
                            if v and v.startswith(b'<bpy id prop:'):
                                bad = True
                                break
                if bad:
                    n += 1
                else:
                    keep.append(p)
            node.kids = keep
        if n:
            log.append('不正なカスタムプロパティ %d 個を除去' % n)

    n = prune_connections(doc)
    if n:
        log.append('参照切れの接続 %d 本を除去' % n)
    fix_definitions(doc)
    return log


# --------------------------------------------------------------------------
def expand(patterns):
    out = []
    for p in patterns:
        if os.path.isdir(p):
            out += sorted(glob.glob(os.path.join(p, '*.fbx')))
        else:
            hits = sorted(glob.glob(p))
            out += hits if hits else [p]
    return out


def cmd_inspect(args):
    for path in expand(args.files):
        print('=' * 70)
        print(path)
        try:
            doc = fbxtool.load(path)
        except (FbxError, OSError) as e:
            print('  読み込み失敗:', e)
            continue
        cnt = Counter()
        for o in doc.objects:
            st = obj_subtype(o).decode() if o.props and len(o.props) > 2 else ''
            cnt[o.name.decode() + (':' + st if st else '')] += 1
        keys = verts = polys = 0
        for o in doc.objects:
            if is_mesh(o):
                v, p = o.find(b'Vertices'), o.find(b'PolygonVertexIndex')
                verts += v.array_len(0) // 3 if v else 0
                polys += p.array_len(0) if p else 0
            elif o.name == b'AnimationCurve':
                k = o.find(b'KeyTime')
                keys += k.array_len(0) if k else 0
        print('  FBX バージョン : %d (%s bit レコード)'
              % (doc.version, '64' if doc.wide else '32'))
        print('  作成            : %s' % doc.creator())
        si = doc.section(b'FBXHeaderExtension')
        si = si.find(b'SceneInfo') if si else None
        if si:
            md = si.find(b'MetaData')
            if md:
                for f in (b'Title', b'Author', b'Comment'):
                    n = md.find(f)
                    if n and n.text(0):
                        print('  %-14s: %s' % (f.decode(), n.text(0)))
        print('  オブジェクト数  : %d / 接続 %d' % (len(doc.objects), len(doc.connections)))
        print('  頂点 %d / ポリゴン索引 %d / アニメキー %d' % (verts, polys, keys))
        print('  内訳:')
        for k, v in cnt.most_common():
            print('      %-28s %d' % (k, v))
        ok, msg = fbxtool.verify_roundtrip(path)
        print('  可逆性チェック  : %s (%s)' % ('OK' if ok else 'NG', msg))


def cmd_audit(args):
    worst = 0
    for path in expand(args.files):
        try:
            doc = fbxtool.load(path)
        except (FbxError, OSError) as e:
            print('%s\n   読み込み失敗: %s\n' % (path, e))
            worst = 2
            continue
        issues = audit(doc)
        shown = [i for i in issues if SEVERITY[i.severity] <= (2 if args.all else 1)]
        n_fatal = sum(1 for i in issues if i.severity == 'fatal')
        print('%s  -- %s' % (path, '問題なし' if not issues else
                             '%d 件 (致命 %d)' % (len(issues), n_fatal)))
        for i in shown[:args.limit]:
            print('   ' + str(i))
        if len(shown) > args.limit:
            print('   ... 他 %d 件 (--limit で増やせます)' % (len(shown) - args.limit))
        print()
        worst = max(worst, 2 if n_fatal else (1 if issues else 0))
    return worst


def cmd_fix(args):
    os.makedirs(args.out, exist_ok=True)
    for path in expand(args.files):
        try:
            doc = fbxtool.load(path)
        except (FbxError, OSError) as e:
            print('%s -> スキップ (%s)' % (path, e))
            continue
        log = repair(doc, args)
        base, ext = os.path.splitext(os.path.basename(path))
        dst = os.path.join(args.out, base + args.suffix + ext)
        doc.save(dst)
        left = [i for i in audit(fbxtool.load(dst)) if i.severity == 'fatal']
        print('%s -> %s' % (path, dst))
        for l in log:
            print('     ' + l)
        if not log:
            print('     修復対象なし (そのまま書き出し)')
        if left:
            print('     ! 残存する致命的問題 %d 件' % len(left))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='fbxdoctor', description='FBX の整合性検査と修復')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('inspect', help='中身の概要と可逆性を表示')
    p.add_argument('files', nargs='+')
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser('audit', help='問題を検出する (書き換えない)')
    p.add_argument('files', nargs='+')
    p.add_argument('-a', '--all', action='store_true', help='info レベルも表示')
    p.add_argument('-n', '--limit', type=int, default=40)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser('fix', help='修復版を書き出す')
    p.add_argument('files', nargs='+')
    p.add_argument('-o', '--out', default='fixed', help='出力先ディレクトリ')
    p.add_argument('--suffix', default='_fixed')
    p.add_argument('--no-polygons', action='store_true', default=True,
                   help='面のないメッシュを除去 (既定で有効)')
    p.add_argument('--keep-no-polygons', dest='no_polygons',
                   action='store_false')
    p.add_argument('--dangling-layer', action='store_true', default=True,
                   help='壊れたレイヤ参照を除去 (既定で有効)')
    p.add_argument('--keep-dangling-layer', dest='dangling_layer',
                   action='store_false')
    p.add_argument('--empty-clusters', action='store_true',
                   help='空のスキンクラスタを除去 (読み込み高速化)')
    p.add_argument('--junk-props', action='store_true',
                   help='非 ASCII 名 / エクスポータ残骸のプロパティを除去')
    p.set_defaults(func=cmd_fix)

    args = ap.parse_args(argv)
    try:
        return args.func(args) or 0
    except FbxAsciiError as e:
        print('エラー:', e, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
