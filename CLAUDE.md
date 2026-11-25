# CLAUDE.md

このリポジトリで作業する際の方針メモ。

## ブランチ運用

- **基本は dev 用ブランチにコミット・プッシュする。**
  現在の dev ブランチ: `claude/repository-handoff-review-k54kwk`
- **`main` への直接プッシュは、ユーザーから明示的な指示があった場合のみ** 行う。
  指示がなければ main には触れない。

## 配布・ホットアップデート（`OG_Toonline_Manager_install.py`）

`maya-hot-update-patterns.md` 準拠の「OG_Toonline_Manager_install.py ドラッグ&ドロップ＋UIボタンで
GitHub 最新版へ更新（Maya 再起動不要）」運用。**単一ファイルツール**構成。

- **配布物**: `OG_Toonline_Manager_install.py` の raw URL のみ。ユーザーはブラウザ保存 → Maya ビューポートに
  ドラッグ。以降は UI フッター「GitHub から更新」または シェルフボタン右クリック → Update。
- **取得元ブランチ**: 現在は開発ブランチ `claude/repository-handoff-review-k54kwk`
  （`OG_Toonline_Manager_install.py` の `_GITHUB_BRANCH` と `OG_Toonline_Manager.py` の `_GITHUB_BRANCH` の
  **2箇所を必ず一致**させる）。安定運用に移すときは両方を `main` に変え、main へマージする。
- **OG_Toonline_Manager_install.py**（`_MODULE="OG_Toonline_Manager"` / `_SHELF_BUTTON_LABEL="OGToon"`）:
  SHA-pinned raw URL で取得（§1-7 CDN キャッシュ回避）・atomic write（§1-4）・
  `__pycache__` 掃除（§1-5）・`sys.modules` flush（§1-6）・シェルフボタン（左=起動/右=Update, §1-8）。
  ローカル反復は環境変数 `OG_TOONLINE_MANAGER_USE_LOCAL=1`。**Py3(Maya2022+)前提**（f-string使用）。
  - **シェルフアイコン**: `_ICON_FILE`（`icon_OG_Toonline_Manager.png`）も同じ SHA で取得し
    `internalVar(userBitmapsDir=True)` に置いて `shelfButton.image` に絶対パス指定
    （`_source_bytes`/`_fetch_icon`/`_add_shelf_button(icon_path)`）。取得失敗時は `pythonFamily.png`
    ＋オーバーレイラベルにフォールバック（非致命）。アイコンを差し替えるなら repo ルートの
    同名 PNG を置き換えるだけ。モジュール本体とアイコンの**2ファイル**を取得する（§1-10 の対象が増えた点に注意）。
- **本体側の更新フロー**（`OG_Toonline_Manager.py`）: `__version__`（インストーラが正規表現で読む・
  上げてから push すると previous→current が変わる）、`update_from_github`/`_run_update`/
  `_reopen_after_update`（evalDeferred 3段, §1-9）、フッターの「GitHub から更新」ボタン。
  **本体は Py2 でも import 可能なまま保つため f-string 禁止（`.format()` を使う）**。urllib.request は
  関数内 import なので Py2 では更新ボタン実行時のみ失敗（import 自体は通る）。
- **重要（§1-10）**: 将来 `.py` を複数ファイルに分割したら、インストーラを単一DLから
  ファイル一覧DL（`_REMOTE_FILES` タプル）に拡張し、**新規 .py は必ず同じ commit で追記**する。
  現状は単一ファイルなので `_REMOTE_FILES` は無い。
- **§8 拡張（バージョンロールバック/新版通知/Changelog 等）はユーザーが明示的に希望しない限り
  実装しない**（doc §8 のルール）。

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
  再計算。タイムライン再生でも追従（バッチレンダーでは不可）。v0.1.52 から重みは om2 で書く
  （4 万頂点で 0.03 秒・Undo 履歴に積まない。詳しくは「曲率ジョブ・重みの書き込み・名前空間」）。
- 各スライダー行に **K ボタン**（その値だけ現フレームにキー）。一括キーボタンは廃止。
- 削除は **Delete キー**（`_OutlineTree.keyPressEvent`→`delete_selected`）。削除ボタンは廃止。
- **「選択をすべてリセット」ボタン**（`reset_selected`）: 選択ライン/グループの全パラメータ
  （太さ/曲率起伏/曲率上限/末端細り/プロファイル、グループは倍率）を初期値に戻す。実行前に
  `QMessageBox.warning`（Yes/No、既定 No）で**確認ダイアログ**を出す。undo チャンクで1操作。
- **Undo**: 生成/削除/各値変更/キー/色/リネーム/移動に加え、ハンドル隠しトグル
  (`_on_toggle_hide_handles`)・選択不可トグル(`_on_toggle_lock_select`)・新規グループ
  (`new_group`)も openChunk/closeChunk で1 Undo にまとめている。スライダードラッグは
  `_begin_drag`/`_end_drag`＋`_dragging` フラグでドラッグ全体を1チャンク化。
- 上部ボタンは **生成／スクリーン輪郭／選択エッジにライン／新規グループ／再取得** の5つに整理
  （ユーザー要望でUI簡素化）。フレネルは専用ボタンを廃してスクリーン輪郭のチェックボックスに統合。
  **クリース/境界ライン（`create_crease_lines`）・隙間埋め（`toggle_gapfill`）・その調整UI（旧
  `grp_gapparams`）はボタンごと撤去**（メソッドと定数は後方互換で残置＝コードは動くがUI導線なし）。
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
- ※ **UIボタンと生成は廃止済み**（ユーザー要望）。`toggle_overlap_mask`/`_create/_remove_overlap_mask`・
  `MASK_*` 定数・`_mask_of`・`_ensure_thickness_chain` のマスク分岐などの裏コードは後方互換で残置。

#### 隙間埋め（facing 駆動・2オブジェクト／カメラ依存・VP2/バッチ対応）【現行の推奨】

背面法ハル A は法線オフセットのため、グラジング角でフチが表面から浮いて隙間（背景が透ける）が出る。
これを **B=元メッシュ複製(線色・背面法)** で塞ぐ方式（ボタン「隙間埋め」、`toggle_gapfill`、
`GAPFILL_TAG`/`GAPFILL_LINK`、`GAPFILL_HOLDER`=`ToonGapFill_grp`）:
- B の各頂点を **符号付き内積 d=法線·視線 で駆動**（`_update_gapfill_weights`、om2・レイ不要で軽い）。
  `facing=|d|` の絶対値で判定すると**シルエットの手前側と奥側の両方が膨らんで二重線**になるため、符号で分ける。
  **裏面のみ表示なので見える面＝背面側(d<0)**。頂点を膨らませるとシルエット付近で背面がカメラ方向を向くため、
  その背面で隙間を埋める:
  - **背面側の寝た面（-thr≤d≤0）**: `weight=1` で平らに保ち B を A の頂点位置(=offset)にぴったり重ねる
    （ランプにすると端で表面に戻る縁が薄い二重線になるため平らにする）。隙間を**1本で**埋める。
  - **深い背面（d<-thr）**: `weight` 負（`GAPFILL_TUCK`(-2.0)）で本体の裏へ一気に沈め隠す。
  - **正面側（d>0）**: `weight=0` で元メッシュ表面に張り付く（裏面表示で前面はカリング＝本体に黒面が出ない）。
- B は **裏面のみ表示（`opposite=1`/`doubleSided=0`）**＝ハル A と同じ。膨らんだ背面が見えて隙間を埋め、
  正面（本体側）はカリングされ黒面が出ない。A と同じ線色SG。`offset` は **A の `textureDeformer.offset` に接続**
  （A の太さに追従＝A の頂点位置に届く）。元へ `outMesh`+`parent/scaleConstraint` で追従。太さは A のリム基準で安定。
- 更新は隠蔽検知と共通のカメラ追従基盤（`_on_cam_moved`→`_request_occ_refresh`→`_refresh_occlusion` が
  ハルの occlusion と gapfill の両方を再計算）。`_ensure_cam_tracking` が **occlusion ON または gapfill 存在**
  のときコールバック＋250msフォールバックを起動/解除。ライン削除で B も `_gather` で一緒に消す。
- レイ検知の引き寄せ（occlusion）より滑らか・軽量で太さも安定しやすい。**カメラ依存・VP2/Hardware バッチ対応**。

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

**クリース/境界の自動エッジライン**（`create_crease_lines`、ボタン「クリース/境界にライン」）:
pfxToon/MayaToonOutline のエッジ分類のうち**クリース（二面角>`CREASE_ANGLE`=30°の折れ目）＋ボーダー
（開境界）**を om2 `MItMeshEdge`(`onBoundary`/隣接2面法線の内積)で検出し、そのエッジを選択して
`create_edge_line` でチューブ化（`_crease_border_edges`）。**カメラ非依存・静的・全レンダラー/バッチ対応**。
※ シルエット（プロファイル＝重なり/浮き隙間）は**カメラ依存**のためここでは対象外（滑らかな閉曲面は
折れ目が無く線が出ない）。隙間問題の本命はシルエットエッジ検出（カメラ依存・別途）。
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
  ロード。VS で `WorldView` でビュー空間法線 `VN` を作り、PS で `facing=abs(normalize(VN).z)`。
  **一定画面px幅の輪郭**（旧: facing<threshold の帯＝面の曲がりで幅が変わり“ムラ”）: PS で
  `sd=facing/fwidth(facing)`（≒シルエット/輪郭からの画面px距離）を出し、`a=1-smoothstep(maxWidthPx±1, sd)`。
  平らな面は `fwidth≈0`→`sd`巨大→線が出ない（ムラ源のワイド帯が消える）。`a<=0` で discard、`float4(lineColor,a)`
  （technique に `isTransparent=1`）。
  ※ **`samplerInfo.facingRatio` は VP2 で評価されず**（lambert/condition/remapValue でも全面黒）、ハードウェアシェーダで
    facing を自前計算するのが VP2/Hardware 2.0 バッチで効く唯一の方法。**DirectX11 VP2 前提**（OpenGL は要 GLSL 版）。
  ※ **dx11Shader はビューポートの「テクスチャ表示 ON（ホットキー 6）」でないと表示されない**（生成時に警告で案内）。
    カメラ位置/行列インデックス（ViewInverse[3]）は行/列メジャーで不安定なので使わず、ビュー空間法線 z で算出。
- **太さ=線の画面px幅 `maxWidthPx` uniform**（`FRES_MAXW`）。`_ensure_thickness_chain` の FRES 分岐が
  **`mB.output × SCRN_SCALE`(=`<ctrl>_fresMaxW`) を `maxWidthPx` へ接続**＝スクリーン輪郭と同じ px 換算なので、
  **同じ太さならフレネル＝スクリーン輪郭と同幅**（太さを下げればより細い一定幅）。ムラは出ない（帯方式を廃止）。
  line→dx11Shader は `FRES_LINK` message で特定。HLSL 既定 `maxWidthPx=3.0`。
  ※ `threshold`(`FRES_THRESH`) uniform と `_thick_target`→`shd.threshold`＋`mB.output×FRES_SCALE` の接続は
    後方互換で残置（現行シェーダは threshold を使わない＝一定px幅方式）。`_thick_target` は FRES で shd.threshold を返す。
- **scriptJob 不要**（曲率の頂点ウェイトは使わない＝`_ensure_line_anim` で curv ジョブをスキップ）。
  カメラ依存。元へ `outMesh`+`parent/scaleConstraint` で追従、z-fight 回避に `textureDeformer` 微小オフセット(lock)。
- 色は dx11Shader の `lineColor` uniform（`FRES_COLOR`）を直接変更。SG 差し替えはしない（線が消えるため）。
  曲率起伏/上限/下限/プロファイルは無効（UIには出るが効かない）。
- ※ 一度 UI を廃止したが、スクリーン輪郭が出せない「内側の折れ目/溝（重なり部の occluding contour）」に
  線を出す用途で**復活**（`create_fresnel_outline`）。**専用ボタンは置かず、チェックボックス
  「スクリーン輪郭に溝ライン（フレネル）も生成」(`chk_scrn_fresnel`)** で、スクリーン輪郭生成時に同じ
  メッシュへフレネルも一緒に作る運用（`create_screen_outline` 末尾で ON なら元選択へ `create_fresnel_outline`）。
  スクリーン輪郭（外周）＋フレネル（内側の溝）の併用。溝の出方は太さ＝facing しきい値で調整。

### スクリーン輪郭（クリップ空間押し出し・隙間なし均一太さ）

ジオメトリ式ハル（textureDeformer のワールド法線オフセット）は、押し出しで元メッシュとライン
メッシュの間に**3D の隙間**ができ、グラジング角でその隙間が見えて輪郭が浮く/交差する。これを
解決するのが `create_screen_outline`（ボタン「スクリーン輪郭」、`SCRN_TAG`）。**ビューポート/リアルタイム
用途では、ワールド押し出しハルの『浮き隙間』もチューブラインの『継ぎ目隙間』も出さない本命方式**（元メッシュ
自身の連続シェルを画面上で広げるだけなので、両方の隙間が原理的に出ない）:
- dx11Shader の**頂点シェーダでクリップ空間（画面上）へ一定ピクセル押し出す**（`_SCRN_FX_HLSL`）。
  `clip = pos×WVP`、ビュー空間法線 xy 方向へ `clip.xy += normalize(sn) * thickness * (2/viewport) * clip.w`。
  押し出しが**元シルエットと同じ深度のまま画面上で広がる**ので隙間が出ず、`clip.w` 補正で**均一ピクセル太さ**。
- カリングは使わず、頂点シェーダで **深度を「ビュー空間で一定量」奥へ押し込む**（`depthBias`＝`SCRN_DEPTH_BIAS`）。
  膨らんだシェルが少し奥に沈むので、**別オブジェクトの元メッシュがシェルの内側を覆い、外周リングだけが残る**＝輪郭。
  旧 `gZBias`(clip 空間NDC一定)は**遠距離で房どうしの深度差がNDC上で圧縮され重なり線が消える**問題があった。
  - **VS の実装で超重要**: `clip.xy`/`clip.w`（＝太さの一定px補正）は**元の `gWVP` から取って不変**にし、
    **深度 `clip.z` だけ**を差し替える。手順: `clip=pos×gWVP` → `vpos=pos×gWV; vpos.z-=depthBias;
    clipB=vpos×gProj` → `clip.z=(clipB.z/clipB.w)×clip.w`。**`clip.w` を触ると近接で線が消える/太さが距離で
    変わる**（バイアスが w を変えシェル位置が距離依存でスケールするため）＝一度踏んだ罠。だから w は必ず WVP 由来。
  - 量/符号は UI スピン「重なり深度(スクリーン)」で調整（`_scrn_set_depth_bias` が全 SCRN ラインの shd へ setAttr、
    既定 `_SCRN_ZBIAS`=0.02）。符号が逆だと塗りつぶし＝UIで反転可。`gProj`(Projection semantic)は深度算出のみに使用。
- 太さ＝dx11Shader の `thickness`(px) uniform。`_thick_target` が SCRN のとき返し、`_ensure_thickness_chain`
  が `mB.output × SCRN_SCALE`(6.0) を流す。色は `lineColor`（フレネルと共通の dx11 線色処理）。
- 元へ `outMesh`(offset=0 lock)+`parent/scaleConstraint` で追従。
- **曲率起伏に対応（頂点カラー駆動）**: 頂点シェーダが **`COLOR0.r`＝太さ倍率**を push に乗算する
  （`_SCRN_FX_HLSL`。カラー未設定/0 は 1.0 扱いで線が消えない安全策）。ラインに曲率用カラーセット
  `SCRN_CSET`(`toonScrnCurv`, clamped=False, deformer より下流の polyColorPerVertex)を作り
  （`_init_scrn_color_set`）、`_update_scrn_curv_weights` が hull と同じ重み計算(`_line_weights`＝
  curvature/cap/cmin/末端細り/プロファイル)を om2 `setVertexColors` で R へ書く。`_ensure_curv_jobs` の
  scriptJob は `_update_line_weights`(ディスパッチャ)経由で SCRN は頂点カラー、それ以外は deformer
  weightList へ振り分け。曲率の再計算タイミングは hull と同じ（スライダー変更時。再生の毎フレームでは
  再計算しない）。プロファイル/末端細りは長手カーブが無いため実質無効（curvature のみ効く）。
  ※ **実機対応（版0.1.6）**: dx11Shader が色セットを自動で COLOR0 に流さない版（Maya 2024 等）があり、
    その場合 `IN.Color.r=0`→シェーダ下限1.0で**常に一定太さ**になる。`_bind_scrn_color_set_to_shader` が
    dx11Shader の **varyingParameters.source を色セット名(`SCRN_CSET`)へ明示バインド**して解消
    （割り当て時に呼ぶ）。失敗時は警告＋診断 `_dump_dx11_varying('<shd>')` を案内。バージョン差は要確認。
- **VP2(DirectX11)・テクスチャ表示 ON(6) 前提**、カメラ依存、Hardware 2.0 バッチ対応。
- 別案として **スクリーンスペース深度エッジ検出**（`OG_Edge_Outline.py` / `MRenderOverride`）も試験的に用意。
- ※ 一度 UI を廃止したが、ビューポート用途では隙間問題の唯一の幾何解のため**復活**（`create_screen_outline`
  ＋ボタン「スクリーン輪郭」）。弱点は「手前の別オブジェクトにリングが隠れる」＝正しい遮蔽挙動で、`gZBias`
  の押し込み量で詰める。
- **v0.1.15 均一太さ化（背面法等価）**: v0.1.11 以降の GS シルエット方式は、頂点ごとに
  `scale/(scale+w)` 距離 atten を掛けていたため、シルエット上の w が大きく変わるメッシュで
  線が波打ち、また quad の perp を**エッジ方向から**算出していたため隣接三角形間で perp が
  数度ずれて外側コーナーに pie-slice 隙間ができ「破線」化していた（centroid による外向き判定も
  2 front+1 back/1 front+2 back で結果が反転して不安定で、旧非対称 quad の内側 15% が
  「面に黒」の原因でもあった）。v0.1.15 で:
  - 距離 atten を**完全撤去**（背面法と同じピクセル一定太さ＝ `thickness * cr * wav` px）
    ※ v0.1.55 で既定を「3D の長さ」（寄れば太く・引けば細く）に変更。下の「線の太さを 3D の長さで」を参照
  - perp を**補間 VSNrm の NDC 射影方向**で算出（`perp_from_normal`）。共有クロス点では隣接
    三角形も同じ補間値 → perp が完全一致 → **真の miter joint** で quad 外側コーナーがピクセル
    一致 → 破線化が消える。法線は正の側＝外側を向くので旧 back_avg 判定は不要。
  - quad を**完全外向き**（内側 0、外側 thickness）化 → 面に黒が原理的に出ない。
  - `p.w` を符号付きで扱い、カメラ裏 (`p.w < 1e-3`) のクロスは丸ごと捨てる（旧 `abs(p.w)` は
    視点裏で quad が flip する暴走源）。
  - PShader の UV は V∈[0,1]（0=シルエット側, 1=外側）。ブラシ roughness は `|UV.y - 0.5|*2`
    で両端ほど discard。

### 画像空間エッジ検出（オブジェクト単位・重なり線をメッシュの上に描く／VP2・実験）

スクリーン輪郭は深度を尊重するため、**手前のメッシュ（自己重なりの前側の房や別オブジェクト）に重なり線が隠れて細る**。
「重なり線をメッシュの上のレイヤーに出したい」用途に、後処理の深度エッジ検出を本体へ統合（UIチェック
「重なり線をメッシュの上に描く（画像空間エッジ・実験）」、`_edge_enable`/`_edge_disable`、`EDGE_OVR_NAME`）:
- **`MRenderOverride`（om2 `maya.api.OpenMayaRender`）** を実行時登録。**レンダーAPIは関数内で遅延 import**（この機能を
  使わなければ import されない＝本体は Py2 でも import 可能・API 欠如環境でも壊れない）。初期化失敗は握りつぶし False。
- **★オブジェクト単位維持のキモ**: パスを2段に分け、①全シーンを色ターゲット `tColor` に描く（背景・他オブジェクトは
  通常表示）②**対象メッシュだけ**を深度ターゲット `tDepth` に描く（`objectSetOverride` が `_EDGE_TARGETS` の
  MSelectionList を返す・深度のみクリアで色は保つ）。フルスクリーン quad が **`tDepth` の深度ラプラシアン**で段差
  （対象の外周＋自己重なり）を検出し `tColor` に線を合成（`_EDGE_FX`）。→ **線は対象メッシュにだけ乗る**。
- 対象は **ON 時の選択メッシュ**（`_on_toggle_edge` が transform を `_edge_set_targets` へ）。しきい値/太さ/線色は
  `_edge_set_params`（線色は共通カラー追従）。UI に「しきい値/太さ」スピン。ウィンドウ closeEvent で `_edge_disable`。
- **VP2/DirectX11・ビューポート専用**。弱点: 画面合成なので**手前の別オブジェクトより線が上に出る**ことがある
  （前後関係は未考慮）。厳密な遮蔽が要るなら対象深度 vs シーン深度の比較を追加する余地あり。
- ※ `MRenderOverride` は**実機(VP2)でのみ動作**。**実機 Maya 2024 で検証済み**（版0.1.6）: 深度線形化
  (`linearizeDepth`/`gNear`/`gFar`、near/farは `_edge_active_camera_near_far` でアクティブカメラから取得)＋
  相対しきい値＋HiDPI(実ピクセルでターゲット作成)で**かすれを低減**。`debug` uniform で深度可視化可。
  残課題: 残りのかすれ/しきい値感度/厳密な前後遮蔽（対象深度 vs シーン深度比較）。om2 render API の
  シグネチャ（`objectSetOverride` 等）は Maya バージョン差の可能性があり、動かない場合の第一容疑者。
- 元になった単体版 `OG_Edge_Outline.py`（全ビューポート対象）も残置。本体統合版はこれをオブジェクト単位に拡張したもの。

### 交差線・隠れ線とオーバーライドの共通基盤（v0.1.40〜v0.1.46）

画像空間の線（交差線・旧エッジ・隠れ線）は全て 1 つの `MRenderOverride`（`EDGE_OVR_NAME`）で描く。
- **有効/無効は `_og_override_sync()` に一本化**: 交差線/旧エッジは `_OVR_STATE`（UI トグル）、隠れ線は
  紐づけキャッシュ `_HIDDEN_GROUPS` が空でない かつ `ROOT.toonHiddenOn`（シーン保存・既定 ON）。
  直接 `_edge_enable/_edge_disable` を呼ばない。パス列は `_EdgeOverride._build_ops()` が毎フレーム組む
  （必要な機能のパスだけ）。オーバーライドは**全モデルパネル**へ適用（どのパネルでプレイブラストしても同じ絵）。
  ただし v0.1.53 から、他のオーバーライドを使っているパネルは取らない（後から作ったパネルはフォーカス時に入れる）。
- **シェーダファイルはファイル名に版を入れる**（`OG_EdgeOutline_v<ver>.fx` / `OG_ToonScreen_v<ver>.fx` /
  `OG_ScrnHidden_v<ver>.fx`、`_write_fx_file`）。dx11Shader・`MShaderManager.getEffectsFileShader` は
  **パス単位でコンパイル結果を使い回す**ため、同じパスで中身だけ変えても反映されない（v0.1.43 で踏んだ）。
  既存 SCRN ラインは `_upgrade_scrn_shader` が新パスへ差し替え、入力接続（太さの駆動）と値を戻す。
- 交差線（v0.1.40〜45）: 対象 = SCRN ラインで `toonIntersectOn`（右クリックで個別切替）かつ表示中の
  元メッシュ。交差線モードでは self-silhouette を混ぜない（円筒の接線シルエットが縦線に見えた）。
  `intersectEdge` は「scene 最前面が target 側/other 側に隣接」を条件に奥の擬似交差を捨てる。
  太さは SCRN 本体の mB.output×SCRN_SCALE に合わせる。
- 複数メッシュ選択で生成すると 1 グループ（`a_b_outlines` / `a_b_etc_outlines`）にまとめる（v0.1.42）。

### ライン詳細設定（v0.1.46・右クリック →「ライン詳細設定…」）

**ビューポート＝マスターの値、プレイブラスト＝レンダーレイヤーを切り替えて書き出す**、という運用に合わせ、
ラインの見え方をレンダーレイヤーごとに変えられる（`_LineOptionsDialog`）。SCRN ラインのみ。
- 見え方の属性はライン transform に置く（`LN_*`: 表側の線/線色/隠れ線モード・色・濃さ・破線）。
  線色 `toonLineColor`→`shd.lineColor`、表側 `toonShowVisible`→`shd.showVisible` は**接続**しているので、
  SCRN の色を変えるときは `_scrn_set_color(line, rgb)` を使う（シェーダへ直接 setAttr しない）。
- **レイヤーごとの値 = Render Setup の絶対オーバーライド**（`_rs_set_override` / `_rs_remove_override`、
  コレクション名 `<layer>_OG_<line>`）。ダイアログでレイヤーを選ぶと表示レイヤーも切り替わる
  （値の読み書きは常に表示中のレイヤー＝getAttr でオーバーライド後の値が読める）。
- **隠れ線 = 紐づけた物の裏に回った所だけ**描く（紐づけ無し＝描かない）。紐づけは
  `toonHiddenOccluders`（物.message → multi。全レイヤー共通・リネームに強い）。旧「何の裏でも透ける
  2nd pass」は撤去（SCRN 本体の technique は p0 のみ）。
  - 描き方: オーバーライド内で、紐づけ先グループごとに `_SceneOcc`（紐づけ先だけの深度 tOccDepth）→
    ラインごとに `_SceneHiddenLine`（線のシェルに shaderOverride で `_SCRN_FX_HLSL_HIDDEN` を当てる）。
    隠れ線 PS は深度テストを使わず「線が紐づけ先より奥 かつ 紐づけ先がその画素で一番手前」の所だけ描く
    （手前に別の物があれば描かない）。shaderOverride では色セットの頂点ストリームが来る保証が無いので
    隠れ線版は COLOR0 を使わない（`#define OG_NO_COLOR0`）。パラメータは毎フレーム `_hidden_set_shader_params`
    がライン属性から読む＝表示中レイヤーの値がそのまま出る。**DirectX11 のみ**（OpenGL は未対応）。
  - 「このラインの隠れ線だけのレイヤーを作る」（`create_hidden_only_layer`）: 表側の線を OFF にし、元メッシュと
    紐づけ先は useBackground で深度だけ書く（透かす判定に必要）。別素材として合成する用途。
- シーンを開き直すと `_on_scene_changed`（scriptJob）が紐づけを読み直してオーバーライドを有効化する。
  **v0.1.48: インストーラが userSetup.py に目印付きの数行（ASCII のみ・バッチでは何もしない・毎回置き換え）を
  書き足し、Maya 起動時に `boot()` が UI を出さずに scriptJob 登録と準備だけ行う**（ツールを一度も開かずに
  レイヤー切替→プレイブラストしても隠れ線が出る）。`_ensure_user_setup`（インストーラ）は既存内容をバイト単位で保持。
- レイヤーのライン用コレクションは**中身（静的選択）で探す**（`_rs_line_collection`）。Render Setup の静的選択は
  リネームに追従するが、コレクション名は古いまま残るため、名前で探すとリネーム後に見失う。
- オーバーライドは背景のアルファを 0 でクリアし、合成パスもアルファを引き継ぐ（v0.1.48）。これで PNG の
  プレイブラストで背景が透明になり、「隠れ線だけのレイヤー」は線だけ不透明＝そのまま合成素材になる
  （useBackground の物もアルファ 0）。
- **`refresh_tree` から呼ばれる処理は「値が違うときだけ setAttr」**（`_set_if_diff`）。毎回同じ値を書くと
  Undo 履歴に積まれ、直前の操作（ライン生成・紐づけ等）が Ctrl+Z 1 回で戻らない（v0.1.46 で
  `_set_outliner_color` / `_lock_trs` / `_ensure_thickness_chain` の係数 / `_apply_line_selectable` /
  `_tuck_handle` / `showHidden` を修正）。新しく「確保」系の処理を足すときも同じにする。
- **オーバーライドの描画先の大きさ**: ビューポートではパネルの実ピクセル（HiDPI 対策）、プレイブラストでは
  `outputTargetSize()`（書き出し解像度）。パネルの大きさで描いて縮めると線の太さが変わる（v0.1.46 で修正）。

### ブラシ（v0.1.49・ライン詳細設定の『ブラシ』タブ、ラインごと＋レイヤーごと）

SCRN のみ（DX11 HLSL。OGSFX は未対応＝属性はあっても効かない）。ライン属性 `toonBrush*`（`BRUSH_ATTRS`）を
シェーダ uniform へ接続し、Render Setup の上書きでレイヤーごとに変えられる。メイン画面の「ブラシ:カサつき/
ゆらぎ/周期」は**変えた項目だけ**全ラインのマスター値へ一括で入れる（`_scrn_set_brush`）。
- 入り抜き `brushTaper`: GS で、輪郭セグメントと視線のなす角の sin `s`（輪郭の途切れる点＝カスプで 0）から
  太さを細くする。局所計算なので連結情報は不要。**正面向きのドーナツのような閉じた輪郭では効かない**（端が無い）。
  検証は低い角度のトーラスで行う。
  - **v0.1.51: 鞍形の三角形だけに効かせる**。`s` は三角形ごとの値でふつうの輪郭でも 0.6〜0.95 でばらつく
    （トーラス外周で実測）。旧 `pow(s, 4*taper)` はこれを外周の太さの起伏にしていた（利用者指摘）。
    カスプは鞍形（ガウス曲率が負）の所にしか起きない（Koenderink）ので、頂点法線の三角形と位置の三角形の
    向きで判定（`kd = (m1×m2)·f̂ / (|m1||m2|)`、縮尺・密度によらない。実測: 凸の外周は最小 -0.53、
    内側の輪郭は -0.90 以下）し、`smoothstep(0.6, 0.85, -kd)` で効かせる。細り方は
    `smoothstep(0, 0.3+0.55*taper, s)`（1 に近い所まで広げると首のような鞍形の途切れない輪郭で起伏が出るので
    上限 0.85）。ハードエッジ（法線が面ごと）では効かない。
  - 検証: `gui_brush_check` で外周の画素数が変わらない／内側の弧が減る／くびれた筒（首）の外形が変わらない。
- かすれ `brushDry`: PS で画面座標の値ノイズ（`vnoise`）で途切れさせる（縁ほど欠けやすい。100 でも形は残る）。
- よれ `brushWobble`: 交点の画面位置の値ノイズで線全体を外側へずらす（共有交点は同じ値＝継ぎ目で切れない）。
- 二重線 `brushDouble`: GS で 2 本目の細い quad を外側に出す（`maxvertexcount(8)` + `RestartStrip`）。
- プリセット（`BRUSH_PRESETS`）は全ブラシ属性をまとめて 1 回の Undo で入れる。

### エッジ線・クリース線の隠れ線と詳細設定（v0.1.50）

ライン詳細設定・隠れ線の対象は `_is_detail_line`（SCRN または EDGE_TAG のチューブ）。ブラシタブは SCRN のみ
（チューブは形で太さが決まるため無効表示）。
- **チューブは詳細設定で値を変えた時点で初めて変換**（ダイアログを開くだけでは何も書かない）。`_ensure_line_render_attrs`
  がライン属性（`LN_*`）を足し、**専用 surfaceShader**（`_tube_own_shader`＝個別カラーと同じ `COL_PREFIX+<line>_SS/_SG`）に
  `LN_COLOR→outColor`、`LN_SHOW_VIS→reverse(<line>_OG_visRev)→outTransparency` を接続。以降は共通カラー一括変更の
  対象外（個別色）で、メイン画面の色変更は `_scrn_set_color` 経由で `LN_COLOR` に入る（接続済みの SS へ直接 setAttr しない）。
- 隠れ線パスは SCRN と同じ `_SceneHiddenLine` で、チューブには `_TUBE_HIDDEN_FX`（GS なし・`OG_TubeHidden_v<ver>.fx`）を当てる。
  判定（紐づけ先より奥 かつ 紐づけ先が一番手前）は SCRN と同じ。
- **破線の位置はチューブの UV を使わない**: 中心線（ライン配下の一番長いカーブ＝polyToCurve）を `_tube_axis` が om2 で
  ワールドの折れ線（最大 `_TUBE_AXIS_MAX`=128 点、多いと間引く）にして `gAxX/gAxY/gAxZ`（`setArrayParameter`）で渡し、
  PS で画素のワールド位置を一番近い区間へ投影して「線に沿った長さ」を出す。px 換算は線の中心（`_tube_center`）の深さ
  1 点で決める（＝破線はワールドで等間隔。頂点ごとの深さだとチューブの手前側と縁でずれて「く」の字になった）。
  ※ UV の V は、角の輪が斜めに切られているため長い面の 2 つの三角形で向きがずれてジグザグになり、さらにエッジ 1 本ごとに
  同じだけ進むので長いエッジで破線が伸びる。画面上の V の変化で割る方式は誤差が積み重なって市松模様になった。
- 描画中（毎フレーム）に呼ぶ処理で **`cmds.arclen` などコマンドを使わない**（Undo 履歴に積まれ、直前の操作が Ctrl+Z
  1 回で戻らなくなる）。om2 で測る。
- 検証: `gui_tube_check`（箱の前面 4 辺のチューブ＋前髪に見立てた板、破線が線に直角に切れているか＝半分だけ描かれた列の
  割合、間隔のばらつきも数える）。

### 曲率ジョブ・重みの書き込み・名前空間（v0.1.52）

- **deformer の重みは `_write_deformer_weights`（om2 の MArrayDataHandle で一括）で書く**。`cmds.setAttr` に
  全頂点ぶん渡すと 4 万頂点で 6〜8 秒かかり、scriptJob から呼ぶと Undo 履歴に積まれて「チャンネルボックスで
  曲率を変えて Ctrl+Z 1 回」で戻らなかった。om2 なら 0.02 秒で Undo 履歴にも積まれない。
  **その代わり重みの Undo は曲率ジョブ任せ**（属性を Undo → attributeChange ジョブ → 重みを計算し直す）なので、
  重みを決める属性は全部ジョブで見る（`CTRL_PROFILE` も対象）。新しく重みに効く属性を足すときはジョブにも足す。
- 曲率ジョブ・キャッシュは**ノードの UUID＋名前空間がキー**（`_ukey` → `UUID@chr`）。名前がキーだと、生成時の
  短い名前とツリー更新時のフルパスで 2 組張られて 1 回の変更で何度も計算し、リネーム後は古い名前で何もしなかった。
  **同じファイルを 2 回リファレンスすると両方のノードが同じ UUID になる**（2024 で確認）ので名前空間も付ける
  （付けないと 2 体目のジョブが 1 体目を消し、同じ値のとき計算を省いて 2 体目が変わらない）。
  ジョブの呼び出し先 `_og_curv_job_cb` はラインを MObjectHandle で持ち、呼ばれた時点のパスを引く。
  `_CURV_JOBS[key] = (jobs, ctrl の MObjectHandle)`。同じキーでも実体が違えば（同じファイルを開き直した等）張り直す。
- om2 で書いた重み・頂点カラーは、**リファレンスしたラインでもショットに保存されて開き直すと残る**（2024 で確認）。
  開いたときに計算し直す必要はない。
- `_curv_sig`（曲率まわりの値＋頂点数）が前回と同じならジョブは計算を省く（UI は直接計算したうえで属性ごとの
  ジョブも走るため）。キャッシュは頂点数が変わったら作り直す。
- **モジュールを読み込み直す（GitHub 更新・reload）と古いモジュールのジョブが残る**。import 時に
  `_kill_curv_jobs_everywhere`（ジョブ一覧で `_og_curv_job_cb`／旧版の `_ensure_curv_jobs` を探して kill）→
  `_ensure_all_curv_jobs` で張り替える。
- シーンを開いたとき（`_on_scene_changed`）は**全部消さずに** `_ensure_all_curv_jobs`（もう無いラインの分だけ片付けて、
  足りない分を張る）。シーン切替のイベントは Maya が忙しいと数秒遅れて届くことがあり、全部消して張り直すと
  その間に変えた値で呼ばれるはずのジョブまで消えた。これでウィンドウを一度も開かなくても曲率のキーが効く。
- **`cmds.file(new=True)` のシーン切替イベント（NewSceneOpened/deleteAll）はすぐには届かない**（スクリプトから
  実行・2024。開いたときは deleteAll→NewSceneOpened→SceneOpened が届く。開くと 2 つのイベントで
  `_on_scene_changed` が 2 回走る）。消えたノードのジョブは一覧に残り続けるので、`refresh_tree` /
  `_ensure_all_curv_jobs` の `_prune_curv_jobs` で**isAlive でない**物だけ片付ける（isValid で判定すると、
  削除を Undo で戻したラインのジョブまで消してしまう）。
- **新しく作るノードの名前は `_base(元)`**（DAG パスを外し、ネームスペースは `_` でつなぐ: `chr:head` →
  `chr_head_outline`）。`_short` のままだとリファレンスのメッシュから作った線が `chr:` の中に入った。
  複製した shape は元の名前空間の名前のまま残るので `_shapes_out_of_ns` で付け直す。
- **コンポーネントはフルパスで指定**（`"{}.e[{}]".format(obj フルパス, i)`）。短い名前だと同じ短い名前の
  メッシュ（charA|box / charB|box）で選択に失敗し、クリース線が 1 本も作られないまま黙って終わっていた。
- リファレンスしたアセットの ROOT（`chrA:toonOutlines_grp`）も `_all_roots()` で拾う（隠れ線・SCRN 一括処理）。
  UI のツリーは今もこのシーンの ROOT だけを表示する。
- ライン単体表示は元の割り当てを**面ごとに JSON で**覚える（`_mesh_sg_assignments`／旧形式の SG 名だけにも対応）。
- Esc は QDialog の `reject()` でウィンドウを隠すだけ（closeEvent が来ない）ので、`reject` でも `_teardown` する。
- ライン削除は `_CTRL_NODE_SUFFIXES` の乗算・条件ノードと SCRN/フレネルの専用シェーダ・SG も一緒に消す。
- 検証: `gui_fix52_check.py`（4 万頂点の生成/再計算時間、ジョブ 1 組、チャンネルボックス/UI/プロファイルの
  Ctrl+Z 1 回、キーの追従、リネーム直後、複数選択、モジュール読み直し、リファレンス、同名、削除の残り、Esc、
  ウィンドウを開かずに開いたシーンでのキーの追従）と `gui_refw_check.py`（リファレンスしたラインの曲率変更→
  保存→開き直し、同じアセットの 2 回リファレンス）。**値を変える手順と測る手順は別のステップに分ける**
  （曲率ジョブはアイドル時、キーの追従はビューポートの評価で走る。同じ手順で測ると変わっていないように見える）。

### レイヤー・オーバーライド・Undo・正投影まわり（v0.1.53）

- **ライン用コレクションは名前に "_OG_" を含み、中身にラインが入っている物で探す**（`_rs_line_collection`）。
  コレクション名は作ったときのレイヤー名（`<layer>_OG_<line>`）のまま残るので、レイヤー名の前方一致で探すと
  レイヤーの名前を変えた後に見失い、2 つ目を作っていた。
- ↺（`_rs_remove_override`）で上書きが 1 つも残らなくなったコレクションは消す（残すとラインがそのレイヤーに
  入ったまま＝上書きをやめる前の状態に戻らない）。Render Setup の削除も Undo 1 回で戻る。
- **他のオーバーライド（Arnold のビューポート描画など）を使っているパネルは取らない**（`_edge_enable` が
  rendererOverrideName が空か自分のパネルだけに入れる）。後から作ったパネルは `ModelPanelSetFocus` の
  scriptJob（`_on_panel_focus`）でフォーカスが来たときに入れる。
- **隠れ線の対象一覧 `_HIDDEN_GROUPS` は Undo/Redo・名前の変更（scriptJob）で読み直す**（`_request_hidden_refresh`
  → 次のイベントループで `_on_hidden_dirty` を 1 回だけ。名前の変更は他のスクリプトが大量に起こすこともあるので
  イベントでは予約だけ）。描画中にパスが無くなった（親を変えた等）のを見つけたときも同じ予約をする
  （描画中に Maya のコマンドでオーバーライドを付け外ししない）。
- シーン単位の scriptJob は `_SCENE_JOB_FUNCS` の関数名で見分けて `_kill_scene_jobs` で消す（モジュール再読込で二重にしない）。
- **ツリー更新（refresh_tree）は、名前変更・グループ移動・新規グループの Undo のまとまりの中で呼ぶ**。
  ツリー更新はコントローラーの付け替え・グループ倍率のつなぎ直し・グループのコントローラー作成を行うので、
  外で呼ぶと Ctrl+Z 1 回で戻らない。
- **太さ計算ノード（`<ctrl>_thkA` など）はコントローラー名を元に名前で探す**ので、コントローラーの名前を変えたら
  `_rename_ctrl_nodes` で合わせる（`rename_node`）。アウトライナーでコントローラーの名前を変えられた場合も、
  `_ensure_thickness_chain` が `ctrl.thickness` の先の `*_thkA` を見つけて名前を合わせる（作り直すと古いノードが残る）。
- 太さプロファイル（`_RampWidget`）は押してから離すまでを Undo 1 回（`dragStarted`/`dragFinished` →
  `_begin_drag`/`_end_drag`）。クリックで足した点もすぐ反映する。`_begin_drag` は二重に開かない。
- ライン詳細設定はラインを MObjectHandle で持ち、使うたびにパスを引く（`_LineOptionsDialog._lines` プロパティ）。
- `_upgrade_scrn_shader` は**いまのビューポート（DX11/OpenGL）と合わない種類のシェーダノードには触らない**
  （DX11 のシーンを OpenGL で開くと dx11Shader に .ogsfx を差していた）。
- **正投影カメラ**: 深度は線形（`near + z*(far-near)`）で、相対差の基準は距離ではなく正投影の幅
  （`gOrtho`/`gOrthoScale`、`og_scale`）。距離基準だと 1000 離れた front カメラで 2 単位以内が同じ深さ扱いになり、
  隠れ線が出なかった。チューブの破線の px 換算も正投影では距離で割らない。Python 側は
  `_edge_active_camera_near_far` がカメラの orthographic/orthographicWidth を控え、`_edge_set_ortho_params` で渡す。
- 検証: `gui_rest_check.py`（レイヤー名変更・↺・他のオーバーライド・プロファイル・名前変更/移動/新規グループの
  Undo・隠れ線一覧の追従・詳細設定の追従）、`gui_ogl_check.py`（OG_PART=save を DX11 で → open を OpenGL で）、
  `gui_ortho_check.py`（front カメラの隠れ線）、`gui_isect_check.py`（交差線 透視/正投影）。
- ラインがレンダーレイヤーのコレクションに入っていると、名前を変えたとき Render Setup が「静的選択を新しい名前に
  書き換える」操作を Undo 履歴に足す（Maya の動作）。名前変更の Undo が 1 回多く要るのはそのため。

### リファレンスの線・色トレス・設定の保存/読み込み・選択の連動（v0.1.54）

- **別 ROOT（リファレンス等の `chrA:toonOutlines_grp`）の下のノードは `_is_foreign` で見分け、構造に触らない**。
  `_ensure_line_anim`（曲率ジョブだけ張る）・`_ensure_group_ctrl`（`_ctrl_of` を返すだけ）・`_ensure_thickness_chain`
  （何もしない）。触るとこのシーンの全体コントローラーへ太さをつなぎ替え、ROOT まで作ってしまう。
  全体倍率は `_target_global_ctrl`（選択が別 ROOT ならそのキャラの全体コントローラー `_global_ctrl_for`、ラベルに名前）。
  一覧は `_add_foreign_root_item` で ROOT ごとの見出し（ドラッグ/ドロップ不可）。名前変更・削除・移動は断る。
  値の変更・キー・詳細設定はリファレンスの編集としてショットに保存される（保存→開き直しで確認済み）。
- 個別カラーのノード名は `COL_PREFIX + _base(line)`（名前空間を `_` に）。既存の専用シェーダは名前ではなく
  つながり（LN_COLOR → surfaceShader、または色トレスの blendColors の先）／割り当て中の SG で見つける。
- **ラインの元メッシュは `_line_source_mesh`**。エッジ線・クリース線は deformer の入力がチューブ自身なので、
  **`polyEdgeToCurve`**（`polyToCurve` コマンドが作るノードの型）の `inputPolymesh` から引く。
  `inputComponents` は**辺ではなく頂点の並び**（`vtx[2]`, `vtx[3]`…）。以前はチューブ自身を元メッシュ扱いし、
  ライン単体表示でチューブの線まで背景色に差し替えていた。
- **色トレス**: スクリーン輪郭は頂点カラー `SCRN_CSET` の **R = 太さ倍率、GBA = 面のマテリアルの色**
  （`_update_scrn_curv_weights` が `_scrn_trace_colors` のキャッシュから書く。頂点は最初に見つかった面の色）。
  シェーダが `traceAmount`/`traceDark` で線の色に混ぜる（`og_trace_color` で少し鮮やかにして暗くする）。
  ライン属性 `LN_TRACE`/`LN_TRACE_DARK` を uniform へ接続＝レイヤーごとに上書きできる。
  エッジ線・クリース線は `_ensure_tube_trace` が DG を組む: `LN_TRACE_RAW`（隣の面で一番多いマテリアルの色）×
  `LN_TRACE_DARK` → blendColors（color2 = LN_COLOR、blender = LN_TRACE）→ 専用 surfaceShader。
  マテリアルの色は `_sg_base_color`（baseColor/color/…、テクスチャがつながっていれば colorAtPoint の平均）。
  マテリアルを変えたら詳細設定の「面の色を読み直す」（`_trace_prepare(recompute=True)`）。背面法の線は対象外。
  OpenGL 版（OGSFX）のシェーダには入れていない。
- **設定の書き出し/読み込み**（`export_line_settings`/`import_line_settings`、⋮ メニュー）: マスターレイヤーで
  値を読む／書く。線の対応は (元メッシュの名前, 種類, 線の名前) → 合わなければ (元メッシュの名前, 種類) の並び順。
  名前空間は見ない。紐づけ先は線の元メッシュと同じ名前空間の同名の物を優先。キー・接続のある値は飛ばす。
  レイヤーごとの上書きは、同名のレイヤーがあるときだけ作る。Undo 1 回で戻る。
- **選択の連動**: UI の `SelectionChanged` ジョブ（`_on_maya_selection`、`_teardown` で消す）が、選んだメッシュ →
  `_src_to_lines`（refresh_tree で作る）→ 一覧で選ぶ。このときは `_syncing_sel` で Maya の選択（メッシュ）を
  コントローラーに置き換えない。右クリック「元のメッシュを選ぶ」（`select_source_meshes`）。
- 検証: `gui_feat54_check.py`（39 件）・`gui_uigrab54_check.py`（画面の撮影・幅）。
- **Maya の `updateRenderOverride`（新しいシーンの直後や Arnold のレンダラーメニュー更新で遅れて走る）が、
  パネルのオーバーライドを空に戻す**ことがある（persp だけ空になり、そのパネルのプレイブラストに隠れ線が
  出なかった）。オーバーライドが有効な間は `_PANEL_GUARD`（1 秒の QTimer、`_guard_panels`）が空のパネルにだけ
  入れ直す（他のオーバーライドのパネルは取らない）。`_edge_disable` で止める。
  **v0.1.55: タイマーから直接パネルを触らず `cmds.evalDeferred(_guard_panels_apply, lowestPriority=True)` で
  Maya が手すきのときに入れ直す**（Qt のタイマーはプレイブラストの最中にも呼ばれるため、その最中にパネルを
  触らないための予防）。v0.1.55 ではこれが開き直しで落ちる件の原因だと考えたが誤りで、本当の原因は
  下の「開き直しで落ちる件（v0.1.56）」の GC だった（ガードを変えると落ちる回数が変わったのはタイミングのため）。
  **描画中・プレイブラスト中に走りうる Qt タイマーからは Maya のパネル/オーバーライドを変えない**（変えるなら
  evalDeferred）。落ちた場所は検証スクリプトで `faulthandler.enable(file=...)` すると分かる（OG_FAULT=1）。
- **ツールのウィンドウを閉じるときは `_find_tool_windows` で先に集めてから `_close_and_delete`**（`show()` /
  `_close_tool_windows` / `show_line_options`）。**`_close_and_delete` は deleteLater のあと、そのウィンドウ宛ての
  DeferredDelete をその場で処理して破棄する**。deleteLater だけだと、新しいウィンドウを作っている最中（ツリー更新の
  描画などでイベントが回ったとき）に古いウィンドウの破棄が走り、Maya ごと落ちた（fix52 の Esc→開き直しで毎回。
  v0.1.54 の変更の組み合わせで起きるようになった。v0.1.53 では起きない）。※ これも v0.1.56 で分かった GC の件が
  タイミングを変えて出ていた可能性が高い（その場で破棄する作りは残している）。
  `_edge_enable` も、何も変えていなければ `cmds.refresh()` しない（閉じる処理などから何度も呼ばれるため）。
- 検証で同じ名前の確認を 2 つ同時に流すときは、出力先（`-outDir`）と MAYA_APP_DIR を分ける（同じフォルダーに
  2 つの Maya が書き込み、ログが混ざって結果が読めなくなった）。`run_suites.ps1` は RESULT が出ないまま Maya が
  終わったら起動し直す（他の検証と並行して Maya が落ちることがある）。

### 線の太さを 3D の長さで（v0.1.55・背面法と同じく寄れば太く・引けば細く）

利用者の報告: キャラに適用するとカメラを引いたときに線がキャラに対して太くなる（スクリーン輪郭は画面上の px が一定のため）。
利用者の指定: **背面法と同じ振る舞い・基準距離は設けない**（太さはスライダーで合わせる）。
- スクリーン輪郭の GS（`og_thick_px`）で交点ごとに `thickness / thickUnitPx(=SCRN_SCALE) × 物のスケール × px/単位`。
  px/単位 = `0.5 × gScreen.y × gProj[1][1] / w`（w = 交点のクリップ座標の w。透視=距離・正投影=1 なので両方この式）。
  物のスケール = WorldView の各軸の長さの平均（ビューは回転だけ）。共有交点は同じ w なので継ぎ目で途切れない。
  **3D の長さ = UI 太さ × 倍率 × スケール＝背面法の textureDeformer.offset と同じ値**（同じ太さなら同じ幅。実測一致）。
  よれ・二重線も同じ太さから。画面の 1/4 で頭打ち（カメラに接したとき画面を覆わない）。
- uniform `thickWorld`（int、既定 1）。ライン属性 `LN_THICK_WORLD`（`toonThickWorld`、bool 既定 ON）から接続＝
  Render Setup でレイヤーごとに上書きできる。UI はライン詳細設定 → 見え方 →「太さ: 引くと細く（背面法と同じ）」。
  OFF で v0.1.54 までの画面 px 一定。隠れ線パスは `_hidden_set_shader_params` で同じ値を渡す。
- フレネル: PS で同じ換算（VS からクリップの w とスケールを渡す）。ライン属性は無く uniform 既定 1。
  **フレネルのファイル名にも版を入れた**（`OG_ToonFresnel_v<ver>`）。既存のフレネル線は `_prepare_scrn_lines` →
  `_upgrade_scrn_shader`（フレネルにも対応）で差し替え、入力接続（maxWidthPx/threshold）と色を戻す。
  ※ 球のような滑らかな面ではフレネルの帯は maxWidthPx よりかなり細く出る（面の粗さで facing が 0 になる前に縁が来る）。
- 交差線: `_scrn_intersect_world_thickness`（一番太い SCRN ラインの mB.output × スケール。そのラインが OFF なら 0）を
  `_EDGE_PARAMS["thick_world"]` に入れ、オーバーライドの PS で画素の深さから px に直す。Projection[1][1] は
  `_edge_camera_proj_yy`（om2 `MFnCamera.getPortFieldOfView(w, h)`＝フィルムフィット込み。正投影は幅と縦横比）。
- 既存シーンは**ツールのウィンドウを開いたとき**（refresh_tree → `_prepare_scrn_lines`）に新しいシェーダへ切り替わる。
  開くまでは前の版のシェーダ（画面 px 一定）のまま。開いた時点でシーンを書き換えないため（開いただけで「変更あり」に
  なるのを避ける）、シーンを開いたイベントでは差し替えない。
- 交差線の判定は `t`（太さ）先の 4 方向だけでなく `t/2`・`t/4` でも前後の反転を調べる（v0.1.55）。太い線で `t` が
  重なりの半分を超えると、`t` 先が重なりの外（片方の物しか無い）に出て線が消えていた（小さな物＋3D の長さの太さで発覚）。
- 旧「遠景で細くする」（v0.1.32、基準距離 `DIST_ATTEN_REF` で Python が `camDistAtten` を書く）は画面から外した
  （重ねると二重に細くなる・基準距離方式は利用者が不要とした）。値が入っている既存シーンでは今までどおり効く。
- 検証: `gui_thk55_check.py`（距離 60/120 の幅・背面法との比較・スケール 2 倍・OFF・Undo・レイヤー上書き・フレネル・交差線・
  書き出し/読み込み）、`gui_up55_check.py`（OG_PART=save を v0.1.54 のコピーで → open を現行で。ツールを開く前後）。

### 開き直しで落ちる件（v0.1.56・GC が一覧の変換中にトップレベルの部品を消していた）

症状: ツールのウィンドウを開き直す（Esc で閉じてから開く・`show()` を続けて呼ぶ）と、ときどき Maya ごと落ちる
（0xc0000374＝ヒープ破損。faulthandler では毎回 `show()` → `_find_tool_windows` の `topLevelWidgets()` の行）。
v0.1.54 から目立ったが、仕組み自体はそれ以前からあった。
- **原因**: 旧機能の状態を持つだけの非表示の部品（`chk_hide_handles`/`chk_lock_select`/`chk_occlude`/`chk_edge`/
  `chk_edge_debug`/`spn_edge_thr`/`spn_edge_w`）を**親無しで作っていた**。親の無いウィジェットはトップレベルになり、
  Python が持ったまま（ownedByPython・C++ 側は生きている）、閉じたウィンドウと参照の輪（`_OutlineTree.ui` など）に
  残って **GC でだけ消える**。`QApplication.topLevelWidgets()` の結果を Python のリストに変える途中で GC が走ると
  （ラッパーを大量に作るので走りやすい）、まだ変換していない（リストに参照の無い）その部品を GC が消し、
  消えたポインタを変換して落ちる。
- **直し方**: その部品は `QCheckBox(self)` のように**ウィンドウを親にして作る**（Python が持たず、ウィンドウと一緒に
  C++ 側で消える）。トップレベルの一覧は **`_top_level_widgets()`（GC を止めて取る）** を使う。他のツールが同じ
  ゴミを残していても落ちない。**部品を親無しで作らない（レイアウトに入れない部品は特に）、`topLevelWidgets()` を
  直接回さない。**
- 切り分けの決め手: GC を止めると落ちない（`gc.disable()`）。`gc.set_debug(gc.DEBUG_SAVEALL)` で片付けられる物を
  取っておき、`shiboken2.ownedByPython/createdByPython/isValid` で数えると「Python が持っている生きた部品」が
  ウィンドウ 1 つにつき 7 個出た。再現は `gui_gc56_check.py`（古いウィンドウをゴミのまま残し、`gc.collect(0)` で
  gen0 の数を 0 にしてから小さいしきい値で開き直す＝一覧の変換の途中で GC が走る）。直す前は 1 回目で毎回落ち、
  直したあとは 10 通りのしきい値で落ちない。他のツールのゴミ（親無しの部品を持った参照の輪）でも、
  `_top_level_widgets` を外すと落ち、入れると落ちない。

### 法線を編集したモデルと dx11Shader の Redo（v0.1.57）

利用者の報告: 顔の法線を編集したキャラで、スクリーン輪郭が顔の内側に出る。
- **原因**: スクリーン輪郭（GS で頂点法線の向きが表→裏に変わる所を輪郭とする）・フレネル（facing）は線用メッシュの
  頂点法線を使う。線用メッシュは元の outMesh を textureDeformer の入力にしているので、元の**ロックした（編集した）
  法線がそのまま流れる**。編集した法線が横を向く所に線が出ていた。なお生成時の `polySoftEdge(ch=False)`（v0.1.14 の
  ハードエッジ対策）は、そのあと元の outMesh を deformer へつなぎ替えるので**効いていなかった**（固い角に線が出なかった）。
- **直し方**: deformer の出口のすぐ後ろに「法線のロック解除（`polyNormalPerVertex -ufn`）→ 全辺なめらか
  （`polySoftEdge` 180°）」を挟む（`_insert_geo_normals`。目印 `GEO_NRM_MARK`、ライン → 出口ノードは `GEO_NRM_LINK`）。
  並びは「元 → textureDeformer → ロック解除 → なめらか → 曲率用カラーセット → 線」。元メッシュの法線は変えない。
  コマンドは部品に掛けるものなので、deformer の出口を受ける一時的な中間シェイプに掛けて作り、つなぎ替えてから消す。
  生成の Undo のまとまりの中で作る（Ctrl+Z 1 回で生成ごと戻る）。前の版の線は `_prepare_scrn_lines` →
  `_ensure_geo_normals`（ツールを開いたとき・1 本ごとに Undo 1 回）。別 ROOT（リファレンス）と静的な線は触らない。
  `_line_src_shape` は今までどおり deformer の入力（元メッシュ）。削除は `_gather` が `_geo_nrm_nodes` を巻き込む。
  固い角の箱にも線が出るようになった（角では線が少し内側を通る。前は線が無かった）。再生の負担は 4 万頂点で +0.04ms/フレーム。
- **dx11Shader の読み込みは Undo に記録しない（`_set_shader_file`）**。`.shader` を setAttr するとエフェクトを読み込み、
  uniform の属性がシェーダ側で作り直される。これを記録すると、Redo でもう一度読み込まれて属性が作り直され、同じまとまりの
  uniform への setAttr の記録が消えた属性を指して **Maya ごと落ちる**（access violation）。最小の再現：dx11Shader を作って
  読み込み → uniform へ setAttr（同じまとまり）→ 別の操作 → Undo 2 回 → Redo。**v0.1.53 から（おそらくもっと前から）**
  スクリーン輪郭・フレネルの生成で起きていた（作る → 何か操作 → Undo 2 回 → Redo）。法線のノードを足すと
  「作る → Undo → Redo」だけでも起きるようになったので見つかった。uniform への**接続だけ**なら落ちない。
  Undo でノードごと消えるので、読み込みを記録しなくても戻し損ねはない。シェーダファイルを変えるときは必ずこれを使う。
- 切り分けの経緯（同じ所で迷わないように）: 挟む位置（入力側/出口側）・作り方（一時シェイプ/createNode/線のシェイプに直接）・
  素通し（nodeState）・まとまりの入れ子・シェーダを先に作る、はどれも関係なかった。surfaceShader に替えると落ちない →
  ツール抜きの最小の手順で dx11Shader の読み込み＋uniform への setAttr に絞った。**まとまりを外して 1 コマンドずつ Undo/Redo
  すると落ちない**ことも手がかり（読み込みの記録とそのあとの setAttr の記録が別々に戻る）。
- 検証: `gui_geonrm57_check.py`（球の法線を前にずらした中心から外向きに固定 → 顔の内側の線の画素 8161→0・外周に線・
  頂点の追従・Undo 1 回・Redo・削除の Undo/Redo・前の版の線の切り替えと Undo・フレネル・箱・4 万頂点の再生時間。
  OG_XOP=1 で「作る → 別の操作 → Undo 2 回 → Redo」、OG_KIND=fres でフレネル）、`gui_dx11redo_check.py`（最小の再現。
  OG_DXVAR=u_set で落ちる・uniform_noundo で落ちない）、`gui_redo57_check.py`（OG_KEEPA=1 で前の版の Redo クラッシュ）。

### 見た目の検証方法

利用者の Maya を commandPort で操作しない（重い操作で落ちたことがある）。専用 MAYA_APP_DIR の GUI Maya 2023 を
**`MAYA_VP2_DEVICE_OVERRIDE=VirtualDeviceDx11`** で背面起動し（無いと OpenGL で起動して dx11Shader が映らない）、
`playblast(offScreen=True)` で撮る。dx11Shader もオーバーライドの出力も写る。物を surfaceShader の 0/1 の色にして、
線あり画像とライン非表示のマスク画像を突き合わせると「線の画素がどの物の上に出たか」を数えられる。

### 互換性の注意

PySide2 系の旧 Maya（2019/2020）は Python 2.7。
`*args` の後ろにキーワード引数を置く呼び出し（例: `cmds.setAttr(attr, *vals, type=...)`）は
Python 2 で構文エラーになるため使わない。引数は明示的に展開する。
