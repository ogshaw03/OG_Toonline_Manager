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
ライン shape に textureDeformer（接線Z=法線方向に offset=太さ）
変形追従は textureDeformer のベース入力 input[0].inputGeometry に 元.outMesh を接続
shape.opposite=1 + doubleSided=0 で法線反転＆バックフェースカリング
最後に parent でグループへ（ワールド位置保持で重なりは維持）
```

1. `cmds.duplicate` した複製はトランスフォームを**動かさない**（元と同じ変換空間に置く）。
2. 膨らみは **textureDeformer** で行う。`cmds.textureDeformer(dshape, strength=0,
   offset=太さ, direction="Normal")`。**direction="Normal" が必須**（既定 "Handle" だと
   ハンドル軸=Y方向にしか動かない）。offset がサーフェス法線方向の一定距離オフセットになり、
   法線は**変形後メッシュから毎フレーム再計算**されるので、元を変形させても太さは一定。
3. 変形追従は **outMesh を textureDeformer のベース入力 `input[0].inputGeometry` に接続**。
   先に静的複製へ deformer を付けてから接続する（先に inMesh へ直結すると評価が壊れて歪む）。
   生成される textureDeformerHandle は **削除すると offset(太さ) が効かなくなる**ため、
   visibility=0 で dup の子に格納してアウトライナーを整理する（削除不可）。
   ※ ハンドルは戻り値に含まれない版があるので、**デフォーマに接続された transform**
     （`listConnections(defm, type="transform")` で名前に textureDeformerHandle を含むもの）
     として特定する。削除は offset を壊すので不可。また**変形対象メッシュ(dup)配下には
     親子付けできない**ため、ROOT 直下の非表示ホルダー `toonOutline_handles` へ退避する。
     ライン削除時は `_cleanup_orphan_handles` で孤立ハンドルと空ホルダーを掃除。
   曲率起伏は textureDeformer の weightList（頂点ウェイト）で実現。
   `weight[i]=min(曲率上限, 1+影響度*|曲率[i]|)`（曲がる所＝太く・直線＝細く、曲率上限で角の
   太り過ぎを抑制）。曲率は `_compute_curvature`（近傍平均との差を法線へ投影、om2）。
   影響度(0〜10)/曲率上限(1〜10)はライン attr `toonCurv`/`toonCurvCap` に保存。太さ=offset とは独立。

### アニメーション（ラインコントローラー）

各ラインに**独立したコントローラーノード**（`spaceLocator` `<line>_ctrl`、shape は非表示）を作り、
**キーアブルな英語アトリビュート** `thickness`/`curvature`/`curvatureCap` を持たせる
（`CTRL_THICK`/`CTRL_CURV`/`CTRL_CAP`）。メッシュ本体には属性を置かない。
- コントローラーは **ROOT 直下の `toonOutline_ctrls` グループ**に格納（ラインの子にはしない）。
  TRS/可視はロック&非表示で3チャンネルだけ見せる。`ctrl.message → line.toonCtrl` で関連付け
  （`_ctrl_of`）。ライン削除時は `delete_selected`＋`_cleanup_orphan_ctrls` で一緒に掃除。
- UIでラインを選択すると **コントローラーを Maya 選択** → タイムスライダにキーが表示される。
- UIのスピンボックスは **キー状態で着色**（`_attr_key_state`: アニメ有り=ピンク / 現フレームが
  キー=赤）。`timeChanged` scriptJob で再生・スクラブ時に色と数値を追従。
- **太さ**: `ctrl.thickness → textureDeformer.offset` を `connectAttr` で直結。DG評価なので
  キー/再生/レンダーまで完全にアニメ可能（軽量）。UIスライダーは `ctrl.thickness` を setAttr。
- **曲率起伏/曲率上限**: 頂点ウェイトは Python 計算が必要なため、`curvature`/`curvatureCap` の
  attributeChange を監視する `scriptJob`（`_ensure_curv_jobs`）で `_update_curv_weights` を呼び
  再計算。タイムライン再生でも追従（高密度メッシュは負荷大・バッチレンダーでは不可）。
- UIの「現フレームにキー」ボタンで選択ラインの3属性（コントローラー）へ `setKeyframe`。
- ノード名・属性名は全て英語（日本語混入による不具合回避）。UIラベルは日本語のまま。

UI: グループは展開式（プルダウン）のツリー項目。ライン/グループともダブルクリックでリネーム。
ラインはグループへ **ドラッグ&ドロップ**で移動（`_OutlineTree.dropEvent` → `_move_lines_to`）。
4. 法線反転は **shape の `opposite=1`**（ヒストリノードを足さない）＋ `doubleSided=0`。

太さの実体は **textureDeformer.offset**（ライン別に setAttr して制御）。

注意（膨らみノードの選定でハマった経緯。**全て一方向にずれ／太さ変動で失敗した**）:
- `polyMoveVertex` localTranslate / `polyExtrudeFacet` は選択全体を単一フレームで動かす
  → 一方向（カプセル/三日月）に歪む。
- `textureDeformer` を既定のまま使うとハンドル軸(+Y)方向。**direction="Normal" 指定**が必須。
- **blendShape** はバインド時の固定デルタを足すため、元メッシュを変形させると**太さが
  変わってしまう**（歪みは出ないが太さ非一定）。よって不使用。
- → 変形でも太さ一定にするには、**法線を毎フレーム再計算する deformer（textureDeformer
  の法線オフセット）** が正解。
- worldMesh + トランスフォーム単位化方式は原点バグの原因なので使わない。
- 制約: outMesh 追従は元の「変形（スキン等）」には追従するが、元トランスフォーム自体の
  アニメーションには追従しない（生成時のワールド位置で固定）。スキンキャラの通常運用では問題なし。

### 互換性の注意

PySide2 系の旧 Maya（2019/2020）は Python 2.7。
`*args` の後ろにキーワード引数を置く呼び出し（例: `cmds.setAttr(attr, *vals, type=...)`）は
Python 2 で構文エラーになるため使わない。引数は明示的に展開する。
