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
EDGE_TAG    = "isToonEdgeLine"        # エッジ由来チューブラインの識別タグ
PROFILE_LINK = "toonProfile"         # エッジラインの円プロファイル(makeNurbCircle)への message
FRES_TAG    = "isToonFresnelLine"    # フレネル輪郭（カメラ依存・VP2/バッチ対応）の識別タグ
FRES_LINK   = "toonFresnelCond"      # フレネルの condition ノードへの message（太さ＝しきい値）
FRES_SCALE  = 0.1                    # UI 太さ → フレネルしきい値(facingRatio カット)への係数（細め＝寝た面を拾いにくい）
FRES_ZOFFSET = 0.001                 # 極小の法線オフセット（手前に出して見えるように・二重線最小）
FRES_THRESH = "threshold"            # dx11Shader 上のしきい値 uniform 名（太さ駆動先）
FRES_COLOR  = "lineColor"            # dx11Shader 上の線色 uniform 名（フレネル/スクリーン共通）
SCRN_TAG    = "isToonScreenLine"     # スクリーン空間押し出し輪郭（隙間なし・均一太さ）の識別タグ
SCRN_THICK  = "thickness"            # dx11Shader 上の太さ(ピクセル) uniform 名（太さ駆動先）
SCRN_SCALE  = 6.0                    # UI 太さ → スクリーン押し出しピクセル数への係数
MASK_TAG    = "isToonOverlapMask"    # 重なり隠しマスク（元メッシュ複製を面色で膨らませた覆い）の識別タグ
MASK_LINK   = "toonMask"             # line → mask への message（重なりマスクの関連付け）
MASK_INFLATE_FRAC = 1.0              # マスク膨らみ量 = ライン太さ × 係数。大きいほど交差を覆える
                                     # （太さは上乗せ補正で一定。係数↑＝覆う力↑だがモデルが膨らむ）
MASK_HOLDER = "ToonMask_grp"         # 重なりマスクの格納グループ（ROOT 直下・別オブジェクトとして可視）
GAPFILL_TAG  = "isToonGapFill"       # 隙間埋めオブジェクト（facing で寝た面をベースの位置へ押し出す）の識別タグ
GAPFILL_LINK = "toonGapFill"         # line(A) → gapfill(B) への message
GAPFILL_HOLDER = "ToonGapFill_grp"   # 隙間埋めオブジェクトの格納グループ（ROOT 直下）
FACING_THRESH = 0.5                  # facing(=|N·視線|) がこれ未満で押し出し開始（シルエット帯の広さ）
GAPFILL_SMOOTH_ITERS = 1             # 隙間埋めウェイトの近傍スムージング回数
GAPFILL_TUCK = -1.0                  # シルエット以外（正面/背面の面）の weight 下限（負＝表面の裏へ潜らせ隠す）
GLOBAL_CTRL = "toonOutline_globalCtrl"  # 全体コントローラー（コントローラー階層の親）
COL_GLOBAL  = (0.4, 0.8, 1.0)        # 全体=水色
COL_GROUP   = (0.55, 0.9, 0.2)       # グループ=黄緑
COL_LINE    = (1.0, 0.8, 0.0)        # ライン=黄
WINDOW_OBJ  = "OG_Toonline_ManagerWin"  # ウィンドウ識別名（重複起動の防止に使用）

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
    float facing = abs(N.z);                              // 1=正面, 0=シルエット
    // facing < threshold の帯を「くっきり」不透明に（トゥーン線）。縁だけ fwidth で1px AA。
    float w = max(fwidth(facing), 1e-5f);
    float a = 1.0f - smoothstep(threshold - w, threshold + w, facing);
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


def _fresnel_fx_path():
    """フレネル用 HLSL(.fx) をユーザー領域に書き出してパスを返す（毎回上書き）。"""
    d = os.path.join(cmds.internalVar(userAppDir=True), "OG_Toonline_Manager")
    try:
        if not os.path.isdir(d):
            os.makedirs(d)
    except Exception:
        d = cmds.internalVar(userTmpDir=True)
    path = os.path.join(d, "OG_ToonFresnel.fx").replace("\\", "/")
    try:
        with open(path, "w") as f:
            f.write(_FRES_FX_HLSL)
    except Exception:
        pass
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


_SCRN_FX_HLSL = """// OG Toonline Manager - Screen-space outline (Maya dx11Shader / HLSL)
// 頂点をクリップ空間でシルエット外側へ一定ピクセル押し出す（隙間なし・均一太さ）。
// さらに深度を僅かに奥へ押し込み、元メッシュに内側を隠させて外周リングだけ残す。
// ※ ビューポートは「テクスチャ表示 ON（ホットキー 6）」で表示されます。
float4x4 gWVP : WorldViewProjection;
float4x4 gWV  : WorldView;
float2   gScreen : ViewportPixelSize;

float thickness <
    string UIName = "Thickness(px)";
    float UIMin = 0.0;
    float UIMax = 30.0;
    float UIStep = 0.1;
> = 3.0;

float3 lineColor <
    string UIName = "Line Color";
    string UIWidget = "Color";
> = {0.0f, 0.0f, 0.0f};

static const float gZBias = 0.0015f;   // 元メッシュに内側を隠させる深度押し込み量

struct APPDATA { float3 Position : POSITION; float3 Normal : NORMAL; };
struct V2P { float4 HPos : SV_Position; };

V2P VShader(APPDATA IN)
{
    V2P OUT;
    float4 clip = mul(float4(IN.Position, 1.0f), gWVP);
    float3 vn = mul(IN.Normal, (float3x3)gWV);     // ビュー空間法線
    float2 sn = vn.xy;
    float  l  = length(sn);
    sn = (l > 1e-5f) ? (sn / l) : float2(0.0f, 0.0f);
    // ピクセル幅を NDC へ変換（clip.w を掛けて透視除算後に一定ピクセルへ）
    float2 px = float2(2.0f / max(gScreen.x, 1.0f), 2.0f / max(gScreen.y, 1.0f));
    clip.xy += sn * thickness * px * clip.w;
    clip.z += gZBias * clip.w;          // 奥へ押し込む → 元メッシュが内側を覆う＝外周だけ残る
    OUT.HPos = clip;
    return OUT;
}

float4 PShader(V2P IN) : SV_Target
{
    return float4(lineColor, 1.0f);
}

technique11 Main
{
    pass p0
    {
        SetVertexShader(CompileShader(vs_5_0, VShader()));
        SetPixelShader(CompileShader(ps_5_0, PShader()));
    }
}
"""


def _screen_fx_path():
    d = os.path.join(cmds.internalVar(userAppDir=True), "OG_Toonline_Manager")
    try:
        if not os.path.isdir(d):
            os.makedirs(d)
    except Exception:
        d = cmds.internalVar(userTmpDir=True)
    path = os.path.join(d, "OG_ToonScreen.fx").replace("\\", "/")
    try:
        with open(path, "w") as f:
            f.write(_SCRN_FX_HLSL)
    except Exception:
        pass
    return path


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
    cmds.sets(shape, e=True, forceElement=sg)
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
    is_scrn = cmds.attributeQuery(SCRN_TAG, node=line, exists=True)
    if not is_edge:
        _ensure_follow(line)
        _ensure_smooth_link(line)
    if not is_fres and not is_scrn:
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


def _gapfill_of(line):
    """ライン A に紐づく隙間埋めオブジェクト B を返す。無ければ None。"""
    if cmds.objExists(line) and cmds.attributeQuery(GAPFILL_LINK, node=line, exists=True):
        c = [x for x in (cmds.listConnections(line + "." + GAPFILL_LINK, s=True, d=False) or [])
             if cmds.objExists(x)]
        if c:
            return c[0]
    return None


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
        facing = abs(nx * vx + ny * vy + nz * vz)   # 0=寝た面(シルエット) / 1=正面・背面
        # facing<thr: 正方向（外へ押し出してベース A 位置へ＝隙間をスカートで埋める）
        # facing>thr: 負（元メッシュ表面の裏へ潜らせ本体に隠す＝二重線/面乗りを防ぐ）
        wt = (thr - facing) / thr
        if wt > 1.0:
            wt = 1.0
        elif wt < GAPFILL_TUCK:
            wt = GAPFILL_TUCK
        w[i] = wt
    w = _smooth_vertex_values(dag, w, GAPFILL_SMOOTH_ITERS)
    try:
        cmds.setAttr(defm + ".weightList[0].weights[0:{}]".format(n - 1), *w)
    except Exception:
        pass


def _update_curv_weights(line):
    """コントローラーの curvature / curvatureCap から頂点ウェイトを再計算。
    scriptJob からも呼ばれる（アニメーション時の追従用）。"""
    if not cmds.objExists(line):
        return
    defm = _line_deformer(line)
    ctrl = _ctrl_of(line)
    if not defm or not ctrl:
        return
    try:
        influence = cmds.getAttr(ctrl + "." + CTRL_CURV)
        cap = cmds.getAttr(ctrl + "." + CTRL_CAP)
    except Exception:
        return
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
        return
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
                                                  lambda ln=line: _update_curv_weights(ln)])
            jobs.append(jid)
        except Exception:
            pass
    _CURV_JOBS[line] = jobs


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
        p.fillRect(self.rect(), QtGui.QColor(45, 45, 45))
        p.setPen(QtGui.QColor(80, 80, 80))
        x0, by = self._to_px(0.0, 1.0); x1, _ = self._to_px(1.0, 1.0)
        p.drawLine(int(x0), int(by), int(x1), int(by))   # 基準 y=1
        p.setPen(QtGui.QPen(QtGui.QColor(120, 200, 255), 2))
        prev = None
        steps = 60
        for k in range(steps + 1):
            x = k / float(steps)
            y = _sample_profile(self._pts, x)
            px, py = self._to_px(x, y)
            if prev is not None:
                p.drawLine(int(prev[0]), int(prev[1]), int(px), int(py))
            prev = (px, py)
        p.setPen(QtGui.QColor(20, 20, 20))
        p.setBrush(QtGui.QColor(255, 200, 80))
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
        self.setWindowTitle("OG_Toonline_Manager")
        self.setObjectName(WINDOW_OBJ)
        self.setMinimumWidth(380)
        # 最小でもリスト(min120)＋固定パネル(200)＋下部コントロールが収まる高さ。
        # リストが余白を吸収するので下に余分な余白は出ない。
        self.setMinimumHeight(650)
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
        crow = QtWidgets.QHBoxLayout()
        self.btn_create = QtWidgets.QPushButton("選択メッシュに輪郭を生成")
        self.btn_create.clicked.connect(self.create_outlines)
        crow.addWidget(self.btn_create, 1)
        self.btn_edge = QtWidgets.QPushButton("選択エッジにライン")
        self.btn_edge.setToolTip("選択したポリゴンエッジに沿ってチューブ状のラインを追加")
        self.btn_edge.clicked.connect(self.create_edge_line)
        crow.addWidget(self.btn_edge)
        self.btn_gap = QtWidgets.QPushButton("隙間埋め")
        self.btn_gap.setToolTip("選択ハルラインに『隙間埋めオブジェクト』を追加/削除（トグル）。"
                                "元メッシュ複製(線色・背面法)で、カメラに対して寝た面=シルエットの頂点だけ"
                                "ベースのライン位置まで押し出し、正面の面は表面に張り付かせて輪郭の浮き隙間を塞ぐ。"
                                "カメラ依存・レイ不要で軽い。")
        self.btn_gap.clicked.connect(self.toggle_gapfill)
        crow.addWidget(self.btn_gap)
        b_grp = QtWidgets.QPushButton("新規グループ")
        b_grp.clicked.connect(self.new_group)
        crow.addWidget(b_grp)
        b_ref = QtWidgets.QPushButton("再取得")
        b_ref.clicked.connect(self.refresh_tree)
        crow.addWidget(b_ref)
        lay.addLayout(crow)

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

        self.chk_hide_handles = QtWidgets.QCheckBox("デフォーマハンドルをアウトライナーから隠す")
        self.chk_hide_handles.setChecked(True)
        self.chk_hide_handles.setToolTip("OFF にするとハンドルがアウトライナーに表示されます（削除は不可）")
        self.chk_hide_handles.toggled.connect(self._on_toggle_hide_handles)
        lay.addWidget(self.chk_hide_handles)

        self.chk_lock_select = QtWidgets.QCheckBox("ラインをビューポートで選択不可にする")
        self.chk_lock_select.setChecked(True)
        self.chk_lock_select.setToolTip("ON: ラインはビューポートで選択できません（表示・レンダーは有効）。"
                                        "OFF にすると通常通り選択できます")
        self.chk_lock_select.toggled.connect(self._on_toggle_lock_select)
        lay.addWidget(self.chk_lock_select)

        self.chk_occlude = QtWidgets.QCheckBox("ハルの隠れた面を元メッシュに寄せる（カメラ依存・検証中）")
        self.chk_occlude.setChecked(False)
        self.chk_occlude.setToolTip(
            "ON: カメラから見て元メッシュに隠れたハルの裏面頂点を元メッシュ内側へ寄せ、"
            "オフセットの隙間が横から見えるのを抑えます。シルエット部の太さは保たれます。\n"
            "※ scriptJob/頂点レイのため重く、ビューポート専用（バッチレンダー不可）。高密度メッシュ注意。")
        self.chk_occlude.toggled.connect(self._on_toggle_occlude)
        lay.addWidget(self.chk_occlude)

        # 隙間埋めパラメータ調整（検証用・最終的に廃止予定）
        self.grp_gapparams = QtWidgets.QGroupBox("隙間埋め 調整（検証用・後で廃止）")
        gpl = QtWidgets.QFormLayout(self.grp_gapparams)
        self.spn_face = QtWidgets.QDoubleSpinBox()
        self.spn_face.setRange(0.05, 1.0); self.spn_face.setSingleStep(0.05)
        self.spn_face.setDecimals(2); self.spn_face.setValue(FACING_THRESH)
        self.spn_face.setToolTip("シルエット帯の広さ（facing しきい値）。大きいほど押し出す帯が広い")
        self.spn_face.valueChanged.connect(self._on_gap_param_changed)
        gpl.addRow("帯の広さ (FACING_THRESH)", self.spn_face)
        self.spn_tuck = QtWidgets.QDoubleSpinBox()
        self.spn_tuck.setRange(-3.0, 0.0); self.spn_tuck.setSingleStep(0.1)
        self.spn_tuck.setDecimals(2); self.spn_tuck.setValue(GAPFILL_TUCK)
        self.spn_tuck.setToolTip("シルエット以外を表面の裏へ潜らせる深さ（負）。浅いと面乗り、深いと安全")
        self.spn_tuck.valueChanged.connect(self._on_gap_param_changed)
        gpl.addRow("裏潜り深さ (GAPFILL_TUCK)", self.spn_tuck)
        self.spn_smooth = QtWidgets.QSpinBox()
        self.spn_smooth.setRange(0, 8); self.spn_smooth.setValue(GAPFILL_SMOOTH_ITERS)
        self.spn_smooth.setToolTip("ウェイトの近傍スムージング回数（境界のジャギ軽減）")
        self.spn_smooth.valueChanged.connect(self._on_gap_param_changed)
        gpl.addRow("スムージング回数", self.spn_smooth)
        lay.addWidget(self.grp_gapparams)

        self.lbl_del = QtWidgets.QLabel("※ ライン/グループの削除は Delete キー")
        self.lbl_del.setStyleSheet("color:#888;")
        lay.addWidget(self.lbl_del)

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

    def _on_gap_param_changed(self, *args):
        """検証用: 隙間埋めパラメータ（帯の広さ/裏潜り深さ/スムージング）を反映して再計算。
        ※ これらはモジュール定数を直接書き換える（最終的にこの調整UIは廃止予定）。"""
        global FACING_THRESH, GAPFILL_TUCK, GAPFILL_SMOOTH_ITERS
        try:
            FACING_THRESH = float(self.spn_face.value())
            GAPFILL_TUCK = float(self.spn_tuck.value())
            GAPFILL_SMOOTH_ITERS = int(self.spn_smooth.value())
        except Exception:
            return
        gaps = _all_gapfills()
        if not gaps:
            return
        try:
            cmds.undoInfo(swf=False)
        except Exception:
            pass
        try:
            for b in gaps:
                _update_gapfill_weights(b)
        finally:
            try:
                cmds.undoInfo(swf=True)
            except Exception:
                pass

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
        is_edge = cmds.attributeQuery(EDGE_TAG, node=line, exists=True)
        is_fres = cmds.attributeQuery(FRES_TAG, node=line, exists=True)
        is_scrn = cmds.attributeQuery(SCRN_TAG, node=line, exists=True)
        if is_scrn:
            suffix = "  [スクリーン]"
        elif is_fres:
            suffix = "  [フレネル]"
        elif is_edge:
            suffix = "  [エッジ]"
        else:
            suffix = "  [背面]"
        label = _short(line) + suffix
        it = QtWidgets.QTreeWidgetItem([label, "", ""])
        it.setData(0, QtCore.Qt.UserRole, line)
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
                sh = self._shape_of(line)
                if not sh:
                    continue
                # フレネル/スクリーンは dx11Shader の lineColor uniform を直接変更（SG は差し替えない）
                if (cmds.attributeQuery(FRES_TAG, node=line, exists=True)
                        or cmds.attributeQuery(SCRN_TAG, node=line, exists=True)):
                    shd = _fresnel_shader(line)
                    if shd and cmds.attributeQuery(FRES_COLOR, node=shd, exists=True):
                        cmds.setAttr(shd + "." + FRES_COLOR, rgb[0], rgb[1], rgb[2], type="double3")
                    continue
                ss, sg = self._ensure_line_shader(line)
                cmds.setAttr(ss + ".outColor", rgb[0], rgb[1], rgb[2], type="double3")
                cmds.sets(sh, e=True, forceElement=sg)
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
        # 両面表示にする（背面法にしない）。スカートの壁＝カメラを向いた面を見せて隙間を線色で塞ぐ。
        # 背面法（doubleSided=0）だと壁がカリングされて二重線＋隙間になるため使わない。
        try:
            cmds.setAttr(bshape + ".doubleSided", 1)
            cmds.setAttr(bshape + ".opposite", 0)
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
        """ライン・グループともダブルクリックでリネーム。"""
        node = item.data(0, QtCore.Qt.UserRole)
        if not node or not cmds.objExists(node):
            return
        if (cmds.attributeQuery(TAG, node=node, exists=True)
                or cmds.attributeQuery(GROUP_TAG, node=node, exists=True)):
            self.rename_node(node)

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
