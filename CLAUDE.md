# CLAUDE.md

このリポジトリで作業する際の方針メモ。

## ブランチ運用

- **基本は dev 用ブランチにコミット・プッシュする。**
  現在の dev ブランチ: `claude/repository-handoff-review-k54kwk`
- **`main` への直接プッシュは、ユーザーから明示的な指示があった場合のみ** 行う。
  指示がなければ main には触れない。

## プロジェクト概要

Maya 用の輪郭線生成ツール「OG Toonline Manager」。
`dx11Shader` の `MayaToonOutline.fx` 相当の輪郭線を、Maya 標準機能（実ジオメトリ）
だけで再現する。inverted hull 方式（押し出し → 法線反転 → バックフェースカリング）で、
元メッシュの変形に自動追従し、PySide6/PySide2 UI で太さ・色をライブ調整できる。

- 実装本体: `OG_Toonline_Manager.py`
- 設計・残課題・ハマりどころ: `toon_outline_handoff.md`（必読）

### 実装上の最重要ポイント（原点バグ対策）

handoff §3 の「ヒストリを積んでから worldMesh を差し替える」方式は、
差し替えが output に伝播せず輪郭が原点に生成される不具合が再発した。
原点バグは「**複製をワールドへ出して T0/R0/S1 に単位化 + worldMesh 接続**」方式が
原因（worldMesh の接続順を NG/OK どちらにしても再発した）。現行は worldMesh を使わない
方式に変更済み。`create_outlines()` の正しい手順:

```
複製は元と同じ親・同じ TRS のまま（動かさない）
元.outMesh → ライン shape.inMesh → textureDeformer.offset(法線方向に膨らみ)
shape.opposite=1 + doubleSided=0 で法線反転＆バックフェースカリング
最後に parent でグループへ（ワールド位置保持で重なりは維持）
```

1. `cmds.duplicate` した複製はトランスフォームを**動かさない**（元と同じ変換空間に置く）。
2. 複製のヒストリを削除して inMesh を空け、**元の `outMesh`（オブジェクト空間の変形後
   メッシュ）** を inMesh に直結。複製は元と同じ TRS なので変形追従しつつ元に重なる。
3. `cmds.textureDeformer(dshape, strength=0)` を付け、`.offset` に太さを設定。offset は
   **各頂点を自身の法線方向へ一様変位**させる（テクスチャ寄与は strength=0 で無効）。
4. 法線反転は **shape の `opposite=1`** で行い（ヒストリノードを足さない）、`doubleSided=0`
   と合わせてリムだけ表示。textureDeformer のハンドルは非表示にして dup の子に入れる。

太さの実体は **textureDeformer.offset**（ライン別に setAttr して制御）。

注意（膨らみノードの選定でハマった経緯）:
- `polyMoveVertex` の localTranslate は選択全体で単一フレーム → 全頂点が一方向へ動き
  カプセル/三日月状に歪む。**使わない**。
- `polyExtrudeFacet`(keepFacesTogether=True) も面群を剛体的に動かすため同様に一方向ずれ。
  keepFacesTogether=False は面がバラけて隙間。ポリゴン操作系では一様な法線オフセット不可。
- 二重壁(厚み)を作らないこと（押し出し系は厚みが出て内側が元と重なる）。
- 法線方向の一様オフセットは textureDeformer.offset が正解。
- worldMesh + トランスフォーム単位化方式は原点バグの原因なので使わない。
- 制約: outMesh 追従は元の「変形（スキン等）」には追従するが、元トランスフォーム自体の
  アニメーションには追従しない（生成時のワールド位置で固定）。スキンキャラの通常運用では問題なし。

### 互換性の注意

PySide2 系の旧 Maya（2019/2020）は Python 2.7。
`*args` の後ろにキーワード引数を置く呼び出し（例: `cmds.setAttr(attr, *vals, type=...)`）は
Python 2 で構文エラーになるため使わない。引数は明示的に展開する。
