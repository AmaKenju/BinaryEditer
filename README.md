# fbxtool / fbxdoctor

バイナリ FBX を Python だけで読み書きし、DCC が開けない原因になる構造上の
破綻を検出・修復するツール。Autodesk FBX SDK も Blender も不要で、標準
ライブラリのみで動く (Python 3.8 以降)。

MotionBuilder 2026 で開けるのに 2024 が読み込み中に停止する、という調査から
生まれたもの。原因は**頂点だけ持ち面を 1 枚も持たないメッシュ**だった。

```
fbxtool.py     ライブラリ本体 (パーサ / シリアライザ)
fbxdoctor.py   CLI (inspect / audit / fix)
```

同じディレクトリに置いて `python3 fbxdoctor.py ...` で使う。

---

## 使い方

### 中身を見る

```bash
python3 fbxdoctor.py inspect scene.fbx
```

FBX バージョン、書き出したアプリ、オブジェクトの内訳、頂点数、アニメーション
キー数を表示する。最後に**可逆性チェック**が出る。これは「無改変で読み書きして
元ファイルとバイト単位で一致するか」の検証で、`OK` ならそのファイルを
このツールで安全に加工できる。

### 問題を洗い出す

```bash
python3 fbxdoctor.py audit scene.fbx
python3 fbxdoctor.py audit "exports/*.fbx" --all
python3 fbxdoctor.py audit exports/            # ディレクトリ指定も可
```

`--all` を付けると情報レベルの指摘も出る。終了コードは
**0 = 問題なし / 1 = 警告のみ / 2 = 致命的問題あり**。CI やシェルスクリプトに
そのまま組み込める。

```bash
python3 fbxdoctor.py audit "exports/*.fbx" || echo "要確認"
```

### 修復する

```bash
python3 fbxdoctor.py fix "exports/*.fbx" -o fixed/
python3 fbxdoctor.py fix scene.fbx -o fixed/ --empty-clusters --junk-props
```

元ファイルは書き換えない。`-o` で指定したディレクトリに `_fixed` を付けた
コピーを書き出し、修復内容をログに出す。書き出し後に自動で再監査し、
致命的問題が残っていれば警告する。

既定で有効な修復は**面なしメッシュの除去**と**壊れたレイヤ参照の除去**の 2 つ。
`--keep-no-polygons` / `--keep-dangling-layer` で個別に無効化できる。
`--empty-clusters` と `--junk-props` は任意で追加する。

---

## 検査項目

| コード | 深刻度 | 内容 | 修復 |
|---|---|---|---|
| `no_polygons` | 致命 | 頂点はあるがポリゴンが 1 枚もないメッシュ。**MotionBuilder がマージ中に停止する原因**。Blender のテキストオブジェクトが変換されずに出力されると発生する | ○ |
| `index_range` | 致命 | ポリゴンの頂点索引が頂点数を超えている | |
| `unclosed_poly` | 致命 | 最終ポリゴンが閉じていない | |
| `layer_index` | 致命 | UV / 頂点カラーの索引が実体数を超えている | |
| `material_index` | 致命 | マテリアル索引が、実際に接続されたマテリアル数を超えている | |
| `dangling_conn` | 致命 | 存在しないオブジェクトを指す接続 | ○ |
| `non_finite` | 致命 | NaN / Inf の値 | |
| `dangling_layer` | 警告 | Layer が存在しない LayerElement を参照している。UV セットを 5 枚宣言して 1 枚しか書いていない、など | ○ |
| `layer_length` | 警告 | レイヤ要素の配列長がマッピング方式と食い違っている | |
| `degenerate_poly` | 警告 | 頂点 1〜2 個の退化ポリゴン | |
| `empty_cluster` | 情報 | 中身が空のスキンクラスタ。変形に寄与しないが読み込みを重くする | ○ |
| `nonascii_prop` | 情報 | 非 ASCII 名のカスタムプロパティ | ○ |
| `exporter_junk` | 情報 | Blender が `<bpy id prop: ...>` の文字列として書き出した残骸 | ○ |

修復はいずれも**表示される形状・スキンウェイト・ブレンドシェイプ・アニメーション
キーを一切減らさない**。除去されるのは、実体を持たないか参照先のない要素だけ。

---

## ライブラリとして使う

```python
import fbxtool

doc = fbxtool.load("scene.fbx")
print(doc.version, doc.creator())

for o in doc.objects:
    if o.name == b'Geometry' and o.str(2) == b'Mesh':
        v = o.find(b'Vertices')
        print(o.text(1), v.array_len(0) // 3, "頂点")

doc.save("copy.fbx")
```

主な API:

- `fbxtool.load(path)` / `loads(bytes)` → `Document`
- `Document.top` トップレベルノードのリスト
- `Document.objects` / `.connections` / `.section(name)` / `.walk()`
- `Document.save(path, version=None)` — `version` を渡すと 7400 ⇄ 7700 の相互変換になる
- `Node.find(name)` / `.find_all(name)` / `.walk()`
- `Node.prop(i)` スカラ / `text(i)` 文字列 / `array(i)` 配列展開 / `array_len(i)` 展開せず要素数だけ
- `fbxtool.verify_roundtrip(path)` → `(bool, 詳細)`

ノードの `props` は型文字を含む生バイト列のまま保持している。これが可逆性の要で、
触っていない部分は 1 バイトも変化しない。

### バージョン変換

```python
doc = fbxtool.load("scene.fbx")   # 7700
doc.save("old.fbx", version=7400) # 32bit レコードで書き直す
```

レコードのオフセット幅とフッタのアライメントは自動で切り替わる。ただしこれは
**器を変えるだけ**で、新しいバージョンでしか表現できないデータを古い形式向けに
変換するわけではない。

---

## 制限

- ASCII 形式の FBX は非対応 (`FbxAsciiError`)
- FBX 6.x 以前 (7000 未満) は未検証
- ファイル全体をメモリに載せる。数百 MB 級では相応にメモリを使う
- 修復は「無効な要素を取り除く」方針。壊れたデータを推測で復元することはしない
