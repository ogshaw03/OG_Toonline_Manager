# HANDOFF — OG Toonline Manager（現状まとめ・実機検証の引き継ぎ）

Maya 用トゥーン輪郭ツール。**輪郭の“重なり/隙間”問題**に長く取り組んでおり、ビューポート(VP2/DirectX11)
依存の機能が多い。本ファイルは「**この問題の全体像**」「**今どの方式が本命でどこまで動くか**」「**現在のコード地図**」
「**残課題と次の一手**」をまとめた引き継ぎ資料。設計のソースコード寄り詳細は `CLAUDE.md` に集約。

> **状態メモ**: 引き継ぎは既に一度行われ、ローカルセッションが実機 Maya 2024 で修正を push 済み（版 0.1.6）。
> 直近で ①スクリーン輪郭の曲率(COLOR0)バインド ②画像空間エッジのかすれ低減 が実機対応された（下記 §3）。

---

## 0. リポジトリ / 環境

- **repo**: `ogshaw03/OG_Toonline_Manager` / **dev ブランチ**: `claude/repository-handoff-review-k54kwk`（全実装）
- **本体**: `OG_Toonline_Manager.py`（約4,700行・単一ファイル）。英語UI版 `OG_Toonline_Manager_EN.py`。
  実験単体版 `OG_Edge_Outline.py`（全ビューポート対象の画像空間エッジ）。別ツール `Follow_Face_Patch.py`/`Transfer_Attr_Tool.py`。
- **配布/更新**: `OG_Toonline_Manager_install.py` を Maya ビューポートにドラッグ（ホットアップデート。CLAUDE.md参照）。
  ローカル反復は `OG_TOONLINE_MANAGER_USE_LOCAL=1`。実機で直したら `__version__` を上げて push → UIの「GitHub から更新」。
- **前提**: Maya 2022+（Py3）／ VP2=**DirectX11** ／ ビューポート「テクスチャ表示 ON(6キー)」。

---

## 1. 中心課題：輪郭の「隙間」と「重なり」（★ここが肝）

このツールの全履歴は、実ジオメトリで MayaToonOutline 相当の輪郭を出すときに起きる **2種類の破綻** との戦い。

### 1-A. 「隙間（浮き）」問題 — ワールド押し出しハルの宿命
- **背面法ハル**（メッシュを法線方向にワールド押し出し→法線反転→バックフェースカリング）は、元メッシュと
  押し出しシェルの間に**3Dの隙間**ができる。**グラジング角**（面を浅く見る角度）でこの隙間が横から見え、
  輪郭が**面から浮く／面を横切る**。チューブ式エッジラインも**継ぎ目に隙間**が出る。
- これを塞ぐため試して**全部ボツにした**方式（コードは後方互換で残置・UI導線なし）:
  - **隠蔽検知ハル**（`_occlusion_factors`, レイ）: 隠れ頂点を内側へ引き寄せ。太さがちらつき不安定。
  - **重なりマスク**（`toggle_overlap_mask`, 2オブジェ・面色の覆い）: 太さ安定と張り付きが両立せず。
  - **隙間埋め**（`toggle_gapfill`, 2オブジェ・facing駆動の背面複製）: 比較的良いが2重線/調整難。
  - **クリース/境界ライン**（`create_crease_lines`）: 折れ目のみ。滑らかな閉曲面のシルエットは出ない。
- **結論**: ワールド押し出し式は原理的に隙間が出る。**ビューポートで隙間を出さない唯一の幾何解は
  「スクリーン空間押し出し」**（画面上で一定px広げるだけ＝元シルエットと同じ深度のまま広がるので
  3D隙間が発生しない）。→ これが現在の本命 **スクリーン輪郭**（§2-A）。

### 1-B. 「重なり（overlapping contour）」問題 — 房が交差する所の線
- 三つ編み等で**房どうしが重なる境界**にも線が欲しい（内側の溝/occluding contour）。
- スクリーン輪郭は深度押し込みの副作用で**重なり部にも線が出る**（＝元メッシュが前後で重なる所の深度段差）。
  これは「欲しい結果」だが、以下の弱点が順に判明し、順に対処してきた:
  1. **重なり線が遠ざかると消える** → 深度押し込みが clip空間NDC一定(`gZBias`)だと遠距離で深度差が圧縮され消える。
     **→ ビュー空間一定の押し込みに変更して解決**（§2-A, `depthBias`）。
  2. **近接で線が消える/太さが距離で変わる**（上記変更の副作用）→ **`clip.w` を触ったのが原因**。
     **→ clip.xy/clip.w は gWVP から不変にし、clip.z だけ差し替える方式で解決**（§2-A の罠）。
  3. **手前のメッシュに重なり線が隠れて細る**（前側の房が後ろの房の線を遮蔽）。ユーザー要望＝
     「**線をメッシュの上のレイヤーに出したい**」。深度尊重のスクリーン輪郭では原理的に不可。
     **→ 後処理の「画像空間エッジ検出」が唯一の解**（§2-B）。
  4. **滑らかな面だとフチがギザギザ**（被覆境界がジオメトリ解像度に縛られる）。高分割か MSAA でしか緩和できず、
     **解像度非依存に綺麗にするには §2-B（画像空間エッジ）が本命**。

> つまり **1-A(隙間)はスクリーン輪郭で解決、1-B(重なり)の最終解は画像空間エッジ検出** という整理。

---

## 2. 現在の主要2方式（本命）

### 2-A. スクリーン輪郭 `create_screen_outline`（ボタン「スクリーン輪郭」/ `SCRN_TAG`）
- dx11Shader の VS で**クリップ空間へ一定px押し出し**（`_SCRN_FX_HLSL`）。隙間なし・`clip.w`補正で均一px太さ。
- 深度を**ビュー空間で一定量奥へ**押し込み、元メッシュにシェル内側を覆わせ外周リング＋重なり線を残す。
- **★VS 実装の鉄則（踏んだ罠）**: `clip.xy`/`clip.w` は `gWVP` から取り**不変**。`clip.z` だけ
  `clip=pos×gWVP` → `vpos=pos×gWV; vpos.z-=depthBias; clipB=vpos×gProj` → `clip.z=(clipB.z/clipB.w)×clip.w`
  で差し替える。**`clip.w` を触ると近接で線が消える/太さが距離で変わる**。`gProj` は深度算出のみ。
- 押し込み量/符号は UI「重なり深度(スクリーン)」(`_scrn_set_depth_bias`, 既定 `_SCRN_ZBIAS`=0.02, 符号反転可)。
- 太さ=`thickness`(px) uniform（`mB.output×SCRN_SCALE`=6.0）。色=`lineColor`。元へ `outMesh`+`parent/scaleConstraint` 追従。
- **曲率対応（頂点カラー COLOR0 駆動）**: `COLOR0.r`＝太さ倍率。色セット `SCRN_CSET`(`toonScrnCurv`) を
  **シェーダ割り当ての前**に作成し、`_update_scrn_curv_weights`(om2 setVertexColors) で R に重みを書く。
  **★実機対応済**: dx11Shader が色セットを自動で COLOR0 に流さない版があるため、
  `_bind_scrn_color_set_to_shader` で **varyingParameters.source を色セット名へ明示バインド**（Maya 2024 で確定）。
  失敗時は警告＋診断 `_dump_dx11_varying('<shd>')` を案内。
- 弱点: 手前オブジェクトに重なり線が隠れる（正しい遮蔽）・滑らか面のギザ（§1-B の 3,4）。

### 2-B. 画像空間エッジ検出 `_edge_enable`（UIチェック「重なり線をメッシュの上に描く（画像空間エッジ・実験）」/ `EDGE_OVR_NAME`）
- **重なり線をメッシュの上に・均一・解像度非依存**で出す本命（§1-B の 3,4 の解）。`MRenderOverride`(om2 OpenMayaRender)。
  **レンダーAPIは関数内で遅延import**（未使用なら import されない＝本体は Py2 import 可・API欠如でも壊れない）。
- **★オブジェクト単位維持（絶対条件）**: 2パス。①全シーンを色 `tColor` へ ②`objectSetOverride` で**対象メッシュだけ**を
  深度 `tDepth` へ（専用スクラッチ色+ALLクリア）→ フルスクリーン quad が `tDepth` の深度ラプラシアンで段差を検出し
  `tColor` に合成。**線は対象メッシュにだけ乗る**。対象は ON 時の選択メッシュ(`_edge_set_targets`)。
- **★実機対応済（かすれ低減）**: (a) 深度を**線形化**(`linearizeDepth`, `gNear`/`gFar`)、near/far は
  **アクティブカメラから取得**(`_edge_active_camera_near_far`)。(b) **相対しきい値**で段差を安定検出。(c) **HiDPI**対応で
  ターゲットを実ピクセルで作成。(d) 深度可視化デバッグ（`debug` uniform: 対象=濃淡/背景=白）。
- 弱点: 画面合成なので**手前の別オブジェクトより線が上に出る**ことがある（前後関係未考慮）。厳密遮蔽は対象深度 vs
  シーン深度比較を足す余地。しきい値/太さ/線色は UI スピン＋共通カラー追従。closeEvent で `_edge_disable`。

---

## 3. 現在のステータス（動く / 要確認）

| 項目 | 状態 |
|---|---|
| スクリーン輪郭 太さ（一定px・距離/近接で安定） | ✅ 実機OK報告 |
| スクリーン輪郭 重なり線（遠/近で消えない） | ✅ ビュー空間深度で対処済（実機で最終確認推奨） |
| スクリーン輪郭 曲率（COLOR0 バインド） | 🟡 実機 Maya 2024 で明示バインド実装済 → **各バージョン/メッシュで要確認**（失敗時 `_dump_dx11_varying`）|
| 画像空間エッジ かすれ | 🟡 線形化+near/far+HiDPI+相対しきい値で低減 → **残りのかすれ/しきい値感度を実機で詰める** |
| 滑らか面のギザ（スクリーン輪郭） | ⚠️ 構造的。MSAA緩和 or §2-B へ寄せる |
| 手前オブジェクトに重なり線が隠れる | ⚠️ スクリーン輪郭では原理的。§2-B で解決する設計 |

---

## 4. コード地図（現在のUI・主要関数）

- **上部ボタン(5)**: 生成(`create_outlines`) / スクリーン輪郭(`create_screen_outline`) /
  選択エッジにライン(`create_edge_line`) / 新規グループ(`new_group`) / 再取得。
- **オプション（チェック）**: ハル隠蔽寄せ(検証中) / **スクリーン輪郭に溝ライン(フレネル)も生成**(`chk_scrn_fresnel`) /
  重なり深度(スクリーン)スピン(`spn_scrn_bias`) / **重なり線をメッシュの上に描く(画像空間エッジ)**(`chk_edge`)+しきい値/太さスピン。
- **輪郭方式**: 背面法ハル(`create_outlines`/textureDeformer法線オフセット, `_is_hull_line`) ／ エッジチューブ(`create_edge_line`,
  `EDGE_TAG`) ／ フレネル(`create_fresnel_outline`, `FRES_TAG`, 一定px幅) ／ スクリーン輪郭(§2-A) ／ 画像空間エッジ(§2-B)。
- **太さ連鎖**: `_ensure_thickness_chain`（line×group×global の multDoubleLinear 2段）。`_thick_target` が方式別に
  offset/threshold/thickness/maxWidthPx へ出し分け。
- **曲率**: `_compute_curvature`(om2, 近傍平均差を法線投影+スムージング)。重み=`_line_weights`(curvature/cap/cmin/taper/profile)。
  hull/edge は textureDeformer weightList、SCRN は頂点カラー(`_update_scrn_curv_weights`)。scriptJob は `_update_line_weights` で分岐。
- **コントローラー階層**: 全体>グループ>ライン(素transform, `message→toonCtrl`)。太さ/曲率/色/キーをUIからsetAttr。
- **休眠（コード残置・UI無し）**: `toggle_gapfill`/`_create_gapfill`、`toggle_overlap_mask`、`create_crease_lines`、
  隠蔽検知(`_occlusion_factors`)、各 `*_TAG`/`*_LINK` 定数。消さずに後方互換維持。

---

## 5. 次の一手（推奨）

1. **本命は §2-B（画像空間エッジ）**。滑らか面でもポリ依存せず重なり線を上に綺麗に出せるのはこれだけ。実機で
   残りのかすれ/しきい値感度/厳密遮蔽を詰めるのが最優先。これが満たせれば §1-B の 3,4 が解決し、スクリーン輪郭の
   弱点を補える。
2. スクリーン輪郭の曲率は実機の各 Maya バージョンで COLOR0 バインド成否を確認（`_dump_dx11_varying`）。
3. 滑らか面ギザはまず MSAA(Viewport2.0 Anti-aliasing)で実用域を探る。根本解は 1。

---

## 6. 運用・git 注意

- **dev ブランチにのみ push**。`main` 直push/force push/ブランチ削除はユーザー確認必須（CLAUDE.md ブランチ運用）。
- 単一ファイル。`.py` 分割時は install.py を `_REMOTE_FILES` 方式へ（CLAUDE.md §1-10）。本体は f-string 禁止(.format)。
- 実機修正後は `__version__` を上げて push。**CLAUDE.md / 本HANDOFF.md も実態に合わせて更新**（今回のように
  コードだけ進んでドキュメントが遅れると次の引き継ぎで混乱する）。
