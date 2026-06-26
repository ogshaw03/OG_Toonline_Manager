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
元.outMesh → ライン shape.inMesh
法線方向に +1 押したターゲット → blendShape（ウェイト=太さ）で膨らませる
shape.opposite=1 + doubleSided=0 で法線反転＆バックフェースカリング
最後に parent でグループへ（ワールド位置保持で重なりは維持）
```

1. `cmds.duplicate` した複製はトランスフォームを**動かさない**（元と同じ変換空間に置く）。
2. 複製のヒストリを削除して inMesh を空け、**元の `outMesh`（オブジェクト空間の変形後
   メッシュ）** を inMesh に直結。複製は元と同じ TRS なので変形追従しつつ元に重なる。
3. 膨らみは **blendShape** で行う。OpenMaya(`om2`) で各頂点を**自分の頂点法線方向へ +1**
   だけ動かしたターゲットメッシュを作り、`cmds.blendShape(target, dshape)` で適用。
   太さ = `blendShape.weight[0]`（ライブ）。ターゲットは visibility=0 で dup の子に保持。
   ※ Edit Mesh > Transform をノーマル方向に使ったのと同じ「頂点ごとの法線オフセット」。
     `polyMoveVertex`（=Transform の実体）は単一方向にしか動かせずスクリプト化不可。
3b. 変形追従は **outMesh を blendShape のベース入力 `input[0].inputGeometry` に接続**して
    行う（先に inMesh へ直結すると評価が壊れて歪むので不可）。失敗しても静的な正しい形は残る。
4. 法線反転は **shape の `opposite=1`**（ヒストリノードを足さない）＋ `doubleSided=0`。

太さの実体は **blendShape.weight[0]**（ライン別に setAttr して制御）。`_push_along_normals`
が `om2.MFnMesh.getVertexNormals`/`getPoints`/`setPoints` でターゲットを生成する。

注意（膨らみノードの選定でハマった経緯。**全て一方向にずれて失敗した**）:
- `polyMoveVertex` の localTranslate は選択全体で単一フレーム → 全頂点が一方向へ動き
  カプセル/三日月状に歪む。
- `polyExtrudeFacet`(keepFacesTogether=True) も面群を剛体的に動かすため一方向ずれ。
  keepFacesTogether=False は面がバラけて隙間。
- `textureDeformer.offset` はハンドル軸（既定 +Y）方向へ一様移動で、法線方向ではない。
- → これらは使わず、**各頂点法線へ押したターゲット + blendShape** が正解（頂点ごとに
  正しい方向へ動く・二重壁の厚みも出ない）。
- worldMesh + トランスフォーム単位化方式は原点バグの原因なので使わない。
- 制約: outMesh 追従は元の「変形（スキン等）」には追従するが、元トランスフォーム自体の
  アニメーションには追従しない（生成時のワールド位置で固定）。スキンキャラの通常運用では問題なし。

### 互換性の注意

PySide2 系の旧 Maya（2019/2020）は Python 2.7。
`*args` の後ろにキーワード引数を置く呼び出し（例: `cmds.setAttr(attr, *vals, type=...)`）は
Python 2 で構文エラーになるため使わない。引数は明示的に展開する。
