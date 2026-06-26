# -*- coding: utf-8 -*-
"""
OG Toonline Manager
===================
dx11Shader の MayaToonOutline.fx 相当の輪郭線を、Maya標準機能（実ジオメトリ）だけで再現。
inverted hull 方式（押し出し → 法線反転 → バックフェースカリング）。
元メッシュの変形に自動追従し、スライダーで太さをライブ調整できる。

使い方:
    Script Editor に貼り付けて実行、もしくは
        import OG_Toonline_Manager
        OG_Toonline_Manager.show()

詳細・残課題は toon_outline_handoff.md を参照。
"""
import maya.cmds as cmds
import maya.OpenMayaUI as omui

# ---- PySide6 / PySide2 両対応 ----
try:
    from PySide6 import QtWidgets, QtCore, QtGui
    from shiboken6 import wrapInstance
except ImportError:
    from PySide2 import QtWidgets, QtCore, QtGui
    from shiboken2 import wrapInstance

SHADER = "toonOutline_SS"
SG     = "toonOutline_SG"
GRP    = "toonOutlines_grp"
TAG    = "isToonOutline"


def _maya_main():
    return wrapInstance(int(omui.MQtUtil.mainWindow()), QtWidgets.QWidget)


def _ensure_shader(color):
    """全アウトライン共有のフラットマテリアルを確保。outColor が線の色。"""
    if not cmds.objExists(SHADER):
        cmds.shadingNode("surfaceShader", asShader=True, name=SHADER)
        cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=SG)
        cmds.connectAttr(SHADER + ".outColor", SG + ".surfaceShader", f=True)
        cmds.setAttr(SHADER + ".outColor", color[0], color[1], color[2], type="double3")
    return SHADER, SG


class ToonOutlineUI(QtWidgets.QDialog):

    def __init__(self, parent=None):
        if parent is None:
            parent = _maya_main()
        super(ToonOutlineUI, self).__init__(parent)
        self.setWindowTitle("OG_Toonline_Manager")
        self.setMinimumWidth(300)
        self._move_nodes = []          # 太さ駆動対象の polyMoveVertex ノード
        self._color = [0.0, 0.0, 0.0]
        self._build()
        self._rebuild_cache()

    # ---------- UI ----------
    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)

        self.btn_create = QtWidgets.QPushButton("選択メッシュに輪郭を生成")
        self.btn_create.clicked.connect(self.create_outlines)
        lay.addWidget(self.btn_create)

        # 太さ：スライダー(0–2000 → 0.000–2.000) + スピンボックス同期
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("太さ"))
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setRange(0, 2000)
        self.slider.setValue(50)
        self.spin = QtWidgets.QDoubleSpinBox()
        self.spin.setDecimals(3)
        self.spin.setRange(0.0, 2.0)
        self.spin.setSingleStep(0.01)
        self.spin.setValue(0.05)
        self.slider.valueChanged.connect(self._on_slider)
        self.spin.valueChanged.connect(self._on_spin)
        row.addWidget(self.slider)
        row.addWidget(self.spin)
        lay.addLayout(row)

        # 色
        crow = QtWidgets.QHBoxLayout()
        crow.addWidget(QtWidgets.QLabel("色"))
        self.btn_color = QtWidgets.QPushButton()
        self.btn_color.setFixedHeight(22)
        self.btn_color.clicked.connect(self._pick_color)
        self._refresh_swatch()
        crow.addWidget(self.btn_color)
        lay.addLayout(crow)

        # 管理
        mrow = QtWidgets.QHBoxLayout()
        b_sel = QtWidgets.QPushButton("輪郭を選択")
        b_del = QtWidgets.QPushButton("輪郭を削除")
        b_ref = QtWidgets.QPushButton("再取得")
        b_sel.clicked.connect(self.select_outlines)
        b_del.clicked.connect(self.delete_outlines)
        b_ref.clicked.connect(self._rebuild_cache)
        mrow.addWidget(b_sel); mrow.addWidget(b_del); mrow.addWidget(b_ref)
        lay.addLayout(mrow)

    # ---------- 太さ ----------
    def _on_slider(self, v):
        val = v / 1000.0
        self.spin.blockSignals(True); self.spin.setValue(val); self.spin.blockSignals(False)
        self._apply_thickness(val)

    def _on_spin(self, val):
        self.slider.blockSignals(True); self.slider.setValue(int(val * 1000)); self.slider.blockSignals(False)
        self._apply_thickness(val)

    def _apply_thickness(self, val):
        for m in list(self._move_nodes):
            if cmds.objExists(m):
                try:
                    cmds.setAttr(m + ".localTranslateZ", val)
                except Exception:
                    pass

    # ---------- 色 ----------
    def _refresh_swatch(self):
        r, g, b = [int(c * 255) for c in self._color]
        self.btn_color.setStyleSheet(
            "background-color: rgb({},{},{}); border:1px solid #222;".format(r, g, b))

    def _pick_color(self):
        c0 = QtGui.QColor.fromRgbF(*self._color)
        c = QtWidgets.QColorDialog.getColor(c0, self, "輪郭の色")
        if c.isValid():
            self._color = [c.redF(), c.greenF(), c.blueF()]
            self._refresh_swatch()
            if cmds.objExists(SHADER):
                cmds.setAttr(SHADER + ".outColor",
                             self._color[0], self._color[1], self._color[2],
                             type="double3")

    # ---------- キャッシュ ----------
    def _managed_outlines(self):
        return [t for t in (cmds.ls(type="transform") or [])
                if cmds.attributeQuery(TAG, node=t, exists=True)]

    def _rebuild_cache(self):
        nodes = []
        for o in self._managed_outlines():
            nodes += cmds.ls(cmds.listHistory(o) or [], type="polyMoveVertex")
        self._move_nodes = list(set(nodes))

    # ---------- 生成 ----------
    def create_outlines(self):
        sel = cmds.ls(sl=True, long=True, type="transform")
        if not sel:
            cmds.warning("メッシュを選択してください"); return
        thick = self.spin.value()
        _, sg = _ensure_shader(self._color)
        if not cmds.objExists(GRP):
            cmds.group(em=True, name=GRP)

        cmds.undoInfo(openChunk=True)
        try:
            made = []
            for obj in sel:
                shps = cmds.listRelatives(obj, shapes=True, type="mesh", ni=True, f=True)
                if not shps:
                    continue
                src = shps[0]

                # 複製 → ワールドへ出してトランスフォームを単位化
                dup = cmds.duplicate(obj, name=obj.split("|")[-1] + "_outline", rr=True)[0]
                for k in cmds.listRelatives(dup, children=True, type="transform", f=True) or []:
                    cmds.delete(k)
                if cmds.listRelatives(dup, parent=True):
                    dup = cmds.parent(dup, world=True)[0]
                for at, v in (("t", 0), ("r", 0), ("s", 1)):
                    for ax in "xyz":
                        cmds.setAttr("{}.{}{}".format(dup, at, ax), v)
                dshape = cmds.listRelatives(dup, shapes=True, type="mesh", ni=True, f=True)[0]

                # 1) 先に 押し出し→法線反転 をヒストリとして積む（入力は複製の静的メッシュ）
                #    ※ この順序が重要。worldMesh を先に繋ぐと原点にラインが出る。
                move_node = cmds.polyMoveVertex(dup + ".vtx[*]",
                                                localTranslateZ=thick, ch=True)[0]
                cmds.polyNormal(dshape, normalMode=0, ch=True)

                # 2) ヒストリ先頭の入力を 元メッシュの worldMesh に差し替え → ライブ追従
                cmds.connectAttr(src + ".worldMesh[0]",
                                 move_node + ".inputPolymesh", f=True)

                # 3) バックフェースカリング
                cmds.setAttr(dshape + ".doubleSided", 0)

                cmds.sets(dshape, e=True, forceElement=sg)
                cmds.addAttr(dup, ln=TAG, at="bool", dv=True)
                cmds.parent(dup, GRP)
                made.append(dup)
            if made:
                cmds.select(made, r=True)
        finally:
            cmds.undoInfo(closeChunk=True)

        self._rebuild_cache()
        self._apply_thickness(thick)   # 現在のスライダー値で揃える

    # ---------- 管理 ----------
    def select_outlines(self):
        outs = self._managed_outlines()
        cmds.select(outs, r=True) if outs else cmds.warning("輪郭がありません")

    def delete_outlines(self):
        outs = self._managed_outlines()
        if not outs:
            cmds.warning("輪郭がありません"); return
        cmds.undoInfo(openChunk=True)
        try:
            cmds.delete(outs)
            if cmds.objExists(GRP) and not (cmds.listRelatives(GRP, c=True) or []):
                cmds.delete(GRP)
        finally:
            cmds.undoInfo(closeChunk=True)
        self._rebuild_cache()


_toon_win = None


def show():
    """ツールウィンドウを起動。既存ウィンドウがあれば閉じてから開く。"""
    global _toon_win
    try:
        _toon_win.close(); _toon_win.deleteLater()
    except Exception:
        pass
    _toon_win = ToonOutlineUI()
    _toon_win.show()
    return _toon_win


if __name__ == "__main__":
    show()
