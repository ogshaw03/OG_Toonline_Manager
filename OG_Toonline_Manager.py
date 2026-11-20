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
except ImportError:
    from PySide2 import QtWidgets, QtCore, QtGui
    from shiboken2 import wrapInstance

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
EDGE_TAG    = "isToonEdgeLine"        # エッジ由来チューブラインの識別タグ
CREASE_TAG  = "isToonCreaseLine"      # v0.1.24: クリース自動生成ラインの識別タグ（EDGE_TAG との並行指標）
CREASES_GROUP_NAME = "__Creases__"    # v0.1.24: クリース線をまとめる専用グループ名（Outline_grp 直下）
CREASES_GROUP_DISPLAY = "クリース"    # v0.1.26: ツリー表示名（ノード名は内部で __Creases__ のまま）

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
_SCRN_ZBIAS = 0.0015                 # v0.1.14: NDC 単位に変更（distance-invariant clip-space Z offset）
SCRN_BRUSH_ROUGH = "brushRoughness"  # v0.1.14 ペンシル質感: 縁のカサつき(ランダム discard)量 (0-1)
SCRN_BRUSH_WAVE  = "brushWaviness"   # v0.1.14 ペンシル質感: 太さの長手ゆらぎ量 (0-1)
SCRN_BRUSH_SCALE = "brushNoiseScale" # v0.1.14 ペンシル質感: ノイズ周期（画面px換算・大=細かい）
_SCRN_BRUSH_ROUGH_DEF = 0.0          # 既定値（0=クリーン、OFF）
_SCRN_BRUSH_WAVE_DEF  = 0.0
_SCRN_BRUSH_SCALE_DEF = 20.0
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
__version__ = "0.1.27"

# 「GitHub から更新」の取得元（開発ブランチ。安定運用に移す際は main へ）
_GITHUB_OWNER  = "ogshaw03"
_GITHUB_REPO   = "OG_Toonline_Manager"
_GITHUB_BRANCH = "claude/repository-handoff-review-k54kwk"
_PACKAGE       = "OG_Toonline_Manager"   # このファイル自身のモジュール名（installer の _MODULE と一致）
_INSTALLER_FILE = "OG_Toonline_Manager_install.py"   # 更新時に再取得するインストーラのファイル名
_GITHUB_API      = "https://api.github.com/repos/{0}/{1}".format(_GITHUB_OWNER, _GITHUB_REPO)
_GITHUB_RAW_BASE = "https://raw.githubusercontent.com/{0}/{1}".format(_GITHUB_OWNER, _GITHUB_REPO)

_CURV_CACHE = {}   # line名 -> 正規化曲率リスト（scriptJob 用・モジュールレベル）
_CURV_JOBS  = {}   # line名 -> [scriptJob id, ...]
_TAPER_CACHE = {}  # line名 -> 各頂点の長手方向パラメータ t(0..1)（エッジライン用）
_OCC_ENABLED = False  # 隠蔽検知ハル（カメラ依存）の有効フラグ（UIチェックで切替・モジュール共有）


def _maya_main():
    return wrapInstance(int(omui.MQtUtil.mainWindow()), QtWidgets.QWidget)


def _short(name):
    return name.split("|")[-1] if name else name


def _ensure_shader(color):
    """全アウトライン共有のフラットマテリアル（基本のブラック）を確保。"""
    if not cmds.objExists(SHADER):
        cmds.shadingNode("surfaceShader", asShader=True, name=SHADER)
        cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=SG)
        cmds.connectAttr(SHADER + ".outColor", SG + ".surfaceShader", f=True)
        cmds.setAttr(SHADER + ".outColor", color[0], color[1], color[2], type="double3")
    return SHADER, SG


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


_FRES_FX_PATH_CACHE = None


def _fresnel_fx_path():
    """フレネル用 HLSL(.fx) をユーザー領域へ 1 セッション 1 回だけ書き出し、以降はキャッシュ。"""
    global _FRES_FX_PATH_CACHE
    if _FRES_FX_PATH_CACHE and os.path.exists(_FRES_FX_PATH_CACHE) and \
       os.path.getsize(_FRES_FX_PATH_CACHE) == len(_FRES_FX_HLSL):
        return _FRES_FX_PATH_CACHE
    d = os.path.join(cmds.internalVar(userAppDir=True), "OG_Toonline_Manager")
    try:
        if not os.path.isdir(d):
            os.makedirs(d)
    except Exception:
        d = cmds.internalVar(userTmpDir=True)
    path = os.path.join(d, "OG_ToonFresnel.fx").replace("\\", "/")
    try:
        if os.path.exists(path) and os.path.getsize(path) == len(_FRES_FX_HLSL):
            _FRES_FX_PATH_CACHE = path
            return path
    except Exception:
        pass
    # 書き込み(fsync で確実に flush)
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(_FRES_FX_HLSL)
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
    """フレネル輪郭シェーダ（VP2 ハードウェア = dx11Shader + 自前 HLSL）を shape に割り当てる。
    カメラから見て寝た面（facingRatio が小さい縁＝シルエット/凹み）だけ不透明な線色、他は透明。
    VP2/Hardware 2.0 バッチで反映・カメラ依存・scriptJob 不要。
    太さ＝dx11Shader の threshold uniform（_ensure_thickness_chain が駆動）。
    ※ samplerInfo.facingRatio は VP2 で評価されないため、ハードウェアシェーダで計算する。"""
    try:
        if not cmds.pluginInfo("dx11Shader", q=True, loaded=True):
            cmds.loadPlugin("dx11Shader", quiet=True)
    except Exception:
        cmds.warning("dx11Shader プラグインを読み込めません。VP2 を DirectX11 に設定してください。")
        return None, None
    base = "toonFresnel_" + _short(line)
    fx = _fresnel_fx_path()
    shd = cmds.shadingNode("dx11Shader", asShader=True, name=base + "_DX11")
    sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=base + "_SG")
    try:
        cmds.connectAttr(shd + ".outColor", sg + ".surfaceShader", f=True)
    except Exception:
        pass
    try:
        cmds.setAttr(shd + ".shader", fx, type="string")   # .fx をロード → uniform が attr 化
    except Exception:
        cmds.warning("フレネル用シェーダの読み込みに失敗しました（VP2/DirectX11 をご確認ください）")
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
    """シーン内の全スクリーン輪郭ライン（SCRN_TAG）を返す。"""
    out = []
    if not cmds.objExists(ROOT):
        return out
    for t in cmds.listRelatives(ROOT, allDescendents=True, type="transform", f=True) or []:
        if cmds.attributeQuery(SCRN_TAG, node=t, exists=True):
            out.append(t)
    return out


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
    pairs = ((SCRN_BRUSH_ROUGH, _SCRN_BRUSH_ROUGH_DEF),
             (SCRN_BRUSH_WAVE,  _SCRN_BRUSH_WAVE_DEF),
             (SCRN_BRUSH_SCALE, _SCRN_BRUSH_SCALE_DEF))
    for line in _all_screen_lines():
        shd = _fresnel_shader(line)
        if not shd: continue
        for an, v in pairs:
            if cmds.attributeQuery(an, node=shd, exists=True):
                try: cmds.setAttr(shd + "." + an, v)
                except Exception: pass
    try: cmds.refresh()
    except Exception: pass


_SCRN_FX_HLSL = """// OG Toonline Manager - Geometry-shader silhouette outline (dx11Shader / HLSL5)
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

struct APPDATA {
    float3 Position : POSITION;
    float3 Normal   : NORMAL;
    float4 Color    : COLOR0;
};
struct V2G {
    float4 CPos  : SV_Position;  // 使わないが rasterizer 要件
    float3 VSPos : TEXCOORD0;    // ビュー空間位置
    float3 VSNrm : TEXCOORD1;    // ビュー空間法線
    float  ColR  : TEXCOORD2;    // 曲率倍率(COLOR0.r)
};
struct G2P {
    float4 HPos : SV_Position;
    float2 UV   : TEXCOORD0;     // v0.1.14: U=長手(0-1), V=太さ方向(-1..+1)
};

// 1D ハッシュ (iq/Hugo Elias 系)
float hash12(float2 p)
{
    float3 p3 = frac(float3(p.xyx) * 0.1031f);
    p3 += dot(p3, p3.yzx + 33.33f);
    return frac((p3.x + p3.y) * p3.z);
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
    float w = i.Color.r;
    o.ColR = (w < 0.001f) ? 1.0f : w;
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
                   out float3 vspos, out float3 vsnrm)
{
    float t = fa / (fa - fb);
    vspos = lerp(a.VSPos, b.VSPos, t);
    vsnrm = normalize(lerp(a.VSNrm, b.VSNrm, t));
    float4 vp4 = float4(vspos, 1.0f);
    cpos = mul(vp4, gProj);
    cpos.z -= depthBias * cpos.w;   // 距離不変: NDC-Z を一定量 camera 側へ
    colR = lerp(a.ColR, b.ColR, t);
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

[maxvertexcount(4)]
void GShader(triangle V2G tri[3], inout TriangleStream<G2P> stream)
{
    float f0 = facing_dot(tri[0].VSPos, tri[0].VSNrm);
    float f1 = facing_dot(tri[1].VSPos, tri[1].VSNrm);
    float f2 = facing_dot(tri[2].VSPos, tri[2].VSNrm);

    // どのエッジがシルエットを横切るか(符号混在)
    bool c01 = (f0 * f1) < 0.0f;
    bool c12 = (f1 * f2) < 0.0f;
    bool c20 = (f2 * f0) < 0.0f;
    int ncross = (c01 ? 1 : 0) + (c12 ? 1 : 0) + (c20 ? 1 : 0);
    if (ncross != 2) return;  // 全頂点同符号 → シルエット外

    // 横断する 2 エッジ上で facing=0 となる点を 2 つ求める。VSPos/VSNrm も返す（perp 用）。
    float4 p1, p2;
    float cr1, cr2;
    float3 vsp1, vsp2, vsn1, vsn2;
    if (c01 && c12) {
        edge_crossing(tri[0], tri[1], f0, f1, p1, cr1, vsp1, vsn1);
        edge_crossing(tri[1], tri[2], f1, f2, p2, cr2, vsp2, vsn2);
    } else if (c12 && c20) {
        edge_crossing(tri[1], tri[2], f1, f2, p1, cr1, vsp1, vsn1);
        edge_crossing(tri[2], tri[0], f2, f0, p2, cr2, vsp2, vsn2);
    } else {
        edge_crossing(tri[0], tri[1], f0, f1, p1, cr1, vsp1, vsn1);
        edge_crossing(tri[2], tri[0], f2, f0, p2, cr2, vsp2, vsn2);
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

    // v0.1.15: 背面法 (inverted hull) と等価な均一太さ。
    //   - 距離 attenuation なし（頂点ごとの w で太さがばらつく主因だった）
    //   - 完全に外向きの quad（内側 0、外側 thickness）: 1 本の線＝ thickness ピクセル
    //   - 線幅 = thickness * cr * wav ピクセル（cr=曲率、wav=ブラシゆらぎ）
    // perp は NDC 単位ベクトル、px = 2/screen。off を w 乗算後に除算すると
    // ピクセル距離が perp * thickness とちょうど一致する（長さ厳密に = thickness px）。
    float2 off1 = perp1 * (thickness * cr1 * wav1) * px;
    float2 off2 = perp2 * (thickness * cr2 * wav2) * px;

    // triangle strip で quad を出力。UV: U=長手(0..1), V=太さ方向(0 内側=silhouette..1 外側)
    G2P v;
    v.HPos = float4(ndc1         * w1, p1.z, w1); v.UV = float2(0.0f, 0.0f); stream.Append(v);
    v.HPos = float4((ndc1 + off1) * w1, p1.z, w1); v.UV = float2(0.0f, 1.0f); stream.Append(v);
    v.HPos = float4(ndc2         * w2, p2.z, w2); v.UV = float2(1.0f, 0.0f); stream.Append(v);
    v.HPos = float4((ndc2 + off2) * w2, p2.z, w2); v.UV = float2(1.0f, 1.0f); stream.Append(v);
}

float4 PShader(G2P i) : SV_Target
{
    // v0.1.15: ペンシル風縁のカサつき。
    // UV.y: 0 = シルエット側(内), 1 = 外側。両端ほど「縁」なので、|UV.y - 0.5|*2 で縁度。
    // 中央(0.5)は残し、両端ほどランダム discard して毛羽立たせる。
    if (brushRoughness > 0.001f) {
        float edge = abs(i.UV.y - 0.5f) * 2.0f;  // 0=中心, 1=縁
        float rough = brushRoughness * edge * edge;
        float h = hash12(i.HPos.xy * 0.37f + i.UV * 11.0f);
        if (h < rough) discard;
    }
    return float4(lineColor, 1.0f);
}

technique11 Main
{
    pass p0
    {
        SetVertexShader(CompileShader(vs_5_0, VShader()));
        SetGeometryShader(CompileShader(gs_5_0, GShader()));
        SetPixelShader(CompileShader(ps_5_0, PShader()));
    }
}
"""


_SCRN_FX_PATH_CACHE = None


def _screen_fx_path():
    """SCRN 用 HLSL(.fx) を 1 セッション 1 回だけ書き出してキャッシュ。dx11Shader との
    read/write 競合で 0 バイトになる事故を回避。"""
    global _SCRN_FX_PATH_CACHE
    if _SCRN_FX_PATH_CACHE and os.path.exists(_SCRN_FX_PATH_CACHE) and \
       os.path.getsize(_SCRN_FX_PATH_CACHE) == len(_SCRN_FX_HLSL):
        return _SCRN_FX_PATH_CACHE
    d = os.path.join(cmds.internalVar(userAppDir=True), "OG_Toonline_Manager")
    try:
        if not os.path.isdir(d):
            os.makedirs(d)
    except Exception:
        d = cmds.internalVar(userTmpDir=True)
    path = os.path.join(d, "OG_ToonScreen.fx").replace("\\", "/")
    try:
        if os.path.exists(path) and os.path.getsize(path) == len(_SCRN_FX_HLSL):
            _SCRN_FX_PATH_CACHE = path
            return path
    except Exception:
        pass
    import tempfile
    try:
        fd, tmp = tempfile.mkstemp(suffix=".fx", prefix="OG_ToonScreen_", dir=d)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(_SCRN_FX_HLSL)
            os.replace(tmp, path)
        except Exception:
            try: os.remove(tmp)
            except Exception: pass
            raise
    except Exception:
        try:
            with open(path, "w") as f:
                f.write(_SCRN_FX_HLSL)
        except Exception:
            pass
    _SCRN_FX_PATH_CACHE = path
    return path


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
    """スクリーン空間押し出し輪郭シェーダ（dx11Shader + 自前 HLSL）を shape に割り当てる。
    頂点シェーダでクリップ空間に一定ピクセル押し出し＋フロントカリングで均一太さの輪郭。
    隙間/浮きが出ず凸部でも細らない。太さ＝thickness uniform（ピクセル）。"""
    try:
        if not cmds.pluginInfo("dx11Shader", q=True, loaded=True):
            cmds.loadPlugin("dx11Shader", quiet=True)
    except Exception:
        cmds.warning("dx11Shader プラグインを読み込めません。VP2 を DirectX11 に設定してください。")
        return None, None
    base = "toonScreen_" + _short(line)
    fx = _screen_fx_path()
    shd = cmds.shadingNode("dx11Shader", asShader=True, name=base + "_DX11")
    sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=base + "_SG")
    try:
        cmds.connectAttr(shd + ".outColor", sg + ".surfaceShader", f=True)
    except Exception:
        pass
    try:
        cmds.setAttr(shd + ".shader", fx, type="string")
    except Exception:
        cmds.warning("スクリーン輪郭用シェーダの読み込みに失敗（VP2/DirectX11 をご確認ください）")
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
    """フレネルラインの dx11Shader ノード（threshold=太さ, lineColor=線色 の uniform を持つ）。"""
    if cmds.objExists(line) and cmds.attributeQuery(FRES_LINK, node=line, exists=True):
        c = cmds.listConnections(line + "." + FRES_LINK, s=True, d=False) or []
        c = [x for x in c if cmds.objExists(x) and cmds.nodeType(x) == "dx11Shader"]
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


def _line_curvature(line):
    curv = _CURV_CACHE.get(line)
    if curv is None:
        src = _line_src_shape(line)
        try:
            curv = _compute_curvature(src) if src else []
        except Exception:
            curv = []
        _CURV_CACHE[line] = curv
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
    f = _TAPER_CACHE.get(line)
    if f is not None:
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
    _TAPER_CACHE[line] = ts
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


def _set_outliner_color(node, rgb):
    try:
        cmds.setAttr(node + ".useOutlinerColor", 1)
        cmds.setAttr(node + ".outlinerColor", rgb[0], rgb[1], rgb[2], type="double3")
    except Exception:
        pass


def _lock_trs(node):
    for at in ("tx", "ty", "tz", "rx", "ry", "rz", "sx", "sy", "sz", "v"):
        try:
            cmds.setAttr(node + "." + at, lock=True, keyable=False, channelBox=False)
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
    _set_outliner_color(gc, COL_GLOBAL)
    _lock_trs(gc)
    return gc


def _ensure_group_ctrl(group):
    """グループコントローラー（黄緑・thicknessMult）を全体コントローラー配下に確保。"""
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
            try:
                src_sh = _line_src_shape(t)
            except Exception:
                continue
            if not src_sh or not cmds.objExists(src_sh):
                continue
            par = cmds.listRelatives(src_sh, parent=True, f=True) or []
            if par:
                srcs.add(par[0])
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


def _set_line_only_mode(on):
    """v0.1.26: 全アウトライン対象メッシュを useBackground に差し替え（holdout）/ 復帰。
    ON: 現在の SG をメッシュ transform の `_toonPrevSG` attr に退避 → holdout SG を割当
    OFF: _toonPrevSG から元の SG を取り出して復帰（attr が無い/SG が無い場合はスキップ）"""
    srcs = _source_mesh_transforms()
    if on:
        sg = _ensure_holdout_sg()
        for mesh_tr in srcs:
            shs = cmds.listRelatives(mesh_tr, shapes=True, type="mesh", ni=True, f=True) or []
            if not shs:
                continue
            cur = _current_mesh_sg(shs[0])
            if cur and cur != sg:
                if not cmds.attributeQuery(MESH_PREV_SG_ATTR, node=mesh_tr, exists=True):
                    try:
                        cmds.addAttr(mesh_tr, ln=MESH_PREV_SG_ATTR, dt="string")
                    except Exception:
                        pass
                try:
                    cmds.setAttr(mesh_tr + "." + MESH_PREV_SG_ATTR, cur, type="string")
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
            prev = None
            if cmds.attributeQuery(MESH_PREV_SG_ATTR, node=mesh_tr, exists=True):
                try:
                    prev = cmds.getAttr(mesh_tr + "." + MESH_PREV_SG_ATTR) or None
                except Exception:
                    prev = None
            if prev and cmds.objExists(prev):
                try:
                    cmds.sets(shs[0], e=True, forceElement=prev)
                except Exception:
                    pass
            # 復帰後は attr を消してよい（次回 ON 時に再記録）
            try:
                if cmds.attributeQuery(MESH_PREV_SG_ATTR, node=mesh_tr, exists=True):
                    cmds.deleteAttr(mesh_tr, at=MESH_PREV_SG_ATTR)
            except Exception:
                pass


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
        try:
            cmds.setAttr(mA + ".input2", 1.0)
        except Exception:
            pass
    # mB = mA * globalCtrl.thicknessMult → 総太さ（UI 表示値ベース）
    _connect(mA + ".output", mB + ".input1")
    _connect(gctrl + "." + GMULT, mB + ".input2")

    is_edge = cmds.attributeQuery(EDGE_TAG, node=line, exists=True)
    if is_edge:
        # エッジは UI 太さ×EDGE_THICK_SCALE を offset に流す（同じ数値でも細く見せる）
        mS = base + "_thkScale"
        if not cmds.objExists(mS):
            mS = cmds.createNode("multDoubleLinear", name=mS)
        _connect(mB + ".output", mS + ".input1")
        try:
            cmds.setAttr(mS + ".input2", EDGE_THICK_SCALE)
        except Exception:
            pass
        _connect(mS + ".output", target)
        # 円プロファイルの基準半径も太さに比例させ、細くするとチューブ全体が細る
        # （固定半径だと細くしても基準半径分の太さが残ってしまうため）。
        circ = _line_circle_node(line)
        if circ:
            mR = base + "_radScale"
            if not cmds.objExists(mR):
                mR = cmds.createNode("multDoubleLinear", name=mR)
            _connect(mB + ".output", mR + ".input1")
            try:
                cmds.setAttr(mR + ".input2", EDGE_BASE_SCALE)
            except Exception:
                pass
            _connect(mR + ".output", circ + ".radius")
    elif cmds.attributeQuery(FRES_TAG, node=line, exists=True):
        # フレネルは UI 太さ×FRES_SCALE を condition.secondTerm(facingRatio しきい値)へ。
        # しきい値が大きいほど寝た面まで線が出る＝太い線になる。
        mS = base + "_fresScale"
        if not cmds.objExists(mS):
            mS = cmds.createNode("multDoubleLinear", name=mS)
        _connect(mB + ".output", mS + ".input1")
        try:
            cmds.setAttr(mS + ".input2", FRES_SCALE)
        except Exception:
            pass
        _connect(mS + ".output", target)
        # 帯の画面px幅の上限 = UI太さ × SCRN_SCALE × globalCtrl.fresSens（感度）。
        # 感度=見た目の太さを変えずに「どこまで内側（寝た面）まで検出するか」を調整。
        shd_fres = target.rsplit(".", 1)[0]
        if cmds.attributeQuery(FRES_MAXW, node=shd_fres, exists=True):
            mW = base + "_fresMaxW"
            if not cmds.objExists(mW):
                mW = cmds.createNode("multDoubleLinear", name=mW)
            _connect(mB + ".output", mW + ".input1")
            try:
                cmds.setAttr(mW + ".input2", SCRN_SCALE)
            except Exception:
                pass
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
        try:
            cmds.setAttr(mS + ".input2", SCRN_SCALE)
        except Exception:
            pass
        _connect(mS + ".output", target)
    else:
        # hull: 重なりマスクがある場合は、マスクが覆う inflate 分だけ offset を上乗せして
        # 見える線幅（= mB.output = 設定太さ）を保つ。offset = mB.output * (1 + MASK_INFLATE_FRAC)。
        mBoost = base + "_thkMaskBoost"
        if _mask_of(line):
            if not cmds.objExists(mBoost):
                mBoost = cmds.createNode("multDoubleLinear", name=mBoost)
            _connect(mB + ".output", mBoost + ".input1")
            try:
                cmds.setAttr(mBoost + ".input2", 1.0 + MASK_INFLATE_FRAC)
            except Exception:
                pass
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
            try:
                cmds.setAttr(cnd + ".operation", 2)          # Greater Than
                cmds.setAttr(cnd + ".secondTerm", EDGE_VIS_EPS)
                cmds.setAttr(cnd + ".colorIfTrueR", 1)
                cmds.setAttr(cnd + ".colorIfFalseR", 0)
            except Exception:
                pass
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
    try:
        cmds.setAttr(defm + ".weightList[0].weights[0:{}]".format(n - 1), *w)
    except Exception:
        pass


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
    try:
        cmds.setAttr(defm + ".weightList[0].weights[0:{}]".format(n - 1), *weights)
    except Exception:
        pass


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
    cols = om2.MColorArray()
    verts = om2.MIntArray()
    for i in range(n):
        w = weights[i]
        cols.append(om2.MColor((w, w, w, 1.0)))
        verts.append(i)
    try:
        mfn.setVertexColors(cols, verts)
    except Exception:
        pass


def _ensure_curv_jobs(line):
    """controller の curvature / curvatureCap 変化で頂点ウェイトを再計算する scriptJob。
    （ノード削除時に自動 kill される attributeChange ジョブ）"""
    ctrl = _ctrl_of(line)
    if not ctrl:
        return
    old = _CURV_JOBS.get(line)
    if old:
        try:
            if all(cmds.scriptJob(exists=j) for j in old):
                return
        except Exception:
            pass
        for j in old:
            try:
                if cmds.scriptJob(exists=j):
                    cmds.scriptJob(kill=j, force=True)
            except Exception:
                pass
    jobs = []
    for at in (CTRL_CURV, CTRL_CAP, CTRL_CMIN, CTRL_TAPER):
        if not cmds.attributeQuery(at, node=ctrl, exists=True):
            continue
        try:
            jid = cmds.scriptJob(attributeChange=[ctrl + "." + at,
                                                  lambda ln=line: _update_line_weights(ln)])
            jobs.append(jid)
        except Exception:
            pass
    _CURV_JOBS[line] = jobs


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


class _RampWidget(QtWidgets.QWidget):
    """長手方向の太さプロファイルを編集するカーブ（ランプ）ウィジェット。
    左クリック=点の追加/ドラッグ、右クリック=点の削除。y は太さ倍率(0〜2、基準1)。"""
    valueChanged = QtCore.Signal()

    def __init__(self, parent=None):
        super(_RampWidget, self).__init__(parent)
        self.setMinimumHeight(90)
        self.setMinimumWidth(180)
        self._pts = [[0.0, 1.0], [1.0, 1.0]]
        self._drag = -1
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
        if i < 0:
            x, y = self._to_norm(px, py)
            self._pts.append([x, y]); self._pts.sort(key=lambda q: q[0])
            i = self._hit(*self._to_px(x, y))
        self._drag = i

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
        self.setMinimumWidth(380)
        # 最小でもリスト(min120)＋固定パネル(200)＋下部コントロールが収まる高さ。
        # リストが余白を吸収するので下に余分な余白は出ない。
        self.setMinimumHeight(880)   # v0.1.25: ランプ(90) + GroupBox 2つ + ツリー + パネル
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
        self._build()
        self.refresh_tree()
        self._update_panels()      # 初期は選択無し → ライン/グループパネル非表示
        self._set_mult_widgets()
        try:
            self._time_job = cmds.scriptJob(event=["timeChanged", self._on_time_changed],
                                            protected=False)
        except Exception:
            self._time_job = None

    def closeEvent(self, event):
        try:
            if self._time_job and cmds.scriptJob(exists=self._time_job):
                cmds.scriptJob(kill=self._time_job, force=True)
        except Exception:
            pass
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
            _edge_disable()          # 画像空間エッジ検出のオーバーライドを後始末
        except Exception:
            pass
        super(ToonOutlineUI, self).closeEvent(event)

    # ========== UI 構築 ==========
    def _slider_spin_row(self, label, smin, smax, sval, dec, rmin, rmax, step, rval,
                         on_slider, on_spin, on_reset, on_key=None, tip=""):
        """ラベル+スライダー+スピン+[キー]+[リセット]の1行を作り (layout, slider, spin) を返す。"""
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel(label))
        sld = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        sld.setRange(smin, smax); sld.setValue(sval)
        spn = QtWidgets.QDoubleSpinBox()
        spn.setDecimals(dec); spn.setRange(rmin, rmax); spn.setSingleStep(step); spn.setValue(rval)
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
        lay = QtWidgets.QVBoxLayout(self)

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
        lay.addLayout(hrow)

        # 全体倍率（常に最上部）
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
        gl.addLayout(arow)
        lay.addWidget(self.w_global)

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
        # v0.1.18: 選択エッジにチューブ状ラインを出すボタンを復活（内部実装 create_edge_line は残っていた）。
        b_edge = QtWidgets.QPushButton("選択エッジにライン")
        b_edge.setToolTip(
            "選択したポリゴンエッジに沿ってチューブ状のラインを 1 本追加する。\n"
            "polyToCurve + 円プロファイル extrude + nurbsToPoly。\n"
            "太さ/曲率起伏/末端細り/プロファイルで調整可能。\n"
            "※ 非分岐な連続エッジ列を想定（分岐ありはクリース線のように自動 chain 分割されません）。")
        b_edge.clicked.connect(self._on_create_edge_line_button)
        crow.addWidget(b_edge)
        b_grp = QtWidgets.QPushButton("新規グループ")
        b_grp.clicked.connect(self.new_group)
        crow.addWidget(b_grp)
        b_ref = QtWidgets.QPushButton("再取得")
        b_ref.clicked.connect(self.refresh_tree)
        crow.addWidget(b_ref)
        lay.addLayout(crow)

        # v0.1.25: タイトルは短く、構造だけで「新規」「ライブ」を分離。
        # ---- 新規作成時のオプション ----
        gb_gen = QtWidgets.QGroupBox("新規作成")
        gb_gen.setObjectName("OG_Toonline_GenBox")
        gb_gen.setMinimumHeight(300)   # v0.1.25: ランプが潰れないよう余裕を持たせる
        glay = QtWidgets.QVBoxLayout(gb_gen)
        glay.setContentsMargins(8, 10, 8, 6); glay.setSpacing(4)

        # フレネル生成チェック
        self.chk_scrn_fresnel = QtWidgets.QCheckBox("フレネル線も生成（滑らかな凹み用）")
        self.chk_scrn_fresnel.setChecked(True)
        glay.addWidget(self.chk_scrn_fresnel)

        # ---- クリース関連（QFrame ネスト撤去して layout を直に）----
        crease_row = QtWidgets.QHBoxLayout()
        self.chk_scrn_crease = QtWidgets.QCheckBox("クリース線も生成（硬い折り目）")
        self.chk_scrn_crease.setChecked(True)
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
        glay.addLayout(crease_row)

        # v0.1.26: 太さプロファイル 3 種（輪郭線 / クリース線 / エッジライン）を QTabWidget で切替。
        # ボタン行はランプ上に 1 行（ラベル + フラット + 既存ラインに適用）。
        prof_tab_row = QtWidgets.QHBoxLayout()
        prof_tab_row.addWidget(QtWidgets.QLabel("太さプロファイル"))
        prof_tab_row.addStretch(1)
        b_flat = QtWidgets.QPushButton(u"↺ フラット"); b_flat.setFixedHeight(22)
        b_flat.setToolTip("現在のタブのランプを (0,1)(1,1) フラットにリセット")
        b_flat.clicked.connect(self._reset_current_prof_ramp)
        prof_tab_row.addWidget(b_flat)
        self.btn_apply_ramp = QtWidgets.QPushButton("既存ラインに適用")
        self.btn_apply_ramp.setFixedHeight(22)
        self.btn_apply_ramp.setToolTip(
            "現在のタブのランプを既存ラインに適用。\n"
            "ツリーで選択あり → 選択ラインのみ\n"
            "選択なし → タブに対応する全ラインに適用")
        self.btn_apply_ramp.setStyleSheet(_PRIMARY_BTN_CSS)
        self.btn_apply_ramp.clicked.connect(self._apply_ramp_to_lines)
        prof_tab_row.addWidget(self.btn_apply_ramp)
        glay.addLayout(prof_tab_row)

        self.prof_tabs = QtWidgets.QTabWidget()
        self.prof_tabs.setObjectName("OG_Toonline_ProfTabs")

        # 輪郭線タブ: SCRN は長手方向を持たないため無効化（UI 表示のみ）
        tab_scrn = QtWidgets.QWidget(); tsl = QtWidgets.QVBoxLayout(tab_scrn)
        tsl.setContentsMargins(4, 4, 4, 4)
        self.ramp_scrn = _RampWidget()
        self.ramp_scrn.setMinimumHeight(90)
        self.ramp_scrn.setEnabled(False)
        tsl.addWidget(self.ramp_scrn)
        tsl.addWidget(QtWidgets.QLabel(
            "※ 輪郭線(SCRN)は GS シルエット検出のため長手方向が無く、\n"
            "   太さプロファイルは適用できません（曲率ランプで代替）。"))
        self.prof_tabs.addTab(tab_scrn, "輪郭線")

        # クリース線タブ
        tab_crease = QtWidgets.QWidget(); tcl = QtWidgets.QVBoxLayout(tab_crease)
        tcl.setContentsMargins(4, 4, 4, 4)
        self.ramp_crease = _RampWidget()
        self.ramp_crease.setMinimumHeight(90)
        self.ramp_crease.setToolTip("クリース線の太さ形状。左=点追加/移動、右=削除")
        tcl.addWidget(self.ramp_crease)
        self.prof_tabs.addTab(tab_crease, "クリース線")

        # エッジラインタブ
        tab_edge = QtWidgets.QWidget(); tel = QtWidgets.QVBoxLayout(tab_edge)
        tel.setContentsMargins(4, 4, 4, 4)
        self.ramp_edge = _RampWidget()
        self.ramp_edge.setMinimumHeight(90)
        self.ramp_edge.setToolTip("エッジラインの太さ形状。左=点追加/移動、右=削除")
        tel.addWidget(self.ramp_edge)
        self.prof_tabs.addTab(tab_edge, "エッジライン")

        # 既定で「クリース」タブ（よく使う）
        self.prof_tabs.setCurrentIndex(1)
        glay.addWidget(self.prof_tabs)

        # 旧 crease_ramp 名で参照する他箇所の互換のため alias を張る
        self.crease_ramp = self.ramp_crease

        def _crease_enabled(on):
            self.spn_crease_angle.setEnabled(on)
        self.chk_scrn_crease.toggled.connect(_crease_enabled)
        _crease_enabled(self.chk_scrn_crease.isChecked())

        lay.addWidget(gb_gen)

        # ---- 作成済みラインに常時反映される設定 ----
        gb_live = QtWidgets.QGroupBox("ライブ調整")
        gb_live.setObjectName("OG_Toonline_LiveBox")
        llay = QtWidgets.QVBoxLayout(gb_live)
        llay.setContentsMargins(8, 10, 8, 6); llay.setSpacing(4)

        # フレネル感度（既存全フレネル線に live で乗算）
        fs_row = QtWidgets.QHBoxLayout()
        fs_row.addWidget(QtWidgets.QLabel("フレネル感度"))
        self.spn_fres_sens = QtWidgets.QDoubleSpinBox()
        self.spn_fres_sens.setRange(0.0, 10.0)
        self.spn_fres_sens.setDecimals(2)
        self.spn_fres_sens.setSingleStep(0.1)
        self.spn_fres_sens.setValue(1.0)
        self.spn_fres_sens.setFixedWidth(80)
        self.spn_fres_sens.setToolTip(
            "フレネル感度（globalCtrl.fresSens）。見た目の太さは変えずに「どこまで内側\n"
            "（寝た面）まで検出するか」だけ調整する倍率。全フレネル線に効く。\n"
            "1.0 = 既定 / 0.5 = シルエット直近だけ / 2.0 = 深い谷まで")
        self.spn_fres_sens.valueChanged.connect(self._on_fres_sens)
        fs_row.addWidget(self.spn_fres_sens)
        fs_row.addStretch(1)
        llay.addLayout(fs_row)

        # 重なり深度（SCRN）
        srow = QtWidgets.QHBoxLayout()
        srow.addWidget(QtWidgets.QLabel("重なり深度(スクリーン)"))
        self.spn_scrn_bias = QtWidgets.QDoubleSpinBox()
        self.spn_scrn_bias.setRange(-0.5, 0.5); self.spn_scrn_bias.setDecimals(4)
        self.spn_scrn_bias.setSingleStep(0.0005); self.spn_scrn_bias.setValue(_SCRN_ZBIAS)
        self.spn_scrn_bias.setToolTip(
            "スクリーン輪郭の NDC-Z バイアス（v0.1.14 以降は clip-space Z オフセット）。\n"
            "0.0015 = NDC 深度を 0.15% だけ camera 側に引く。距離不変で動く：\n"
            "遠距離でも重なり線が消えない。符号を反転すると逆に。\n"
            "値を大きくし過ぎると他オブジェクトを貫通して黒く見える、\n"
            "小さすぎると近距離で z-fighting する。")
        self.spn_scrn_bias.valueChanged.connect(self._on_scrn_bias)
        srow.addWidget(self.spn_scrn_bias)
        srow.addStretch(1)
        llay.addLayout(srow)

        # ブラシ質感
        brow = QtWidgets.QHBoxLayout()
        brow.addWidget(QtWidgets.QLabel("ブラシ:カサつき"))
        self.spn_brush_rough = QtWidgets.QDoubleSpinBox()
        self.spn_brush_rough.setRange(0.0, 1.0); self.spn_brush_rough.setDecimals(2)
        self.spn_brush_rough.setSingleStep(0.05); self.spn_brush_rough.setValue(_SCRN_BRUSH_ROUGH_DEF)
        self.spn_brush_rough.setToolTip(
            "SCRN ラインの縁をランダムに欠けさせてペンシル風の毛羽立ちを出す。\n"
            "0=クリーン、1=強いカサつき。線の中心は残るので視認性は保たれる。")
        self.spn_brush_rough.valueChanged.connect(self._on_brush_changed)
        brow.addWidget(self.spn_brush_rough)
        brow.addWidget(QtWidgets.QLabel("ゆらぎ"))
        self.spn_brush_wave = QtWidgets.QDoubleSpinBox()
        self.spn_brush_wave.setRange(0.0, 1.0); self.spn_brush_wave.setDecimals(2)
        self.spn_brush_wave.setSingleStep(0.05); self.spn_brush_wave.setValue(_SCRN_BRUSH_WAVE_DEF)
        self.spn_brush_wave.setToolTip(
            "SCRN ラインの太さをノイズで長手方向に揺らす（鉛筆の筆圧ゆらぎ風）。\n"
            "0=均一、1=大きなゆらぎ。")
        self.spn_brush_wave.valueChanged.connect(self._on_brush_changed)
        brow.addWidget(self.spn_brush_wave)
        brow.addWidget(QtWidgets.QLabel("周期"))
        self.spn_brush_scale = QtWidgets.QDoubleSpinBox()
        self.spn_brush_scale.setRange(1.0, 100.0); self.spn_brush_scale.setDecimals(1)
        self.spn_brush_scale.setSingleStep(1.0); self.spn_brush_scale.setValue(_SCRN_BRUSH_SCALE_DEF)
        self.spn_brush_scale.setToolTip(
            "ノイズの周期（画面 px 換算）。大きいほど細かくザラッ、小さいほど大きなうねり。")
        self.spn_brush_scale.valueChanged.connect(self._on_brush_changed)
        brow.addWidget(self.spn_brush_scale)
        brow.addStretch(1)
        llay.addLayout(brow)

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
        llay.addWidget(self.chk_line_only)

        lay.addWidget(gb_live)

        # 対象グループ（生成先）
        crow2 = QtWidgets.QHBoxLayout()
        crow2.addWidget(QtWidgets.QLabel("対象グループ"))
        self.group_combo = QtWidgets.QComboBox()
        self.group_combo.setMinimumWidth(110)
        crow2.addWidget(self.group_combo, 1)
        lay.addLayout(crow2)

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
        # リストは余白(ウィンドウの伸縮分)を吸収して広がる。スライダーパネル側を
        # 固定高さにすることで、選択でパネルが切り替わってもリストの大きさは一定。
        self.tree.setMinimumHeight(120)
        lay.addWidget(self.tree, 1)
        self.lbl_dd = QtWidgets.QLabel("※ ラインをグループへドラッグ&ドロップで移動。ダブルクリックで名前変更")
        self.lbl_dd.setStyleSheet("color:#888;")
        lay.addWidget(self.lbl_dd)

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

        # ライン用/グループ用パネルを固定高さの領域に収める。どちらを表示しても
        # 領域の高さは一定なので、リスト(上)の大きさが選択で変化しない。
        self.w_panels = QtWidgets.QWidget()
        pvl = QtWidgets.QVBoxLayout(self.w_panels); pvl.setContentsMargins(0, 0, 0, 0)
        pvl.setSpacing(0)
        pvl.addWidget(self.w_line)
        pvl.addWidget(self.w_group)
        pvl.addStretch(1)
        self.w_panels.setFixedHeight(230)   # 太さ/曲率/曲率上限/曲率下限+プロファイルが収まる高さ
        lay.addWidget(self.w_panels)

        self.lbl_hint = QtWidgets.QLabel("※ 値はツリーで選択したライン/グループに適用されます")
        self.lbl_hint.setStyleSheet("color:#888;")
        lay.addWidget(self.lbl_hint)

        # 選択物のすべての値を初期値に戻す（実行前に確認ダイアログ）
        self.btn_reset_all = QtWidgets.QPushButton("選択をすべてリセット")
        self.btn_reset_all.setToolTip("選択したライン/グループの全パラメータを初期値に戻します（確認あり）")
        self.btn_reset_all.clicked.connect(self.reset_selected)
        self.btn_reset_all.setStyleSheet(_PRIMARY_BTN_CSS)
        lay.addWidget(self.btn_reset_all)

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
        lay.addLayout(clrow)

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
        lay.addWidget(self.lbl_del)

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

    def _current_group(self):
        name = self.group_combo.currentText().strip() if self.group_combo.count() else ""
        return self._ensure_group(name or DEFAULT_GROUP)

    def _tuck_handle(self, h):
        """ハンドルをビューポート非表示にし、アウトライナーからは設定に応じて隠す。"""
        if not (h and cmds.objExists(h)):
            return
        hide = 1 if getattr(self, "chk_hide_handles", None) is None or self.chk_hide_handles.isChecked() else 0
        for fn in (
            lambda: cmds.setAttr(h + ".hiddenInOutliner", hide),
            lambda: cmds.setAttr(h + ".visibility", 0),
        ):
            try:
                fn()
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
        try:
            cmds.setAttr(sh + ".overrideEnabled", 1 if lock else 0)
            if lock:
                cmds.setAttr(sh + ".overrideDisplayType", 2)  # 2 = reference
            else:
                cmds.setAttr(sh + ".overrideDisplayType", 0)  # 0 = normal
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
        """カメラ追従の再計算が必要か（隠蔽検知ON、または隙間埋めオブジェクトが存在）。"""
        return _OCC_ENABLED or bool(_all_gapfills())

    def _refresh_occlusion(self):
        """カメラ依存の頂点ウェイトを再計算（隠蔽検知ハル＋隙間埋め）。undo は汚さない。
        再計算中（_occ_busy）は重畳を避けてスキップ＝重いメッシュでもビューポートが固まらない。"""
        if self._occ_busy:
            return
        gaps = _all_gapfills()
        lines = self._hull_lines() if _OCC_ENABLED else []
        if not lines and not gaps:
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
            if not _edge_enable():
                self.chk_edge.blockSignals(True)
                self.chk_edge.setChecked(False)
                self.chk_edge.blockSignals(False)
            else:
                # 成功通知は情報レベルで（cmds.warning は Script Editor で黄色になり誤読される）
                om2.MGlobal.displayInfo(
                    "画像空間エッジ検出 ON（対象 {0} メッシュ）。VP2/DirectX11・ビューポート専用。"
                    .format(len(targets)))
        else:
            _edge_disable()

    def _on_edge_param(self, *args):
        """しきい値/太さ/色/デバッグ表示を反映（線色は共通カラーに追従）。"""
        _edge_set_params(threshold=self.spn_edge_thr.value(),
                         thickness=self.spn_edge_w.value(), color=self._color,
                         debug=self.chk_edge_debug.isChecked())

    def _on_scrn_bias(self, *args):
        """スクリーン輪郭の重なり深度押し込み（ビュー空間）を全ラインへ反映。"""
        _scrn_set_depth_bias(self.spn_scrn_bias.value())

    def _on_brush_changed(self, *args):
        """v0.1.14: ブラシ質感（カサつき/ゆらぎ/周期）を全 SCRN ラインへ反映。"""
        _scrn_set_brush(rough=self.spn_brush_rough.value(),
                        wave=self.spn_brush_wave.value(),
                        scale=self.spn_brush_scale.value())

    def _on_create_edge_line_button(self, *args):
        """v0.1.26: 「選択エッジにライン」ボタン。エッジタブのランプを新規ラインへ適用。
        内部実装 create_edge_line() は常にフラット初期化するので、生成差分で特定して
        エッジタブのランプが非フラットなら上書きする。"""
        before = set()
        for t in cmds.ls(type="transform", long=True) or []:
            if cmds.attributeQuery(EDGE_TAG, node=t, exists=True):
                before.add(t)
        self.create_edge_line()
        # エッジタブが非フラットなら新規ラインへ適用
        try:
            pts = self.ramp_edge.points()
        except Exception:
            pts = None
        is_flat = (pts and len(pts) == 2 and
                   abs(pts[0][0]) < 1e-4 and abs(pts[0][1] - 1.0) < 1e-4 and
                   abs(pts[-1][0] - 1.0) < 1e-4 and abs(pts[-1][1] - 1.0) < 1e-4)
        if pts and not is_flat:
            s = _serialize_profile(pts)
            after = set()
            for t in cmds.ls(type="transform", long=True) or []:
                if cmds.attributeQuery(EDGE_TAG, node=t, exists=True):
                    after.add(t)
            new_lines = after - before
            for ln in new_lines:
                ctrl = _ctrl_of(ln)
                if ctrl and cmds.attributeQuery(CTRL_PROFILE, node=ctrl, exists=True):
                    try:
                        cmds.setAttr(ctrl + "." + CTRL_PROFILE, s, type="string")
                    except Exception:
                        pass
                try:
                    _update_curv_weights(ln)
                except Exception:
                    pass

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

    def _current_prof_ramp(self):
        """現在のプロファイルタブに対応する (ramp, kind) を返す。kind: 'scrn'|'crease'|'edge'."""
        idx = self.prof_tabs.currentIndex()
        if idx == 0: return self.ramp_scrn, "scrn"
        if idx == 1: return self.ramp_crease, "crease"
        return self.ramp_edge, "edge"

    def _reset_current_prof_ramp(self, *args):
        ramp, _ = self._current_prof_ramp()
        ramp.set_points([[0.0, 1.0], [1.0, 1.0]])

    def _apply_ramp_to_lines(self, *args):
        """v0.1.26: 現在のタブに対応したランプを既存ラインに適用。
        ツリー選択ありなら選択ライン、無ければタブに対応する全ライン。"""
        ramp, kind = self._current_prof_ramp()
        if kind == "scrn":
            cmds.warning("SCRN 輪郭線には長手プロファイルを適用できません"); return
        try:
            pts = ramp.points()
        except Exception:
            cmds.warning("ランプの値が取れませんでした"); return
        s = _serialize_profile(pts) if pts else "0:1,1:1"
        sel = self._selected_lines() if hasattr(self, "_selected_lines") else []
        if sel:
            targets = [ln for ln in sel if cmds.objExists(ln)]
            label = "選択 {} 本".format(len(targets))
        elif kind == "crease":
            targets = [t for t in (cmds.ls(type="transform", long=True) or [])
                       if cmds.attributeQuery(CREASE_TAG, node=t, exists=True)]
            label = "クリース {} 本".format(len(targets))
        else:  # edge
            targets = [t for t in (cmds.ls(type="transform", long=True) or [])
                       if cmds.attributeQuery(EDGE_TAG, node=t, exists=True)
                       and not cmds.attributeQuery(CREASE_TAG, node=t, exists=True)]
            label = "エッジ {} 本".format(len(targets))
        if not targets:
            cmds.warning("適用対象のラインがありません"); return
        n = 0
        cmds.undoInfo(openChunk=True)
        try:
            for ln in targets:
                ctrl = _ctrl_of(ln)
                if ctrl and cmds.attributeQuery(CTRL_PROFILE, node=ctrl, exists=True):
                    try:
                        cmds.setAttr(ctrl + "." + CTRL_PROFILE, s, type="string")
                        n += 1
                    except Exception:
                        pass
                try:
                    _update_curv_weights(ln)
                except Exception:
                    pass
        finally:
            cmds.undoInfo(closeChunk=True)
        cmds.warning("ランプを {} に適用しました（{} 本）".format(label, n))

    def _apply_ramp_to_all_creases(self, *args):
        """v0.1.24: UI のクリースランプを、既存の全クリース線（CREASE_TAG 付き）に適用。
        生成時だけでなく「あとから一括変更」したいときに使う。"""
        try:
            pts = self.crease_ramp.points()
        except Exception:
            cmds.warning("ランプの値が取れませんでした"); return
        s = _serialize_profile(pts) if pts else "0:1,1:1"
        creases = [t for t in (cmds.ls(type="transform", long=True) or [])
                   if cmds.attributeQuery(CREASE_TAG, node=t, exists=True)]
        if not creases:
            cmds.warning("クリース線（CREASE_TAG 付き）が見つかりません"); return
        n = 0
        cmds.undoInfo(openChunk=True)
        try:
            for ln in creases:
                ctrl = _ctrl_of(ln)
                if ctrl and cmds.attributeQuery(CTRL_PROFILE, node=ctrl, exists=True):
                    try:
                        cmds.setAttr(ctrl + "." + CTRL_PROFILE, s, type="string")
                        n += 1
                    except Exception:
                        pass
                try:
                    _update_curv_weights(ln)
                except Exception:
                    pass
        finally:
            cmds.undoInfo(closeChunk=True)
        cmds.warning("現在のランプを {} 本のクリース線に適用しました。".format(n))

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
        base = COL_PREFIX + _short(line)
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
        suffix = ""  # 旧テキスト撤去（アイコンで判別）
        label = _short(line) + suffix
        it = QtWidgets.QTreeWidgetItem([label, "", ""])
        it.setData(0, QtCore.Qt.UserRole, line)
        # v0.1.27: 種別はテキスト色で表現（旧ドットアイコンはチェックボックスと紛らわしかったため）
        it.setForeground(0, QtGui.QBrush(QtGui.QColor(*type_color)))
        it.setToolTip(0, "{} ({})".format(_short(line), type_label))
        # ライン: ドラッグ可・ドロップ不可（グループにのみ落とす）
        it.setFlags((it.flags() | QtCore.Qt.ItemIsUserCheckable
                     | QtCore.Qt.ItemIsDragEnabled) & ~QtCore.Qt.ItemIsDropEnabled)
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
        return it

    def _add_group_item(self, group_node, lines, visible):
        """展開式（プルダウン）のグループ項目を追加する。"""
        label = _short(group_node) if group_node else "(未分類)"
        # v0.1.26: __Creases__ はツリー表示では「クリース」に見せる（ノード名は内部で維持）
        if label == CREASES_GROUP_NAME:
            label = CREASES_GROUP_DISPLAY
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
        f = gi.font(0); f.setBold(True); gi.setFont(0, f)
        gi.setBackground(0, QtGui.QBrush(QtGui.QColor(70, 78, 88)))
        self.tree.addTopLevelItem(gi)
        for line in lines:
            self._add_line_item(gi, line)
            # 既存ラインにもコントローラー・接続・追従ジョブを確保（旧データ/再開対応）
            _ensure_line_anim(line, self._thickness_of(line) or 0.05)
        gi.setExpanded(True)
        return gi

    def refresh_tree(self):
        self._populating = True
        self.tree.clear()
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
        ctrls = [_ctrl_of(n) for n in self._selected_nodes()]
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

    def _set_thickness_widgets(self, val):
        self.slider.blockSignals(True); self.spin.blockSignals(True)
        self.spin.setValue(val)
        self.slider.setValue(int(val * 1000))
        self.slider.blockSignals(False); self.spin.blockSignals(False)

    # ========== undo 制御 ==========
    def _begin_drag(self):
        """スライダー押下：ドラッグ全体を1つの undo チャンクにまとめる。"""
        self._dragging = True
        cmds.undoInfo(openChunk=True)

    def _end_drag(self):
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
            gc = _ensure_global_ctrl()
            self._set_ctrl_attr(gc, GMULT, val, "全体倍率")
        finally:
            if chunk:
                cmds.undoInfo(closeChunk=True)
        self._update_mult_labels()

    def _update_mult_labels(self):
        """ツリーのグループ倍率（太さ列）を再描画（再構築せず）。"""
        self._populating = True
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            gi = root.child(i)
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

    def _set_mult_widgets(self):
        """選択に応じてグループ倍率・全体倍率スライダーを現在値へ同期。"""
        g = 1.0
        gc = _find_global_ctrl()
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

    # ========== 曲率起伏（選択ラインのみ） ==========
    def _on_cslider(self, v):
        val = v / 100.0
        self.cspin.blockSignals(True); self.cspin.setValue(val); self.cspin.blockSignals(False)
        self._apply_curvature()

    def _on_cspin(self, val):
        self.cslider.blockSignals(True); self.cslider.setValue(int(val * 100)); self.cslider.blockSignals(False)
        self._apply_curvature()

    def _on_cap_slider(self, v):
        val = v / 100.0
        self.cap_spin.blockSignals(True); self.cap_spin.setValue(val); self.cap_spin.blockSignals(False)
        self._apply_curvature()

    def _on_cap_spin(self, val):
        self.cap_slider.blockSignals(True); self.cap_slider.setValue(int(val * 100)); self.cap_slider.blockSignals(False)
        self._apply_curvature()

    def _on_cmin_slider(self, v):
        val = v / 100.0
        self.cmin_spin.blockSignals(True); self.cmin_spin.setValue(val); self.cmin_spin.blockSignals(False)
        self._apply_curvature()

    def _on_cmin_spin(self, val):
        self.cmin_slider.blockSignals(True); self.cmin_slider.setValue(int(val * 100)); self.cmin_slider.blockSignals(False)
        self._apply_curvature()

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
        cmds.undoInfo(openChunk=True)
        try:
            for line in lines:
                ctrl = _ensure_line_anim(line, self.spin.value())
                self._set_ctrl_attr(ctrl, CTRL_PROFILE, s, "太さプロファイル", as_string=True)
                _update_curv_weights(line)
        finally:
            cmds.undoInfo(closeChunk=True)

    def _reset_profile(self):
        self.ramp.set_points([[0.0, 1.0], [1.0, 1.0]])
        self._apply_profile()

    def _apply_curvature(self):
        lines = self._selected_lines()
        if not lines:
            return
        influence = self.cspin.value()
        cap = self.cap_spin.value()
        cmin = self.cmin_spin.value()
        chunk = not self._dragging   # ドラッグ中は _begin/_end_drag のチャンクに含める
        if chunk:
            cmds.undoInfo(openChunk=True)
        try:
            for line in lines:
                # コントローラー属性 curvature / curvatureCap / curvatureMin を設定 → scriptJob
                #   で頂点ウェイトが再計算されるが、即時反映のため明示的にも更新する。
                ctrl = _ensure_line_anim(line, self.spin.value())
                self._set_ctrl_attr(ctrl, CTRL_CURV, influence, "曲率起伏")
                self._set_ctrl_attr(ctrl, CTRL_CAP, cap, "曲率上限")
                self._set_ctrl_attr(ctrl, CTRL_CMIN, cmin, "曲率下限")
                _update_curv_weights(line)
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
        gc = _ensure_global_ctrl()
        try:
            cmds.select(gc, r=True)
        except Exception:
            pass
        self._update_key_colors()

    def _key_gmult(self):
        gc = _ensure_global_ctrl()
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
        gc = _find_global_ctrl()
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
            self.refresh_tree()

    def _apply_line_color(self, line, rgb):
        """v0.1.25: 1 本のラインに色を適用するコア処理（undo チャンクは呼び出し側で）。"""
        sh = self._shape_of(line)
        if not sh:
            return
        # フレネル/スクリーンは dx11Shader の lineColor uniform を直接変更
        if (cmds.attributeQuery(FRES_TAG, node=line, exists=True)
                or cmds.attributeQuery(SCRN_TAG, node=line, exists=True)):
            shd = _fresnel_shader(line)
            if shd and cmds.attributeQuery(FRES_COLOR, node=shd, exists=True):
                cmds.setAttr(shd + "." + FRES_COLOR, rgb[0], rgb[1], rgb[2], type="double3")
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
                if (cmds.attributeQuery(FRES_TAG, node=line, exists=True)
                        or cmds.attributeQuery(SCRN_TAG, node=line, exists=True)):
                    shd = _fresnel_shader(line)
                    if shd and cmds.attributeQuery(FRES_COLOR, node=shd, exists=True):
                        cmds.setAttr(shd + "." + FRES_COLOR, self._color[0], self._color[1],
                                     self._color[2], type="double3")
                    continue
                if sh:
                    cmds.sets(sh, e=True, forceElement=sg)
                base = COL_PREFIX + _short(line)
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
        b = cmds.duplicate(srcT, name=_short(srcT) + "_gapfill", rr=True)[0]
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
                dup = cmds.duplicate(obj, name=_short(obj) + "_outline", rr=True)[0]
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
        grp = self._current_group()

        cmds.undoInfo(openChunk=True)
        made = []
        try:
            for obj in sel:
                shps = cmds.listRelatives(obj, shapes=True, type="mesh", ni=True, f=True)
                if not shps:
                    continue
                src = shps[0]

                # 元と同じ位置に複製 → 履歴削除で静的化
                dup = cmds.duplicate(obj, name=_short(obj) + "_fresnel", rr=True)[0]
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
        grp = self._current_group()

        cmds.undoInfo(openChunk=True)
        made = []
        try:
            for obj in sel:
                shps = cmds.listRelatives(obj, shapes=True, type="mesh", ni=True, f=True)
                if not shps:
                    continue
                src = shps[0]
                dup = cmds.duplicate(obj, name=_short(obj) + "_screen", rr=True)[0]
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
        # v0.1.23: ランプから太さプロファイル文字列を作る。フラット (0,1)(1,1) なら何もしない。
        crease_profile_str = None
        try:
            pts = self.crease_ramp.points()
        except Exception:
            pts = None
        if pts and not (len(pts) == 2 and
                        abs(pts[0][0]) < 1e-4 and abs(pts[0][1] - 1.0) < 1e-4 and
                        abs(pts[-1][0] - 1.0) < 1e-4 and abs(pts[-1][1] - 1.0) < 1e-4):
            try:
                crease_profile_str = _serialize_profile(pts)
            except Exception:
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
                short = _short(obj)
                indices = []
                for ec in edges:
                    try:
                        indices.append(int(ec.split("[")[-1].rstrip("]")))
                    except Exception:
                        pass
                chains = _split_edges_into_chains(shps[0], indices)
                any_made = False
                for chain in chains:
                    if not chain: continue
                    comps = ["{}.e[{}]".format(short, i) for i in chain]
                    cmds.select(comps, r=True)
                    try:
                        self.create_edge_line()
                        any_made = True
                    except Exception:
                        pass
                if any_made:
                    made += 1
            # v0.1.24: 新規生成されたクリース線だけを処理する
            new_lines = _all_edges() - before
            if new_lines:
                # 専用グループ (__Creases__) 確保 → 全クリース線を集約
                creases_grp = self._ensure_group(CREASES_GROUP_NAME)
                for ln in new_lines:
                    # CREASE_TAG を付けてクリース出自を識別
                    if not cmds.attributeQuery(CREASE_TAG, node=ln, exists=True):
                        try:
                            cmds.addAttr(ln, ln=CREASE_TAG, at="bool", dv=True)
                        except Exception:
                            pass
                    # __Creases__ へ親子移動
                    try:
                        cur_parent = cmds.listRelatives(ln, parent=True, f=True)
                        if not cur_parent or cur_parent[0] != creases_grp:
                            cmds.parent(ln, creases_grp)
                    except Exception:
                        pass
                    # v0.1.23: ランププロファイルの書き込み（フラットなら変更しない）
                    if crease_profile_str:
                        ctrl = _ctrl_of(ln)
                        if ctrl and cmds.attributeQuery(CTRL_PROFILE, node=ctrl, exists=True):
                            try:
                                cmds.setAttr(ctrl + "." + CTRL_PROFILE, crease_profile_str, type="string")
                            except Exception:
                                pass
                        try:
                            _update_curv_weights(ln)
                        except Exception:
                            pass
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

    def create_edge_line(self, *args):
        """選択ポリゴンエッジに沿ってチューブ状のラインを追加（太さ調整可・元に追従）。"""
        sel = cmds.ls(sl=True, fl=True) or []
        edges = cmds.filterExpand(sel, sm=32) or []
        if not edges:
            cmds.warning("メッシュのエッジを選択してください"); return
        # 新規エッジラインは UI の現在値ではなく初期値で生成する
        thick = DEFAULT_EDGE_THICK
        _, sg = _ensure_shader(self._color)
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
        old = _short(node)
        new, ok = QtWidgets.QInputDialog.getText(self, "リネーム", "新しい名前:", text=old)
        new = (new or "").strip()
        if not ok or not new or new == old:
            return
        cmds.undoInfo(openChunk=True)
        try:
            # コントローラーはリネーム前に取得（message リンクで特定。名前依存しない）
            ctrl = _ctrl_of(node)
            newpath = cmds.rename(node, new)
            # コントローラー名もラインに合わせて変更（アウトライナーの見た目を一致させる）
            if ctrl and cmds.objExists(ctrl):
                try:
                    cmds.rename(ctrl, _short(newpath) + CTRL_SUFFIX)
                except Exception:
                    pass
        except Exception:
            cmds.warning("リネームに失敗しました: {}".format(new))
        finally:
            cmds.undoInfo(closeChunk=True)
        self.refresh_tree()
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
        if is_group:
            a1 = menu.addAction("全ライン 表示")
            a1.triggered.connect(lambda: self._set_group_lines_vis(node, True))
            a2 = menu.addAction("全ライン 非表示")
            a2.triggered.connect(lambda: self._set_group_lines_vis(node, False))
            menu.addSeparator()
            a3 = menu.addAction("色を揃える…")
            a3.triggered.connect(lambda: self._unify_group_color(node))
            if is_crease_group:
                menu.addSeparator()
                a4 = menu.addAction("現在のランプを適用")
                a4.triggered.connect(self._apply_ramp_to_all_creases)
            menu.addSeparator()
            a5 = menu.addAction("リネーム…")
            a5.triggered.connect(lambda: self.rename_node(node))
            a6 = menu.addAction("削除")
            a6.triggered.connect(lambda: self.delete_selected())
        elif is_line:
            a1 = menu.addAction("表示切替")
            a1.triggered.connect(lambda: self._toggle_line_vis(node))
            a2 = menu.addAction("色を変える…")
            a2.triggered.connect(lambda: self._pick_color_for_line(node))
            menu.addSeparator()
            a3 = menu.addAction("リネーム…")
            a3.triggered.connect(lambda: self.rename_node(node))
            a4 = menu.addAction("削除")
            a4.triggered.connect(lambda: self.delete_selected())
        menu.exec_(self.tree.viewport().mapToGlobal(pos))

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
        self.refresh_tree()

    def _toggle_line_vis(self, line):
        if not line or not cmds.objExists(line): return
        try:
            cur = bool(cmds.getAttr(line + ".visibility"))
        except Exception:
            cur = True
        try: cmds.setAttr(line + ".visibility", 0 if cur else 1)
        except Exception: pass
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
        cmds.undoInfo(openChunk=True)
        try:
            for ln in lines:
                try:
                    cmds.parent(ln, group)
                except Exception:
                    pass
        finally:
            cmds.undoInfo(closeChunk=True)
        self.refresh_tree()

    # ========== グループ管理 ==========
    def new_group(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "新規グループ", "グループ名:")
        name = (name or "").strip()
        if not ok or not name:
            return
        cmds.undoInfo(openChunk=True)
        try:
            self._ensure_group(name)
        finally:
            cmds.undoInfo(closeChunk=True)
        self.refresh_tree()
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
                _update_curv_weights(line)
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

    def delete_selected(self):
        nodes = self._selected_nodes()
        if not nodes:
            cmds.warning("削除する項目をツリーで選択してください"); return
        # 削除対象ライン（選択ライン＋選択グループ配下ライン）のコントローラー・乗算ノードも巻き込む
        def _gather(line):
            c = _ctrl_of(line)
            if c:
                victims.add(c)
                for s in ("_thkA", "_thkB", "_thkMaskBoost"):
                    n2 = _short(c) + s
                    if cmds.objExists(n2):
                        victims.add(n2)
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
        _toon_win.close(); _toon_win.deleteLater()
    except Exception:
        pass
    try:
        for w in QtWidgets.QApplication.topLevelWidgets():
            try:
                if w.objectName() == WINDOW_OBJ:
                    w.close(); w.deleteLater()
            except Exception:
                pass
    except Exception:
        pass


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
        exec(compile(source, _INSTALLER_FILE + " (from GitHub)", "exec"), ns)
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
_EDGE_PARAMS = {"threshold": 0.05, "thickness": 1.0, "color": (0.0, 0.0, 0.0),
                "near": 0.1, "far": 10000.0, "debug": 0}
_EDGE_TARGETS = []          # エッジ検出対象の transform 名リスト（オブジェクト単位を保持）
_edge_override = None
_edge_size_logged = (0, 0)  # 直近ログしたターゲット (w, h) — 変化時に一度だけ報告

_EDGE_FX = """// OG Toonline Manager - object-scoped screen-space depth-edge outline
Texture2D gColorTex;   // 全シーンの色
Texture2D gDepthTex;   // 対象メッシュだけを描いた深度（対象なしなら 1.0=遠クリア）

SamplerState gSamp { Filter = MIN_MAG_MIP_POINT; AddressU = Clamp; AddressV = Clamp; };

float2 gTexel     = {0.001f, 0.001f};   // (1/width, 1/height) * thickness
float  gThreshold = 0.01f;              // ビュー空間深度の相対差しきい値（|Δz|/z）
float3 gLineColor = {0.0f, 0.0f, 0.0f};
float  gNear      = 0.1f;
float  gFar       = 10000.0f;
int    gDebug     = 0;                  // 1: 線形化深度を可視化（切り分け用）

struct VIN  { float3 Pos : POSITION; float2 UV : TEXCOORD0; };
struct VOUT { float4 Pos : SV_Position; float2 UV : TEXCOORD0; };

VOUT VS(VIN i) { VOUT o; o.Pos = float4(i.Pos, 1.0f); o.UV = i.UV; return o; }

// DirectX の非線形深度 [0,1] → ビュー空間の線形深度
float linearizeDepth(float z)
{
    // near*far / (far - z*(far-near)). z=1(遠)→far, z=0(近)→near
    float denom = gFar - z * (gFar - gNear);
    return (gNear * gFar) / max(denom, 1e-6f);
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
    float rel  = dmax / max(c, 1e-4f);

    if (gDebug != 0)
    {
        // 線形化深度を [near, far] で正規化して可視化（対象なし=白, 近い=黒）。
        float span = max(gFar - gNear, 1e-4f);
        float g = saturate((c - gNear) / span);
        return float4(g, g, g, 1.0f);
    }

    float3 scene = gColorTex.Sample(gSamp, i.UV).rgb;
    // 対象未描画（背景）ピクセルは線化しない
    float mask = (zc >= 0.9999f) ? 0.0f : 1.0f;
    // 遷移幅は最小 1 ピクセルAA程度に絞って線をハッキリさせる（かすれ対策）。
    // 幅を広くすると内部の緩い深度勾配がだらだら半透明で塗られて滲む。
    float hi = gThreshold * 1.15f;
    float e  = mask * smoothstep(gThreshold, hi, rel);
    return float4(lerp(scene, gLineColor, e), 1.0f);
}

technique11 Main { pass p0 {
    SetVertexShader(CompileShader(vs_5_0, VS()));
    SetPixelShader(CompileShader(ps_5_0, PS()));
} }
"""


def _edge_fx_path():
    d = os.path.join(cmds.internalVar(userAppDir=True), "OG_Toonline_Manager")
    try:
        if not os.path.isdir(d):
            os.makedirs(d)
    except Exception:
        d = cmds.internalVar(userTmpDir=True)
    p = os.path.join(d, "OG_EdgeOutline.fx").replace("\\", "/")
    try:
        with open(p, "w") as f:
            f.write(_EDGE_FX)
    except Exception:
        pass
    return p


def _edge_set_targets(names):
    """エッジ検出の対象メッシュ（transform 名）を設定。存在するものだけ保持。"""
    global _EDGE_TARGETS
    _EDGE_TARGETS = [n for n in (names or []) if cmds.objExists(n)]


def _edge_set_params(threshold=None, thickness=None, color=None, debug=None):
    if threshold is not None:
        # smoothstep(gThreshold, gThreshold*2, ..) が 0/0 になる事故を防ぐ最小値
        _EDGE_PARAMS["threshold"] = max(1e-4, float(threshold))
    if thickness is not None:
        _EDGE_PARAMS["thickness"] = float(thickness)
    if color is not None:
        _EDGE_PARAMS["color"] = (color[0], color[1], color[2])
    if debug is not None:
        _EDGE_PARAMS["debug"] = 1 if debug else 0
    try:
        cmds.refresh()
    except Exception:
        pass


def _edge_active_camera_near_far(destination=None):
    """指定パネル（無ければフォーカス→最初の modelPanel）の active camera の near/far を返す。
    複数ビューポート運用でも、shader() 実行中はその描画対象パネルのカメラを見るのが正しい。
    取得に失敗したら現在値を維持。"""
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
                return n, f
    except Exception:
        pass
    return _EDGE_PARAMS["near"], _EDGE_PARAMS["far"]


def _edge_active():
    return _edge_override is not None


def _edge_enable():
    """画像空間エッジ検出のレンダーオーバーライドを現在のモデルパネルへ適用。
    成功で True。レンダー API が無い/失敗しても例外を投げず False を返す（本体は無事）。"""
    global _edge_override
    try:
        import maya.api.OpenMayaRender as omr
    except Exception as exc:
        cmds.warning("画像空間エッジ検出はこの環境で使えません（VP2/DirectX11 が必要）: {0}".format(exc))
        return False
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
                    try:
                        if grad:
                            c.setClearGradient(True)
                            c.setClearColor([float(top[0]), float(top[1]), float(top[2]), 1.0])
                            c.setClearColor2([float(bot[0]), float(bot[1]), float(bot[2]), 1.0])
                        else:
                            c.setClearGradient(False)
                            c.setClearColor([float(bg[0]), float(bg[1]), float(bg[2]), 1.0])
                    except Exception:
                        c.setClearGradient(False)
                        c.setClearColor([0.36, 0.36, 0.36, 1.0])
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
                        s.setParameter("gDebug", int(_EDGE_PARAMS["debug"]))
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
                    self._tmgr = omr.MRenderer.getRenderTargetManager()
                    self._cDesc = omr.MRenderTargetDescription(
                        "OG_edgeColor", 256, 256, 1, omr.MRenderer.kR8G8B8A8_UNORM, 0, False)
                    self._csDesc = omr.MRenderTargetDescription(
                        "OG_edgeColorScratch", 256, 256, 1, omr.MRenderer.kR8G8B8A8_UNORM, 0, False)
                    self._dsDesc = omr.MRenderTargetDescription(
                        "OG_edgeDepthScene", 256, 256, 1, omr.MRenderer.kD24S8, 0, False)
                    self._dDesc = omr.MRenderTargetDescription(
                        "OG_edgeDepth", 256, 256, 1, omr.MRenderer.kD24S8, 0, False)
                    self._sceneFull = _SceneFull("og_edge_full", self)
                    self._sceneTgt = _SceneTargets("og_edge_tgt", self)
                    self._quad = _QuadEdge("og_edge_quad", self)
                    self._hud = omr.MHUDRender()
                    self._present = omr.MPresentTarget("og_edge_present")
                    self._ops = [self._sceneFull, self._sceneTgt, self._quad, self._hud, self._present]
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
                    w = 0; h = 0
                    try:
                        ptr = omui.MQtUtil.findControl(destination) if destination else None
                        if ptr:
                            widget = wrapInstance(int(ptr), QtWidgets.QWidget)
                            try:
                                dpr = float(widget.devicePixelRatioF())
                            except AttributeError:
                                dpr = float(widget.devicePixelRatio())
                            w = int(max(1, round(widget.width() * dpr)))
                            h = int(max(1, round(widget.height() * dpr)))
                    except Exception:
                        w = 0; h = 0
                    if w <= 0 or h <= 0:
                        try:
                            tgt = omr.MRenderer.outputTargetSize()
                            w, h = int(tgt[0]), int(tgt[1])
                        except Exception:
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
                    for desc in (self._cDesc, self._csDesc, self._dsDesc, self._dDesc):
                        desc.setWidth(self.w); desc.setHeight(self.h)
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
                def cleanup(self):
                    pass
                def startOperationIterator(self):
                    self._it = 0; return True
                def renderOperation(self):
                    return self._ops[self._it]
                def nextRenderOperation(self):
                    self._it += 1; return self._it < len(self._ops)
                def release(self):
                    for a in ("tColor", "tColorScratch", "tDepthScene", "tDepth"):
                        t = getattr(self, a, None)
                        if t is not None:
                            try:
                                self._tmgr.releaseRenderTarget(t)
                            except Exception:
                                pass
                            setattr(self, a, None)

            _edge_override = _EdgeOverride(EDGE_OVR_NAME)
            omr.MRenderer.registerOverride(_edge_override)
        except Exception as exc:
            _edge_override = None
            cmds.warning("画像空間エッジ検出の初期化に失敗: {0}".format(exc))
            return False
    # 現在のモデルパネルへ適用
    panel = None
    p = cmds.getPanel(withFocus=True)
    if p and cmds.getPanel(typeOf=p) == "modelPanel":
        panel = p
    else:
        for pp in (cmds.getPanel(type="modelPanel") or []):
            panel = pp; break
    if not panel:
        cmds.warning("モデルパネルが見つかりません"); return False
    try:
        cmds.modelEditor(panel, e=True, rendererOverrideName=EDGE_OVR_NAME)
        cmds.refresh()
    except Exception as exc:
        cmds.warning("画像空間エッジ検出の適用に失敗: {0}".format(exc)); return False
    return True


def _edge_disable():
    """画像空間エッジ検出を全モデルパネルから解除し、オーバーライドを破棄。"""
    global _edge_override
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
    # モジュールグローバルの参照を後始末
    try:
        _toon_win.close(); _toon_win.deleteLater()
    except Exception:
        pass
    # スクリプト再実行で global がリセットされても残っている同名ウィンドウを全て掃除
    for w in QtWidgets.QApplication.topLevelWidgets():
        try:
            if w.objectName() == WINDOW_OBJ:
                w.close(); w.deleteLater()
        except Exception:
            pass
    _toon_win = ToonOutlineUI()
    _toon_win.show()
    return _toon_win


if __name__ == "__main__":
    show()
