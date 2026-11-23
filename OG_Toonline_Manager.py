# -*- coding: utf-8 -*-
"""
OG Toonline Manager
===================
dx11Shader の MayaToonOutline.fx 相当の輪郭線を、Maya標準機能（実ジオメトリ）だけで再現。
inverted hull 方式（押し出し → 法線反転 → バックフェースカリング）。
元メッシュの変形に自動追従し、ライン単位で太さ・色・表示／非表示を管理できる。

主な機能:
    - 選択メッシュにアウトライン（ライン）を生成（生成ボタン／各グループ行の +追加）
    - ライン／グループをツリーで一覧表示（グループ行はバー表示）
    - ライン単位の太さ調整（選択したラインだけに適用）
    - カラー: 基本は共通ブラック。ラインごとに個別カラーも指定可
    - グループ単位／ライン単位の表示・非表示
    - グループの新規作成・ラインの移動・削除・再取得

使い方:
    Script Editor に貼り付けて実行、もしくは
        import OG_Toonline_Manager
        OG_Toonline_Manager.show()

使い方の概要は README.md を参照。
"""
import os
import math
import maya.cmds as cmds
import maya.OpenMayaUI as omui
import maya.api.OpenMaya as om2

# ---- PySide6 / PySide2 両対応 ----
try:
    from PySide6 import QtWidgets, QtCore, QtGui
    from shiboken6 import wrapInstance
    try:
        from shiboken6 import isValid as _shib_is_valid
    except ImportError:
        _shib_is_valid = None
except ImportError:
    from PySide2 import QtWidgets, QtCore, QtGui
    from shiboken2 import wrapInstance
    try:
        from shiboken2 import isValid as _shib_is_valid
    except ImportError:
        _shib_is_valid = None


def _qt_valid(obj):
    try:
        return obj is not None and (_shib_is_valid is None or _shib_is_valid(obj))
    except Exception:
        return False


def _find_tool_windows(names):
    """このツールのウィンドウ（objectName が names のもの）を集める。
    v0.1.54: 集めてから閉じる（閉じる処理の途中で描画が回り、一覧の後ろのウィンドウが破棄されたあとに
    触ると Maya ごと落ちることがあった）。"""
    out = []
    for w in QtWidgets.QApplication.topLevelWidgets():
        try:
            if _qt_valid(w) and w.objectName() in names:
                out.append(w)
        except Exception:
            pass
    return out


def _close_and_delete(widgets):
    """閉じて、その場で破棄する。v0.1.54: deleteLater だけだと、あとで新しいウィンドウを作っている最中
    （ツリー更新の描画などでイベントが回ったとき）に古いウィンドウの破棄が走り、Maya ごと落ちることがあった。
    破棄はこのツールのウィンドウだけに限る（sendPostedEvents の対象をそのウィンドウにする）。"""
    for w in widgets:
        try:
            if _qt_valid(w):
                w.close()
            if _qt_valid(w):
                w.deleteLater()
                QtCore.QCoreApplication.sendPostedEvents(w, QtCore.QEvent.DeferredDelete)
        except Exception:
            pass

SHADER    = "toonOutline_SS"        # 共通（ブラック）サーフェスシェーダ
SG        = "toonOutline_SG"        # 共通シェーディンググループ
ROOT      = "toonOutlines_grp"      # 全ライン／グループの親
TAG       = "isToonOutline"         # ライン識別タグ
GROUP_TAG = "isToonOutlineGroup"    # グループ識別タグ
DEFAULT_GROUP = "Outline_Group1"
HANDLE_HOLDER = "toonOutline_handles"   # textureDeformerHandle 退避用の非表示ホルダー
CTRL_HOLDER   = "toonOutline_ctrls"     # 旧: ラインコントローラー格納グループ（互換掃除用）
LINE_HOLDER   = "Outline_grp"           # ライングループの格納グループ（ROOT 直下）
COL_PREFIX = "toonOutlineCol_"      # ライン個別カラーシェーダの接頭辞
THICK_TYPE = "textureDeformer"      # 太さ駆動ノードの型
THICK_ATTR = "offset"               # 現在法線方向への一定オフセット（太さ）

# ラインコントローラー（独立ノード）のアニメ用アトリビュート（全て英語）
CTRL_THICK  = "thickness"
CTRL_CURV   = "curvature"
CTRL_CAP    = "curvatureCap"
CTRL_CMIN   = "curvatureMin"          # 曲率下限（曲率が小さい所＝平らな所の太さ倍率の下限）
DEFAULT_THICK = 0.5                   # 新規ライン生成時の初期太さ
DEFAULT_EDGE_THICK = 0.5              # 新規エッジライン生成時の初期太さ(UI表示値)
EDGE_THICK_SCALE = 0.2               # エッジは UI 太さ×この係数を offset に流す（見た目を細く）
EDGE_BASE_SCALE = 0.2                # エッジ円プロファイル半径 = UI 太さ×この係数（太さに比例して細る）
EDGE_BASE_RADIUS = 0.01              # 生成直後の初期半径（直後に太さチェーンで駆動される）
EDGE_VIS_EPS = 1e-4                   # 総太さがこれ以下ならエッジチューブを非表示にする閾値
DEFAULT_CURV  = 0.0                   # 〃 初期曲率起伏
DEFAULT_CAP   = 3.0                   # 〃 初期曲率上限
DEFAULT_CMIN  = 1.0                   # 〃 初期曲率下限（1.0=平らな所を細くしない）
CTRL_TAPER  = "endTaper"             # 末端細り（0=なし / 1=端をほぼ0に）
MIN_WEIGHT  = 0.05                    # 頂点ウェイトの下限（チューブが点に潰れる/反転するのを防ぐ）
OCC_HIDDEN_WEIGHT = -1.5             # 隠蔽検知ハル: 隠れた頂点の重み（負＝元メッシュ内側へ深く寄せて隠す）
OCC_SMOOTH_ITERS  = 2               # 隠蔽係数の近傍スムージング回数（可視/隠蔽境界のジャギ軽減）
OCC_KEEP_DILATE   = 0               # 可視リムを内側へ太らせるリング数（>0は太さ安定するが重なり部が浮く）
OCC_HIDE_CEIL     = -0.1            # 隠す頂点の上限係数（必ず表面の裏に入れて張り付かせる・スムージング後にクランプ）
CTRL_PROFILE = "thicknessProfile"    # 長手方向の太さプロファイル（"x:y,x:y,..." 文字列）
CTRL_SUFFIX = "_ctrl"                # コントローラー名 = <line>_ctrl
CTRL_LINK   = "toonCtrl"            # line 側の message 属性（→ controller）
GMULT       = "thicknessMult"        # グループ/全体コントローラーの太さ倍率アトリビュート
FRES_SENS_ATTR = "fresSens"          # 全体コントローラーのフレネル感度アトリビュート（全フレネル線の maxWidthPx 倍率）
DIST_ATTEN_STRENGTH = "distAtten"    # v0.1.32: 全体コントローラーの遠景減衰強度アトリビュート（0=無効, 1=最大）
CTRL_DISTATTEN = "camDistAtten"      # v0.1.32: 各ラインの距離減衰倍率（0..1, 1=full）
DIST_ATTEN_REF = 100.0               # 減衰の基準距離（world units）
EDGE_TAG    = "isToonEdgeLine"        # エッジ由来チューブラインの識別タグ
CREASE_TAG  = "isToonCreaseLine"      # v0.1.24: クリース自動生成ラインの識別タグ（EDGE_TAG との並行指標）
CREASES_GROUP_NAME = "__Creases__"    # v0.1.24: クリース線をまとめる専用サブグループ名
MATBOUND_TAG = "isToonMatBoundLine"   # v0.1.38: マテリアル境界ライン識別タグ
MATBOUND_GROUP_NAME = "__MatBounds__" # v0.1.38: マテリアル境界ラインをまとめる専用サブグループ名
CREASES_GROUP_DISPLAY = "クリース"    # v0.1.26: ツリー表示名（ノード名は内部で __Creases__ のまま）
MATBOUND_GROUP_DISPLAY = "マテリアル境界"  # v0.1.38: ツリー表示名
EDGES_GROUP_NAME   = "__Edges__"     # v0.1.31: 手動エッジ線をまとめる専用サブグループ名
EDGES_GROUP_DISPLAY = "エッジ"        # v0.1.31: ツリー表示名
MESH_GROUP_SUFFIX  = "_outlines"     # v0.1.31: オブジェクトごとのトップグループ接尾辞

# v0.1.26: ライン単体表示モード（メッシュを useBackground で holdout）
HOLDOUT_SHADER_NAME = "_toonHoldoutShader"
HOLDOUT_SG_NAME = "_toonHoldoutSG"
MESH_PREV_SG_ATTR = "_toonPrevSG"    # メッシュに保存する元の SG 名
PROFILE_LINK = "toonProfile"         # エッジラインの円プロファイル(makeNurbCircle)への message
FRES_TAG    = "isToonFresnelLine"    # フレネル輪郭（カメラ依存・VP2/バッチ対応）の識別タグ
FRES_LINK   = "toonFresnelCond"      # フレネルの condition ノードへの message（太さ＝しきい値）
FRES_SCALE  = 0.3                    # UI 太さ → フレネルしきい値(threshold=facing カット)への係数。
                                     # 太さ0.5→threshold0.15(細い縁)、太さ2.0→0.6(θ>53°の広い帯=溝を拾う)。
                                     # 「太さ」スライダーが実質フレネルの角度調整（大きいほど寝た面を広く拾う）
FRES_ZOFFSET = 0.001                 # 極小の法線オフセット（手前に出して見えるように・二重線最小）
FRES_THRESH = "threshold"            # dx11Shader 上のしきい値 uniform 名（太さ駆動先）
FRES_MAXW   = "maxWidthPx"           # フレネル帯の画面上ピクセル幅の上限 uniform 名（太さ×SCRN_SCALE で駆動）
FRES_COLOR  = "lineColor"            # dx11Shader 上の線色 uniform 名（フレネル/スクリーン共通）
SCRN_TAG    = "isToonScreenLine"     # スクリーン空間押し出し輪郭（隙間なし・均一太さ）の識別タグ
SCRN_THICK  = "thickness"            # dx11Shader 上の太さ(ピクセル) uniform 名（太さ駆動先）
SCRN_SCALE  = 6.0                    # UI 太さ → スクリーン押し出しピクセル数への係数
SCRN_CSET   = "toonScrnCurv"         # スクリーン輪郭の曲率倍率を格納する頂点カラーセット名
SCRN_DEPTH_BIAS = "depthBias"        # スクリーン輪郭の重なり深度押し込み uniform 名（ビュー空間）
_SCRN_ZBIAS = 0.0                    # v0.1.30: 既定 0（ユーザー要望・重なり線が必要なときだけ上げる）
SCRN_BRUSH_ROUGH = "brushRoughness"  # v0.1.14 ペンシル質感: 縁のカサつき(ランダム discard)量 (0-1)
SCRN_BRUSH_WAVE  = "brushWaviness"   # v0.1.14 ペンシル質感: 太さの長手ゆらぎ量 (0-1)
SCRN_BRUSH_SCALE = "brushNoiseScale" # v0.1.14 ペンシル質感: ノイズ周期（画面px換算・大=細かい）
_SCRN_BRUSH_ROUGH_DEF = 0.0          # 既定値（0=クリーン、OFF）
_SCRN_BRUSH_WAVE_DEF  = 0.0
_SCRN_BRUSH_SCALE_DEF = 20.0
SCRN_HIDDEN_SHOW = "showHidden"      # v0.1.38 隠れ線（裏側シルエット）を破線表示する uniform（int 0/1, DX11のみ）
SCRN_HIDDEN_DASH = "hiddenDashLen"   # v0.1.38 隠れ線の 1 周期ピクセル長
SCRN_HIDDEN_FILL = "hiddenDashFill"  # v0.1.38 隠れ線の塗り比率 (0-1)
SCRN_HIDDEN_ALPHA = "hiddenAlpha"    # v0.1.38 隠れ線の alpha（透明度）
_SCRN_HIDDEN_DEF = 0                  # 既定 OFF（互換性）
_SCRN_HIDDEN_DASH_DEF = 10.0
_SCRN_HIDDEN_FILL_DEF = 0.55
_SCRN_HIDDEN_ALPHA_DEF = 0.6
SCRN_INTERSECT_ON = "toonIntersectOn"  # v0.1.41 SCRN ライン個別の「交差線に含める」フラグ(bool, 既定 True)
# v0.1.46: ライン詳細設定。見え方の属性はライン transform に持たせ、Render Setup の
# 絶対オーバーライドでレイヤーごとに上書きする（マスター=ビューポート、各レイヤー=書き出し）。
SCRN_SHOW_VIS = "showVisible"          # SCRN シェーダ: 表側の線を描くか（int 0/1）
LN_SHOW_VIS   = "toonShowVisible"      # 表側の線を描く → shd.showVisible へ接続
LN_COLOR      = "toonLineColor"        # 線の色 → shd.lineColor へ接続
LN_HID_MODE   = "toonHiddenMode"       # 隠れ線 0=出さない 1=破線 2=実線
LN_HID_SAME   = "toonHiddenColorSame"  # 隠れ線の色を線の色と同じにする
LN_HID_COLOR  = "toonHiddenColor"      # 隠れ線の色（LN_HID_SAME=0 のとき）
LN_HID_ALPHA  = "toonHiddenAlpha"      # 隠れ線の濃さ 0-1
LN_HID_DASH   = "toonHiddenDashLen"    # 破線 1 周期の長さ（画面 px）
LN_HID_FILL   = "toonHiddenDashFill"   # 破線の塗り割合 0-1
LN_OCCLUDERS  = "toonHiddenOccluders"  # 隠れ線を透かす物（message multi ← 物.message）。全レイヤー共通
# v0.1.53: 色トレス（線の色を下の面のマテリアルの色から作る）。スクリーン輪郭とエッジ線・クリース線
LN_TRACE      = "toonTraceAmount"      # 0=線の色のまま / 1=面の色から作った色 → shd.traceAmount
LN_TRACE_DARK = "toonTraceDark"        # 面の色の明るさ 0-1（0.45 = 半分弱に暗くする）→ shd.traceDark
LN_TRACE_RAW  = "toonTraceRaw"         # エッジ線・クリース線: 隣の面のマテリアルの色（Python が入れる）
SCRN_TRACE    = "traceAmount"          # SCRN シェーダの uniform
SCRN_TRACE_DARK = "traceDark"
TRACE_DARK_DEF = 0.45
ROOT_HIDDEN_ON = "toonHiddenOn"        # シーン全体で隠れ線を描くか（ROOT の属性、既定 ON）
HIDDEN_DEPTH_TOL = 0.002               # 隠れ線の前後判定に使う相対深度の許容差
# v0.1.49: ブラシ（SCRN シェーダの uniform 名と、ラインごと・レイヤーごとの属性名）
SCRN_BRUSH_TAPER  = "brushTaper"       # 入り抜き（輪郭の端ほど細く）0-1
SCRN_BRUSH_DRY    = "brushDry"         # かすれ 0-1
SCRN_BRUSH_WOBBLE = "brushWobble"      # よれ（線を外側へ滑らかにずらす）0-1
SCRN_BRUSH_DOUBLE = "brushDouble"      # 二重線 0-1
LN_BR_ROUGH  = "toonBrushRough"        # → brushRoughness（カサつき）
LN_BR_WAVE   = "toonBrushWave"         # → brushWaviness（太さのゆらぎ）
LN_BR_SCALE  = "toonBrushScale"        # → brushNoiseScale（ゆらぎの細かさ）
LN_BR_TAPER  = "toonBrushTaper"
LN_BR_DRY    = "toonBrushDry"
LN_BR_WOBBLE = "toonBrushWobble"
LN_BR_DOUBLE = "toonBrushDouble"
MASK_TAG    = "isToonOverlapMask"    # 重なり隠しマスク（元メッシュ複製を面色で膨らませた覆い）の識別タグ
MASK_LINK   = "toonMask"             # line → mask への message（重なりマスクの関連付け）
MASK_INFLATE_FRAC = 1.0              # マスク膨らみ量 = ライン太さ × 係数。大きいほど交差を覆える
                                     # （太さは上乗せ補正で一定。係数↑＝覆う力↑だがモデルが膨らむ）
MASK_HOLDER = "ToonMask_grp"         # 重なりマスクの格納グループ（ROOT 直下・別オブジェクトとして可視）
GAPFILL_TAG  = "isToonGapFill"       # 隙間埋めオブジェクト（facing で寝た面をベースの位置へ押し出す）の識別タグ
GAPFILL_LINK = "toonGapFill"         # line(A) → gapfill(B) への message
GAPFILL_HOLDER = "ToonGapFill_grp"   # 隙間埋めオブジェクトの格納グループ（ROOT 直下）
FACING_THRESH = 0.2                  # facing(=|N·視線|) がこれ未満で押し出し開始（シルエット帯の広さ）
                                     # 小さいほど帯が狭く＝フチでだけ接線方向に膨らみ本体上の黒面を防ぐ
GAPFILL_SMOOTH_ITERS = 1             # 隙間埋めウェイトの近傍スムージング回数
GAPFILL_TUCK = -2.0                  # シルエット以外と膨らみ壁の本体側を本体の裏へ深く沈める weight 下限
                                     # （負で深いほど二重線の内側の縁が本体に隠れる。裏面表示時の二重線対策）
CREASE_ANGLE = 30.0                  # クリース判定の二面角しきい値（度）。これ超で折れ目エッジとみなす
GLOBAL_CTRL = "toonOutline_globalCtrl"  # 全体コントローラー（コントローラー階層の親）
COL_GLOBAL  = (0.4, 0.8, 1.0)        # 全体=水色
COL_GROUP   = (0.55, 0.9, 0.2)       # グループ=黄緑
COL_LINE    = (1.0, 0.8, 0.0)        # ライン=黄

# v0.1.20: 配色をダークグレー背景 + 暖色イエローのプライマリボタンに変更。
# ピンクより落ち着いた印象で、全体のダーク UI に馴染む色味。
_WINDOW_BG     = "#2d2d2d"   # メインウィンドウ背景（ダークグレー）
_WINDOW_FG     = "#e0e0e0"   # 文字色

# プライマリ（主要アクション）ボタン QSS。Maya のバージョン/テーマ差でも
# 「選択メッシュに輪郭を生成 / 選択をすべてリセット / バージョン選択」が
# 同じ暖色イエローで統一表示されるよう明示指定。
_PRIMARY_BTN_CSS = (
    "QPushButton {"
    " background-color: #c9a848;"      # 暖色イエロー（dark gray に馴染む muted amber）
    " color: #1a1a1a;"                 # コントラスト確保用の濃色文字
    " border: 1px solid #5a4a20;"
    " padding: 4px 6px;"
    " border-radius: 3px;"
    "}"
    "QPushButton:hover    { background-color: #e0bf65; color:#111; }"
    "QPushButton:pressed  { background-color: #a88a35; color:#000; }"
    "QPushButton:disabled { background-color: #6a5e40; color:#9a9a9a; }"
)

# ダークグレー背景を Dialog に当てるための QSS（objectName=WINDOW_OBJ に一致）
# v0.1.22: QSlider の視認性を確保（ダーク背景で groove/handle が消えていたため）。
_DIALOG_BG_CSS = (
    "QDialog#OG_Toonline_ManagerWin { background-color: " + _WINDOW_BG + "; }"
    "QDialog#OG_Toonline_ManagerWin QLabel { color: " + _WINDOW_FG + "; background: transparent; }"
    # QSlider 水平: groove はしっかりした帯、handle は暖色イエローの丸ノブ
    "QDialog#OG_Toonline_ManagerWin QSlider::groove:horizontal {"
    " border: 1px solid #1a1a1a; height: 4px;"
    " background: #4a4a4a; border-radius: 2px; margin: 0;"
    "}"
    "QDialog#OG_Toonline_ManagerWin QSlider::sub-page:horizontal {"
    " background: #c9a848; border-radius: 2px;"
    "}"
    "QDialog#OG_Toonline_ManagerWin QSlider::add-page:horizontal {"
    " background: #3a3a3a; border-radius: 2px;"
    "}"
    "QDialog#OG_Toonline_ManagerWin QSlider::handle:horizontal {"
    " background: #e0bf65; border: 1px solid #333; width: 12px; height: 12px;"
    " margin: -5px 0; border-radius: 6px;"
    "}"
    "QDialog#OG_Toonline_ManagerWin QSlider::handle:horizontal:hover {"
    " background: #ffd978;"
    "}"
    "QDialog#OG_Toonline_ManagerWin QSlider::handle:horizontal:pressed {"
    " background: #a88a35;"
    "}"
    # v0.1.24: 「生成オプション」「全体設定」の QGroupBox 枠。
    "QDialog#OG_Toonline_ManagerWin QGroupBox {"
    " border: 1px solid #4a4a4a; border-radius: 4px; margin-top: 10px;"
    " padding-top: 6px; background: #333333;"
    "}"
    "QDialog#OG_Toonline_ManagerWin QGroupBox::title {"
    " subcontrol-origin: margin; subcontrol-position: top left;"
    " padding: 0 6px; color: #c9a848; font-weight: bold;"
    " left: 8px; top: -2px; background: " + _WINDOW_BG + ";"
    "}"
    # v0.1.25: クリース系の設定をネスト表示するためのフレーム（左黄枠）。
    "QFrame#OG_Toonline_CreaseFrame {"
    " border-left: 3px solid #c9a848; border-radius: 0;"
    " margin-left: 4px; padding-left: 4px;"
    " background: transparent;"
    "}"
)

# 左上タイトルラベル QSS + ヘッダーバージョン + ⋮メニューボタン
_TITLE_CSS = (
    "QLabel#OG_Toonline_TitleLabel {"
    " color: #c9a848;"                  # 暖色イエロー（プライマリ色）
    " font-size: 14pt;"
    " font-weight: bold;"
    " padding: 4px 4px 2px 2px;"
    " background: transparent;"
    "}"
    "QLabel#OG_Toonline_HeaderVer {"
    " color: #888;"                     # 小さいグレーの version 表示
    " font-size: 8pt;"
    " padding-bottom: 6px;"
    " background: transparent;"
    "}"
    "QToolButton#OG_Toonline_MenuBtn {"
    " background: transparent;"
    " color: #c0c0c0;"
    " border: none;"
    " font-size: 16pt;"
    " padding: 0 4px;"
    "}"
    "QToolButton#OG_Toonline_MenuBtn:hover { color: #ffffff; background: #3a3a3a; border-radius: 3px; }"
    "QToolButton#OG_Toonline_MenuBtn::menu-indicator { image: none; width:0px; }"
)
WINDOW_OBJ  = "OG_Toonline_ManagerWin"  # ウィンドウ識別名（重複起動の防止に使用）

# ---- バージョン & GitHub ホットアップデート設定 ----
# install.py が __version__ を before/after ダイアログとバージョン表示に使う。
# 値を上げてから push すると「GitHub から更新」で previous → current が変わる。
__version__ = "0.1.54"

# 「GitHub から更新」の取得元（開発ブランチ。安定運用に移す際は main へ）
_GITHUB_OWNER  = "ogshaw03"
_GITHUB_REPO   = "OG_Toonline_Manager"
_GITHUB_BRANCH = "claude/repository-handoff-review-k54kwk"
_PACKAGE       = "OG_Toonline_Manager"   # このファイル自身のモジュール名（installer の _MODULE と一致）
_INSTALLER_FILE = "OG_Toonline_Manager_install.py"   # 更新時に再取得するインストーラのファイル名
_GITHUB_API      = "https://api.github.com/repos/{0}/{1}".format(_GITHUB_OWNER, _GITHUB_REPO)
_GITHUB_RAW_BASE = "https://raw.githubusercontent.com/{0}/{1}".format(_GITHUB_OWNER, _GITHUB_REPO)

# v0.1.52: キャッシュ・ジョブはノードの UUID で引く（名前で引くと、リネームや短い名前/フルパスの
# 違いで同じラインが別物扱いになり、曲率ジョブが二重に張られたり古いキャッシュを使ったりした）。
_CURV_CACHE = {}   # line UUID -> 正規化曲率リスト
_CURV_JOBS  = {}   # line UUID -> ([scriptJob id, ...], コントローラーの MObjectHandle)
_CURV_SIG   = {}   # line UUID -> 最後に重みを書いたときの曲率まわりの値（同じなら再計算しない）
_TAPER_CACHE = {}  # line UUID -> 各頂点の長手方向パラメータ t(0..1)（エッジライン用）
_OCC_ENABLED = False  # 隠蔽検知ハル（カメラ依存）の有効フラグ（UIチェックで切替・モジュール共有）


def _maya_main():
    return wrapInstance(int(omui.MQtUtil.mainWindow()), QtWidgets.QWidget)


def _short(name):
    return name.split("|")[-1] if name else name


def _base(name):
    """新しく作るノードの名前の元にする名前。DAG パスを外し、ネームスペースは '_' でつなぐ
    （v0.1.52: リファレンスしたメッシュ 'chr:head' から作る線を 'chr:' の中に置かないため。
    'chr_head_outline' のように元の名前空間は名前に残して区別できるようにする）。"""
    s = _short(name)
    if not s:
        return s
    return s.lstrip(":").replace(":", "_")


def _shapes_out_of_ns(tr):
    """複製した transform の shape は元の名前（'chr:headShape1' など）のまま名前空間に残るので、
    名前空間の外の名前に付け直す（v0.1.52）。"""
    for s in cmds.listRelatives(tr, shapes=True, f=True) or []:
        if ":" in _short(s):
            try:
                cmds.rename(s, _base(tr) + "Shape")
            except Exception:
                pass


def _ukey(node):
    """キャッシュ・ジョブのキー（ノードの UUID＋名前空間。取れなければフルパス）。
    同じファイルを 2 回リファレンスすると両方のノードが同じ UUID になるので、名前空間も付けて区別する
    （名前の変更では変わらない）。"""
    full = (cmds.ls(node, long=True) or [node])[0]
    try:
        u = cmds.ls(node, uuid=True) or []
        if u:
            ns = _short(full).rpartition(":")[0]
            return u[0] + "@" + ns if ns else u[0]
    except Exception:
        pass
    return full


# _ensure_thickness_chain がラインのコントローラー名の後ろに付けて作るノード（削除時に一緒に消す）
_CTRL_NODE_SUFFIXES = ("_thkA", "_thkB", "_thkMaskBoost", "_edgeDist", "_thkScale", "_radScale",
                       "_fresScale", "_fresMaxW", "_fresMaxWSens", "_scrnScale", "_scrnDist",
                       "_visCond")


def _rename_ctrl_nodes(old_base, new_base):
    """コントローラー名の後ろに付けて作ったノード（_thkA など）を新しいコントローラー名に合わせる。"""
    if not old_base or old_base == new_base:
        return
    for s in _CTRL_NODE_SUFFIXES:
        if cmds.objExists(old_base + s) and not cmds.objExists(new_base + s):
            try:
                cmds.rename(old_base + s, new_base + s)
            except Exception:
                pass


def _clear_line_caches():
    """シーンが変わったら曲率・長手方向のキャッシュを捨てる（同じ UUID の物を
    リファレンスし直したときなどに古い値を使わないため）。"""
    _CURV_CACHE.clear()
    _TAPER_CACHE.clear()
    _CURV_SIG.clear()
    try:
        _TRACE_CACHE.clear()
    except NameError:
        pass


def _ensure_shader(color):
    """全アウトライン共有のフラットマテリアル（基本のブラック）を確保。"""
    if not cmds.objExists(SHADER):
        cmds.shadingNode("surfaceShader", asShader=True, name=SHADER)
        cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=SG)
        cmds.connectAttr(SHADER + ".outColor", SG + ".surfaceShader", f=True)
        cmds.setAttr(SHADER + ".outColor", color[0], color[1], color[2], type="double3")
    return SHADER, SG


def _vp2_is_opengl():
    """現在の VP2 バックエンドが OpenGL（旧 OGL か Core Profile）かを返す。
    判定不能なら False（＝DirectX11 想定）。SCRN/Fresnel/EdgeOverride の
    HLSL(.fx) vs OGSFX(.ogsfx) 書き分けに使う。"""
    try:
        import maya.api.OpenMayaRender as omr
        api = omr.MRenderer.drawAPI()
        return api == omr.MRenderer.kOpenGL or api == omr.MRenderer.kOpenGLCoreProfile
    except Exception:
        return False


def _hw_shader_plugin():
    """VP2 バックエンドに合う自前シェーダ用プラグイン名とノード型を返す。
    OpenGL → ('glslShader', 'GLSLShader') / DirectX11 → ('dx11Shader', 'dx11Shader')。"""
    if _vp2_is_opengl():
        return ("glslShader", "GLSLShader")
    return ("dx11Shader", "dx11Shader")


_FRES_FX_HLSL = """// OG Toonline Manager - Fresnel contour (Maya dx11Shader / HLSL)
// ビュー空間法線の z 成分で facing を算出（正面=1 / シルエット=0）。
// しきい値(threshold)未満の縁だけ線色で不透明、それ以外は透明(discard)。
// ※ ビューポートは「テクスチャ表示 ON（ホットキー 6）」で表示されます。
float4x4 gWVP : WorldViewProjection;
float4x4 gWV  : WorldView;

float threshold <
    string UIName = "Threshold";
    float UIMin = 0.0;
    float UIMax = 1.0;
    float UIStep = 0.001;
> = 0.15;

float maxWidthPx <
    string UIName = "Width (px)";
    float UIMin = 0.0;
    float UIMax = 64.0;
    float UIStep = 0.1;
> = 3.0;   // 輪郭線の画面px幅。太さチェーン(UI太さ×SCRN_SCALE)から接続される。既定は約3px。

float3 lineColor <
    string UIName = "Line Color";
    string UIWidget = "Color";
> = {0.0f, 0.0f, 0.0f};

struct APPDATA { float3 Position : POSITION; float3 Normal : NORMAL; };
struct V2P { float4 HPos : SV_Position; float3 VN : TEXCOORD0; };

V2P VShader(APPDATA IN)
{
    V2P OUT;
    OUT.HPos = mul(float4(IN.Position, 1.0f), gWVP);
    OUT.VN   = mul(IN.Normal, (float3x3)gWV);    // ビュー空間法線
    return OUT;
}

float4 PShader(V2P IN) : SV_Target
{
    float3 N = normalize(IN.VN);
    float facing = abs(N.z);                              // 1=正面, 0=シルエット/輪郭
    // 一定「画面ピクセル幅」の輪郭線。facing の帯(threshold 幅)は面の曲がり方で画面幅が
    // 変わり“ムラ”になるので使わない。代わりに シルエット(facing=0)からの画面px距離
    // sd = facing / fwidth(facing) を出し、sd < maxWidthPx の一定幅だけ線にする。
    // 平らな面は fwidth≈0 → sd 巨大 → 線が出ない（ムラ源のワイドな帯そのものが消える）。
    float w = max(fwidth(facing), 1e-6f);
    float sd = facing / w;                                // ≒ シルエット/輪郭からのピクセル距離
    float a = 1.0f - smoothstep(maxWidthPx - 1.0f, maxWidthPx + 1.0f, sd);
    if (a <= 0.002f) discard;
    return float4(lineColor, a);
}

technique11 Main < int isTransparent = 1; >
{
    pass p0
    {
        SetVertexShader(CompileShader(vs_5_0, VShader()));
        SetPixelShader(CompileShader(ps_5_0, PShader()));
    }
}
"""


_FRES_FX_OGSFX = """#version 410
// OG Toonline Manager - Fresnel contour (Maya glslShader / OGSFX / GLSL for OpenGL VP2)
// HLSL 版 (_FRES_FX_HLSL) と同じ facing=|VN.z|＋fwidth で一定px幅の輪郭。v0.1.37 で OpenGL 対応追加。

uniform mat4 gWVP : WorldViewProjection;
uniform mat4 gWV  : WorldView;

uniform float threshold
<
    string UIName = "Threshold";
> = 0.15;

uniform float maxWidthPx
<
    string UIName = "Width (px)";
> = 3.0;

uniform vec3 lineColor
<
    string UIName = "Line Color";
    string UIWidget = "ColorPicker";
> = { 0.0, 0.0, 0.0 };

attribute VS_INPUT
{
    vec3 Pos : POSITION;
    vec3 Nrm : NORMAL;
};

attribute VS_TO_PS
{
    vec3 vn;
};

attribute PS_OUTPUT
{
    vec4 outColor : COLOR0;
};

GLSLShader ShaderVertex
{
    void main()
    {
        gl_Position = gWVP * vec4(Pos, 1.0);
        VS_OUT.vn = mat3(gWV) * Nrm;
    }
}

GLSLShader ShaderPixel
{
    void main()
    {
        vec3 N = normalize(PS_IN.vn);
        float facing = abs(N.z);
        float w = max(fwidth(facing), 1e-6);
        float sd = facing / w;
        float a = 1.0 - smoothstep(maxWidthPx - 1.0, maxWidthPx + 1.0, sd);
        if (a <= 0.002) discard;
        outColor = vec4(lineColor, a);
    }
}

technique Main
<
    string transparency = "transparent";
>
{
    pass P0
    <
        string drawContext = "colorPass";
    >
    {
        VertexShader (in VS_INPUT, out VS_TO_PS VS_OUT) = ShaderVertex;
        PixelShader  (in VS_TO_PS PS_IN, out PS_OUTPUT) = ShaderPixel;
    }
}
"""


_FRES_FX_PATH_CACHE = None


def _fresnel_fx_path():
    """フレネル用シェーダを現 drawAPI に応じて HLSL(.fx) か OGSFX(.ogsfx) で書き出してパスを返す。
    v0.1.37: OpenGL VP2 でも動くように GLSL(OGSFX) 版を追加。"""
    global _FRES_FX_PATH_CACHE
    is_gl = _vp2_is_opengl()
    src = _FRES_FX_OGSFX if is_gl else _FRES_FX_HLSL
    if _FRES_FX_PATH_CACHE and os.path.exists(_FRES_FX_PATH_CACHE) and \
       os.path.getsize(_FRES_FX_PATH_CACHE) == len(src):
        return _FRES_FX_PATH_CACHE
    d = os.path.join(cmds.internalVar(userAppDir=True), "OG_Toonline_Manager")
    try:
        if not os.path.isdir(d):
            os.makedirs(d)
    except Exception:
        d = cmds.internalVar(userTmpDir=True)
    fname = "OG_ToonFresnel.ogsfx" if is_gl else "OG_ToonFresnel.fx"
    path = os.path.join(d, fname).replace("\\", "/")
    try:
        if os.path.exists(path) and os.path.getsize(path) == len(src):
            _FRES_FX_PATH_CACHE = path
            return path
    except Exception:
        pass
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(src)
            f.flush()
            try: os.fsync(f.fileno())
            except Exception: pass
    except Exception as e:
        try:
            om2.MGlobal.displayWarning("[FRES] FX write failed: {0}".format(e))
        except Exception:
            pass
    _FRES_FX_PATH_CACHE = path
    return path


def _build_fresnel_network(line, shape, color):
    """フレネル輪郭シェーダを shape に割り当てる。
    VP2 が DirectX11 なら dx11Shader + HLSL、OpenGL なら glslShader + OGSFX を使う（v0.1.37）。
    カメラから見て寝た面（facing が 0 付近のシルエット/凹み）だけ不透明な線色、他は透明。
    VP2/Hardware 2.0 バッチで反映・カメラ依存・scriptJob 不要。
    太さ＝shader の maxWidthPx uniform（_ensure_thickness_chain が駆動）。"""
    plug, node_type = _hw_shader_plugin()
    try:
        if not cmds.pluginInfo(plug, q=True, loaded=True):
            cmds.loadPlugin(plug, quiet=True)
    except Exception:
        cmds.warning("{0} プラグインを読み込めません。VP2 のバックエンドをご確認ください。".format(plug))
        return None, None
    base = "toonFresnel_" + _short(line)
    fx = _fresnel_fx_path()
    suffix = "_GL" if node_type == "GLSLShader" else "_DX11"
    shd = cmds.shadingNode(node_type, asShader=True, name=base + suffix)
    sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=base + "_SG")
    try:
        cmds.connectAttr(shd + ".outColor", sg + ".surfaceShader", f=True)
    except Exception:
        pass
    try:
        cmds.setAttr(shd + ".shader", fx, type="string")   # .fx/.ogsfx をロード → uniform が attr 化
    except Exception:
        cmds.warning("フレネル用シェーダの読み込みに失敗しました（{0} をご確認ください）".format(plug))
    # 線色 uniform を設定
    if cmds.attributeQuery(FRES_COLOR, node=shd, exists=True):
        try:
            cmds.setAttr(shd + "." + FRES_COLOR, color[0], color[1], color[2], type="double3")
        except Exception:
            pass
    cmds.sets(shape, e=True, forceElement=sg)
    # line → dx11Shader を message でリンク（太さ駆動先・色変更先の特定に使う）
    if not cmds.attributeQuery(FRES_LINK, node=line, exists=True):
        cmds.addAttr(line, ln=FRES_LINK, at="message")
    try:
        cmds.connectAttr(shd + ".message", line + "." + FRES_LINK, f=True)
    except Exception:
        pass
    return shd, sg


def _all_screen_lines():
    """シーン内の全スクリーン輪郭ライン（SCRN_TAG）を返す（リファレンスしたアセットの線も含む）。"""
    out = []
    for root in _all_roots():
        for t in cmds.listRelatives(root, allDescendents=True, type="transform", f=True) or []:
            if cmds.attributeQuery(SCRN_TAG, node=t, exists=True) and t not in out:
                out.append(t)
    return out


def _scrn_intersect_on(line):
    """v0.1.41: SCRN ラインが交差線の対象かどうか。未設定（旧シーン）は True 扱い。"""
    try:
        if not cmds.attributeQuery(SCRN_INTERSECT_ON, node=line, exists=True):
            return True
        return bool(cmds.getAttr(line + "." + SCRN_INTERSECT_ON))
    except Exception:
        return True


def _scrn_set_intersect_on(line, enabled):
    """v0.1.41: SCRN ラインの交差線参加フラグを設定（属性が無ければ作る）。"""
    try:
        if not cmds.attributeQuery(SCRN_INTERSECT_ON, node=line, exists=True):
            cmds.addAttr(line, ln=SCRN_INTERSECT_ON, at="bool", dv=True)
        cmds.setAttr(line + "." + SCRN_INTERSECT_ON, bool(enabled))
    except Exception:
        pass


def _scrn_line_visible(line):
    """v0.1.44: SCRN ライン transform の visibility（親グループの可視も合わせる）。
    どちらかの階層で 0 なら全体として非表示扱い。"""
    try:
        node = line
        for _ in range(8):
            if not node or not cmds.objExists(node):
                return True
            try:
                if int(cmds.getAttr(node + ".visibility")) == 0:
                    return False
            except Exception:
                pass
            par = cmds.listRelatives(node, parent=True, f=True)
            if not par:
                return True
            node = par[0]
    except Exception:
        pass
    return True


def _scrn_intersect_meshes():
    """v0.1.41: 交差線対象の元メッシュ transform リスト（SCRN ラインで intersectOn=True のもの）。
    v0.1.44: 非表示 SCRN ラインは対象から外す（UI で非表示にしたら交差線も出ないように）。"""
    lines = [ln for ln in _all_screen_lines()
             if _scrn_intersect_on(ln) and _scrn_line_visible(ln)]
    return _source_meshes_of(lines)


def _scrn_intersect_screen_thickness():
    """v0.1.45: 交差線の画面 px 幅を、参加中の SCRN ラインの実効太さに合わせて算出。
    各 SCRN ラインの mB.output = ctrl.thickness × group.mult × global.mult を読み、最大値 ×
    SCRN_SCALE を返す（SCRN 本体と同じ pixel 幅）。該当ラインが無ければ既定 3.0。"""
    best = 0.0
    for ln in _all_screen_lines():
        if not _scrn_intersect_on(ln) or not _scrn_line_visible(ln):
            continue
        ctrl = _ctrl_of(ln)
        if not ctrl:
            continue
        mB = _short(ctrl) + "_thkB"
        if cmds.objExists(mB):
            try:
                v = float(cmds.getAttr(mB + ".output"))
                if v > best:
                    best = v
            except Exception:
                pass
        else:
            # 単純に ctrl.thickness を使う（倍率は無視）
            try:
                v = float(cmds.getAttr(ctrl + "." + CTRL_THICK))
                if v > best:
                    best = v
            except Exception:
                pass
    if best <= 0.0:
        return 3.0
    return max(1.0, best * SCRN_SCALE)


# --------------------------------------------------------------------------- #
# v0.1.46 ライン詳細設定: ラインごとの見え方（レンダーレイヤーで上書き可）と隠れ線の紐づけ
#   ・見え方の属性はライン transform に置く（LN_*）。線色/表側表示はシェーダへ接続。
#   ・マスター（ビューポート）の値＝属性値。各レンダーレイヤーでは Render Setup の
#     絶対オーバーライドで上書きする（プレイブラストはレイヤーを切り替えて行う運用）。
#   ・隠れ線は「紐づけた物」の裏に回った所だけ描く（紐づけ無し＝描かない）。
# --------------------------------------------------------------------------- #
def _attr_exists(node, attr):
    try:
        return bool(node) and cmds.objExists(node) and cmds.attributeQuery(attr, node=node, exists=True)
    except Exception:
        return False


def _plug_source(plug):
    """plug に入力接続があればその接続元 plug、無ければ None。"""
    try:
        c = cmds.listConnections(plug, s=True, d=False, p=True) or []
        return c[0] if c else None
    except Exception:
        return None


def _add_color_attr(node, ln, rgb):
    cmds.addAttr(node, ln=ln, at="float3", usedAsColor=True)
    for ch in ("R", "G", "B"):
        cmds.addAttr(node, ln=ln + ch, at="float", parent=ln)
    cmds.setAttr(node + "." + ln, rgb[0], rgb[1], rgb[2], type="float3")


# v0.1.49: ブラシ属性（ライン属性, シェーダ uniform, 既定値, 最小, 最大）
BRUSH_ATTRS = (
    (LN_BR_ROUGH,  SCRN_BRUSH_ROUGH,  0.0, 0.0, 1.0),
    (LN_BR_WAVE,   SCRN_BRUSH_WAVE,   0.0, 0.0, 1.0),
    (LN_BR_SCALE,  SCRN_BRUSH_SCALE, 20.0, 1.0, 100.0),
    (LN_BR_TAPER,  SCRN_BRUSH_TAPER,  0.0, 0.0, 1.0),
    (LN_BR_DRY,    SCRN_BRUSH_DRY,    0.0, 0.0, 1.0),
    (LN_BR_WOBBLE, SCRN_BRUSH_WOBBLE, 0.0, 0.0, 1.0),
    (LN_BR_DOUBLE, SCRN_BRUSH_DOUBLE, 0.0, 0.0, 1.0),
)

# v0.1.49: ブラシのプリセット（書いていない項目は既定値＝効果なし）
BRUSH_PRESETS = (
    ("ペン（くっきり）", {LN_BR_TAPER: 0.25}),
    ("筆", {LN_BR_TAPER: 0.85, LN_BR_WAVE: 0.3, LN_BR_ROUGH: 0.1, LN_BR_DRY: 0.1,
            LN_BR_SCALE: 25.0}),
    ("鉛筆", {LN_BR_ROUGH: 0.45, LN_BR_WAVE: 0.15, LN_BR_DRY: 0.35, LN_BR_TAPER: 0.3,
              LN_BR_WOBBLE: 0.15, LN_BR_SCALE: 14.0}),
    ("マーカー", {LN_BR_ROUGH: 0.05, LN_BR_WAVE: 0.05, LN_BR_DRY: 0.3, LN_BR_SCALE: 40.0}),
    ("スケッチ", {LN_BR_ROUGH: 0.25, LN_BR_WAVE: 0.2, LN_BR_DRY: 0.1, LN_BR_WOBBLE: 0.5,
                  LN_BR_DOUBLE: 0.5, LN_BR_TAPER: 0.3, LN_BR_SCALE: 25.0}),
    ("ブラシ効果なし", {}),
)


def _brush_preset_values(name):
    """プリセット名 → [(ライン属性, 値), ...]（全ブラシ属性ぶん）。"""
    vals = {}
    for pname, d in BRUSH_PRESETS:
        if pname == name:
            vals = d
            break
    return [(ln, float(vals.get(ln, dv))) for ln, _sh, dv, _lo, _hi in BRUSH_ATTRS]


def _tube_shape(line):
    sh = cmds.listRelatives(line, shapes=True, type="mesh", ni=True, f=True) or []
    return sh[0] if sh else None


def _tube_current_color(line):
    """チューブ（エッジ/クリース線）がいま使っている surfaceShader の色。"""
    sh = _tube_shape(line)
    if sh:
        for sg in cmds.listConnections(sh, type="shadingEngine") or []:
            for ss in cmds.listConnections(sg + ".surfaceShader") or []:
                try:
                    v = cmds.getAttr(ss + ".outColor")[0]
                    return [float(v[0]), float(v[1]), float(v[2])]
                except Exception:
                    pass
    return [0.0, 0.0, 0.0]


def _tube_own_shader(line, rgb):
    """チューブ専用の surfaceShader/SG（無ければ作って割り当てる）。個別カラーと同じ名前。"""
    # v0.1.53: 線色属性がつながっている surfaceShader があればそれを使う（リファレンスしたアセットの
    # 'chrA:toonCol_..._SS' は名前では見つからず、ショット側に別のシェーダを作って差し替えていた）
    if _attr_exists(line, LN_COLOR):
        for ss0 in cmds.listConnections(line + "." + LN_COLOR, s=False, d=True,
                                        type="surfaceShader") or []:
            return ss0
        bl = _tube_trace_blend(line)     # 色トレスを組んだチューブは blendColors の先
        if bl:
            for ss0 in cmds.listConnections(bl + ".output", s=False, d=True,
                                            type="surfaceShader") or []:
                return ss0
    base = COL_PREFIX + _base(line)
    ss = base + "_SS"
    sg = base + "_SG"
    if not cmds.objExists(ss):
        ss = cmds.shadingNode("surfaceShader", asShader=True, name=ss)
        cmds.setAttr(ss + ".outColor", rgb[0], rgb[1], rgb[2], type="double3")
    if not cmds.objExists(sg):
        sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=sg)
    _connect(ss + ".outColor", sg + ".surfaceShader")
    sh = _tube_shape(line)
    if sh:
        try:
            if not cmds.sets(sh, isMember=sg):
                cmds.sets(sh, e=True, forceElement=sg)
        except Exception:
            pass
    return ss


def _is_detail_line(line):
    """ライン詳細設定・隠れ線の対象（スクリーン輪郭 / エッジ線・クリース線）か。"""
    return _attr_exists(line, SCRN_TAG) or _attr_exists(line, EDGE_TAG)


def _ensure_line_render_attrs(line):
    """ラインに見え方属性を用意し、線色/表側表示をシェーダへ接続する（冪等）。
    SCRN はシェーダの uniform へ、エッジ/クリース線（v0.1.50）は専用 surfaceShader へ接続する。"""
    is_scrn = _attr_exists(line, SCRN_TAG)
    is_tube = (not is_scrn) and _attr_exists(line, EDGE_TAG)
    if not (is_scrn or is_tube):
        return False
    shd = _fresnel_shader(line) if is_scrn else None
    cur = [0.0, 0.0, 0.0]
    if shd and _attr_exists(shd, FRES_COLOR):
        try:
            v = cmds.getAttr(shd + "." + FRES_COLOR)[0]
            cur = [float(v[0]), float(v[1]), float(v[2])]
        except Exception:
            pass
    elif is_tube:
        cur = _tube_current_color(line)
    if not _attr_exists(line, LN_SHOW_VIS):
        cmds.addAttr(line, ln=LN_SHOW_VIS, at="bool", dv=True)
    if not _attr_exists(line, LN_COLOR):
        _add_color_attr(line, LN_COLOR, cur)
    if not _attr_exists(line, LN_HID_MODE):
        cmds.addAttr(line, ln=LN_HID_MODE, at="enum", en="off:dashed:solid", dv=1)
    if not _attr_exists(line, LN_HID_SAME):
        cmds.addAttr(line, ln=LN_HID_SAME, at="bool", dv=True)
    if not _attr_exists(line, LN_HID_COLOR):
        _add_color_attr(line, LN_HID_COLOR, cur)
    if not _attr_exists(line, LN_HID_ALPHA):
        cmds.addAttr(line, ln=LN_HID_ALPHA, at="double", min=0.0, max=1.0,
                     dv=_SCRN_HIDDEN_ALPHA_DEF)
    if not _attr_exists(line, LN_HID_DASH):
        cmds.addAttr(line, ln=LN_HID_DASH, at="double", min=1.0, max=500.0,
                     dv=_SCRN_HIDDEN_DASH_DEF)
    if not _attr_exists(line, LN_HID_FILL):
        cmds.addAttr(line, ln=LN_HID_FILL, at="double", min=0.05, max=0.95,
                     dv=_SCRN_HIDDEN_FILL_DEF)
    if not _attr_exists(line, LN_OCCLUDERS):
        cmds.addAttr(line, ln=LN_OCCLUDERS, at="message", multi=True, indexMatters=False)
    # v0.1.53: 色トレス（0 = 線の色のまま）
    if not _attr_exists(line, LN_TRACE):
        cmds.addAttr(line, ln=LN_TRACE, at="double", min=0.0, max=1.0, dv=0.0)
    if not _attr_exists(line, LN_TRACE_DARK):
        cmds.addAttr(line, ln=LN_TRACE_DARK, at="double", min=0.0, max=1.0, dv=TRACE_DARK_DEF)
    if is_tube:
        # 専用の surfaceShader に色を、表側の線の表示は透明度（0=見える / 1=消える）に接続
        ss = _tube_own_shader(line, cur)
        # 色トレスを組んだチューブは blendColors 経由（LN_COLOR → blend.color2）なので、直接つなぎ直さない
        if not _tube_trace_blend(line):
            _connect(line + "." + LN_COLOR, ss + ".outColor")
        rev = _base(line) + "_OG_visRev"
        if not cmds.objExists(rev):
            rev = cmds.createNode("reverse", name=rev)
        for ax in ("X", "Y", "Z"):
            _connect(line + "." + LN_SHOW_VIS, rev + ".input" + ax)
        _connect(rev + ".output", ss + ".outTransparency")
        return True
    # v0.1.49: ブラシ（初期値はいまのシェーダの値＝メイン画面で一括設定していた値を引き継ぐ）
    for ln_attr, sh_attr, dv, lo, hi in BRUSH_ATTRS:
        if _attr_exists(line, ln_attr):
            continue
        cur_v = dv
        if shd and _attr_exists(shd, sh_attr) and not _plug_source(shd + "." + sh_attr):
            try:
                cur_v = float(cmds.getAttr(shd + "." + sh_attr))
            except Exception:
                pass
        cmds.addAttr(line, ln=ln_attr, at="double", min=lo, max=hi, dv=dv)
        try:
            cmds.setAttr(line + "." + ln_attr, max(lo, min(hi, cur_v)))
        except Exception:
            pass
    if shd:
        links = [(LN_COLOR, FRES_COLOR), (LN_SHOW_VIS, SCRN_SHOW_VIS),
                 (LN_TRACE, SCRN_TRACE), (LN_TRACE_DARK, SCRN_TRACE_DARK)]
        links += [(ln_attr, sh_attr) for ln_attr, sh_attr, _dv, _lo, _hi in BRUSH_ATTRS]
        for src_attr, dst_attr in links:
            if not _attr_exists(shd, dst_attr):
                continue
            src = line + "." + src_attr
            dst = shd + "." + dst_attr
            try:
                if not cmds.isConnected(src, dst):
                    cmds.connectAttr(src, dst, f=True)
            except Exception:
                pass
        # 旧版の「何の裏でも透ける隠れ線」は使わない（値が違うときだけ書く＝Undo 履歴を汚さない）
        if _attr_exists(shd, SCRN_HIDDEN_SHOW) and not _plug_source(shd + "." + SCRN_HIDDEN_SHOW):
            try:
                if int(cmds.getAttr(shd + "." + SCRN_HIDDEN_SHOW)) != 0:
                    cmds.setAttr(shd + "." + SCRN_HIDDEN_SHOW, 0)
            except Exception:
                pass
    return True


def _scrn_set_color(line, rgb):
    """SCRN/フレネルの線色（マスター値）を設定。レイヤーで上書き中なら警告して False。"""
    if _attr_exists(line, LN_COLOR):
        try:
            cmds.setAttr(line + "." + LN_COLOR, rgb[0], rgb[1], rgb[2], type="float3")
            return True
        except Exception:
            cmds.warning("{0}: 線の色はいまのレンダーレイヤーで上書きされているため変更できません"
                         "（マスターレイヤーに切り替えるか、ライン詳細設定で変更してください）"
                         .format(_short(line)))
            return False
    shd = _fresnel_shader(line)
    if shd and _attr_exists(shd, FRES_COLOR) and not _plug_source(shd + "." + FRES_COLOR):
        try:
            cmds.setAttr(shd + "." + FRES_COLOR, rgb[0], rgb[1], rgb[2], type="double3")
            return True
        except Exception:
            pass
    return False


# ---- v0.1.53: 色トレス（線の色を下の面のマテリアルの色から作る） ----
_TRACE_CACHE = {}   # line key -> 頂点ごとの面の色 [(r,g,b), ...]（スクリーン輪郭）


def _sg_base_color(sg):
    """シェーディンググループの『面の色』（マテリアルの基本色。テクスチャがつながっていれば平均色）。"""
    ss = (cmds.listConnections(sg + ".surfaceShader", s=True, d=False) or [None])[0]
    if not ss:
        return (0.5, 0.5, 0.5)
    for at in ("baseColor", "color", "diffuseColor", "outColor"):
        if not _attr_exists(ss, at):
            continue
        src = cmds.listConnections(ss + "." + at, s=True, d=False) or []
        if src:
            try:
                vals = cmds.colorAtPoint(src[0], o="RGB", su=6, sv=6, mu=0.05, mv=0.05,
                                         xu=0.95, xv=0.95) or []
                n = len(vals) // 3
                if n:
                    return (sum(vals[0::3]) / n, sum(vals[1::3]) / n, sum(vals[2::3]) / n)
            except Exception:
                pass
        try:
            v = cmds.getAttr(ss + "." + at)[0]
            return (float(v[0]), float(v[1]), float(v[2]))
        except Exception:
            pass
    return (0.5, 0.5, 0.5)


def _mesh_face_colors(shape):
    """メッシュの面ごとの色（割り当てたマテリアルの色）と MFnMesh を返す。"""
    sl = om2.MSelectionList()
    sl.add(shape)
    dag = sl.getDagPath(0)
    mfn = om2.MFnMesh(dag)
    shaders, idx = mfn.getConnectedShaders(dag.instanceNumber())
    cols = [_sg_base_color(om2.MFnDependencyNode(shaders[k]).name()) for k in range(len(shaders))]
    gray = (0.5, 0.5, 0.5)
    return [cols[i] if 0 <= i < len(cols) else gray for i in idx], idx, mfn


def _scrn_trace_colors(line):
    """スクリーン輪郭の頂点ごとの面の色（元メッシュの面のマテリアル）。キャッシュする。
    元メッシュとラインは同じトポロジー（複製）なので頂点番号がそのまま対応する。"""
    key = _ukey(line)
    n = _mesh_vertex_count(line)
    c = _TRACE_CACHE.get(key)
    if c is not None and len(c) == n:
        return c
    out = []
    src = _line_source_mesh(line)
    shs = cmds.listRelatives(src, shapes=True, type="mesh", ni=True, f=True) or [] if src else []
    if shs:
        try:
            fcols, _idx, mfn = _mesh_face_colors(shs[0])
            counts, verts = mfn.getVertices()
            out = [None] * mfn.numVertices
            k = 0
            for f, cnt in enumerate(counts):
                col = fcols[f]
                for j in range(cnt):
                    v = verts[k + j]
                    if out[v] is None:
                        out[v] = col
                k += cnt
            out = [c2 if c2 is not None else (0.5, 0.5, 0.5) for c2 in out]
        except Exception:
            out = []
    if len(out) != n:
        out = []
    _TRACE_CACHE[key] = out
    return out


def _tube_trace_raw_color(line):
    """エッジ線・クリース線の元のエッジに隣り合う面のうち、いちばん多いマテリアルの色。"""
    src = _line_source_mesh(line)
    shs = cmds.listRelatives(src, shapes=True, type="mesh", ni=True, f=True) or [] if src else []
    p2c = cmds.ls(cmds.listHistory(line) or [], type="polyEdgeToCurve") or []
    if not shs or not p2c:
        return None
    # inputComponents は 'vtx[2]', 'vtx[3]', ... の頂点の並び（版によっては 'e[...]'）
    vtx, edges = [], []
    try:
        for comp in cmds.getAttr(p2c[0] + ".inputComponents") or []:
            kind = comp.split("[")[0]
            inner = comp.split("[")[-1].rstrip("]")
            if ":" in inner:
                a, b = inner.split(":")
                ids = list(range(int(a), int(b) + 1))
            elif inner.isdigit():
                ids = [int(inner)]
            else:
                continue
            (edges if kind == "e" else vtx).extend(ids)
    except Exception:
        return None
    try:
        fcols, idx, mfn = _mesh_face_colors(shs[0])
        sl = om2.MSelectionList()
        sl.add(shs[0])
        dag = sl.getDagPath(0)
        if vtx and not edges:
            # 隣り合う頂点の組をつなぐ辺
            itv = om2.MItMeshVertex(dag)
            for a, b in zip(vtx[:-1], vtx[1:]):
                itv.setIndex(a)
                for e in itv.getConnectedEdges():
                    ev = mfn.getEdgeVertices(e)
                    if b in ev:
                        edges.append(e)
                        break
        if not edges:
            return None
        ite = om2.MItMeshEdge(dag)
        counts = {}
        colors = {}
        for e in edges:
            ite.setIndex(e)
            for f in ite.getConnectedFaces():
                if 0 <= f < len(fcols):
                    s = int(idx[f])
                    counts[s] = counts.get(s, 0) + 1
                    colors[s] = fcols[f]
        if not counts:
            return None
        best = max(counts, key=lambda k: counts[k])
        return colors[best]
    except Exception:
        return None


def _tube_trace_blend(line):
    """色トレスを組んだチューブの blendColors（LN_COLOR → color2）。無ければ None。"""
    if not _attr_exists(line, LN_COLOR):
        return None
    for n in cmds.listConnections(line + "." + LN_COLOR, s=False, d=True, type="blendColors") or []:
        return n
    return None


def _ensure_tube_trace(line, recompute=False):
    """エッジ線・クリース線に色トレスの DG ネットワークを組む（レイヤーごとの上書きもそのまま効く）。
    面の色(LN_TRACE_RAW) × 明るさ(LN_TRACE_DARK) と 線の色(LN_COLOR) を LN_TRACE で混ぜて surfaceShader へ。"""
    if not (_attr_exists(line, EDGE_TAG) and _attr_exists(line, LN_COLOR)):
        return False
    if not _attr_exists(line, LN_TRACE_RAW):
        _add_color_attr(line, LN_TRACE_RAW, (0.5, 0.5, 0.5))
        recompute = True
    if recompute:
        rgb = _tube_trace_raw_color(line)
        if rgb:
            try:
                cmds.setAttr(line + "." + LN_TRACE_RAW, rgb[0], rgb[1], rgb[2], type="float3")
            except Exception:
                pass
    try:
        cur = cmds.getAttr(line + "." + LN_COLOR)[0]
    except Exception:
        cur = (0.0, 0.0, 0.0)
    ss = _tube_own_shader(line, cur)
    base = _base(line)
    mul = base + "_OG_traceMul"
    if not cmds.objExists(mul):
        mul = cmds.createNode("multiplyDivide", name=mul)
    blend = _tube_trace_blend(line) or base + "_OG_traceBlend"
    if not cmds.objExists(blend):
        blend = cmds.createNode("blendColors", name=blend)
    _connect(line + "." + LN_TRACE_RAW, mul + ".input1")
    for ax in ("X", "Y", "Z"):
        _connect(line + "." + LN_TRACE_DARK, mul + ".input2" + ax)
    _connect(mul + ".output", blend + ".color1")
    _connect(line + "." + LN_COLOR, blend + ".color2")
    _connect(line + "." + LN_TRACE, blend + ".blender")
    _connect(blend + ".output", ss + ".outColor")
    return True


def _trace_prepare(line, recompute=False):
    """色トレスを使えるようにする（スクリーン輪郭は頂点カラーに面の色を書く／チューブは DG を組む）。"""
    if _attr_exists(line, SCRN_TAG):
        if recompute:
            _TRACE_CACHE.pop(_ukey(line), None)
        if recompute or not _TRACE_CACHE.get(_ukey(line)):
            _update_scrn_curv_weights(line)    # 太さ倍率(R) と一緒に面の色(GBA) を書く
        return True
    if _attr_exists(line, EDGE_TAG):
        return _ensure_tube_trace(line, recompute=recompute)
    return False


def _line_occluders(line):
    """隠れ線を透かす物（transform のロング名）。"""
    if not _attr_exists(line, LN_OCCLUDERS):
        return []
    out = []
    for n in cmds.listConnections(line + "." + LN_OCCLUDERS, s=True, d=False) or []:
        full = cmds.ls(n, long=True) or []
        if full and full[0] not in out:
            out.append(full[0])
    return out


def _mesh_transforms_under(nodes):
    """transform 群の配下（自身を含む）にあるメッシュの transform。toon ラインは除く。"""
    out = []
    seen = set()
    for t in nodes or []:
        if not cmds.objExists(t):
            continue
        for s in cmds.listRelatives(t, ad=True, type="mesh", f=True, ni=True) or []:
            par = cmds.listRelatives(s, parent=True, f=True) or []
            if not par or par[0] in seen or _is_toon_node(par[0]):
                continue
            seen.add(par[0])
            out.append(par[0])
    return out


def _occluder_candidates(nodes):
    """選択などから紐づけ先にできる transform（メッシュを含み、toon ラインではない物）。"""
    out = []
    for n in cmds.ls(nodes or [], objectsOnly=True, long=True) or []:
        t = n
        if not cmds.objectType(n, isAType="transform"):
            par = cmds.listRelatives(n, parent=True, f=True) or []
            if not par:
                continue
            t = par[0]
        if t in out or _is_toon_node(t) or not _mesh_transforms_under([t]):
            continue
        out.append(t)
    return out


def _add_line_occluders(line, nodes):
    """ラインの隠れ線を透かす物を追加。追加した数を返す。"""
    _ensure_line_render_attrs(line)
    if not _attr_exists(line, LN_OCCLUDERS):
        return 0
    cur = set(_line_occluders(line))
    added = 0
    for t in _occluder_candidates(nodes):
        if t in cur:
            continue
        try:
            cmds.connectAttr(t + ".message", line + "." + LN_OCCLUDERS, na=True)
            cur.add(t)
            added += 1
        except Exception:
            pass
    return added


def _remove_line_occluders(line, nodes):
    """ラインの隠れ線の紐づけから物を外す。外した数を返す。"""
    if not _attr_exists(line, LN_OCCLUDERS):
        return 0
    targets = set()
    for n in nodes or []:
        if cmds.objExists(n):
            targets.add((cmds.ls(n, long=True) or [n])[0])
    conns = cmds.listConnections(line + "." + LN_OCCLUDERS, s=True, d=False, c=True, p=True) or []
    removed = 0
    for i in range(0, len(conns) - 1, 2):
        dst, src = conns[i], conns[i + 1]
        node = src.split(".")[0]
        node = (cmds.ls(node, long=True) or [node])[0]
        if node in targets:
            try:
                cmds.disconnectAttr(src, dst)
                removed += 1
            except Exception:
                pass
    return removed


_HIDDEN_ON_FALLBACK = [True]   # ROOT が無いときの「隠れ線を描く」既定


def _all_roots():
    """シーン内の全 ROOT（このシーンのもの＋リファレンスしたアセットの 'chr:toonOutlines_grp' など）。
    v0.1.52: 以前は名前空間なしの ROOT しか見ず、輪郭入りのキャラをリファレンスすると
    隠れ線が出なかった。"""
    try:
        return cmds.ls(ROOT, recursive=True, long=True, type="transform") or []
    except Exception:
        return [ROOT] if cmds.objExists(ROOT) else []


def _root_of(node):
    """ノードが属する ROOT のフルパス（このシーンの '|toonOutlines_grp' か、リファレンス等の
    '|chrA:toonOutlines_grp'）。ROOT の下でなければ None。"""
    try:
        full = (cmds.ls(node, long=True) or [node])[0]
    except Exception:
        return None
    parts = full.split("|")
    if len(parts) > 1 and parts[1].split(":")[-1] == ROOT:
        return "|" + parts[1]
    return None


def _is_foreign(node):
    """v0.1.53: リファレンスしたキャラなど、このシーンの ROOT 以外（名前空間付きの ROOT）の下のノードか。
    こういうラインはアセット側で作ってあるので、ツールは構造（コントローラーの親子・太さの計算のつなぎ）に
    触らず、値の変更とキーだけを行う（触るとこのシーンの全体コントローラーへつなぎ替えてしまう）。"""
    r = _root_of(node)
    return bool(r) and r != "|" + ROOT


def _global_ctrl_for(node):
    """node が別 ROOT（リファレンス等）の下なら、その ROOT の全体コントローラー。それ以外は None。"""
    r = _root_of(node) if node else None
    if not r or r == "|" + ROOT:
        return None
    for c in cmds.listRelatives(r, children=True, type="transform", f=True) or []:
        if _short(c).split(":")[-1] == GLOBAL_CTRL:
            return c
    return None


def _hidden_scene_on():
    """シーン全体で隠れ線を描くか（ROOT.toonHiddenOn、既定 ON）。
    このシーンの ROOT にあればそれに従う。無ければリファレンスしたアセットの ROOT
    （どれかが ON なら ON）。"""
    if _attr_exists(ROOT, ROOT_HIDDEN_ON):
        try:
            return bool(cmds.getAttr(ROOT + "." + ROOT_HIDDEN_ON))
        except Exception:
            pass
    vals = []
    for r in _all_roots():
        if _attr_exists(r, ROOT_HIDDEN_ON):
            try:
                vals.append(bool(cmds.getAttr(r + "." + ROOT_HIDDEN_ON)))
            except Exception:
                pass
    if vals:
        return any(vals)
    return _HIDDEN_ON_FALLBACK[0]


def _set_hidden_scene_on(on):
    _HIDDEN_ON_FALLBACK[0] = bool(on)
    if cmds.objExists(ROOT):
        if not _attr_exists(ROOT, ROOT_HIDDEN_ON):
            cmds.addAttr(ROOT, ln=ROOT_HIDDEN_ON, at="bool", dv=True)
        try:
            cmds.setAttr(ROOT + "." + ROOT_HIDDEN_ON, bool(on))
        except Exception:
            pass


_HIDDEN_GROUPS = []   # [(紐づけ先メッシュ transform のタプル, [ライン, ...]), ...]


def _all_detail_lines():
    """ライン詳細設定・隠れ線の対象になる全ライン（スクリーン輪郭＋エッジ線・クリース線）。"""
    out = []
    for root in _all_roots():
        for t in cmds.listRelatives(root, allDescendents=True, type="transform", f=True) or []:
            if _is_detail_line(t) and t not in out:
                out.append(t)
    return out


def _hidden_refresh_cache():
    """紐づけ先ごとに隠れ線を描くラインをまとめ直す（紐づけ/ライン増減時に呼ぶ）。"""
    groups = {}
    order = []
    for ln in _all_detail_lines():
        occ = _mesh_transforms_under(_line_occluders(ln))
        if not occ:
            continue
        key = tuple(sorted(occ))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(ln)
    _HIDDEN_GROUPS[:] = [(k, groups[k]) for k in order]
    return _HIDDEN_GROUPS


def _hidden_line_mode(line):
    try:
        return int(cmds.getAttr(line + "." + LN_HID_MODE))
    except Exception:
        return 0


def _hidden_frame_groups():
    """描画ごとに: 表示中で隠れ線モードが ON のラインだけを残したグループ。"""
    out = []
    if not _hidden_scene_on():
        return out
    for occ, lines in _HIDDEN_GROUPS:
        # v0.1.53: 親を変えた等でパスが変わったら、次のイベントループで一覧を読み直す
        if any(not cmds.objExists(ln) for ln in lines):
            _request_hidden_refresh()
        occ2 = [o for o in occ if cmds.objExists(o)]
        lines2 = [ln for ln in lines if cmds.objExists(ln) and _scrn_line_visible(ln)
                  and _hidden_line_mode(ln) != 0
                  and (_fresnel_shader(ln) if _attr_exists(ln, SCRN_TAG) else _tube_shape(ln))]
        if occ2 and lines2:
            out.append((occ2, lines2))
    return out


def _upgrade_scrn_shader(line):
    """v0.1.46: 旧版のシェーダファイルを指す SCRN ラインを現行版へ差し替える。
    入力接続（太さの駆動など）と値は控えて戻す。差し替えたら True。"""
    shd = _fresnel_shader(line)
    if not shd or not _attr_exists(line, SCRN_TAG) or not _attr_exists(shd, "shader"):
        return False
    try:
        cur = (cmds.getAttr(shd + ".shader") or "").replace("\\", "/")
    except Exception:
        return False
    # v0.1.53: いまのビューポート（DX11/OpenGL）と合わない種類のシェーダノードは触らない。
    # 以前は DX11 で作ったシーンを OpenGL で開くと、dx11Shader に OpenGL 用(.ogsfx)のファイルを
    # 差してしまい、そのまま保存すると DX11 で開いたときも壊れたまま（読み込みエラー）になった。
    if (cmds.nodeType(shd) == "GLSLShader") != _vp2_is_opengl():
        return False
    path = _screen_fx_path()
    if cur == path:
        return False
    conns = cmds.listConnections(shd, s=True, d=False, c=True, p=True) or []
    pairs = [(conns[i + 1], conns[i]) for i in range(0, len(conns) - 1, 2)]
    keep = {}
    for an in (FRES_COLOR, SCRN_THICK, SCRN_DEPTH_BIAS, "stability",
               SCRN_BRUSH_ROUGH, SCRN_BRUSH_WAVE, SCRN_BRUSH_SCALE):
        if _attr_exists(shd, an) and not _plug_source(shd + "." + an):
            try:
                keep[an] = cmds.getAttr(shd + "." + an)
            except Exception:
                pass
    try:
        cmds.setAttr(shd + ".shader", path, type="string")
    except Exception:
        return False
    for an, v in keep.items():
        if not _attr_exists(shd, an):
            continue
        try:
            if isinstance(v, (list, tuple)) and v and isinstance(v[0], (list, tuple)):
                c = v[0]
                cmds.setAttr(shd + "." + an, c[0], c[1], c[2], type="double3")
            else:
                cmds.setAttr(shd + "." + an, v)
        except Exception:
            pass
    for src, dst in pairs:
        try:
            if cmds.objExists(dst) and not cmds.isConnected(src, dst):
                cmds.connectAttr(src, dst, f=True)
        except Exception:
            pass
    _bind_scrn_color_set_to_shader(shd, SCRN_CSET)
    return True


def _prepare_scrn_lines():
    """既存 SCRN ラインを現行シェーダ＋見え方属性へ揃える（冪等・ツール起動時に呼ぶ）。"""
    for ln in _all_screen_lines():
        try:
            _upgrade_scrn_shader(ln)
        except Exception:
            pass
        try:
            _ensure_line_render_attrs(ln)
        except Exception:
            pass
    # エッジ/クリース線は、詳細設定を使ったもの（見え方属性がある）だけ接続を直す。
    # 使っていない線を勝手に専用シェーダへ切り替えない（共通カラーに付いたままにする）。
    for ln in _all_detail_lines():
        if not _attr_exists(ln, SCRN_TAG) and _attr_exists(ln, LN_COLOR):
            try:
                _ensure_line_render_attrs(ln)
            except Exception:
                pass


def _scrn_set_depth_bias(v):
    """全スクリーン輪郭ラインの重なり深度押し込み（ビュー空間）を設定。"""
    global _SCRN_ZBIAS
    try:
        _SCRN_ZBIAS = float(v)
    except Exception:
        return
    for line in _all_screen_lines():
        shd = _fresnel_shader(line)
        if shd and cmds.attributeQuery(SCRN_DEPTH_BIAS, node=shd, exists=True):
            try:
                cmds.setAttr(shd + "." + SCRN_DEPTH_BIAS, _SCRN_ZBIAS)
            except Exception:
                pass
    try:
        cmds.refresh()
    except Exception:
        pass


def _scrn_set_stability(v):
    """v0.1.33: 全 SCRN shader に stability uniform を反映。"""
    try:
        v = float(v)
    except Exception:
        return
    for line in _all_screen_lines():
        shd = _fresnel_shader(line)
        if shd and cmds.attributeQuery("stability", node=shd, exists=True):
            try:
                cmds.setAttr(shd + ".stability", v)
            except Exception:
                pass
    try:
        cmds.refresh()
    except Exception:
        pass


def _scrn_set_brush(rough=None, wave=None, scale=None):
    """v0.1.14: 全スクリーン輪郭ラインのブラシ質感（縁カサつき/太さゆらぎ/ノイズ周期）を設定。
    None の項目は変更しない。"""
    global _SCRN_BRUSH_ROUGH_DEF, _SCRN_BRUSH_WAVE_DEF, _SCRN_BRUSH_SCALE_DEF
    if rough is not None:
        try: _SCRN_BRUSH_ROUGH_DEF = float(rough)
        except Exception: pass
    if wave is not None:
        try: _SCRN_BRUSH_WAVE_DEF = float(wave)
        except Exception: pass
    if scale is not None:
        try: _SCRN_BRUSH_SCALE_DEF = float(scale)
        except Exception: pass
    # v0.1.49: 渡された項目だけを、全ラインのマスター値（ライン属性）へ書く。
    # 1 つ変えただけで他の項目まで上書きすると、ライン詳細設定で変えた値が消えるため。
    pairs = []
    if rough is not None:
        pairs.append((LN_BR_ROUGH, SCRN_BRUSH_ROUGH, _SCRN_BRUSH_ROUGH_DEF))
    if wave is not None:
        pairs.append((LN_BR_WAVE, SCRN_BRUSH_WAVE, _SCRN_BRUSH_WAVE_DEF))
    if scale is not None:
        pairs.append((LN_BR_SCALE, SCRN_BRUSH_SCALE, _SCRN_BRUSH_SCALE_DEF))
    cmds.undoInfo(openChunk=True)
    try:
        for line in _all_screen_lines():
            shd = _fresnel_shader(line)
            for ln_attr, sh_attr, v in pairs:
                if _attr_exists(line, ln_attr):
                    try:
                        cmds.setAttr(line + "." + ln_attr, v)
                    except Exception:
                        pass   # レンダーレイヤーで上書き中
                elif shd and _attr_exists(shd, sh_attr) and not _plug_source(shd + "." + sh_attr):
                    try:
                        cmds.setAttr(shd + "." + sh_attr, v)
                    except Exception:
                        pass
    finally:
        cmds.undoInfo(closeChunk=True)
    try: cmds.refresh()
    except Exception: pass


def _scrn_set_hidden(enabled=None, dash_len=None, dash_fill=None, alpha=None):
    """v0.1.38: 全スクリーン輪郭ラインの隠れ線（破線で裏側シルエット）設定。
    HLSL(DX11) の 2nd pass に効く。OpenGL(OGSFX) の shader にはこれらの uniform
    が無いので attributeQuery で存在チェックしてから setAttr（＝OpenGL では無効）。
    None の項目は変更しない。"""
    global _SCRN_HIDDEN_DEF, _SCRN_HIDDEN_DASH_DEF, _SCRN_HIDDEN_FILL_DEF, _SCRN_HIDDEN_ALPHA_DEF
    if enabled is not None:
        try: _SCRN_HIDDEN_DEF = 1 if enabled else 0
        except Exception: pass
    if dash_len is not None:
        try: _SCRN_HIDDEN_DASH_DEF = float(dash_len)
        except Exception: pass
    if dash_fill is not None:
        try: _SCRN_HIDDEN_FILL_DEF = float(dash_fill)
        except Exception: pass
    if alpha is not None:
        try: _SCRN_HIDDEN_ALPHA_DEF = float(alpha)
        except Exception: pass
    pairs = ((SCRN_HIDDEN_SHOW,  _SCRN_HIDDEN_DEF),
             (SCRN_HIDDEN_DASH,  _SCRN_HIDDEN_DASH_DEF),
             (SCRN_HIDDEN_FILL,  _SCRN_HIDDEN_FILL_DEF),
             (SCRN_HIDDEN_ALPHA, _SCRN_HIDDEN_ALPHA_DEF))
    for line in _all_screen_lines():
        shd = _fresnel_shader(line)
        if not shd: continue
        for an, v in pairs:
            if cmds.attributeQuery(an, node=shd, exists=True):
                try: cmds.setAttr(shd + "." + an, v)
                except Exception: pass
    try: cmds.refresh()
    except Exception: pass


_SCRN_FX_HLSL_COMMON = """// OG Toonline Manager - Geometry-shader silhouette outline (dx11Shader / HLSL5)
// v0.1.11+ 本格派: VS でビュー空間座標・法線を計算 → GS で三角形ごとに
// シルエット検出(頂点の facing 符号が混在する三角形だけ処理) → エッジ横断点
// 2 つを結ぶ細い quad をスクリーン空間で押し出す。これにより:
//   - 外周シルエット: 綺麗な薄線(従来通り)
//   - 凹部内側シルエット(トーラスの穴など): blob 化せず綺麗な薄線に
//   - distance attenuation 付き(遠距離で線が緩やかに細くなる)
//   - 曲率(COLOR0.r)もエッジ横断点で補間
// ※ ビューポートは「テクスチャ表示 ON(ホットキー 6)」で表示。VP2/DirectX11 前提。
float4x4 gWVP : WorldViewProjection;
float4x4 gWV  : WorldView;
float4x4 gProj : Projection;
float2   gScreen : ViewportPixelSize;

float thickness <
    string UIName = "Thickness(px)";
    float UIMin = 0.0;
    float UIMax = 30.0;
    float UIStep = 0.1;
> = 3.0;

float depthBias <
    string UIName = "Overlap Depth Bias (view ratio)";
    float UIMin = -0.5;
    float UIMax = 0.5;
    float UIStep = 0.001;
> = 0.02;   // 相対量: vpos.z *= (1+depthBias)。0.02 = 2% 奥へ押す。

// v0.1.33: 時間的安定性（チラつき抑制）
// 0.0 = 無効（既定）。0.0〜1.0 で境界付近の弱い三角形を emission 抑制。
// GS 内で min(|f0|,|f1|,|f2|) が stability * 0.02 以下の三角形をスキップ。
// 弱い triangle は camera 動きでフリップしやすいので止めるとチラつきが減る。
// 高い値にすると線が薄くなる・小さな隙間が出ることがある。
float stability <
    string UIName = "Temporal Stability";
    float UIMin = 0.0;
    float UIMax = 1.0;
    float UIStep = 0.01;
> = 0.0;

float3 lineColor <
    string UIName = "Line Color";
    string UIWidget = "Color";
> = {0.0f, 0.0f, 0.0f};

// v0.1.14: ペンシル風ブラシテクスチャ
float brushRoughness <
    string UIName = "Brush Roughness (edge noise)";
    float UIMin = 0.0;
    float UIMax = 1.0;
    float UIStep = 0.01;
> = 0.0;
float brushWaviness <
    string UIName = "Brush Waviness (thickness noise)";
    float UIMin = 0.0;
    float UIMax = 1.0;
    float UIStep = 0.01;
> = 0.0;
float brushNoiseScale <
    string UIName = "Brush Noise Scale (px freq)";
    float UIMin = 1.0;
    float UIMax = 100.0;
    float UIStep = 0.5;
> = 20.0;

// v0.1.38: 隠れ線（裏側シルエット）を破線で表示するモード
int showHidden <
    string UIName = "Show Hidden Lines (dashed)";
    int UIMin = 0;
    int UIMax = 1;
> = 0;

float hiddenDashLen <
    string UIName = "Hidden Dash Length (px)";
    float UIMin = 1.0;
    float UIMax = 80.0;
    float UIStep = 0.5;
> = 10.0;

float hiddenDashFill <
    string UIName = "Hidden Dash Fill (0-1)";
    float UIMin = 0.05;
    float UIMax = 0.95;
    float UIStep = 0.01;
> = 0.55;

float hiddenAlpha <
    string UIName = "Hidden Line Alpha";
    float UIMin = 0.0;
    float UIMax = 1.0;
    float UIStep = 0.01;
> = 0.6;

// v0.1.46: 表側の線を描くか（レイヤーで「隠れ線だけ」を書き出すときに 0）
int showVisible <
    string UIName = "Show Visible Line";
    int UIMin = 0;
    int UIMax = 1;
> = 1;

// v0.1.49: ブラシのバリエーション（すべて 0 = 効果なし）
float brushTaper <
    string UIName = "Brush Taper (line ends thinner)";
    float UIMin = 0.0;
    float UIMax = 1.0;
> = 0.0;
float brushDry <
    string UIName = "Brush Dry (broken stroke)";
    float UIMin = 0.0;
    float UIMax = 1.0;
> = 0.0;
float brushWobble <
    string UIName = "Brush Wobble (sideways drift)";
    float UIMin = 0.0;
    float UIMax = 1.0;
> = 0.0;
float brushDouble <
    string UIName = "Brush Double Stroke";
    float UIMin = 0.0;
    float UIMax = 1.0;
> = 0.0;

// v0.1.53: 色トレス。COLOR0.gba = 下の面のマテリアルの色（Python が書く）。
float traceAmount <
    string UIName = "Color Trace Amount";
    float UIMin = 0.0;
    float UIMax = 1.0;
> = 0.0;
float traceDark <
    string UIName = "Color Trace Brightness";
    float UIMin = 0.0;
    float UIMax = 1.0;
> = 0.45;

struct APPDATA {
    float3 Position : POSITION;
    float3 Normal   : NORMAL;
#ifndef OG_NO_COLOR0
    float4 Color    : COLOR0;
#endif
};
struct V2G {
    float4 CPos  : SV_Position;  // 使わないが rasterizer 要件
    float3 VSPos : TEXCOORD0;    // ビュー空間位置
    float3 VSNrm : TEXCOORD1;    // ビュー空間法線
    float  ColR  : TEXCOORD2;    // 曲率倍率(COLOR0.r)
    float3 ColT  : TEXCOORD3;    // v0.1.53: 面の色（COLOR0.gba）
};
struct G2P {
    float4 HPos  : SV_Position;
    float2 UV    : TEXCOORD0;     // v0.1.14: U=長手(0-1), V=太さ方向(-1..+1)
    float  LenPx : TEXCOORD1;     // v0.1.38: この quad の長手方向のスクリーン pixel 長
    float3 Trace : TEXCOORD2;     // v0.1.53: 面の色（色トレス）
};

// v0.1.53: 面の色から線の色を作る（少し鮮やかにして traceDark 倍に暗くする）
float3 og_trace_color(float3 raw)
{
    float mx = max(raw.r, max(raw.g, raw.b));
    float3 c = raw;
    if (mx > 1e-4f) {
        float3 gray = float3(mx, mx, mx);
        c = saturate(gray + (raw - gray) * 1.35f);
    }
    return c * traceDark;
}

// 1D ハッシュ (iq/Hugo Elias 系)
float hash12(float2 p)
{
    float3 p3 = frac(float3(p.xyx) * 0.1031f);
    p3 += dot(p3, p3.yzx + 33.33f);
    return frac((p3.x + p3.y) * p3.z);
}

// v0.1.49: なめらかな値ノイズ（0..1）。隣り合う点で値が連続するので、線の継ぎ目で途切れない。
float vnoise(float2 p)
{
    float2 i = floor(p);
    float2 f = frac(p);
    float a = hash12(i);
    float b = hash12(i + float2(1.0f, 0.0f));
    float c = hash12(i + float2(0.0f, 1.0f));
    float d = hash12(i + float2(1.0f, 1.0f));
    float2 u = f * f * (3.0f - 2.0f * f);
    return lerp(lerp(a, b, u.x), lerp(c, d, u.x), u.y);
}

V2G VShader(APPDATA i)
{
    V2G o;
    float4 vp = mul(float4(i.Position, 1.0f), gWV);
    o.VSPos = vp.xyz;
    o.VSNrm = mul(i.Normal, (float3x3)gWV);
    // v0.1.11: GS アプローチでは深度バイアス不要(quad はシルエット位置に描く)。
    // 旧 VS 版のため uniform は残置するが VS では使わない。
    o.CPos = mul(vp, gProj);
#ifndef OG_NO_COLOR0
    float w = i.Color.r;
    o.ColR = (w < 0.001f) ? 1.0f : w;
    o.ColT = i.Color.gba;
#else
    o.ColR = 1.0f;
    o.ColT = float3(0.0f, 0.0f, 0.0f);
#endif
    return o;
}

// 符号付き facing: カメラ(view 空間原点)から見て面が向いていれば正、裏向きで負。
float facing_dot(float3 vspos, float3 vsnrm)
{
    float3 n = normalize(vsnrm);
    float3 v = vspos;
    float len = length(v);
    if (len < 1e-6f) return 1.0f;
    v = v / len;
    return -dot(n, v);  // -view_dir(=-normalize(vspos)) と normal の内積
}

// エッジ(a,b) 上で facing が符号反転する点(シルエット上)を求め、
// その点のクリップ座標・ColR・view 空間位置と法線 を返す。
// v0.1.14: distance-invariant 深度バイアス。クリップ空間で cpos.z を depthBias*cpos.w だけ
// camera 寄りに引く。NDC Z で一定量のオフセットになるので、カメラがどれだけ遠くても
// 他オブジェクトとの重なり線が消えない。
// v0.1.15b: perp 計算のため補間済み VSPos と VSNrm も返す（隣接三角形と完全一致する miter joint 用）。
void edge_crossing(V2G a, V2G b, float fa, float fb,
                   out float4 cpos, out float colR,
                   out float3 vspos, out float3 vsnrm, out float3 colT)
{
    float t = fa / (fa - fb);
    vspos = lerp(a.VSPos, b.VSPos, t);
    vsnrm = normalize(lerp(a.VSNrm, b.VSNrm, t));
    float4 vp4 = float4(vspos, 1.0f);
    cpos = mul(vp4, gProj);
    cpos.z -= depthBias * cpos.w;   // 距離不変: NDC-Z を一定量 camera 側へ
    colR = lerp(a.ColR, b.ColR, t);
    colT = lerp(a.ColT, b.ColT, t);
}

// 頂点 VSPos を投影した NDC 座標。
float2 vs_to_ndc(float3 vspos)
{
    float4 cp = mul(float4(vspos, 1.0f), gProj);
    return cp.xy / max(abs(cp.w), 1e-6f);
}

// v0.1.15b: クロス点の perp（太さ方向）= 法線を NDC に投影した方向。
// 共有クロス点では隣接三角形も同じ補間 VSPos/VSNrm になるため、両側で完全に同じ perp に
// なり、quad の外側コーナーが完全一致（miter joint 相当）→ パイスライス隙間/破線が出ない。
// 法線は正の側＝視差的に外側を向くので、別途「外向きに揃える」判定は不要。
float2 perp_from_normal(float3 vspos, float3 vsnrm)
{
    // 視点距離に比例した小さいステップで前方差分（投影の線形領域内）。
    float eps = 0.001f * max(abs(vspos.z), 1.0f);
    float2 n0 = vs_to_ndc(vspos);
    float2 n1 = vs_to_ndc(vspos + vsnrm * eps);
    float2 d = n1 - n0;
    float l = length(d);
    if (l < 1e-6f) return float2(1.0f, 0.0f);
    return d / l;
}

[maxvertexcount(8)]
void GShader(triangle V2G tri[3], inout TriangleStream<G2P> stream)
{
    float f0 = facing_dot(tri[0].VSPos, tri[0].VSNrm);
    float f1 = facing_dot(tri[1].VSPos, tri[1].VSNrm);
    float f2 = facing_dot(tri[2].VSPos, tri[2].VSNrm);

    // v0.1.33: 時間的安定性。min(|f0|,|f1|,|f2|) が閾値以下の弱い triangle は
    // camera 微動で sign が flipしてチラつきの原因になるためスキップ。
    if (stability > 0.001f) {
        float minMag = min(min(abs(f0), abs(f1)), abs(f2));
        if (minMag < stability * 0.02f) return;
    }

    // どのエッジがシルエットを横切るか(符号混在)
    bool c01 = (f0 * f1) < 0.0f;
    bool c12 = (f1 * f2) < 0.0f;
    bool c20 = (f2 * f0) < 0.0f;
    int ncross = (c01 ? 1 : 0) + (c12 ? 1 : 0) + (c20 ? 1 : 0);
    if (ncross != 2) return;  // 全頂点同符号 → シルエット外

    // 横断する 2 エッジ上で facing=0 となる点を 2 つ求める。VSPos/VSNrm も返す（perp 用）。
    float4 p1, p2;
    float cr1, cr2;
    float3 vsp1, vsp2, vsn1, vsn2, ct1, ct2;
    if (c01 && c12) {
        edge_crossing(tri[0], tri[1], f0, f1, p1, cr1, vsp1, vsn1, ct1);
        edge_crossing(tri[1], tri[2], f1, f2, p2, cr2, vsp2, vsn2, ct2);
    } else if (c12 && c20) {
        edge_crossing(tri[1], tri[2], f1, f2, p1, cr1, vsp1, vsn1, ct1);
        edge_crossing(tri[2], tri[0], f2, f0, p2, cr2, vsp2, vsn2, ct2);
    } else {
        edge_crossing(tri[0], tri[1], f0, f1, p1, cr1, vsp1, vsn1, ct1);
        edge_crossing(tri[2], tri[0], f2, f0, p2, cr2, vsp2, vsn2, ct2);
    }

    // v0.1.15b: p.w の符号は保つ（abs で潰すと視点裏のクロスで quad が flip）。
    //   near_eps 未満（ニアプレーン手前・カメラ背後）は丸ごと捨てる。
    if (p1.w < 1e-3f || p2.w < 1e-3f) return;
    float w1 = p1.w;
    float w2 = p2.w;
    float2 ndc1 = p1.xy / w1;
    float2 ndc2 = p2.xy / w2;

    // v0.1.15b: perp は補間 VSNrm の NDC 射影方向。共有クロス点では隣接三角形も同じ
    // 補間値になる = 両側で perp が完全一致 → quad 外側コーナーがピクセル一致で繋がる
    // → 「破線」化が消える（真の miter joint 相当）。法線は正の側＝外側なので、
    // 旧 back_avg 判定は不要。
    float2 perp1 = perp_from_normal(vsp1, vsn1);
    float2 perp2 = perp_from_normal(vsp2, vsn2);

    float2 px = float2(2.0f / max(gScreen.x, 1.0f), 2.0f / max(gScreen.y, 1.0f));

    // v0.1.15: 太さ長手ゆらぎ (waviness)。両端点でハッシュ→線が「震える」ように太さが変わる。
    float phase1 = hash12(ndc1 * brushNoiseScale * 100.0f);
    float phase2 = hash12(ndc2 * brushNoiseScale * 100.0f);
    float wav1 = 1.0f + brushWaviness * (phase1 - 0.5f) * 2.0f;
    float wav2 = 1.0f + brushWaviness * (phase2 - 0.5f) * 2.0f;

    // v0.1.49 入り抜き: 輪郭の端（輪郭が視線の方向に沿って縮む所）ほど細くする。
    // 輪郭の途切れる点では輪郭線が視線と平行になる（なす角の sin → 0）性質を使う局所判定。
    // v0.1.51: s は三角形ごとの値で、ふつうの輪郭でも 0.6〜0.95 くらいでばらつく（傾けたトーラスの外周で
    // 実測）。旧 s^4 はそのばらつきを外周の太さの起伏にしてしまった。
    // 輪郭が途切れる点（カスプ）は鞍形（ガウス曲率が負）の所にしか起きない（Koenderink）ので、
    // 三角形が鞍形のときだけ細くする。凸の所（頭や体の外周など）の輪郭は s がばらついても元の太さのまま。
    // 鞍形の判定: 頂点法線の三角形と位置の三角形の向きが逆か（大きさで割った値＝縮尺・密度によらない）。
    // 実測: 外周（凸）は最小 -0.53（ほぼ平らな所のぶれ）、内側の輪郭（鞍）は -0.90 以下。
    float taperK = 1.0f;
    if (brushTaper > 0.001f) {
        float3 seg = vsp2 - vsp1;
        float segLen2 = dot(seg, seg);
        float3 ta = normalize(tri[0].VSNrm);
        float3 m1 = normalize(tri[1].VSNrm) - ta;
        float3 m2 = normalize(tri[2].VSNrm) - ta;
        float3 fc = cross(tri[1].VSPos - tri[0].VSPos, tri[2].VSPos - tri[0].VSPos);
        float fl = length(fc);
        float mm = length(m1) * length(m2);
        float kd = (fl > 1e-12f && mm > 1e-12f) ? dot(cross(m1, m2), fc / fl) / mm : 1.0f;
        float saddle = smoothstep(0.6f, 0.85f, -kd);
        if (segLen2 > 1e-12f && saddle > 0.0f) {
            float3 vdir = normalize(vsp1 + vsp2);
            float along = dot(seg, vdir);
            float s = sqrt(saturate(1.0f - along * along / segLen2));
            // 強さで細り始める所を決める（100 で s<0.85 から＝端の手前から長めに、50 で s<0.575）。
            // 1 に近い所まで広げると、首のような鞍形の途切れない輪郭で s のばらつきが起伏に出るので 0.85 まで。
            float hi = 0.3f + 0.55f * saturate(brushTaper);
            taperK = lerp(1.0f, smoothstep(0.0f, hi, s), saddle);
        }
    }

    // v0.1.15: 背面法 (inverted hull) と等価な均一太さ。
    //   - 距離 attenuation なし（頂点ごとの w で太さがばらつく主因だった）
    //   - 完全に外向きの quad（内側 0、外側 thickness）: 1 本の線＝ thickness ピクセル
    //   - 線幅 = thickness * cr * wav * taper ピクセル（cr=曲率、wav=ブラシゆらぎ）
    // perp は NDC 単位ベクトル、px = 2/screen。off を w 乗算後に除算すると
    // ピクセル距離が perp * thickness とちょうど一致する（長さ厳密に = thickness px）。
    float tw1 = thickness * cr1 * wav1 * taperK;
    float tw2 = thickness * cr2 * wav2 * taperK;
    float2 off1 = perp1 * tw1 * px;
    float2 off2 = perp2 * tw2 * px;

    // v0.1.49 よれ: 線全体を外側へなめらかにずらす。ノイズは交点の画面位置で決まるので、
    // 隣の三角形と共有する交点では同じずれ量になり、継ぎ目で切れない。
    float2 sp1 = ndc1 * gScreen * 0.5f;
    float2 sp2 = ndc2 * gScreen * 0.5f;
    float cell = 400.0f / max(brushNoiseScale, 1.0f);    // 細かさ 20 → 20px 周期
    float wb1 = 0.0f;
    float wb2 = 0.0f;
    if (brushWobble > 0.001f) {
        wb1 = brushWobble * thickness * 1.5f * vnoise(sp1 / cell);
        wb2 = brushWobble * thickness * 1.5f * vnoise(sp2 / cell);
    }
    float2 b1 = ndc1 + perp1 * wb1 * px;
    float2 b2 = ndc2 + perp2 * wb2 * px;

    // v0.1.38: 隠れ線の破線用に quad の長手ピクセル長を算出し全頂点に乗せる
    float lenPx = length((ndc2 - ndc1) * gScreen * 0.5f);

    // triangle strip で quad を出力。UV: U=長手(0..1), V=太さ方向(0 内側=silhouette..1 外側)
    G2P v; v.LenPx = lenPx;
    v.Trace = ct1;
    v.HPos = float4(b1          * w1, p1.z, w1); v.UV = float2(0.0f, 0.0f); stream.Append(v);
    v.HPos = float4((b1 + off1) * w1, p1.z, w1); v.UV = float2(0.0f, 1.0f); stream.Append(v);
    v.Trace = ct2;
    v.HPos = float4(b2          * w2, p2.z, w2); v.UV = float2(1.0f, 0.0f); stream.Append(v);
    v.HPos = float4((b2 + off2) * w2, p2.z, w2); v.UV = float2(1.0f, 1.0f); stream.Append(v);

    // v0.1.49 二重線: 少し外側に細い 2 本目を引く（スケッチ風）
    if (brushDouble > 0.001f) {
        stream.RestartStrip();
        float g1 = tw1 * (0.8f + 1.5f * brushDouble) + thickness * 0.8f * brushDouble * vnoise(sp1 / cell + 7.31f);
        float g2 = tw2 * (0.8f + 1.5f * brushDouble) + thickness * 0.8f * brushDouble * vnoise(sp2 / cell + 7.31f);
        float2 c1 = b1 + perp1 * g1 * px;
        float2 c2 = b2 + perp2 * g2 * px;
        float2 o1 = perp1 * (tw1 * 0.6f) * px;
        float2 o2 = perp2 * (tw2 * 0.6f) * px;
        v.Trace = ct1;
        v.HPos = float4(c1        * w1, p1.z, w1); v.UV = float2(0.0f, 0.0f); stream.Append(v);
        v.HPos = float4((c1 + o1) * w1, p1.z, w1); v.UV = float2(0.0f, 1.0f); stream.Append(v);
        v.Trace = ct2;
        v.HPos = float4(c2        * w2, p2.z, w2); v.UV = float2(1.0f, 0.0f); stream.Append(v);
        v.HPos = float4((c2 + o2) * w2, p2.z, w2); v.UV = float2(1.0f, 1.0f); stream.Append(v);
    }
}

// ブラシの画素側。UV.y: 0 = シルエット側(内), 1 = 外側。
void brush_discard(G2P i)
{
    // ペンシル風縁のカサつき（縁ほどランダムに欠ける）
    if (brushRoughness > 0.001f) {
        float edge = abs(i.UV.y - 0.5f) * 2.0f;  // 0=中心, 1=縁
        float rough = brushRoughness * edge * edge;
        float h = hash12(i.HPos.xy * 0.37f + i.UV * 11.0f);
        if (h < rough) discard;
    }
    // v0.1.49 かすれ: なめらかなノイズで線の中が途切れる（縁ほど欠けやすい）
    if (brushDry > 0.001f) {
        float cellD = 160.0f / max(brushNoiseScale, 1.0f);
        float n = 0.65f * vnoise(i.HPos.xy / cellD) + 0.35f * vnoise(i.HPos.xy / (cellD * 0.35f) + 11.7f);
        float edgeD = abs(i.UV.y - 0.5f) * 2.0f;
        // 100 でも線の形が残る強さ（中心は 3 割、縁は 6 割強まで欠ける）
        if (n < saturate(brushDry) * (0.3f + 0.35f * edgeD)) discard;
    }
}
"""

# 通常描画（dx11Shader に割り当てる本体）。v0.1.46 で旧「何にでも透ける隠れ線」pass は撤去し、
# 隠れ線は MRenderOverride 側の専用パス（_SCRN_FX_HLSL_HIDDEN）だけで描く。
_SCRN_FX_HLSL_MAIN_TAIL = """
float4 PShader(G2P i) : SV_Target
{
    if (showVisible == 0) discard;
    brush_discard(i);
    float3 col = lineColor;
    if (traceAmount > 0.001f)
        col = lerp(lineColor, og_trace_color(i.Trace), saturate(traceAmount));   // v0.1.53 色トレス
    return float4(col, 1.0f);
}

DepthStencilState DSS_Visible { DepthEnable = TRUE; DepthFunc = LESS_EQUAL; DepthWriteMask = ALL; };

technique11 Main
{
    pass p0
    {
        SetVertexShader(CompileShader(vs_5_0, VShader()));
        SetGeometryShader(CompileShader(gs_5_0, GShader()));
        SetPixelShader(CompileShader(ps_5_0, PShader()));
        SetDepthStencilState(DSS_Visible, 0);
    }
}
"""

# v0.1.46: 紐づけた物（例: 前髪）の裏に回った部分だけを描く隠れ線パス。MRenderOverride の
# MSceneRender.shaderOverride で線のシェルに当てる。深度テストは使わず、PS で
#   紐づけ先だけの深度(gOccDepth) と 全シーン深度(gSceneDepth) を見て
#   「線が紐づけ先より奥」かつ「紐づけ先が一番手前」の画素だけ描く。
# shaderOverride では色セットの頂点ストリームが来る保証がないので COLOR0 は使わない。
_SCRN_FX_HLSL_HIDDEN_TAIL = """
Texture2D gOccDepth;
Texture2D gSceneDepth;
float gNear = 0.1f;
float gFar  = 10000.0f;
float gOrtho = 0.0f;           // v0.1.53: 1 = 正投影カメラ
float gOrthoScale = 10.0f;     // 正投影のとき深さの差を比べる基準の長さ（正投影の幅）
int   hiddenMode = 1;          // 1=破線 2=実線
float hiddenTol  = 0.002f;     // 相対深度の許容差

float og_lin(float z)
{
    if (gOrtho > 0.5f) return gNear + z * (gFar - gNear);   // 正投影の深度は線形
    float denom = gFar - z * (gFar - gNear);
    return (gNear * gFar) / max(denom, 1e-6f);
}

// 許容差の基準。透視はカメラからの距離、正投影は距離に意味がない（カメラが 1000 離れていると
// 2 単位以内が同じ深さ扱いになり隠れ線が消えた）ので正投影の幅を使う。
float og_scale(float z) { return (gOrtho > 0.5f) ? gOrthoScale : z; }

float4 PShaderOcc(G2P i) : SV_Target
{
    int3 px = int3(int(i.HPos.x), int(i.HPos.y), 0);
    float ro = gOccDepth.Load(px).r;
    if (ro >= 0.99999f) discard;                     // 紐づけ先が無い画素
    float zl = og_lin(i.HPos.z);
    float zo = og_lin(ro);
    if (zl - zo <= og_scale(zo) * hiddenTol) discard;   // 線が紐づけ先より手前（通常の線が描く）
    float zs = og_lin(gSceneDepth.Load(px).r);
    if (zo - zs > og_scale(zo) * hiddenTol) discard;    // 紐づけ先より手前に別の物がある
    brush_discard(i);
    if (hiddenMode == 1) {
        float dashPosPx = i.UV.x * i.LenPx;
        float period = max(hiddenDashLen, 1.0f);
        float phase = frac(dashPosPx / period);
        if (phase > hiddenDashFill) discard;
    }
    return float4(lineColor, clamp(hiddenAlpha, 0.0f, 1.0f));
}

DepthStencilState DSS_Off { DepthEnable = FALSE; DepthWriteMask = ZERO; };
BlendState BS_Alpha {
    BlendEnable[0] = TRUE;
    SrcBlend[0] = SRC_ALPHA;
    DestBlend[0] = INV_SRC_ALPHA;
    SrcBlendAlpha[0] = ONE;
    DestBlendAlpha[0] = INV_SRC_ALPHA;
    BlendOp[0] = ADD;
    BlendOpAlpha[0] = ADD;
    RenderTargetWriteMask[0] = 0x0F;
};

technique11 Main
{
    pass p0
    {
        SetVertexShader(CompileShader(vs_5_0, VShader()));
        SetGeometryShader(CompileShader(gs_5_0, GShader()));
        SetPixelShader(CompileShader(ps_5_0, PShaderOcc()));
        SetDepthStencilState(DSS_Off, 0);
        SetBlendState(BS_Alpha, float4(0, 0, 0, 0), 0xFFFFFFFF);
    }
}
"""

# v0.1.50: エッジ線・クリース線（チューブ）の隠れ線パス。チューブは普通のメッシュなので GS は不要。
# 判定は SCRN の隠れ線と同じ（線が紐づけ先より奥 かつ 紐づけ先がその画素で一番手前）。
# 破線: 線の中心線（折れ線・ワールド）を Python が毎フレーム配列で渡し、PS で画素のワールド位置に一番近い
# 区間へ投影して「線に沿った長さ」を出す → 線の中心の深さでの「1 単位あたりの px」を掛けて px 位置にする。
# ※ チューブの UV の V は使わない: 角の輪が斜めに切られているので長い面の 2 つの三角形で V の向きがずれて
#   破線がジグザグになり、さらに V はエッジ 1 本ごとに同じだけ進むので長いエッジで破線が伸びる。
#   画面上の V の変化で割る方式は三角形ごとのずれが積み重なって市松模様になった。
_TUBE_AXIS_MAX = 128
_TUBE_HIDDEN_FX = """// OG Toonline Manager - tube (edge/crease) line hidden pass (MRenderOverride shaderOverride)
#define OG_AXIS_MAX 128
float4x4 gWVP : WorldViewProjection;
float4x4 gWorld : World;
float4x4 gView : View;
float4x4 gProj : Projection;
float2   gScreen : ViewportPixelSize;
float  tubeLength = 1.0f;                     // 線の長さ（ワールド単位）。中心線が無いときの予備
float3 tubeCenter = float3(0.0f, 0.0f, 0.0f); // 線の中心（ワールド）。px 換算はこの深さ 1 点で決める
int    gAxisCount = 0;                        // 中心線の点の数（0/1 = 中心線なし → UV の V で代用）
float  gAxX[OG_AXIS_MAX];
float  gAxY[OG_AXIS_MAX];
float  gAxZ[OG_AXIS_MAX];
Texture2D gOccDepth;
Texture2D gSceneDepth;
float gNear = 0.1f;
float gFar  = 10000.0f;
float gOrtho = 0.0f;           // v0.1.53: 1 = 正投影カメラ
float gOrthoScale = 10.0f;     // 正投影のとき深さの差を比べる基準の長さ（正投影の幅）
int   hiddenMode = 1;
float hiddenTol  = 0.002f;
float3 lineColor = {0.0f, 0.0f, 0.0f};
float hiddenAlpha = 0.6f;
float hiddenDashLen = 10.0f;
float hiddenDashFill = 0.55f;

struct VIN  { float3 Pos : POSITION; float2 UV : TEXCOORD0; };
struct VOUT { float4 HPos : SV_Position; float2 UV : TEXCOORD0; float PxPerUnit : TEXCOORD1;
              float3 WPos : TEXCOORD2; };

VOUT VS(VIN i)
{
    VOUT o;
    o.HPos = mul(float4(i.Pos, 1.0f), gWVP);
    o.UV = i.UV;
    o.WPos = mul(float4(i.Pos, 1.0f), gWorld).xyz;
    // 1 単位が何 px か。頂点ごとの深さで出すとチューブの手前側と縁で値がずれ、それが線の長さぶん
    // 積み重なって破線が「く」の字に曲がる。線の中心の深さ 1 点で決める（＝破線はワールドで等間隔）。
    float vz = max(abs(mul(float4(tubeCenter, 1.0f), gView).z), 1e-4f);
    // v0.1.53: 正投影は距離で大きさが変わらない（距離で割ると 1000 離れたカメラで破線が伸びきる）
    o.PxPerUnit = 0.5f * gScreen.y * gProj[1][1] / ((gOrtho > 0.5f) ? 1.0f : vz);
    return o;
}

float og_lin(float z)
{
    if (gOrtho > 0.5f) return gNear + z * (gFar - gNear);   // 正投影の深度は線形
    float denom = gFar - z * (gFar - gNear);
    return (gNear * gFar) / max(denom, 1e-6f);
}

float og_scale(float z) { return (gOrtho > 0.5f) ? gOrthoScale : z; }

// 画素のワールド位置を中心線（折れ線）の一番近い区間へ投影し、線の始点からの長さを返す
float og_axis_len(float3 p, float vFallback)
{
    int n = min(gAxisCount, OG_AXIS_MAX);
    if (n < 2) return vFallback * tubeLength;
    float best = 3.4e38f;
    float bestS = 0.0f;
    float acc = 0.0f;
    float3 a = float3(gAxX[0], gAxY[0], gAxZ[0]);
    [loop]
    for (int k = 1; k < n; ++k) {
        float3 b = float3(gAxX[k], gAxY[k], gAxZ[k]);
        float3 ab = b - a;
        float l2 = dot(ab, ab);
        float t = (l2 > 1e-12f) ? saturate(dot(p - a, ab) / l2) : 0.0f;
        float3 d = p - (a + ab * t);
        float dd = dot(d, d);
        float l = sqrt(l2);
        if (dd < best) { best = dd; bestS = acc + t * l; }
        acc += l;
        a = b;
    }
    return bestS;
}

float4 PS(VOUT i) : SV_Target
{
    int3 px = int3(int(i.HPos.x), int(i.HPos.y), 0);
    float ro = gOccDepth.Load(px).r;
    if (ro >= 0.99999f) discard;
    float zl = og_lin(i.HPos.z);
    float zo = og_lin(ro);
    if (zl - zo <= og_scale(zo) * hiddenTol) discard;
    float zs = og_lin(gSceneDepth.Load(px).r);
    if (zo - zs > og_scale(zo) * hiddenTol) discard;
    if (hiddenMode == 1) {
        float posPx = og_axis_len(i.WPos, i.UV.y) * i.PxPerUnit;
        float phase = frac(posPx / max(hiddenDashLen, 1.0f));
        if (phase > hiddenDashFill) discard;
    }
    return float4(lineColor, saturate(hiddenAlpha));
}

DepthStencilState DSS_Off { DepthEnable = FALSE; DepthWriteMask = ZERO; };
RasterizerState RS_Back { CullMode = BACK; };
BlendState BS_Alpha {
    BlendEnable[0] = TRUE;
    SrcBlend[0] = SRC_ALPHA;
    DestBlend[0] = INV_SRC_ALPHA;
    SrcBlendAlpha[0] = ONE;
    DestBlendAlpha[0] = INV_SRC_ALPHA;
    BlendOp[0] = ADD;
    BlendOpAlpha[0] = ADD;
    RenderTargetWriteMask[0] = 0x0F;
};

technique11 Main
{
    pass p0
    {
        SetVertexShader(CompileShader(vs_5_0, VS()));
        SetGeometryShader(NULL);
        SetPixelShader(CompileShader(ps_5_0, PS()));
        SetDepthStencilState(DSS_Off, 0);
        SetRasterizerState(RS_Back);
        SetBlendState(BS_Alpha, float4(0, 0, 0, 0), 0xFFFFFFFF);
    }
}
"""

_SCRN_FX_HLSL = _SCRN_FX_HLSL_COMMON + _SCRN_FX_HLSL_MAIN_TAIL
_SCRN_FX_HLSL_HIDDEN = "#define OG_NO_COLOR0 1\n" + _SCRN_FX_HLSL_COMMON + _SCRN_FX_HLSL_HIDDEN_TAIL


_SCRN_FX_OGSFX = """#version 410
// OG Toonline Manager - Geometry-shader silhouette outline (glslShader / OGSFX / GLSL for OpenGL VP2)
// HLSL 版 (_SCRN_FX_HLSL) と同じアルゴリズム: VS でビュー空間 pos/法線計算 → GS でシルエット
// 三角形を検出し横断点 2 つを結ぶ細い quad をスクリーン空間で押し出す（v0.1.37 で GLSL 版追加）。

uniform mat4 gWVP : WorldViewProjection;
uniform mat4 gWV  : WorldView;
uniform mat4 gProj : Projection;
uniform vec2 gScreen : ViewportPixelSize;

uniform float thickness
<
    string UIName = "Thickness(px)";
> = 3.0;

uniform float depthBias
<
    string UIName = "Overlap Depth Bias (view ratio)";
> = 0.02;

uniform float stability
<
    string UIName = "Temporal Stability";
> = 0.0;

uniform vec3 lineColor
<
    string UIName = "Line Color";
    string UIWidget = "ColorPicker";
> = { 0.0, 0.0, 0.0 };

uniform float brushRoughness
<
    string UIName = "Brush Roughness (edge noise)";
> = 0.0;

uniform float brushWaviness
<
    string UIName = "Brush Waviness (thickness noise)";
> = 0.0;

uniform float brushNoiseScale
<
    string UIName = "Brush Noise Scale (px freq)";
> = 20.0;

uniform int showVisible
<
    string UIName = "Show Visible Line";
> = 1;

attribute VS_INPUT
{
    vec3 Pos : POSITION;
    vec3 Nrm : NORMAL;
    vec4 Color : COLOR0;
};

attribute VS_TO_GS
{
    vec3 vspos;
    vec3 vsnrm;
    float colR;
};

attribute GS_TO_PS
{
    vec2 uv;
};

attribute PS_OUTPUT
{
    vec4 outColor : COLOR0;
};

GLSLShader ShaderVertex
{
    void main()
    {
        vec4 vp = gWV * vec4(Pos, 1.0);
        VS_OUT.vspos = vp.xyz;
        VS_OUT.vsnrm = mat3(gWV) * Nrm;
        gl_Position = gProj * vp;
        float w = Color.r;
        VS_OUT.colR = (w < 0.001) ? 1.0 : w;
    }
}

GLSLShader ShaderGeometry
{
    layout(triangles) in;
    layout(triangle_strip, max_vertices = 4) out;

    float ogHash12(vec2 p)
    {
        vec3 p3 = fract(vec3(p.xyx) * 0.1031);
        p3 += dot(p3, p3.yzx + 33.33);
        return fract((p3.x + p3.y) * p3.z);
    }

    float facingDot(vec3 vspos, vec3 vsnrm)
    {
        vec3 n = normalize(vsnrm);
        float len = length(vspos);
        if (len < 1e-6) return 1.0;
        vec3 v = vspos / len;
        return -dot(n, v);
    }

    vec2 vsToNdc(vec3 vspos)
    {
        vec4 cp = gProj * vec4(vspos, 1.0);
        return cp.xy / max(abs(cp.w), 1e-6);
    }

    vec2 perpFromNormal(vec3 vspos, vec3 vsnrm)
    {
        float eps = 0.001 * max(abs(vspos.z), 1.0);
        vec2 n0 = vsToNdc(vspos);
        vec2 n1 = vsToNdc(vspos + vsnrm * eps);
        vec2 d = n1 - n0;
        float l = length(d);
        if (l < 1e-6) return vec2(1.0, 0.0);
        return d / l;
    }

    void edgeCrossing(int ia, int ib, float fa, float fb,
                      out vec4 cpos, out float colR,
                      out vec3 vspos, out vec3 vsnrm)
    {
        float t = fa / (fa - fb);
        vspos = mix(GS_IN[ia].vspos, GS_IN[ib].vspos, t);
        vsnrm = normalize(mix(GS_IN[ia].vsnrm, GS_IN[ib].vsnrm, t));
        cpos = gProj * vec4(vspos, 1.0);
        cpos.z -= depthBias * cpos.w;
        colR = mix(GS_IN[ia].colR, GS_IN[ib].colR, t);
    }

    void emitVert(vec2 ndc, float z, float w, vec2 uv)
    {
        gl_Position = vec4(ndc * w, z, w);
        GS_OUT.uv = uv;
        EmitVertex();
    }

    void main()
    {
        float f0 = facingDot(GS_IN[0].vspos, GS_IN[0].vsnrm);
        float f1 = facingDot(GS_IN[1].vspos, GS_IN[1].vsnrm);
        float f2 = facingDot(GS_IN[2].vspos, GS_IN[2].vsnrm);

        if (stability > 0.001) {
            float minMag = min(min(abs(f0), abs(f1)), abs(f2));
            if (minMag < stability * 0.02) return;
        }

        bool c01 = (f0 * f1) < 0.0;
        bool c12 = (f1 * f2) < 0.0;
        bool c20 = (f2 * f0) < 0.0;
        int ncross = (c01 ? 1 : 0) + (c12 ? 1 : 0) + (c20 ? 1 : 0);
        if (ncross != 2) return;

        vec4 p1, p2;
        float cr1, cr2;
        vec3 vsp1, vsp2, vsn1, vsn2;
        if (c01 && c12) {
            edgeCrossing(0, 1, f0, f1, p1, cr1, vsp1, vsn1);
            edgeCrossing(1, 2, f1, f2, p2, cr2, vsp2, vsn2);
        } else if (c12 && c20) {
            edgeCrossing(1, 2, f1, f2, p1, cr1, vsp1, vsn1);
            edgeCrossing(2, 0, f2, f0, p2, cr2, vsp2, vsn2);
        } else {
            edgeCrossing(0, 1, f0, f1, p1, cr1, vsp1, vsn1);
            edgeCrossing(2, 0, f2, f0, p2, cr2, vsp2, vsn2);
        }

        if (p1.w < 1e-3 || p2.w < 1e-3) return;
        float w1 = p1.w;
        float w2 = p2.w;
        vec2 ndc1 = p1.xy / w1;
        vec2 ndc2 = p2.xy / w2;

        vec2 perp1 = perpFromNormal(vsp1, vsn1);
        vec2 perp2 = perpFromNormal(vsp2, vsn2);

        vec2 px = vec2(2.0 / max(gScreen.x, 1.0), 2.0 / max(gScreen.y, 1.0));

        float phase1 = ogHash12(ndc1 * brushNoiseScale * 100.0);
        float phase2 = ogHash12(ndc2 * brushNoiseScale * 100.0);
        float wav1 = 1.0 + brushWaviness * (phase1 - 0.5) * 2.0;
        float wav2 = 1.0 + brushWaviness * (phase2 - 0.5) * 2.0;

        vec2 off1 = perp1 * (thickness * cr1 * wav1) * px;
        vec2 off2 = perp2 * (thickness * cr2 * wav2) * px;

        emitVert(ndc1,        p1.z, w1, vec2(0.0, 0.0));
        emitVert(ndc1 + off1, p1.z, w1, vec2(0.0, 1.0));
        emitVert(ndc2,        p2.z, w2, vec2(1.0, 0.0));
        emitVert(ndc2 + off2, p2.z, w2, vec2(1.0, 1.0));
        EndPrimitive();
    }
}

GLSLShader ShaderPixel
{
    float ogHash12(vec2 p)
    {
        vec3 p3 = fract(vec3(p.xyx) * 0.1031);
        p3 += dot(p3, p3.yzx + 33.33);
        return fract((p3.x + p3.y) * p3.z);
    }
    void main()
    {
        if (showVisible == 0) discard;
        if (brushRoughness > 0.001) {
            float edge = abs(PS_IN.uv.y - 0.5) * 2.0;
            float rough = brushRoughness * edge * edge;
            float h = ogHash12(gl_FragCoord.xy * 0.37 + PS_IN.uv * 11.0);
            if (h < rough) discard;
        }
        outColor = vec4(lineColor, 1.0);
    }
}

technique Main
{
    pass P0
    <
        string drawContext = "colorPass";
    >
    {
        VertexShader   (in VS_INPUT,  out VS_TO_GS VS_OUT) = ShaderVertex;
        GeometryShader (in VS_TO_GS GS_IN, out GS_TO_PS GS_OUT) = ShaderGeometry;
        PixelShader    (in GS_TO_PS PS_IN, out PS_OUTPUT) = ShaderPixel;
    }
}
"""


_FX_WRITTEN = {}   # このセッションで書き出し済みのシェーダファイル {path: True}


def _write_fx_file(fname, src):
    """userAppDir/OG_Toonline_Manager/<fname> へシェーダを書き出してパスを返す（1 セッション 1 回）。
    v0.1.38: 日本語コメントが cp932 で書き出されるとパーサが文字化けするため UTF-8 固定。"""
    d = os.path.join(cmds.internalVar(userAppDir=True), "OG_Toonline_Manager")
    try:
        if not os.path.isdir(d):
            os.makedirs(d)
    except Exception:
        d = cmds.internalVar(userTmpDir=True)
    path = os.path.join(d, fname).replace("\\", "/")
    if _FX_WRITTEN.get(path) and os.path.exists(path):
        return path
    import tempfile
    try:
        fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(fname)[1], prefix="OG_fx_", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(src)
            os.replace(tmp, path)
        except Exception:
            try: os.remove(tmp)
            except Exception: pass
            raise
    except Exception:
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(src)
        except Exception:
            pass
    _FX_WRITTEN[path] = True
    return path


def _fx_ver_tag():
    return __version__.replace(".", "_")


def _screen_fx_path():
    """SCRN 用シェーダのパス。drawAPI に応じて HLSL(.fx) か OGSFX(.ogsfx) を書き分ける（v0.1.37）。
    v0.1.46: ファイル名に版を入れる。dx11Shader/MShaderManager はパス単位でコンパイル結果を
    使い回すため、同じパスで中身だけ変えても新しい版が反映されない。既存ラインは
    _upgrade_scrn_shader で新しいパスへ差し替える。"""
    if _vp2_is_opengl():
        return _write_fx_file("OG_ToonScreen_v" + _fx_ver_tag() + ".ogsfx", _SCRN_FX_OGSFX)
    return _write_fx_file("OG_ToonScreen_v" + _fx_ver_tag() + ".fx", _SCRN_FX_HLSL)


def _scrn_hidden_fx_path():
    """v0.1.46: 紐づけ先の裏だけに描く隠れ線パス用シェーダ（DX11 のみ）。"""
    return _write_fx_file("OG_ScrnHidden_v" + _fx_ver_tag() + ".fx", _SCRN_FX_HLSL_HIDDEN)


def _tube_hidden_fx_path():
    """v0.1.50: エッジ線・クリース線（チューブ）の隠れ線パス用シェーダ（DX11 のみ）。"""
    return _write_fx_file("OG_TubeHidden_v" + _fx_ver_tag() + ".fx", _TUBE_HIDDEN_FX)


def _dump_dx11_varying(shd):
    """dx11Shader の頂点入力バインド状態を Script Editor へ全部吐き出す診断ダンパ。
    varyingParameters 系の属性名は Maya のバージョン差が大きいため、attribute schema
    そのものを列挙する。実機で `import OG_Toonline_Manager as m;
    m._dump_dx11_varying('<shader>')` を叩き、この出力を貼ってもらえれば
    バインダを実機のスキーマに合わせて直せる。"""
    if not cmds.objExists(shd):
        print("[dx11 dump] no node:", shd); return
    print("[dx11 dump] node =", shd, "type =", cmds.nodeType(shd))
    # 1) 頂点/セマンティクス関連の可能性がある属性を全部拾って値を出す
    keys = ("vary", "semantic", "source", "vertex", "color", "input",
            "bind", "elem", "buffer", "map", "attr")
    try:
        attrs = cmds.listAttr(shd) or []
    except Exception:
        attrs = []
    hits = sorted({a for a in attrs if any(k in a.lower() for k in keys)})
    print("[dx11 dump] candidate top-level attrs ({0}):".format(len(hits)))
    for a in hits:
        line = "  " + a
        try:
            v = cmds.getAttr(shd + "." + a); line += "  = " + repr(v)
        except Exception:
            try:
                sz = cmds.getAttr(shd + "." + a, size=True)
                line += "  [array size={0}]".format(sz)
            except Exception:
                pass
        print(line)
    # 2) varying/uniform 系 compound の中身を掘る
    for cname in ("varyingParameters", "vary", "varyingParameterList",
                  "uniformParameters", "vertexBufferMapping", "vertexElement"):
        base_full = "{0}.{1}".format(shd, cname)
        try:
            n = cmds.getAttr(base_full, size=True)
        except Exception:
            continue
        print("[dx11 dump] compound '{0}' size={1}".format(cname, n))
        # 実際に populated な index を multi 列挙
        try:
            multi = cmds.listAttr(base_full, multi=True) or []
        except Exception:
            multi = []
        if multi:
            print("  multi listAttr:", multi[:20], ("..." if len(multi) > 20 else ""))
        for i in range(max(n, 1)):
            elem = "{0}[{1}]".format(base_full, i)
            # element 直下の子属性名を listAttr で取れないか
            for flag in ({"string": "*"}, {}):
                try:
                    subs = cmds.listAttr(elem, **flag) or []
                    if subs:
                        print("  [{0}] listAttr({1}) -> {2}".format(i, flag, subs[:30]))
                        break
                except Exception:
                    pass
            # 想定される sub 属性名を総当たり
            sub_candidates = ("varyingParameterName", "vpn", "varyingParameterSemantic",
                              "vps", "varyingParameterSemanticIndex", "vpsi",
                              "varyingParameterType", "vpt", "varyingParameterSource", "vpss",
                              "name", "semantic", "semanticIndex", "type", "source",
                              "sourceCombined", "sourceSet", "setName",
                              "channelIndex", "channelName", "channelSet")
            row = {}
            for s in sub_candidates:
                try:
                    v = cmds.getAttr(elem + "." + s); row[s] = v
                except Exception:
                    pass
            if row:
                print("  [{0}] sub attrs:".format(i))
                for k, v in row.items():
                    print("    .{0} = {1!r}".format(k, v))
    # 3) dx11Shader コマンドが query フラグを持っていれば列挙
    for flag in ("listVaryingParameters", "listAvailableSemantics", "listUniformParameters"):
        try:
            r = cmds.dx11Shader(shd, query=True, **{flag: True})
            print("[dx11 dump] dx11Shader -q -{0} -> {1!r}".format(flag, r))
        except Exception:
            pass
    print("[dx11 dump] done")


def _bind_scrn_color_set_to_shader(shd, color_set_name):
    """dx11Shader の COLOR0 varying param を Maya の色セット(color_set_name)へバインドする。

    Maya 2024 の dx11Shader スキーマ（実機で確定）:
      - `<node>.varyingParameters` は文字列配列。例: ['Position','Normal','Color0']
      - 各エントリ名につき top-level 属性 `<Name>_Source` (string) を持ち、値は
        descriptor 文字列。例: 'position' / 'normal' / 'color:<colorSetName>'。
      - シェーダ既定は 'color:colorSet' 固定なので、存在しない colorSet を指しっぱなしになり
        IN.Color.r=0 → シェーダ内フォールバックで w=1.0 固定 → 曲率が一定太さに見える。
    ここで <ColorParam>_Source を 'color:<color_set_name>' に上書きすると各頂点の R が流れる。"""
    if not cmds.objExists(shd):
        return False
    # 1) varyingParameters の文字列配列を取る
    try:
        params = cmds.getAttr(shd + ".varyingParameters")
    except Exception:
        params = None
    if not params:
        return False
    if isinstance(params, str):
        params = [params]
    # 2) Color 系のパラメータ名を特定
    #   優先: Color0 完全一致 > _Source が 'color' で始まる > 名前が 'color' で始まる
    color_param = None
    for p in params:
        if str(p) == "Color0":
            color_param = p; break
    if not color_param:
        for p in params:
            try:
                v = cmds.getAttr("{0}.{1}_Source".format(shd, p))
                if isinstance(v, str) and v.startswith("color"):
                    color_param = p; break
            except Exception:
                pass
    if not color_param:
        for p in params:
            if str(p).lower().startswith("color"):
                color_param = p; break
    if not color_param:
        return False
    # 3) 上書き（既にその値なら成功扱いで返す）
    src_attr = "{0}.{1}_Source".format(shd, color_param)
    desired = "color:" + color_set_name
    try:
        current = cmds.getAttr(src_attr)
    except Exception:
        current = None
    if current == desired:
        return True
    try:
        cmds.setAttr(src_attr, desired, type="string")
        return True
    except Exception:
        return False


def _rebind_all_scrn_shaders():
    """既存の全 SCRN ラインの shader を SCRN_CSET(=toonScrnCurv) にリバインドし、
    曲率倍率を再計算して頂点カラーへ反映する。ホットアップデートで v0.1.6 の
    バインダを取り込んだあと、既存の SCRN ラインを削除せずに一発で修復するための入り口。
    実機で: `import OG_Toonline_Manager as m; m._rebind_all_scrn_shaders()`"""
    lines = _all_screen_lines()
    fixed = 0
    misses = []
    for line in lines:
        shd = _fresnel_shader(line)   # SCRN も FRES_LINK を message で共有している
        if not shd:
            misses.append(_short(line) + " (no shader link)"); continue
        if _bind_scrn_color_set_to_shader(shd, SCRN_CSET):
            fixed += 1
            try:
                _update_scrn_curv_weights(line)
            except Exception:
                pass
        else:
            misses.append(_short(line) + " -> " + _short(shd))
    try:
        om2.MGlobal.displayInfo("[SCRN] rebind: {0}/{1} succeeded".format(fixed, len(lines)))
        for m in misses:
            om2.MGlobal.displayWarning("[SCRN] rebind miss: " + m)
    except Exception:
        pass
    return fixed


def _build_screen_network(line, shape, color):
    """スクリーン空間押し出し輪郭シェーダを shape に割り当てる。GS でシルエット三角形を検出し
    横断点を結ぶ細い quad をスクリーン空間で押し出す。VP2 の backend に応じて dx11Shader+HLSL か
    glslShader+OGSFX を使う（v0.1.37）。太さ＝thickness uniform（ピクセル）。"""
    plug, node_type = _hw_shader_plugin()
    try:
        if not cmds.pluginInfo(plug, q=True, loaded=True):
            cmds.loadPlugin(plug, quiet=True)
    except Exception:
        cmds.warning("{0} プラグインを読み込めません。VP2 のバックエンドをご確認ください。".format(plug))
        return None, None
    base = "toonScreen_" + _short(line)
    fx = _screen_fx_path()
    suffix = "_GL" if node_type == "GLSLShader" else "_DX11"
    shd = cmds.shadingNode(node_type, asShader=True, name=base + suffix)
    sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=base + "_SG")
    try:
        cmds.connectAttr(shd + ".outColor", sg + ".surfaceShader", f=True)
    except Exception:
        pass
    try:
        cmds.setAttr(shd + ".shader", fx, type="string")
    except Exception:
        cmds.warning("スクリーン輪郭用シェーダの読み込みに失敗（{0} をご確認ください）".format(plug))
    if cmds.attributeQuery(FRES_COLOR, node=shd, exists=True):
        try:
            cmds.setAttr(shd + "." + FRES_COLOR, color[0], color[1], color[2], type="double3")
        except Exception:
            pass
    # 重なり深度押し込み (★v0.1.9+ は相対値 = ビュー距離の N% で押し込み)。
    # HLSL 側の vpos.z *= (1 + depthBias) に合わせ、距離に比例した相対押し込みになる。
    # メッシュサイズ依存の bbox スケーリングは不要。_SCRN_ZBIAS 既定 0.02 のまま全スケールで動く。
    if cmds.attributeQuery(SCRN_DEPTH_BIAS, node=shd, exists=True):
        try:
            cmds.setAttr(shd + "." + SCRN_DEPTH_BIAS, _SCRN_ZBIAS)
        except Exception:
            pass
    # v0.1.14: ブラシ質感の初期値を shd へ反映。
    for an, v in ((SCRN_BRUSH_ROUGH, _SCRN_BRUSH_ROUGH_DEF),
                  (SCRN_BRUSH_WAVE,  _SCRN_BRUSH_WAVE_DEF),
                  (SCRN_BRUSH_SCALE, _SCRN_BRUSH_SCALE_DEF)):
        if cmds.attributeQuery(an, node=shd, exists=True):
            try: cmds.setAttr(shd + "." + an, v)
            except Exception: pass
    # v0.1.38: 隠れ線（DX11 HLSL のみ、OGSFX には uniform 無し＝attributeQuery で skip）。
    for an, v in ((SCRN_HIDDEN_SHOW,  _SCRN_HIDDEN_DEF),
                  (SCRN_HIDDEN_DASH,  _SCRN_HIDDEN_DASH_DEF),
                  (SCRN_HIDDEN_FILL,  _SCRN_HIDDEN_FILL_DEF),
                  (SCRN_HIDDEN_ALPHA, _SCRN_HIDDEN_ALPHA_DEF)):
        if cmds.attributeQuery(an, node=shd, exists=True):
            try: cmds.setAttr(shd + "." + an, v)
            except Exception: pass
    cmds.sets(shape, e=True, forceElement=sg)
    # ★COLOR0 → SCRN_CSET(頂点カラー) を明示バインド。dx11Shader は既定で頂点色を
    #   自動バインドしないバージョンがあり、この場合 IN.Color.r が 0 のまま届き、
    #   曲率倍率がゼロ扱い→シェーダ側の下限フォールバック(1.0)で常に一定太さになる。
    #   ここで varyingParameters.source を色セット名に上書きすれば毎頂点の R が流れる。
    if _bind_scrn_color_set_to_shader(shd, SCRN_CSET):
        try:
            om2.MGlobal.displayInfo(
                "[SCRN] {0}: COLOR0 → '{1}' にバインドしました".format(_short(shd), SCRN_CSET))
        except Exception:
            pass
    else:
        try:
            om2.MGlobal.displayWarning(
                "[SCRN] {0}: COLOR0 のバインドに失敗（曲率が一定太さになります）。"
                "実機で `import OG_Toonline_Manager as m; m._dump_dx11_varying('{0}')` を"
                "実行し、Script Editor で varyingParameters の中身を確認してください。"
                .format(_short(shd)))
        except Exception:
            pass
    if not cmds.attributeQuery(FRES_LINK, node=line, exists=True):
        cmds.addAttr(line, ln=FRES_LINK, at="message")
    try:
        cmds.connectAttr(shd + ".message", line + "." + FRES_LINK, f=True)
    except Exception:
        pass
    return shd, sg


def _ensure_root():
    if not cmds.objExists(ROOT):
        cmds.group(em=True, name=ROOT)
    return ROOT


CURV_SMOOTH_ITERS = 3   # 曲率の近傍平均スムージング回数（チクチク=トゲ状の輪郭を防ぐ）


def _compute_curvature(shape):
    """各頂点の符号付き曲率を [-1,1] に正規化して返す（凸 > 0 / 凹 < 0）。
    近傍平均との差（ラプラシアン/アンブレラ）を頂点法線へ投影して曲率とする。
    ハードエッジ（立方体など）で曲率が1頂点に集中して輪郭がトゲ状になるのを防ぐため、
    近傍平均で数回スムージングしてから正規化する。"""
    sl = om2.MSelectionList()
    sl.add(shape)
    dag = sl.getDagPath(0)
    mfn = om2.MFnMesh(dag)
    pts = mfn.getPoints(om2.MSpace.kObject)
    nrm = mfn.getVertexNormals(False, om2.MSpace.kObject)
    n = len(pts)
    curv = [0.0] * n
    adj = [()] * n           # 近傍頂点（スムージングで再利用）
    itv = om2.MItMeshVertex(dag)
    while not itv.isDone():
        i = itv.index()
        conn = list(itv.getConnectedVertices())
        adj[i] = conn
        m = len(conn)
        if m > 0:
            ax = ay = az = 0.0
            for c in conn:
                p = pts[c]
                ax += p.x; ay += p.y; az += p.z
            ax /= m; ay /= m; az /= m
            pi = pts[i]
            lx = ax - pi.x; ly = ay - pi.y; lz = az - pi.z
            ni = nrm[i]
            # 凸面では近傍平均が法線の逆側 → 符号を反転して凸を正にする
            curv[i] = -(lx * ni.x + ly * ni.y + lz * ni.z)
        itv.next()
    # 近傍平均によるスムージング（隣接頂点との重みの急変＝トゲを均す）
    for _ in range(CURV_SMOOTH_ITERS):
        sm = list(curv)
        for i in range(n):
            conn = adj[i]
            if conn:
                s = 0.0
                for c in conn:
                    s += curv[c]
                sm[i] = 0.5 * curv[i] + 0.5 * (s / len(conn))
        curv = sm
    mx = 0.0
    for c in curv:
        if abs(c) > mx:
            mx = abs(c)
    if mx > 1e-9:
        curv = [c / mx for c in curv]
    return curv


# ---- ラインコントローラー（アニメーション）まわり ----
def _line_deformer(line):
    nodes = cmds.ls(cmds.listHistory(line) or [], type=THICK_TYPE)
    return nodes[0] if nodes else None


def _fresnel_shader(line):
    """フレネル/スクリーン輪郭ラインの自前シェーダノード（dx11Shader か GLSLShader）。
    maxWidthPx/thickness・lineColor 等の uniform を持つ。VP2 バックエンドで型が変わる（v0.1.37）。"""
    if cmds.objExists(line) and cmds.attributeQuery(FRES_LINK, node=line, exists=True):
        c = cmds.listConnections(line + "." + FRES_LINK, s=True, d=False) or []
        c = [x for x in c if cmds.objExists(x) and cmds.nodeType(x) in ("dx11Shader", "GLSLShader")]
        if c:
            return c[0]
    return None


def _line_src_shape(line):
    """曲率計算用の元メッシュ（deformer のベース入力）を返す。"""
    defm = _line_deformer(line)
    if defm:
        conn = cmds.listConnections(defm + ".input[0].inputGeometry",
                                    s=True, d=False, sh=True) or []
        if conn:
            return conn[0]
    sh = cmds.listRelatives(line, shapes=True, type="mesh", ni=True, f=True)
    return sh[0] if sh else None


def _mesh_vertex_count(node):
    try:
        return int(cmds.polyEvaluate(node, vertex=True))
    except Exception:
        return -1


def _line_curvature(line):
    key = _ukey(line)
    curv = _CURV_CACHE.get(key)
    # v0.1.52: 頂点数が変わっていたら（メッシュを作り直した等）計算し直す
    if curv is not None and len(curv) != _mesh_vertex_count(line):
        curv = None
    if curv is None:
        src = _line_src_shape(line)
        try:
            curv = _compute_curvature(src) if src else []
        except Exception:
            curv = []
        _CURV_CACHE[key] = curv
    return curv


def _line_curve(line):
    """エッジラインのカーブ shape（polyToCurve のカーブ）を返す。無ければ None。"""
    for s in cmds.listRelatives(line, allDescendents=True, type="nurbsCurve", f=True) or []:
        return s
    return None


def _line_circle_node(line):
    """エッジラインの円プロファイル(makeNurbCircle)を履歴から返す。無ければ None。"""
    for n in (cmds.listHistory(line) or []):
        if cmds.nodeType(n) == "makeNurbCircle":
            return n
    return None


def _taper_factors(line):
    """各頂点のライン長手方向パラメータ t(0..1) をキャッシュして返す（カーブ最近接で算出）。"""
    key = _ukey(line)
    f = _TAPER_CACHE.get(key)
    if f is not None and len(f) == _mesh_vertex_count(line):
        return f
    crv = _line_curve(line)
    sh = cmds.listRelatives(line, shapes=True, type="mesh", ni=True, f=True) or []
    ts = []
    if crv and sh:
        try:
            sl = om2.MSelectionList()
            sl.add(sh[0]); sl.add(crv)
            mfn_mesh = om2.MFnMesh(sl.getDagPath(0))
            mfn_crv = om2.MFnNurbsCurve(sl.getDagPath(1))
            pts = mfn_mesh.getPoints(om2.MSpace.kWorld)
            dom = mfn_crv.knotDomain
            tmin, tmax = dom[0], dom[1]
            span = (tmax - tmin) or 1.0
            for p in pts:
                try:
                    _, param = mfn_crv.closestPoint(p, space=om2.MSpace.kWorld)
                except Exception:
                    param = tmin
                ts.append(max(0.0, min(1.0, (param - tmin) / span)))
        except Exception:
            ts = []
    _TAPER_CACHE[key] = ts
    return ts


def _tube_normals_outward(lshape, curve):
    """チューブ poly の頂点法線が外向き（中心カーブから離れる向き）かを多数決で判定。
    extrude の向き次第で法線が内向きになると textureDeformer(direction="Normal") が
    内側へ押し込み、offset が基準半径を超えるとチューブが軸を貫通して反転するため、
    生成直後に外向きかどうか調べて内向きなら反転させる。"""
    try:
        sl = om2.MSelectionList()
        sl.add(lshape); sl.add(curve)
        mfn = om2.MFnMesh(sl.getDagPath(0))
        mcrv = om2.MFnNurbsCurve(sl.getDagPath(1))
        pts = mfn.getPoints(om2.MSpace.kWorld)
        n = len(pts)
        if n == 0:
            return True
        step = max(1, n // 12)
        votes = 0
        count = 0
        for i in range(0, n, step):
            p = pts[i]
            try:
                nrm = mfn.getVertexNormal(i, False, om2.MSpace.kWorld)
                cp, _ = mcrv.closestPoint(p, space=om2.MSpace.kWorld)
            except Exception:
                continue
            ox, oy, oz = p.x - cp.x, p.y - cp.y, p.z - cp.z
            if ox * nrm.x + oy * nrm.y + oz * nrm.z >= 0.0:
                votes += 1
            count += 1
        if count == 0:
            return True
        return votes * 2 >= count
    except Exception:
        return True


def _parse_profile(s):
    """ "x:y,x:y,..." → [(x,y),...]。空/不正なら一様 [(0,1),(1,1)]。"""
    pts = []
    for tok in (s or "").split(","):
        tok = tok.strip()
        if not tok or ":" not in tok:
            continue
        try:
            x, y = tok.split(":")
            pts.append((max(0.0, min(1.0, float(x))), max(0.0, min(2.0, float(y)))))
        except Exception:
            pass
    pts.sort(key=lambda p: p[0])
    if len(pts) < 2:
        return [(0.0, 1.0), (1.0, 1.0)]
    return pts


def _serialize_profile(pts):
    return ",".join("{:.4f}:{:.4f}".format(x, y) for x, y in pts)


def _sample_profile(pts, t):
    """Catmull-Rom スプラインでスムーズ補間（端点は複製してタンジェント代用）。"""
    if not pts:
        return 1.0
    n = len(pts)
    if t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:
        return pts[-1][1]
    for i in range(n - 1):
        x1, y1 = pts[i][0], pts[i][1]
        x2, y2 = pts[i + 1][0], pts[i + 1][1]
        if x1 <= t <= x2:
            if x2 <= x1:
                return y2
            s = (t - x1) / (x2 - x1)
            p0 = pts[i - 1][1] if i - 1 >= 0 else y1
            p3 = pts[i + 2][1] if i + 2 < n else y2
            y = 0.5 * (2 * y1
                       + (-p0 + y2) * s
                       + (2 * p0 - 5 * y1 + 4 * y2 - p3) * s * s
                       + (-p0 + 3 * y1 - 3 * y2 + p3) * s * s * s)
            return max(0.0, min(2.0, y))
    return pts[-1][1]


def _profile_of_ctrl(ctrl):
    if ctrl and cmds.attributeQuery(CTRL_PROFILE, node=ctrl, exists=True):
        try:
            return _parse_profile(cmds.getAttr(ctrl + "." + CTRL_PROFILE))
        except Exception:
            pass
    return [(0.0, 1.0), (1.0, 1.0)]


def _profile_is_flat(pts):
    return all(abs(y - 1.0) < 1e-4 for _, y in pts)


def _attr_key_state(plug):
    """属性プラグのアニメ状態を返す: 'none' / 'anim'（アニメ有・キー上でない）/ 'key'（現フレームがキー）。"""
    try:
        kc = cmds.keyframe(plug, query=True, keyframeCount=True) or 0
    except Exception:
        kc = 0
    if kc <= 0:
        return "none"
    t = cmds.currentTime(query=True)
    try:
        at_t = cmds.keyframe(plug, query=True, time=(t, t)) or []
    except Exception:
        at_t = []
    return "key" if at_t else "anim"


def _ctrl_of(line):
    """ラインに紐づくコントローラー（独立ノード）を返す。無ければ None。"""
    if cmds.objExists(line) and cmds.attributeQuery(CTRL_LINK, node=line, exists=True):
        c = cmds.listConnections(line + "." + CTRL_LINK) or []
        if c and cmds.objExists(c[0]):
            return c[0]
    return None


def _set_if_diff(plug, value):
    """数値属性を、値が違うときだけ setAttr する。
    ツリー更新などで毎回呼ばれる「確保」処理が同じ値を書くと Undo 履歴に無意味な操作が
    積まれ、直前の操作が Ctrl+Z 1 回で戻らなくなるため（v0.1.46）。"""
    try:
        if abs(float(cmds.getAttr(plug)) - float(value)) <= 1e-9:
            return
    except Exception:
        pass
    try:
        cmds.setAttr(plug, value)
    except Exception:
        pass


def _set_outliner_color(node, rgb):
    try:
        _set_if_diff(node + ".useOutlinerColor", 1)
        cur = cmds.getAttr(node + ".outlinerColor")[0]
        if any(abs(float(cur[i]) - float(rgb[i])) > 1e-4 for i in range(3)):
            cmds.setAttr(node + ".outlinerColor", rgb[0], rgb[1], rgb[2], type="double3")
    except Exception:
        pass


def _lock_trs(node):
    for at in ("tx", "ty", "tz", "rx", "ry", "rz", "sx", "sy", "sz", "v"):
        plug = node + "." + at
        try:
            if (cmds.getAttr(plug, lock=True) and not cmds.getAttr(plug, keyable=True)
                    and not cmds.getAttr(plug, channelBox=True)):
                continue   # すでにロック済み（書くと Undo 履歴を汚す）
            cmds.setAttr(plug, lock=True, keyable=False, channelBox=False)
        except Exception:
            pass


def _ensure_mult_attr(node):
    if node and cmds.objExists(node) and not cmds.attributeQuery(GMULT, node=node, exists=True):
        try:
            cmds.addAttr(node, ln=GMULT, at="double", dv=1.0, min=0.0, keyable=True)
        except Exception:
            pass


def _line_group(line):
    par = cmds.listRelatives(line, parent=True, f=True) or []
    if par and cmds.attributeQuery(GROUP_TAG, node=par[0], exists=True):
        return par[0]
    return None


def _find_global_ctrl():
    if not cmds.objExists(ROOT):
        return None
    for c in cmds.listRelatives(ROOT, children=True, type="transform", f=True) or []:
        if _short(c) == GLOBAL_CTRL:
            return c
    return None


def _ensure_global_ctrl():
    """全体コントローラー（青・thicknessMult）を ROOT 直下に確保。コントローラー階層の親。"""
    _ensure_root()
    gc = None
    for c in cmds.listRelatives(ROOT, children=True, type="transform", f=True) or []:
        if _short(c) == GLOBAL_CTRL:
            gc = c
            break
    if gc is None:
        gc = cmds.createNode("transform", name=GLOBAL_CTRL)
        gc = cmds.parent(gc, ROOT)[0]
    _ensure_mult_attr(gc)
    # v0.1.18: フレネル感度アトリビュート（全フレネル線の maxWidthPx 倍率）。
    if not cmds.attributeQuery(FRES_SENS_ATTR, node=gc, exists=True):
        try:
            cmds.addAttr(gc, ln=FRES_SENS_ATTR, at="double", dv=1.0, min=0.0, keyable=True)
        except Exception:
            pass
    # v0.1.32: 遠景で細くする（0=無効, 1=強）
    if not cmds.attributeQuery(DIST_ATTEN_STRENGTH, node=gc, exists=True):
        try:
            cmds.addAttr(gc, ln=DIST_ATTEN_STRENGTH, at="double", dv=0.0,
                         min=0.0, max=1.0, keyable=True)
        except Exception:
            pass
    _set_outliner_color(gc, COL_GLOBAL)
    _lock_trs(gc)
    return gc


def _ensure_group_ctrl(group):
    """グループコントローラー（黄緑・thicknessMult）を全体コントローラー配下に確保。"""
    if _is_foreign(group):
        return _ctrl_of(group)     # v0.1.53: リファレンス等のグループは作り替えない
    gctrl_parent = _ensure_global_ctrl()
    gc = _ctrl_of(group)
    if gc is None or not cmds.objExists(gc):
        gc = cmds.createNode("transform", name=_short(group) + CTRL_SUFFIX)
        if not cmds.attributeQuery(CTRL_LINK, node=group, exists=True):
            cmds.addAttr(group, ln=CTRL_LINK, at="message")
        try:
            cmds.connectAttr(gc + ".message", group + "." + CTRL_LINK, f=True)
        except Exception:
            pass
    # 全体コントローラー配下へ
    par = cmds.listRelatives(gc, parent=True, f=True) or []
    if not par or _short(par[0]) != GLOBAL_CTRL:
        try:
            gc = cmds.parent(gc, gctrl_parent)[0]
        except Exception:
            pass
    _ensure_mult_attr(gc)
    _set_outliner_color(gc, COL_GROUP)
    _lock_trs(gc)
    return gc


def _create_line_ctrl(line, thick):
    """ライン用コントローラー（黄）を作成し、line と message で関連付ける（親子付けは呼び出し側）。"""
    ctrl = cmds.createNode("transform", name=_short(line) + CTRL_SUFFIX)
    for at, dv in ((CTRL_THICK, thick), (CTRL_CURV, 0.0), (CTRL_CAP, 3.0),
                   (CTRL_CMIN, DEFAULT_CMIN), (CTRL_TAPER, 0.0)):
        cmds.addAttr(ctrl, ln=at, at="double", dv=dv, keyable=True)
    cmds.addAttr(ctrl, ln=CTRL_PROFILE, dt="string")
    cmds.setAttr(ctrl + "." + CTRL_PROFILE, "0:1,1:1", type="string")
    if not cmds.attributeQuery(CTRL_LINK, node=line, exists=True):
        cmds.addAttr(line, ln=CTRL_LINK, at="message")
    try:
        cmds.connectAttr(ctrl + ".message", line + "." + CTRL_LINK, f=True)
    except Exception:
        pass
    return ctrl


def _ensure_line_anim(line, default_thick=0.05):
    """コントローラー階層（全体>グループ>ライン）を確保し、太さ乗算チェーンと追従ジョブを張る。"""
    if _is_foreign(line):
        # v0.1.53: リファレンス等のラインは構造を変えない（曲率ジョブだけ張る）
        ctrl = _ctrl_of(line)
        if ctrl and not cmds.attributeQuery(FRES_TAG, node=line, exists=True):
            _ensure_curv_jobs(line)
        return ctrl
    defm = _line_deformer(line)
    ctrl = _ctrl_of(line)
    if ctrl is None:
        thick = default_thick
        if defm:
            try:
                thick = cmds.getAttr(defm + ".offset")
            except Exception:
                pass
        ctrl = _create_line_ctrl(line, thick)
    # 旧コントローラーに endTaper / thicknessProfile / curvatureMin が無ければ追加（後方互換）
    if not cmds.attributeQuery(CTRL_TAPER, node=ctrl, exists=True):
        try:
            cmds.addAttr(ctrl, ln=CTRL_TAPER, at="double", dv=0.0, keyable=True)
        except Exception:
            pass
    if not cmds.attributeQuery(CTRL_CMIN, node=ctrl, exists=True):
        try:
            cmds.addAttr(ctrl, ln=CTRL_CMIN, at="double", dv=DEFAULT_CMIN, keyable=True)
        except Exception:
            pass
    if not cmds.attributeQuery(CTRL_PROFILE, node=ctrl, exists=True):
        try:
            cmds.addAttr(ctrl, ln=CTRL_PROFILE, dt="string")
            cmds.setAttr(ctrl + "." + CTRL_PROFILE, "0:1,1:1", type="string")
        except Exception:
            pass
    # ラインコントローラーを 所属グループのコントローラー（無ければ全体コントローラー）配下へ
    grp = _line_group(line)
    parent_ctrl = _ensure_group_ctrl(grp) if grp else _ensure_global_ctrl()
    par = cmds.listRelatives(ctrl, parent=True, f=True) or []
    if not par or _short(par[0]) != _short(parent_ctrl):
        try:
            ctrl = cmds.parent(ctrl, parent_ctrl)[0]
        except Exception:
            pass
    _set_outliner_color(ctrl, COL_LINE)
    _lock_trs(ctrl)
    _ensure_thickness_chain(line)
    # エッジライン（チューブ）は polyToCurve 経由で元に追従するので拘束/スムース連動は不要。
    # フレネルラインは hull 同様に拘束で追従させるが、曲率の頂点ウェイトは使わない。
    is_edge = cmds.attributeQuery(EDGE_TAG, node=line, exists=True)
    is_fres = cmds.attributeQuery(FRES_TAG, node=line, exists=True)
    if not is_edge:
        _ensure_follow(line)
        _ensure_smooth_link(line)
    # 曲率ジョブ: フレネル以外（背面法ハル/エッジ/スクリーン）で張る。スクリーンは頂点カラー、
    # それ以外は deformer weightList へ書き込む（_update_line_weights が振り分け）。
    if not is_fres:
        _ensure_curv_jobs(line)
    return ctrl


def _ensure_smooth_link(line):
    """元 shape のスムースメッシュプレビュー（サブディビ表示）をライン shape へ接続。
    元で 3 キー等のサブディビ表示を切り替えるとラインも追従する。"""
    src = _line_src_shape(line)
    osh = cmds.listRelatives(line, shapes=True, type="mesh", ni=True, f=True) or []
    if not src or not osh:
        return
    osh = osh[0]
    for at in ("displaySmoothMesh", "smoothLevel"):
        try:
            if (cmds.attributeQuery(at, node=src, exists=True)
                    and cmds.attributeQuery(at, node=osh, exists=True)
                    and not cmds.isConnected(src + "." + at, osh + "." + at)):
                cmds.connectAttr(src + "." + at, osh + "." + at, f=True)
        except Exception:
            pass


def _source_mesh_transforms():
    """v0.1.26: シーン内全ラインの元メッシュ transform のユニーク集合を返す。"""
    srcs = set()
    for t in cmds.ls(type="transform", long=True) or []:
        if (cmds.attributeQuery(TAG, node=t, exists=True) or
            cmds.attributeQuery(SCRN_TAG, node=t, exists=True) or
            cmds.attributeQuery(FRES_TAG, node=t, exists=True) or
            cmds.attributeQuery(EDGE_TAG, node=t, exists=True)):
            src = _line_source_mesh(t)     # v0.1.53: チューブは polyToCurve の入力メッシュ
            if src:
                srcs.add(src)
    return srcs


def _ensure_holdout_sg():
    """v0.1.26: ホールドアウト用 useBackground シェーダと SG を確保して SG 名を返す。"""
    if cmds.objExists(HOLDOUT_SG_NAME):
        return HOLDOUT_SG_NAME
    shd = HOLDOUT_SHADER_NAME
    if not cmds.objExists(shd):
        shd = cmds.shadingNode("useBackground", asShader=True, name=HOLDOUT_SHADER_NAME)
    sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=HOLDOUT_SG_NAME)
    try:
        cmds.connectAttr(shd + ".outColor", sg + ".surfaceShader", f=True)
    except Exception:
        pass
    return sg


def _current_mesh_sg(mesh_shape):
    """メッシュ shape に接続している shadingGroup 名（最初の一つ）を返す。"""
    sgs = cmds.listConnections(mesh_shape + ".instObjGroups", type="shadingEngine") or []
    return sgs[0] if sgs else None


def _mesh_sg_assignments(mesh_shape):
    """メッシュのマテリアル割り当てを [[SG, None], [SG, ["f[0:2]", ...]], ...] で返す
    （None=オブジェクト全体、リスト=面ごと。面はシェイプ名を除いた形で持つ）。
    v0.1.52: 以前は最初の SG だけを覚えていたため、面ごとに分けたマテリアルが
    「ライン単体表示」を ON→OFF すると 1 つにまとまって消えていた。"""
    full = (cmds.ls(mesh_shape, long=True) or [mesh_shape])[0]
    tr = (cmds.listRelatives(full, parent=True, f=True) or [None])[0]
    out = []
    seen = set()
    for sg in cmds.listConnections(full, type="shadingEngine") or []:
        if sg in seen:
            continue
        seen.add(sg)
        whole = False
        comps = []
        for m in cmds.ls(cmds.sets(sg, q=True) or [], long=True) or []:
            node, _, comp = m.partition(".")
            if node != full and node != tr:
                continue
            if comp:
                comps.append(comp)
            else:
                whole = True
        if whole:
            out.append([sg, None])
        elif comps:
            out.append([sg, comps])
    return out


def _parse_sg_assignments(text):
    """_mesh_sg_assignments を保存した文字列を戻す（旧形式＝SG 名だけ、にも対応）。"""
    import json
    text = (text or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
            return [[d[0], d[1]] for d in data if d and d[0]]
        except Exception:
            return []
    return [[text, None]]


def _restore_sg_assignments(mesh_shape, data):
    """保存しておいた割り当てを戻す（全体の割り当てを先に、面ごとを後に）。"""
    for sg, comps in data:
        if comps is None and cmds.objExists(sg):
            try:
                cmds.sets(mesh_shape, e=True, forceElement=sg)
            except Exception:
                pass
    for sg, comps in data:
        if comps and cmds.objExists(sg):
            try:
                cmds.sets([mesh_shape + "." + c for c in comps], e=True, forceElement=sg)
            except Exception:
                pass


def _set_line_only_mode(on):
    """v0.1.26: 全アウトライン対象メッシュを useBackground に差し替え（holdout）/ 復帰。
    ON: 現在の SG をメッシュ transform の `_toonPrevSG` attr に退避 → holdout SG を割当
    OFF: _toonPrevSG から元の SG を取り出して復帰（attr が無い/SG が無い場合はスキップ）"""
    import json
    srcs = _source_mesh_transforms()
    if on:
        sg = _ensure_holdout_sg()
        for mesh_tr in srcs:
            shs = cmds.listRelatives(mesh_tr, shapes=True, type="mesh", ni=True, f=True) or []
            if not shs:
                continue
            cur = [a for a in _mesh_sg_assignments(shs[0]) if a[0] != sg]
            # すでにホールドアウトだけ（ON を 2 回）のときは、覚えている元の割り当てを残す
            if cur:
                if not cmds.attributeQuery(MESH_PREV_SG_ATTR, node=mesh_tr, exists=True):
                    try:
                        cmds.addAttr(mesh_tr, ln=MESH_PREV_SG_ATTR, dt="string")
                    except Exception:
                        pass
                try:
                    cmds.setAttr(mesh_tr + "." + MESH_PREV_SG_ATTR, json.dumps(cur), type="string")
                except Exception:
                    pass
            try:
                cmds.sets(shs[0], e=True, forceElement=sg)
            except Exception:
                pass
    else:
        for mesh_tr in srcs:
            shs = cmds.listRelatives(mesh_tr, shapes=True, type="mesh", ni=True, f=True) or []
            if not shs:
                continue
            prev = []
            if cmds.attributeQuery(MESH_PREV_SG_ATTR, node=mesh_tr, exists=True):
                try:
                    prev = _parse_sg_assignments(cmds.getAttr(mesh_tr + "." + MESH_PREV_SG_ATTR))
                except Exception:
                    prev = []
            if prev:
                _restore_sg_assignments(shs[0], prev)
            # 復帰後は attr を消してよい（次回 ON 時に再記録）
            try:
                if cmds.attributeQuery(MESH_PREV_SG_ATTR, node=mesh_tr, exists=True):
                    cmds.deleteAttr(mesh_tr, at=MESH_PREV_SG_ATTR)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# v0.1.35: 選択からレンダーレイヤーを作成
# --------------------------------------------------------------------------- #
def _collect_lines_from_selection(nodes):
    """選択ノード列（ライン or グループ）から、含まれる全ラインを重複なく収集。"""
    lines = []
    seen = set()
    def _add(ln):
        if ln not in seen:
            seen.add(ln); lines.append(ln)
    for n in nodes:
        if not n or not cmds.objExists(n):
            continue
        if cmds.attributeQuery(TAG, node=n, exists=True):
            _add(n)
        elif cmds.attributeQuery(GROUP_TAG, node=n, exists=True):
            for d in cmds.listRelatives(n, ad=True, type="transform", f=True) or []:
                if cmds.attributeQuery(TAG, node=d, exists=True):
                    _add(d)
    return lines


def _line_source_mesh(line):
    """ラインの元メッシュ transform（フルパス）。無ければ None。
    v0.1.53: エッジ線・クリース線（チューブ）は deformer の入力がチューブ自身の中間シェイプなので、
    polyToCurve の入力メッシュから引く（以前はチューブ自身を返し、ライン単体表示でチューブの線まで
    背景色に差し替えていた）。"""
    try:
        if _attr_exists(line, EDGE_TAG):
            # polyToCurve コマンドが作るノードの型は polyEdgeToCurve
            for n in cmds.ls(cmds.listHistory(line) or [], type="polyEdgeToCurve") or []:
                for s in cmds.listConnections(n + ".inputPolymesh", s=True, d=False, sh=True) or []:
                    if cmds.nodeType(s) == "mesh":
                        par = cmds.listRelatives(s, parent=True, f=True) or []
                        if par:
                            return par[0]
            return None
        sh = _line_src_shape(line)
        if not sh or not cmds.objExists(sh) or cmds.nodeType(sh) != "mesh":
            return None
        par = cmds.listRelatives(sh, parent=True, f=True) or []
        if par and not cmds.attributeQuery(TAG, node=par[0], exists=True):
            return par[0]
    except Exception:
        pass
    return None


def _source_meshes_of(lines):
    """ライン列から元メッシュ transform をユニークに返す（_source_mesh_transforms の選択版）。"""
    srcs = []
    seen = set()
    for ln in lines:
        src = _line_source_mesh(ln)
        if src and src not in seen:
            seen.add(src); srcs.append(src)
    return srcs


def _sanitize_layer_name(name):
    """Render Setup レイヤー名を Maya のノード名規則に正規化（英数/アンダースコアのみ）。"""
    import re
    s = re.sub(r"[^A-Za-z0-9_]+", "_", name)
    if not s or not s[0].isalpha():
        s = "layer_" + s
    return s


def _unique_rs_layer_name(name):
    """既存レイヤーと衝突しない名前にする。"""
    try:
        import maya.app.renderSetup.model.renderSetup as rsM
        existing = {L.name() for L in rsM.instance().getRenderLayers()}
    except Exception:
        existing = set()
    base = name; i = 1
    while name in existing:
        i += 1
        name = "{0}_{1}".format(base, i)
    return name


def create_outline_render_layer(name, lines, src_meshes):
    """Render Setup に 「ライン include + 元メッシュ useBackground 差替」レイヤーを 1 つ作る。

    - collection_lines: 選択ラインを含める（グループが渡されても、呼び出し側で展開済み）
    - collection_src: 元メッシュを include → 配下に SG を拾う子コレクション →
        shaderOverride で useBackground に差替（Hardware 2.0 ホールドアウト）"""
    try:
        import maya.app.renderSetup.model.renderSetup as rsM
        import maya.app.renderSetup.model.selector as selectorM
        import maya.app.renderSetup.model.override as overrideM
    except ImportError as e:
        raise RuntimeError("Render Setup が利用できません: {0}".format(e))

    sg = _ensure_holdout_sg()                    # useBackground + _toonHoldoutSG
    shd = HOLDOUT_SHADER_NAME                    # 差替元 shader

    rs = rsM.instance()
    name = _unique_rs_layer_name(_sanitize_layer_name(name))
    layer = rs.createRenderLayer(name)

    # --- ライン include ---
    c_lines = layer.createCollection(name + "_lines")
    sel = c_lines.getSelector()
    try:
        sel.staticSelection.set([_short(ln) for ln in lines])
    except Exception:
        sel.staticSelection.set(list(lines))

    # --- 元メッシュを include + その SG を useBackground 差替 ---
    if src_meshes:
        c_src = layer.createCollection(name + "_src")
        sel2 = c_src.getSelector()
        try:
            sel2.staticSelection.set([_short(m) for m in src_meshes])
        except Exception:
            sel2.staticSelection.set(list(src_meshes))

        c_sg = c_src.createCollection(name + "_src_SG")
        sel3 = c_sg.getSelector()
        try:
            sel3.setFilterType(11)               # 11 = ShadingEngine filter
        except Exception:
            pass
        try:
            sel3.setPattern("*")
        except Exception:
            pass

        try:
            ov = c_sg.createOverride(name + "_holdout",
                                     overrideM.ShaderOverride.kTypeName)
        except Exception:
            # 旧API名フォールバック
            ov = c_sg.createOverride(name + "_holdout", "shaderOverride")
        # useBackground の outColor を override の attrValue に接続
        ov_node = ov.name() if hasattr(ov, "name") else str(ov)
        try:
            cmds.connectAttr(shd + ".outColor", ov_node + ".attrValue", f=True)
        except Exception:
            pass

    return name


# ---- v0.1.46: Render Setup でラインの見え方をレイヤーごとに上書きする ----
def _rs_model():
    try:
        import maya.app.renderSetup.model.renderSetup as rsM
        return rsM.instance()
    except Exception:
        return None


def _rs_layer_names():
    rs = _rs_model()
    if rs is None:
        return []
    try:
        return [L.name() for L in rs.getRenderLayers()]
    except Exception:
        return []


def _rs_get_layer(name):
    rs = _rs_model()
    if rs is None or not name:
        return None
    for L in rs.getRenderLayers():
        if L.name() == name:
            return L
    return None


def _rs_visible_layer_name():
    """いま表示中のレンダーレイヤー名（マスターなら ""）。"""
    rs = _rs_model()
    if rs is None:
        return ""
    try:
        vis = rs.getVisibleRenderLayer()
        if vis is None or vis.name() == rs.getDefaultRenderLayer().name():
            return ""
        return vis.name()
    except Exception:
        return ""


def _rs_switch(name):
    """表示レイヤーを切り替える（"" ならマスター）。"""
    rs = _rs_model()
    if rs is None:
        return False
    try:
        L = _rs_get_layer(name) if name else rs.getDefaultRenderLayer()
        if L is None:
            return False
        rs.switchToLayer(L)
        return True
    except Exception as exc:
        cmds.warning("レンダーレイヤーの切り替えに失敗: {0}".format(exc))
        return False


def _rs_line_collection(layer, line, create=False):
    """layer 内の、このライン用コレクション（<layer>_OG_<line>）。
    v0.1.48: 名前ではなく中身（静的選択にこのラインが入っているか）で探す。Render Setup の
    静的選択はリネームに追従するが、コレクション名は古いままなので名前で探すと見失う。"""
    full = (cmds.ls(line, long=True) or [line])[0]
    prefix = layer.name() + "_OG_"
    for c in layer.getCollections():
        # v0.1.53: レイヤー名ではなく "_OG_" で見分ける（レイヤーの名前を変えるとコレクション名は
        # 古いレイヤー名のまま残り、前方一致で探すと見失って 2 つ目を作っていた）
        if "_OG_" not in c.name():
            continue
        try:
            members = c.getSelector().staticSelection.asList()
        except Exception:
            members = []
        if full in members or line in members:
            return c
    if not create:
        return None
    c = layer.createCollection(_sanitize_layer_name(prefix + _short(line)))
    c.getSelector().staticSelection.set([full])
    return c


def _rs_find_override(layer_name, line, attr):
    L = _rs_get_layer(layer_name)
    if L is None:
        return None
    c = _rs_line_collection(L, line)
    if c is None:
        return None
    for ov in c.getOverrides():
        try:
            if ov.attributeName() == attr:
                return ov
        except Exception:
            pass
    return None


def _rs_set_override(layer_name, line, attr, value):
    """layer で line.attr を value に上書きする（絶対オーバーライドが無ければ作る）。"""
    L = _rs_get_layer(layer_name)
    if L is None:
        return False
    ov = _rs_find_override(layer_name, line, attr)
    if ov is None:
        c = _rs_line_collection(L, line, create=True)
        ov = c.createAbsoluteOverride(_short(line), attr)
    ov.setAttrValue(value)
    return True


def _rs_remove_override(layer_name, line, attr):
    """layer での line.attr の上書きを消す（マスターと同じに戻す）。
    v0.1.53: 上書きが 1 つも残らなくなったコレクションも消す（残すとラインがそのレイヤーに
    入ったままになる＝上書きをやめる前と同じ状態に戻らない）。"""
    ov = _rs_find_override(layer_name, line, attr)
    if ov is None:
        return False
    try:
        import maya.app.renderSetup.model.override as overrideM
        overrideM.delete(ov)
    except Exception:
        try:
            cmds.delete(ov.name())
        except Exception:
            return False
    L = _rs_get_layer(layer_name)
    c = _rs_line_collection(L, line) if L is not None else None
    if c is not None:
        try:
            left = list(c.getOverrides())
        except Exception:
            left = [None]
        if not left:
            try:
                import maya.app.renderSetup.model.collection as collectionM
                collectionM.delete(c)
            except Exception:
                try:
                    cmds.delete(c.name())
                except Exception:
                    pass
    return True


def create_hidden_only_layer(lines, name=None):
    """lines の「隠れ線だけ」を書き出すレイヤーを作る（表側の線は描かない）。
    シーンのメッシュ（元メッシュ・紐づけ先・手前の物）は useBackground で深度だけ書く
    （透かす位置の判定と、手前の物で隠れる所を本番の絵と一致させるため）。
    作ったレイヤー名を返す。"""
    lines = [ln for ln in lines or [] if _is_detail_line(ln)]
    if not lines:
        raise RuntimeError("スクリーン輪郭・エッジ線・クリース線のラインがありません")
    holdouts = list(_source_meshes_of(lines))
    for ln in lines:
        for o in _mesh_transforms_under(_line_occluders(ln)):
            if o not in holdouts:
                holdouts.append(o)
    # 他のメッシュも深度だけ書かせる（手前の物が隠れ線を正しく遮る＝本番の絵と合成で一致する）
    for s in cmds.ls(type="mesh", long=True, ni=True) or []:
        par = cmds.listRelatives(s, parent=True, f=True) or []
        if par and par[0] not in holdouts and not _is_toon_node(par[0]):
            holdouts.append(par[0])
    lname = create_outline_render_layer(name or (_short(lines[0]) + "_hidden"), lines, holdouts)
    _rs_switch(lname)
    for ln in lines:
        _ensure_line_render_attrs(ln)
        _rs_set_override(lname, ln, LN_SHOW_VIS, False)
        if _hidden_line_mode(ln) == 0:
            _rs_set_override(lname, ln, LN_HID_MODE, 1)
    return lname


def _ensure_follow(line):
    """元オブジェクトのトランスフォーム移動にラインを追従させる（parent/scale 拘束）。
    outMesh は変形のみ追従するので、移動/回転/スケールは拘束で合わせる。"""
    if cmds.listConnections(line + ".translateX", s=True, d=False, type="parentConstraint"):
        return
    srcsh = _line_src_shape(line)
    if not srcsh:
        return
    par = cmds.listRelatives(srcsh, parent=True, f=True) or []
    if not par:
        return
    src = par[0]
    try:
        cmds.parentConstraint(src, line, maintainOffset=False)
        cmds.scaleConstraint(src, line, maintainOffset=False)
    except Exception:
        pass


def _mask_of(line):
    """ライン A に紐づく重なりマスク（B）を返す。無ければ None。"""
    if cmds.objExists(line) and cmds.attributeQuery(MASK_LINK, node=line, exists=True):
        c = [x for x in (cmds.listConnections(line + "." + MASK_LINK, s=True, d=False) or [])
             if cmds.objExists(x)]
        if c:
            return c[0]
    return None


def _connect(src, dst):
    """未接続のときだけ接続（既接続時の警告を避ける）。"""
    try:
        if not cmds.isConnected(src, dst):
            cmds.connectAttr(src, dst, f=True)
    except Exception:
        pass


def _thick_target(line):
    """太さを流し込む先のプラグ。
    hull / エッジ: textureDeformer.offset。フレネル: dx11Shader.threshold。
    スクリーン輪郭: dx11Shader.thickness（ピクセル）。"""
    if cmds.attributeQuery(SCRN_TAG, node=line, exists=True):
        shd = _fresnel_shader(line)
        if shd and cmds.attributeQuery(SCRN_THICK, node=shd, exists=True):
            return shd + "." + SCRN_THICK
        return None
    if cmds.attributeQuery(FRES_TAG, node=line, exists=True):
        shd = _fresnel_shader(line)
        if shd and cmds.attributeQuery(FRES_THRESH, node=shd, exists=True):
            return shd + "." + FRES_THRESH
        return None
    defm = _line_deformer(line)
    return (defm + ".offset") if defm else None


def _ensure_thickness_chain(line):
    """太さ = lineCtrl.thickness * groupCtrl.thicknessMult * globalCtrl.thicknessMult を DG で構築。
    出力先は hull ラインなら textureDeformer.offset、エッジラインなら円プロファイル半径。"""
    if _is_foreign(line):
        return      # v0.1.53: リファレンス等のラインはアセット側でつないである（このシーンへつなぎ替えない）
    target = _thick_target(line)
    ctrl = _ctrl_of(line)
    if not target or not ctrl:
        return
    gctrl = _ensure_global_ctrl()
    grp = _line_group(line)
    grpctrl = _ensure_group_ctrl(grp) if grp else None
    base = _short(ctrl)
    mA = base + "_thkA"
    mB = base + "_thkB"
    if not cmds.objExists(mA):
        # v0.1.53: コントローラーの名前が変わっていたら、つながっている既存のノードの名前を合わせる
        # （以前は新しく作り直し、古いノードが残って Ctrl+Z 1 回で名前変更が戻らなかった）
        for n in cmds.listConnections(ctrl + "." + CTRL_THICK, s=False, d=True,
                                      type="multDoubleLinear") or []:
            if n.endswith("_thkA"):
                _rename_ctrl_nodes(n[:-len("_thkA")], base)
                break
    if not cmds.objExists(mA):
        mA = cmds.createNode("multDoubleLinear", name=mA)
    if not cmds.objExists(mB):
        mB = cmds.createNode("multDoubleLinear", name=mB)
    # mA = lineCtrl.thickness * groupCtrl.thicknessMult
    _connect(ctrl + "." + CTRL_THICK, mA + ".input1")
    if grpctrl:
        if not cmds.isConnected(grpctrl + "." + GMULT, mA + ".input2"):
            for p in (cmds.listConnections(mA + ".input2", s=True, d=False, p=True) or []):
                try:
                    cmds.disconnectAttr(p, mA + ".input2")
                except Exception:
                    pass
            _connect(grpctrl + "." + GMULT, mA + ".input2")
    else:
        for p in (cmds.listConnections(mA + ".input2", s=True, d=False, p=True) or []):
            try:
                cmds.disconnectAttr(p, mA + ".input2")
            except Exception:
                pass
        _set_if_diff(mA + ".input2", 1.0)
    # mB = mA * globalCtrl.thicknessMult → 総太さ（UI 表示値ベース）
    _connect(mA + ".output", mB + ".input1")
    _connect(gctrl + "." + GMULT, mB + ".input2")

    is_edge = cmds.attributeQuery(EDGE_TAG, node=line, exists=True)
    if is_edge:
        # v0.1.33: 距離減衰 attr を先に乗算（offset と radius の両方に効かせる）
        if not cmds.attributeQuery(CTRL_DISTATTEN, node=ctrl, exists=True):
            try:
                cmds.addAttr(ctrl, ln=CTRL_DISTATTEN, at="double", dv=1.0, min=0.0, keyable=True)
            except Exception:
                pass
        mTD = base + "_edgeDist"
        if not cmds.objExists(mTD):
            mTD = cmds.createNode("multDoubleLinear", name=mTD)
        _connect(mB + ".output", mTD + ".input1")
        _connect(ctrl + "." + CTRL_DISTATTEN, mTD + ".input2")
        # エッジは UI 太さ×EDGE_THICK_SCALE を offset に流す（同じ数値でも細く見せる）
        mS = base + "_thkScale"
        if not cmds.objExists(mS):
            mS = cmds.createNode("multDoubleLinear", name=mS)
        _connect(mTD + ".output", mS + ".input1")   # v0.1.33: mB→mTD→mS
        _set_if_diff(mS + ".input2", EDGE_THICK_SCALE)
        _connect(mS + ".output", target)
        # 円プロファイルの基準半径も太さに比例させ、細くするとチューブ全体が細る
        circ = _line_circle_node(line)
        if circ:
            mR = base + "_radScale"
            if not cmds.objExists(mR):
                mR = cmds.createNode("multDoubleLinear", name=mR)
            _connect(mTD + ".output", mR + ".input1")   # v0.1.33: distAtten 後の値を使う
            _set_if_diff(mR + ".input2", EDGE_BASE_SCALE)
            _connect(mR + ".output", circ + ".radius")
    elif cmds.attributeQuery(FRES_TAG, node=line, exists=True):
        # フレネルは UI 太さ×FRES_SCALE を condition.secondTerm(facingRatio しきい値)へ。
        # しきい値が大きいほど寝た面まで線が出る＝太い線になる。
        mS = base + "_fresScale"
        if not cmds.objExists(mS):
            mS = cmds.createNode("multDoubleLinear", name=mS)
        _connect(mB + ".output", mS + ".input1")
        _set_if_diff(mS + ".input2", FRES_SCALE)
        _connect(mS + ".output", target)
        # 帯の画面px幅の上限 = UI太さ × SCRN_SCALE × globalCtrl.fresSens（感度）。
        # 感度=見た目の太さを変えずに「どこまで内側（寝た面）まで検出するか」を調整。
        shd_fres = target.rsplit(".", 1)[0]
        if cmds.attributeQuery(FRES_MAXW, node=shd_fres, exists=True):
            mW = base + "_fresMaxW"
            if not cmds.objExists(mW):
                mW = cmds.createNode("multDoubleLinear", name=mW)
            _connect(mB + ".output", mW + ".input1")
            _set_if_diff(mW + ".input2", SCRN_SCALE)
            # v0.1.18: mW.output × gctrl.fresSens → maxWidthPx
            mWS = base + "_fresMaxWSens"
            if not cmds.objExists(mWS):
                mWS = cmds.createNode("multDoubleLinear", name=mWS)
            _connect(mW + ".output", mWS + ".input1")
            _connect(gctrl + "." + FRES_SENS_ATTR, mWS + ".input2")
            _connect(mWS + ".output", shd_fres + "." + FRES_MAXW)
    elif cmds.attributeQuery(SCRN_TAG, node=line, exists=True):
        # スクリーン輪郭は UI 太さ×SCRN_SCALE をピクセル太さ(thickness uniform)へ。
        mS = base + "_scrnScale"
        if not cmds.objExists(mS):
            mS = cmds.createNode("multDoubleLinear", name=mS)
        _connect(mB + ".output", mS + ".input1")
        _set_if_diff(mS + ".input2", SCRN_SCALE)
        # v0.1.32: 距離減衰 (ctrl.camDistAtten を乗算) を末尾に挿入。
        if not cmds.attributeQuery(CTRL_DISTATTEN, node=ctrl, exists=True):
            try:
                cmds.addAttr(ctrl, ln=CTRL_DISTATTEN, at="double", dv=1.0, min=0.0, keyable=True)
            except Exception:
                pass
        mD = base + "_scrnDist"
        if not cmds.objExists(mD):
            mD = cmds.createNode("multDoubleLinear", name=mD)
        _connect(mS + ".output", mD + ".input1")
        _connect(ctrl + "." + CTRL_DISTATTEN, mD + ".input2")
        _connect(mD + ".output", target)
    else:
        # hull: 重なりマスクがある場合は、マスクが覆う inflate 分だけ offset を上乗せして
        # 見える線幅（= mB.output = 設定太さ）を保つ。offset = mB.output * (1 + MASK_INFLATE_FRAC)。
        mBoost = base + "_thkMaskBoost"
        if _mask_of(line):
            if not cmds.objExists(mBoost):
                mBoost = cmds.createNode("multDoubleLinear", name=mBoost)
            _connect(mB + ".output", mBoost + ".input1")
            _set_if_diff(mBoost + ".input2", 1.0 + MASK_INFLATE_FRAC)
            for p in (cmds.listConnections(target, s=True, d=False, p=True) or []):
                if p != mBoost + ".output":
                    try:
                        cmds.disconnectAttr(p, target)
                    except Exception:
                        pass
            _connect(mBoost + ".output", target)
        else:
            if cmds.objExists(mBoost):
                try:
                    cmds.delete(mBoost)
                except Exception:
                    pass
            for p in (cmds.listConnections(target, s=True, d=False, p=True) or []):
                if p != mB + ".output":
                    try:
                        cmds.disconnectAttr(p, target)
                    except Exception:
                        pass
            _connect(mB + ".output", target)

    # エッジラインは円プロファイルの基準半径があるため、太さ(offset)が 0 でも
    # チューブが残る。総太さ(mB.output)が ~0 のときシェイプ可視を 0 にして消す。
    # （シェイプ可視を駆動。手動表示/非表示はトランスフォーム可視なので競合しない）
    if cmds.attributeQuery(EDGE_TAG, node=line, exists=True):
        shp = cmds.listRelatives(line, shapes=True, type="mesh", ni=True, f=True) or []
        if shp:
            lshape = shp[0]
            cnd = base + "_visCond"
            if not cmds.objExists(cnd):
                cnd = cmds.createNode("condition", name=cnd)
            _set_if_diff(cnd + ".operation", 2)          # Greater Than
            _set_if_diff(cnd + ".secondTerm", EDGE_VIS_EPS)
            _set_if_diff(cnd + ".colorIfTrueR", 1)
            _set_if_diff(cnd + ".colorIfFalseR", 0)
            _connect(mB + ".output", cnd + ".firstTerm")
            try:
                if cmds.getAttr(lshape + ".visibility", lock=True):
                    cmds.setAttr(lshape + ".visibility", lock=False)
            except Exception:
                pass
            _connect(cnd + ".outColorR", lshape + ".visibility")


def _is_hull_line(line):
    """背面法ハルライン（エッジ/フレネル/スクリーン以外）か。隠蔽検知の対象判定に使う。"""
    if not cmds.attributeQuery(TAG, node=line, exists=True):
        return False
    for t in (EDGE_TAG, FRES_TAG, SCRN_TAG):
        if cmds.attributeQuery(t, node=line, exists=True):
            return False
    return True


def _active_camera():
    """アクティブなモデルパネルのカメラ shape を返す。無ければ persp。"""
    def _cam_shape(c):
        if not c:
            return None
        if cmds.nodeType(c) == "camera":
            return c
        sh = cmds.listRelatives(c, shapes=True, type="camera", f=True) or []
        return sh[0] if sh else None
    try:
        p = cmds.getPanel(withFocus=True)
        if p and cmds.getPanel(typeOf=p) == "modelPanel":
            sh = _cam_shape(cmds.modelEditor(p, q=True, camera=True))
            if sh:
                return sh
    except Exception:
        pass
    for p in (cmds.getPanel(type="modelPanel") or []):
        try:
            sh = _cam_shape(cmds.modelEditor(p, q=True, camera=True))
            if sh:
                return sh
        except Exception:
            pass
    return "perspShape" if cmds.objExists("perspShape") else None


def _vertex_adjacency(dag, n):
    """各頂点の近傍頂点リスト（dilate/スムージングで使い回す）。失敗時は None。"""
    try:
        itv = om2.MItMeshVertex(dag)
    except Exception:
        return None
    adj = [()] * n
    while not itv.isDone():
        adj[itv.index()] = tuple(itv.getConnectedVertices())
        itv.next()
    return adj


def _smooth_vertex_values(dag, vals, iters, adj=None):
    """頂点値リストを近傍平均で iters 回スムージングして返す（境界のジャギを均す）。"""
    n = len(vals)
    if n == 0 or iters <= 0:
        return vals
    if adj is None:
        adj = _vertex_adjacency(dag, n)
    if adj is None:
        return vals
    cur = list(vals)
    for _ in range(iters):
        nxt = list(cur)
        for i, a in enumerate(adj):
            if a:
                s = cur[i]
                for c in a:
                    s += cur[c]
                nxt[i] = s / (len(a) + 1)
        cur = nxt
    return cur


def _mesh_all_intersections(mfn, src_pt, direction, space, maxp, accel):
    """MFnMesh.allIntersections をシグネチャ違いに強く呼ぶ。(hitFaces, hitParams) を返す。
    om2 の引数順は環境差があるため、複数フォームを順に試す。失敗時は ([],[])。"""
    forms = []
    if accel is not None:
        forms.append(lambda: mfn.allIntersections(src_pt, direction, space, maxp, False,
                                                  accelParams=accel))
    forms.append(lambda: mfn.allIntersections(src_pt, direction, space, maxp, False))
    for f in forms:
        try:
            r = f()
        except Exception:
            continue
        if not r:
            return [], []
        try:
            faces = list(r[2])      # hitFaces (MIntArray)
            params = list(r[1])     # hitRayParams (MFloatArray)
            return faces, params
        except Exception:
            return [], []
    return [], []


def _occlusion_factors(line):
    """オフセット後のシェル頂点 S=P+N*太さ から、**カメラ方向（手前）と逆方向（奥）の両方**へレイを
    飛ばし、どちらかで元メッシュに当たれば「画面上で本体に重なっている」＝引き寄せ対象とする方式。
    重なり頂点 → OCC_HIDDEN_WEIGHT（負＝元メッシュ内側へ寄せて隠す）、フチ（背景に抜ける）→ 1.0。
    両方外れる＝シルエットのフチ（背景に抜ける）だけ太さを残す。
    オブジェクト空間で計算（MFnMesh のレイ交差はオブジェクト空間が確実なため）。"""
    src = _line_src_shape(line)
    if not src or not cmds.objExists(src):
        return []
    camsh = _active_camera()
    if not camsh:
        return []
    try:
        csl = om2.MSelectionList(); csl.add(camsh)
        cmat = csl.getDagPath(0).inclusiveMatrix()
        campos = (cmat[12], cmat[13], cmat[14])   # カメラのワールド位置（行列の並進成分）
    except Exception:
        return []
    try:
        sl = om2.MSelectionList(); sl.add(src)
        dag = sl.getDagPath(0)
        mfn = om2.MFnMesh(dag)
        pts = mfn.getPoints(om2.MSpace.kObject)
        nrm = mfn.getVertexNormals(False, om2.MSpace.kObject)
    except Exception:
        return []
    n = len(pts)
    if n == 0:
        return []
    # ワールド→オブジェクト変換でカメラ位置/向きをオブジェクト空間へ
    wim = dag.inclusiveMatrixInverse()
    cam_o = om2.MPoint(campos[0], campos[1], campos[2]) * wim
    cam = om2.MFloatPoint(cam_o.x, cam_o.y, cam_o.z)
    is_ortho = False
    try:
        is_ortho = bool(cmds.getAttr(camsh + ".orthographic"))
    except Exception:
        is_ortho = False
    vdir = None   # ortho: 頂点→カメラ方向（一定）
    if is_ortho:
        try:
            m = cmds.xform(camsh, q=True, ws=True, m=True)   # カメラのワールド行列
            fwd = om2.MVector(-m[8], -m[9], -m[10])           # カメラ前方 = -Z
            fwd_o = (fwd * wim).normal()
            vdir = om2.MFloatVector(-fwd_o.x, -fwd_o.y, -fwd_o.z)
        except Exception:
            is_ortho = False
    # オブジェクト空間 bbox から最大距離/バイアスを決定
    try:
        bb = mfn.boundingBox
        diag = (bb.width ** 2 + bb.height ** 2 + bb.depth ** 2) ** 0.5
    except Exception:
        diag = 1.0
    far = max(diag * 4.0, 1.0)
    # シェルのオフセット量（= textureDeformer.offset の実太さ）。0 なら bbox から微小量。
    off = 0.0
    defm = _line_deformer(line)
    if defm:
        try:
            off = abs(cmds.getAttr(defm + ".offset"))
        except Exception:
            off = 0.0
    if off <= 1e-6:
        off = max(diag * 5e-3, 1e-4)
    bias = max(off * 1e-2, 1e-5)   # 自己交差回避の微小バイアス
    try:
        accel = mfn.autoUniformGridParams()
    except Exception:
        accel = None
    kob = om2.MSpace.kObject

    def _hit(origin, d, maxp):
        if maxp <= 0.0:
            return False
        faces, _params = _mesh_all_intersections(mfn, origin, d, kob, maxp, accel)
        return bool(faces)

    fac = [1.0] * n
    for i in range(n):
        p = pts[i]; nv = nrm[i]
        # オフセット後のシェル頂点 S
        sx = p.x + nv.x * off; sy = p.y + nv.y * off; sz = p.z + nv.z * off
        if is_ortho and vdir is not None:
            to_cam = vdir
            dist = far
        else:
            dx = cam.x - sx; dy = cam.y - sy; dz = cam.z - sz
            to_cam = om2.MFloatVector(dx, dy, dz)
            dist = to_cam.length()
            if dist < 1e-6:
                continue
            to_cam = to_cam / dist
        away = om2.MFloatVector(-to_cam.x, -to_cam.y, -to_cam.z)
        # バイアス分だけ視線方向にずらした始点（自己交差回避）
        fwd_org = om2.MFloatPoint(sx + to_cam.x * bias, sy + to_cam.y * bias, sz + to_cam.z * bias)
        bwd_org = om2.MFloatPoint(sx + away.x * bias, sy + away.y * bias, sz + away.z * bias)
        fmax = far if (is_ortho and vdir is not None) else (dist - bias * 2.0)
        # 手前（カメラ側）に本体があるか / 奥に本体があるか → どちらかで重なり
        if _hit(fwd_org, to_cam, fmax) or _hit(bwd_org, away, far):
            fac[i] = OCC_HIDDEN_WEIGHT
    adj = _vertex_adjacency(dag, n)
    kept = [f >= 1.0 for f in fac]
    # 可視リムが1頂点幅だとカメラ移動で頂点単位に切り替わり太さがちらつく。残す頂点を
    # 内側へ OCC_KEEP_DILATE リング分太らせて帯にし、太さを安定させる（隠れ側へ食い込む）。
    if adj is not None and OCC_KEEP_DILATE > 0:
        for _ring in range(OCC_KEEP_DILATE):
            add = [i for i in range(n) if not kept[i] and any(kept[c] for c in adj[i])]
            for i in add:
                kept[i] = True
        for i in range(n):
            if kept[i]:
                fac[i] = 1.0
    # スムージングは隠す側の段差を均すためだけに使う。
    # ・可視リム（残す頂点）: スムージングで 1.0 未満に下がると太さが減るため必ず 1.0 に再クランプ。
    # ・隠す頂点: スムージングで 0 付近まで上がると表面手前に残って張り付かないため、必ず
    #   OCC_HIDE_CEIL 以下（表面の裏）にクランプして確実に潜らせる。
    sm = _smooth_vertex_values(dag, fac, OCC_SMOOTH_ITERS, adj=adj)
    for i in range(n):
        if kept[i]:
            sm[i] = 1.0
        elif sm[i] > OCC_HIDE_CEIL:
            sm[i] = OCC_HIDE_CEIL
    return sm


def _occlusion_debug(line):
    """隠蔽検知が 0/0 になる原因を切り分けるための診断文字列を返す。"""
    src = _line_src_shape(line)
    if not src or not cmds.objExists(src):
        return "src shape 取得失敗 (src={})".format(src)
    cam = _active_camera()
    if not cam:
        return "アクティブカメラ取得失敗"
    try:
        csl = om2.MSelectionList(); csl.add(cam)
        cmat = csl.getDagPath(0).inclusiveMatrix()
        cpos = (round(cmat[12], 2), round(cmat[13], 2), round(cmat[14], 2))
    except Exception as e:
        return "カメラ行列取得失敗: {} (cam={})".format(e, cam)
    try:
        sl = om2.MSelectionList(); sl.add(src)
        dag = sl.getDagPath(0)
        mfn = om2.MFnMesh(dag)
        np = len(mfn.getPoints(om2.MSpace.kObject))
    except Exception as e:
        return "om2 メッシュ取得失敗: {} (src={})".format(e, _short(src))
    return "src={} verts={} cam={} pos={}".format(_short(src), np, _short(cam), cpos)


def _face_sg_map(shape, tform):
    """v0.1.38: face index → shadingEngine 名の辞書を返す。
    単一 SG（＝面ごとの差が無い）メッシュは {} を返す（内部でマテリアル境界が存在しない）。
    SG がバラバラに割り当てられたメッシュのみマテリアル境界検出対象にする。"""
    try:
        sgs = list(set(cmds.listConnections(shape, type="shadingEngine") or []))
    except Exception:
        sgs = []
    if len(sgs) <= 1:
        return {}
    try:
        total = cmds.polyEvaluate(shape, face=True) or 0
    except Exception:
        total = 0
    if total == 0:
        return {}
    # v0.1.52: フルパスで比べる（短い名前だと charA|box と charB|box の面を取り違える）
    full_shape = (cmds.ls(shape, long=True) or [shape])[0]
    full_tform = (cmds.ls(tform, long=True) or [tform])[0]
    face_sg = {}
    for sg in sgs:
        try:
            flat = cmds.ls(cmds.sets(sg, q=True) or [], flatten=True, long=True) or []
        except Exception:
            flat = []
        for m in flat:
            if ".f[" not in m:
                continue
            node_part = m.split(".f[")[0]
            if node_part != full_shape and node_part != full_tform:
                continue
            try:
                fidx = int(m.split(".f[")[1].rstrip("]"))
            except Exception:
                continue
            face_sg[fidx] = sg
    return face_sg


def _material_boundary_edges(shape, tform):
    """v0.1.38: 隣接 2 面の shadingEngine が異なるエッジのコンポーネント名リストを返す
    （"<tform>.e[i]"）。カメラ非依存・静的。単一 SG メッシュでは空リスト。"""
    face_sg = _face_sg_map(shape, tform)
    if not face_sg:
        return []
    try:
        sl = om2.MSelectionList(); sl.add(shape)
        dag = sl.getDagPath(0)
        ite = om2.MItMeshEdge(dag)
    except Exception:
        return []
    short = _short(tform)
    out = []
    while not ite.isDone():
        idx = ite.index()
        try:
            if not ite.onBoundary():
                faces = ite.getConnectedFaces()
                if len(faces) >= 2:
                    sg0 = face_sg.get(faces[0])
                    sg1 = face_sg.get(faces[1])
                    if sg0 and sg1 and sg0 != sg1:
                        out.append("{}.e[{}]".format(short, idx))
        except Exception:
            pass
        ite.next()
    return out


def _crease_border_edges(shape, tform, angle_deg=CREASE_ANGLE):
    """シェイプのクリース（二面角がしきい値超）＋ボーダー（開境界）エッジの
    コンポーネント名リスト（"<tform>.e[i]"）を返す。カメラ非依存・静的。"""
    try:
        sl = om2.MSelectionList(); sl.add(shape)
        dag = sl.getDagPath(0)
        mfn = om2.MFnMesh(dag)
        ite = om2.MItMeshEdge(dag)
    except Exception:
        return []
    cos_thr = math.cos(math.radians(max(0.0, min(179.0, angle_deg))))
    short = _short(tform)
    out = []
    while not ite.isDone():
        idx = ite.index()
        crease = False
        try:
            if ite.onBoundary():
                crease = True
            else:
                faces = ite.getConnectedFaces()
                if len(faces) >= 2:
                    n0 = mfn.getPolygonNormal(faces[0], om2.MSpace.kObject)
                    n1 = mfn.getPolygonNormal(faces[1], om2.MSpace.kObject)
                    # 法線の内積 < cos(しきい角) ⇔ 面の成す角がしきい角超＝折れ目
                    if (n0 * n1) < cos_thr:
                        crease = True
        except Exception:
            crease = False
        if crease:
            out.append("{}.e[{}]".format(short, idx))
        ite.next()
    return out


def _split_edges_into_chains(shape, edge_indices):
    """選択エッジインデックスを「非分岐な連続パス」(chain) に分割して返す。
    polyToCurve は一本道のエッジ列しか受け付けず、立方体のように 12 本の
    バラバラな折り目があると 1 本しかカーブ化できない。連結成分＋分岐検出で
    chain のリストに分け、各 chain を個別のエッジラインにする。"""
    try:
        sl = om2.MSelectionList(); sl.add(shape)
        dag = sl.getDagPath(0)
        mesh = om2.MFnMesh(dag)
    except Exception:
        return [[e] for e in edge_indices]
    edge_verts = {}
    vert_edges = {}
    for e in edge_indices:
        try:
            v = mesh.getEdgeVertices(e)
        except Exception:
            continue
        edge_verts[e] = (v[0], v[1])
        vert_edges.setdefault(v[0], []).append(e)
        vert_edges.setdefault(v[1], []).append(e)

    def other_vert(e, v):
        a, b = edge_verts[e]
        return b if v == a else a

    chains = []
    used = set()
    endpoints = [v for v, es in vert_edges.items() if len(es) != 2]
    for start_v in endpoints:
        for e0 in list(vert_edges[start_v]):
            if e0 in used: continue
            chain = []; v = start_v; e = e0
            while e is not None and e not in used:
                used.add(e)
                chain.append(e)
                nv = other_vert(e, v)
                nbrs = vert_edges.get(nv, [])
                cand = [x for x in nbrs if x not in used]
                if len(nbrs) == 2 and len(cand) == 1:
                    v = nv; e = cand[0]
                else:
                    e = None
            if chain:
                chains.append(chain)
    for e0 in edge_indices:
        if e0 in used or e0 not in edge_verts: continue
        chain = []; v = edge_verts[e0][0]; e = e0
        while e is not None and e not in used:
            used.add(e)
            chain.append(e)
            nv = other_vert(e, v)
            cand = [x for x in vert_edges.get(nv, []) if x not in used]
            if len(cand) == 1:
                v = nv; e = cand[0]
            else:
                e = None
        if chain:
            chains.append(chain)
    return chains


def _gapfill_of(line):
    """ライン A に紐づく隙間埋めオブジェクト B を返す。無ければ None。"""
    if cmds.objExists(line) and cmds.attributeQuery(GAPFILL_LINK, node=line, exists=True):
        c = [x for x in (cmds.listConnections(line + "." + GAPFILL_LINK, s=True, d=False) or [])
             if cmds.objExists(x)]
        if c:
            return c[0]
    return None


def _resoften_all_hull_shapes():
    """既存の全ハルライン(SCRN/FRES/EDGE 以外)の shape に polySoftEdge(a=180) を再適用。
    紐づく GAPFILL B も同時に soft 化。v0.1.8 で create_outlines に組み込んだ
    polySoftEdge は**生成時**にしか走らないため、v0.1.7 以前に作ったラインには適用
    されていない。これを削除せず一発で修復する入り口。
    実機で: `import OG_Toonline_Manager as m; m._resoften_all_hull_shapes()`"""
    n = 0
    for t in cmds.ls(type="transform") or []:
        if not cmds.attributeQuery(TAG, node=t, exists=True):
            continue
        # SCRN / FRES / EDGE は normals に手を入れないのでスキップ
        skip = False
        for tag in (EDGE_TAG, FRES_TAG, SCRN_TAG):
            if cmds.attributeQuery(tag, node=t, exists=True):
                skip = True; break
        if skip:
            continue
        for shape in (cmds.listRelatives(t, shapes=True, type="mesh", ni=True, f=True) or []):
            try:
                cmds.polySoftEdge(shape + ".e[*]", a=180, ch=False); n += 1
            except Exception:
                pass
        gf = _gapfill_of(t)
        if gf:
            for bshp in (cmds.listRelatives(gf, shapes=True, type="mesh", ni=True, f=True) or []):
                try:
                    cmds.polySoftEdge(bshp + ".e[*]", a=180, ch=False); n += 1
                except Exception:
                    pass
    try:
        om2.MGlobal.displayInfo("[hull] re-softened {0} shape(s)".format(n))
    except Exception:
        pass
    return n


def _all_gapfills():
    """シーン内の全隙間埋めオブジェクト（GAPFILL_TAG 付き transform）。"""
    return [t for t in (cmds.ls(type="transform") or [])
            if cmds.attributeQuery(GAPFILL_TAG, node=t, exists=True)]


def _gapfill_src_shape(b):
    """隙間埋め B の元メッシュ shape（textureDeformer のベース入力）。"""
    defm = cmds.ls(cmds.listHistory(b) or [], type=THICK_TYPE)
    if defm:
        conn = cmds.listConnections(defm[0] + ".input[0].inputGeometry",
                                    s=True, d=False, sh=True) or []
        if conn:
            return conn[0]
    return None


def _update_gapfill_weights(b):
    """隙間埋め B の weightList を facing（=|法線·視線|）で再計算。
    寝た面（カメラに対して横向き＝シルエット）→ 1（ベース位置=offset まで押し出す）、
    正面/背面を向いた面 → 0（元メッシュ表面に張り付き）。カメラ依存・レイ不要で軽い。"""
    if not cmds.objExists(b):
        return
    dl = cmds.ls(cmds.listHistory(b) or [], type=THICK_TYPE)
    if not dl:
        return
    defm = dl[0]
    src = _gapfill_src_shape(b)
    if not src or not cmds.objExists(src):
        return
    cam = _active_camera()
    if not cam:
        return
    try:
        csl = om2.MSelectionList(); csl.add(cam)
        cmat = csl.getDagPath(0).inclusiveMatrix()
        campos = (cmat[12], cmat[13], cmat[14])
    except Exception:
        return
    try:
        sl = om2.MSelectionList(); sl.add(src)
        dag = sl.getDagPath(0)
        mfn = om2.MFnMesh(dag)
        pts = mfn.getPoints(om2.MSpace.kObject)
        nrm = mfn.getVertexNormals(False, om2.MSpace.kObject)
    except Exception:
        return
    n = len(pts)
    if n == 0:
        return
    wim = dag.inclusiveMatrixInverse()
    cam_o = om2.MPoint(campos[0], campos[1], campos[2]) * wim
    is_ortho = False
    try:
        is_ortho = bool(cmds.getAttr(cam + ".orthographic"))
    except Exception:
        is_ortho = False
    vdir = None
    if is_ortho:
        try:
            m = cmds.xform(cam, q=True, ws=True, m=True)
            fwd = om2.MVector(-m[8], -m[9], -m[10])
            vdir = (fwd * wim).normal()
        except Exception:
            is_ortho = False
    thr = FACING_THRESH if FACING_THRESH > 1e-4 else 0.5
    w = [0.0] * n
    for i in range(n):
        p = pts[i]; nv = nrm[i]
        nl = (nv.x * nv.x + nv.y * nv.y + nv.z * nv.z) ** 0.5 or 1.0
        nx, ny, nz = nv.x / nl, nv.y / nl, nv.z / nl
        if is_ortho and vdir is not None:
            vx, vy, vz = vdir.x, vdir.y, vdir.z
        else:
            vx = cam_o.x - p.x; vy = cam_o.y - p.y; vz = cam_o.z - p.z
            vl = (vx * vx + vy * vy + vz * vz) ** 0.5
            if vl < 1e-9:
                continue
            vx, vy, vz = vx / vl, vy / vl, vz / vl
        d = nx * vx + ny * vy + nz * vz   # 符号付き: >0=手前(法線カメラ向き) / <0=奥(背面)
        # 裏面のみ表示では「見える面＝背面側(d<0)」。背面側の寝た面(-thr<=d<=0)は weight=1 で平らに保ち、
        # B を A の位置にぴったり重ねる（ランプにすると端で表面に戻る縁が薄い二重線になるため平ら）。
        # それより奥(d<-thr)は一気に裏へ沈めて隠す。正面側(d>0)は張り付き(裏面表示でカリング)。
        if d > 0.0:
            wt = 0.0                       # 正面側＝張り付き（前面はカリングで黒面なし）
        elif d >= -thr:
            wt = 1.0                       # 背面側の寝た面＝A にぴったり重ねる（隙間を埋める・二重線なし）
        else:
            wt = GAPFILL_TUCK              # 深い背面＝裏へ沈めて隠す
        w[i] = wt
    w = _smooth_vertex_values(dag, w, GAPFILL_SMOOTH_ITERS)
    _write_deformer_weights(defm, w)


def _line_weights(line):
    """コントローラーの curvature / curvatureCap / curvatureMin / 末端細り / プロファイルから
    頂点ウェイト（太さ倍率）のリストを計算して返す。背面法ハル(=deformer weightList)と
    スクリーン輪郭(=頂点カラー)で共通利用。occlusion は含まない（呼び出し側で合成）。"""
    ctrl = _ctrl_of(line)
    if not ctrl:
        return []
    try:
        influence = cmds.getAttr(ctrl + "." + CTRL_CURV)
        cap = cmds.getAttr(ctrl + "." + CTRL_CAP)
    except Exception:
        return []
    # 曲率下限: 曲率が小さい所（平らな所）の太さ倍率の基準。1.0=細くしない、<1.0で細く。
    cmin = DEFAULT_CMIN
    if cmds.attributeQuery(CTRL_CMIN, node=ctrl, exists=True):
        try:
            cmin = cmds.getAttr(ctrl + "." + CTRL_CMIN)
        except Exception:
            cmin = DEFAULT_CMIN
    curv = _line_curvature(line)
    n = len(curv)
    if n == 0:
        return []
    # 平らな所=cmin、曲がっている所ほど influence で増えて cap で頭打ち
    weights = [min(cap, max(0.0, cmin + influence * abs(c))) for c in curv]
    # 長手方向の太さ強弱（末端細り＋プロファイルカーブ）。長手 t を一度だけ算出して合成。
    taper = 0.0
    if cmds.attributeQuery(CTRL_TAPER, node=ctrl, exists=True):
        try:
            taper = cmds.getAttr(ctrl + "." + CTRL_TAPER)
        except Exception:
            taper = 0.0
    prof = _profile_of_ctrl(ctrl)
    use_prof = not _profile_is_flat(prof)
    if taper > 0.0 or use_prof:
        ts = _taper_factors(line)
        if len(ts) == n:
            for i in range(n):
                t = ts[i]
                if taper > 0.0:
                    d = t if t < 1.0 - t else 1.0 - t   # min(t,1-t)
                    weights[i] *= (1.0 - taper) + taper * (d / 0.5)
                if use_prof:
                    weights[i] *= _sample_profile(prof, t)
    # 末端細り/プロファイルでウェイトが 0 まで落ちるとチューブが基準半径(点)に潰れて
    # スピンドル状に尖る（場合により反転して -値に見える）。下限を入れて潰れを防ぐ。
    weights = [w if w > MIN_WEIGHT else MIN_WEIGHT for w in weights]
    return weights


def _write_deformer_weights(defm, weights):
    """deformer の weightList[0].weights[0..n-1] へ一括で書き込む（v0.1.52）。
    以前は cmds.setAttr に全頂点ぶんの値を渡していたが、4 万頂点で約 6〜8 秒かかり、
    さらに scriptJob から呼ぶと Undo 履歴に積まれて「チャンネルボックスで曲率を変えて
    Ctrl+Z 1 回」で戻らなかった。om2 で書くと速く（4 万頂点で 0.02 秒程度）Undo 履歴にも
    積まれない。重みは曲率などの属性から毎回計算し直す値なので、属性を Undo すれば
    scriptJob が走って重みも元に戻る。"""
    n = len(weights)
    if n == 0 or not defm:
        return False
    try:
        sl = om2.MSelectionList()
        sl.add(defm)
        fn = om2.MFnDependencyNode(sl.getDependNode(0))
        wp = fn.findPlug("weightList", False).elementByLogicalIndex(0).child(0)
    except Exception:
        return False
    try:
        h = wp.asMDataHandle()
        ah = om2.MArrayDataHandle(h)
        b = ah.builder()
        for i in range(n):
            b.addElement(i).setFloat(float(weights[i]))
        ah.set(b)
        wp.setMDataHandle(h)
        try:
            wp.destructHandle(h)
        except Exception:
            pass
        return True
    except Exception:
        pass
    # 予備: 1 要素ずつ（こちらも Undo 履歴には積まれない。4 万頂点で 0.15 秒程度）
    try:
        for i in range(n):
            wp.elementByLogicalIndex(i).setFloat(float(weights[i]))
        return True
    except Exception:
        return False


def _curv_sig(line):
    """曲率の重みを決める値の組。scriptJob が同じ値で何度も呼ばれたとき再計算を省くのに使う
    （UI の変更は直接計算したうえで属性ごとのジョブも走るため、省かないと 3〜4 回計算していた）。"""
    ctrl = _ctrl_of(line)
    if not ctrl:
        return None
    vals = []
    for at in (CTRL_CURV, CTRL_CAP, CTRL_CMIN, CTRL_TAPER, CTRL_PROFILE):
        try:
            vals.append(cmds.getAttr(ctrl + "." + at))
        except Exception:
            vals.append(None)
    vals.append(_mesh_vertex_count(line))
    return tuple(vals)


def _update_line_weights(line):
    """ライン種別に応じて曲率ウェイトの書き込み先を振り分ける（scriptJob のディスパッチャ）。"""
    if cmds.objExists(line) and cmds.attributeQuery(SCRN_TAG, node=line, exists=True):
        _update_scrn_curv_weights(line)
    else:
        _update_curv_weights(line)


def _update_curv_weights(line):
    """背面法ハル/エッジ: 曲率ウェイトを textureDeformer の weightList に書き込む。"""
    if not cmds.objExists(line):
        return
    defm = _line_deformer(line)
    if not defm:
        return
    sig = _curv_sig(line)
    weights = _line_weights(line)
    n = len(weights)
    if n == 0:
        return
    # 隠蔽検知ハル（カメラ依存）: 元メッシュに隠れた頂点の重みを内側へ寄せて裏面を隠す。
    # 可視（シルエット）頂点は係数 1.0 のままなので太さは保たれる。
    # 重なりマスクを付けたラインは単純な均一ハルとして使う（引き寄せの per-vertex ムラを
    # かけると二重になり太さが凸凹するため、マスク有のラインは occlusion 対象外）。
    if _OCC_ENABLED and _is_hull_line(line) and not _mask_of(line):
        occ = _occlusion_factors(line)
        if len(occ) == n:
            weights = [weights[i] * occ[i] for i in range(n)]
    if _write_deformer_weights(defm, weights):
        _CURV_SIG[_ukey(line)] = sig


def _init_scrn_color_set(shape):
    """スクリーン輪郭ラインに曲率用カラーセット(SCRN_CSET)を作り、全頂点を白(倍率1.0)で初期化。
    dx11 頂点シェーダの COLOR0.r がこの倍率を太さに乗算する。textureDeformer より下流に
    polyColorPerVertex を積む形になるので、変形後の頂点に対して倍率が残る。"""
    try:
        existing = cmds.polyColorSet(shape, q=True, allColorSets=True) or []
        if SCRN_CSET not in existing:
            cmds.polyColorSet(shape, create=True, colorSet=SCRN_CSET,
                              clamped=False, representation="RGBA")
        cmds.polyColorSet(shape, currentColorSet=True, colorSet=SCRN_CSET)
        cmds.polyColorPerVertex(shape + ".vtx[*]", r=1.0, g=1.0, b=1.0, a=1.0)
    except Exception:
        cmds.warning("スクリーン輪郭の曲率用カラーセット作成に失敗（曲率は一定太さになります）")


def _update_scrn_curv_weights(line):
    """スクリーン輪郭: 曲率倍率を頂点カラー R(=SCRN_CSET) に書き込む（シェーダが push に乗算）。
    ※ カラーセットが無い/頂点数不一致のときは何もしない（線は一定太さのまま=安全）。"""
    if not cmds.objExists(line):
        return
    if not cmds.attributeQuery(SCRN_TAG, node=line, exists=True):
        return
    shps = cmds.listRelatives(line, shapes=True, type="mesh", ni=True, f=True) or []
    if not shps:
        return
    shape = shps[0]
    sig = _curv_sig(line)
    weights = _line_weights(line)
    n = len(weights)
    if n == 0:
        return
    try:
        sl = om2.MSelectionList(); sl.add(shape)
        dag = sl.getDagPath(0)
        mfn = om2.MFnMesh(dag)
    except Exception:
        return
    try:
        if SCRN_CSET not in mfn.getColorSetNames():
            return
        mfn.setCurrentColorSetName(SCRN_CSET)
    except Exception:
        pass
    if n != mfn.numVertices:
        return
    # v0.1.53: R = 太さ倍率、GBA = 面の色（色トレス用。シェーダの traceAmount が 0 なら使わない）
    tc = _scrn_trace_colors(line)
    if len(tc) != n:
        tc = [(1.0, 1.0, 1.0)] * n
    cols = om2.MColorArray()
    verts = om2.MIntArray()
    for i in range(n):
        w = weights[i]
        t = tc[i]
        cols.append(om2.MColor((w, t[0], t[1], t[2])))
        verts.append(i)
    try:
        mfn.setVertexColors(cols, verts)
        _CURV_SIG[_ukey(line)] = sig
    except Exception:
        pass


_CURV_JOB_MARK = "_og_curv_job_cb"   # scriptJob 一覧でこのツールの曲率ジョブを見分ける関数名


def _mobj_handle(node):
    try:
        sl = om2.MSelectionList()
        sl.add(node)
        return om2.MObjectHandle(sl.getDependNode(0))
    except Exception:
        return None


def _curv_job_callback(line):
    """曲率ジョブの呼び出し先。ラインは MObjectHandle で持ち、呼ばれた時点のパスを引く
    （v0.1.52: 以前は作成時の名前を持っていたため、リネーム後は何もしなかった）。"""
    handle = _mobj_handle(line)

    def _og_curv_job_cb():
        if handle is None or not handle.isValid():
            return
        try:
            path = om2.MDagPath.getAPathTo(handle.object()).fullPathName()
        except Exception:
            return
        sig = _curv_sig(path)
        if sig is not None and _CURV_SIG.get(_ukey(path)) == sig:
            return      # 同じ値で計算済み（UI からの直接計算・同じ変更で走る他の属性のジョブ）
        _update_line_weights(path)
    return _og_curv_job_cb


def _kill_curv_jobs_everywhere():
    """このツール（古い版のモジュールも含む）が張った曲率ジョブをすべて消す。
    モジュールを読み込み直す・GitHub から更新すると、古いモジュールのジョブが残って
    同じ変更で何度も計算していた（v0.1.52）。"""
    killed = 0
    try:
        for j in cmds.scriptJob(listJobs=True) or []:
            # v0.1.51 以前は _ensure_curv_jobs 内の lambda
            if _CURV_JOB_MARK in j or "_ensure_curv_jobs" in j:
                try:
                    cmds.scriptJob(kill=int(j.split(":")[0]), force=True)
                    killed += 1
                except Exception:
                    pass
    except Exception:
        pass
    _CURV_JOBS.clear()
    return killed


def _ensure_curv_jobs(line):
    """controller の curvature / curvatureCap 変化で頂点ウェイトを再計算する scriptJob。
    （ノード削除時に自動 kill される attributeChange ジョブ）"""
    ctrl = _ctrl_of(line)
    if not ctrl:
        return
    key = _ukey(line)
    ch = _mobj_handle(ctrl)
    old = _CURV_JOBS.get(key)
    if old:
        jobs, oh = old
        # 同じ UUID でも別の実体（同じファイルを開き直した・リファレンスを読み直した）なら張り直す。
        # 消えたノードのジョブは一覧に残り続けるので、exists だけでは判定できない。
        try:
            same = (oh is not None and ch is not None and oh.isValid()
                    and oh.hashCode() == ch.hashCode())
            if same and all(cmds.scriptJob(exists=j) for j in jobs):
                return
        except Exception:
            pass
        _kill_job_ids(jobs)
    cb = _curv_job_callback(line)
    jobs = []
    # プロファイル（文字列）も見る: 重みは Undo 履歴に積まないので、プロファイルの変更を
    # Undo したときに重みを戻すのはこのジョブの役目（v0.1.52）
    for at in (CTRL_CURV, CTRL_CAP, CTRL_CMIN, CTRL_TAPER, CTRL_PROFILE):
        if not cmds.attributeQuery(at, node=ctrl, exists=True):
            continue
        try:
            jid = cmds.scriptJob(attributeChange=[ctrl + "." + at, cb])
            jobs.append(jid)
        except Exception:
            pass
    _CURV_JOBS[key] = (jobs, ch)


def _kill_job_ids(jobs):
    for j in jobs or []:
        try:
            if cmds.scriptJob(exists=j):
                cmds.scriptJob(kill=j, force=True)
        except Exception:
            pass


def _prune_curv_jobs():
    """もう無いライン（コントローラー）のジョブを片付ける。cmds.file(new=True) ではシーン切替の
    イベントが届かず（Maya 2024 で確認）、前のシーンのジョブが残るため。
    削除しても Undo で戻せる物（isAlive）は残す（消すと、削除を Undo したラインの曲率が効かなくなる）。"""
    for key, val in list(_CURV_JOBS.items()):
        jobs, h = val
        if h is None or not h.isAlive():
            _kill_job_ids(jobs)
            _CURV_JOBS.pop(key, None)


def _ensure_all_curv_jobs():
    """シーン内の全ライン（フレネル以外）に曲率ジョブを張る。シーンを開いた直後や
    モジュールを読み込み直した後に、ツールのウィンドウを開かなくても曲率のキーが効くように。"""
    _prune_curv_jobs()
    for t in cmds.ls(type="transform", long=True) or []:
        try:
            if (cmds.attributeQuery(TAG, node=t, exists=True)
                    and not cmds.attributeQuery(FRES_TAG, node=t, exists=True)
                    and _ctrl_of(t)):
                _ensure_curv_jobs(t)
        except Exception:
            pass


# v0.1.25: 種別ドットアイコン（カラー別にキャッシュ）
_TYPE_ICON_CACHE = {}

def _type_dot_icon(rgb):
    """ツリー項目の種別表現用カラードット QIcon。rgb は (r,g,b) in 0..255。キャッシュ。"""
    key = tuple(rgb)
    if key in _TYPE_ICON_CACHE:
        return _TYPE_ICON_CACHE[key]
    size = 14
    pix = QtGui.QPixmap(size, size)
    pix.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pix)
    p.setRenderHint(QtGui.QPainter.Antialiasing, True)
    p.setPen(QtGui.QPen(QtGui.QColor(30, 30, 30), 1))
    p.setBrush(QtGui.QBrush(QtGui.QColor(*key)))
    pad = 2
    p.drawEllipse(pad, pad, size - 2 * pad, size - 2 * pad)
    p.end()
    icon = QtGui.QIcon(pix)
    _TYPE_ICON_CACHE[key] = icon
    return icon


# v0.1.30: スピン/スライダーでマウスホイールによる値変更を抑止。
# パネルスクロール中の誤操作を防ぐ (UI_GUIDELINES §3)。
class _NoWheelFilter(QtCore.QObject):
    def eventFilter(self, obj, ev):
        if ev.type() == QtCore.QEvent.Wheel:
            ev.ignore()
            return True
        return False


# v0.1.39: 折りたたみセクション（アコーディオン）。
# UI_GUIDELINES §1「縦に長くなるのは構わない。ただし折りたためるセクションに分け、
# よく使うものを上にする」に従い、全機能を 4 セクションに整理する元部品。
# 見出しは ▼/▶ の三角 + タイトル。チェッカブルな QToolButton で開閉。
_COLLAPSIBLE_CSS = (
    "QToolButton#OG_Toonline_SectionHead {"
    " text-align: left; padding: 6px 8px; margin: 0;"
    " background: #333333; color: #e5c76b;"
    " border: 1px solid #2a2a2a; border-radius: 3px;"
    " font-weight: bold;"
    "}"
    "QToolButton#OG_Toonline_SectionHead:hover { background: #3a3a3a; }"
    "QToolButton#OG_Toonline_SectionHead:checked { background: #3a3220; }"
)


class _CollapsibleBox(QtWidgets.QWidget):
    """▼/▶ で開閉できる 1 セクション。中身は addLayout / addWidget で足す。"""
    def __init__(self, title, parent=None, expanded=True):
        super(_CollapsibleBox, self).__init__(parent)
        self._head = QtWidgets.QToolButton()
        self._head.setObjectName("OG_Toonline_SectionHead")
        self._head.setCheckable(True)
        self._head.setChecked(bool(expanded))
        self._head.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self._head.setArrowType(QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow)
        self._head.setText(title)
        self._head.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                                 QtWidgets.QSizePolicy.Fixed)
        self._head.clicked.connect(self._on_toggle)
        self._head.setStyleSheet(_COLLAPSIBLE_CSS)
        self._body = QtWidgets.QWidget()
        self._body_lay = QtWidgets.QVBoxLayout(self._body)
        self._body_lay.setContentsMargins(6, 4, 2, 6)
        self._body_lay.setSpacing(4)
        self._body.setVisible(bool(expanded))
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._head)
        outer.addWidget(self._body)

    def _on_toggle(self, checked):
        self._body.setVisible(bool(checked))
        self._head.setArrowType(QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)

    def add_layout(self, lay):
        self._body_lay.addLayout(lay)

    def add_widget(self, w):
        self._body_lay.addWidget(w)

    def body_layout(self):
        return self._body_lay


class _RampWidget(QtWidgets.QWidget):
    """長手方向の太さプロファイルを編集するカーブ（ランプ）ウィジェット。
    左クリック=点の追加/ドラッグ、右クリック=点の削除。y は太さ倍率(0〜2、基準1)。"""
    valueChanged = QtCore.Signal()
    dragStarted = QtCore.Signal()    # v0.1.53: 左ボタンを押した（ここから離すまでを Undo 1 回にまとめる）
    dragFinished = QtCore.Signal()   # 左ボタンを離した

    def __init__(self, parent=None):
        super(_RampWidget, self).__init__(parent)
        self.setMinimumHeight(90)
        self.setMinimumWidth(180)
        self._pts = [[0.0, 1.0], [1.0, 1.0]]
        self._drag = -1
        self._pressed = False
        self._m = 6
        self._ymax = 2.0

    def points(self):
        return [list(p) for p in self._pts]

    def set_points(self, pts):
        if pts and len(pts) >= 2:
            self._pts = sorted([[max(0.0, min(1.0, p[0])), max(0.0, min(self._ymax, p[1]))]
                                for p in pts], key=lambda p: p[0])
        else:
            self._pts = [[0.0, 1.0], [1.0, 1.0]]
        self.update()

    def _evt_xy(self, e):
        try:
            pt = e.position(); return pt.x(), pt.y()       # PySide6
        except AttributeError:
            return float(e.x()), float(e.y())               # PySide2

    def _to_px(self, x, y):
        w = max(1, self.width() - 2 * self._m); h = max(1, self.height() - 2 * self._m)
        return (self._m + x * w, self._m + (1.0 - y / self._ymax) * h)

    def _to_norm(self, px, py):
        w = max(1, self.width() - 2 * self._m); h = max(1, self.height() - 2 * self._m)
        x = (px - self._m) / w
        y = (1.0 - (py - self._m) / h) * self._ymax
        return max(0.0, min(1.0, x)), max(0.0, min(self._ymax, y))

    def _hit(self, px, py):
        for i, (x, y) in enumerate(self._pts):
            ax, ay = self._to_px(x, y)
            if abs(ax - px) < 8 and abs(ay - py) < 8:
                return i
        return -1

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        # v0.1.25: ダーク UI と馴染ませる配色（控えめ）
        p.fillRect(self.rect(), QtGui.QColor(38, 38, 38))
        p.setPen(QtGui.QPen(QtGui.QColor(74, 74, 74), 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        p.setPen(QtGui.QColor(80, 80, 80))
        x0, by = self._to_px(0.0, 1.0); x1, _ = self._to_px(1.0, 1.0)
        p.drawLine(int(x0), int(by), int(x1), int(by))   # 基準 y=1
        p.setPen(QtGui.QPen(QtGui.QColor(170, 170, 170), 2))   # カーブ: 中間グレー（景観に馴染む）
        prev = None
        steps = 60
        for k in range(steps + 1):
            x = k / float(steps)
            y = _sample_profile(self._pts, x)
            px, py = self._to_px(x, y)
            if prev is not None:
                p.drawLine(int(prev[0]), int(prev[1]), int(px), int(py))
            prev = (px, py)
        # ハンドル: 暖色イエロー（UI プライマリ色に合わせる・小さめ）
        p.setPen(QtGui.QPen(QtGui.QColor(30, 30, 30), 1))
        p.setBrush(QtGui.QBrush(QtGui.QColor(201, 168, 72)))
        for x, y in self._pts:
            px, py = self._to_px(x, y)
            p.drawEllipse(QtCore.QPointF(px, py), 4, 4)
        p.end()

    def mousePressEvent(self, e):
        px, py = self._evt_xy(e)
        i = self._hit(px, py)
        if e.button() == QtCore.Qt.RightButton:
            if 0 < i < len(self._pts) - 1:
                del self._pts[i]
                self.update(); self.valueChanged.emit()
            return
        if e.button() != QtCore.Qt.LeftButton:
            return
        added = False
        if i < 0:
            x, y = self._to_norm(px, py)
            self._pts.append([x, y]); self._pts.sort(key=lambda q: q[0])
            i = self._hit(*self._to_px(x, y))
            added = True
        self._drag = i
        if not self._pressed:
            self._pressed = True
            self.dragStarted.emit()
        if added:
            # v0.1.53: クリックで足した点もすぐ反映する（以前は動かすまでシーンに入らなかった）
            self.update(); self.valueChanged.emit()

    def mouseMoveEvent(self, e):
        if self._drag < 0:
            return
        px, py = self._evt_xy(e)
        x, y = self._to_norm(px, py)
        if self._drag == 0:
            x = 0.0
        elif self._drag == len(self._pts) - 1:
            x = 1.0
        self._pts[self._drag] = [x, y]
        self._pts.sort(key=lambda q: q[0])
        self._drag = self._hit(*self._to_px(x, y))
        self.update(); self.valueChanged.emit()

    def mouseReleaseEvent(self, e):
        self._drag = -1
        self._finish_press()

    def hideEvent(self, e):
        # 押したまま隠れた（パネルの切り替えなど）ときも Undo のまとまりを閉じる
        self._drag = -1
        self._finish_press()
        super(_RampWidget, self).hideEvent(e)

    def _finish_press(self):
        if self._pressed:
            self._pressed = False
            self.dragFinished.emit()


class _OutlineTree(QtWidgets.QTreeWidget):
    """ライン→グループのドラッグ&ドロップ移動に対応したツリー。"""
    def __init__(self, ui, parent=None):
        super(_OutlineTree, self).__init__(parent)
        self.ui = ui

    def keyPressEvent(self, event):
        if event.key() in (QtCore.Qt.Key_Delete, QtCore.Qt.Key_Backspace):
            self.ui.delete_selected()
            event.accept()
            return
        super(_OutlineTree, self).keyPressEvent(event)

    def dropEvent(self, event):
        # ドロップ先のグループを解決し、選択ライン群を Maya 側で親子付けし直す。
        try:
            pos = event.position().toPoint()   # PySide6
        except AttributeError:
            pos = event.pos()                  # PySide2
        target = self.itemAt(pos)
        group = self.ui._group_of_item(target)
        lines = self.ui._selected_lines()
        # Qt 既定の InternalMove に任せると、Move を accept した時点で startDrag が
        # ソース項目を削除する。空白にドロップ（移動先なし）すると再構築されないまま
        # 項目が消えるため、既定の移動は行わせず必ず IgnoreAction にして、
        # 親子付けは Maya 側で行い refresh_tree でツリーを作り直す。
        event.setDropAction(QtCore.Qt.IgnoreAction)
        event.ignore()
        if group and lines:
            self.ui._move_lines_to(lines, group)   # 内部で refresh_tree
        else:
            self.ui.refresh_tree()                 # 空白等への無効ドロップは元の状態へ復元


class ToonOutlineUI(QtWidgets.QDialog):

    def __init__(self, parent=None):
        if parent is None:
            parent = _maya_main()
        super(ToonOutlineUI, self).__init__(parent)
        self.setWindowTitle("OG_Toonline_Manager  —  v{0}".format(__version__))
        self.setObjectName(WINDOW_OBJ)
        # v0.1.20: ダークグレー背景 + 暖色イエロー配色（objectName セット後に適用）
        try:
            self.setStyleSheet(_DIALOG_BG_CSS + _TITLE_CSS)
        except Exception:
            pass
        self.setMinimumWidth(340)   # v0.1.30: UI_GUIDELINES §1 - ドッキング幅 ~330px
        # v0.1.39: アコーディオン + QScrollArea 化。縮めてもセクション見出しと
        # ヘッダー＋全体倍率さえ見えれば可（中身はスクロール）。
        self.setMinimumHeight(360)
        self._color = [0.0, 0.0, 0.0]
        self._populating = False       # ツリー再構築中のシグナル抑止フラグ
        self._dragging = False         # スライダードラッグ中（undoチャンク制御）
        self._time_job = None          # timeChanged scriptJob
        self._occ_timer = None         # 隠蔽検知ハル: カメラ移動監視の QTimer
        self._occ_cam_key = None       # 最後に処理したカメラ位置/行列のキー（変化検知用）
        self._occ_busy = False         # 再計算中フラグ（処理の重畳＝ビューポート固着を防ぐ）
        self._occ_cb_ids = []          # カメラ worldMatrix 変化コールバック id（om2）
        self._occ_scheduled = False    # 次イベントループでの再計算予約済みフラグ
        self._warned_connected = set() # 接続済みで設定不可と警告済みのプラグ（選択変更でクリア）
        self._sel_job = None           # v0.1.53: ビューポートの選択 → 一覧の選択（SelectionChanged）
        self._syncing_sel = False      # 一覧をビューポートの選択に合わせている最中（Maya の選択を変えない）
        self._src_to_lines = {}        # 元メッシュ → ライン（refresh_tree で作り直す）
        self._build()
        # v0.1.30: 全スピン/スライダーでホイール値変更を抑止（UI_GUIDELINES §3）
        self._no_wheel = _NoWheelFilter(self)
        for w in self.findChildren(QtWidgets.QAbstractSpinBox):
            w.installEventFilter(self._no_wheel)
            w.setFocusPolicy(QtCore.Qt.StrongFocus)
        for w in self.findChildren(QtWidgets.QSlider):
            w.installEventFilter(self._no_wheel)
        # v0.1.52: グループ選択のコンボもホイールで勝手に変わらないように（詳細設定ウィンドウと同じ）
        for w in self.findChildren(QtWidgets.QComboBox):
            w.installEventFilter(self._no_wheel)
            w.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.refresh_tree()
        self._update_panels()      # 初期は選択無し → ライン/グループパネル非表示
        self._set_mult_widgets()
        try:
            self._time_job = cmds.scriptJob(event=["timeChanged", self._on_time_changed],
                                            protected=False)
        except Exception:
            self._time_job = None
        try:
            self._sel_job = cmds.scriptJob(event=["SelectionChanged", self._on_maya_selection],
                                           protected=False)
        except Exception:
            self._sel_job = None

    def closeEvent(self, event):
        self._teardown()
        # v0.1.39: importlib.reload 後は ToonOutlineUI シンボルが別クラスに差し
        # 変わって古いインスタンスが super(ToonOutlineUI, self) を解決できず
        # TypeError になる。QDialog へ直接呼んで回避。
        QtWidgets.QDialog.closeEvent(self, event)

    def reject(self):
        # v0.1.52: Esc（QDialog の既定動作）は reject() でウィンドウを隠すだけで closeEvent が
        # 呼ばれず、フレーム変更の監視などが残っていた。閉じるときと同じ後片付けをする。
        self._teardown()
        QtWidgets.QDialog.reject(self)

    def _teardown(self):
        """ウィンドウを閉じる/隠すときの後片付け（何度呼んでもよい）。"""
        for attr in ("_time_job", "_sel_job"):
            jid = getattr(self, attr, None)
            try:
                if jid and cmds.scriptJob(exists=jid):
                    cmds.scriptJob(kill=jid, force=True)
            except Exception:
                pass
            setattr(self, attr, None)
        try:
            if self._occ_timer is not None:
                self._occ_timer.stop()
        except Exception:
            pass
        try:
            self._remove_cam_callbacks()
        except Exception:
            pass
        try:
            # 交差線/旧エッジは UI と一緒に止める。隠れ線はシーンの設定なので、
            # ウィンドウを閉じてもプレイブラストに出るよう残す（v0.1.46）。
            _OVR_STATE["edge"] = False
            _OVR_STATE["intersect"] = False
            _og_override_sync()
        except Exception:
            pass
        try:
            if _line_options_win[0] is not None:
                _line_options_win[0].close()
        except Exception:
            pass

    # ========== UI 構築 ==========
    def _slider_spin_row(self, label, smin, smax, sval, dec, rmin, rmax, step, rval,
                         on_slider, on_spin, on_reset, on_key=None, tip=""):
        """ラベル+スライダー+スピン+[キー]+[リセット]の1行を作り (layout, slider, spin) を返す。"""
        row = QtWidgets.QHBoxLayout()
        lab = QtWidgets.QLabel(label)
        row.addWidget(lab)
        self._last_row_label = lab     # v0.1.54: 呼び出し側でラベルを使うとき用（itemAt は使わない）
        sld = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        sld.setRange(smin, smax); sld.setValue(sval)
        spn = QtWidgets.QDoubleSpinBox()
        spn.setDecimals(dec); spn.setRange(rmin, rmax); spn.setSingleStep(step); spn.setValue(rval)
        # v0.1.34: 330px ドック時にスライダーが伸びて数値欄が潰れ、
        # 「開いた直後に数字が見えない」問題が出ていた。固定幅で確実に桁が出るようにする。
        spn.setFixedWidth(64)
        if tip:
            spn.setToolTip(tip)
        sld.valueChanged.connect(on_slider)
        sld.sliderPressed.connect(self._begin_drag)
        sld.sliderReleased.connect(self._end_drag)
        spn.valueChanged.connect(on_spin)
        row.addWidget(sld); row.addWidget(spn)
        if on_key:
            bk = QtWidgets.QPushButton("K"); bk.setFixedWidth(22)
            bk.setToolTip("この値を現フレームにキー")
            bk.clicked.connect(on_key)
            row.addWidget(bk)
        b = QtWidgets.QPushButton("↺"); b.setFixedWidth(26); b.setToolTip("リセット")
        b.clicked.connect(on_reset)
        row.addWidget(b)
        return row, sld, spn

    def _build(self):
        # v0.1.39: 4 セクションのアコーディオン化。
        #   固定上部: ヘッダー + 全体倍率
        #   QScrollArea 内: ▼作る / ▶風合い・特殊 / ▼調整 / ▶カラー・管理
        # ウィンドウを縮めても潰れず、下にスクロールする。
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        # v0.1.21: ヘッダー: 左にタイトル+バージョン小、右に ⋮ メニュー（SIMFLOW 風）。
        hrow = QtWidgets.QHBoxLayout()
        hrow.setContentsMargins(0, 0, 0, 0)
        self.lbl_title = QtWidgets.QLabel("OG TOONLINE")
        self.lbl_title.setObjectName("OG_Toonline_TitleLabel")
        self.lbl_title.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        hrow.addWidget(self.lbl_title)
        self.lbl_header_ver = QtWidgets.QLabel("v{0}".format(__version__))
        self.lbl_header_ver.setObjectName("OG_Toonline_HeaderVer")
        self.lbl_header_ver.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignBottom)
        hrow.addWidget(self.lbl_header_ver)
        hrow.addStretch(1)
        # ⋮ メニューボタン
        self.btn_menu = QtWidgets.QToolButton()
        self.btn_menu.setObjectName("OG_Toonline_MenuBtn")
        self.btn_menu.setText(u"⋮")   # U+22EE vertical ellipsis
        self.btn_menu.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.btn_menu.setToolTip("メニュー（バージョン選択・情報など）")
        self.btn_menu.setFixedWidth(28)
        menu = QtWidgets.QMenu(self)
        # v0.1.53: 線の設定（太さ・曲率・色・見え方・ブラシ・紐づけ・レイヤーごとの上書き）の保存/読み込み
        act_exp = menu.addAction("線の設定を書き出す…")
        act_exp.setToolTip("一覧で選んだ線（選んでいなければ全部）の設定をファイルに書き出します。")
        act_exp.triggered.connect(self.export_settings_dialog)
        act_imp = menu.addAction("線の設定を読み込む…")
        act_imp.setToolTip("書き出した設定を、元のメッシュの名前が同じ線に当てます"
                           "（一覧で選んでいればその線だけ）。名前空間は無視します。")
        act_imp.triggered.connect(self.import_settings_dialog)
        menu.addSeparator()
        act_ver = menu.addAction("バージョン選択…")
        act_ver.setToolTip("GitHub Releases/開発ブランチ先端から任意バージョンに切替。Maya 再起動不要。")
        act_ver.triggered.connect(open_version_dialog)
        act_upd = menu.addAction("最新を取得")
        act_upd.setToolTip("開発ブランチの先端を即座にインストール。")
        act_upd.triggered.connect(update_from_github)
        menu.addSeparator()
        act_about = menu.addAction("バージョン情報…")
        act_about.triggered.connect(self._show_about)
        self.btn_menu.setMenu(menu)
        hrow.addWidget(self.btn_menu)
        outer.addLayout(hrow)

        # 全体倍率（常に最上部・スクロール外）。全ライン太さの大元なのでいつでも触れる位置に。
        self.w_global = QtWidgets.QWidget()
        gl = QtWidgets.QVBoxLayout(self.w_global); gl.setContentsMargins(0, 0, 0, 0)
        arow, self.gslider, self.gspin = self._slider_spin_row(
            "全体倍率", 0, 500, 100, 2, 0.0, 5.0, 0.05, 1.0,
            self._on_gslider, self._on_gspin, self._reset_gmult,
            on_key=self._key_gmult, tip="全アウトラインの太さ倍率")
        b_gsel = QtWidgets.QPushButton("選択")
        b_gsel.setFixedWidth(40)
        b_gsel.setToolTip("全体コントローラーを選択（タイムスライダにキーを表示）")
        b_gsel.clicked.connect(self._select_global_ctrl)
        arow.addWidget(b_gsel)
        # v0.1.54: リファレンスしたキャラの線を選んでいるときは、そのキャラの全体倍率になる（ラベルで示す）
        # ※ layout.itemAt(i) の戻り値を Python で持つと、ウィンドウを破棄するときに Maya ごと落ちることが
        #   あるので、ラベルは作った時点の参照を使う
        self.lbl_global = getattr(self, "_last_row_label", None)
        gl.addLayout(arow)
        outer.addWidget(self.w_global)

        # ---- スクロール可能な本体。4 セクションのアコーディオン ----
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        body = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # ================ セクション 1: 作る ================
        sec_make = _CollapsibleBox("作る", expanded=True)

        # 生成 / 新規グループ / 再取得（上部）
        # ★v0.1.10: UI 整理。SCRN(スクリーン輪郭)をメインに据え、hull/エッジ/GAPFILL は
        # UI から外した(内部コード・関数は残置、再公開時は buttons を戻すだけで動く)。
        crow = QtWidgets.QHBoxLayout()
        self.btn_create = QtWidgets.QPushButton("選択メッシュに輪郭を生成")
        self.btn_create.setToolTip(
            "選択メッシュにスクリーン空間輪郭(SCRN)を生成する。\n"
            "dx11 頂点シェーダでクリップ空間に一定 px 押し出し+距離 attenuation で、\n"
            "どの距離・どのサイズのメッシュでも安定した輪郭を描く(Pencil+4 風)。\n"
            "※ VP2/DirectX11 前提・カメラ依存。ビューポート『テクスチャ表示 ON(ホットキー 6)』で表示。")
        self.btn_create.clicked.connect(self.create_screen_outline)
        self.btn_create.setStyleSheet(_PRIMARY_BTN_CSS)
        crow.addWidget(self.btn_create, 1)
        sec_make.add_layout(crow)
        # v0.1.30: 補助ボタンは 2 段目に（UI_GUIDELINES §1 - 1 行は 2〜3 項目まで）。
        crow2 = QtWidgets.QHBoxLayout()
        b_edge = QtWidgets.QPushButton("選んだエッジに線")
        b_edge.setToolTip(
            "選択したポリゴンエッジに沿って 1 本の線を追加。\n"
            "太さ/曲率/プロファイルは、作成後にツリーで選んで下のパネルで調整。")
        b_edge.clicked.connect(self.create_edge_line)
        crow2.addWidget(b_edge)
        b_grp = QtWidgets.QPushButton("新しいグループ")
        b_grp.setToolTip("線をまとめるためのグループを作る。")
        b_grp.clicked.connect(self.new_group)
        crow2.addWidget(b_grp)
        b_ref = QtWidgets.QPushButton("一覧更新")
        b_ref.setToolTip("ツリーの内容をシーンから再読み込みする。")
        b_ref.clicked.connect(self.refresh_tree)
        crow2.addWidget(b_ref)
        sec_make.add_layout(crow2)

        # v0.1.38: マテリアル境界ライン（面ごとの SG 違いを検出 → チューブ化）
        crow4 = QtWidgets.QHBoxLayout()
        self.btn_matbound = QtWidgets.QPushButton("マテリアル境界にライン")
        self.btn_matbound.setToolTip(
            "選択メッシュの、隣接 2 面の shadingEngine（マテリアル割り当て）が\n"
            "違うエッジを自動検出してチューブ状のラインを生成する。\n"
            "・単一 SG のメッシュは対象外（境界が存在しないため）\n"
            "・カメラ非依存・DG ベース＝VP2/全レンダラー/バッチ/OpenGL 対応\n"
            "・ライン本体は既存のエッジラインと同じ機能（太さ/曲率/プロファイル）")
        self.btn_matbound.clicked.connect(self.create_material_boundary_lines)
        crow4.addWidget(self.btn_matbound)
        sec_make.add_layout(crow4)

        # v0.1.39: 旧 QGroupBox「新規作成」の中身はセクション「作る」直下へ。
        # 区切り線で "生成" と "新規作成オプション" の境を見せる。
        _sep1 = QtWidgets.QFrame()
        _sep1.setFrameShape(QtWidgets.QFrame.HLine)
        _sep1.setStyleSheet("color:#4a4a4a;")
        sec_make.add_widget(_sep1)

        # フレネル生成チェック
        self.chk_scrn_fresnel = QtWidgets.QCheckBox("谷の線も出す（フレネル・滑らかな凹み用）")
        self.chk_scrn_fresnel.setChecked(False)   # v0.1.30: 既定 OFF（必要なときだけ）
        sec_make.add_widget(self.chk_scrn_fresnel)

        # ---- クリース関連（QFrame ネスト撤去して layout を直に）----
        crease_row = QtWidgets.QHBoxLayout()
        self.chk_scrn_crease = QtWidgets.QCheckBox("折り目の線も出す（クリース・硬いエッジ）")
        self.chk_scrn_crease.setChecked(False)   # v0.1.30: 既定 OFF（必要なときだけ）
        crease_row.addWidget(self.chk_scrn_crease)
        crease_row.addWidget(QtWidgets.QLabel("角度"))
        self.spn_crease_angle = QtWidgets.QDoubleSpinBox()
        self.spn_crease_angle.setRange(1.0, 179.0)
        self.spn_crease_angle.setDecimals(0)
        self.spn_crease_angle.setSingleStep(5.0)
        self.spn_crease_angle.setValue(CREASE_ANGLE)
        self.spn_crease_angle.setSuffix("°")
        self.spn_crease_angle.setFixedWidth(70)
        crease_row.addWidget(self.spn_crease_angle)
        crease_row.addStretch(1)
        sec_make.add_layout(crease_row)

        # v0.1.28: 生成時設定は最小限に。作成後に下のパネルで調整が自然な workflow。
        # クリース生成テンプレのランプは撤去（下の「太さプロファイル」パネルで複数選択→
        # 一括適用で対応する）。
        def _crease_enabled(on):
            self.spn_crease_angle.setEnabled(on)
        self.chk_scrn_crease.toggled.connect(_crease_enabled)
        _crease_enabled(self.chk_scrn_crease.isChecked())

        # 新しく作る線の入れ先グループ（複数グループがある時のみ意味がある）
        gcrow = QtWidgets.QHBoxLayout()
        gcrow.addWidget(QtWidgets.QLabel("新しい線の入れ先"))
        self.group_combo = QtWidgets.QComboBox()
        self.group_combo.setMinimumWidth(110)
        self.group_combo.setToolTip(
            "次に作る線をどのグループに入れるかを選ぶ。\n"
            "グループが 1 つだけならいじらなくてよい（既定で OK）。\n"
            "※ クリース線は常に専用の『クリース』グループに入ります。")
        self.group_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.group_combo.setMinimumContentsLength(6)
        gcrow.addWidget(self.group_combo, 1)
        sec_make.add_layout(gcrow)

        lay.addWidget(sec_make)

        # ================ セクション 2: 風合い・特殊 ================
        sec_feel = _CollapsibleBox("風合い・特殊", expanded=False)

        # フレネル感度（既存全フレネル線に live で乗算）
        fs_row = QtWidgets.QHBoxLayout()
        fs_row.addWidget(QtWidgets.QLabel("谷の線の広さ"))
        self.spn_fres_sens = QtWidgets.QDoubleSpinBox()
        self.spn_fres_sens.setRange(0.0, 10.0)
        self.spn_fres_sens.setDecimals(2)
        self.spn_fres_sens.setSingleStep(0.1)
        self.spn_fres_sens.setValue(1.0)
        self.spn_fres_sens.setFixedWidth(80)
        self.spn_fres_sens.setToolTip(
            "谷っぽい凹みに出る線（フレネル線）の広さ。\n"
            "線の太さは変えずに「どこまで奥まで線を出すか」を調整。\n"
            "0.0 = 線なし / 0.5 = フチ際だけ / 1.0 = 既定 / 2.0 = 深い谷まで\n"
            "全フレネル線に同時に効く。")
        self.spn_fres_sens.valueChanged.connect(self._on_fres_sens)
        fs_row.addWidget(self.spn_fres_sens)
        fs_row.addStretch(1)
        sec_feel.add_layout(fs_row)

        # 重なり線の強さ（元 SCRN の depthBias）
        srow = QtWidgets.QHBoxLayout()
        srow.addWidget(QtWidgets.QLabel("重なり線の強さ"))
        self.spn_scrn_bias = QtWidgets.QDoubleSpinBox()
        self.spn_scrn_bias.setRange(-0.5, 0.5); self.spn_scrn_bias.setDecimals(4)
        self.spn_scrn_bias.setSingleStep(0.0005); self.spn_scrn_bias.setValue(_SCRN_ZBIAS)
        self.spn_scrn_bias.setToolTip(
            "物が重なった所に輪郭線を出すかどうか。既定 0 = 出さない。\n"
            "0.0 = 自然な遮蔽（手前が奥を隠す）\n"
            "0.001〜0.005 = 遠くても重なり線が見える\n"
            "大きすぎると他のメッシュに線が貫通して見える。")
        self.spn_scrn_bias.valueChanged.connect(self._on_scrn_bias)
        srow.addWidget(self.spn_scrn_bias)
        srow.addStretch(1)
        sec_feel.add_layout(srow)

        # ブラシ質感（v0.1.49: 全ラインのマスター値へ一括で入れる。ラインごと・レイヤーごとの
        # 入り抜き/かすれ/よれ/二重線/プリセットはライン詳細設定の『ブラシ』タブで）
        brow = QtWidgets.QHBoxLayout()
        brow.addWidget(QtWidgets.QLabel("ブラシ:カサつき"))
        self.spn_brush_rough = QtWidgets.QDoubleSpinBox()
        self.spn_brush_rough.setRange(0.0, 1.0); self.spn_brush_rough.setDecimals(2)
        self.spn_brush_rough.setSingleStep(0.05); self.spn_brush_rough.setValue(_SCRN_BRUSH_ROUGH_DEF)
        self.spn_brush_rough.setToolTip(
            "全ラインに一括で入れる: 線の縁をランダムに欠けさせてペンシル風の毛羽立ちを出す。\n"
            "0=クリーン、1=強いカサつき。線の中心は残るので視認性は保たれる。\n"
            "ラインごとに変えるときは右クリック →『ライン詳細設定』の『ブラシ』。")
        self.spn_brush_rough.valueChanged.connect(lambda v: _scrn_set_brush(rough=v))
        brow.addWidget(self.spn_brush_rough)
        brow.addWidget(QtWidgets.QLabel("ゆらぎ"))
        self.spn_brush_wave = QtWidgets.QDoubleSpinBox()
        self.spn_brush_wave.setRange(0.0, 1.0); self.spn_brush_wave.setDecimals(2)
        self.spn_brush_wave.setSingleStep(0.05); self.spn_brush_wave.setValue(_SCRN_BRUSH_WAVE_DEF)
        self.spn_brush_wave.setToolTip(
            "全ラインに一括で入れる: 線の太さをノイズで長手方向に揺らす（鉛筆の筆圧ゆらぎ風）。\n"
            "0=均一、1=大きなゆらぎ。")
        self.spn_brush_wave.valueChanged.connect(lambda v: _scrn_set_brush(wave=v))
        brow.addWidget(self.spn_brush_wave)
        brow.addWidget(QtWidgets.QLabel("周期"))
        self.spn_brush_scale = QtWidgets.QDoubleSpinBox()
        self.spn_brush_scale.setRange(1.0, 100.0); self.spn_brush_scale.setDecimals(1)
        self.spn_brush_scale.setSingleStep(1.0); self.spn_brush_scale.setValue(_SCRN_BRUSH_SCALE_DEF)
        self.spn_brush_scale.setToolTip(
            "全ラインに一括で入れる: ノイズの細かさ。大きいほど細かくザラッ、小さいほど大きなうねり。")
        self.spn_brush_scale.valueChanged.connect(lambda v: _scrn_set_brush(scale=v))
        brow.addWidget(self.spn_brush_scale)
        brow.addStretch(1)
        sec_feel.add_layout(brow)

        # v0.1.33: チラつき抑制（時間的安定性）
        stab_row = QtWidgets.QHBoxLayout()
        stab_row.addWidget(QtWidgets.QLabel("チラつき抑制"))
        self.spn_stability = QtWidgets.QDoubleSpinBox()
        self.spn_stability.setRange(0.0, 1.0)
        self.spn_stability.setDecimals(2)
        self.spn_stability.setSingleStep(0.1)
        self.spn_stability.setValue(0.0)
        self.spn_stability.setFixedWidth(80)
        self.spn_stability.setToolTip(
            "カメラを動かしたときに境界付近の線がチカチカするのを抑える。\n"
            "0 = 無効 / 0.5 = 中程度 / 1.0 = 強め\n"
            "高い値にすると線が薄くなったり小さな隙間が出ることがある。")
        self.spn_stability.valueChanged.connect(
            lambda v: _scrn_set_stability(v))
        stab_row.addWidget(self.spn_stability)
        stab_row.addStretch(1)
        sec_feel.add_layout(stab_row)

        # v0.1.32: 遠景で細くする（距離減衰・SCRN のみ）
        dist_row = QtWidgets.QHBoxLayout()
        dist_row.addWidget(QtWidgets.QLabel("遠景で細くする"))
        self.spn_dist_atten = QtWidgets.QDoubleSpinBox()
        self.spn_dist_atten.setRange(0.0, 1.0)
        self.spn_dist_atten.setDecimals(2)
        self.spn_dist_atten.setSingleStep(0.1)
        self.spn_dist_atten.setValue(0.0)
        self.spn_dist_atten.setFixedWidth(80)
        self.spn_dist_atten.setToolTip(
            "カメラから遠い輪郭線を細くする度合い（SCRN 輪郭線のみ）。\n"
            "0 = 全て同じ太さ / 0.5 = 中程度 / 1.0 = 遠景で大きく細る\n"
            "カメラを動かすとリアルタイムで反映。")
        self.spn_dist_atten.valueChanged.connect(self._on_dist_atten_changed)
        dist_row.addWidget(self.spn_dist_atten)
        dist_row.addStretch(1)
        sec_feel.add_layout(dist_row)

        # v0.1.36: 交差線（他オブジェクトとの重なりに線）
        ix_row = QtWidgets.QHBoxLayout()
        self.chk_intersect = QtWidgets.QCheckBox("交差線（選んだメッシュと他の重なり）")
        self.chk_intersect.setChecked(False)
        self.chk_intersect.setToolTip(
            "ON で、ビューポートで選んだメッシュと他のメッシュが『実際に交差している』\n"
            "位置に画面空間で線を出す（SCRN とは別の後処理レイヤー）。\n"
            "・選んだメッシュを対象、それ以外のメッシュ(ラインは除外)を『他』として比較\n"
            "・両方の深度が近接しかつ隣接ピクセルで前後が反転する所＝実交差\n"
            "・VP2/DirectX11・テクスチャ表示 ON (ホットキー 6) 前提\n"
            "・カメラ依存、プレイブラスト対応、他レンダラー不可")
        self.chk_intersect.toggled.connect(self._on_toggle_intersect)
        ix_row.addWidget(self.chk_intersect)
        ix_row.addStretch(1)
        ix_row.addWidget(QtWidgets.QLabel("敏感度"))
        self.spn_intersect_thr = QtWidgets.QDoubleSpinBox()
        self.spn_intersect_thr.setRange(0.001, 0.2); self.spn_intersect_thr.setDecimals(3)
        self.spn_intersect_thr.setSingleStep(0.005); self.spn_intersect_thr.setValue(0.010)
        self.spn_intersect_thr.setFixedWidth(64)
        self.spn_intersect_thr.setToolTip(
            "交差線の検出しきい値（相対深度差）。\n"
            "小 = 厳密な交差だけ / 大 = 近接も交差扱い（線が太く・多く）\n"
            "0.005〜0.03 が実用域。")
        self.spn_intersect_thr.valueChanged.connect(
            lambda v: _edge_set_params(intersect_threshold=float(v)))
        ix_row.addWidget(self.spn_intersect_thr)
        sec_feel.add_layout(ix_row)

        # v0.1.46: 隠れ線は「紐づけた物の裏」だけに出る。紐づけ・破線/色はライン詳細設定で。
        # ここはシーン全体の ON/OFF だけ（シーンに保存。プレイブラストにも効く）。
        self.chk_hidden_dash = QtWidgets.QCheckBox("隠れ線を透かして描く（紐づけた物の裏）")
        self.chk_hidden_dash.setChecked(_hidden_scene_on())
        self.chk_hidden_dash.setToolTip(
            "ON: ラインを『紐づけた物』の裏に回った所だけ、隠れ線として透かして描きます。\n"
            "    例: 眉のラインに前髪を紐づけると、前髪越しに眉が透けて見える。\n"
            "    紐づけていない物（頭など）の裏では隠れ線は出ません。\n"
            "OFF: シーン全体で隠れ線を描きません。\n"
            "紐づけ・破線/実線・色は、ライン一覧で右クリック →『ライン詳細設定』。\n"
            "DirectX11 のビューポートのみ（プレイブラストにも出ます）。")
        self.chk_hidden_dash.toggled.connect(self._on_toggle_hidden_dash)
        sec_feel.add_widget(self.chk_hidden_dash)

        # v0.1.26: ライン単体表示モード（メッシュを useBackground で透明に・奥側線を隠蔽）
        self.chk_line_only = QtWidgets.QCheckBox(
            "ライン単体表示（メッシュを透明化・奥側の線は隠す）")
        self.chk_line_only.setChecked(False)
        self.chk_line_only.setToolTip(
            "ON: 全アウトライン対象メッシュに useBackground シェーダを割当。\n"
            "メッシュは色が消えて深度だけ書くので、メッシュの奥側にあるラインは\n"
            "正しく隠蔽される（メッシュを単純に非表示にすると奥側まで見えてしまう）。\n"
            "OFF: 元のシェーダに復帰。")
        self.chk_line_only.toggled.connect(self._on_toggle_line_only)
        sec_feel.add_widget(self.chk_line_only)

        lay.addWidget(sec_feel)

        # ================ セクション 3: 調整 ================
        sec_adjust = _CollapsibleBox("調整", expanded=True)

        # ライン／グループ ツリー（チェック=表示、色列=個別カラー、D&Dでグループ移動）
        self.tree = _OutlineTree(self)
        self.tree.setHeaderLabels(["名前", "太さ", "色"])
        self.tree.setColumnWidth(0, 230)
        self.tree.setColumnWidth(1, 60)
        self.tree.setColumnWidth(2, 40)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.tree.setDragEnabled(True)
        self.tree.viewport().setAcceptDrops(True)
        self.tree.setDropIndicatorShown(True)
        self.tree.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.tree.setExpandsOnDoubleClick(False)   # ダブルクリックはリネーム用
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemSelectionChanged.connect(self._on_tree_selection)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        # v0.1.25: 右クリックメニュー
        self.tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        # v0.1.39: スクロール内にツリー。最小高さはそこそこ確保（200px）、
        # スクロール内で自然にサイズされ、ウィンドウを縮めれば本体ごとスクロール。
        self.tree.setMinimumHeight(200)
        sec_adjust.add_widget(self.tree)
        self.lbl_dd = QtWidgets.QLabel("※ グループへ D&D で移動。ダブルクリックで名前変更")
        self.lbl_dd.setStyleSheet("color:#888;")
        sec_adjust.add_widget(self.lbl_dd)
        # v0.1.53: ビューポートでメッシュを選ぶと、そのメッシュの線を一覧で選ぶ（右クリックで逆も）
        self.chk_sel_sync = QtWidgets.QCheckBox("ビューポートで選んだメッシュの線を一覧で選ぶ")
        self.chk_sel_sync.setChecked(True)
        self.chk_sel_sync.setToolTip(
            "ON: ビューポートでメッシュ（体・髪など）を選ぶと、そのメッシュから作った線を一覧で選びます。\n"
            "逆に、一覧の線から元のメッシュを選ぶには、右クリック →『元のメッシュを選ぶ』。")
        sec_adjust.add_widget(self.chk_sel_sync)

        # ライン用パネル（ライン選択時のみ表示）: 太さ / 曲率起伏 / 曲率上限
        self.w_line = QtWidgets.QWidget()
        lvl = QtWidgets.QVBoxLayout(self.w_line); lvl.setContentsMargins(0, 0, 0, 0)
        trow, self.slider, self.spin = self._slider_spin_row(
            "太さ", 0, 2000, 500, 3, 0.0, 2.0, 0.01, 0.5,
            self._on_slider, self._on_spin, self._reset_thickness,
            on_key=self._key_thickness)
        lvl.addLayout(trow)
        c2row, self.cslider, self.cspin = self._slider_spin_row(
            "曲率起伏", 0, 1000, 0, 2, 0.0, 10.0, 0.1, 0.0,
            self._on_cslider, self._on_cspin, self._reset_curv,
            on_key=self._key_curv)
        lvl.addLayout(c2row)
        c3row, self.cap_slider, self.cap_spin = self._slider_spin_row(
            "曲率上限", 100, 1000, 300, 1, 1.0, 10.0, 0.5, 3.0,
            self._on_cap_slider, self._on_cap_spin, self._reset_cap,
            on_key=self._key_cap, tip="起伏の最大倍率（角が太くなりすぎないよう上限を設定）")
        lvl.addLayout(c3row)
        c4row, self.cmin_slider, self.cmin_spin = self._slider_spin_row(
            "曲率下限", 0, 100, 100, 2, 0.0, 1.0, 0.05, 1.0,
            self._on_cmin_slider, self._on_cmin_spin, self._reset_cmin,
            on_key=self._key_cmin, tip="曲率が小さい所（平らな所）の太さ倍率の下限。"
                                       "下げると細い所をより細くできる")
        lvl.addLayout(c4row)
        # 太さプロファイル（長手方向のカーブで強弱）。エッジライン選択時のみ表示。
        # （末端細りはプロファイルで代替できるため UI スライダーは廃止。互換のため
        #   endTaper 属性自体は残し、既存シーンの値があれば _update_curv_weights が反映する）
        self.w_profile = QtWidgets.QWidget()
        prow = QtWidgets.QHBoxLayout(self.w_profile); prow.setContentsMargins(0, 0, 0, 0)
        plabel = QtWidgets.QLabel("太さプロファイル")
        plabel.setAlignment(QtCore.Qt.AlignTop)
        prow.addWidget(plabel)
        self.ramp = _RampWidget()
        self.ramp.setToolTip("長手方向の太さ強弱。左クリックで点追加/移動、右クリックで削除")
        self.ramp.valueChanged.connect(self._apply_profile)
        # v0.1.53: 押してから離すまでを Undo 1 回に（以前はマウスを動かすたびに 1 回ずつ積んでいた）
        self.ramp.dragStarted.connect(self._begin_drag)
        self.ramp.dragFinished.connect(self._end_drag)
        prow.addWidget(self.ramp, 1)
        b_rp = QtWidgets.QPushButton("↺"); b_rp.setFixedWidth(26)
        b_rp.setToolTip("プロファイルをリセット")
        b_rp.clicked.connect(self._reset_profile)
        prow.addWidget(b_rp)
        lvl.addWidget(self.w_profile)

        # グループ用パネル（グループ選択時のみ表示）: グループ倍率
        self.w_group = QtWidgets.QWidget()
        gvl = QtWidgets.QVBoxLayout(self.w_group); gvl.setContentsMargins(0, 0, 0, 0)
        grow, self.grpslider, self.grpspin = self._slider_spin_row(
            "グループ倍率", 0, 500, 100, 2, 0.0, 5.0, 0.05, 1.0,
            self._on_grpslider, self._on_grpspin, self._reset_grpmult,
            on_key=self._key_grpmult, tip="選択グループの太さ倍率")
        gvl.addLayout(grow)

        # v0.1.39: ライン用/グループ用パネルは選択に応じて片方だけが可視になる。
        # 固定高さを撤廃し、中身のサイズに応じて自然に伸縮する（ライン時 ≈ 230px、
        # グループ時 ≈ 40px、未選択時 ≈ 0px）。
        self.w_panels = QtWidgets.QWidget()
        pvl = QtWidgets.QVBoxLayout(self.w_panels); pvl.setContentsMargins(0, 0, 0, 0)
        pvl.setSpacing(0)
        pvl.addWidget(self.w_line)
        pvl.addWidget(self.w_group)
        sec_adjust.add_widget(self.w_panels)

        self.lbl_hint = QtWidgets.QLabel("※ 値はツリーで選択したライン/グループに適用されます")
        self.lbl_hint.setStyleSheet("color:#888;")
        sec_adjust.add_widget(self.lbl_hint)

        # 選択物のすべての値を初期値に戻す（実行前に確認ダイアログ）→ 調整の末尾にそのまま置く
        self.btn_reset_all = QtWidgets.QPushButton("選択をすべてリセット")
        self.btn_reset_all.setToolTip("選択したライン/グループの全パラメータを初期値に戻します（確認あり）")
        self.btn_reset_all.clicked.connect(self.reset_selected)
        self.btn_reset_all.setStyleSheet(_PRIMARY_BTN_CSS)
        sec_adjust.add_widget(self.btn_reset_all)

        lay.addWidget(sec_adjust)

        # ================ セクション 4: カラー・管理 ================
        sec_admin = _CollapsibleBox("カラー・管理", expanded=False)

        # カラー
        clrow = QtWidgets.QHBoxLayout()
        clrow.addWidget(QtWidgets.QLabel("共通カラー"))
        self.btn_color = QtWidgets.QPushButton()
        self.btn_color.setFixedHeight(22)
        self.btn_color.setToolTip("共通カラー（基本のブラック）を変更。共通カラーのライン全部に効く")
        self.btn_color.clicked.connect(self._pick_color)
        self._refresh_swatch()
        clrow.addWidget(self.btn_color, 1)
        b_icol = QtWidgets.QPushButton("選択に個別カラー")
        b_scol = QtWidgets.QPushButton("選択を共通へ")
        b_icol.clicked.connect(self.set_individual_color)
        b_scol.clicked.connect(self.reset_to_shared_color)
        clrow.addWidget(b_icol)
        clrow.addWidget(b_scol)
        sec_admin.add_layout(clrow)

        # v0.1.35: 選択からレンダーレイヤー作成 (管理系なのでここに移動)
        self.btn_render_layer = QtWidgets.QPushButton("選択からレンダーレイヤー")
        self.btn_render_layer.setToolTip(
            "ツリーで選んだライン（グループを選ぶと配下の全ライン）だけが見える\n"
            "Render Setup レイヤーを作る。元メッシュは useBackground で透明化し\n"
            "線は Outline グループから include。Hardware 2.0 プレイブラスト用。\n"
            "作成時にレイヤー名を入力。既存名と被れば自動連番。")
        self.btn_render_layer.clicked.connect(self.create_render_layer_from_selection)
        sec_admin.add_widget(self.btn_render_layer)

        # ★v0.1.10 以降 UI 整理: hull/GAPFILL/画像空間エッジ関連のオプション群は
        #  ToonOutlineUI から非表示(コード・関数自体は残置、_edge_*/_on_toggle_edge/
        #  _on_toggle_occlude/_on_toggle_hide_handles/_on_toggle_lock_select/
        #  chk_edge/chk_edge_debug/spn_edge_thr/spn_edge_w は後方互換で属性だけ生成)。
        # 既定値を適用する最低限のステートを属性として保持する:
        self.chk_hide_handles = QtWidgets.QCheckBox(); self.chk_hide_handles.setChecked(True)
        self.chk_hide_handles.hide()
        self.chk_lock_select = QtWidgets.QCheckBox(); self.chk_lock_select.setChecked(True)
        self.chk_lock_select.hide()
        self.chk_occlude = QtWidgets.QCheckBox(); self.chk_occlude.setChecked(False)
        self.chk_occlude.hide()
        self.chk_edge = QtWidgets.QCheckBox(); self.chk_edge.setChecked(False)
        self.chk_edge.hide()
        self.chk_edge_debug = QtWidgets.QCheckBox(); self.chk_edge_debug.setChecked(False)
        self.chk_edge_debug.hide()
        self.spn_edge_thr = QtWidgets.QDoubleSpinBox()
        self.spn_edge_thr.setRange(0.001, 0.5); self.spn_edge_thr.setValue(_EDGE_PARAMS["threshold"])
        self.spn_edge_thr.hide()
        self.spn_edge_w = QtWidgets.QDoubleSpinBox()
        self.spn_edge_w.setRange(0.5, 8.0); self.spn_edge_w.setValue(_EDGE_PARAMS["thickness"])
        self.spn_edge_w.hide()

        self.lbl_del = QtWidgets.QLabel("※ ライン/グループの削除は Delete キー")
        self.lbl_del.setStyleSheet("color:#888;")
        sec_admin.add_widget(self.lbl_del)

        lay.addWidget(sec_admin)
        lay.addStretch(1)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # v0.1.21: フッターのバージョン表示/選択ボタンは ⋮ ヘッダーメニューへ移動済み。

    # ========== シーン走査ヘルパ ==========
    def _ensure_line_holder(self):
        """ライングループの格納グループ（ROOT 直下 Outline_grp）を確保。"""
        _ensure_root()
        for c in cmds.listRelatives(ROOT, children=True, type="transform", f=True) or []:
            if _short(c) == LINE_HOLDER:
                return c
        return cmds.group(em=True, name=LINE_HOLDER, parent=ROOT)

    def _find_line_holder(self):
        if not cmds.objExists(ROOT):
            return None
        for c in cmds.listRelatives(ROOT, children=True, type="transform", f=True) or []:
            if _short(c) == LINE_HOLDER:
                return c
        return None

    def _managed_groups(self):
        """ライングループ（GROUP_TAG 付き）。Outline_grp 配下。旧 ROOT 直下のものは移行する。
        ※ 空のときにホルダーを作らない（ツール起動だけでノードを作らないため）。"""
        if not cmds.objExists(ROOT):
            return []
        # 旧データ: ROOT 直下のグループがあれば Outline_grp へ移す
        direct = [t for t in (cmds.listRelatives(ROOT, children=True, type="transform", f=True) or [])
                  if cmds.attributeQuery(GROUP_TAG, node=t, exists=True)]
        if direct:
            holder = self._ensure_line_holder()
            for t in direct:
                try:
                    cmds.parent(t, holder)
                except Exception:
                    pass
        holder = self._find_line_holder()
        if not holder:
            return []
        out = []
        for t in cmds.listRelatives(holder, children=True, type="transform", f=True) or []:
            if cmds.attributeQuery(GROUP_TAG, node=t, exists=True):
                out.append(t)
        return out

    def _lines_in(self, group):
        """グループ直下のライン（TAG 付き）。"""
        out = []
        for t in cmds.listRelatives(group, children=True, type="transform", f=True) or []:
            if cmds.attributeQuery(TAG, node=t, exists=True):
                out.append(t)
        return out

    def _loose_lines(self):
        """Outline_grp 直下に直接ぶら下がっているライン（グループ未所属）。"""
        holder = self._find_line_holder()
        if not holder:
            return []
        out = []
        for t in cmds.listRelatives(holder, children=True, type="transform", f=True) or []:
            if cmds.attributeQuery(TAG, node=t, exists=True):
                out.append(t)
        return out

    def _shape_of(self, line):
        sh = cmds.listRelatives(line, shapes=True, type="mesh", ni=True, f=True)
        return sh[0] if sh else None

    def _thick_node_for(self, line):
        """ラインの太さ駆動ノード（textureDeformer）を返す。"""
        return _line_deformer(line)

    def _thickness_of(self, line):
        # 太さの真値はコントローラー属性 ctrl.thickness（offset を駆動）。
        ctrl = _ctrl_of(line)
        if ctrl:
            try:
                return cmds.getAttr(ctrl + "." + CTRL_THICK)
            except Exception:
                pass
        node = self._thick_node_for(line)
        if node and cmds.objExists(node):
            try:
                return cmds.getAttr(node + "." + THICK_ATTR)
            except Exception:
                pass
        return None

    def _line_color(self, line):
        """ラインの線色 [r,g,b]。フレネルは dx11Shader.lineColor、他は surfaceShader.outColor。"""
        sh = self._shape_of(line)
        if not sh:
            return None
        sgs = cmds.listConnections(sh, type="shadingEngine") or []
        if not sgs:
            return None
        ss = cmds.listConnections(sgs[0] + ".surfaceShader") or []
        if not ss:
            return None
        is_dx11 = (cmds.attributeQuery(FRES_TAG, node=line, exists=True)
                   or cmds.attributeQuery(SCRN_TAG, node=line, exists=True))
        attr = ("." + FRES_COLOR) if is_dx11 else ".outColor"
        try:
            c = cmds.getAttr(ss[0] + attr)[0]
            return [c[0], c[1], c[2]]
        except Exception:
            return None

    def _ensure_group(self, name):
        """指定名のグループを Outline_grp 配下に確保して返す。"""
        for g in self._managed_groups():
            if _short(g) == name:
                return g
        grp = cmds.group(em=True, name=name, parent=self._ensure_line_holder())
        cmds.addAttr(grp, ln=GROUP_TAG, at="bool", dv=True)
        return grp

    def _ensure_mesh_group(self, mesh_short):
        """v0.1.31: メッシュごとのトップグループ <mesh>_outlines を確保。"""
        return self._ensure_group(mesh_short + MESH_GROUP_SUFFIX)

    def _batch_group_name(self, sel):
        """v0.1.42: 複数メッシュ選択時に 1 まとめ用の共通グループ名。
        単一ならメッシュ名で従来グループ、2 個なら 'a_b_outlines'、3+ なら 'a_b_etc_outlines'。"""
        if not sel:
            return None
        shorts = [_base(o) for o in sel if o]
        if len(shorts) <= 1:
            return None
        if len(shorts) == 2:
            return shorts[0] + "_" + shorts[1] + MESH_GROUP_SUFFIX
        return shorts[0] + "_" + shorts[1] + "_etc" + MESH_GROUP_SUFFIX

    def _ensure_batch_group(self, sel):
        """v0.1.42: 複数選択は 1 グループへまとめる。単一は None を返して per-mesh へフォールバック。"""
        name = self._batch_group_name(sel)
        if name is None:
            return None
        return self._ensure_group(name)

    def _ensure_sub_group(self, parent_group, sub_name):
        """v0.1.31: 指定グループ直下のサブグループを確保。"""
        for c in cmds.listRelatives(parent_group, c=True, type="transform", f=True) or []:
            if _short(c) == sub_name:
                return c
        grp = cmds.group(em=True, name=sub_name, parent=parent_group)
        cmds.addAttr(grp, ln=GROUP_TAG, at="bool", dv=True)
        return grp

    def _subgroups_of(self, group):
        """GROUP_TAG を持つ直下のサブグループ。"""
        out = []
        for c in cmds.listRelatives(group, c=True, type="transform", f=True) or []:
            if cmds.attributeQuery(GROUP_TAG, node=c, exists=True):
                out.append(c)
        return out

    def _current_group(self):
        name = self.group_combo.currentText().strip() if self.group_combo.count() else ""
        return self._ensure_group(name or DEFAULT_GROUP)

    def _tuck_handle(self, h):
        """ハンドルをビューポート非表示にし、アウトライナーからは設定に応じて隠す。"""
        if not (h and cmds.objExists(h)):
            return
        hide = 1 if getattr(self, "chk_hide_handles", None) is None or self.chk_hide_handles.isChecked() else 0
        # 値が同じなら書かない（Undo の履歴を汚さない）
        for plug, v in ((h + ".hiddenInOutliner", hide), (h + ".visibility", 0)):
            try:
                if int(cmds.getAttr(plug)) != v:
                    cmds.setAttr(plug, v)
            except Exception:
                pass

    def _on_toggle_hide_handles(self, state):
        """UIチェックでハンドルの hiddenInOutliner を一括切替。"""
        hide = 1 if state else 0
        cmds.undoInfo(openChunk=True)
        try:
            for h in cmds.ls("textureDeformerHandle*", type="transform") or []:
                try:
                    cmds.setAttr(h + ".hiddenInOutliner", hide)
                except Exception:
                    pass
        finally:
            cmds.undoInfo(closeChunk=True)
        try:
            import maya.mel as _mel
            _mel.eval("AEdagNodeCommonRefreshOutliners();")
        except Exception:
            pass

    def _all_lines(self):
        """管理下の全ライン（TAG 付き transform）。"""
        return [t for t in (cmds.ls(type="transform") or [])
                if cmds.attributeQuery(TAG, node=t, exists=True)]

    def _lock_select(self):
        """ビューポート選択不可チェックの現在状態（True=選択不可）。"""
        c = getattr(self, "chk_lock_select", None)
        return True if c is None else c.isChecked()

    def _apply_line_selectable(self, line, lock):
        """ライン shape を選択不可(reference)/通常に切替。
        reference(displayType=2) は表示・レンダーは有効のまま選択だけ不可にする。"""
        sh = self._shape_of(line)
        if not sh:
            return
        # 値が同じなら書かない（refresh_tree のたびに setAttr すると Undo の履歴に
        # 無意味な操作が積まれ、直前の操作が Ctrl+Z 1 回で戻らなくなる）
        want = ((sh + ".overrideEnabled", 1 if lock else 0),
                (sh + ".overrideDisplayType", 2 if lock else 0))   # 2 = reference, 0 = normal
        for plug, v in want:
            try:
                if int(cmds.getAttr(plug)) != v:
                    cmds.setAttr(plug, v)
            except Exception:
                pass

    def _on_toggle_lock_select(self, state):
        """UIチェックで全ラインのビューポート選択可否を一括切替。"""
        lock = bool(state)
        cmds.undoInfo(openChunk=True)
        try:
            for line in self._all_lines():
                self._apply_line_selectable(line, lock)
        finally:
            cmds.undoInfo(closeChunk=True)

    def _hull_lines(self):
        """管理下のハルライン（隠蔽検知の対象）。"""
        return [l for l in self._all_lines() if _is_hull_line(l)]

    def _cam_tracking_needed(self):
        """カメラ追従の再計算が必要か（隠蔽検知ON、隙間埋め、または距離減衰 ON）。"""
        return _OCC_ENABLED or bool(_all_gapfills()) or self._dist_atten_active()

    def _dist_atten_active(self):
        """v0.1.32: 距離減衰が有効か（globalCtrl.distAtten > 0）。"""
        gc = _find_global_ctrl()
        if not gc or not cmds.attributeQuery(DIST_ATTEN_STRENGTH, node=gc, exists=True):
            return False
        try:
            return float(cmds.getAttr(gc + "." + DIST_ATTEN_STRENGTH)) > 1e-4
        except Exception:
            return False

    def _active_cam_transform(self):
        """アクティブビューポートのカメラ transform を返す。無ければ 'persp'。"""
        try:
            p = cmds.playblast(ae=True)
            if p:
                cam_sh = cmds.modelPanel(p, q=True, cam=True)
                if cam_sh:
                    par = cmds.listRelatives(cam_sh, parent=True, f=True)
                    return par[0] if par else cam_sh
        except Exception:
            pass
        return "persp"

    def _refresh_distatten(self):
        """v0.1.32: SCRN 全ラインの camDistAtten を現アクティブカメラ距離から計算。
        強度=0 のとき全ラインを 1.0 にリセット（無効化）。"""
        if not cmds.ls(long=True, type="transform"): return
        strength = 0.0
        gc = _find_global_ctrl()
        if gc and cmds.attributeQuery(DIST_ATTEN_STRENGTH, node=gc, exists=True):
            try: strength = float(cmds.getAttr(gc + "." + DIST_ATTEN_STRENGTH))
            except Exception: strength = 0.0
        cam = self._active_cam_transform()
        cam_pos = None
        try:
            cam_pos = cmds.xform(cam, q=True, ws=True, t=True)
        except Exception:
            return
        if not cam_pos or len(cam_pos) < 3:
            return
        cx, cy, cz = cam_pos[0], cam_pos[1], cam_pos[2]
        for ln in (cmds.ls(long=True, type="transform") or []):
            # v0.1.33: SCRN と EDGE_TAG (クリース/エッジ) 両方に適用
            is_target = (cmds.attributeQuery(SCRN_TAG, node=ln, exists=True) or
                         cmds.attributeQuery(EDGE_TAG, node=ln, exists=True))
            if not is_target:
                continue
            ctrl = _ctrl_of(ln)
            if not ctrl or not cmds.attributeQuery(CTRL_DISTATTEN, node=ctrl, exists=True):
                continue
            atten = 1.0
            if strength > 1e-4:
                # ラインの中心 = bounding box 中心
                try:
                    bb = cmds.xform(ln, q=True, ws=True, bb=True)
                    mx = (bb[0] + bb[3]) * 0.5
                    my = (bb[1] + bb[4]) * 0.5
                    mz = (bb[2] + bb[5]) * 0.5
                    dx, dy, dz = mx - cx, my - cy, mz - cz
                    dist = (dx*dx + dy*dy + dz*dz) ** 0.5
                    raw = DIST_ATTEN_REF / (DIST_ATTEN_REF + dist)
                    # strength でブレンド: 0=常に 1.0, 1=raw そのまま
                    atten = (1.0 - strength) * 1.0 + strength * raw
                    if atten < 0.1: atten = 0.1
                except Exception:
                    atten = 1.0
            try:
                if abs(cmds.getAttr(ctrl + "." + CTRL_DISTATTEN) - atten) > 1e-4:
                    cmds.setAttr(ctrl + "." + CTRL_DISTATTEN, atten)
            except Exception:
                pass

    def _refresh_occlusion(self):
        """カメラ依存の頂点ウェイトを再計算（隠蔽検知ハル＋隙間埋め＋距離減衰）。undo は汚さない。
        再計算中（_occ_busy）は重畳を避けてスキップ＝重いメッシュでもビューポートが固まらない。"""
        if self._occ_busy:
            return
        gaps = _all_gapfills()
        lines = self._hull_lines() if _OCC_ENABLED else []
        dist_on = self._dist_atten_active()
        if not lines and not gaps and not dist_on:
            return
        self._occ_busy = True
        try:
            cmds.undoInfo(swf=False)
        except Exception:
            pass
        try:
            for l in lines:
                _update_curv_weights(l)        # 隠蔽検知ハル（occlusion ON のとき）
            for b in gaps:
                _update_gapfill_weights(b)      # 隙間埋め（facing 駆動）
            if dist_on:
                self._refresh_distatten()       # v0.1.32: 距離減衰
        finally:
            try:
                cmds.undoInfo(swf=True)
            except Exception:
                pass
            self._occ_busy = False

    def _request_occ_refresh(self):
        """カメラ移動コールバックから呼ぶ。次のイベントループで1回だけ再計算を予約
        （DG評価中の setAttr 再入を避け、連続発火でも重複予約しない）。"""
        if self._occ_scheduled or not self._cam_tracking_needed():
            return
        self._occ_scheduled = True
        QtCore.QTimer.singleShot(0, self._do_scheduled_occ)

    def _do_scheduled_occ(self):
        self._occ_scheduled = False
        if self._cam_tracking_needed():
            self._refresh_occlusion()

    def _on_cam_moved(self, *args):
        """カメラ transform の worldMatrix が変化したとき（=カメラを動かすたび）に発火。"""
        self._request_occ_refresh()

    def _add_cam_callbacks(self):
        """全カメラ transform に worldMatrix 変化コールバックを張る（動かすたびに更新）。"""
        self._remove_cam_callbacks()
        ids = []
        for cam in (cmds.ls(type="camera") or []):
            par = cmds.listRelatives(cam, parent=True, fullPath=True) or []
            if not par:
                continue
            try:
                sl = om2.MSelectionList(); sl.add(par[0])
                dag = sl.getDagPath(0)
                cid = om2.MDagMessage.addWorldMatrixModifiedCallback(dag, self._on_cam_moved, None)
                ids.append(cid)
            except Exception:
                pass
        self._occ_cb_ids = ids

    def _remove_cam_callbacks(self):
        for cid in (self._occ_cb_ids or []):
            try:
                om2.MMessage.removeCallback(cid)
            except Exception:
                pass
        self._occ_cb_ids = []

    def _poll_camera(self):
        """フォールバック: カメラ切替や新規カメラに備え、低頻度ポーリングでも変化を拾う。
        通常の追従は worldMatrix コールバック（_on_cam_moved）が行う。"""
        if not self._cam_tracking_needed():
            return
        cam = _active_camera()
        if not cam:
            return
        try:
            csl = om2.MSelectionList(); csl.add(cam)
            m = list(csl.getDagPath(0).inclusiveMatrix())
        except Exception:
            return
        key = (cam, tuple(round(v, 5) for v in m))
        if key == self._occ_cam_key:
            return
        self._occ_cam_key = key
        # 切替時にコールバック先も貼り直す
        self._add_cam_callbacks()
        self._refresh_occlusion()

    def _ensure_cam_tracking(self):
        """カメラ追従が必要なら worldMatrix コールバック＋低頻度フォールバックタイマーを起動、
        不要なら解除する（隠蔽検知ON または 隙間埋めオブジェクトが存在するときに必要）。"""
        if self._cam_tracking_needed():
            self._occ_cam_key = None
            self._add_cam_callbacks()                       # カメラを動かすたびに更新
            if self._occ_timer is None:
                self._occ_timer = QtCore.QTimer(self)
                self._occ_timer.setInterval(250)            # フォールバック（カメラ切替検知）
                self._occ_timer.timeout.connect(self._poll_camera)
            self._occ_timer.start()
        else:
            if self._occ_timer is not None:
                self._occ_timer.stop()
            self._remove_cam_callbacks()

    def _on_toggle_edge(self, state):
        """画像空間エッジ検出（オブジェクト単位）の ON/OFF。ON 時は選択メッシュを対象にする。"""
        if state:
            sel = cmds.ls(sl=True, long=True, type="transform") or []
            targets = [o for o in sel
                       if cmds.listRelatives(o, shapes=True, type="mesh", ni=True)]
            if not targets:
                cmds.warning("エッジ検出の対象メッシュを選択してから ON にしてください")
                self.chk_edge.blockSignals(True)
                self.chk_edge.setChecked(False)
                self.chk_edge.blockSignals(False)
                return
            _edge_set_targets(targets)
            _edge_set_params(threshold=self.spn_edge_thr.value(),
                             thickness=self.spn_edge_w.value(), color=self._color,
                             debug=self.chk_edge_debug.isChecked())
            _OVR_STATE["edge"] = True
            if not _og_override_sync():
                _OVR_STATE["edge"] = False
                self.chk_edge.blockSignals(True)
                self.chk_edge.setChecked(False)
                self.chk_edge.blockSignals(False)
            else:
                # 成功通知は情報レベルで（cmds.warning は Script Editor で黄色になり誤読される）
                om2.MGlobal.displayInfo(
                    "画像空間エッジ検出 ON（対象 {0} メッシュ）。VP2/DirectX11・ビューポート専用。"
                    .format(len(targets)))
        else:
            _OVR_STATE["edge"] = False
            _og_override_sync()

    def _on_edge_param(self, *args):
        """しきい値/太さ/色/デバッグ表示を反映（線色は共通カラーに追従）。"""
        _edge_set_params(threshold=self.spn_edge_thr.value(),
                         thickness=self.spn_edge_w.value(), color=self._color,
                         debug=self.chk_edge_debug.isChecked())

    def _on_scrn_bias(self, *args):
        """スクリーン輪郭の重なり深度押し込み（ビュー空間）を全ラインへ反映。"""
        _scrn_set_depth_bias(self.spn_scrn_bias.value())

    def _on_toggle_intersect(self, state):
        """v0.1.36 → v0.1.41: 交差線（画像空間、対象 vs 他メッシュ）の ON/OFF。
        対象メッシュは「SCRN ラインで『交差線に含める』(toonIntersectOn=True) の元メッシュ」。
        ツリー右クリックメニューでライン個別に切替できる。"""
        if state:
            targets = _scrn_intersect_meshes()
            if not targets:
                cmds.warning("対象になる SCRN ラインがありません。先に『選択メッシュに輪郭を生成』で"
                             "ラインを作ってから交差線を ON にしてください。"
                             "（ツリー右クリックで個別に対象/除外できます）")
                self.chk_intersect.blockSignals(True)
                self.chk_intersect.setChecked(False)
                self.chk_intersect.blockSignals(False)
                return
            _edge_set_targets(targets)
            # v0.1.45: thickness は SCRN 本体と同じ pixel 幅になるように計算
            _edge_set_params(threshold=0.05,
                             thickness=_scrn_intersect_screen_thickness(),
                             color=self._color,
                             intersect_mode=True,
                             intersect_threshold=float(self.spn_intersect_thr.value()))
            _OVR_STATE["intersect"] = True
            if not _og_override_sync():
                _OVR_STATE["intersect"] = False
                self.chk_intersect.blockSignals(True)
                self.chk_intersect.setChecked(False)
                self.chk_intersect.blockSignals(False)
            else:
                om2.MGlobal.displayInfo(
                    "交差線 ON（対象 {0} メッシュ・他メッシュとの重なりに線）"
                    .format(len(targets)))
        else:
            _OVR_STATE["intersect"] = False
            _edge_set_params(intersect_mode=False)
            _og_override_sync()   # 隠れ線を使っていればオーバーライドは残る

    def _on_toggle_line_only(self, state):
        """v0.1.26: ライン単体表示モードのトグル。"""
        try:
            cmds.undoInfo(openChunk=True)
            _set_line_only_mode(bool(state))
        except Exception as e:
            cmds.warning("ライン単体表示の切替に失敗: {}".format(e))
        finally:
            try: cmds.undoInfo(closeChunk=True)
            except Exception: pass

    def _on_toggle_hidden_dash(self, state):
        """v0.1.46: シーン全体で隠れ線（紐づけた物の裏に回った線）を描くか。
        どの物の裏で透かすかは、ライン一覧の右クリック →『ライン詳細設定』で紐づける。"""
        cmds.undoInfo(openChunk=True)
        try:
            _set_hidden_scene_on(bool(state))
        finally:
            cmds.undoInfo(closeChunk=True)
        _og_override_sync()
        if state and _vp2_is_opengl():
            try:
                om2.MGlobal.displayWarning(
                    "隠れ線は DirectX11 のビューポートでのみ描けます（OpenGL では表示されません）。")
            except Exception:
                pass

    def _on_dist_atten_changed(self, *args):
        """v0.1.32: 遠景減衰強度をシーンへ書き込み + 即時計算 + カメラ追従起動。"""
        gc = _ensure_global_ctrl()
        if not cmds.attributeQuery(DIST_ATTEN_STRENGTH, node=gc, exists=True):
            return
        try:
            cmds.setAttr(gc + "." + DIST_ATTEN_STRENGTH, float(self.spn_dist_atten.value()))
        except Exception:
            pass
        try:
            self._refresh_distatten()
        except Exception:
            pass
        try:
            self._ensure_cam_tracking()
        except Exception:
            pass

    def _on_fres_sens(self, *args):
        """v0.1.18: フレネル感度（globalCtrl.fresSens）を書き込む。
        接続されていれば全フレネル線の maxWidthPx 倍率に即反映。globalCtrl が
        まだ無い場合はスピン値だけ保持（次の生成時 _ensure_global_ctrl で attr 追加）。"""
        gc = None
        for c in cmds.ls(long=True, type="transform") or []:
            if _short(c) == GLOBAL_CTRL:
                gc = c; break
        if not gc or not cmds.attributeQuery(FRES_SENS_ATTR, node=gc, exists=True):
            return
        try:
            cmds.setAttr(gc + "." + FRES_SENS_ATTR, float(self.spn_fres_sens.value()))
        except Exception:
            pass

    def _show_about(self, *args):
        """バージョン情報 ダイアログ（⋮ メニューから）。"""
        try:
            QtWidgets.QMessageBox.information(
                self, "OG_Toonline_Manager",
                "OG_Toonline_Manager v{0}\n\n"
                "Maya 用の輪郭線生成ツール。\n"
                "SCRN (GS シルエット) / フレネル (facing) / クリース (折れ目) を組み合わせて\n"
                "Pencil+4 風の toon ラインを生成します。\n\n"
                "取得元: {1}\n"
                "GitHub: https://github.com/{2}/{3}".format(
                    __version__, _GITHUB_BRANCH, _GITHUB_OWNER, _GITHUB_REPO))
        except Exception:
            pass

    def _sync_fres_sens_from_scene(self):
        """シーン側の globalCtrl.fresSens → UI スピンへ一方向コピー（refresh 時用）。"""
        gc = None
        for c in cmds.ls(long=True, type="transform") or []:
            if _short(c) == GLOBAL_CTRL:
                gc = c; break
        if not gc or not cmds.attributeQuery(FRES_SENS_ATTR, node=gc, exists=True):
            return
        try:
            v = float(cmds.getAttr(gc + "." + FRES_SENS_ATTR))
        except Exception:
            return
        if getattr(self, "spn_fres_sens", None) is not None:
            self.spn_fres_sens.blockSignals(True)
            self.spn_fres_sens.setValue(v)
            self.spn_fres_sens.blockSignals(False)

    def _on_toggle_occlude(self, state):
        """UIチェックで隠蔽検知ハル（カメラ依存）の ON/OFF。"""
        global _OCC_ENABLED
        _OCC_ENABLED = bool(state)
        self._ensure_cam_tracking()
        self._refresh_occlusion()
        if _OCC_ENABLED:
            # 検出できているか切り分け用に頂点数を報告（0なら検出失敗＝レイ/カメラ要確認）
            try:
                total = flagged = 0
                lines = self._hull_lines()
                for l in lines:
                    occ = _occlusion_factors(l)
                    total += len(occ)
                    flagged += sum(1 for v in occ if v < 1.0)
                msg = ("隠蔽検知ハル: {} ライン中 {}/{} 頂点を引き寄せ検出"
                       .format(len(lines), flagged, total))
                if total == 0 and lines:
                    msg += " ／ 診断: " + _occlusion_debug(lines[0])
                cmds.warning(msg)
            except Exception:
                pass

    def _stash_loose_handles(self):
        """全 textureDeformerHandle をその場で隠す。旧ハンドルグループがあれば解体する。"""
        # 旧 toonOutline_handles グループを解体（中身をワールドへ出して削除）
        if cmds.objExists(HANDLE_HOLDER):
            for c in cmds.listRelatives(HANDLE_HOLDER, children=True, type="transform", f=True) or []:
                try:
                    cmds.parent(c, world=True)
                except Exception:
                    pass
            try:
                cmds.delete(HANDLE_HOLDER)
            except Exception:
                pass
        for h in cmds.ls("textureDeformerHandle*", type="transform") or []:
            self._tuck_handle(h)
        # アウトライナーを更新して hiddenInOutliner を反映
        try:
            import maya.mel as _mel
            _mel.eval("AEdagNodeCommonRefreshOutliners();")
        except Exception:
            pass

    def _cleanup_orphan_handles(self):
        """どの textureDeformer にも繋がっていないハンドルを削除する。"""
        for h in cmds.ls("textureDeformerHandle*", type="transform") or []:
            if not (cmds.listConnections(h, type="textureDeformer") or []):
                try:
                    cmds.delete(h)
                except Exception:
                    pass

    def _cleanup_orphan_ctrls(self):
        """リンク先（ライン/グループ）が消えたコントローラーと空の全体コントローラーを掃除。"""
        gc = _find_global_ctrl()
        if gc:
            orphans = []
            for c in cmds.listRelatives(gc, allDescendents=True, type="transform", f=True) or []:
                dest = cmds.listConnections(c + ".message", s=False, d=True) or []
                if not any(cmds.objExists(d) for d in dest):
                    orphans.append(c)
            for c in orphans:
                if cmds.objExists(c):
                    try:
                        cmds.delete(c)
                    except Exception:
                        pass
            if cmds.objExists(gc) and not (cmds.listRelatives(gc, children=True, type="transform") or []):
                try:
                    cmds.delete(gc)
                except Exception:
                    pass
        # 旧 toonOutline_ctrls が空で残っていれば掃除
        if cmds.objExists(CTRL_HOLDER) and not (cmds.listRelatives(CTRL_HOLDER, c=True) or []):
            try:
                cmds.delete(CTRL_HOLDER)
            except Exception:
                pass

    def _ensure_line_shader(self, line):
        """ラインの個別カラー用 surfaceShader/SG を確保。"""
        # v0.1.53: いま割り当てている個別カラーのシェーダがあればそれを使う（リファレンスしたアセットの
        # シェーダは名前空間付きで、名前では見つからない）
        sh = self._shape_of(line)
        for sg0 in (cmds.listConnections(sh, type="shadingEngine") or []) if sh else []:
            for ss0 in cmds.listConnections(sg0 + ".surfaceShader") or []:
                if _short(ss0).split(":")[-1].startswith(COL_PREFIX) and ss0.endswith("_SS"):
                    return ss0, sg0
        base = COL_PREFIX + _base(line)
        ss = base + "_SS"
        sg = base + "_SG"
        if not cmds.objExists(ss):
            ss = cmds.shadingNode("surfaceShader", asShader=True, name=ss)
        if not cmds.objExists(sg):
            sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=sg)
            cmds.connectAttr(ss + ".outColor", sg + ".surfaceShader", f=True)
        return ss, sg

    # ========== ツリー ==========
    def _add_line_item(self, parent_item, line):
        is_crease = cmds.attributeQuery(CREASE_TAG, node=line, exists=True)
        is_edge = cmds.attributeQuery(EDGE_TAG, node=line, exists=True)
        is_fres = cmds.attributeQuery(FRES_TAG, node=line, exists=True)
        is_scrn = cmds.attributeQuery(SCRN_TAG, node=line, exists=True)
        # v0.1.25: 種別はテキスト suffix でなく色付きドット（アイコン）で表現。
        if is_scrn:
            type_color, type_label = (96, 192, 255), "スクリーン"
        elif is_fres:
            type_color, type_label = (255, 143, 176), "フレネル"
        elif is_crease:
            type_color, type_label = (230, 192, 96), "クリース"
        elif is_edge:
            type_color, type_label = (255, 158, 64), "エッジ"
        else:
            type_color, type_label = (128, 224, 128), "背面"
        # v0.1.46: 隠れ線を紐づけたラインは [透] を付け、ツールチップに紐づけ先を出す
        occ = _line_occluders(line) if (is_scrn or is_edge) else []
        suffix = "  [透]" if occ else ""
        label = _short(line) + suffix
        it = QtWidgets.QTreeWidgetItem([label, "", ""])
        it.setData(0, QtCore.Qt.UserRole, line)
        # v0.1.27: 種別はテキスト色で表現（旧ドットアイコンはチェックボックスと紛らわしかったため）
        it.setForeground(0, QtGui.QBrush(QtGui.QColor(*type_color)))
        tip = "{} ({})".format(_short(line), type_label)
        if occ:
            tip += "\n隠れ線を透かす物: " + ", ".join(_short(o) for o in occ)
        it.setToolTip(0, tip)
        # ライン: ドラッグ可・ドロップ不可（グループにのみ落とす）
        it.setFlags((it.flags() | QtCore.Qt.ItemIsUserCheckable
                     | QtCore.Qt.ItemIsDragEnabled) & ~QtCore.Qt.ItemIsDropEnabled)
        if _is_foreign(line):
            # v0.1.53: リファレンス等のラインは別のグループへ移せない（アセット側の構造）
            it.setFlags(it.flags() & ~QtCore.Qt.ItemIsDragEnabled)
        vis = True
        try:
            vis = bool(cmds.getAttr(line + ".visibility"))
        except Exception:
            pass
        it.setCheckState(0, QtCore.Qt.Checked if vis else QtCore.Qt.Unchecked)
        t = self._thickness_of(line)
        it.setText(1, "" if t is None else "{:.3f}".format(t))
        col = self._line_color(line)
        if col:
            r, g, b = [int(max(0.0, min(1.0, x)) * 255) for x in col]
            it.setBackground(2, QtGui.QBrush(QtGui.QColor(r, g, b)))
        parent_item.addChild(it)
        # v0.1.53: ビューポートで元メッシュを選んだときに一覧で選ぶための対応表
        src = _line_source_mesh(line)
        if src:
            full = (cmds.ls(line, long=True) or [line])[0]
            self._src_to_lines.setdefault(src, []).append(full)
        return it

    def _add_group_item(self, group_node, lines, visible, parent_item=None, foreign=False):
        """v0.1.31: parent_item 指定でサブグループとしてネスト追加。
        指定なし=トップレベル追加（既存動作）。
        v0.1.53: foreign=True（リファレンス等の別 ROOT の下）はドロップ不可。"""
        foreign = foreign or bool(group_node and _is_foreign(group_node))
        label = _short(group_node) if group_node else "(未分類)"
        # v0.1.26: __Creases__ はツリー表示では「クリース」に見せる。v0.1.31: __Edges__ も。
        if label == CREASES_GROUP_NAME:
            label = CREASES_GROUP_DISPLAY
        elif label == MATBOUND_GROUP_NAME:
            label = MATBOUND_GROUP_DISPLAY
        elif label == EDGES_GROUP_NAME:
            label = EDGES_GROUP_DISPLAY
        gi = QtWidgets.QTreeWidgetItem([label, "", ""])
        if group_node:
            gi.setData(0, QtCore.Qt.UserRole, group_node)
            # グループ: チェック可・ドロップ可・自身はドラッグ不可
            gi.setFlags((gi.flags() | QtCore.Qt.ItemIsUserCheckable
                         | QtCore.Qt.ItemIsDropEnabled) & ~QtCore.Qt.ItemIsDragEnabled)
            gi.setCheckState(0, QtCore.Qt.Checked if visible else QtCore.Qt.Unchecked)
            # 太さ列にグループ倍率を表示
            grpctrl = _ensure_group_ctrl(group_node)
            gm = 1.0
            try:
                gm = cmds.getAttr(grpctrl + "." + GMULT)
            except Exception:
                pass
            gi.setText(1, "x{:.2f}".format(gm))
        else:
            gi.setFlags((gi.flags() | QtCore.Qt.ItemIsDropEnabled)
                        & ~QtCore.Qt.ItemIsUserCheckable & ~QtCore.Qt.ItemIsDragEnabled)
        if foreign:
            gi.setFlags(gi.flags() & ~QtCore.Qt.ItemIsDropEnabled)
        f = gi.font(0); f.setBold(True); gi.setFont(0, f)
        gi.setBackground(0, QtGui.QBrush(QtGui.QColor(70, 78, 88)))
        if parent_item is None:
            self.tree.addTopLevelItem(gi)
        else:
            parent_item.addChild(gi)
        for line in lines:
            self._add_line_item(gi, line)
            _ensure_line_anim(line, self._thickness_of(line) or 0.05)
        # v0.1.31: サブグループを再帰的に子として追加
        if group_node:
            for sub in self._subgroups_of(group_node):
                sub_vis = True
                try:
                    sub_vis = bool(cmds.getAttr(sub + ".visibility"))
                except Exception:
                    pass
                self._add_group_item(sub, self._lines_in(sub), sub_vis, parent_item=gi, foreign=foreign)
        gi.setExpanded(True)
        return gi

    def _add_foreign_root_item(self, root):
        """v0.1.53: リファレンスしたキャラなど、別 ROOT（名前空間付き）の線を 1 つの見出しにまとめて表示する。
        値の変更・キー・ライン詳細設定は使える。グループ移動・名前変更・削除はアセット側で行う。"""
        holder = None
        for c in cmds.listRelatives(root, children=True, type="transform", f=True) or []:
            if _short(c).split(":")[-1] == LINE_HOLDER:
                holder = c
                break
        if not holder:
            return None
        kids = cmds.listRelatives(holder, children=True, type="transform", f=True) or []
        groups = [t for t in kids if cmds.attributeQuery(GROUP_TAG, node=t, exists=True)]
        loose = [t for t in kids if cmds.attributeQuery(TAG, node=t, exists=True)]
        if not groups and not loose:
            return None
        ns = _short(root).rpartition(":")[0] or _short(root)
        try:
            is_ref = cmds.referenceQuery(root, isNodeReferenced=True)
        except Exception:
            is_ref = False
        ri = QtWidgets.QTreeWidgetItem([u"{0}（{1}）".format(ns, u"リファレンス" if is_ref else u"名前空間"), "", ""])
        ri.setData(0, QtCore.Qt.UserRole, root)
        ri.setFlags(ri.flags() & ~QtCore.Qt.ItemIsUserCheckable & ~QtCore.Qt.ItemIsDragEnabled
                    & ~QtCore.Qt.ItemIsDropEnabled)
        ri.setToolTip(0, u"{0}\nアセット側で作った線です。太さ・曲率・色・キー・ライン詳細設定は"
                         u"ここから変えられます（このシーンに保存）。\n"
                         u"グループの移動・名前の変更・削除はアセットのファイルで行ってください。".format(root))
        f = ri.font(0); f.setBold(True); f.setItalic(True); ri.setFont(0, f)
        ri.setBackground(0, QtGui.QBrush(QtGui.QColor(60, 66, 84)))
        self.tree.addTopLevelItem(ri)
        for g in groups:
            gvis = True
            try:
                gvis = bool(cmds.getAttr(g + ".visibility"))
            except Exception:
                pass
            self._add_group_item(g, self._lines_in(g), gvis, parent_item=ri, foreign=True)
        if loose:
            self._add_group_item(None, loose, True, parent_item=ri, foreign=True)
        ri.setExpanded(True)
        return ri

    def refresh_tree(self):
        # v0.1.46: 既存 SCRN ラインを現行シェーダ＋見え方属性へ揃え、隠れ線の紐づけを読み直す
        try:
            _prepare_scrn_lines()
            _hidden_refresh_cache()
        except Exception:
            pass
        try:
            _prune_curv_jobs()      # v0.1.52: もう無い（Undo でも戻らない）ラインの曲率ジョブを片付ける
        except Exception:
            pass
        self._populating = True
        self.tree.clear()
        self._src_to_lines = {}
        # v0.1.18: フレネル感度はシーン属性と一方向同期（起動/再取得で現在値を反映）
        try:
            self._sync_fres_sens_from_scene()
        except Exception:
            pass
        for g in self._managed_groups():
            gvis = True
            try:
                gvis = bool(cmds.getAttr(g + ".visibility"))
            except Exception:
                pass
            self._add_group_item(g, self._lines_in(g), gvis)
        loose = self._loose_lines()
        if loose:
            self._add_group_item(None, loose, True)
        # v0.1.53: リファレンスしたキャラなど、別 ROOT の線も見出しごとに出す
        for root in _all_roots():
            if root != "|" + ROOT:
                try:
                    self._add_foreign_root_item(root)
                except Exception:
                    pass
        self.tree.setHeaderLabels(["名前", "太さ", "色"])
        self._stash_loose_handles()   # はみ出したハンドルを退避
        # 既存/再取得ラインにも現在の「選択不可」状態を反映
        lock = self._lock_select()
        for line in self._all_lines():
            self._apply_line_selectable(line, lock)
        self._populating = False
        self._refresh_group_combo()
        # 既存シーンに隙間埋めがあればカメラ追従を起動（再取得/再起動時）
        try:
            self._ensure_cam_tracking()
        except Exception:
            pass
        # v0.1.46: シーンの隠れ線 ON/OFF を UI へ反映し、オーバーライドを必要に応じて有効化
        try:
            self.chk_hidden_dash.blockSignals(True)
            self.chk_hidden_dash.setChecked(_hidden_scene_on())
            self.chk_hidden_dash.blockSignals(False)
            _og_override_sync()
        except Exception:
            pass

    def _refresh_group_combo(self):
        cur = self.group_combo.currentText()
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        names = [_short(g) for g in self._managed_groups()]
        if not names:
            names = [DEFAULT_GROUP]
        self.group_combo.addItems(names)
        idx = self.group_combo.findText(cur)
        if idx >= 0:
            self.group_combo.setCurrentIndex(idx)
        self.group_combo.blockSignals(False)

    def _selected_nodes(self):
        out = []
        for it in self.tree.selectedItems():
            n = it.data(0, QtCore.Qt.UserRole)
            if n and cmds.objExists(n):
                out.append(n)
        return out

    def _selected_lines(self):
        return [n for n in self._selected_nodes()
                if cmds.attributeQuery(TAG, node=n, exists=True)]

    # ---- 表示・非表示（ライン項目のチェックボックス） ----
    def _on_item_changed(self, item, column):
        if self._populating or column != 0:
            return
        node = item.data(0, QtCore.Qt.UserRole)
        if node and cmds.objExists(node):
            vis = item.checkState(0) == QtCore.Qt.Checked
            try:
                cmds.setAttr(node + ".visibility", vis)
            except Exception:
                cmds.warning("{} の表示属性を変更できません（接続/ロック）".format(_short(node)))
            self._refresh_intersect_targets_if_on()

    def _refresh_intersect_targets_if_on(self):
        """v0.1.44: SCRN ラインの可視/intersectOn 変更時に交差線対象を再評価。
        v0.1.45: 併せて交差線の画面 px 幅を SCRN 太さに合わせて再設定。
        対象が空になれば UI トグルも OFF へ戻す。"""
        try:
            if not getattr(self, "chk_intersect", None) or not self.chk_intersect.isChecked():
                return
            tgt = _scrn_intersect_meshes()
            if tgt:
                _edge_set_targets(tgt)
                _edge_set_params(thickness=_scrn_intersect_screen_thickness())
                try: cmds.refresh()
                except Exception: pass
            else:
                # 全 SCRN が非表示 or intersectOn=False → 交差線自体 OFF
                self.chk_intersect.setChecked(False)
        except Exception:
            pass

    # ---- 選択変更 → 太さUIを同期 ----
    def _update_panels(self):
        """選択種別に応じてライン用/グループ用パネルの表示を切替（全体は常時表示）。"""
        nodes = self._selected_nodes()
        has_line = any(cmds.attributeQuery(TAG, node=n, exists=True) for n in nodes)
        has_group = any(cmds.attributeQuery(GROUP_TAG, node=n, exists=True) for n in nodes)
        has_edge = any(cmds.attributeQuery(EDGE_TAG, node=n, exists=True) for n in nodes)
        self.w_line.setVisible(has_line)
        self.w_group.setVisible(has_group and not has_line)
        # 太さプロファイルはエッジライン選択時のみ
        self.w_profile.setVisible(has_line and has_edge)

    def _on_tree_selection(self):
        self._warned_connected.clear()   # 選択が変わったら接続警告の抑制をリセット
        self._update_panels()
        self._set_mult_widgets()
        # 選択ノードのコントローラーを Maya 選択（タイムスライダにキー表示）
        # v0.1.53: ビューポートの選択に一覧を合わせている最中は、Maya の選択（選んだメッシュ）を変えない
        ctrls = [] if self._syncing_sel else [_ctrl_of(n) for n in self._selected_nodes()]
        ctrls = [c for c in ctrls if c and cmds.objExists(c)]
        if ctrls:
            try:
                cmds.select(ctrls, r=True)
            except Exception:
                pass
        lines = self._selected_lines()
        if lines:
            ctrl = _ctrl_of(lines[0])
            t = self._thickness_of(lines[0])
            if t is not None:
                self._set_thickness_widgets(t)
            infl, cap, cmin = 0.0, None, None
            if ctrl:
                try:
                    infl = cmds.getAttr(ctrl + "." + CTRL_CURV)
                except Exception:
                    infl = 0.0
                try:
                    cap = cmds.getAttr(ctrl + "." + CTRL_CAP)
                except Exception:
                    cap = None
                if cmds.attributeQuery(CTRL_CMIN, node=ctrl, exists=True):
                    try:
                        cmin = cmds.getAttr(ctrl + "." + CTRL_CMIN)
                    except Exception:
                        cmin = None
            self._set_curv_widgets(infl, cap, cmin)
            self.ramp.set_points([list(p) for p in _profile_of_ctrl(ctrl)])
        self._update_key_colors()

    # ---- v0.1.53: ビューポートの選択 ↔ 一覧の選択 ----
    def _on_maya_selection(self):
        """ビューポートでメッシュを選んだら、そのメッシュから作った線を一覧で選ぶ。"""
        if self._populating or self._syncing_sel or not self.isVisible():
            return
        if not getattr(self, "chk_sel_sync", None) or not self.chk_sel_sync.isChecked():
            return
        try:
            sel = cmds.ls(sl=True, long=True) or []
        except Exception:
            return
        meshes = []
        for s in sel:
            if "." in s:
                continue     # コンポーネント選択は対象外
            if cmds.nodeType(s) == "mesh":
                s = (cmds.listRelatives(s, parent=True, f=True) or [s])[0]
            meshes.append(s)
        lines = []
        for m in meshes:
            for ln in self._src_to_lines.get(m, []):
                if ln not in lines:
                    lines.append(ln)
        if not lines:
            return      # ツールの線・コントローラーを選んだとき（一覧から選んだ等）は何もしない
        want = set(lines)
        self._syncing_sel = True
        try:
            self.tree.blockSignals(True)
            self.tree.clearSelection()
            first = None
            it = QtWidgets.QTreeWidgetItemIterator(self.tree)
            while it.value():
                item = it.value()
                n = item.data(0, QtCore.Qt.UserRole)
                if n and (cmds.ls(n, long=True) or [n])[0] in want:
                    item.setSelected(True)
                    if first is None:
                        first = item
                it += 1
            self.tree.blockSignals(False)
            if first is not None:
                self.tree.scrollToItem(first)
            self._on_tree_selection()      # パネル・数値の表示を合わせる（Maya の選択は変えない）
        finally:
            self.tree.blockSignals(False)
            self._syncing_sel = False

    # ---- v0.1.53: 線の設定の書き出し / 読み込み ----
    def _settings_dir(self):
        try:
            sn = cmds.file(q=True, sceneName=True)
            if sn:
                return os.path.dirname(sn)
            return cmds.workspace(q=True, rootDirectory=True) or ""
        except Exception:
            return ""

    def export_settings_dialog(self):
        lines = [(cmds.ls(n, long=True) or [n])[0] for n in self._selected_lines()] or _scene_lines()
        if not lines:
            cmds.warning(u"書き出す線がありません"); return
        default = os.path.join(self._settings_dir(), "toonline_settings.json")
        path, _flt = QtWidgets.QFileDialog.getSaveFileName(
            self, u"線の設定を書き出す", default, "JSON (*.json)")
        if not path:
            return
        try:
            n = export_line_settings(path, lines, self)
        except Exception as exc:
            cmds.warning(u"書き出しに失敗しました: {0}".format(exc)); return
        om2.MGlobal.displayInfo(u"線の設定を書き出しました（{0} 本）: {1}".format(n, path))

    def import_settings_dialog(self):
        path, _flt = QtWidgets.QFileDialog.getOpenFileName(
            self, u"線の設定を読み込む", self._settings_dir(), "JSON (*.json)")
        if not path:
            return
        targets = [(cmds.ls(n, long=True) or [n])[0] for n in self._selected_lines()] or _scene_lines()
        try:
            n, missing = import_line_settings(path, targets, self)
        except Exception as exc:
            cmds.warning(u"読み込みに失敗しました: {0}".format(exc)); return
        self.refresh_tree()
        msg = u"{0} 本の線に設定を当てました。".format(n)
        if missing:
            msg += u"\n\n見つからなかった線（元のメッシュの名前と種類が合う線がない）:\n" + \
                   u"\n".join(missing[:20]) + (u"\n…" if len(missing) > 20 else u"")
        QtWidgets.QMessageBox.information(self, u"線の設定を読み込む", msg)

    def select_source_meshes(self, line=None):
        """一覧で選んでいる線（または右クリックした線）の元のメッシュをビューポートで選ぶ。"""
        lines = self._selected_lines()
        if line and line not in lines:
            lines = [line]
        srcs = _source_meshes_of(lines)
        if not srcs:
            cmds.warning(u"元のメッシュが見つかりません")
            return
        self._syncing_sel = True
        try:
            cmds.select(srcs, r=True)
        finally:
            self._syncing_sel = False

    def _set_thickness_widgets(self, val):
        self.slider.blockSignals(True); self.spin.blockSignals(True)
        self.spin.setValue(val)
        self.slider.setValue(int(val * 1000))
        self.slider.blockSignals(False); self.spin.blockSignals(False)

    # ========== undo 制御 ==========
    def _begin_drag(self):
        """スライダー押下：ドラッグ全体を1つの undo チャンクにまとめる。"""
        if self._dragging:
            return      # 二重に開かない（閉じ損ねると Undo が効かなくなる）
        self._dragging = True
        cmds.undoInfo(openChunk=True)

    def _end_drag(self):
        if not self._dragging:
            return
        cmds.undoInfo(closeChunk=True)
        self._dragging = False

    # ========== リセット ==========
    def _sel_is_edge(self):
        """選択ラインがエッジライン（チューブ）を含むか。リセット既定値の出し分けに使う。"""
        return any(cmds.attributeQuery(EDGE_TAG, node=n, exists=True)
                   for n in self._selected_lines())

    def _reset_thickness(self):
        # エッジラインとハルラインで初期太さが異なるため選択種別で出し分ける
        dv = DEFAULT_EDGE_THICK if self._sel_is_edge() else DEFAULT_THICK
        self.spin.setValue(dv)   # spin の valueChanged が適用＋slider同期する

    def _reset_curv(self):
        self.cspin.setValue(DEFAULT_CURV)

    def _reset_cap(self):
        self.cap_spin.setValue(DEFAULT_CAP)

    def _reset_cmin(self):
        self.cmin_spin.setValue(DEFAULT_CMIN)

    def _reset_grpmult(self):
        self.grpspin.setValue(1.0)

    def _reset_gmult(self):
        self.gspin.setValue(1.0)

    # ========== 太さ倍率（グループ / 全体） ==========
    def _target_group(self):
        """倍率の対象グループ: グループ選択中はそれ、ライン選択中はその所属グループ。"""
        for n in self._selected_nodes():
            if cmds.attributeQuery(GROUP_TAG, node=n, exists=True):
                return n
        for n in self._selected_nodes():
            if cmds.attributeQuery(TAG, node=n, exists=True):
                g = _line_group(n)
                if g:
                    return g
        return None

    def _on_grpslider(self, v):
        val = v / 100.0
        self.grpspin.blockSignals(True); self.grpspin.setValue(val); self.grpspin.blockSignals(False)
        self._apply_group_mult(val)

    def _on_grpspin(self, val):
        self.grpslider.blockSignals(True); self.grpslider.setValue(int(val * 100)); self.grpslider.blockSignals(False)
        self._apply_group_mult(val)

    def _apply_group_mult(self, val):
        grp = self._target_group()
        if not grp:
            return
        chunk = not self._dragging
        if chunk:
            cmds.undoInfo(openChunk=True)
        try:
            grpctrl = _ensure_group_ctrl(grp)
            self._set_ctrl_attr(grpctrl, GMULT, val, "グループ倍率")
        finally:
            if chunk:
                cmds.undoInfo(closeChunk=True)
        self._update_mult_labels()
        self._refresh_intersect_targets_if_on()

    def _on_gslider(self, v):
        val = v / 100.0
        self.gspin.blockSignals(True); self.gspin.setValue(val); self.gspin.blockSignals(False)
        self._apply_global_mult(val)

    def _on_gspin(self, val):
        self.gslider.blockSignals(True); self.gslider.setValue(int(val * 100)); self.gslider.blockSignals(False)
        self._apply_global_mult(val)

    def _apply_global_mult(self, val):
        chunk = not self._dragging
        if chunk:
            cmds.undoInfo(openChunk=True)
        try:
            gc = self._target_global_ctrl(create=True)
            self._set_ctrl_attr(gc, GMULT, val, "全体倍率")
        finally:
            if chunk:
                cmds.undoInfo(closeChunk=True)
        self._update_mult_labels()
        self._refresh_intersect_targets_if_on()

    def _update_mult_labels(self):
        """ツリーのグループ倍率（太さ列）を再描画（再構築せず）。"""
        self._populating = True
        # v0.1.53: サブグループ・リファレンスの見出しの下のグループも更新する（以前は一番上の階層だけ）
        stack = [self.tree.invisibleRootItem()]
        while stack:
            parent = stack.pop()
            for i in range(parent.childCount()):
                gi = parent.child(i)
                stack.append(gi)
                node = gi.data(0, QtCore.Qt.UserRole)
                if node and cmds.objExists(node) and cmds.attributeQuery(GROUP_TAG, node=node, exists=True):
                    grpctrl = _ctrl_of(node)
                    gm = 1.0
                    if grpctrl:
                        try:
                            gm = cmds.getAttr(grpctrl + "." + GMULT)
                        except Exception:
                            pass
                    gi.setText(1, "x{:.2f}".format(gm))
        self._populating = False

    def _target_global_ctrl(self, create=False):
        """全体倍率の対象。リファレンス等の線（別 ROOT）を選んでいればそのキャラの全体コントローラー、
        それ以外はこのシーンの全体コントローラー（create=True なら無ければ作る）。"""
        for n in self._selected_nodes():
            if _is_foreign(n):
                gc = _global_ctrl_for(n)
                if gc:
                    return gc
        return _ensure_global_ctrl() if create else _find_global_ctrl()

    def _update_global_label(self):
        lb = getattr(self, "lbl_global", None)
        if lb is None:
            return
        ns = ""
        for n in self._selected_nodes():
            if _is_foreign(n) and _global_ctrl_for(n):
                ns = _short(_root_of(n)).rpartition(":")[0]
                break
        lb.setText(u"全体倍率（{0}）".format(ns) if ns else u"全体倍率")

    def _set_mult_widgets(self):
        """選択に応じてグループ倍率・全体倍率スライダーを現在値へ同期。"""
        g = 1.0
        self._update_global_label()
        gc = self._target_global_ctrl()
        if gc and cmds.attributeQuery(GMULT, node=gc, exists=True):
            try:
                g = cmds.getAttr(gc + "." + GMULT)
            except Exception:
                g = 1.0
        self.gspin.blockSignals(True); self.gslider.blockSignals(True)
        self.gspin.setValue(g); self.gslider.setValue(int(g * 100))
        self.gspin.blockSignals(False); self.gslider.blockSignals(False)
        grp = self._target_group()
        gm = 1.0
        grpctrl = _ctrl_of(grp) if grp else None
        if grpctrl and cmds.attributeQuery(GMULT, node=grpctrl, exists=True):
            try:
                gm = cmds.getAttr(grpctrl + "." + GMULT)
            except Exception:
                gm = 1.0
        self.grpspin.blockSignals(True); self.grpslider.blockSignals(True)
        self.grpspin.setValue(gm); self.grpslider.setValue(int(gm * 100))
        self.grpspin.blockSignals(False); self.grpslider.blockSignals(False)

    # ========== 太さ（選択ラインのみ） ==========
    def _on_slider(self, v):
        val = v / 1000.0
        self.spin.blockSignals(True); self.spin.setValue(val); self.spin.blockSignals(False)
        self._apply_thickness(val)

    def _on_spin(self, val):
        self.slider.blockSignals(True); self.slider.setValue(int(val * 1000)); self.slider.blockSignals(False)
        self._apply_thickness(val)

    # ---- コントローラー属性の設定可否チェック（接続/ロックで UI 制御不可なら警告） ----
    def _set_ctrl_attr(self, ctrl, attr, val, label, as_string=False):
        """ctrl.attr を設定。接続/ロックで設定不可なら一度だけ忠告して False を返す。
        （コントローラーにコンストレイント等のノードが繋がっていると UI から変更できない）"""
        if not ctrl or not cmds.attributeQuery(attr, node=ctrl, exists=True):
            return False
        plug = ctrl + "." + attr
        settable = True
        try:
            settable = cmds.getAttr(plug, settable=True)
        except Exception:
            settable = True
        if not settable:
            if plug not in self._warned_connected:
                self._warned_connected.add(plug)
                src = cmds.listConnections(plug, s=True, d=False, p=True) or []
                why = ("接続元: " + src[0]) if src else "ロックされています"
                cmds.warning("{} の「{}」は他のノードに接続されているため UI から変更できません"
                             "（{}）。アニメ/接続を外すか K でキーしてください。"
                             .format(_short(ctrl), label, why))
            return False
        try:
            if as_string:
                cmds.setAttr(plug, val, type="string")
            else:
                cmds.setAttr(plug, val)
            return True
        except Exception:
            return False

    def _apply_thickness(self, val):
        lines = self._selected_lines()
        if not lines:
            return
        chunk = not self._dragging   # ドラッグ中は _begin/_end_drag のチャンクに含める
        if chunk:
            cmds.undoInfo(openChunk=True)
        try:
            for line in lines:
                # コントローラー属性 thickness を設定（offset へ接続済みなので反映される）
                ctrl = _ensure_line_anim(line, val)
                self._set_ctrl_attr(ctrl, CTRL_THICK, val, "太さ")
        finally:
            if chunk:
                cmds.undoInfo(closeChunk=True)
        self._update_key_colors()
        # ツリーの太さ表示を更新
        self._populating = True
        for it in self.tree.selectedItems():
            n = it.data(0, QtCore.Qt.UserRole)
            if n and cmds.attributeQuery(TAG, node=n, exists=True):
                it.setText(1, "{:.3f}".format(val))
        self._populating = False
        # v0.1.45: 選択に SCRN ラインが含まれてて交差線 ON なら、太さ変更を交差線にも反映
        try:
            if any(cmds.attributeQuery(SCRN_TAG, node=ln, exists=True) for ln in lines):
                self._refresh_intersect_targets_if_on()
        except Exception:
            pass

    # ========== 曲率起伏（選択ラインのみ） ==========
    def _on_cslider(self, v):
        val = v / 100.0
        self.cspin.blockSignals(True); self.cspin.setValue(val); self.cspin.blockSignals(False)
        self._apply_curvature(CTRL_CURV)

    def _on_cspin(self, val):
        self.cslider.blockSignals(True); self.cslider.setValue(int(val * 100)); self.cslider.blockSignals(False)
        self._apply_curvature(CTRL_CURV)

    def _on_cap_slider(self, v):
        val = v / 100.0
        self.cap_spin.blockSignals(True); self.cap_spin.setValue(val); self.cap_spin.blockSignals(False)
        self._apply_curvature(CTRL_CAP)

    def _on_cap_spin(self, val):
        self.cap_slider.blockSignals(True); self.cap_slider.setValue(int(val * 100)); self.cap_slider.blockSignals(False)
        self._apply_curvature(CTRL_CAP)

    def _on_cmin_slider(self, v):
        val = v / 100.0
        self.cmin_spin.blockSignals(True); self.cmin_spin.setValue(val); self.cmin_spin.blockSignals(False)
        self._apply_curvature(CTRL_CMIN)

    def _on_cmin_spin(self, val):
        self.cmin_slider.blockSignals(True); self.cmin_slider.setValue(int(val * 100)); self.cmin_slider.blockSignals(False)
        self._apply_curvature(CTRL_CMIN)

    def _set_curv_widgets(self, infl, cap=None, cmin=None):
        self.cslider.blockSignals(True); self.cspin.blockSignals(True)
        self.cspin.setValue(infl)
        self.cslider.setValue(int(infl * 100))
        self.cslider.blockSignals(False); self.cspin.blockSignals(False)
        if cap is not None:
            self.cap_spin.blockSignals(True); self.cap_slider.blockSignals(True)
            self.cap_spin.setValue(cap)
            self.cap_slider.setValue(int(cap * 100))
            self.cap_spin.blockSignals(False); self.cap_slider.blockSignals(False)
        if cmin is not None:
            self.cmin_spin.blockSignals(True); self.cmin_slider.blockSignals(True)
            self.cmin_spin.setValue(cmin)
            self.cmin_slider.setValue(int(cmin * 100))
            self.cmin_spin.blockSignals(False); self.cmin_slider.blockSignals(False)

    def _apply_profile(self):
        lines = self._selected_lines()
        if not lines:
            return
        s = _serialize_profile(self.ramp.points())
        chunk = not self._dragging   # ドラッグ中は _begin/_end_drag のチャンクに含める
        if chunk:
            cmds.undoInfo(openChunk=True)
        try:
            for line in lines:
                ctrl = _ensure_line_anim(line, self.spin.value())
                self._set_ctrl_attr(ctrl, CTRL_PROFILE, s, "太さプロファイル", as_string=True)
                _update_line_weights(line)
        finally:
            if chunk:
                cmds.undoInfo(closeChunk=True)

    def _reset_profile(self):
        self.ramp.set_points([[0.0, 1.0], [1.0, 1.0]])
        self._apply_profile()

    def _apply_curvature(self, which=None):
        """which = 動かした項目（CTRL_CURV / CTRL_CAP / CTRL_CMIN）。v0.1.52: 複数ラインを選んで
        1 項目を動かしたとき、動かしていない項目まで UI の値で上書きしないよう、その項目だけ入れる
        （None のときは 3 項目とも入れる）。"""
        lines = self._selected_lines()
        if not lines:
            return
        vals = ((CTRL_CURV, self.cspin.value(), "曲率起伏"),
                (CTRL_CAP, self.cap_spin.value(), "曲率上限"),
                (CTRL_CMIN, self.cmin_spin.value(), "曲率下限"))
        chunk = not self._dragging   # ドラッグ中は _begin/_end_drag のチャンクに含める
        if chunk:
            cmds.undoInfo(openChunk=True)
        try:
            for line in lines:
                # コントローラー属性 curvature / curvatureCap / curvatureMin を設定 → scriptJob
                #   で頂点ウェイトが再計算されるが、即時反映のため明示的にも更新する
                #   （ジョブは同じ値なら計算を省く）。
                ctrl = _ensure_line_anim(line, self.spin.value())
                for at, v, label in vals:
                    if which is None or at == which:
                        self._set_ctrl_attr(ctrl, at, v, label)
                _update_line_weights(line)
        finally:
            if chunk:
                cmds.undoInfo(closeChunk=True)
        self._update_key_colors()

    # ========== キー（アニメーション） ==========
    def _key_line_attr(self, attr):
        lines = self._selected_lines()
        if not lines:
            cmds.warning("キーを打つラインをツリーで選択してください"); return
        cmds.undoInfo(openChunk=True)
        try:
            for line in lines:
                ctrl = _ensure_line_anim(line, self.spin.value())
                try:
                    cmds.setKeyframe(ctrl + "." + attr)
                except Exception:
                    pass
        finally:
            cmds.undoInfo(closeChunk=True)
        self._update_key_colors()

    def _key_thickness(self):
        self._key_line_attr(CTRL_THICK)

    def _key_curv(self):
        self._key_line_attr(CTRL_CURV)

    def _key_cap(self):
        self._key_line_attr(CTRL_CAP)

    def _key_cmin(self):
        self._key_line_attr(CTRL_CMIN)

    def _key_grpmult(self):
        grp = self._target_group()
        if not grp:
            cmds.warning("キーを打つグループをツリーで選択してください"); return
        gctrl = _ensure_group_ctrl(grp)
        cmds.undoInfo(openChunk=True)
        try:
            try:
                cmds.setKeyframe(gctrl + "." + GMULT)
            except Exception:
                pass
        finally:
            cmds.undoInfo(closeChunk=True)
        self._update_key_colors()

    def _select_global_ctrl(self):
        """全体コントローラーを Maya 選択（タイムスライダにキーを表示）。"""
        gc = self._target_global_ctrl(create=True)
        try:
            cmds.select(gc, r=True)
        except Exception:
            pass
        self._update_key_colors()

    def _key_gmult(self):
        gc = self._target_global_ctrl(create=True)
        cmds.undoInfo(openChunk=True)
        try:
            try:
                cmds.setKeyframe(gc + "." + GMULT)
            except Exception:
                pass
        finally:
            cmds.undoInfo(closeChunk=True)
        self._update_key_colors()

    # ---- 数値欄のキー色（アトリビュートエディター風） ----
    def _style_spin(self, spin, state):
        if state == "key":
            css = "QDoubleSpinBox{background-color:#c25450; color:#fff;}"        # 赤=現フレームがキー
        elif state == "anim":
            css = "QDoubleSpinBox{background-color:#e0a6a6; color:#000;}"        # ピンク=アニメ有り（黒文字）
        else:
            css = ""
        spin.setStyleSheet(css)

    def _update_key_colors(self):
        lines = self._selected_lines()
        lctrl = _ctrl_of(lines[0]) if lines else None
        for spin, at in ((self.spin, CTRL_THICK), (self.cspin, CTRL_CURV),
                         (self.cap_spin, CTRL_CAP), (self.cmin_spin, CTRL_CMIN)):
            state = "none"
            if lctrl and cmds.attributeQuery(at, node=lctrl, exists=True):
                state = _attr_key_state(lctrl + "." + at)
            self._style_spin(spin, state)
        # グループ倍率
        grp = self._target_group()
        grpctrl = _ctrl_of(grp) if grp else None
        st = "none"
        if grpctrl and cmds.attributeQuery(GMULT, node=grpctrl, exists=True):
            st = _attr_key_state(grpctrl + "." + GMULT)
        self._style_spin(self.grpspin, st)
        # 全体倍率
        gc = self._target_global_ctrl()
        st = "none"
        if gc and cmds.attributeQuery(GMULT, node=gc, exists=True):
            st = _attr_key_state(gc + "." + GMULT)
        self._style_spin(self.gspin, st)

    def _on_time_changed(self):
        """フレーム変更時：選択中の値をスライダーへ反映し、キー色を更新（ライン/グループ/全体）。"""
        lines = self._selected_lines()
        if lines:
            t = self._thickness_of(lines[0])
            if t is not None:
                self._set_thickness_widgets(t)   # blockSignals 済み → 適用は走らない
            ctrl = _ctrl_of(lines[0])
            if ctrl:
                infl = cap = cmin = None
                try:
                    infl = cmds.getAttr(ctrl + "." + CTRL_CURV)
                except Exception:
                    pass
                try:
                    cap = cmds.getAttr(ctrl + "." + CTRL_CAP)
                except Exception:
                    pass
                if cmds.attributeQuery(CTRL_CMIN, node=ctrl, exists=True):
                    try:
                        cmin = cmds.getAttr(ctrl + "." + CTRL_CMIN)
                    except Exception:
                        pass
                if infl is not None:
                    self._set_curv_widgets(infl, cap, cmin)
        # グループ/全体倍率の値・キー色もラインと同様に追従
        self._set_mult_widgets()
        self._update_mult_labels()
        self._update_key_colors()
        # 隠蔽検知ハル: 再生/スクラブでメッシュ変形→シルエットが変わるので再計算
        if _OCC_ENABLED:
            self._occ_cam_key = None   # 次の poll で必ず更新されるようリセット
            self._refresh_occlusion()

    # ========== カラー ==========
    def _refresh_swatch(self):
        r, g, b = [int(c * 255) for c in self._color]
        self.btn_color.setStyleSheet(
            "background-color: rgb({},{},{}); border:1px solid #222;".format(r, g, b))

    def _pick_color(self):
        c0 = QtGui.QColor.fromRgbF(*self._color)
        c = QtWidgets.QColorDialog.getColor(c0, self, "共通カラー")
        if c.isValid():
            self._color = [c.redF(), c.greenF(), c.blueF()]
            self._refresh_swatch()
            if cmds.objExists(SHADER):
                cmds.undoInfo(openChunk=True)
                try:
                    cmds.setAttr(SHADER + ".outColor",
                                 self._color[0], self._color[1], self._color[2],
                                 type="double3")
                finally:
                    cmds.undoInfo(closeChunk=True)
            # v0.1.36: 交差線が有効なら線色も追従
            try:
                if _edge_active():
                    _edge_set_params(color=self._color)
            except Exception:
                pass
            self.refresh_tree()

    def _apply_line_color(self, line, rgb):
        """v0.1.25: 1 本のラインに色を適用するコア処理（undo チャンクは呼び出し側で）。"""
        sh = self._shape_of(line)
        if not sh:
            return
        # フレネル/スクリーンはシェーダの lineColor（v0.1.46: SCRN はライン属性経由）
        if (cmds.attributeQuery(FRES_TAG, node=line, exists=True)
                or cmds.attributeQuery(SCRN_TAG, node=line, exists=True)
                or _attr_exists(line, LN_COLOR)):   # v0.1.50: 詳細設定を使ったエッジ線も属性経由
            _scrn_set_color(line, rgb)
            return
        ss, sg = self._ensure_line_shader(line)
        cmds.setAttr(ss + ".outColor", rgb[0], rgb[1], rgb[2], type="double3")
        cmds.sets(sh, e=True, forceElement=sg)

    def set_individual_color(self):
        """選択ラインに個別カラーを割り当てる（専用シェーダを生成）。"""
        lines = self._selected_lines()
        if not lines:
            cmds.warning("色を変えるラインをツリーで選択してください"); return
        init = self._line_color(lines[0]) or [0.0, 0.0, 0.0]
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor.fromRgbF(*init), self, "ラインの個別カラー")
        if not c.isValid():
            return
        rgb = [c.redF(), c.greenF(), c.blueF()]
        cmds.undoInfo(openChunk=True)
        try:
            for line in lines:
                self._apply_line_color(line, rgb)
        finally:
            cmds.undoInfo(closeChunk=True)
        self.refresh_tree()

    def reset_to_shared_color(self):
        """選択ラインを共通カラー（ブラック）に戻し、専用シェーダを片付ける。"""
        lines = self._selected_lines()
        if not lines:
            cmds.warning("共通カラーに戻すラインをツリーで選択してください"); return
        _, sg = _ensure_shader(self._color)
        cmds.undoInfo(openChunk=True)
        try:
            for line in lines:
                sh = self._shape_of(line)
                # フレネル/スクリーンは SG を共有に差し替えると線が出ないため、色だけ共通値に戻す
                # v0.1.50: 詳細設定を使ったエッジ線は専用シェーダのまま色だけ戻す（接続を保つ）
                if (cmds.attributeQuery(FRES_TAG, node=line, exists=True)
                        or cmds.attributeQuery(SCRN_TAG, node=line, exists=True)
                        or _attr_exists(line, LN_COLOR)):
                    _scrn_set_color(line, self._color)
                    continue
                if sh:
                    cmds.sets(sh, e=True, forceElement=sg)
                base = COL_PREFIX + _base(line)
                for n in (base + "_SG", base + "_SS"):
                    if cmds.objExists(n):
                        try:
                            cmds.delete(n)
                        except Exception:
                            pass
        finally:
            cmds.undoInfo(closeChunk=True)
        self.refresh_tree()

    # ========== 隙間埋め（2オブジェクト・facing 駆動／カメラ依存・VP2/バッチ対応） ==========
    def toggle_gapfill(self, *args):
        """選択ハルライン A に『隙間埋めオブジェクト B』を追加/削除（トグル）。
        B = 元メッシュ複製(線色・背面法)。カメラに対して寝た面（シルエット）の頂点だけ
        ベース A の頂点位置(= A の offset)まで押し出し、正面/背面の面は元メッシュ表面に
        張り付かせる。A の輪郭が浮いて見える隙間を B のスカートで塞ぐ。"""
        lines = [l for l in self._selected_lines() if _is_hull_line(l)]
        if not lines:
            cmds.warning("ハルラインをツリーで選択してください"); return
        cmds.undoInfo(openChunk=True)
        added = removed = 0
        try:
            for line in lines:
                if _gapfill_of(line):
                    if self._remove_gapfill(line):
                        removed += 1
                else:
                    if self._create_gapfill(line):
                        added += 1
            cmds.select(clear=True)
            self._stash_loose_handles()
            self._ensure_cam_tracking()
        finally:
            cmds.undoInfo(closeChunk=True)
        self.refresh_tree()
        cmds.warning("隙間埋め: 追加 {} / 削除 {}（facing で寝た面を押し出してスカートで塞ぐ）"
                     .format(added, removed))

    def _create_gapfill(self, line):
        """ライン A の元メッシュを複製し、facing 駆動で隙間を塞ぐ背面法オブジェクト B を作る。"""
        src = _line_src_shape(line)
        if not src or not cmds.objExists(src):
            return False
        par = cmds.listRelatives(src, parent=True, f=True) or []
        if not par:
            return False
        srcT = par[0]
        b = cmds.duplicate(srcT, name=_base(srcT) + "_gapfill", rr=True)[0]
        _shapes_out_of_ns(b)
        for k in cmds.listRelatives(b, children=True, type="transform", f=True) or []:
            cmds.delete(k)
        bsh = cmds.listRelatives(b, shapes=True, type="mesh", ni=True, f=True)
        if not bsh:
            cmds.delete(b); return False
        bshape = bsh[0]
        cmds.delete(b, constructionHistory=True)
        # ★ハル A と同じく B も全エッジを soft 化。B の push 方向を連続化して
        # スパイク/ジャギを避ける（facing 駆動の weight が隣接頂点でも滑らかに続く）。
        try:
            cmds.polySoftEdge(bshape + ".e[*]", a=180, ch=False)
        except Exception:
            pass
        # 変形追従の textureDeformer（offset は後で A の太さに接続。weightList=facing）
        td = cmds.textureDeformer(bshape, strength=0, offset=0.0, direction="Normal")
        bdefm = td[0]
        handle = None
        for c in (cmds.listConnections(bdefm, type="transform") or []):
            if "textureDeformerHandle" in _short(c):
                handle = c; break
        if handle is None and len(td) > 1:
            handle = td[1]
        try:
            cmds.connectAttr(src + ".outMesh", bdefm + ".input[0].inputGeometry", f=True)
        except Exception:
            cmds.warning("隙間埋めの変形追従の接続に失敗（静的に生成）")
        self._tuck_handle(handle)
        # 裏面のみ表示（opposite=1/doubleSided=0）。頂点を膨らませるとシルエット付近で背面がカメラ方向を
        # 向くので、その背面が見えて隙間を埋める。正面側(d>0)は前面なのでカリングされ本体に黒面が出ない。
        try:
            cmds.setAttr(bshape + ".doubleSided", 0)
            cmds.setAttr(bshape + ".opposite", 1)
        except Exception:
            pass
        # A と同じシェーディング（線色）を割り当て
        sgs = cmds.listConnections(self._shape_of(line), type="shadingEngine") or []
        sg = sgs[0] if sgs else None
        if not sg:
            _, sg = _ensure_shader(self._color)
        try:
            cmds.sets(bshape, e=True, forceElement=sg)
        except Exception:
            pass
        # タグ → ホルダーへ（先に親付けしてから拘束）
        if not cmds.attributeQuery(GAPFILL_TAG, node=b, exists=True):
            cmds.addAttr(b, ln=GAPFILL_TAG, at="bool", dv=True)
        _ensure_root()
        if not cmds.objExists(GAPFILL_HOLDER):
            cmds.group(em=True, name=GAPFILL_HOLDER, parent=ROOT)
        try:
            b = cmds.parent(b, GAPFILL_HOLDER)[0]
        except Exception:
            pass
        try:
            cmds.parentConstraint(srcT, b, maintainOffset=False)
            cmds.scaleConstraint(srcT, b, maintainOffset=False)
        except Exception:
            pass
        # offset = A の太さ（押し出した頂点が A の頂点位置に届く）
        ldefm = _line_deformer(line)
        if ldefm:
            _connect(ldefm + ".offset", bdefm + ".offset")
        else:
            try:
                cmds.setAttr(bdefm + ".offset", DEFAULT_THICK)
            except Exception:
                pass
        # リンク・選択不可・初回 facing 計算
        if not cmds.attributeQuery(GAPFILL_LINK, node=line, exists=True):
            cmds.addAttr(line, ln=GAPFILL_LINK, at="message")
        try:
            cmds.connectAttr(b + ".message", line + "." + GAPFILL_LINK, f=True)
        except Exception:
            pass
        self._apply_line_selectable(b, self._lock_select())
        _update_gapfill_weights(b)
        return True

    def _remove_gapfill(self, line):
        """ライン A の隙間埋めオブジェクト B を削除する。"""
        b = _gapfill_of(line)
        if not b:
            return False
        try:
            cmds.delete(b)
        except Exception:
            pass
        return True

    # ========== 生成 ==========
    def create_outlines(self, *args, **kwargs):
        # target_group が来ればそのグループへ、無ければコンボの対象グループへ
        target_group = kwargs.get("target_group", None)
        sel = cmds.ls(sl=True, long=True, type="transform")
        if not sel:
            cmds.warning("メッシュを選択してください"); return
        # 新規ハルラインも UI の現在値ではなく初期値で生成する
        thick = DEFAULT_THICK
        _, sg = _ensure_shader(self._color)
        if target_group and cmds.objExists(target_group):
            grp = target_group
        else:
            grp = self._current_group()

        cmds.undoInfo(openChunk=True)
        made = []
        try:
            for obj in sel:
                shps = cmds.listRelatives(obj, shapes=True, type="mesh", ni=True, f=True)
                if not shps:
                    continue
                src = shps[0]

                # 複製は元と同じ位置（同じ親・同じ TRS）に出る。トランスフォームは動かさない。
                # → 元と同じ変換空間に留まるので原点に飛ばない。
                # v0.1.52: リファレンスしたメッシュ（chr:head）でも線は名前空間の外に作る
                dup = cmds.duplicate(obj, name=_base(obj) + "_outline", rr=True)[0]
                _shapes_out_of_ns(dup)
                for k in cmds.listRelatives(dup, children=True, type="transform", f=True) or []:
                    cmds.delete(k)
                dshape = cmds.listRelatives(dup, shapes=True, type="mesh", ni=True, f=True)[0]

                # 複製のヒストリ／デフォーマを除去して inMesh を空ける
                cmds.delete(dup, constructionHistory=True)

                # ★ハル複製の全エッジを soft 化してシェル頂点法線を近傍面法線の平均にする。
                # これで textureDeformer(direction="Normal") 時の push 方向が頂点間で連続化し、
                # 円柱キャップ↔サイド境界・立方体の稜線・ポリキューブの角などの「頂点法線の不連続」
                # 由来で出ていた**ハル外周の跳ね/飛び火**が原理的に消える。
                # 元メッシュは複製後に捨てられる履歴しか見ないので影響しない。ch=False で履歴ノードを
                # 残さず inMesh の normal データを書き換える。全エッジ対象を .e[*] で明示（引数省略だと
                # 版により選択依存になり no-op になるケースがある）。失敗は無視。
                try:
                    cmds.polySoftEdge(dshape + ".e[*]", a=180, ch=False)
                except Exception:
                    pass

                # --- inverted hull を構築 ---
                # 1) textureDeformer(direction="Normal") で各頂点を「現在のサーフェス法線」方向へ
                #    一定距離(offset)オフセット。direction 既定の "Handle" だとハンドル軸(Y)にしか
                #    動かないため必ず "Normal" を指定する。法線は変形後メッシュから毎フレーム
                #    再計算されるので、元メッシュを変形しても太さ(offset 距離)は一定に保たれる
                #    （blendShape はバインド時固定デルタで変形すると太さが変わるため不使用）。
                #    strength=0・テクスチャ無しで、純粋な法線方向の一定オフセットだけにする。
                #    まず静的な複製に付け、その後ベース入力へ outMesh を流して追従させる
                #    （先に inMesh へ直結すると評価が壊れて歪むので繋がない）。
                td = cmds.textureDeformer(dshape, strength=0, offset=thick, direction="Normal")
                defm = td[0]
                # ハンドルはデフォーマに接続された transform として特定する
                # （戻り値に含まれない版があるため接続から拾う。方向フィルタは付けない）。
                handle = None
                for c in (cmds.listConnections(defm, type="transform") or []):
                    if "textureDeformerHandle" in _short(c):
                        handle = c
                        break
                if handle is None and len(td) > 1:
                    handle = td[1]

                # 2) 変形追従: deformer のベース入力に 元の outMesh を流し込む。
                try:
                    cmds.connectAttr(src + ".outMesh", defm + ".input[0].inputGeometry", f=True)
                except Exception:
                    cmds.warning("変形追従の接続に失敗（静的な輪郭として生成）")

                # deformer ハンドルは direction="Normal" では不要だが削除すると offset が
                # 効かなくなるため、アウトライナーから隠してホルダーへ退避する。
                self._tuck_handle(handle)

                # 3) 法線反転は shape の opposite 属性で行う（ヒストリノードを足さない）。
                #    doubleSided=0 のバックフェースカリングと合わせて輪郭のリムだけ見せる。
                cmds.setAttr(dshape + ".doubleSided", 0)
                try:
                    cmds.setAttr(dshape + ".opposite", 1)
                except Exception:
                    pass
                cmds.sets(dshape, e=True, forceElement=sg)
                if not cmds.attributeQuery(TAG, node=dup, exists=True):
                    cmds.addAttr(dup, ln=TAG, at="bool", dv=True)

                # グループへ（ワールド位置を保持したまま移動 → 重なりは維持）
                dup = cmds.parent(dup, grp)[0]

                # ラインコントローラー（独立ノード）を作成。太さは offset へ直結（DGでアニメ可）、
                # 曲率/上限は scriptJob で頂点ウェイトを追従。
                ctrl = _ensure_line_anim(dup, thick)
                # 各項目を初期値で生成（太さ/曲率起伏/曲率上限/末端細り）
                for at, dv in ((CTRL_THICK, DEFAULT_THICK), (CTRL_CURV, DEFAULT_CURV),
                               (CTRL_CAP, DEFAULT_CAP), (CTRL_CMIN, DEFAULT_CMIN),
                               (CTRL_TAPER, 0.0)):
                    if cmds.attributeQuery(at, node=ctrl, exists=True):
                        try:
                            cmds.setAttr(ctrl + "." + at, dv)
                        except Exception:
                            pass
                _update_curv_weights(dup)
                self._apply_line_selectable(dup, self._lock_select())
                made.append(dup)
            # 生成したラインはビューポート/アウトライナーで選択状態にしない
            cmds.select(clear=True)
            # 取りこぼしたハンドルがあればトップから退避（確実化）
            self._stash_loose_handles()
        finally:
            cmds.undoInfo(closeChunk=True)

        self.refresh_tree()

    # ========== エッジライン（チューブ） ==========
    def create_fresnel_outline(self, *args):
        """選択メッシュにフレネル輪郭ライン（カメラから見て寝た縁＝シルエット/凹みに線）を生成。
        背面法の凹凸ズレが出ず、VP2/Maya Software のバッチでも反映。カメラ依存。"""
        sel = cmds.ls(sl=True, long=True, type="transform")
        if not sel:
            cmds.warning("メッシュを選択してください"); return
        thick = DEFAULT_THICK

        cmds.undoInfo(openChunk=True)
        made = []
        try:
            # v0.1.42: 複数選択時は 1 グループへまとめる
            batch_grp = self._ensure_batch_group(sel)
            for obj in sel:
                shps = cmds.listRelatives(obj, shapes=True, type="mesh", ni=True, f=True)
                if not shps:
                    continue
                src = shps[0]
                # v0.1.31→v0.1.42: 複数選択時は batch_grp、単一は per-mesh
                grp = batch_grp or self._ensure_mesh_group(_base(obj))

                # 元と同じ位置に複製 → 履歴削除で静的化
                dup = cmds.duplicate(obj, name=_base(obj) + "_fresnel", rr=True)[0]
                _shapes_out_of_ns(dup)
                for k in cmds.listRelatives(dup, children=True, type="transform", f=True) or []:
                    cmds.delete(k)
                dshape = cmds.listRelatives(dup, shapes=True, type="mesh", ni=True, f=True)[0]
                cmds.delete(dup, constructionHistory=True)

                # z-fighting 回避の微小オフセット + 変形追従（hull と同じ deformer ベース入力方式）
                td = cmds.textureDeformer(dshape, strength=0, offset=FRES_ZOFFSET, direction="Normal")
                defm = td[0]
                handle = None
                for c in (cmds.listConnections(defm, type="transform") or []):
                    if "textureDeformerHandle" in _short(c):
                        handle = c
                        break
                if handle is None and len(td) > 1:
                    handle = td[1]
                try:
                    cmds.connectAttr(src + ".outMesh", defm + ".input[0].inputGeometry", f=True)
                except Exception:
                    cmds.warning("変形追従の接続に失敗（静的な輪郭として生成）")
                self._tuck_handle(handle)
                # 微小オフセットは固定（太さは facingRatio しきい値で制御）→ weightList は使わない
                try:
                    cmds.setAttr(defm + ".offset", FRES_ZOFFSET, lock=True)
                except Exception:
                    pass

                # フレネルシェーダ（dx11Shader + HLSL、VP2 ハードウェア）を割り当て
                _build_fresnel_network(dup, dshape, self._color)

                if not cmds.attributeQuery(TAG, node=dup, exists=True):
                    cmds.addAttr(dup, ln=TAG, at="bool", dv=True)
                if not cmds.attributeQuery(FRES_TAG, node=dup, exists=True):
                    cmds.addAttr(dup, ln=FRES_TAG, at="bool", dv=True)

                dup = cmds.parent(dup, grp)[0]
                ctrl = _ensure_line_anim(dup, thick)   # 太さ→しきい値、追従。曲率ジョブは張らない
                for at, dv in ((CTRL_THICK, DEFAULT_THICK), (CTRL_CURV, DEFAULT_CURV),
                               (CTRL_CAP, DEFAULT_CAP), (CTRL_CMIN, DEFAULT_CMIN),
                               (CTRL_TAPER, 0.0)):
                    if cmds.attributeQuery(at, node=ctrl, exists=True):
                        try:
                            cmds.setAttr(ctrl + "." + at, dv)
                        except Exception:
                            pass
                self._apply_line_selectable(dup, self._lock_select())
                made.append(dup)
            cmds.select(clear=True)
            self._stash_loose_handles()
        finally:
            cmds.undoInfo(closeChunk=True)
        self.refresh_tree()
        if made:
            cmds.warning("フレネル輪郭はハードウェアシェーダです。ビューポートの "
                         "「テクスチャ表示 ON（ホットキー 6）」で表示されます。")

    def create_screen_outline(self, *args):
        """選択メッシュにスクリーン輪郭（dx11 頂点シェーダでクリップ空間に一定px押し出し）を生成。
        ワールド押し出しの隙間/浮きが出ず、凸部でも均一太さ。重なり/シルエットのみ・カメラ依存。"""
        sel = cmds.ls(sl=True, long=True, type="transform")
        if not sel:
            cmds.warning("メッシュを選択してください"); return
        thick = DEFAULT_THICK

        cmds.undoInfo(openChunk=True)
        made = []
        try:
            # v0.1.42: 複数選択時は 1 グループへまとめる（単一は per-mesh）
            batch_grp = self._ensure_batch_group(sel)
            for obj in sel:
                shps = cmds.listRelatives(obj, shapes=True, type="mesh", ni=True, f=True)
                if not shps:
                    continue
                src = shps[0]
                # v0.1.31: オブジェクトごとのトップグループに入れる
                # v0.1.42: 複数選択時は batch_grp（共通）、単一は per-mesh グループ
                grp = batch_grp or self._ensure_mesh_group(_base(obj))
                dup = cmds.duplicate(obj, name=_base(obj) + "_screen", rr=True)[0]
                _shapes_out_of_ns(dup)
                for k in cmds.listRelatives(dup, children=True, type="transform", f=True) or []:
                    cmds.delete(k)
                dshape = cmds.listRelatives(dup, shapes=True, type="mesh", ni=True, f=True)[0]
                cmds.delete(dup, constructionHistory=True)

                # v0.1.14: ハードエッジ対策。立方体のような face-split normal メッシュは
                # 1 三角形の 3 頂点が同じ面法線を共有し、GS 内 facing が全頂点で同符号になり
                # シルエットが取れない。a=180 で全エッジを soften → 頂点法線が平均化され、
                # 頂点ごとに facing が変わるようになり GS が外周を検出できる。
                try:
                    cmds.polySoftEdge(dshape + ".e[*]", a=180, ch=False)
                except Exception:
                    pass

                # 変形追従用に textureDeformer(offset=0)。押し出しはシェーダで行うので 0 固定。
                td = cmds.textureDeformer(dshape, strength=0, offset=0.0, direction="Normal")
                defm = td[0]
                handle = None
                for c in (cmds.listConnections(defm, type="transform") or []):
                    if "textureDeformerHandle" in _short(c):
                        handle = c
                        break
                if handle is None and len(td) > 1:
                    handle = td[1]
                try:
                    cmds.connectAttr(src + ".outMesh", defm + ".input[0].inputGeometry", f=True)
                except Exception:
                    cmds.warning("変形追従の接続に失敗（静的な輪郭として生成）")
                self._tuck_handle(handle)
                try:
                    cmds.setAttr(defm + ".offset", 0.0, lock=True)
                except Exception:
                    pass

                # 曲率用カラーセット（頂点カラー R = 太さ倍率）を deformer より下流に用意。
                # ★シェーダ割り当ての「前」に作る: dx11Shader は割り当て時に頂点入力レイアウトを
                #   確定するため、後から色セットを足すと COLOR0 が取り込まれず曲率が効かない。
                _init_scrn_color_set(dshape)

                _build_screen_network(dup, dshape, self._color)

                if not cmds.attributeQuery(TAG, node=dup, exists=True):
                    cmds.addAttr(dup, ln=TAG, at="bool", dv=True)
                if not cmds.attributeQuery(SCRN_TAG, node=dup, exists=True):
                    cmds.addAttr(dup, ln=SCRN_TAG, at="bool", dv=True)
                _ensure_line_render_attrs(dup)   # v0.1.46: 見え方属性（レイヤー別上書き用）

                dup = cmds.parent(dup, grp)[0]
                ctrl = _ensure_line_anim(dup, thick)
                for at, dv in ((CTRL_THICK, DEFAULT_THICK), (CTRL_CURV, DEFAULT_CURV),
                               (CTRL_CAP, DEFAULT_CAP), (CTRL_CMIN, DEFAULT_CMIN),
                               (CTRL_TAPER, 0.0)):
                    if cmds.attributeQuery(at, node=ctrl, exists=True):
                        try:
                            cmds.setAttr(ctrl + "." + at, dv)
                        except Exception:
                            pass
                _update_scrn_curv_weights(dup)   # 初期の曲率倍率を頂点カラーへ反映
                self._apply_line_selectable(dup, self._lock_select())
                made.append(dup)
            cmds.select(clear=True)
            self._stash_loose_handles()
        finally:
            cmds.undoInfo(closeChunk=True)
        self.refresh_tree()
        if made:
            cmds.warning("スクリーン輪郭はハードウェアシェーダです。ビューポートの "
                         "「テクスチャ表示 ON（ホットキー 6）」で表示されます。")
        # オプション: 内側の折れ目/溝用にフレネル輪郭も一緒に生成
        if made and getattr(self, "chk_scrn_fresnel", None) is not None \
                and self.chk_scrn_fresnel.isChecked() and sel:
            try:
                cmds.select(sel, r=True)
            except Exception:
                pass
            self.create_fresnel_outline()
        # オプション: 硬いエッジ(折り目)にチューブ状クリース線も一緒に生成
        if made and getattr(self, "chk_scrn_crease", None) is not None \
                and self.chk_scrn_crease.isChecked() and sel:
            try:
                cmds.select(sel, r=True)
            except Exception:
                pass
            self.create_crease_lines()

    def create_crease_lines(self, *args):
        """選択メッシュのクリース（折れ目）＋ボーダー（開境界）エッジを自動検出し、
        そこにチューブ状のエッジラインを生成する（カメラ非依存・全レンダラー/バッチ対応）。
        ※ シルエット（重なり/浮き隙間）はカメラ依存のため別途。ここでは折れ目/境界のみ。"""
        sel = cmds.ls(sl=True, long=True, type="transform")
        if not sel:
            cmds.warning("メッシュを選択してください"); return
        # しきい値は UI スピンから取得（無ければ既定 CREASE_ANGLE）。
        try:
            crease_angle = float(self.spn_crease_angle.value())
        except Exception:
            crease_angle = CREASE_ANGLE
        # v0.1.28: 生成時はプロファイル適用しない（作成後にパネルで調整する運用）。
        crease_profile_str = None
        # 生成前の EDGE_TAG ライン集合を snapshot（生成後に差分で新規ラインを特定）
        def _all_edges():
            out = set()
            for t in cmds.ls(type="transform", long=True) or []:
                if cmds.attributeQuery(EDGE_TAG, node=t, exists=True):
                    out.add(t)
            return out
        before = _all_edges()
        made = 0
        skipped = 0
        cmds.undoInfo(openChunk=True)
        try:
            # v0.1.42: 複数選択時は 1 共通グループ配下にクリースをまとめる
            batch_grp = self._ensure_batch_group(sel)
            for obj in sel:
                shps = cmds.listRelatives(obj, shapes=True, type="mesh", ni=True, f=True)
                if not shps:
                    continue
                edges = _crease_border_edges(shps[0], obj, crease_angle)
                if not edges:
                    skipped += 1
                    continue
                # polyToCurve は非分岐連続パスしか受け付けないので、
                # 連結成分＋分岐で chain に分けて chain ごとにライン化する。
                indices = []
                for ec in edges:
                    try:
                        indices.append(int(ec.split("[")[-1].rstrip("]")))
                    except Exception:
                        pass
                chains = _split_edges_into_chains(shps[0], indices)
                any_made = False
                # v0.1.31→v0.1.42: 複数選択時は batch_grp（共通）配下のクリースサブグループへ
                mesh_grp = batch_grp or self._ensure_mesh_group(_base(obj))
                crease_sub = self._ensure_sub_group(mesh_grp, CREASES_GROUP_NAME)
                for chain in chains:
                    if not chain: continue
                    # v0.1.52: フルパスで選ぶ（短い名前が同じメッシュが他にあると選択に失敗し、
                    # 1 本も作られないまま何も言わずに終わっていた）
                    comps = ["{}.e[{}]".format(obj, i) for i in chain]
                    cmds.select(comps, r=True)
                    try:
                        self.create_edge_line()
                        any_made = True
                    except Exception:
                        pass
                # v0.1.31: 新規作成されたエッジラインをこのメッシュのクリースサブグループへ移動＋タグ付け
                after_edges = _all_edges()
                new_now = after_edges - before
                before = after_edges   # 次のメッシュのため update
                for ln in new_now:
                    if not cmds.attributeQuery(CREASE_TAG, node=ln, exists=True):
                        try: cmds.addAttr(ln, ln=CREASE_TAG, at="bool", dv=True)
                        except Exception: pass
                    try:
                        cur_parent = cmds.listRelatives(ln, parent=True, f=True)
                        if not cur_parent or cur_parent[0] != crease_sub:
                            cmds.parent(ln, crease_sub)
                    except Exception:
                        pass
                if any_made:
                    made += 1
        finally:
            cmds.undoInfo(closeChunk=True)
        cmds.select(clear=True)
        self.refresh_tree()
        if made:
            cmds.warning("クリース/境界エッジにラインを生成しました（{} メッシュ）。"
                         "折れ目/境界が無いメッシュは生成されません。".format(made))
        elif skipped:
            cmds.warning("クリース（二面角>{:.0f}°）も境界エッジも見つかりませんでした"
                         "（滑らかな閉曲面では折れ目が無いため線は出ません）。".format(crease_angle))

    def create_material_boundary_lines(self, *args):
        """v0.1.38: 選択メッシュで隣接 2 面の shadingEngine が異なるエッジを検出し、
        そこにチューブ状のライン（マテリアル境界）を生成。カメラ非依存・全レンダラー対応。
        単一 SG メッシュは対象外。"""
        sel = cmds.ls(sl=True, long=True, type="transform")
        if not sel:
            cmds.warning("メッシュを選択してください"); return
        def _all_edges():
            out = set()
            for t in cmds.ls(type="transform", long=True) or []:
                if cmds.attributeQuery(EDGE_TAG, node=t, exists=True):
                    out.add(t)
            return out
        before = _all_edges()
        made = 0
        skipped = 0
        cmds.undoInfo(openChunk=True)
        try:
            # v0.1.42: 複数選択時は 1 共通グループ配下にマテリアル境界をまとめる
            batch_grp = self._ensure_batch_group(sel)
            for obj in sel:
                shps = cmds.listRelatives(obj, shapes=True, type="mesh", ni=True, f=True)
                if not shps:
                    continue
                edges = _material_boundary_edges(shps[0], obj)
                if not edges:
                    skipped += 1
                    continue
                indices = []
                for ec in edges:
                    try:
                        indices.append(int(ec.split("[")[-1].rstrip("]")))
                    except Exception:
                        pass
                chains = _split_edges_into_chains(shps[0], indices)
                any_made = False
                mesh_grp = batch_grp or self._ensure_mesh_group(_base(obj))
                mb_sub = self._ensure_sub_group(mesh_grp, MATBOUND_GROUP_NAME)
                for chain in chains:
                    if not chain: continue
                    comps = ["{}.e[{}]".format(obj, i) for i in chain]   # v0.1.52: フルパス
                    cmds.select(comps, r=True)
                    try:
                        self.create_edge_line()
                        any_made = True
                    except Exception:
                        pass
                after_edges = _all_edges()
                new_now = after_edges - before
                before = after_edges
                for ln in new_now:
                    if not cmds.attributeQuery(MATBOUND_TAG, node=ln, exists=True):
                        try: cmds.addAttr(ln, ln=MATBOUND_TAG, at="bool", dv=True)
                        except Exception: pass
                    try:
                        cur_parent = cmds.listRelatives(ln, parent=True, f=True)
                        if not cur_parent or cur_parent[0] != mb_sub:
                            cmds.parent(ln, mb_sub)
                    except Exception:
                        pass
                if any_made:
                    made += 1
        finally:
            cmds.undoInfo(closeChunk=True)
        cmds.select(clear=True)
        self.refresh_tree()
        if made:
            cmds.warning("マテリアル境界にラインを生成しました（{} メッシュ）。"
                         "単一 SG のメッシュは対象外です。".format(made))
        elif skipped:
            cmds.warning("マテリアル境界が見つかりませんでした"
                         "（全選択メッシュが単一 SG、または面ごとの shadingEngine が同じ）。")

    def create_edge_line(self, *args):
        """選択ポリゴンエッジに沿ってチューブ状のラインを追加（太さ調整可・元に追従）。"""
        sel = cmds.ls(sl=True, fl=True) or []
        edges = cmds.filterExpand(sel, sm=32) or []
        if not edges:
            cmds.warning("メッシュのエッジを選択してください"); return
        # 新規エッジラインは UI の現在値ではなく初期値で生成する
        thick = DEFAULT_EDGE_THICK
        _, sg = _ensure_shader(self._color)
        # v0.1.31: エッジ所属メッシュを特定 → <mesh>_outlines/__Edges__ サブグループへ
        mesh_short = None
        try:
            # edges[0] = "pCube1.e[3]" → mesh transform name
            mesh_sh_path = edges[0].split(".")[0]
            # path may be short or full; get the mesh shape → parent transform short name
            shp = (cmds.ls(mesh_sh_path, type="transform", long=True)
                   or cmds.ls(mesh_sh_path, type="mesh", long=True) or [])
            if shp:
                node = shp[0]
                # v0.1.52: グループ名はパス・ネームスペースを外した名前で作る
                # （'charA|box' や 'chr:box' のままだと名前として作れない/名前空間に入る）
                if cmds.objectType(node) == "mesh":
                    par = cmds.listRelatives(node, parent=True, f=True) or []
                    if par: mesh_short = _base(par[0])
                else:
                    mesh_short = _base(node)
        except Exception:
            mesh_short = None
        if mesh_short:
            mesh_grp = self._ensure_mesh_group(mesh_short)
            grp = self._ensure_sub_group(mesh_grp, EDGES_GROUP_NAME)
        else:
            grp = self._current_group()

        cmds.undoInfo(openChunk=True)
        line = None
        try:
            cmds.select(edges, r=True)
            # エッジ → カーブ（履歴付き＝メッシュ変形/移動に追従）
            curve = cmds.polyToCurve(form=2, degree=1, ch=True)[0]
            # 細い円プロファイル（実太さは textureDeformer.offset で出す）
            circ = cmds.circle(radius=EDGE_BASE_RADIUS, normal=(0, 1, 0), ch=True)
            circ_x = circ[0]
            # カーブに沿って押し出し → NURBS チューブ
            surf = cmds.extrude(circ_x, curve, et=2, fixedPath=True, useComponentPivot=1,
                                useProfileNormal=True, reverseSurfaceIfPathReversed=True,
                                ch=True)[0]
            # ポリゴン化（履歴付き）
            line = cmds.nurbsToPoly(surf, ch=True, polygonType=1, format=2,
                                    uType=3, uNumber=1, vType=3, vNumber=1)[0]
            line = cmds.rename(line, "edgeLine1")
            lshape = cmds.listRelatives(line, shapes=True, type="mesh", ni=True, f=True)[0]

            # チューブの法線が内向きだと textureDeformer が内側へ押し込み、offset が基準半径を
            # 超えるとチューブが反転する。生成直後に外向きか調べ、内向きなら法線反転を履歴に積む
            # （textureDeformer の前に積むことで、deformer が外向き法線を読んで必ず膨らむ）。
            if not _tube_normals_outward(lshape, curve):
                try:
                    cmds.polyNormal(lshape, normalMode=0, ch=True)
                except Exception:
                    pass

            # hull ラインと同じく textureDeformer で太さ(offset)＋曲率起伏(weightList)を出す
            td = cmds.textureDeformer(lshape, strength=0, offset=thick, direction="Normal")
            defm = td[0]
            handle = None
            for c in (cmds.listConnections(defm, type="transform") or []):
                if "textureDeformerHandle" in _short(c):
                    handle = c
                    break
            if handle is None and len(td) > 1:
                handle = td[1]
            self._tuck_handle(handle)

            cmds.sets(lshape, e=True, forceElement=sg)
            for at in (TAG, EDGE_TAG):
                if not cmds.attributeQuery(at, node=line, exists=True):
                    cmds.addAttr(line, ln=at, at="bool", dv=True)
            # 中間ノード（カーブ/円/NURBS面）はラインの子に隠して格納（履歴は保持）
            for n in (curve, circ_x, surf):
                if n and cmds.objExists(n):
                    try:
                        cmds.setAttr(n + ".visibility", 0)
                        cmds.setAttr(n + ".hiddenInOutliner", 1)
                        cmds.parent(n, line)
                    except Exception:
                        pass
            line = cmds.parent(line, grp)[0]
            ctrl = _ensure_line_anim(line, thick)
            # 各項目を初期値で生成（太さ/曲率起伏/曲率上限/末端細り/プロファイル）
            for at, dv in ((CTRL_THICK, DEFAULT_EDGE_THICK), (CTRL_CURV, DEFAULT_CURV),
                           (CTRL_CAP, DEFAULT_CAP), (CTRL_CMIN, DEFAULT_CMIN),
                           (CTRL_TAPER, 0.0)):
                if cmds.attributeQuery(at, node=ctrl, exists=True):
                    try:
                        cmds.setAttr(ctrl + "." + at, dv)
                    except Exception:
                        pass
            if cmds.attributeQuery(CTRL_PROFILE, node=ctrl, exists=True):
                try:
                    cmds.setAttr(ctrl + "." + CTRL_PROFILE, "0:1,1:1", type="string")
                except Exception:
                    pass
            _update_curv_weights(line)
            self._apply_line_selectable(line, self._lock_select())
            # 生成したエッジラインはアウトライナーで選択状態にしない
            cmds.select(clear=True)
        finally:
            cmds.undoInfo(closeChunk=True)

        self.refresh_tree()

    # ========== リネーム ==========
    def rename_node(self, node):
        """ライン／グループのノードをリネームする。"""
        if not node or not cmds.objExists(node):
            return
        if _is_foreign(node):
            cmds.warning(u"リファレンスしたキャラの線の名前は、アセットのファイルで変えてください")
            return
        old = _short(node)
        new, ok = QtWidgets.QInputDialog.getText(self, "リネーム", "新しい名前:", text=old)
        new = (new or "").strip()
        if not ok or not new or new == old:
            return
        cmds.undoInfo(openChunk=True)
        try:
            try:
                # コントローラーはリネーム前に取得（message リンクで特定。名前依存しない）
                ctrl = _ctrl_of(node)
                newpath = cmds.rename(node, new)
                # コントローラー名もラインに合わせて変更（アウトライナーの見た目を一致させる）
                if ctrl and cmds.objExists(ctrl):
                    try:
                        old_c = _short(ctrl)
                        new_c = cmds.rename(ctrl, _short(newpath) + CTRL_SUFFIX)
                        _rename_ctrl_nodes(old_c, _short(new_c))
                    except Exception:
                        pass
            except Exception:
                cmds.warning("リネームに失敗しました: {}".format(new))
            # v0.1.53: ツリー更新で張り直す接続も同じ Undo に入れる（外にあると Ctrl+Z 1 回で戻らなかった）
            self.refresh_tree()
        finally:
            cmds.undoInfo(closeChunk=True)
        # 環境によってはスクリプトの rename がアウトライナーに即時反映されないため明示更新
        self._refresh_outliner()

    def _refresh_outliner(self):
        """アウトライナーパネルの表示を強制更新する。"""
        try:
            import maya.mel as _mel
            _mel.eval("AEdagNodeCommonRefreshOutliners();")
        except Exception:
            pass

    def _on_double_click(self, item, column):
        """ライン・グループともダブルクリックでリネーム。
        v0.1.25: 色列(2) ダブルクリックは直接カラーピッカーを開く。"""
        node = item.data(0, QtCore.Qt.UserRole)
        if not node or not cmds.objExists(node):
            return
        # 色列: 直接カラーピッカー → そのラインに個別カラー適用
        if column == 2 and cmds.attributeQuery(TAG, node=node, exists=True):
            self._pick_color_for_line(node)
            return
        if (cmds.attributeQuery(TAG, node=node, exists=True)
                or cmds.attributeQuery(GROUP_TAG, node=node, exists=True)):
            self.rename_node(node)

    def _pick_color_for_line(self, line):
        """v0.1.25: 単一ラインに個別カラーを即設定するピッカー。"""
        if not line or not cmds.objExists(line): return
        init = self._line_color(line) or (0.0, 0.0, 0.0)
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor.fromRgbF(*init), self, "ラインのカラー")
        if not c.isValid(): return
        rgb = (c.redF(), c.greenF(), c.blueF())
        cmds.undoInfo(openChunk=True)
        try:
            self._apply_line_color(line, rgb)
        finally:
            cmds.undoInfo(closeChunk=True)
        self.refresh_tree()

    def _on_tree_context_menu(self, pos):
        """v0.1.25: ツリーで右クリック → コンテキストメニュー。
        グループ行: 全ライン表示/非表示、色を揃える、ランプ適用(クリースのみ)
        ライン行:  表示/非表示、色変更、削除
        """
        item = self.tree.itemAt(pos)
        if item is None: return
        node = item.data(0, QtCore.Qt.UserRole)
        if not node or not cmds.objExists(node): return
        menu = QtWidgets.QMenu(self.tree)
        is_group = cmds.attributeQuery(GROUP_TAG, node=node, exists=True)
        is_line  = cmds.attributeQuery(TAG, node=node, exists=True)
        is_crease_group = is_group and _short(node) == CREASES_GROUP_NAME
        foreign = _is_foreign(node)     # v0.1.53: リファレンス等は名前変更・削除を出さない
        if is_group:
            a1 = menu.addAction("全ライン 表示")
            a1.triggered.connect(lambda: self._set_group_lines_vis(node, True))
            a2 = menu.addAction("全ライン 非表示")
            a2.triggered.connect(lambda: self._set_group_lines_vis(node, False))
            menu.addSeparator()
            a3 = menu.addAction("色を揃える…")
            a3.triggered.connect(lambda: self._unify_group_color(node))
            if not foreign:
                menu.addSeparator()
                a5 = menu.addAction("リネーム…")
                a5.triggered.connect(lambda: self.rename_node(node))
                a6 = menu.addAction("削除")
                a6.triggered.connect(lambda: self.delete_selected())
        elif is_line:
            # v0.1.46: ライン詳細設定（隠れ線の紐づけ・レイヤーごとの色/表現）。v0.1.50: エッジ線も
            if _is_detail_line(node):
                a0 = menu.addAction("ライン詳細設定…（隠れ線・レイヤーごとの見え方）")
                f0 = a0.font(); f0.setBold(True); a0.setFont(f0)
                a0.triggered.connect(lambda: self.open_line_options(node))
                menu.addSeparator()
            a1 = menu.addAction("表示切替")
            a1.triggered.connect(lambda: self._toggle_line_vis(node))
            a2 = menu.addAction("色を変える…")
            a2.triggered.connect(lambda: self._pick_color_for_line(node))
            # v0.1.41: SCRN ラインのみ「交差線に含める」チェック
            if cmds.attributeQuery(SCRN_TAG, node=node, exists=True):
                menu.addSeparator()
                aix = menu.addAction("交差線に含める")
                aix.setCheckable(True)
                aix.setChecked(_scrn_intersect_on(node))
                aix.setToolTip(
                    "このラインを交差線の対象に含めるかどうか。\n"
                    "OFF にすると、交差線 ON 時にこのメッシュとの交差は検出されない。\n"
                    "複数選択時は、このラインと同じ状態に全部切り替わる。")
                aix.triggered.connect(lambda chk: self._toggle_scrn_intersect(node, chk))
            menu.addSeparator()
            a_src = menu.addAction("元のメッシュを選ぶ")   # v0.1.53: ラインから元のメッシュへ
            a_src.triggered.connect(lambda: self.select_source_meshes(node))
            if not foreign:
                menu.addSeparator()
                a3 = menu.addAction("リネーム…")
                a3.triggered.connect(lambda: self.rename_node(node))
                a4 = menu.addAction("削除")
                a4.triggered.connect(lambda: self.delete_selected())
        menu.exec_(self.tree.viewport().mapToGlobal(pos))

    def open_line_options(self, line):
        """v0.1.46: ライン詳細設定を開く。ツリーで複数選択中なら選択中の対象ライン全部。"""
        lines = [n for n in self._selected_lines() if _is_detail_line(n)]
        if line not in lines:
            lines = [line]
        return show_line_options(lines, self)

    def _toggle_scrn_intersect(self, line, enabled):
        """v0.1.41: SCRN ラインの『交差線に含める』フラグを切替。
        ツリーで複数選択されていれば、同じ値を全選択 SCRN ラインへ適用する。"""
        lines = []
        for it in self.tree.selectedItems():
            n = it.data(0, QtCore.Qt.UserRole)
            if n and cmds.objExists(n) and cmds.attributeQuery(SCRN_TAG, node=n, exists=True):
                lines.append(n)
        if line not in lines:
            lines.append(line)
        cmds.undoInfo(openChunk=True)
        try:
            for ln in lines:
                _scrn_set_intersect_on(ln, enabled)
        finally:
            cmds.undoInfo(closeChunk=True)
        self._refresh_intersect_targets_if_on()
        self.refresh_tree()

    def _set_group_lines_vis(self, group, show):
        """グループ配下の全ラインの visibility を一括設定。"""
        if not group or not cmds.objExists(group): return
        cmds.undoInfo(openChunk=True)
        try:
            for ln in self._lines_in(group):
                try: cmds.setAttr(ln + ".visibility", int(bool(show)))
                except Exception: pass
        finally:
            cmds.undoInfo(closeChunk=True)
        self._refresh_intersect_targets_if_on()
        self.refresh_tree()

    def _toggle_line_vis(self, line):
        if not line or not cmds.objExists(line): return
        try:
            cur = bool(cmds.getAttr(line + ".visibility"))
        except Exception:
            cur = True
        try: cmds.setAttr(line + ".visibility", 0 if cur else 1)
        except Exception: pass
        self._refresh_intersect_targets_if_on()
        self.refresh_tree()

    def _unify_group_color(self, group):
        """グループ内の全ラインに同じ色を適用するピッカー。"""
        if not group or not cmds.objExists(group): return
        lines = self._lines_in(group)
        if not lines: return
        init = self._line_color(lines[0]) or (0.0, 0.0, 0.0)
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor.fromRgbF(*init), self, "グループの色を揃える")
        if not c.isValid(): return
        rgb = (c.redF(), c.greenF(), c.blueF())
        cmds.undoInfo(openChunk=True)
        try:
            for ln in lines:
                try: self._apply_line_color(ln, rgb)
                except Exception: pass
        finally:
            cmds.undoInfo(closeChunk=True)
        self.refresh_tree()

    # ========== ドラッグ&ドロップ移動 ==========
    def _group_of_item(self, item):
        """ツリー項目から移動先グループ（ノード）を解決する。"""
        if item is None:
            return None
        node = item.data(0, QtCore.Qt.UserRole)
        if not node or not cmds.objExists(node):
            return None
        if cmds.attributeQuery(GROUP_TAG, node=node, exists=True):
            return node
        if cmds.attributeQuery(TAG, node=node, exists=True):
            par = cmds.listRelatives(node, parent=True, f=True) or []
            if par and cmds.attributeQuery(GROUP_TAG, node=par[0], exists=True):
                return par[0]
        return None

    def _move_lines_to(self, lines, group):
        if not group or not cmds.objExists(group):
            return
        # v0.1.53: リファレンス等の線・グループは移さない（アセット側の構造）
        if _is_foreign(group) or any(_is_foreign(ln) for ln in lines):
            cmds.warning(u"リファレンスしたキャラの線は別のグループへ移せません（アセットのファイルで行ってください）")
            lines = [ln for ln in lines if not _is_foreign(ln)]
            if _is_foreign(group) or not lines:
                self.refresh_tree()
                return
        cmds.undoInfo(openChunk=True)
        try:
            for ln in lines:
                try:
                    cmds.parent(ln, group)
                except Exception:
                    pass
            # v0.1.53: コントローラーの付け替え・グループ倍率のつなぎ直し（ツリー更新）も同じ Undo に
            self.refresh_tree()
        finally:
            cmds.undoInfo(closeChunk=True)

    # ========== グループ管理 ==========
    def new_group(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "新規グループ", "グループ名:")
        name = (name or "").strip()
        if not ok or not name:
            return
        cmds.undoInfo(openChunk=True)
        try:
            self._ensure_group(name)
            self.refresh_tree()    # v0.1.53: グループのコントローラー作成も同じ Undo に
        finally:
            cmds.undoInfo(closeChunk=True)
        idx = self.group_combo.findText(name)
        if idx >= 0:
            self.group_combo.setCurrentIndex(idx)

    def reset_selected(self):
        """選択中のライン/グループの値をすべて初期値に戻す（実行前に確認ダイアログ）。"""
        nodes = self._selected_nodes()
        lines = [n for n in nodes if cmds.attributeQuery(TAG, node=n, exists=True)]
        groups = [n for n in nodes if cmds.attributeQuery(GROUP_TAG, node=n, exists=True)]
        if not lines and not groups:
            cmds.warning("リセットする項目をツリーで選択してください"); return
        # 忠告（確認ダイアログ）。Py2/Py3 両対応のため静的メソッドの warning を使う。
        res = QtWidgets.QMessageBox.warning(
            self, "選択をすべてリセット",
            "選択したライン/グループの値（太さ・曲率起伏・曲率上限・曲率下限・"
            "末端細り・プロファイル／グループ倍率）をすべて初期値に戻します。\n"
            "現在の調整値は失われます（Undo で元に戻せます）。\n\nよろしいですか？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if res != QtWidgets.QMessageBox.Yes:
            return
        cmds.undoInfo(openChunk=True)
        try:
            for line in lines:
                ctrl = _ensure_line_anim(line, DEFAULT_THICK)
                is_edge = cmds.attributeQuery(EDGE_TAG, node=line, exists=True)
                thick = DEFAULT_EDGE_THICK if is_edge else DEFAULT_THICK
                for at, dv in ((CTRL_THICK, thick), (CTRL_CURV, DEFAULT_CURV),
                               (CTRL_CAP, DEFAULT_CAP), (CTRL_CMIN, DEFAULT_CMIN),
                               (CTRL_TAPER, 0.0)):
                    if cmds.attributeQuery(at, node=ctrl, exists=True):
                        try:
                            cmds.setAttr(ctrl + "." + at, dv)
                        except Exception:
                            pass
                if cmds.attributeQuery(CTRL_PROFILE, node=ctrl, exists=True):
                    try:
                        cmds.setAttr(ctrl + "." + CTRL_PROFILE, "0:1,1:1", type="string")
                    except Exception:
                        pass
                _update_line_weights(line)
            for g in groups:
                gc = _ctrl_of(g)
                if gc and cmds.attributeQuery(GMULT, node=gc, exists=True):
                    try:
                        cmds.setAttr(gc + "." + GMULT, 1.0)
                    except Exception:
                        pass
        finally:
            cmds.undoInfo(closeChunk=True)
        # ツリーの太さ列を更新（選択は保持したまま）
        self._populating = True
        for it in self.tree.selectedItems():
            n = it.data(0, QtCore.Qt.UserRole)
            if not n:
                continue
            if cmds.attributeQuery(TAG, node=n, exists=True):
                t = self._thickness_of(n)
                it.setText(1, "" if t is None else "{:.3f}".format(t))
            elif cmds.attributeQuery(GROUP_TAG, node=n, exists=True):
                gc = _ctrl_of(n)
                gm = 1.0
                if gc:
                    try:
                        gm = cmds.getAttr(gc + "." + GMULT)
                    except Exception:
                        pass
                it.setText(1, "x{:.2f}".format(gm))
        self._populating = False
        self._on_tree_selection()   # スライダー類を初期値表示へ同期

    def create_render_layer_from_selection(self):
        """v0.1.35: 選択したライン／ライングループだけを含む Render Setup レイヤーを作成。
        元メッシュは useBackground 差替で透明化。レイヤー名は入力ダイアログで指定。"""
        nodes = self._selected_nodes()
        if not nodes:
            cmds.warning("レンダーレイヤーに入れるライン／グループをツリーで選んでください"); return
        lines = _collect_lines_from_selection(nodes)
        if not lines:
            cmds.warning("選択にラインが含まれていません（グループが空の可能性）"); return
        srcs = _source_meshes_of(lines)

        # デフォルト名: 単一グループ選択→そのグループ名、単一ラインなら ライン名、
        # 複数なら "outlines"
        tops_groups = [n for n in nodes if cmds.attributeQuery(GROUP_TAG, node=n, exists=True)]
        tops_lines  = [n for n in nodes if cmds.attributeQuery(TAG, node=n, exists=True)]
        if len(tops_groups) == 1 and not tops_lines:
            default = _short(tops_groups[0])
        elif len(tops_lines) == 1 and not tops_groups:
            default = _short(tops_lines[0])
        else:
            default = "outlines"

        name, ok = QtWidgets.QInputDialog.getText(
            self, "レンダーレイヤーを作成",
            u"レイヤー名（英数/_）:",
            QtWidgets.QLineEdit.Normal, default)
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            cmds.warning("レイヤー名が空です"); return

        cmds.undoInfo(openChunk=True)
        try:
            try:
                final_name = create_outline_render_layer(name, lines, srcs)
            except RuntimeError as e:
                cmds.warning(str(e)); return
        finally:
            cmds.undoInfo(closeChunk=True)
        try:
            cmds.inViewMessage(amg=u'レンダーレイヤー <hl>' + final_name + u'</hl> を作成（線 '
                               + str(len(lines)) + u' 本 / 元メッシュ ' + str(len(srcs)) + u' 個）',
                               pos='topCenter', fade=True)
        except Exception:
            pass

    def delete_selected(self):
        nodes = self._selected_nodes()
        if not nodes:
            cmds.warning("削除する項目をツリーで選択してください"); return
        # v0.1.53: リファレンス等の線は消せない（アセット側で消す）。見出し（ROOT）も対象外。
        skipped = [n for n in nodes if _is_foreign(n) or _root_of(n) == n or
                   (cmds.ls(n, long=True) or [""])[0] in _all_roots()]
        nodes = [n for n in nodes if n not in skipped]
        if skipped:
            cmds.warning(u"リファレンスしたキャラの線は削除できません（アセットのファイルで行ってください）")
        if not nodes:
            return
        # 削除対象ライン（選択ライン＋選択グループ配下ライン）のコントローラー・乗算ノードも巻き込む
        def _gather(line):
            c = _ctrl_of(line)
            if c:
                victims.add(c)
                # v0.1.52: _ensure_thickness_chain が作る乗算・条件ノードをすべて片付ける
                # （以前は _thkA/_thkB/_thkMaskBoost だけで、スクリーン輪郭やエッジ線の
                #   ノードが消すたびにシーンに残っていた）
                for s in _CTRL_NODE_SUFFIXES:
                    n2 = _short(c) + s
                    if cmds.objExists(n2):
                        victims.add(n2)
            # v0.1.52: スクリーン輪郭・フレネルの専用シェーダと SG も片付ける
            shd = _fresnel_shader(line)
            if shd:
                victims.add(shd)
                for sg in cmds.listConnections(shd, type="shadingEngine") or []:
                    victims.add(sg)
            # 重なりマスク本体（別ホルダー）と膨らみ乗算ノードもラインと一緒に削除する
            m = _mask_of(line)
            if m:
                victims.add(m)
                infl = _short(m) + "_inflate"
                if cmds.objExists(infl):
                    victims.add(infl)
            # 隙間埋めオブジェクトもラインと一緒に削除する
            g = _gapfill_of(line)
            if g:
                victims.add(g)
            # v0.1.50: エッジ線の専用シェーダと表示切替ノードも片付ける
            for n2 in (COL_PREFIX + _base(line) + "_SS", COL_PREFIX + _base(line) + "_SG",
                       _base(line) + "_OG_visRev",
                       _base(line) + "_OG_traceMul", _base(line) + "_OG_traceBlend"):   # v0.1.53
                if cmds.objExists(n2):
                    victims.add(n2)

        victims = set(nodes)
        for n in list(nodes):
            if cmds.attributeQuery(TAG, node=n, exists=True):
                _gather(n)
            if cmds.attributeQuery(GROUP_TAG, node=n, exists=True):
                gc = _ctrl_of(n)          # グループコントローラーも削除
                if gc:
                    victims.add(gc)
                for d in cmds.listRelatives(n, ad=True, type="transform", f=True) or []:
                    if cmds.attributeQuery(TAG, node=d, exists=True):
                        _gather(d)
        cmds.undoInfo(openChunk=True)
        try:
            cmds.delete(list(victims))
            # 孤立したハンドル／コントローラー／空ホルダーを掃除
            self._cleanup_orphan_handles()
            self._cleanup_orphan_ctrls()
            if cmds.objExists(LINE_HOLDER) and not (cmds.listRelatives(LINE_HOLDER, c=True) or []):
                cmds.delete(LINE_HOLDER)
            if cmds.objExists(MASK_HOLDER) and not (cmds.listRelatives(MASK_HOLDER, c=True) or []):
                cmds.delete(MASK_HOLDER)
            if cmds.objExists(GAPFILL_HOLDER) and not (cmds.listRelatives(GAPFILL_HOLDER, c=True) or []):
                cmds.delete(GAPFILL_HOLDER)
            # 空になった ROOT は片付ける
            if cmds.objExists(ROOT) and not (cmds.listRelatives(ROOT, c=True) or []):
                cmds.delete(ROOT)
        finally:
            cmds.undoInfo(closeChunk=True)
        self.refresh_tree()
        self._ensure_cam_tracking()   # 隙間埋めが無くなればカメラ追従を止める


# --------------------------------------------------------------------------- #
# v0.1.46 ライン詳細設定ウィンドウ（右クリック →「ライン詳細設定…」）
#   ・隠れ線を透かす物の紐づけ（全レイヤー共通）
#   ・レイヤーごとの見え方（線を描く/線の色/隠れ線の表現・色・濃さ・破線）
#     マスター＝ふだんのビューポート。各レンダーレイヤーは Render Setup の上書き。
#   ・「隠れ線だけ」を書き出すレイヤーの作成
# --------------------------------------------------------------------------- #
_LINEOPT_OBJ = "OG_Toonline_LineOptions"
_LINEOPT_CSS = _DIALOG_BG_CSS.replace("OG_Toonline_ManagerWin", _LINEOPT_OBJ) + (
    "QLabel#OG_Toonline_LineOptSection { color: #c9a848; font-weight: bold;"
    " padding-top: 6px; }"
    "QLabel#OG_Toonline_LineOptNote { color: #9a9a9a; }"
)
_line_options_win = [None]


# ---- v0.1.53: 線の設定の書き出し / 読み込み ----
_EXPORT_CTRL_ATTRS = (CTRL_THICK, CTRL_CURV, CTRL_CAP, CTRL_CMIN, CTRL_TAPER)


def _export_look_attrs():
    return ([LN_SHOW_VIS, LN_HID_MODE, LN_HID_SAME, LN_HID_COLOR, LN_HID_ALPHA, LN_HID_DASH,
             LN_HID_FILL, LN_TRACE, LN_TRACE_DARK] + [b[0] for b in BRUSH_ATTRS])


def _scene_lines():
    """シーン内の全ライン（TAG 付き transform のフルパス。リファレンスも含む）。"""
    return [t for t in cmds.ls(type="transform", long=True) or []
            if cmds.attributeQuery(TAG, node=t, exists=True)]


def _line_kind(line):
    for tag, kind in ((SCRN_TAG, "screen"), (FRES_TAG, "fresnel"), (CREASE_TAG, "crease"),
                      (MATBOUND_TAG, "matbound"), (EDGE_TAG, "edge")):
        if _attr_exists(line, tag):
            return kind
    return "hull"


def _bare(name):
    """パスと名前空間を外した名前（'|chr:grp|chr:head' → 'head'）。"""
    return _short(name).split(":")[-1] if name else ""


def _jsonable(v):
    if isinstance(v, (list, tuple)):
        if v and isinstance(v[0], (list, tuple)):
            v = v[0]
        return [float(x) for x in v]
    return v


def _plug_settable(plug):
    try:
        return bool(cmds.getAttr(plug, settable=True))
    except Exception:
        return False


def _set_plug(plug, v):
    """JSON から戻した値を入れる（色は float3、文字列は string）。設定できなければ False。"""
    if not _plug_settable(plug):
        return False
    try:
        if isinstance(v, (list, tuple)):
            cmds.setAttr(plug, float(v[0]), float(v[1]), float(v[2]), type="float3")
        elif isinstance(v, str) or (not isinstance(v, (bool, int, float)) and v is not None):
            cmds.setAttr(plug, v, type="string")
        else:
            cmds.setAttr(plug, v)
        return True
    except Exception:
        return False


def _line_record(ui, line):
    """1 本の線の設定（マスターの値＋レイヤーごとの上書き）を dict にする。"""
    src = _line_source_mesh(line)
    rec = {"name": _bare(line), "kind": _line_kind(line), "src": _bare(src)}
    ctrl = _ctrl_of(line)
    c = {}
    if ctrl:
        for at in _EXPORT_CTRL_ATTRS:
            if _attr_exists(ctrl, at):
                try:
                    c[at] = float(cmds.getAttr(ctrl + "." + at))
                except Exception:
                    pass
        if _attr_exists(ctrl, CTRL_PROFILE):
            try:
                c[CTRL_PROFILE] = cmds.getAttr(ctrl + "." + CTRL_PROFILE) or "0:1,1:1"
            except Exception:
                pass
    rec["ctrl"] = c
    g = _line_group(line)
    gc = _ctrl_of(g) if g else None
    rec["group"] = _bare(g)
    gl = _global_ctrl_for(line) or _find_global_ctrl()
    for key, node in (("group_mult", gc), ("global_mult", gl)):
        if node and _attr_exists(node, GMULT):
            try:
                rec[key] = float(cmds.getAttr(node + "." + GMULT))
            except Exception:
                pass
    try:
        col = ui._line_color(line) if ui is not None else None
        if col:
            rec["color"] = [float(x) for x in col]
    except Exception:
        pass
    try:
        rec["visible"] = bool(cmds.getAttr(line + ".visibility"))
    except Exception:
        pass
    look = {}
    for at in _export_look_attrs():
        if _attr_exists(line, at):
            try:
                look[at] = _jsonable(cmds.getAttr(line + "." + at))
            except Exception:
                pass
    rec["look"] = look
    rec["occluders"] = [_bare(o) for o in _line_occluders(line)]
    if _attr_exists(line, SCRN_TAG):
        rec["intersect"] = bool(_scrn_intersect_on(line))
    layers = {}
    for lname in _rs_layer_names():
        L = _rs_get_layer(lname)
        cl = _rs_line_collection(L, line) if L is not None else None
        if cl is None:
            continue
        vals = {}
        for ov in cl.getOverrides():
            try:
                vals[ov.attributeName()] = _jsonable(ov.getAttrValue())
            except Exception:
                pass
        if vals:
            layers[lname] = vals
    rec["layers"] = layers
    return rec


def export_line_settings(path, lines, ui=None):
    """lines の設定を JSON に書き出す。書き出した本数を返す。値はマスターレイヤーの値で書く。"""
    import json
    prev = _rs_visible_layer_name()
    if prev:
        _rs_switch("")
    try:
        recs = [_line_record(ui, ln) for ln in lines if cmds.objExists(ln)]
    finally:
        if prev:
            _rs_switch(prev)
    import io
    data = {"tool": _PACKAGE, "version": __version__, "lines": recs}
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if not isinstance(text, type(u"")):
        text = text.decode("utf-8")      # Python 2
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return len(recs)


def _natural_key(s):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s or "")]


def _match_line_records(recs, targets):
    """書き出した線と今のシーンの線を対応付ける。(元メッシュの名前, 種類, 線の名前) が同じものを先に、
    残りは (元メッシュの名前, 種類) ごとの並び順で。名前空間は見ない。"""
    info = dict((t, (_bare(_line_source_mesh(t)), _line_kind(t), _bare(t))) for t in targets)
    by_name = {}
    t_groups = {}
    for t in targets:
        s, k, n = info[t]
        by_name.setdefault((s, k, n), t)
        t_groups.setdefault((s, k), []).append(t)
    for v in t_groups.values():
        v.sort(key=lambda t: _natural_key(info[t][2]))
    pairs = []
    used = set()
    rest = []
    for r in recs:
        t = by_name.get((r.get("src", ""), r.get("kind", ""), r.get("name", "")))
        if t and t not in used:
            pairs.append((r, t))
            used.add(t)
        else:
            rest.append(r)
    r_groups = {}
    for r in recs:
        r_groups.setdefault((r.get("src", ""), r.get("kind", "")), []).append(r)
    for v in r_groups.values():
        v.sort(key=lambda r: _natural_key(r.get("name", "")))
    missing = []
    for r in rest:
        key = (r.get("src", ""), r.get("kind", ""))
        idx = r_groups.get(key, []).index(r)
        cands = t_groups.get(key, [])
        t = cands[idx] if idx < len(cands) else None
        if t is not None and t not in used:
            pairs.append((r, t))
            used.add(t)
        else:
            missing.append(r)
    return pairs, missing


def _find_occluder(base, line):
    """書き出した紐づけ先の名前から、今のシーンの物を探す（線の元メッシュと同じ名前空間を優先）。"""
    src = _line_source_mesh(line)
    ns = _short(src).rpartition(":")[0] if src else ""
    cands = []
    if ns:
        cands = cmds.ls(ns + ":" + base, long=True, type="transform") or []
    if not cands:
        cands = cmds.ls(base, long=True, type="transform") or []
    if not cands:
        cands = cmds.ls("*:" + base, long=True, type="transform", recursive=True) or []
    return cands[0] if len(cands) == 1 else (cands[0] if cands and ns else None)


def import_line_settings(path, targets, ui=None):
    """書き出した設定を targets（今のシーンの線）に当てる。(当てた本数, 見つからなかった線の名前) を返す。
    キーを打った・つないだ値は変えない（その値は飛ばす）。Undo 1 回で戻る。"""
    import io
    import json
    with io.open(path, encoding="utf-8") as f:
        data = json.loads(f.read())
    recs = data.get("lines", [])
    pairs, missing = _match_line_records(recs, [t for t in targets if cmds.objExists(t)])
    prev = _rs_visible_layer_name()
    if prev:
        _rs_switch("")
    layers_now = set(_rs_layer_names())
    cmds.undoInfo(openChunk=True)
    try:
        done = set()
        for rec, line in pairs:
            ctrl = _ctrl_of(line)
            if ctrl:
                for at, v in rec.get("ctrl", {}).items():
                    if _attr_exists(ctrl, at):
                        _set_plug(ctrl + "." + at, v)
            g = _line_group(line)
            gc = _ctrl_of(g) if g else None
            if gc and "group_mult" in rec and gc not in done and _attr_exists(gc, GMULT):
                _set_plug(gc + "." + GMULT, rec["group_mult"])
                done.add(gc)
            gl = _global_ctrl_for(line) or _find_global_ctrl()
            if gl and "global_mult" in rec and gl not in done and _attr_exists(gl, GMULT):
                _set_plug(gl + "." + GMULT, rec["global_mult"])
                done.add(gl)
            if "visible" in rec:
                _set_plug(line + ".visibility", bool(rec["visible"]))
            look = rec.get("look", {})
            if look and _is_detail_line(line):
                _ensure_line_render_attrs(line)
            if "color" in rec and ui is not None:
                try:
                    cur = ui._line_color(line) or [0.0, 0.0, 0.0]
                    if max(abs(float(a) - float(b)) for a, b in zip(cur, rec["color"])) > 1e-4:
                        ui._apply_line_color(line, rec["color"])
                except Exception:
                    pass
            for at, v in look.items():
                if _attr_exists(line, at):
                    _set_plug(line + "." + at, v)
            if float(look.get(LN_TRACE, 0.0) or 0.0) > 0.0:
                _trace_prepare(line)
            occ = [o for o in (_find_occluder(b, line) for b in rec.get("occluders", [])) if o]
            if occ:
                _add_line_occluders(line, occ)
            if "intersect" in rec and _attr_exists(line, SCRN_TAG):
                try:
                    _scrn_set_intersect_on(line, bool(rec["intersect"]))
                except Exception:
                    pass
            for lname, vals in rec.get("layers", {}).items():
                if lname not in layers_now:
                    continue
                for at, v in vals.items():
                    if _attr_exists(line, at):
                        try:
                            _rs_set_override(lname, line, at, tuple(v) if isinstance(v, list) else v)
                        except Exception:
                            pass
    finally:
        cmds.undoInfo(closeChunk=True)
        if prev:
            _rs_switch(prev)
    _hidden_refresh_cache()
    _og_override_sync()
    return len(pairs), [r.get("name", "") for r in missing]


def _line_set_attr(line, attr, value):
    """ラインの見え方属性（マスター値）を設定する。"""
    plug = line + "." + attr
    if isinstance(value, (list, tuple)):
        cmds.setAttr(plug, value[0], value[1], value[2], type="float3")
    else:
        cmds.setAttr(plug, value)


def _rgb_style(rgb):
    r, g, b = [int(max(0.0, min(1.0, float(c))) * 255) for c in rgb]
    return "background-color: rgb({0},{1},{2}); border: 1px solid #222; min-height: 18px;".format(r, g, b)


class _LineOptionsDialog(QtWidgets.QDialog):
    """ライン詳細設定。複数ライン指定時は、表示は先頭のラインの値・変更は全ラインへ。"""

    def __init__(self, lines, owner=None):
        parent = owner if owner is not None else _maya_main()
        super(_LineOptionsDialog, self).__init__(parent)
        self.setObjectName(_LINEOPT_OBJ)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.Tool)
        self.setStyleSheet(_LINEOPT_CSS)
        self._owner = owner
        # v0.1.53: ラインはパスではなく実体（MObjectHandle）で持ち、使うたびに今のパスを引く
        # （開いたまま名前を変えたりグループを移したりすると、古いパスで失敗していた）
        self._line_handles = [h for h in (_mobj_handle(ln) for ln in lines if cmds.objExists(ln)) if h]
        self._updating = False
        self._rows = {}           # attr -> (QLabel, 戻すボタン, 見出し)
        # SCRN は属性を揃えておく（通常は作成時に済んでいる）。エッジ線は開いただけで専用シェーダへ
        # 切り替えないよう、値を変えたとき（_apply_many / 紐づけ）に初めて用意する。
        for ln in self._lines:
            if _attr_exists(ln, SCRN_TAG):
                try:
                    _ensure_line_render_attrs(ln)
                except Exception:
                    pass
        if len(self._lines) > 1:
            self.setWindowTitle("ライン詳細設定 — {0} ほか {1} 本".format(
                _short(self._lines[0]), len(self._lines) - 1))
        else:
            self.setWindowTitle("ライン詳細設定 — {0}".format(
                _short(self._lines[0]) if self._lines else ""))
        self.setMinimumWidth(380)
        self._build()
        self._no_wheel = _NoWheelFilter(self)
        for w in self.findChildren(QtWidgets.QAbstractSpinBox):
            w.installEventFilter(self._no_wheel)
            w.setFocusPolicy(QtCore.Qt.StrongFocus)
        for w in self.findChildren(QtWidgets.QSlider):
            w.installEventFilter(self._no_wheel)
        for w in self.findChildren(QtWidgets.QComboBox):
            w.installEventFilter(self._no_wheel)
            w.setFocusPolicy(QtCore.Qt.StrongFocus)
        self._reload_layers()
        self._refresh()

    @property
    def _lines(self):
        out = []
        for h in self._line_handles:
            try:
                if h.isValid():
                    out.append(om2.MDagPath.getAPathTo(h.object()).fullPathName())
            except Exception:
                pass
        return out

    # ---------- 構築 ----------
    def _section(self, lay, text):
        lb = QtWidgets.QLabel(text)
        lb.setObjectName("OG_Toonline_LineOptSection")
        lay.addWidget(lb)

    def _note(self, lay, text):
        lb = QtWidgets.QLabel(text)
        lb.setObjectName("OG_Toonline_LineOptNote")
        lb.setWordWrap(True)
        lay.addWidget(lb)
        return lb

    def _row(self, grid, r, attr, title, widget, tip):
        lb = QtWidgets.QLabel(title)
        lb.setToolTip(tip)
        widget.setToolTip(tip)
        rb = QtWidgets.QPushButton(u"↺")
        rb.setFixedWidth(26)
        rb.setToolTip("このレイヤーでの変更をやめて、マスターと同じ値に戻す")
        rb.clicked.connect(lambda: self._reset(attr))
        grid.addWidget(lb, r, 0)
        grid.addWidget(widget, r, 1)
        grid.addWidget(rb, r, 2)
        self._rows[attr] = (lb, rb, title)

    def _pct(self, lo, hi, attr):
        """0〜100 のスライダー＋数値欄。ドラッグ中は値だけ追従し、離したときに反映する。"""
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0)
        sl = QtWidgets.QSlider(QtCore.Qt.Horizontal); sl.setRange(lo, hi)
        sp = QtWidgets.QSpinBox(); sp.setRange(lo, hi); sp.setFixedWidth(56)
        h.addWidget(sl, 1); h.addWidget(sp)

        def _from_slider(v):
            sp.blockSignals(True); sp.setValue(v); sp.blockSignals(False)
            if not sl.isSliderDown():
                self._apply(attr, v / 100.0)
        sl.valueChanged.connect(_from_slider)
        sl.sliderReleased.connect(lambda: self._apply(attr, sl.value() / 100.0))

        def _from_spin(v):
            sl.blockSignals(True); sl.setValue(v); sl.blockSignals(False)
            self._apply(attr, v / 100.0)
        sp.valueChanged.connect(_from_spin)
        return w, sl, sp

    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(4)

        # ---- 隠れ線を透かす物 ----
        self._section(lay, "隠れ線を透かす物（全レイヤー共通）")
        self._note(lay, "ここに入れた物の裏に回った所だけ、隠れ線として透けて見えます"
                        "（例: 眉 → 前髪）。入れていない物（頭など）の裏では出ません。")
        self.lst_occ = QtWidgets.QListWidget()
        self.lst_occ.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.lst_occ.setFixedHeight(84)
        lay.addWidget(self.lst_occ)
        orow = QtWidgets.QHBoxLayout()
        b_add = QtWidgets.QPushButton("ビューポートで選んだ物を追加")
        b_add.setToolTip("前髪など、透かしたい物をビューポートで選んでから押します。")
        b_add.clicked.connect(self._add_occ)
        b_rem = QtWidgets.QPushButton("一覧で選んだ物を外す")
        b_rem.clicked.connect(self._remove_occ)
        orow.addWidget(b_add); orow.addWidget(b_rem)
        lay.addLayout(orow)
        self.lbl_occ_state = self._note(lay, "")

        # ---- レイヤーごとの見え方 ----
        self._section(lay, "レイヤーごとの見え方")
        lrow = QtWidgets.QHBoxLayout()
        lrow.addWidget(QtWidgets.QLabel("レイヤー"))
        self.cmb_layer = QtWidgets.QComboBox()
        self.cmb_layer.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_layer.setMinimumContentsLength(10)
        self.cmb_layer.setToolTip(
            "どのレイヤーの見え方を設定するか。選ぶとビューポートもそのレイヤーに切り替わります。\n"
            "マスター＝ふだんのビューポート。プレイブラスト用の色などは各レイヤーで変えます。")
        self.cmb_layer.currentIndexChanged.connect(self._on_layer_changed)
        lrow.addWidget(self.cmb_layer, 1)
        b_rl = QtWidgets.QPushButton(u"↻")
        b_rl.setFixedWidth(26)
        b_rl.setToolTip("レイヤー一覧を読み直す")
        b_rl.clicked.connect(lambda: (self._reload_layers(), self._refresh()))
        lrow.addWidget(b_rl)
        lay.addLayout(lrow)
        self._note(lay, u"● が付いた項目は、このレイヤーだけ変えています（↺ でマスターと同じに戻す）。")

        # v0.1.49: 『見え方』と『ブラシ』をタブに分ける（縦に伸びすぎないように）
        self.tabs = QtWidgets.QTabWidget()
        page_look = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(page_look)
        grid.setContentsMargins(6, 6, 6, 6)
        grid.setColumnStretch(1, 1)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(4)

        self.chk_vis = QtWidgets.QCheckBox("描く")
        self.chk_vis.toggled.connect(lambda v: self._apply(LN_SHOW_VIS, bool(v)))
        self._row(grid, 0, LN_SHOW_VIS, "線（表側）", self.chk_vis,
                  "見えている部分の線を描くか。\nOFF にすると、隠れ線だけを書き出すレイヤーが作れます。")

        self.btn_col = QtWidgets.QPushButton()
        self.btn_col.clicked.connect(lambda: self._pick(LN_COLOR, "線の色"))
        self._row(grid, 1, LN_COLOR, "線の色", self.btn_col,
                  "このレイヤーでの線の色。\n例: マスターは黒、プレイブラスト用レイヤーでは緑。")

        # v0.1.47: 破線/実線がひと目で選べるよう、プルダウンではなくラジオボタンを横並びに
        w_hid = QtWidgets.QWidget()
        hh = QtWidgets.QHBoxLayout(w_hid); hh.setContentsMargins(0, 0, 0, 0)
        self.rb_hid = []
        grp_hid = QtWidgets.QButtonGroup(w_hid)
        for i, text in enumerate(("出さない", "破線", "実線")):
            rb = QtWidgets.QRadioButton(text)
            grp_hid.addButton(rb, i)
            rb.clicked.connect(lambda checked=False, v=i: self._apply(LN_HID_MODE, v))
            hh.addWidget(rb)
            self.rb_hid.append(rb)
        hh.addStretch(1)
        self._row(grid, 2, LN_HID_MODE, "隠れ線", w_hid,
                  "紐づけた物の裏に回った部分の描き方。\n"
                  "出さない / 破線 / 実線（色や濃さは下で変えられます）。")

        hc = QtWidgets.QWidget()
        hcl = QtWidgets.QHBoxLayout(hc); hcl.setContentsMargins(0, 0, 0, 0)
        self.chk_same = QtWidgets.QCheckBox("線と同じ")
        self.chk_same.toggled.connect(lambda v: self._apply(LN_HID_SAME, bool(v)))
        self.btn_hcol = QtWidgets.QPushButton()
        self.btn_hcol.clicked.connect(lambda: self._pick(LN_HID_COLOR, "隠れ線の色"))
        hcl.addWidget(self.chk_same); hcl.addWidget(self.btn_hcol, 1)
        self._row(grid, 3, LN_HID_COLOR, "隠れ線の色", hc,
                  "隠れ線の色。『線と同じ』を外すと別の色にできます。")
        # 「線と同じ」も同じ行の ↺ で戻す
        self._rows[LN_HID_SAME] = self._rows[LN_HID_COLOR]

        w_a, self.sl_alpha, self.sp_alpha = self._pct(0, 100, LN_HID_ALPHA)
        self._row(grid, 4, LN_HID_ALPHA, "隠れ線の濃さ", w_a,
                  "0 = 見えない / 50 = 半分透けた線 / 100 = 普通の線と同じ濃さ")

        self.sp_dash = QtWidgets.QDoubleSpinBox()
        self.sp_dash.setRange(1.0, 200.0); self.sp_dash.setDecimals(1)
        self.sp_dash.setSingleStep(1.0); self.sp_dash.setSuffix(" px")
        self.sp_dash.valueChanged.connect(lambda v: self._apply(LN_HID_DASH, float(v)))
        self._row(grid, 5, LN_HID_DASH, "破線の長さ", self.sp_dash,
                  "破線 1 区切り（線＋すき間）の長さ。画面のピクセル数。\n小さいほど細かい破線。")

        w_f, self.sl_fill, self.sp_fill = self._pct(5, 95, LN_HID_FILL)
        self._row(grid, 6, LN_HID_FILL, "破線の線の割合", w_f,
                  "1 区切りのうち線を描く割合。\n5 = ほぼ点 / 50 = 線とすき間が半々 / 95 = ほぼつながった線")
        # v0.1.53: 色トレス（線の色を下の面のマテリアルの色から作る）
        w_t, self.sl_trace, self.sp_trace = self._pct(0, 100, LN_TRACE)
        self._row(grid, 7, LN_TRACE, "色トレス", w_t,
                  "線の色を、下の面のマテリアルの色から作る割合。\n"
                  "0 = 線の色のまま / 50 = 半々 / 100 = 面の色から作った色\n"
                  "（肌の上は赤茶、髪の上は濃い髪色、のようになります）")
        w_d, self.sl_tdark, self.sp_tdark = self._pct(0, 100, LN_TRACE_DARK)
        self._row(grid, 8, LN_TRACE_DARK, "トレスの明るさ", w_d,
                  "面の色をどれくらいの明るさで使うか。\n20 = かなり暗い / 45 = 標準 / 100 = 面の色そのまま")
        b_tr = QtWidgets.QPushButton("面の色を読み直す")
        b_tr.setToolTip("マテリアルの色や割り当てを変えたあとに押すと、色トレスの色を読み直します。")
        b_tr.clicked.connect(self._reload_trace)
        grid.addWidget(b_tr, 9, 1)
        grid.setRowStretch(10, 1)
        self.tabs.addTab(page_look, "見え方")

        # ---- ブラシ（v0.1.49）----
        page_br = QtWidgets.QWidget()
        bgrid = QtWidgets.QGridLayout(page_br)
        bgrid.setContentsMargins(6, 6, 6, 6)
        bgrid.setColumnStretch(1, 1)
        bgrid.setHorizontalSpacing(6)
        bgrid.setVerticalSpacing(4)
        prow = QtWidgets.QHBoxLayout()
        prow.addWidget(QtWidgets.QLabel("プリセット"))
        self.cmb_preset = QtWidgets.QComboBox()
        self.cmb_preset.addItem("選ぶと下の項目をまとめて変更…")
        for pname, _d in BRUSH_PRESETS:
            self.cmb_preset.addItem(pname)
        self.cmb_preset.setToolTip(
            "線の質感をまとめて切り替えます（このレイヤーの値として入ります）。\n"
            "ペン=くっきり / 筆=入り抜き強め / 鉛筆=かすれ+カサつき / マーカー=かすれ /\n"
            "スケッチ=よれ+二重線 / ブラシ効果なし=全部 0。選んだ後に下で微調整できます。")
        self.cmb_preset.activated.connect(self._on_preset)
        prow.addWidget(self.cmb_preset, 1)
        bgrid.addLayout(prow, 0, 0, 1, 3)
        self._br_widgets = {}
        brush_rows = (
            (LN_BR_TAPER, "入り抜き",
             "線の端（輪郭が途切れる所）ほど細くする。\n0 = 端まで同じ太さ / 50 = ほどよく細る / 100 = 端で消えるほど細る"),
            (LN_BR_DRY, "かすれ",
             "線の中がところどころ途切れる（ドライブラシ）。縁ほど欠けやすい。\n0 = かすれない / 50 = ほどよく / 100 = 大きく途切れる"),
            (LN_BR_WOBBLE, "よれ",
             "線全体が外側へなめらかにずれる（手描きのよれ）。\n0 = ずれない / 50 = 太さの半分強 / 100 = 太さの 1.5 倍までずれる"),
            (LN_BR_DOUBLE, "二重線",
             "少し外側に細い 2 本目を引く（スケッチ風）。\n0 = 1 本 / 50 = 少し離れた 2 本目 / 100 = 大きく離れた 2 本目"),
            (LN_BR_ROUGH, "カサつき",
             "線の縁がランダムに欠ける（鉛筆の毛羽立ち）。\n0 = きれい / 50 = ほどよく / 100 = 強くカサつく"),
            (LN_BR_WAVE, "太さのゆらぎ",
             "線の太さが長さ方向に揺れる（筆圧のゆらぎ）。\n0 = 均一 / 50 = ほどよく / 100 = 大きく揺れる"),
        )
        r = 1
        for attr, title, tip in brush_rows:
            w, sl, sp = self._pct(0, 100, attr)
            self._row(bgrid, r, attr, title, w, tip)
            self._br_widgets[attr] = (sl, sp)
            r += 1
        self.sp_bscale = QtWidgets.QSpinBox()
        self.sp_bscale.setRange(1, 100)
        self.sp_bscale.valueChanged.connect(lambda v: self._apply(LN_BR_SCALE, float(v)))
        self._row(bgrid, r, LN_BR_SCALE, "ゆらぎの細かさ", self.sp_bscale,
                  "かすれ・よれ・ゆらぎ・カサつきの模様の細かさ。\n1 = 大きなうねり / 20 = 標準 / 100 = 細かくザラザラ")
        bgrid.setRowStretch(r + 1, 1)
        self.lbl_brush_note = QtWidgets.QLabel("")
        self.lbl_brush_note.setObjectName("OG_Toonline_LineOptNote")
        self.lbl_brush_note.setWordWrap(True)
        bgrid.addWidget(self.lbl_brush_note, r + 2, 0, 1, 3)
        self.tabs.addTab(page_br, "ブラシ")
        lay.addWidget(self.tabs)

        # ---- 書き出し用 ----
        self._section(lay, "書き出し用レイヤー")
        b_hl = QtWidgets.QPushButton("このラインの隠れ線だけのレイヤーを作る")
        b_hl.setToolTip(
            "隠れ線だけが写るレンダーレイヤーを作ります（表側の線は描かない）。\n"
            "元のメッシュと紐づけた物は、背景色で深度だけ書くので見えません。\n"
            "別素材として書き出して合成するときに使います。")
        b_hl.clicked.connect(self._make_hidden_layer)
        lay.addWidget(b_hl)
        self.lbl_status = self._note(lay, "")

        brow = QtWidgets.QHBoxLayout()
        brow.addStretch(1)
        b_close = QtWidgets.QPushButton("閉じる")
        b_close.clicked.connect(self.close)
        brow.addWidget(b_close)
        lay.addLayout(brow)

    # ---------- レイヤー ----------
    def _layer(self):
        d = self.cmb_layer.itemData(self.cmb_layer.currentIndex())
        return d or ""

    def _reload_layers(self):
        cur = _rs_visible_layer_name()
        self.cmb_layer.blockSignals(True)
        self.cmb_layer.clear()
        self.cmb_layer.addItem("マスター（ふだんのビューポート）", "")
        for n in _rs_layer_names():
            self.cmb_layer.addItem(n, n)
        idx = self.cmb_layer.findData(cur)
        self.cmb_layer.setCurrentIndex(idx if idx >= 0 else 0)
        self.cmb_layer.blockSignals(False)

    def _on_layer_changed(self, idx):
        if self._updating:
            return
        _rs_switch(self._layer())
        self._refresh()

    # ---------- 値の反映 ----------
    def _apply(self, attr, value):
        self._apply_many([(attr, value)])

    def _on_preset(self, idx):
        """プリセットを選んだら、ブラシの全項目をまとめて入れる（Ctrl+Z 1 回で戻る）。"""
        if idx <= 0:
            return
        name = self.cmb_preset.itemText(idx)
        self.cmb_preset.blockSignals(True)
        self.cmb_preset.setCurrentIndex(0)
        self.cmb_preset.blockSignals(False)
        self._apply_many(_brush_preset_values(name), only_scrn=True)
        self.lbl_status.setText(u"ブラシ「{0}」を入れました。".format(name))

    def _apply_many(self, pairs, only_scrn=False):
        """[(属性, 値), ...] を、選択中のレイヤー（マスターなら属性値）へまとめて入れる。"""
        if self._updating or not self._lines:
            return
        layer = self._layer()
        if layer != _rs_visible_layer_name():
            _rs_switch(layer)   # 表示中のレイヤーと設定先を必ず揃える
        uses_trace = any(a in (LN_TRACE, LN_TRACE_DARK) for a, _v in pairs)
        cmds.undoInfo(openChunk=True)
        try:
            for ln in self._lines:
                if only_scrn and not _attr_exists(ln, SCRN_TAG):
                    continue
                _ensure_line_render_attrs(ln)
                if uses_trace:
                    _trace_prepare(ln)     # v0.1.53: 面の色を書く／チューブは DG を組む（初回だけ）
                for attr, value in pairs:
                    if not _attr_exists(ln, attr):
                        continue
                    if layer:
                        _rs_set_override(layer, ln, attr, value)
                    else:
                        _line_set_attr(ln, attr, value)
        except Exception as exc:
            cmds.warning("ライン詳細設定の変更に失敗: {0}".format(exc))
        finally:
            cmds.undoInfo(closeChunk=True)
        try:
            cmds.refresh()
        except Exception:
            pass
        self._refresh()
        self._owner_refresh_colors()

    def _pick(self, attr, title):
        if not self._lines:
            return
        try:
            cur = cmds.getAttr(self._lines[0] + "." + attr)[0]
        except Exception:
            cur = (0.0, 0.0, 0.0)
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor.fromRgbF(cur[0], cur[1], cur[2]), self, title)
        if c.isValid():
            self._apply(attr, (c.redF(), c.greenF(), c.blueF()))

    def _reset(self, attr):
        layer = self._layer()
        if not layer:
            return
        attrs = [attr]
        if attr in (LN_HID_COLOR, LN_HID_SAME):
            attrs = [LN_HID_COLOR, LN_HID_SAME]
        cmds.undoInfo(openChunk=True)
        try:
            for ln in self._lines:
                for a in attrs:
                    _rs_remove_override(layer, ln, a)
        finally:
            cmds.undoInfo(closeChunk=True)
        _rs_switch(layer)   # 上書きを外した値を取り直す
        self._refresh()
        self._owner_refresh_colors()

    def _owner_refresh_colors(self):
        try:
            if self._owner is not None:
                self._owner.refresh_tree()
        except Exception:
            pass

    def _reload_trace(self):
        """v0.1.53: マテリアルの色・割り当てを変えたあとに、色トレスの面の色を読み直す。"""
        cmds.undoInfo(openChunk=True)
        try:
            for ln in self._lines:
                _ensure_line_render_attrs(ln)
                _trace_prepare(ln, recompute=True)
        finally:
            cmds.undoInfo(closeChunk=True)
        try:
            cmds.refresh()
        except Exception:
            pass
        self.lbl_status.setText(u"面の色を読み直しました。")

    # ---------- 紐づけ ----------
    def _add_occ(self):
        cands = _occluder_candidates(cmds.ls(sl=True, long=True) or [])
        if not cands:
            cmds.warning("透かしたい物（前髪など）をビューポートで選んでから押してください"
                         "（ラインそのものは選べません）")
            return
        cmds.undoInfo(openChunk=True)
        n = 0
        try:
            for ln in self._lines:
                n += _add_line_occluders(ln, cands)
        finally:
            cmds.undoInfo(closeChunk=True)
        _hidden_refresh_cache()
        _og_override_sync()
        self._refresh()
        self._owner_refresh_colors()

    def _remove_occ(self):
        names = [it.data(QtCore.Qt.UserRole) for it in self.lst_occ.selectedItems()]
        if not names:
            cmds.warning("外す物を一覧で選んでください")
            return
        cmds.undoInfo(openChunk=True)
        try:
            for ln in self._lines:
                _remove_line_occluders(ln, names)
        finally:
            cmds.undoInfo(closeChunk=True)
        _hidden_refresh_cache()
        _og_override_sync()
        self._refresh()
        self._owner_refresh_colors()

    # ---------- 書き出し用レイヤー ----------
    def _make_hidden_layer(self):
        if not any(_line_occluders(ln) for ln in self._lines):
            cmds.warning("先に『隠れ線を透かす物』を紐づけてください（紐づけが無いと隠れ線は出ません）")
            return
        default = _sanitize_layer_name(_short(self._lines[0]) + "_hidden")
        name, ok = QtWidgets.QInputDialog.getText(
            self, "隠れ線だけのレイヤー", u"レイヤー名（英数/_）:",
            QtWidgets.QLineEdit.Normal, default)
        if not ok or not (name or "").strip():
            return
        cmds.undoInfo(openChunk=True)
        try:
            lname = create_hidden_only_layer(self._lines, name.strip())
        except Exception as exc:
            cmds.warning("レイヤーを作れませんでした: {0}".format(exc))
            return
        finally:
            cmds.undoInfo(closeChunk=True)
        self._reload_layers()
        self._refresh()
        self.lbl_status.setText(u"レイヤー「{0}」を作りました（いま表示中）。".format(lname))

    # ---------- 表示 ----------
    def _refresh(self):
        if not self._lines:
            return
        self._updating = True
        try:
            # 他の画面でレイヤーが切り替わっていたら合わせる
            vis = _rs_visible_layer_name()
            if vis != self._layer():
                idx = self.cmb_layer.findData(vis)
                if idx < 0:
                    self._reload_layers()
                else:
                    self.cmb_layer.setCurrentIndex(idx)
            ln = self._lines[0]

            def _get(attr, dv):
                try:
                    return cmds.getAttr(ln + "." + attr)
                except Exception:
                    return dv
            self.chk_vis.setChecked(bool(_get(LN_SHOW_VIS, 1)))
            if _attr_exists(ln, LN_COLOR):
                line_rgb = _get(LN_COLOR, [(0, 0, 0)])[0]
            else:
                line_rgb = _tube_current_color(ln)   # まだ詳細設定を使っていないエッジ線
            self.btn_col.setStyleSheet(_rgb_style(line_rgb))
            mode = int(_get(LN_HID_MODE, 1))
            if 0 <= mode < len(self.rb_hid):
                self.rb_hid[mode].setChecked(True)
            same = bool(_get(LN_HID_SAME, 1))
            self.chk_same.setChecked(same)
            self.btn_hcol.setEnabled(not same)
            self.btn_hcol.setStyleSheet(_rgb_style(
                _get(LN_COLOR if same else LN_HID_COLOR, [(0, 0, 0)])[0]))
            a = int(round(float(_get(LN_HID_ALPHA, _SCRN_HIDDEN_ALPHA_DEF)) * 100))
            self.sl_alpha.setValue(a); self.sp_alpha.setValue(a)
            self.sp_dash.setValue(float(_get(LN_HID_DASH, _SCRN_HIDDEN_DASH_DEF)))
            fl = int(round(float(_get(LN_HID_FILL, _SCRN_HIDDEN_FILL_DEF)) * 100))
            self.sl_fill.setValue(fl); self.sp_fill.setValue(fl)
            tr = int(round(float(_get(LN_TRACE, 0.0)) * 100))
            self.sl_trace.setValue(tr); self.sp_trace.setValue(tr)
            td = int(round(float(_get(LN_TRACE_DARK, TRACE_DARK_DEF)) * 100))
            self.sl_tdark.setValue(td); self.sp_tdark.setValue(td)
            # v0.1.49: ブラシ
            for attr, (sl, sp) in self._br_widgets.items():
                bv = int(round(float(_get(attr, 0.0)) * 100))
                sl.setValue(bv); sp.setValue(bv)
            self.sp_bscale.setValue(int(round(float(_get(LN_BR_SCALE, 20.0)))))
            # v0.1.50: ブラシはスクリーン輪郭だけ（エッジ線・クリース線はチューブの形で太さが決まる）
            n_scrn = sum(1 for l2 in self._lines if _attr_exists(l2, SCRN_TAG))
            self.tabs.setTabEnabled(1, n_scrn > 0)
            if n_scrn == 0:
                self.lbl_brush_note.setText(u"")
                self.tabs.setTabToolTip(1, u"ブラシはスクリーン輪郭のラインで使えます"
                                           u"（エッジ線・クリース線は対象外）。")
            elif n_scrn < len(self._lines):
                self.lbl_brush_note.setText(u"ブラシはスクリーン輪郭のラインだけに入ります"
                                            u"（選択中のエッジ線・クリース線には入りません）。")
            else:
                self.lbl_brush_note.setText(u"")
            # 隠れ線を出さないときは関連項目を薄く、破線の項目は破線のときだけ
            for w in (self.chk_same, self.btn_hcol, self.sl_alpha, self.sp_alpha):
                w.setEnabled(mode != 0 and (w is not self.btn_hcol or not same))
            for w in (self.sp_dash, self.sl_fill, self.sp_fill):
                w.setEnabled(mode == 1)
            # このレイヤーで上書きしている項目に印
            layer = self._layer()
            for attr, (lb, rb, title) in self._rows.items():
                ov = False
                if layer:
                    keys = [LN_HID_COLOR, LN_HID_SAME] if attr in (LN_HID_COLOR, LN_HID_SAME) else [attr]
                    ov = any(_rs_find_override(layer, ln, k) is not None for k in keys)
                lb.setText((u"● " if ov else u"") + title)
                lb.setStyleSheet("color: #e5c76b; font-weight: bold;" if ov else "")
                rb.setVisible(bool(layer))
                rb.setEnabled(ov)
            # 紐づけ一覧（全ラインの合併）
            self.lst_occ.clear()
            seen = []
            for l2 in self._lines:
                for o in _line_occluders(l2):
                    if o not in seen:
                        seen.append(o)
            for o in seen:
                it = QtWidgets.QListWidgetItem(_short(o))
                it.setData(QtCore.Qt.UserRole, o)
                it.setToolTip(o)
                self.lst_occ.addItem(it)
            if not seen:
                self.lbl_occ_state.setText(u"まだ紐づけがありません。このラインの隠れ線は出ません。")
            elif not _hidden_scene_on():
                self.lbl_occ_state.setText(u"メイン画面の『隠れ線を透かして描く』が OFF のため、いまは描かれません。")
            elif _vp2_is_opengl():
                self.lbl_occ_state.setText(u"OpenGL のビューポートでは隠れ線は描かれません（DirectX11 で表示）。")
            else:
                self.lbl_occ_state.setText(u"")
        finally:
            self._updating = False

    def closeEvent(self, event):
        if _line_options_win[0] is self:
            _line_options_win[0] = None
        QtWidgets.QDialog.closeEvent(self, event)


def show_line_options(lines, owner=None):
    """ライン詳細設定ウィンドウを開く（開いていれば閉じて開き直す）。"""
    old = _line_options_win[0]
    if old is not None:
        _close_and_delete([old])      # v0.1.54: その場で破棄（新しいウィンドウを作る前に）
    lines = [ln for ln in lines or [] if _is_detail_line(ln)]
    if not lines:
        cmds.warning("ライン詳細設定はスクリーン輪郭・エッジ線・クリース線で使えます")
        return None
    dlg = _LineOptionsDialog(lines, owner)
    _line_options_win[0] = dlg
    dlg.show()
    return dlg


# ---- シーンを開き直したら隠れ線の紐づけを読み直す（ツールのウィンドウが閉じていても） ----
def _on_scene_changed():
    _clear_line_caches()
    try:
        _hidden_refresh_cache()
        _og_override_sync()
    except Exception:
        pass
    # v0.1.52: ウィンドウを開かなくても、曲率のキー・チャンネルボックスの変更が効くように。
    # もう無いラインのジョブだけ片付け、足りないジョブを張る（全部消して張り直すと、このイベントが
    # 遅れて届いたとき＝Maya が忙しいと数秒遅れる、その間の変更で呼ばれるはずのジョブまで消える）。
    # ※ om2 で書いた重み・頂点カラーはリファレンスしたラインでも保存されて開き直すと残る
    #   （Maya 2024 で確認）ので、開いたときに計算し直す必要はない。
    try:
        _ensure_all_curv_jobs()
    except Exception:
        pass
    try:
        if _toon_win is not None and _toon_win.isVisible():
            _toon_win.refresh_tree()
    except Exception:
        pass


_SCENE_JOB_FUNCS = ("_on_scene_changed", "_on_panel_focus", "_request_hidden_refresh")


def _kill_scene_jobs():
    """このツールが登録したシーン単位の scriptJob を消す（モジュール再読込で二重登録しない）。"""
    try:
        for j in cmds.scriptJob(listJobs=True) or []:
            if any(f in j for f in _SCENE_JOB_FUNCS):
                try:
                    cmds.scriptJob(kill=int(j.split(":")[0]), force=True)
                except Exception:
                    pass
    except Exception:
        pass


def _ensure_scene_jobs():
    _kill_scene_jobs()
    for ev in ("SceneOpened", "NewSceneOpened"):
        try:
            cmds.scriptJob(event=[ev, _on_scene_changed], protected=False)
        except Exception:
            pass
    # v0.1.53: 後から作ったパネル・Undo/Redo・名前の変更に隠れ線のオーバーライドを追従させる。
    # 名前の変更は他のスクリプトが大量に起こすこともあるので、読み直しは次のイベントループで 1 回だけ。
    jobs = [("ModelPanelSetFocus", _on_panel_focus)]
    jobs += [(ev, _request_hidden_refresh) for ev in ("Undo", "Redo", "NameChanged")]
    for ev, fn in jobs:
        try:
            cmds.scriptJob(event=[ev, fn], protected=False)
        except Exception:
            pass


_toon_win = None


# --------------------------------------------------------------------------- #
# GitHub ホットアップデート（maya-hot-update-patterns.md §1-7/§1-8/§1-9 準拠）
#   ・SHA-pinned URL で CDN キャッシュ回避（§1-7）
#   ・ドラッグ&ドロップに依存せず UI ボタンから更新（§1-8）
#   ・evalDeferred 3 段でウィンドウ破棄→install→再オープン（§1-9）
#   ※ 取得は Python3 の urllib.request 前提（Maya 2022+）。f-string は使わず
#     本体を Py2 でも import 可能なまま保つ。
# --------------------------------------------------------------------------- #

def _resolve_latest_sha():
    """raw.githubusercontent.com はパスのみを cache key にするため、確実な
    cache-buster は SHA-pinned URL のみ。API で対象ブランチの先頭 commit を取得。"""
    import json
    import random
    import time
    import urllib.request
    salt = "{0:.6f}_{1}".format(time.time(), random.randint(0, 2 ** 32))
    req = urllib.request.Request(
        "{0}/branches/{1}?_={2}".format(_GITHUB_API, _GITHUB_BRANCH, salt),
        headers={
            "Accept": "application/vnd.github+json",
            "Cache-Control": "no-cache",
            "User-Agent": "{0}-updater/{1}".format(_PACKAGE, salt),
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read().decode("utf-8"))["commit"]["sha"]
    except Exception as exc:
        print("[{0}] SHA lookup failed ({1}); falling back to {2}".format(
            _PACKAGE, exc, _GITHUB_BRANCH))
        return _GITHUB_BRANCH


def _fetch_releases():
    """GitHub Releases API を叩いてリリース一覧を取得。
    返り値: [{"tag","name","sha","body","prerelease","published_at","is_dev"}, ...]。
    先頭に「開発ブランチ(dev)最新」仮想項目を1つ付ける(_GITHUB_BRANCH の HEAD)。
    失敗したら最低限 dev 項目だけ入れて返す。"""
    import json, random, time, urllib.request
    out = []
    # 1) dev ブランチ HEAD を仮想リリースとして追加
    dev_sha = _resolve_latest_sha()
    out.append({
        "tag": "dev",
        "name": "開発ブランチ最新 ({0})".format(_GITHUB_BRANCH),
        "sha": dev_sha,
        "body": "ブランチ {0} の先端commit。チームメンバー向けの安定版ではない。".format(_GITHUB_BRANCH),
        "prerelease": True, "published_at": "", "is_dev": True,
    })
    # 2) リリース一覧(published のみ)
    try:
        salt = "{0:.6f}_{1}".format(time.time(), random.randint(0, 2 ** 32))
        req = urllib.request.Request(
            "{0}/releases?per_page=50&_={1}".format(_GITHUB_API, salt),
            headers={
                "Accept": "application/vnd.github+json",
                "Cache-Control": "no-cache",
                "User-Agent": "{0}-updater/{1}".format(_PACKAGE, salt),
            })
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read().decode("utf-8"))
        for r in data:
            if r.get("draft"):
                continue
            tag = r.get("tag_name") or ""
            if not tag:
                continue
            # tag の SHA を得るには別クエリ必要だが、raw URL には tag 名を直接使える
            # ので sha フィールドには tag をそのまま入れる(raw.githubusercontent は
            # ref として tag を解決してくれる)。
            out.append({
                "tag": tag,
                "name": r.get("name") or tag,
                "sha": tag,
                "body": r.get("body") or "",
                "prerelease": bool(r.get("prerelease")),
                "published_at": r.get("published_at") or "",
                "is_dev": False,
            })
    except Exception as exc:
        print("[{0}] fetch releases failed: {1}".format(_PACKAGE, exc))
    return out


class _VersionDialog(QtWidgets.QDialog):
    """GitHub Releases を一覧表示し、任意のバージョンをインストールするダイアログ。"""
    def __init__(self, parent=None):
        super(_VersionDialog, self).__init__(parent)
        self.setWindowTitle("バージョン選択 — OG_Toonline_Manager")
        self.setMinimumSize(640, 460)
        self.selected_sha = None
        self.selected_tag = None

        lay = QtWidgets.QVBoxLayout(self)
        info = QtWidgets.QLabel(
            "現在のバージョン: v{0}\n"
            "GitHub Releases からインストールするバージョンを選んでください。\n"
            "'開発ブランチ最新' は dev の HEAD (不安定の可能性あり)。"
            .format(__version__))
        info.setStyleSheet("color:#888;")
        lay.addWidget(info)

        self.list = QtWidgets.QListWidget()
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.list.itemSelectionChanged.connect(self._on_sel)
        lay.addWidget(self.list, 1)

        self.body = QtWidgets.QTextEdit()
        self.body.setReadOnly(True)
        self.body.setMaximumHeight(140)
        lay.addWidget(self.body)

        brow = QtWidgets.QHBoxLayout()
        self.btn_refresh = QtWidgets.QPushButton("再取得")
        self.btn_refresh.clicked.connect(self._reload)
        brow.addWidget(self.btn_refresh)
        brow.addStretch(1)
        self.btn_cancel = QtWidgets.QPushButton("キャンセル")
        self.btn_cancel.clicked.connect(self.reject)
        brow.addWidget(self.btn_cancel)
        self.btn_install = QtWidgets.QPushButton("この版をインストール")
        self.btn_install.setEnabled(False)
        self.btn_install.clicked.connect(self._accept)
        brow.addWidget(self.btn_install)
        lay.addLayout(brow)

        self._releases = []
        self._reload()

    def _reload(self):
        self.list.clear()
        self.body.setPlainText("取得中...")
        QtWidgets.QApplication.processEvents()
        try:
            self._releases = _fetch_releases()
        except Exception as e:
            self._releases = []
            self.body.setPlainText("取得失敗: {0}".format(e))
            return
        for r in self._releases:
            label = r["name"]
            if r["is_dev"]:
                label = "[dev] " + label
            elif r["prerelease"]:
                label = "[pre] " + label
            if r.get("published_at"):
                label += "    " + r["published_at"][:10]
            label += "    (ref: {0})".format(r["sha"][:8] if r["is_dev"] else r["tag"])
            self.list.addItem(label)
        if self._releases:
            self.list.setCurrentRow(0)
        self.body.setPlainText("" if self._releases else "リリースが取得できませんでした。")

    def _on_sel(self):
        row = self.list.currentRow()
        if row < 0 or row >= len(self._releases):
            self.btn_install.setEnabled(False)
            self.body.setPlainText("")
            return
        r = self._releases[row]
        self.selected_sha = r["sha"]
        self.selected_tag = r["tag"]
        self.btn_install.setEnabled(True)
        self.body.setPlainText(r.get("body") or "(リリースノートなし)")

    def _accept(self):
        self.accept()


def open_version_dialog(*_args):
    """UI ボタンのコールバック: バージョン選択ダイアログを表示 → 選択バージョンをインストール。"""
    try:
        parent = None
        for w in QtWidgets.QApplication.topLevelWidgets():
            if w.objectName() == WINDOW_OBJ:
                parent = w; break
        dlg = _VersionDialog(parent=parent)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        sha = dlg.selected_sha or _GITHUB_BRANCH
        tag = dlg.selected_tag or ""
    except Exception as e:
        try: cmds.confirmDialog(title="バージョン選択失敗", message=str(e), button=["OK"])
        except Exception: pass
        return
    # 実行は次の Maya idle に defer (ウィンドウ破棄→install→再オープン)
    cmds.evalDeferred(lambda: _run_update(sha_override=sha, tag_label=tag), lowestPriority=True)


def update_from_github(*_args):
    """後方互換の入り口: 開発ブランチ最新をそのまま適用(旧 UI/シェルフボタン用)。"""
    cmds.evalDeferred(_run_update, lowestPriority=True)


def _close_tool_windows():
    """モジュールグローバル参照＋同名 objectName のツールウィンドウを全て閉じる（PySide）。"""
    try:
        global _toon_win
        wins = _find_tool_windows((WINDOW_OBJ, _LINEOPT_OBJ))
        if _qt_valid(_toon_win) and _toon_win not in wins:
            wins.insert(0, _toon_win)
        _close_and_delete(wins)
    except Exception:
        pass
    # v0.1.46: 更新でモジュールが入れ替わる前に、このモジュールのオーバーライドと
    # シーン監視を片付ける（新しいモジュールが同名で登録し直す）
    try:
        _edge_disable()
    except Exception:
        pass
    _kill_scene_jobs()


def _run_update(sha_override=None, tag_label=""):
    """SHA/tag-pinned でインストーラを取得 → 自ウィンドウを閉じて exec → 再オープンを defer。
    sha_override が指定されれば tag(リリース)またはブランチ名/SHA を直接使う。
    OG_TOONLINE_MANAGER_REF 環境変数経由で install.py にも伝播する。"""
    import os
    import sys
    import traceback
    import urllib.request
    sha = sha_override if sha_override else _resolve_latest_sha()
    # install.py 側に ref を env var で渡す(リリース tag の場合もモジュール本体の
    # 取得先が揃うようにする)
    try:
        os.environ["OG_TOONLINE_MANAGER_REF"] = str(sha)
    except Exception:
        pass
    url = "{0}/{1}/{2}".format(_GITHUB_RAW_BASE, sha, _INSTALLER_FILE)
    label = tag_label or sha
    print("[{0}] update: fetching {1} ({2})".format(_PACKAGE, url, label))
    try:
        req = urllib.request.Request(url, headers={
            "Cache-Control": "no-cache",
            "User-Agent": "{0}-updater/{1}".format(_PACKAGE, sha[:10]),
        })
        source = urllib.request.urlopen(req, timeout=30).read()
    except Exception as exc:
        traceback.print_exc()
        try:
            cmds.confirmDialog(title="Update failed",
                               message="{0} の取得に失敗しました:\n{1}".format(_INSTALLER_FILE, exc),
                               button=["OK"])
        except Exception:
            pass
        return

    _close_tool_windows()

    ns = {"__name__": "install", "__file__": "<github>"}
    try:
        try:
            exec(compile(source, _INSTALLER_FILE + " (from GitHub)", "exec"), ns)
        finally:
            # v0.1.52: インストーラは exec の中で読み終えるので、ここで消す。残すと同じ Maya で
            # もう一度「GitHub から更新」したとき、新しい版ではなく前回と同じ版を入れていた。
            os.environ.pop("OG_TOONLINE_MANAGER_REF", None)
    except Exception as exc:
        traceback.print_exc()
        try:
            cmds.confirmDialog(
                title="Update failed",
                message=("{0} 実行でエラー:\n{1}: {2}\n\n"
                         "詳細は Script Editor を確認してください。").format(
                             _INSTALLER_FILE, type(exc).__name__, exc),
                button=["OK"])
        except Exception:
            pass
        return

    # 自分自身を sys.modules から落とし、次の import でディスクの最新を読ませる（§1-6）
    for m in [k for k in list(sys.modules) if k == _PACKAGE]:
        sys.modules.pop(m, None)

    # install() の confirmDialog が閉じ切ってから再オープン（§1-9）
    cmds.evalDeferred(_reopen_after_update, lowestPriority=True)


def _reopen_after_update():
    """更新後、ディスクの最新モジュールを fresh import して show()。"""
    import importlib
    import sys
    import traceback
    try:
        if _PACKAGE in sys.modules:
            importlib.reload(sys.modules[_PACKAGE])
        mod = importlib.import_module(_PACKAGE)
        mod.show()
    except Exception as exc:
        traceback.print_exc()
        try:
            cmds.confirmDialog(
                title="Reopen failed",
                message=("更新は完了しましたが、ウィンドウの再オープンに失敗しました:\n"
                         "{0}: {1}\n\nシェルフボタンから開き直してください。").format(
                             type(exc).__name__, exc),
                button=["OK"])
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 画像空間エッジ検出（オブジェクト単位・重なり線をメッシュの上に描く／VP2・実験）
#   深度ラプラシアンで段差(=シルエット/自己重なり)を検出し、シーンの色に線を合成。
#   ★オブジェクト単位維持: エッジ検出用の深度は「対象メッシュだけ」を描いた深度を使う
#     （objectSetOverride）。色は全シーンを描くので他オブジェクトは通常表示のまま、
#     線は対象メッシュにだけ乗る。手前の別オブジェクトに隠れず“上のレイヤー”に出る。
#   レンダー API は遅延 import（この機能を使わなければ import されない＝本体は Py2 でも安全）。
#   ※ MRenderOverride は実機(VP2/DirectX11)でのみ動作。ここでは構造のみ・実機検証は別途。
# --------------------------------------------------------------------------- #

EDGE_OVR_NAME = "OG_ToonEdgeOutline"
# しきい値はビュー空間深度で「相対差」|Δz|/z を測る無次元値（既定 0.05 = 5%）。
# 実機検証で 0.01 は微差まで拾って線が滲む/太る（かすれ）、0.05〜0.1 が実用域だった。
# v0.1.36: intersect_mode=1 で「対象メッシュと他オブジェクトの交差線」モード。
#   intersect_threshold は target と other の深度一致判定しきい値（相対差 |Δz|/z）。
_EDGE_PARAMS = {"threshold": 0.05, "thickness": 1.0, "color": (0.0, 0.0, 0.0),
                "near": 0.1, "far": 10000.0, "debug": 0,
                "intersect_mode": 0, "intersect_threshold": 0.01}
_EDGE_TARGETS = []          # エッジ検出対象の transform 名リスト（オブジェクト単位を保持）
_edge_override = None
_edge_size_logged = (0, 0)  # 直近ログしたターゲット (w, h) — 変化時に一度だけ報告

_EDGE_FX = """// OG Toonline Manager - object-scoped screen-space depth-edge outline
Texture2D gColorTex;      // 全シーンの色
Texture2D gDepthTex;      // 対象メッシュだけを描いた深度（対象なしなら 1.0=遠クリア）
Texture2D gDepthOtherTex; // 対象を除いた他メッシュだけの深度（対象を含まない部分）
Texture2D gDepthSceneTex; // v0.1.40: 全シーン深度（occlusion マスク用）

SamplerState gSamp { Filter = MIN_MAG_MIP_POINT; AddressU = Clamp; AddressV = Clamp; };

float2 gTexel     = {0.001f, 0.001f};   // (1/width, 1/height) * thickness
float  gThreshold = 0.01f;              // ビュー空間深度の相対差しきい値（|Δz|/z）
float3 gLineColor = {0.0f, 0.0f, 0.0f};
float  gNear      = 0.1f;
float  gFar       = 10000.0f;
float  gOrtho      = 0.0f;              // v0.1.53: 1 = 正投影カメラ
float  gOrthoScale = 10.0f;             // 正投影のとき相対差の基準にする長さ（正投影の幅）
int    gDebug     = 0;                  // 1: 線形化深度を可視化（切り分け用）
int    gIntersectMode    = 0;           // 1: 対象と他オブジェクトの交差線モード
int    gEdgeMode         = 0;           // 1: 対象の自己エッジ（旧・画像空間エッジ）
float  gIntersectThresh  = 0.01f;       // 深度一致とみなす相対差しきい値

struct VIN  { float3 Pos : POSITION; float2 UV : TEXCOORD0; };
struct VOUT { float4 Pos : SV_Position; float2 UV : TEXCOORD0; };

VOUT VS(VIN i) { VOUT o; o.Pos = float4(i.Pos, 1.0f); o.UV = i.UV; return o; }

// DirectX の深度 [0,1] → ビュー空間の線形深度
float linearizeDepth(float z)
{
    if (gOrtho > 0.5f) return gNear + z * (gFar - gNear);   // 正投影の深度は線形
    // near*far / (far - z*(far-near)). z=1(遠)→far, z=0(近)→near
    float denom = gFar - z * (gFar - gNear);
    return (gNear * gFar) / max(denom, 1e-6f);
}

// 相対差の基準。透視はカメラからの距離、正投影は距離に意味がないので正投影の幅。
float og_scale(float z) { return max((gOrtho > 0.5f) ? gOrthoScale : z, 1e-4f); }

// 交差モード本体: 対象と他の 2 深度が「近接」かつ「隣接で前後が反転」した位置に線を出す。
// near-miss（近いが交差していない）を避けるため sign flip を併用。
// v0.1.42: さらに「シーン上で target 最前面 と other 最前面 の両サイドが隣接する」条件を
// 追加。target が他オブジェクトを完全に手前で隠してる領域 (奥で深度が偶然近接する) の
// 擬似交差を排除する。
float intersectEdge(float2 uv, float2 t)
{
    float ra_c = gDepthTex.Sample(gSamp, uv).r;
    float rb_c = gDepthOtherTex.Sample(gSamp, uv).r;
    float rs_c = gDepthSceneTex.Sample(gSamp, uv).r;
    // 片方でも遠クリア（対象 or 他がこのピクセルに無い）は交差ではない
    if (ra_c >= 0.9999f || rb_c >= 0.9999f) return 0.0f;
    float za_c = linearizeDepth(ra_c);
    float zb_c = linearizeDepth(rb_c);
    float zs_c = linearizeDepth(rs_c);
    float rel_c = abs(za_c - zb_c) / og_scale(min(za_c, zb_c));
    // 中心が近接の目安を外れていればまず線にしない（範囲を少し緩めて取りこぼし防止）
    if (rel_c > gIntersectThresh * 3.0f) return 0.0f;
    bool a_front_c = (za_c <= zb_c);
    // scene の最前面が target / other どちらか（イプシロンは画面空間深度の 0.2%）
    float eps_c = og_scale(max(za_c, zb_c)) * 0.002f;
    bool saw_a_front = (abs(zs_c - za_c) < eps_c);
    bool saw_b_front = (abs(zs_c - zb_c) < eps_c);

    bool any_flip = false;
    float min_rel = rel_c;
    // 4 方向で前後関係が反転しているか（= 実際に 3D で交差している証拠）
    [unroll]
    for (int k = 0; k < 4; k++)
    {
        float2 off = (k == 0) ? float2(0.0f, -t.y)
                   : (k == 1) ? float2(0.0f,  t.y)
                   : (k == 2) ? float2(-t.x, 0.0f)
                   :            float2( t.x, 0.0f);
        float2 nuv = uv + off;
        float ra_n = gDepthTex.Sample(gSamp, nuv).r;
        float rb_n = gDepthOtherTex.Sample(gSamp, nuv).r;
        float rs_n = gDepthSceneTex.Sample(gSamp, nuv).r;
        if (ra_n >= 0.9999f || rb_n >= 0.9999f) continue;
        float za_n = linearizeDepth(ra_n);
        float zb_n = linearizeDepth(rb_n);
        float zs_n = linearizeDepth(rs_n);
        bool a_front_n = (za_n <= zb_n);
        if (a_front_c != a_front_n) any_flip = true;
        float rel_n = abs(za_n - zb_n) / og_scale(min(za_n, zb_n));
        min_rel = min(min_rel, rel_n);
        float eps_n = og_scale(max(za_n, zb_n)) * 0.002f;
        if (abs(zs_n - za_n) < eps_n) saw_a_front = true;
        if (abs(zs_n - zb_n) < eps_n) saw_b_front = true;
    }
    if (!any_flip) return 0.0f;
    // v0.1.42: scene 上で target と other の両方が最前面になる領域が近傍に存在しなければ
    // 「見えている交差」ではない（奥に隠れた擬似交差を排除）。
    if (!(saw_a_front && saw_b_front)) return 0.0f;
    // 強度: 近接ほど強い。min_rel（周辺で一番近い点）で smoothstep
    float hi = gIntersectThresh * 1.5f;
    return 1.0f - smoothstep(gIntersectThresh * 0.5f, hi, min_rel);
}

float4 PS(VOUT i) : SV_Target
{
    float2 t = gTexel;
    float zc = gDepthTex.Sample(gSamp, i.UV).r;
    float zu = gDepthTex.Sample(gSamp, i.UV + float2(0.0f, -t.y)).r;
    float zd = gDepthTex.Sample(gSamp, i.UV + float2(0.0f,  t.y)).r;
    float zl = gDepthTex.Sample(gSamp, i.UV + float2(-t.x, 0.0f)).r;
    float zr = gDepthTex.Sample(gSamp, i.UV + float2( t.x, 0.0f)).r;

    float c = linearizeDepth(zc);
    float u = linearizeDepth(zu);
    float d = linearizeDepth(zd);
    float l = linearizeDepth(zl);
    float r = linearizeDepth(zr);

    // 対象外領域（zc==1.0）は線を出さない。エッジは 4 方向の差の最大値で判定
    // （ラプラシアンだと対象と背景の境界で両サイドに強く出て太る）。
    float dmax = max(max(abs(u - c), abs(d - c)), max(abs(l - c), abs(r - c)));
    float rel  = dmax / og_scale(c);

    if (gDebug != 0)
    {
        // 線形化深度を [near, far] で正規化して可視化（対象なし=白, 近い=黒）。
        float span = max(gFar - gNear, 1e-4f);
        float g = saturate((c - gNear) / span);
        return float4(g, g, g, 1.0f);
    }

    float4 sceneA = gColorTex.Sample(gSamp, i.UV);   // v0.1.48: アルファも引き継ぐ
    float3 scene = sceneA.rgb;

    // v0.1.40: occlusion マスク。target が「実際に画面に見えてる」ピクセルだけ線を描く。
    // tDepth(target) と tDepthScene(全シーン) が一致 = target が最前面 = 見えてる。
    // 一致しない = target は他メッシュに隠されてる → そのピクセルに線を描かない
    // （以前は target_depth を根拠に、手前の別メッシュの上に線が出るバグがあった）。
    float zs = gDepthSceneTex.Sample(gSamp, i.UV).r;
    float visible = (zc < 0.9999f && abs(zc - zs) < 0.001f) ? 1.0f : 0.0f;

    // v0.1.44: 交差線モードは intersect 線「だけ」を描く。
    // （以前は self-silhouette と合成していたため、交差線 ON にすると cup 自身の
    // 円筒 tangential silhouette が余計に overlay されて「cup 中に縦線がある」ように
    // 見えていた。）
    // v0.1.46: どちらも OFF（隠れ線だけでオーバーライドを使うとき）は素通し。
    float e = 0.0f;
    if (gIntersectMode != 0)
    {
        e = intersectEdge(i.UV, t);
    }
    else if (gEdgeMode != 0)
    {
        float mask = visible;
        float hi = gThreshold * 1.15f;
        e = mask * smoothstep(gThreshold, hi, rel);
    }
    return float4(lerp(scene, gLineColor, e), max(sceneA.a, e));
}

technique11 Main { pass p0 {
    SetVertexShader(CompileShader(vs_5_0, VS()));
    SetPixelShader(CompileShader(ps_5_0, PS()));
} }
"""


_EDGE_OGSFX = """// OG Toonline Manager - edge outline (OGSFX/GLSL variant for OpenGL VP2)

uniform texture2D gColorTex;
uniform sampler2D gColorSamp = sampler_state { texture = <gColorTex>; };
uniform texture2D gDepthTex;
uniform sampler2D gDepthSamp = sampler_state { texture = <gDepthTex>; };
uniform texture2D gDepthOtherTex;
uniform sampler2D gDepthOtherSamp = sampler_state { texture = <gDepthOtherTex>; };
uniform texture2D gDepthSceneTex;
uniform sampler2D gDepthSceneSamp = sampler_state { texture = <gDepthSceneTex>; };

uniform vec2 gTexel;
uniform float gThreshold;
uniform vec3 gLineColor;
uniform float gNear;
uniform float gFar;
uniform float gOrtho;                // v0.1.53: 1 = 正投影カメラ（Python から毎フレーム入れる）
uniform float gOrthoScale;           // 正投影のとき相対差の基準にする長さ（正投影の幅）
uniform int gDebug;
uniform int gIntersectMode;
uniform int gEdgeMode;
uniform float gIntersectThresh;

attribute VS_IN {
    vec3 Pos : POSITION;
    vec2 UV : TEXCOORD0;
};
attribute VS_TO_PS {
    vec2 VSUV : TEXCOORD0;
};
attribute PS_OUT {
    vec4 colorOut : COLOR0;
};

GLSLShader VS {
    void main() {
        gl_Position = vec4(Pos, 1.0);
        VSUV = UV;
    }
}

GLSLShader PS {
    float linearize(float z) {
        if (gOrtho > 0.5) return gNear + z * (gFar - gNear);   // 正投影の深度は線形
        float denom = gFar - z * (gFar - gNear);
        return (gNear * gFar) / max(denom, 1e-6);
    }

    float og_scale(float z) { return max((gOrtho > 0.5) ? gOrthoScale : z, 1e-4); }

    float intersectEdge(vec2 uv, vec2 t) {
        float ra_c = texture2D(gDepthSamp, uv).r;
        float rb_c = texture2D(gDepthOtherSamp, uv).r;
        float rs_c = texture2D(gDepthSceneSamp, uv).r;
        if (ra_c >= 0.9999 || rb_c >= 0.9999) return 0.0;
        float za_c = linearize(ra_c);
        float zb_c = linearize(rb_c);
        float zs_c = linearize(rs_c);
        float rel_c = abs(za_c - zb_c) / og_scale(min(za_c, zb_c));
        if (rel_c > gIntersectThresh * 3.0) return 0.0;
        bool a_front_c = (za_c <= zb_c);
        // v0.1.42: scene 最前面が target / other どちらか（HLSL 版と同条件）
        float eps_c = og_scale(max(za_c, zb_c)) * 0.002;
        bool saw_a_front = (abs(zs_c - za_c) < eps_c);
        bool saw_b_front = (abs(zs_c - zb_c) < eps_c);
        bool any_flip = false;
        float min_rel = rel_c;
        for (int k = 0; k < 4; ++k) {
            vec2 off;
            if (k == 0)      off = vec2(0.0, -t.y);
            else if (k == 1) off = vec2(0.0,  t.y);
            else if (k == 2) off = vec2(-t.x, 0.0);
            else             off = vec2( t.x, 0.0);
            vec2 nuv = uv + off;
            float ra_n = texture2D(gDepthSamp, nuv).r;
            float rb_n = texture2D(gDepthOtherSamp, nuv).r;
            float rs_n = texture2D(gDepthSceneSamp, nuv).r;
            if (ra_n >= 0.9999 || rb_n >= 0.9999) continue;
            float za_n = linearize(ra_n);
            float zb_n = linearize(rb_n);
            float zs_n = linearize(rs_n);
            bool a_front_n = (za_n <= zb_n);
            if (a_front_c != a_front_n) any_flip = true;
            float rel_n = abs(za_n - zb_n) / og_scale(min(za_n, zb_n));
            min_rel = min(min_rel, rel_n);
            float eps_n = og_scale(max(za_n, zb_n)) * 0.002;
            if (abs(zs_n - za_n) < eps_n) saw_a_front = true;
            if (abs(zs_n - zb_n) < eps_n) saw_b_front = true;
        }
        if (!any_flip) return 0.0;
        // v0.1.42: 奥に隠れた擬似交差を排除
        if (!(saw_a_front && saw_b_front)) return 0.0;
        float hi = gIntersectThresh * 1.5;
        return 1.0 - smoothstep(gIntersectThresh * 0.5, hi, min_rel);
    }

    void main() {
        vec2 t = gTexel;
        float zc = texture2D(gDepthSamp, VSUV).r;
        float zu = texture2D(gDepthSamp, VSUV + vec2(0.0, -t.y)).r;
        float zd = texture2D(gDepthSamp, VSUV + vec2(0.0,  t.y)).r;
        float zl = texture2D(gDepthSamp, VSUV + vec2(-t.x, 0.0)).r;
        float zr = texture2D(gDepthSamp, VSUV + vec2( t.x, 0.0)).r;

        float c = linearize(zc);
        float u = linearize(zu);
        float d = linearize(zd);
        float l = linearize(zl);
        float r = linearize(zr);

        float dmax = max(max(abs(u - c), abs(d - c)), max(abs(l - c), abs(r - c)));
        float rel = dmax / og_scale(c);

        if (gDebug != 0) {
            float span = max(gFar - gNear, 1e-4);
            float g = clamp((c - gNear) / span, 0.0, 1.0);
            colorOut = vec4(g, g, g, 1.0);
            return;
        }

        vec4 sceneA = texture2D(gColorSamp, VSUV);   // v0.1.48: アルファも引き継ぐ
        vec3 scene = sceneA.rgb;

        // v0.1.40: occlusion マスク（HLSL 版と同じ）
        float zs = texture2D(gDepthSceneSamp, VSUV).r;
        float visible = (zc < 0.9999 && abs(zc - zs) < 0.001) ? 1.0 : 0.0;

        // v0.1.44: 交差線モード時は intersect だけ描く（self-silhouette と合成しない）
        float e = 0.0;
        if (gIntersectMode != 0) {
            e = intersectEdge(VSUV, t);
        } else if (gEdgeMode != 0) {
            float mask = visible;
            float hi = gThreshold * 1.15;
            e = mask * smoothstep(gThreshold, hi, rel);
        }

        colorOut = vec4(mix(scene, gLineColor, e), max(sceneA.a, e));
    }
}

technique Main {
    pass p0 {
        VertexShader (in VS_IN, out VS_TO_PS) = VS;
        PixelShader (in VS_TO_PS, out PS_OUT) = PS;
    }
}
"""


def _edge_fx_path():
    """現 drawAPI に応じて HLSL(.fx) か GLSL(.ogsfx) のパスを返して書き出す。
    v0.1.36: OpenGL VP2 でも動くように GLSL 版を追加。
    v0.1.42: MShaderManager.getEffectsFileShader が path でキャッシュするため、バージョン
    番号をファイル名に埋め込んでキャッシュバスティング（新版リリース毎に新しいパスで
    load される→新 HLSL/GLSL が反映）。"""
    is_gl = _vp2_is_opengl()
    d = os.path.join(cmds.internalVar(userAppDir=True), "OG_Toonline_Manager")
    try:
        if not os.path.isdir(d):
            os.makedirs(d)
    except Exception:
        d = cmds.internalVar(userTmpDir=True)
    ver_tag = __version__.replace(".", "_")
    if is_gl:
        p = os.path.join(d, "OG_EdgeOutline_v" + ver_tag + ".ogsfx").replace("\\", "/")
        src = _EDGE_OGSFX
    else:
        p = os.path.join(d, "OG_EdgeOutline_v" + ver_tag + ".fx").replace("\\", "/")
        src = _EDGE_FX
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(src)
    except Exception:
        pass
    return p


def _edge_set_targets(names):
    """エッジ検出の対象メッシュ（transform 名）を設定。存在するものだけ保持。"""
    global _EDGE_TARGETS
    _EDGE_TARGETS = [n for n in (names or []) if cmds.objExists(n)]


def _edge_set_params(threshold=None, thickness=None, color=None, debug=None,
                     intersect_mode=None, intersect_threshold=None):
    if threshold is not None:
        # smoothstep(gThreshold, gThreshold*2, ..) が 0/0 になる事故を防ぐ最小値
        _EDGE_PARAMS["threshold"] = max(1e-4, float(threshold))
    if thickness is not None:
        _EDGE_PARAMS["thickness"] = float(thickness)
    if color is not None:
        _EDGE_PARAMS["color"] = (color[0], color[1], color[2])
    if debug is not None:
        _EDGE_PARAMS["debug"] = 1 if debug else 0
    if intersect_mode is not None:
        _EDGE_PARAMS["intersect_mode"] = 1 if intersect_mode else 0
    if intersect_threshold is not None:
        _EDGE_PARAMS["intersect_threshold"] = max(1e-4, float(intersect_threshold))
    try:
        cmds.refresh()
    except Exception:
        pass


def _is_toon_node(xform):
    """toon 系（ハル/エッジ/フレネル/SCRN/マスク/隙間埋め）のラインを交差検出の『他』集合から外すための判定。"""
    try:
        for a in (TAG, EDGE_TAG, FRES_TAG, SCRN_TAG, MASK_TAG, GAPFILL_TAG):
            if cmds.attributeQuery(a, node=xform, exists=True):
                return True
    except Exception:
        pass
    return False


def _edge_other_transforms():
    """『他オブジェクト』= シーンのポリメッシュ transform から target と toon ラインを除いたもの。"""
    others = []
    try:
        tgt = set(_EDGE_TARGETS)
        meshes = cmds.ls(type="mesh", long=True) or []
        seen = set()
        for m in meshes:
            try:
                par = cmds.listRelatives(m, parent=True, fullPath=True) or []
            except Exception:
                par = []
            for p in par:
                if p in seen:
                    continue
                seen.add(p)
                # short name での一致も見る（EDGE_TARGETS は短名のことがある）
                short = p.rsplit("|", 1)[-1]
                if p in tgt or short in tgt:
                    continue
                if _is_toon_node(p):
                    continue
                others.append(p)
    except Exception:
        pass
    return others


_EDGE_CAM_ORTHO = {"ortho": False, "scale": 10.0}


def _edge_active_camera_near_far(destination=None):
    """指定パネル（無ければフォーカス→最初の modelPanel）の active camera の near/far を返す。
    複数ビューポート運用でも、shader() 実行中はその描画対象パネルのカメラを見るのが正しい。
    取得に失敗したら現在値を維持。
    v0.1.53: 同じカメラが正投影かどうかと正投影の幅を _EDGE_CAM_ORTHO に入れる（_edge_camera_ortho で読む）。"""
    try:
        panels = []
        if destination:
            # destination は setup(destinationName) から渡るパネル名（優先）
            try:
                if cmds.getPanel(typeOf=destination) == "modelPanel":
                    panels.append(destination)
            except Exception:
                pass
        p = cmds.getPanel(withFocus=True)
        if p and p not in panels and cmds.getPanel(typeOf=p) == "modelPanel":
            panels.append(p)
        for pp in (cmds.getPanel(type="modelPanel") or []):
            if pp not in panels:
                panels.append(pp)
        for panel in panels:
            cam = cmds.modelEditor(panel, q=True, camera=True)
            if not cam or not cmds.objExists(cam):
                continue
            cam_shape = cam
            if cmds.nodeType(cam) == "transform":
                shs = cmds.listRelatives(cam, s=True, type="camera") or []
                if shs:
                    cam_shape = shs[0]
            n = float(cmds.getAttr(cam_shape + ".nearClipPlane"))
            f = float(cmds.getAttr(cam_shape + ".farClipPlane"))
            if n > 0.0 and f > n:
                try:
                    ortho = bool(cmds.getAttr(cam_shape + ".orthographic"))
                    scale = float(cmds.getAttr(cam_shape + ".orthographicWidth")) if ortho else 10.0
                except Exception:
                    ortho, scale = False, 10.0
                _EDGE_CAM_ORTHO["ortho"] = ortho
                _EDGE_CAM_ORTHO["scale"] = max(scale, 1e-3)
                return n, f
    except Exception:
        pass
    return _EDGE_PARAMS["near"], _EDGE_PARAMS["far"]


def _edge_set_ortho_params(s):
    """直前の _edge_active_camera_near_far で見たカメラの正投影の情報をシェーダへ入れる（v0.1.53）。"""
    try:
        s.setParameter("gOrtho", 1.0 if _EDGE_CAM_ORTHO["ortho"] else 0.0)
        s.setParameter("gOrthoScale", float(_EDGE_CAM_ORTHO["scale"]))
    except Exception:
        pass


def _edge_active():
    return _edge_override is not None


# v0.1.46: オーバーライドを使う機能の状態。交差線/旧エッジは UI のトグル、隠れ線は
# 紐づけの有無（_HIDDEN_GROUPS）とシーン設定（ROOT.toonHiddenOn）で決まる。
_OVR_STATE = {"edge": False, "intersect": False}


def _tube_axis(line):
    """チューブ（エッジ線・クリース線）の中心線。ライン配下のカーブのうち一番長いもの（円の断面は短い）を
    ワールドの折れ線にして (点のリスト, 長さ) で返す。点は最大 _TUBE_AXIS_MAX 個（多いときは間引く）。
    描画中に毎フレーム呼ばれるので om2 で測る（cmds.arclen 等は Undo 履歴に積まれて直前の操作が戻らなくなる）。"""
    best = None
    try:
        import maya.api.OpenMaya as om2
        for cv in cmds.listRelatives(line, allDescendents=True, type="nurbsCurve", f=True) or []:
            try:
                sel = om2.MSelectionList()
                sel.add(cv)
                fn = om2.MFnNurbsCurve(sel.getDagPath(0))
                ln = float(fn.length())
                if best is None or ln > best[1]:
                    best = (fn, ln)
            except Exception:
                pass
        if best is None:
            return [], 1.0
        fn, ln = best
        lo, hi = fn.knotDomain
        if fn.degree == 1:
            # 折れ線のカーブ（polyToCurve の既定）: 区間の境目＝ノットの位置に点がある
            params = sorted(set(k for k in fn.knots() if lo - 1e-9 <= k <= hi + 1e-9))
        else:
            params = [lo + (hi - lo) * j / 63.0 for j in range(64)]
        if len(params) > _TUBE_AXIS_MAX:
            step = (len(params) - 1) / float(_TUBE_AXIS_MAX - 1)
            params = [params[int(round(j * step))] for j in range(_TUBE_AXIS_MAX)]
        pts = []
        for p in params:
            q = fn.getPointAtParam(p, om2.MSpace.kWorld)
            pts.append((float(q.x), float(q.y), float(q.z)))
        return pts, (ln if ln > 1e-6 else 1.0)
    except Exception:
        return [], 1.0


def _tube_center(line):
    """チューブの形の中心（ワールド）。om2 で測る（Undo 履歴に何も積まない）。"""
    try:
        import maya.api.OpenMaya as om2
        shp = _tube_shape(line)
        if shp:
            sel = om2.MSelectionList()
            sel.add(shp)
            dp = sel.getDagPath(0)
            c = om2.MPoint(om2.MFnDagNode(dp).boundingBox.center) * dp.inclusiveMatrix()
            return (float(c.x), float(c.y), float(c.z))
    except Exception:
        pass
    return (0.0, 0.0, 0.0)


def _hidden_set_shader_params(s, line, ovr):
    """隠れ線パスのシェーダへ、このラインの現在値を渡す。ライン属性は Render Setup の
    オーバーライド後の値が読めるので、表示中のレイヤーの見え方がそのまま出る。"""
    def _get(node, attr, dv):
        try:
            return cmds.getAttr(node + "." + attr)
        except Exception:
            return dv
    s.setParameter("gOccDepth", ovr.tOccDepth)
    s.setParameter("gSceneDepth", ovr.tDepthScene)
    n, f = _edge_active_camera_near_far(ovr.destination)
    s.setParameter("gNear", float(n))
    s.setParameter("gFar", float(f))
    _edge_set_ortho_params(s)
    s.setParameter("hiddenTol", float(HIDDEN_DEPTH_TOL))
    s.setParameter("hiddenMode", int(_hidden_line_mode(line)))
    if bool(_get(line, LN_HID_SAME, 1)):
        col = _get(line, LN_COLOR, [(0.0, 0.0, 0.0)])[0]
    else:
        col = _get(line, LN_HID_COLOR, [(0.0, 0.0, 0.0)])[0]
    s.setParameter("lineColor", (float(col[0]), float(col[1]), float(col[2])))
    s.setParameter("hiddenAlpha", float(_get(line, LN_HID_ALPHA, _SCRN_HIDDEN_ALPHA_DEF)))
    s.setParameter("hiddenDashLen", float(_get(line, LN_HID_DASH, _SCRN_HIDDEN_DASH_DEF)))
    s.setParameter("hiddenDashFill", float(_get(line, LN_HID_FILL, _SCRN_HIDDEN_FILL_DEF)))
    if not _attr_exists(line, SCRN_TAG):
        # v0.1.50: チューブの破線用に中心線（ワールドの折れ線）と長さ・中心を渡す
        pts, length = _tube_axis(line)
        s.setParameter("tubeLength", float(length))
        s.setParameter("tubeCenter", _tube_center(line))
        n = len(pts)
        try:
            if n >= 2:
                pad = [0.0] * (_TUBE_AXIS_MAX - n)
                s.setArrayParameter("gAxX", [p[0] for p in pts] + pad, _TUBE_AXIS_MAX)
                s.setArrayParameter("gAxY", [p[1] for p in pts] + pad, _TUBE_AXIS_MAX)
                s.setArrayParameter("gAxZ", [p[2] for p in pts] + pad, _TUBE_AXIS_MAX)
            s.setParameter("gAxisCount", int(n if n >= 2 else 0))
        except Exception:
            s.setParameter("gAxisCount", 0)
        return
    shd = _fresnel_shader(line)
    if shd:
        params = [(SCRN_THICK, 3.0), (SCRN_DEPTH_BIAS, 0.0), ("stability", 0.0)]
        params += [(sh_attr, dv) for _ln, sh_attr, dv, _lo, _hi in BRUSH_ATTRS]
        for an, dv in params:
            s.setParameter(an, float(_get(shd, an, dv)))


def _og_override_needed():
    need_hidden = bool(_HIDDEN_GROUPS) and _hidden_scene_on() and not _vp2_is_opengl()
    return bool(_OVR_STATE["edge"] or _OVR_STATE["intersect"] or need_hidden)


def _og_override_sync():
    """いま必要な機能に合わせてオーバーライドを有効/無効にする。有効なら True。"""
    if _og_override_needed():
        return _edge_enable()
    if _edge_override is not None:
        _edge_disable()
    return False


_PANEL_GUARD = [None]   # QTimer: オーバーライドが有効な間、空に戻されたパネルへ入れ直す


def _guard_panels():
    """v0.1.54: Maya の updateRenderOverride（新しいシーンの直後や、Arnold などレンダラーのメニュー更新で
    遅れて走る）がパネルのオーバーライドを空に戻すことがある。戻されたままだとそのパネルの
    プレイブラストに隠れ線が出ないので、空のパネルにだけ入れ直す（他のオーバーライドのパネルは取らない）。"""
    if _edge_override is None:
        t = _PANEL_GUARD[0]
        if t is not None:
            try:
                t.stop()
            except Exception:
                pass
        return
    try:
        for p in cmds.getPanel(type="modelPanel") or []:
            if not cmds.modelEditor(p, q=True, rendererOverrideName=True):
                cmds.modelEditor(p, e=True, rendererOverrideName=EDGE_OVR_NAME)
    except Exception:
        pass


def _start_panel_guard():
    try:
        t = _PANEL_GUARD[0]
        if t is None:
            t = QtCore.QTimer()
            t.setInterval(1000)
            t.timeout.connect(_guard_panels)
            _PANEL_GUARD[0] = t
        if not t.isActive():
            t.start()
    except Exception:
        pass


def _stop_panel_guard():
    try:
        t = _PANEL_GUARD[0]
        if t is not None:
            t.stop()
    except Exception:
        pass


def _on_panel_focus():
    """v0.1.53: 後から作ったモデルパネル（ティアオフなど）にフォーカスが来たら、オーバーライドが
    有効なときはそのパネルにも入れる（以前は有効にした時点のパネルにしか入らず、新しいパネルで
    プレイブラストすると隠れ線が出なかった）。他のオーバーライドを使っているパネルは取らない。"""
    if _edge_override is None:
        return
    try:
        p = cmds.getPanel(withFocus=True)
        if p and cmds.getPanel(typeOf=p) == "modelPanel":
            if not cmds.modelEditor(p, q=True, rendererOverrideName=True):
                cmds.modelEditor(p, e=True, rendererOverrideName=EDGE_OVR_NAME)
    except Exception:
        pass


_HIDDEN_REFRESH_PENDING = [False]


def _on_hidden_dirty():
    """v0.1.53: Undo/Redo・名前の変更のあとに、隠れ線の対象一覧を読み直す。
    以前はツールの画面を通した操作でしか読み直さず、アウトライナーで名前を変える・紐づけを Undo する
    と、古い一覧のまま描いた（名前変更では隠れ線が消え、紐づけの Undo ではオーバーライドが残った）。"""
    _HIDDEN_REFRESH_PENDING[0] = False
    try:
        _hidden_refresh_cache()
        if _og_override_needed() != (_edge_override is not None):
            _og_override_sync()
    except Exception:
        pass


def _request_hidden_refresh():
    """描画中から呼ぶ用: 次のイベントループで 1 回だけ読み直す（描画中に Maya のコマンドで
    オーバーライドを付け外ししないため Qt のタイマーで後回しにする）。"""
    if _HIDDEN_REFRESH_PENDING[0]:
        return
    _HIDDEN_REFRESH_PENDING[0] = True
    try:
        QtCore.QTimer.singleShot(0, _on_hidden_dirty)
    except Exception:
        _HIDDEN_REFRESH_PENDING[0] = False


def _edge_enable():
    """画像空間エッジ検出のレンダーオーバーライドを現在のモデルパネルへ適用。
    成功で True。レンダー API が無い/失敗しても例外を投げず False を返す（本体は無事）。"""
    global _edge_override
    try:
        import maya.api.OpenMayaRender as omr
    except Exception as exc:
        cmds.warning("画像空間エッジ検出はこの環境で使えません（VP2/DirectX11 が必要）: {0}".format(exc))
        return False
    fresh = _edge_override is None
    if _edge_override is None:
        try:
            fx_path = _edge_fx_path()

            def _targets_sel():
                sel = om2.MSelectionList()
                for n in _EDGE_TARGETS:
                    try:
                        sel.add(n)
                    except Exception:
                        pass
                return sel

            class _SceneFull(omr.MSceneRender):
                """全シーンを色ターゲットへ（背景・他オブジェクトは通常表示のため）。"""
                def __init__(self, name, ovr):
                    omr.MSceneRender.__init__(self, name); self.ovr = ovr
                def targetOverrideList(self):
                    return [self.ovr.tColor, self.ovr.tDepthScene]
                def clearOperation(self):
                    # 背景をビューポートの背景色でクリア（黒くならないように）。
                    c = self.mClearOperation
                    try:
                        bg = cmds.displayRGBColor("background", q=True)
                        top = cmds.displayRGBColor("backgroundTop", q=True)
                        bot = cmds.displayRGBColor("backgroundBottom", q=True)
                        grad = bool(cmds.displayPref(q=True, displayGradient=True))
                    except Exception:
                        bg = [0.36, 0.36, 0.36]; top = bg; bot = bg; grad = False
                    # v0.1.48: 背景のアルファは 0（通常の VP2 と同じ）。PNG のプレイブラストで
                    # 背景が透明になり、線だけの素材を合成に使える。
                    try:
                        if grad:
                            c.setClearGradient(True)
                            c.setClearColor([float(top[0]), float(top[1]), float(top[2]), 0.0])
                            c.setClearColor2([float(bot[0]), float(bot[1]), float(bot[2]), 0.0])
                        else:
                            c.setClearGradient(False)
                            c.setClearColor([float(bg[0]), float(bg[1]), float(bg[2]), 0.0])
                    except Exception:
                        c.setClearGradient(False)
                        c.setClearColor([0.36, 0.36, 0.36, 0.0])
                    c.setMask(omr.MClearOperation.kClearAll)
                    return c

            class _SceneTargets(omr.MSceneRender):
                """対象メッシュだけを深度ターゲット tDepth へ（オブジェクト単位のエッジ源）。
                専用のスクラッチ色ターゲットへ描き ALL クリアするので、tDepth は必ず遠クリア＋
                対象のみの深度になり、全シーン色 tColor は汚さない（かすれ/ノイズ対策）。"""
                def __init__(self, name, ovr):
                    omr.MSceneRender.__init__(self, name); self.ovr = ovr
                def targetOverrideList(self):
                    return [self.ovr.tColorScratch, self.ovr.tDepth]
                def clearOperation(self):
                    c = self.mClearOperation
                    c.setClearGradient(False)
                    c.setClearColor([0.0, 0.0, 0.0, 0.0])
                    c.setClearDepth(1.0)
                    c.setMask(omr.MClearOperation.kClearAll)      # スクラッチ色+深度を確実にクリア
                    return c
                def objectSetOverride(self):
                    return _targets_sel()

            def _others_sel():
                sel = om2.MSelectionList()
                for n in _edge_other_transforms():
                    try:
                        sel.add(n)
                    except Exception:
                        pass
                return sel

            class _SceneOthers(omr.MSceneRender):
                """対象を除いた他メッシュだけを tDepthOther へ（交差線用）。"""
                def __init__(self, name, ovr):
                    omr.MSceneRender.__init__(self, name); self.ovr = ovr
                def targetOverrideList(self):
                    return [self.ovr.tColorScratch, self.ovr.tDepthOther]
                def clearOperation(self):
                    c = self.mClearOperation
                    c.setClearGradient(False)
                    c.setClearColor([0.0, 0.0, 0.0, 0.0])
                    c.setClearDepth(1.0)
                    c.setMask(omr.MClearOperation.kClearAll)
                    return c
                def objectSetOverride(self):
                    return _others_sel()

            # ---- v0.1.46: 隠れ線（紐づけ先の裏だけ）。v0.1.50: チューブ用シェーダも ----
            hidden_fx = _scrn_hidden_fx_path()
            tube_fx = _tube_hidden_fx_path()
            try:
                _FILTER_SHADED = omr.MSceneRender.kRenderShadedItems
            except Exception:
                _FILTER_SHADED = None
            try:
                _MODE_SHADED = omr.MSceneRender.kShaded
            except Exception:
                _MODE_SHADED = None

            def _sel_of(names):
                sel = om2.MSelectionList()
                for n in names:
                    try:
                        sel.add(n)
                    except Exception:
                        pass
                return sel

            class _SceneOcc(omr.MSceneRender):
                """紐づけ先の物だけを tOccDepth へ（深度だけを使う）。"""
                def __init__(self, name, ovr):
                    omr.MSceneRender.__init__(self, name); self.ovr = ovr; self.names = []
                def targetOverrideList(self):
                    return [self.ovr.tColorScratch, self.ovr.tOccDepth]
                def clearOperation(self):
                    c = self.mClearOperation
                    c.setClearGradient(False)
                    c.setClearColor([0.0, 0.0, 0.0, 0.0])
                    c.setClearDepth(1.0)
                    c.setMask(omr.MClearOperation.kClearAll)
                    return c
                def objectSetOverride(self):
                    return _sel_of(self.names)
                def renderFilterOverride(self):
                    return _FILTER_SHADED if _FILTER_SHADED is not None else \
                        omr.MSceneRender.renderFilterOverride(self)
                def displayModeOverride(self):
                    return _MODE_SHADED if _MODE_SHADED is not None else \
                        omr.MSceneRender.displayModeOverride(self)

            class _SceneHiddenLine(omr.MSceneRender):
                """1 本のラインの隠れ線を tColor へ重ねる（深度テストは使わず PS で判定）。"""
                def __init__(self, name, ovr):
                    omr.MSceneRender.__init__(self, name); self.ovr = ovr
                    self.line = None; self.shaderInst = None; self.fx = hidden_fx
                def targetOverrideList(self):
                    return [self.ovr.tColor, self.ovr.tDepthHidden]
                def clearOperation(self):
                    c = self.mClearOperation
                    c.setClearDepth(1.0)
                    c.setMask(omr.MClearOperation.kClearDepth)   # 色は消さない
                    return c
                def objectSetOverride(self):
                    return _sel_of([self.line] if self.line else [])
                def renderFilterOverride(self):
                    return _FILTER_SHADED if _FILTER_SHADED is not None else \
                        omr.MSceneRender.renderFilterOverride(self)
                def displayModeOverride(self):
                    return _MODE_SHADED if _MODE_SHADED is not None else \
                        omr.MSceneRender.displayModeOverride(self)
                def ensure_shader(self):
                    """隠れ線シェーダを用意する。失敗したらこのパスは組まない（素の線が
                    深度テスト無しで全面に重なるのを防ぐ）。"""
                    if self.shaderInst is None:
                        sm = omr.MRenderer.getShaderManager()
                        if sm is not None:
                            try:
                                self.shaderInst = sm.getEffectsFileShader(self.fx, "Main")
                            except Exception:
                                self.shaderInst = None
                    return self.shaderInst is not None
                def shaderOverride(self):
                    s = self.shaderInst
                    if s is not None and self.line:
                        try:
                            _hidden_set_shader_params(s, self.line, self.ovr)
                        except Exception as exc:
                            om2.MGlobal.displayWarning("[OG hidden] shader param: {0}".format(exc))
                    return s

            class _QuadEdge(omr.MQuadRender):
                def __init__(self, name, ovr):
                    omr.MQuadRender.__init__(self, name); self.ovr = ovr; self.shaderInst = None
                def targetOverrideList(self):
                    return None   # 画面へ出力
                def shader(self):
                    if self.shaderInst is None:
                        sm = omr.MRenderer.getShaderManager()
                        if sm is None:
                            return None
                        self.shaderInst = sm.getEffectsFileShader(fx_path, "Main")
                    s = self.shaderInst
                    if s is None:
                        return None
                    try:
                        s.setParameter("gColorTex", self.ovr.tColor)
                        s.setParameter("gDepthTex", self.ovr.tDepth)
                        s.setParameter("gDepthOtherTex", self.ovr.tDepthOther)
                        s.setParameter("gDepthSceneTex", self.ovr.tDepthScene)
                        w = max(1, self.ovr.w); h = max(1, self.ovr.h)
                        th = max(0.0, _EDGE_PARAMS["thickness"])
                        s.setParameter("gTexel", (th / float(w), th / float(h)))
                        s.setParameter("gThreshold", float(_EDGE_PARAMS["threshold"]))
                        col = _EDGE_PARAMS["color"]
                        s.setParameter("gLineColor", (col[0], col[1], col[2]))
                        # 深度線形化用の near/far は今描画中のパネルのカメラから毎フレーム取得
                        # (destination を渡さないと focus パネル固定→複数ビューポートで不整合)
                        n, f = _edge_active_camera_near_far(self.ovr.destination)
                        _EDGE_PARAMS["near"] = n; _EDGE_PARAMS["far"] = f
                        s.setParameter("gNear", float(n))
                        s.setParameter("gFar", float(f))
                        _edge_set_ortho_params(s)
                        s.setParameter("gDebug", int(_EDGE_PARAMS["debug"]))
                        s.setParameter("gIntersectMode",
                                       int(bool(_OVR_STATE["intersect"] and _EDGE_PARAMS["intersect_mode"])))
                        s.setParameter("gEdgeMode", int(bool(_OVR_STATE["edge"])))
                        s.setParameter("gIntersectThresh", float(_EDGE_PARAMS["intersect_threshold"]))
                    except Exception as exc:
                        om2.MGlobal.displayWarning("[OG edge] shader param: {0}".format(exc))
                    return s

            class _EdgeOverride(omr.MRenderOverride):
                def __init__(self, name):
                    omr.MRenderOverride.__init__(self, name)
                    self.w = 0; self.h = 0
                    self.destination = None    # 現在描画中のパネル名（shader() が使う）
                    self.tColor = None; self.tColorScratch = None
                    self.tDepthScene = None; self.tDepth = None
                    self.tDepthOther = None    # v0.1.36: 交差線用（対象を除いた他メッシュ深度）
                    self._tmgr = omr.MRenderer.getRenderTargetManager()
                    self._cDesc = omr.MRenderTargetDescription(
                        "OG_edgeColor", 256, 256, 1, omr.MRenderer.kR8G8B8A8_UNORM, 0, False)
                    self._csDesc = omr.MRenderTargetDescription(
                        "OG_edgeColorScratch", 256, 256, 1, omr.MRenderer.kR8G8B8A8_UNORM, 0, False)
                    self._dsDesc = omr.MRenderTargetDescription(
                        "OG_edgeDepthScene", 256, 256, 1, omr.MRenderer.kD24S8, 0, False)
                    self._dDesc = omr.MRenderTargetDescription(
                        "OG_edgeDepth", 256, 256, 1, omr.MRenderer.kD24S8, 0, False)
                    self._doDesc = omr.MRenderTargetDescription(
                        "OG_edgeDepthOther", 256, 256, 1, omr.MRenderer.kD24S8, 0, False)
                    # v0.1.46: 隠れ線用（紐づけ先だけの深度 / 隠れ線パスのダミー深度）
                    self.tOccDepth = None; self.tDepthHidden = None
                    self._ocDesc = omr.MRenderTargetDescription(
                        "OG_hidOccDepth", 256, 256, 1, omr.MRenderer.kD24S8, 0, False)
                    self._dhDesc = omr.MRenderTargetDescription(
                        "OG_hidDepth", 256, 256, 1, omr.MRenderer.kD24S8, 0, False)
                    self._occ_ops = []      # _SceneOcc（紐づけ先グループごと）
                    self._hid_ops = {}      # line -> _SceneHiddenLine
                    self._n_hid = 0
                    self._sceneFull = _SceneFull("og_edge_full", self)
                    self._sceneTgt = _SceneTargets("og_edge_tgt", self)
                    self._sceneOther = _SceneOthers("og_edge_other", self)
                    self._quad = _QuadEdge("og_edge_quad", self)
                    self._hud = omr.MHUDRender()
                    self._present = omr.MPresentTarget("og_edge_present")
                    self._ops = [self._sceneFull, self._quad, self._hud, self._present]
                    self._it = 0
                def supportedDrawAPIs(self):
                    return omr.MRenderer.kAllDevices
                def setup(self, destination):
                    # shader() から見えるように保持（複数ビューポートで対象パネルの near/far を使う）
                    self.destination = destination or ""
                    # まずは HiDPI 安全経路: destination の Qt ウィジェット実ピクセル
                    # サイズを直接測る（outputTargetSize() は Windows の HiDPI で論理
                    # サイズを返し、ターゲットが実バックバッファの半分になる→拡大ボケ
                    # によるかすれの主因になっていた）。取得不可なら旧 API へフォールバック。
                    # v0.1.46: プレイブラスト（パネルと違う解像度への出力）では出力解像度で描く。
                    # パネルの大きさで描いて縮めると線が細く/太くなる（480x300 の書き出しで
                    # 1321x764 で描いて縮小→線が 1/3 に細っていた）。
                    lw = lh = 0; dpr = 1.0
                    try:
                        ptr = omui.MQtUtil.findControl(destination) if destination else None
                        if ptr:
                            widget = wrapInstance(int(ptr), QtWidgets.QWidget)
                            try:
                                dpr = float(widget.devicePixelRatioF())
                            except AttributeError:
                                dpr = float(widget.devicePixelRatio())
                            lw, lh = int(widget.width()), int(widget.height())
                    except Exception:
                        lw = lh = 0
                    ow = oh = 0
                    try:
                        tgt = omr.MRenderer.outputTargetSize()
                        ow, oh = int(tgt[0]), int(tgt[1])
                    except Exception:
                        ow = oh = 0
                    pw = int(max(1, round(lw * dpr))); ph = int(max(1, round(lh * dpr)))
                    if ow > 0 and oh > 0:
                        is_panel = lw > 0 and (((ow, oh) == (lw, lh)) or
                                               (abs(ow - pw) <= 1 and abs(oh - ph) <= 1))
                        w, h = (pw, ph) if is_panel else (ow, oh)
                    elif lw > 0:
                        w, h = pw, ph
                    else:
                        w, h = 1280, 720
                    self.w, self.h = w, h
                    # 変化時に一度だけログ（HiDPI/解像度の切り分け用）
                    global _edge_size_logged
                    if (self.w, self.h) != _edge_size_logged:
                        _edge_size_logged = (self.w, self.h)
                        try:
                            om2.MGlobal.displayInfo(
                                "[OG edge] render target = {0}x{1} (panel='{2}')"
                                .format(self.w, self.h, destination or ""))
                        except Exception:
                            pass
                    for desc in (self._cDesc, self._csDesc, self._dsDesc, self._dDesc, self._doDesc,
                                 self._ocDesc, self._dhDesc):
                        desc.setWidth(self.w); desc.setHeight(self.h)
                    if self.tOccDepth is None:
                        self.tOccDepth = self._tmgr.acquireRenderTarget(self._ocDesc)
                    else:
                        self.tOccDepth.updateDescription(self._ocDesc)
                    if self.tDepthHidden is None:
                        self.tDepthHidden = self._tmgr.acquireRenderTarget(self._dhDesc)
                    else:
                        self.tDepthHidden.updateDescription(self._dhDesc)
                    if self.tColor is None:
                        self.tColor = self._tmgr.acquireRenderTarget(self._cDesc)
                    else:
                        self.tColor.updateDescription(self._cDesc)
                    if self.tColorScratch is None:
                        self.tColorScratch = self._tmgr.acquireRenderTarget(self._csDesc)
                    else:
                        self.tColorScratch.updateDescription(self._csDesc)
                    if self.tDepthScene is None:
                        self.tDepthScene = self._tmgr.acquireRenderTarget(self._dsDesc)
                    else:
                        self.tDepthScene.updateDescription(self._dsDesc)
                    if self.tDepth is None:
                        self.tDepth = self._tmgr.acquireRenderTarget(self._dDesc)
                    else:
                        self.tDepth.updateDescription(self._dDesc)
                    if self.tDepthOther is None:
                        self.tDepthOther = self._tmgr.acquireRenderTarget(self._doDesc)
                    else:
                        self.tDepthOther.updateDescription(self._doDesc)
                    self._ops = self._build_ops()
                def _build_ops(self):
                    """このフレームで使う機能だけのパス列（交差線/旧エッジ/隠れ線）。"""
                    ops = [self._sceneFull]
                    if _OVR_STATE["edge"] or _OVR_STATE["intersect"]:
                        ops.append(self._sceneTgt)
                        if _OVR_STATE["intersect"]:
                            ops.append(self._sceneOther)
                    groups = []
                    try:
                        if not _vp2_is_opengl():
                            groups = _hidden_frame_groups()
                    except Exception:
                        groups = []
                    for gi, (occ, lines) in enumerate(groups):
                        hops = []
                        for ln in lines:
                            hop = self._hid_ops.get(ln)
                            if hop is None:
                                self._n_hid += 1
                                hop = _SceneHiddenLine("og_hid_line_{0}".format(self._n_hid), self)
                                hop.line = ln
                                if not _attr_exists(ln, SCRN_TAG):
                                    hop.fx = tube_fx    # エッジ線・クリース線（チューブ）
                                self._hid_ops[ln] = hop
                            if hop.ensure_shader():
                                hops.append(hop)
                            elif not getattr(hop, "_warned", False):
                                hop._warned = True
                                om2.MGlobal.displayWarning(
                                    "[OG hidden] 隠れ線シェーダを読み込めません: {0}".format(hop.fx))
                        if not hops:
                            continue
                        while len(self._occ_ops) <= gi:
                            self._occ_ops.append(
                                _SceneOcc("og_hid_occ_{0}".format(len(self._occ_ops)), self))
                        op = self._occ_ops[gi]
                        op.names = list(occ)
                        ops.append(op)
                        ops.extend(hops)
                    ops.extend([self._quad, self._hud, self._present])
                    return ops
                def cleanup(self):
                    pass
                def startOperationIterator(self):
                    self._it = 0; return True
                def renderOperation(self):
                    return self._ops[self._it]
                def nextRenderOperation(self):
                    self._it += 1; return self._it < len(self._ops)
                def release(self):
                    for a in ("tColor", "tColorScratch", "tDepthScene", "tDepth", "tDepthOther",
                              "tOccDepth", "tDepthHidden"):
                        t = getattr(self, a, None)
                        if t is not None:
                            try:
                                self._tmgr.releaseRenderTarget(t)
                            except Exception:
                                pass
                            setattr(self, a, None)
                    sm = omr.MRenderer.getShaderManager()
                    for hop in list(self._hid_ops.values()):
                        if hop.shaderInst is not None and sm is not None:
                            try:
                                sm.releaseShader(hop.shaderInst)
                            except Exception:
                                pass
                        hop.shaderInst = None

            # 古いモジュール（再読込前）が同名で登録したままなら外してから登録する
            try:
                stale = omr.MRenderer.findRenderOverride(EDGE_OVR_NAME)
                if stale is not None:
                    omr.MRenderer.deregisterOverride(stale)
            except Exception:
                pass
            _edge_override = _EdgeOverride(EDGE_OVR_NAME)
            omr.MRenderer.registerOverride(_edge_override)
        except Exception as exc:
            _edge_override = None
            cmds.warning("画像空間エッジ検出の初期化に失敗: {0}".format(exc))
            return False
    # v0.1.46: 全モデルパネルへ適用（隠れ線はプレイブラストでも必要。どのパネルで
    # プレイブラストしても同じ絵になるように、フォーカス中のパネルだけにしない）
    panels = cmds.getPanel(type="modelPanel") or []
    if not panels:
        cmds.warning("モデルパネルが見つかりません"); return False
    ok = False
    changed = fresh
    for panel in panels:
        try:
            cur = cmds.modelEditor(panel, q=True, rendererOverrideName=True)
            # v0.1.53: 他のオーバーライド（Arnold のビューポート描画など）を使っているパネルは取らない
            # （以前は上書きし、外すときも空に戻すだけで元のオーバーライドが失われた）
            if cur and cur != EDGE_OVR_NAME:
                continue
            # 登録し直した直後は、名前が同じでもパネルへ入れ直す。シーンを開くとパネル設定が
            # 「このオーバーライドを使う」で復元されるが、その時点で未登録だと既定描画に
            # 戻ったままになり、後から登録しても反映されない（v0.1.46 で確認）。
            if fresh and cur == EDGE_OVR_NAME:
                cmds.modelEditor(panel, e=True, rendererOverrideName="")
            if fresh or cur != EDGE_OVR_NAME:
                cmds.modelEditor(panel, e=True, rendererOverrideName=EDGE_OVR_NAME)
                changed = True
            ok = True
        except Exception as exc:
            cmds.warning("画像空間エッジ検出の適用に失敗（{0}）: {1}".format(panel, exc))
    _start_panel_guard()    # v0.1.54: Maya が後から空に戻したパネルへ入れ直す
    # v0.1.54: 何も変えていなければ描画を回さない（ウィンドウを閉じる処理などから何度も呼ばれ、
    # そのたびに描画を回すと、その間に破棄されたウィンドウへ後から触って落ちることがあった）
    if changed:
        try:
            cmds.refresh()
        except Exception:
            pass
    return ok


def _edge_disable():
    """画像空間エッジ検出を全モデルパネルから解除し、オーバーライドを破棄。"""
    global _edge_override
    _stop_panel_guard()
    try:
        import maya.api.OpenMayaRender as omr
    except Exception:
        omr = None
    for panel in (cmds.getPanel(type="modelPanel") or []):
        try:
            if cmds.modelEditor(panel, q=True, rendererOverrideName=True) == EDGE_OVR_NAME:
                cmds.modelEditor(panel, e=True, rendererOverrideName="")
        except Exception:
            pass
    if _edge_override is not None:
        if omr is not None:
            try:
                omr.MRenderer.deregisterOverride(_edge_override)
            except Exception:
                pass
        try:
            _edge_override.release()
        except Exception:
            pass
    _edge_override = None
    try:
        cmds.refresh()
    except Exception:
        pass


def show():
    """ツールウィンドウを起動。既存ウィンドウがあれば閉じてから開く（重複起動を防止）。"""
    global _toon_win
    # スクリプト再実行で global がリセットされても残っている同名ウィンドウを全て掃除
    # （v0.1.54: 先に集めてから閉じる。モジュールグローバルの参照も同じ一覧で閉じる）
    wins = _find_tool_windows((WINDOW_OBJ,))
    if _qt_valid(_toon_win) and _toon_win not in wins:
        wins.insert(0, _toon_win)
    _close_and_delete(wins)
    _toon_win = ToonOutlineUI()
    _toon_win.show()
    _ensure_scene_jobs()   # v0.1.46: シーンを開き直したら隠れ線の紐づけを読み直す
    return _toon_win


def boot():
    """v0.1.48: Maya 起動時（インストーラが userSetup.py に書き足した行から）呼ばれる。
    UI は出さず、シーンを開いたら隠れ線の紐づけを読み直してオーバーライドを有効にする
    準備だけ行う（ツールを一度も開かずにレイヤー切替→プレイブラストしても隠れ線が出る）。"""
    try:
        if cmds.about(batch=True):
            return
    except Exception:
        return
    _ensure_scene_jobs()
    _on_scene_changed()


# v0.1.52: モジュールを読み込み直したとき、古いモジュールが張った曲率ジョブを消して
# このモジュールのジョブに張り替える（残すと同じ変更で古い方も計算し、回数が増えていく）。
try:
    if _kill_curv_jobs_everywhere():
        _ensure_all_curv_jobs()
except Exception:
    pass


if __name__ == "__main__":
    show()
