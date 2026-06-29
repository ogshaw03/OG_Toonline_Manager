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

#### 隠蔽検知ハル（カメラ依存・検証中／ビューポート専用）

ハルはワールド法線オフセットのため、元メッシュとラインの間に 3D の隙間ができ、グラジング角で
その隙間が横から見えて輪郭が浮く/面に乗る。これを抑える試験機能（UIチェック「ハルの隠れた面を
元メッシュに寄せる」、既定OFF。`_OCC_ENABLED`/`_on_toggle_occlude`）:
- オフセット後のシェル頂点 S=P+N*太さ から、**カメラ方向（手前）と逆方向（奥）の両方へレイ**を飛ばし、
  どちらかで元メッシュに当たれば「画面上で本体に重なっている」＝隠蔽と判定（`_occlusion_factors`、om2
  `MFnMesh.allIntersections`、オブジェクト空間。両方外れる＝シルエットのフチだけ太さを残す。ortho は
  ビュー方向一定）。allIntersections はシグネチャ環境差に備え `_mesh_all_intersections` で複数フォーム
  試行。カメラ位置は shape の `inclusiveMatrix` 並進成分から取得（`cmds.xform` はシェイプ相手で例外
  になり 0/0 未検出の原因になった）。ON 時に検出頂点数を `cmds.warning` で報告（0なら検出失敗の切り分け用）。
- 隠れた頂点の weightList 係数を `OCC_HIDDEN_WEIGHT`(-1.5)＝**元メッシュ内側へ深く寄せて裏面を隠す**
  （浅いと重なり部のシェルが表面手前に残り隙間が見えるため深めにする）。
  **可視（シルエット）頂点は係数 1.0 のまま**なので太さは保たれる（＝交差部だけ細くならない）。
  境界のジャギは `_smooth_vertex_values`(`OCC_SMOOTH_ITERS`=2)で均す。
- **太さ**: 太さはシルエット端から `off` 張り出す量で決まり、残す帯の幅では決まらない。スムージング後も
  **残す頂点は必ず係数 1.0 に再クランプ**してシルエット頂点のフル太さを維持（これだけで太さは出る）。
  **隠す頂点は必ず `OCC_HIDE_CEIL`(-0.1)以下にクランプ**してスムージングで 0 付近に戻っても表面の裏へ確実に
  潜らせ重なり部を張り付かせる。`OCC_KEEP_DILATE`(既定0)>0 にすると残す帯を内側へ太らせ太さが安定する
  が重なり部が浮く（張り付きと競合）ため既定0。`_vertex_adjacency` を dilate/smooth で共有。
- 実体は `_update_curv_weights` 末尾で `weights[i] *= occ[i]`（曲率/プロファイルと合成）。`_is_hull_line` のみ対象。
- 更新は **カメラ transform の worldMatrix 変化コールバック**(om2 `MDagMessage.addWorldMatrixModifiedCallback`
  →`_on_cam_moved`)で**カメラを動かすたびにリアルタイム**再計算。DG評価中の setAttr 再入を避けるため
  `_request_occ_refresh` が `QTimer.singleShot(0)` で次ループに1回だけ予約（`_occ_scheduled`）。
  カメラ切替/新規カメラ用に低頻度ポーリング(`_poll_camera` 250ms)もフォールバックで併用。再計算の重畳は
  `_occ_busy` でスキップ。コールバックは ON で全カメラに登録・OFF/クローズで `_remove_cam_callbacks`。
  **scriptJob/レイのためバッチレンダー不可・高密度メッシュは追従が落ちる**。真の毎フレーム/バッチ対応には
  MPxDeformerNode 化が必要（未実装）。

#### 重なりマスク（別オブジェクト方式・太さ安定／バッチ対応）

隠蔽検知ハルは単一オブジェクトで太さ安定と張り付きを両立できない（法線オフセット式の限界。残す帯を
太らせると重なり部が浮き、細らせると太さがちらつく）。そこで **A=単純な均一ハル（太さ安定）＋
B=元メッシュ複製を面色で膨らませた覆い** の2オブジェクトに分離する方式（ボタン「重なりマスク」、
`toggle_overlap_mask`、`MASK_TAG`/`MASK_LINK`）:
- A は通常の単純ハル（occlusion OFF）でフル均一太さ＝安定。B はマスクで内側交差を隠す。
  **マスク有のラインは occlusion 対象外**（`_update_curv_weights` で `not _mask_of(line)` 条件）。
  引き寄せの per-vertex ムラと二重がけになって太さが凸凹するのを防ぐ。マスク追加/削除時に
  `toggle_overlap_mask` が `_update_curv_weights` を呼んで残ウェイトをクリア/復帰。
- B = 元メッシュの追従複製（`outMesh`→textureDeformer ベース入力＋`parent/scaleConstraint`）に、
  **元と同じシェーディンググループ（面色）を割り当て**、法線方向へ少し膨らませて A の内側交差を覆う。
  B は **ROOT 直下 `MASK_HOLDER`(`ToonMask_grp`) に格納＝別オブジェクトとして可視**（ライン子にすると
  最上位で1つにしか見えず分かりにくいため）。膨らみ量 = 総太さ(mB.output) × `MASK_INFLATE_FRAC`(0.5) を
  `multDoubleLinear`(`<mask>_inflate`)で接続。
- **線が細くならない補正**: マスクは inflate 分だけ線を覆うので、その分だけハル A の offset を上乗せして
  見える線幅＝設定太さを保つ。`_ensure_thickness_chain` の hull 分岐がマスク有のとき
  `offset = mB.output×(1+MASK_INFLATE_FRAC)`(`<ctrl>_thkMaskBoost`)に張り替え、マスク無で外す。
  `toggle_overlap_mask` が追加/削除後に `_ensure_thickness_chain` を呼んで切替。線幅 = A_off − inflate = mB.output。
- ボタンはトグル: 既にマスクがあれば削除（`_remove_overlap_mask`）。ライン削除時は `_gather` が
  `_mask_of` で B 本体と `<mask>_inflate` を victims に加えて一緒に消す（空 `MASK_HOLDER` も掃除）。
- カメラ非依存・DG のみ＝**VP2/バッチ対応・毎フレーム追従**。注意: B が法線方向に inflate する分だけ
  モデルのシルエットがわずかに太る。極端なグラジング角では交差の出っ張りが inflate を超えて少し漏れる
  （inflate を上げると覆えるが線が細る）。

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
- シェーダは **VP2 ハードウェアシェーダ `dx11Shader` + 自前 HLSL**（`_build_fresnel_network`/`_FRES_FX_HLSL`）。
  HLSL を `_fresnel_fx_path`（userAppDir/OG_Toonline_Manager/OG_ToonFresnel.fx）へ書き出して `dx11Shader.shader` に
  ロード。VS で `WorldView` でビュー空間法線 `VN` を作り、PS で `facing=abs(normalize(VN).z)`、
  `a=1-smoothstep(0,threshold,facing)`、`a<=0` で discard、`float4(lineColor,a)`（technique に `isTransparent=1`）。
  ※ **`samplerInfo.facingRatio` は VP2 で評価されず**（lambert/condition/remapValue でも全面黒）、ハードウェアシェーダで
    facing を自前計算するのが VP2/Hardware 2.0 バッチで効く唯一の方法。**DirectX11 VP2 前提**（OpenGL は要 GLSL 版）。
  ※ **dx11Shader はビューポートの「テクスチャ表示 ON（ホットキー 6）」でないと表示されない**（生成時に警告で案内）。
    カメラ位置/行列インデックス（ViewInverse[3]）は行/列メジャーで不安定なので使わず、ビュー空間法線 z で算出。
- **太さ=dx11Shader の `threshold` uniform**（`FRES_THRESH`）。`_thick_target` が FRES のとき `shd.threshold` を返し、
  `_ensure_thickness_chain` が `mB.output × FRES_SCALE`(0.3) を流す。line→dx11Shader は `FRES_LINK` message で特定。
- **scriptJob 不要**（曲率の頂点ウェイトは使わない＝`_ensure_line_anim` で curv ジョブをスキップ）。
  カメラ依存。元へ `outMesh`+`parent/scaleConstraint` で追従、z-fight 回避に `textureDeformer` 微小オフセット(lock)。
- 色は dx11Shader の `lineColor` uniform（`FRES_COLOR`）を直接変更。SG 差し替えはしない（線が消えるため）。
  曲率起伏/上限/下限/プロファイルは無効（UIには出るが効かない）。

### スクリーン輪郭（クリップ空間押し出し・隙間なし均一太さ）

ジオメトリ式ハル（textureDeformer のワールド法線オフセット）は、押し出しで元メッシュとライン
メッシュの間に**3D の隙間**ができ、グラジング角でその隙間が見えて輪郭が浮く/交差する。これを
解決するのが `create_screen_outline`（ボタン「スクリーン輪郭」、`SCRN_TAG`）:
- dx11Shader の**頂点シェーダでクリップ空間（画面上）へ一定ピクセル押し出す**（`_SCRN_FX_HLSL`）。
  `clip = pos×WVP`、ビュー空間法線 xy 方向へ `clip.xy += normalize(sn) * thickness * (2/viewport) * clip.w`。
  押し出しが**元シルエットと同じ深度のまま画面上で広がる**ので隙間が出ず、`clip.w` 補正で**均一ピクセル太さ**。
- `RasterizerState CullMode=Front`（背面のみ）で外周リング＝輪郭。元は別オブジェクトの深度で内側を覆う。
- 太さ＝dx11Shader の `thickness`(px) uniform。`_thick_target` が SCRN のとき返し、`_ensure_thickness_chain`
  が `mB.output × SCRN_SCALE`(6.0) を流す。色は `lineColor`（フレネルと共通の dx11 線色処理）。
- 元へ `outMesh`(offset=0 lock)+`parent/scaleConstraint` で追従。曲率/プロファイルは無効・scriptJob 不要。
- **VP2(DirectX11)・テクスチャ表示 ON(6) 前提**、カメラ依存、Hardware 2.0 バッチ対応。
- 別案として **スクリーンスペース深度エッジ検出**（`OG_Edge_Outline.py` / `MRenderOverride`）も試験的に用意。

### 互換性の注意

PySide2 系の旧 Maya（2019/2020）は Python 2.7。
`*args` の後ろにキーワード引数を置く呼び出し（例: `cmds.setAttr(attr, *vals, type=...)`）は
Python 2 で構文エラーになるため使わない。引数は明示的に展開する。
