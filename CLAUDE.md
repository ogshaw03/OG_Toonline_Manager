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
   削除すると offset が壊れるので不可。代わりに `_tuck_handle` で `visibility=0`＋
   `hiddenInOutliner` でその場で隠す（**専用グループは作らない**）。hiddenInOutliner は
   UIチェック「デフォーマハンドルをアウトライナーから隠す」(`_on_toggle_hide_handles`)で
   ON/OFF 切替可（既定ON。完全に消すと危険なので OFF で表示できる）。
   ※ ハンドルは戻り値に含まれない版があるので、`listConnections(defm, type="transform")` で
     名前に textureDeformerHandle を含むものとして特定。`_stash_loose_handles` が全ハンドルを
     隠し、旧 `toonOutline_handles` グループがあれば解体。孤立分は `_cleanup_orphan_handles`。
   曲率起伏は textureDeformer の weightList（頂点ウェイト）で実現。
   `weight[i]=min(曲率上限, 1+影響度*|曲率[i]|)`（曲がる所＝太く・直線＝細く、曲率上限で角の
   太り過ぎを抑制）。曲率は `_compute_curvature`（近傍平均との差を法線へ投影、om2）。
   影響度(0〜10)/曲率上限(1〜10)はライン attr `toonCurv`/`toonCurvCap` に保存。太さ=offset とは独立。

### アニメーション（ラインコントローラー）

コントローラーは **全体>グループ>ライン の3階層**（すべて素の `transform`、シェイプ無し。
`useOutlinerColor`/`outlinerColor` でアウトライナーの文字を色分け。`overrideColor` はビューポート
用なので使わない）:
- **全体** `toonOutline_globalCtrl`（水色 / `thicknessMult`）= ROOT 直下、コントローラー階層の親。
- **グループ** `<group>_ctrl`（黄緑 / `thicknessMult`）= 全体コントローラー配下。`group.toonCtrl`。
- **ライン** `<line>_ctrl`（黄 / `thickness`/`curvature`/`curvatureCap`、`CTRL_THICK`等）=
  所属グループのコントローラー配下。`line.toonCtrl`。メッシュ本体には属性を置かない。
- 関連付けは `ctrl.message → node.toonCtrl`（`_ctrl_of`）。TRS/可視はロック&非表示。
  ライン/グループ削除時は `delete_selected`＋`_cleanup_orphan_ctrls` で一緒に掃除。
- 太さ = `lineCtrl.thickness × groupCtrl.thicknessMult × globalCtrl.thicknessMult`
  （`_ensure_thickness_chain`、multDoubleLinear 2段）。
- ライングループ（GROUP_TAG）は **ROOT 直下 `Outline_grp`（`LINE_HOLDER`）の中**に格納。
  UIツリーの太さ列にグループ倍率（x?.??）、ヘッダーに全体倍率を表示。
- UIでラインを選択すると **コントローラーを Maya 選択** → タイムスライダにキーが表示される。
- UIのスピンボックスは **キー状態で着色**（`_attr_key_state`: アニメ有り=ピンク / 現フレームが
  キー=赤）。`timeChanged` scriptJob で再生・スクラブ時に色と数値を追従。
- **太さ**: `offset = ctrl.thickness × group.thicknessMult × ROOT.thicknessMult` を
  `multDoubleLinear` 2段（`<ctrl>_thkA`/`_thkB`）で構成し `textureDeformer.offset` へ接続。
  ライン値・グループ倍率（`GMULT` on group）・全体倍率（`GMULT` on ROOT）の乗算で、全て DG・
  アニメ可能。`_ensure_thickness_chain` が構築（D&D移動・再取得でグループ入力を張り直す）。
  UIは「太さ」=ctrl.thickness、「グループ倍率」「全体倍率」=各 thicknessMult を setAttr。
- **曲率起伏/曲率上限**: 頂点ウェイトは Python 計算が必要なため、`curvature`/`curvatureCap` の
  attributeChange を監視する `scriptJob`（`_ensure_curv_jobs`）で `_update_curv_weights` を呼び
  再計算。タイムライン再生でも追従（高密度メッシュは負荷大・バッチレンダーでは不可）。
- 各スライダー行に **K ボタン**（その値だけ現フレームにキー）。一括キーボタンは廃止。
- 削除は **Delete キー**（`_OutlineTree.keyPressEvent`→`delete_selected`）。削除ボタンは廃止。
- 上部に 生成／新規グループ／再取得 ボタンを配置。
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
- 追従: 変形（スキン等）は `outMesh→inMesh` で追従。元トランスフォームの移動/回転/スケールは
  `parentConstraint`+`scaleConstraint`（`_ensure_follow`）でラインを元に拘束して追従させる。

UIパネルは選択で切替: 全体倍率は常に最上部。グループ選択時はグループ倍率＋全体のみ、
ライン選択時は太さ/曲率起伏/曲率上限＋全体のみ表示（`_update_panels` で w_line/w_group をトグル）。

### エッジライン（チューブ・追加機能）

inverted hull とは別に、**選択ポリゴンエッジに沿ったチューブ状ライン**を追加できる
（`create_edge_line`、ボタン「選択エッジにライン」）。`polyToCurve`(履歴付=変形/移動追従)
→ 円プロファイル `circle` を `extrude` でカーブに沿わせ → `nurbsToPoly` でチューブ poly 化。
- `EDGE_TAG`(`isToonEdgeLine`) で識別。中間ノード(カーブ/円/NURBS面)はラインの子に隠して保持。
  円プロファイルは細い固定半径(0.01)で、実太さはチューブ表面の **textureDeformer** で出す。
- 新規生成時は UI の現在値ではなく **初期値で生成**（太さ=`DEFAULT_THICK`(0.05)/曲率起伏=0/
  曲率上限=3/末端細り=0/プロファイル=フラット）。生成後は **アウトライナーで選択しない**
  （`cmds.select(clear=True)`）。hull(`create_outlines`)は従来どおり現在値で生成・選択する。
- **太さ・曲率起伏は hull と完全に同じ機構**（チューブ shape に textureDeformer を付け、
  offset=太さ／weightList=曲率）。`_thick_target` は hull/エッジとも `textureDeformer.offset`。
  曲率はチューブ自身の表面曲率（曲がった所ほど太い）。グループ/全体倍率・キー・色も共通。
- polyToCurve が世界空間で追従するため parentConstraint/スムース連動は付けない
  （`_ensure_line_anim` で EDGE_TAG のとき follow/smooth をスキップ。曲率ジョブは張る）。
- **末端細り** `endTaper`(0〜1, `CTRL_TAPER`): 各頂点のカーブ長手パラメータ t を
  `_taper_factors`（om2 `MFnNurbsCurve.closestPoint`）で求め、端ほど weightList を減衰
  （`weight*= (1-taper)+taper*norm`）。hull はカーブが無いので無効。
  ※ **UI スライダーは廃止**（太さプロファイルで代替できるため）。`endTaper` 属性自体は
    後方互換で残し、既存シーンに値があれば `_update_curv_weights` が引き続き反映する。
    新規ライン生成時は 0（初期値）。
- **太さプロファイル** `thicknessProfile`(文字列 `CTRL_PROFILE`, "x:y,x:y,..."): 長手 t を
  カーブでサンプルして weightList に乗算。UI は `_RampWidget`（左クリックで点追加/移動・
  右クリック削除）。`_parse_profile`/`_sample_profile` でサンプル。末端細りと合成。edge 向け。
  ※ 末端細り/プロファイルで weightList が 0 まで落ちるとチューブが基準半径(≈点)へ潰れて
    スピンドル状に尖る（反転して -値に見える）ため、`_update_curv_weights` 末尾で
    全 weight を `MIN_WEIGHT`(=0.05) で下限クランプして潰れを防ぐ。
- UIツリーは名前に種別サフィックス（`[背面]`/`[エッジ]`）を付けて判別表示。

### 互換性の注意

PySide2 系の旧 Maya（2019/2020）は Python 2.7。
`*args` の後ろにキーワード引数を置く呼び出し（例: `cmds.setAttr(attr, *vals, type=...)`）は
Python 2 で構文エラーになるため使わない。引数は明示的に展開する。
