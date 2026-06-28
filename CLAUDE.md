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
- 設計・残課題・ハマりどころ: 本ファイル（CLAUDE.md）に集約。

### 実装上の最重要ポイント（原点バグ対策）

旧方式の「ヒストリを積んでから worldMesh を差し替える」方式は、
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
   `weight[i]=min(曲率上限, max(0, 曲率下限+影響度*|曲率[i]|))`（曲がる所＝太く・直線＝細く、
   曲率上限で角の太り過ぎを抑制、**曲率下限**(`curvatureMin`/`CTRL_CMIN`, 0〜1, 既定1.0)で平らな所
   ＝曲率の小さい所の太さを下げて細くできる）。曲率は `_compute_curvature`（近傍平均との差を法線へ投影、om2）。
   ※ ハードエッジ（立方体等）で曲率が1頂点に集中すると隣接頂点との重み差で輪郭が
     トゲ状（チクチク）になるため、`_compute_curvature` で曲率を近傍平均で
     `CURV_SMOOTH_ITERS`(=3)回スムージングしてから正規化する。
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
  UIツリーの太さ列にグループ倍率（x?.??）を表示（ヘッダーは「名前/太さ/色」のみ）。
- UIでラインを選択すると **コントローラーを Maya 選択** → タイムスライダにキーが表示される。
- UIのスピンボックスは **キー状態で着色**（`_attr_key_state`: アニメ有り=ピンク / 現フレームが
  キー=赤）。`timeChanged` scriptJob で再生・スクラブ時に色と数値を追従。
- **接続/ロックで設定不可なときの忠告**: コントローラー属性にコンストレイント等が接続されていて
  UI から `setAttr` できない場合、`_set_ctrl_attr` が `getAttr settable` で判定し「他ノードに接続
  されているため変更できない」旨を `cmds.warning` で一度だけ表示（同一プラグは `_warned_connected`
  でデデュープ、選択変更でクリア）。全 apply（太さ/曲率起伏/上限/下限/プロファイル/グループ・全体倍率）で共通。
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
- **「選択をすべてリセット」ボタン**（`reset_selected`）: 選択ライン/グループの全パラメータ
  （太さ/曲率起伏/曲率上限/末端細り/プロファイル、グループは倍率）を初期値に戻す。実行前に
  `QMessageBox.warning`（Yes/No、既定 No）で**確認ダイアログ**を出す。undo チャンクで1操作。
- **Undo**: 生成/削除/各値変更/キー/色/リネーム/移動に加え、ハンドル隠しトグル
  (`_on_toggle_hide_handles`)・選択不可トグル(`_on_toggle_lock_select`)・新規グループ
  (`new_group`)も openChunk/closeChunk で1 Undo にまとめている。スライダードラッグは
  `_begin_drag`/`_end_drag`＋`_dragging` フラグでドラッグ全体を1チャンク化。
- 上部に 生成／新規グループ／再取得 ボタンを配置。
- ノード名・属性名は全て英語（日本語混入による不具合回避）。UIラベルは日本語のまま。

UI: グループは展開式（プルダウン）のツリー項目。ライン/グループともダブルクリックでリネーム。
ラインはグループへ **ドラッグ&ドロップ**で移動（`_OutlineTree.dropEvent` → `_move_lines_to`）。
※ Qt 既定の InternalMove は Move を accept すると startDrag がソース項目を削除するため、
  空白へのドロップで項目が消える。`dropEvent` は必ず `IgnoreAction`＋`ignore()` にして
  既定移動を行わせず、親子付けは Maya 側で行い `refresh_tree` で作り直す（無効ドロップは復元）。
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

### 生成時の初期値・選択不可

- **新規生成は常に初期値**（UIの現在スライダー値を引き継がない）。hull/エッジとも
  `create_outlines`/`create_edge_line` で太さ=`DEFAULT_THICK`(hull 0.5)/`DEFAULT_EDGE_THICK`
  (edge 0.5)・曲率起伏=`DEFAULT_CURV`(0)・曲率上限=`DEFAULT_CAP`(3)・末端細り=0・
  プロファイル=フラット を `setAttr` してから生成。リセット(↺)も同じ定数・edge/hull 出し分け。
- **エッジは UI 太さと実太さを分離**: UI 値(=ctrl.thickness)は hull と同じ既定にしつつ、
  実際の offset は `EDGE_THICK_SCALE`(0.2) を掛けて細くする（`_ensure_thickness_chain` が
  edge のとき `mB.output × EDGE_THICK_SCALE`(=`<ctrl>_thkScale`) を offset へ）。
- **円プロファイル半径も太さに比例**: 固定半径だと細くしても基準半径分の太さが残るため、
  `makeNurbCircle.radius` を `mB.output × EDGE_BASE_SCALE`(0.2)(=`<ctrl>_radScale`) で駆動し
  （`_line_circle_node` で履歴から円ノードを取得）、太さに比例してチューブ全体が細る。
  既定 UI 0.5 → offset 0.1 + 半径 0.1 ≒ 半径0.2。太さ→0 で半径も offset も 0（可視は
  `EDGE_VIS_EPS` 条件で非表示）。生成直後の初期半径は `EDGE_BASE_RADIUS`(0.01)。
- **UIリストの大きさを選択で変えない**: リスト(tree)は stretch=1 でウィンドウの伸縮分を
  吸収（＝下に余分な余白を出さない）。選択で切り替わるスライダーパネルは固定高さの
  コンテナ `w_panels`(`setFixedHeight(200)`、中に w_line/w_group)に収めるため、ライン/
  グループ/エッジ(プロファイル有無)のどれを選んでもパネル領域の高さが一定＝リストは不動。
  ウィンドウ最小高さ `setMinimumHeight(620)`。
- **エッジラインの全体/グループ/ライン太さが 0 のとき消える**: 円プロファイル基準半径が
  あるため offset=0 でもチューブが残る。`_ensure_thickness_chain` で総太さ出力(mB.output)を
  `condition`(Greater Than `EDGE_VIS_EPS`=1e-4) 経由で **shape.visibility** に接続し、
  総太さ ~0 で非表示にする（shape 可視を駆動。手動表示/非表示は transform 可視なので競合しない）。
- **ラインのビューポート選択不可**（UIチェック「ラインをビューポートで選択不可にする」既定ON。
  `_on_toggle_lock_select`/`_apply_line_selectable`）: shape の `overrideEnabled=1`＋
  `overrideDisplayType=2`(reference) で表示・レンダーは有効のまま選択だけ不可。OFF で通常選択可。
  生成時・`refresh_tree` で現在状態を全ラインへ反映。

### エッジライン（チューブ・追加機能）

inverted hull とは別に、**選択ポリゴンエッジに沿ったチューブ状ライン**を追加できる
（`create_edge_line`、ボタン「選択エッジにライン」）。`polyToCurve`(履歴付=変形/移動追従)
→ 円プロファイル `circle` を `extrude` でカーブに沿わせ → `nurbsToPoly` でチューブ poly 化。
- `EDGE_TAG`(`isToonEdgeLine`) で識別。中間ノード(カーブ/円/NURBS面)はラインの子に隠して保持。
  円プロファイルは細い固定半径(0.01)で、実太さはチューブ表面の **textureDeformer** で出す。
  ※ extrude の向き次第でチューブ法線が**内向き**になると textureDeformer が内側へ押し込み、
    offset が基準半径(0.01)を超えるとチューブが軸を貫通して**反転**する。生成直後に
    `_tube_normals_outward`（中心カーブ最近接点→頂点への向きと頂点法線の内積を多数決）で
    外向きか判定し、内向きなら `polyNormal(normalMode=0)` を **textureDeformer の前**に積んで
    必ず外向き＝膨らむ向きにする。
- 新規生成時は UI の現在値ではなく **初期値で生成**（太さ=`DEFAULT_EDGE_THICK`(0.5)/曲率起伏=0/
  曲率上限=3/末端細り=0/プロファイル=フラット）。**hull/edge とも生成後は選択状態にしない**
  （`cmds.select(clear=True)`）。
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
- UIツリーは名前に種別サフィックス（`[背面]`/`[エッジ]`/`[フレネル]`）を付けて判別表示。

### フレネル輪郭（カメラ依存・VP2/バッチ対応）

背面法は凸シルエットの内側でハル境界が実際の見え方とズレて面を横切る線になる弱点がある。
これを避けるため、**カメラから見て寝た面（facingRatio が小さい縁＝シルエット/凹み）に線を出す**
方式を追加（`create_fresnel_outline`、ボタン「フレネル輪郭」、`FRES_TAG`）。
- 元を複製→`outMesh`で変形追従＋`parent/scaleConstraint`で移動追従（hull と同じ）。z-fighting
  回避に `textureDeformer` を微小オフセット(`FRES_ZOFFSET`=0.001、lock)で付けるが、太さ制御には使わない。
- シェーダ網 `_build_fresnel_network`: `samplerInfo.facingRatio → remapValue → lambert.transparency`
  （VP2 で実績のある構成）。線色は `lambert.incandescence`（unlit）、`color/diffuse/ambient=0`。
  facingRatio 0（寝た縁）→不透明な線、しきい値以上→透明。間は線形でソフトな縁。
  ※ **condition / surfaceShader の透明は VP2 で評価されず全面真っ黒**になるため使わない（remapValue は VP2 対応）。
    色取得/設定は `incandescence`（`_line_color`/色ボタンが FRES を出し分け）。
- **太さ=remapValue の遷移位置**（`value[1].value_Position`）。`_thick_target` が FRES のとき
  これを返し、`_ensure_thickness_chain` が `mB.output × FRES_SCALE`(0.3) を流す。line→remap は `FRES_LINK` message で特定。
- **scriptJob 不要**（曲率の頂点ウェイトは使わない＝`_ensure_line_anim` で curv ジョブをスキップ）。
  純シェーダなので **VP2/Maya Software のバッチで反映**、カメラ依存。レンダラーは現状 VP2/Maya SW 向け
  （Arnold 等は facing ノードが別なので未対応）。
- 色は専用 lambert の `incandescence` を直接変更（`_fresnel_ss`）。SG 差し替えはしない（線が消えるため）。
  曲率起伏/上限/下限/プロファイルは無効（UIには出るが効かない）。

### 互換性の注意

PySide2 系の旧 Maya（2019/2020）は Python 2.7。
`*args` の後ろにキーワード引数を置く呼び出し（例: `cmds.setAttr(attr, *vals, type=...)`）は
Python 2 で構文エラーになるため使わない。引数は明示的に展開する。
