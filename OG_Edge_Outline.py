# -*- coding: utf-8 -*-
"""
OG Edge Outline  (screen-space depth-edge outline / VP2 render override)
=======================================================================
ビューポート全体の後処理として、**深度（と任意で法線）の不連続を検出**して輪郭線を描く。
フレネルと違い「カメラに寝た面」全部ではなく、**実際の重なり/シルエット/折れ目の縁だけ**に線が出る。

- VP2（DirectX11）/ Hardware 2.0 前提・カメラ依存。
- per-line ではなく**画面全体に1つの効果**（色・太さ・しきい値はグローバル）。
- プラグイン不要。`MRenderOverride` を実行時登録してアクティブパネルに適用する。

使い方:
    import OG_Edge_Outline
    OG_Edge_Outline.enable()     # 現在のパネルに適用
    OG_Edge_Outline.disable()    # 解除
    OG_Edge_Outline.set_params(threshold=0.0008, thickness=1.0, color=(0,0,0))

※ 実験的実装。環境により調整が必要です。エラーは Script Editor の出力をご確認ください。
"""
import os
import maya.cmds as cmds
import maya.api.OpenMaya as om
import maya.api.OpenMayaRender as omr


def maya_useNewAPI():
    pass


OVR_NAME = "OG_EdgeOutlineOverride"

# 全画面クアッド用 HLSL（深度ラプラシアンでエッジ検出 → シーン色に線色を合成）
_FX = """// OG Edge Outline - fullscreen depth edge detect
Texture2D gColorTex;
Texture2D gDepthTex;

SamplerState gSamp
{
    Filter = MIN_MAG_MIP_POINT;
    AddressU = Clamp;
    AddressV = Clamp;
};

float2 gTexel    = {0.001f, 0.001f};   // 1/width, 1/height * thickness
float  gThreshold = 0.0008f;           // エッジ判定のしきい値（深度差）
float3 gLineColor = {0.0f, 0.0f, 0.0f};

struct VIN  { float3 Pos : POSITION; float2 UV : TEXCOORD0; };
struct VOUT { float4 Pos : SV_Position; float2 UV : TEXCOORD0; };

VOUT VS(VIN i)
{
    VOUT o;
    o.Pos = float4(i.Pos, 1.0f);
    o.UV  = i.UV;
    return o;
}

float4 PS(VOUT i) : SV_Target
{
    float2 t = gTexel;
    float c  = gDepthTex.Sample(gSamp, i.UV).r;
    float u  = gDepthTex.Sample(gSamp, i.UV + float2(0.0f, -t.y)).r;
    float d  = gDepthTex.Sample(gSamp, i.UV + float2(0.0f,  t.y)).r;
    float l  = gDepthTex.Sample(gSamp, i.UV + float2(-t.x, 0.0f)).r;
    float r  = gDepthTex.Sample(gSamp, i.UV + float2( t.x, 0.0f)).r;
    float edge = abs(u + d + l + r - 4.0f * c);   // ラプラシアン
    float3 scene = gColorTex.Sample(gSamp, i.UV).rgb;
    float e = step(gThreshold, edge);
    return float4(lerp(scene, gLineColor, e), 1.0f);
}

technique11 Main
{
    pass p0
    {
        SetVertexShader(CompileShader(vs_5_0, VS()));
        SetPixelShader(CompileShader(ps_5_0, PS()));
    }
}
"""


def _fx_path():
    d = os.path.join(cmds.internalVar(userAppDir=True), "OG_Toonline_Manager")
    try:
        if not os.path.isdir(d):
            os.makedirs(d)
    except Exception:
        d = cmds.internalVar(userTmpDir=True)
    p = os.path.join(d, "OG_EdgeOutline.fx").replace("\\", "/")
    try:
        with open(p, "w") as f:
            f.write(_FX)
    except Exception:
        pass
    return p


# パラメータ（グローバル）
_PARAMS = {"threshold": 0.0008, "thickness": 1.0, "color": (0.0, 0.0, 0.0)}


class _SceneRender(omr.MSceneRender):
    def __init__(self, name, ovr):
        omr.MSceneRender.__init__(self, name)
        self.ovr = ovr

    def targetOverrideList(self):
        return [self.ovr.tColor, self.ovr.tDepth]

    def clearOperation(self):
        c = self.mClearOperation
        c.setClearGradient(False)
        c.setMask(omr.MClearOperation.kClearAll)
        return c


class _QuadEdge(omr.MQuadRender):
    def __init__(self, name, ovr):
        omr.MQuadRender.__init__(self, name)
        self.ovr = ovr
        self.shaderInst = None

    def targetOverrideList(self):
        # 画面（既定の出力ターゲット）へ描く
        return None

    def shader(self):
        if self.shaderInst is None:
            sm = omr.MRenderer.getShaderManager()
            if sm is None:
                return None
            self.shaderInst = sm.getEffectsFileShader(_fx_path(), "Main")
        s = self.shaderInst
        if s is None:
            return None
        try:
            # 入力テクスチャ（色・深度）をバインド
            s.setParameter("gColorTex", self.ovr.tColor)
            s.setParameter("gDepthTex", self.ovr.tDepth)
            w = max(1, self.ovr.w)
            h = max(1, self.ovr.h)
            th = max(0.0, _PARAMS["thickness"])
            s.setParameter("gTexel", (th / float(w), th / float(h)))
            s.setParameter("gThreshold", float(_PARAMS["threshold"]))
            col = _PARAMS["color"]
            s.setParameter("gLineColor", (col[0], col[1], col[2]))
        except Exception as e:
            om.MGlobal.displayWarning("[OG_Edge_Outline] shader param error: {}".format(e))
        return s


class EdgeRenderOverride(omr.MRenderOverride):
    def __init__(self, name):
        omr.MRenderOverride.__init__(self, name)
        self.w = 0
        self.h = 0
        self.tColor = None
        self.tDepth = None
        self._tmgr = omr.MRenderer.getRenderTargetManager()
        # ターゲット記述（サイズは setup で更新）
        self._colorDesc = omr.MRenderTargetDescription(
            "OG_edgeColor", 256, 256, 1, omr.MRenderer.kR8G8B8A8_UNORM, 0, False)
        self._depthDesc = omr.MRenderTargetDescription(
            "OG_edgeDepth", 256, 256, 1, omr.MRenderer.kD24S8, 0, False)
        self._scene = _SceneRender("og_edge_scene", self)
        self._quad = _QuadEdge("og_edge_quad", self)
        self._hud = omr.MHUDRender()
        self._present = omr.MPresentTarget("og_edge_present")
        self._ops = [self._scene, self._quad, self._hud, self._present]
        self._iter = 0

    def supportedDrawAPIs(self):
        return omr.MRenderer.kAllDevices

    def setup(self, destination):
        # ターゲットサイズをパネルに合わせて更新
        try:
            tgt = omr.MRenderer.outputTargetSize()
            self.w, self.h = int(tgt[0]), int(tgt[1])
        except Exception:
            self.w, self.h = 1280, 720
        self._colorDesc.setWidth(self.w); self._colorDesc.setHeight(self.h)
        self._depthDesc.setWidth(self.w); self._depthDesc.setHeight(self.h)
        if self.tColor is None:
            self.tColor = self._tmgr.acquireRenderTarget(self._colorDesc)
        else:
            self.tColor.updateDescription(self._colorDesc)
        if self.tDepth is None:
            self.tDepth = self._tmgr.acquireRenderTarget(self._depthDesc)
        else:
            self.tDepth.updateDescription(self._depthDesc)

    def cleanup(self):
        pass

    def startOperationIterator(self):
        self._iter = 0
        return True

    def renderOperation(self):
        return self._ops[self._iter]

    def nextRenderOperation(self):
        self._iter += 1
        return self._iter < len(self._ops)

    def release(self):
        if self.tColor is not None:
            self._tmgr.releaseRenderTarget(self.tColor); self.tColor = None
        if self.tDepth is not None:
            self._tmgr.releaseRenderTarget(self.tDepth); self.tDepth = None


_override = None


def _active_panel():
    p = cmds.getPanel(withFocus=True)
    if p and cmds.getPanel(typeOf=p) == "modelPanel":
        return p
    for p in (cmds.getPanel(type="modelPanel") or []):
        return p
    return None


def enable():
    """現在のモデルパネルにエッジ輪郭オーバーライドを適用。"""
    global _override
    if _override is None:
        _override = EdgeRenderOverride(OVR_NAME)
        omr.MRenderer.registerOverride(_override)
    panel = _active_panel()
    if not panel:
        cmds.warning("モデルパネルが見つかりません"); return
    cmds.modelEditor(panel, e=True, rendererOverrideName=OVR_NAME)
    cmds.refresh()


def disable():
    """エッジ輪郭オーバーライドを解除。"""
    global _override
    for panel in (cmds.getPanel(type="modelPanel") or []):
        try:
            if cmds.modelEditor(panel, q=True, rendererOverrideName=True) == OVR_NAME:
                cmds.modelEditor(panel, e=True, rendererOverrideName="")
        except Exception:
            pass
    if _override is not None:
        try:
            omr.MRenderer.deregisterOverride(_override)
        except Exception:
            pass
        try:
            _override.release()
        except Exception:
            pass
        _override = None
    cmds.refresh()


def set_params(threshold=None, thickness=None, color=None):
    """エッジ検出のパラメータを設定（しきい値/太さ/線色）。"""
    if threshold is not None:
        _PARAMS["threshold"] = float(threshold)
    if thickness is not None:
        _PARAMS["thickness"] = float(thickness)
    if color is not None:
        _PARAMS["color"] = (color[0], color[1], color[2])
    cmds.refresh()
