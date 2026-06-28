# -*- coding: utf-8 -*-
"""
Follow Face Patch
=================
選択したポリゴンフェースを複製し、元メッシュの変形・移動に追従する独立メッシュ
（パッチ）を作成するツール。Maya の押し出しなどと同じく、生成後もヒストリ
（チャンネルボックス）からオフセットなどの数値を調整できる。

仕組み（OG_Toonline_Manager の手法を応用）:
  1. 元メッシュを全トポロジで複製（履歴は一度削除して静的化）。
  2. `polyDelFacet` で「選択していないフェース」を削除する履歴ノードを作る。
  3. その `inputPolymesh` を 元メッシュの `outMesh` に差し替える
     → 削除が変形後メッシュに毎フレーム再適用され、選択フェースだけが追従。
  4. `textureDeformer`(direction="Normal") の offset で法線方向オフセット。
     transform の `followOffset` 属性に接続し、チャンネルボックスから調整可能。
  5. transform は `parentConstraint`+`scaleConstraint` で元に追従。

使い方:
    import Follow_Face_Patch
    Follow_Face_Patch.show()
  もしくはフェース選択後:
    Follow_Face_Patch.create_patch()
"""
import maya.cmds as cmds
import maya.OpenMayaUI as omui

try:
    from PySide6 import QtWidgets, QtCore
    from shiboken6 import wrapInstance
except ImportError:
    from PySide2 import QtWidgets, QtCore
    from shiboken2 import wrapInstance

TAG = "isFollowPatch"          # パッチ識別タグ
OFFSET_ATTR = "followOffset"   # チャンネルボックスで調整するオフセット
HOLDER = "followPatch_grp"     # 格納グループ
WINDOW_OBJ = "FollowFacePatchWin"


def _maya_main():
    return wrapInstance(int(omui.MQtUtil.mainWindow()), QtWidgets.QWidget)


def _short(n):
    return n.split("|")[-1]


def _resolve_mesh(node):
    """コンポーネント名の左辺（transform/shape どちらでも）から (transform, ni-shape) を返す。"""
    if cmds.nodeType(node) == "mesh":
        shape = node
        par = cmds.listRelatives(node, parent=True, fullPath=True) or []
        tform = par[0] if par else node
    else:
        tform = node
        shps = cmds.listRelatives(node, shapes=True, type="mesh", ni=True, fullPath=True) or []
        shape = shps[0] if shps else None
    return tform, shape


def _tuck_handle(defm):
    """textureDeformer のハンドルをビューポート/アウトライナーから隠す。"""
    for c in (cmds.listConnections(defm, type="transform") or []):
        if "textureDeformerHandle" in _short(c):
            for fn in (lambda: cmds.setAttr(c + ".visibility", 0),
                       lambda: cmds.setAttr(c + ".hiddenInOutliner", 1)):
                try:
                    fn()
                except Exception:
                    pass


def _ensure_holder():
    if not cmds.objExists(HOLDER):
        cmds.group(em=True, name=HOLDER)
    return HOLDER


def create_patch(offset=0.0):
    """選択フェースを複製して元メッシュに追従するパッチを作成。生成したパッチ名のリストを返す。"""
    faces = cmds.filterExpand(cmds.ls(sl=True, fl=True) or [], sm=34) or []
    if not faces:
        cmds.warning("複製するポリゴンフェースを選択してください"); return []

    # フェースをメッシュ単位にまとめる
    by_obj = {}
    for f in faces:
        if ".f[" not in f:
            continue
        obj, idx = f.split(".f[")
        by_obj.setdefault(obj, set()).add(int(idx[:-1]))

    made = []
    cmds.undoInfo(openChunk=True)
    try:
        for obj, sel_idx in by_obj.items():
            tform, shape = _resolve_mesh(obj)
            if not shape:
                continue
            nface = cmds.polyEvaluate(shape, face=True)
            if not isinstance(nface, int):
                continue
            complement = [i for i in range(nface) if i not in sel_idx]

            # 1) 全トポロジで複製 → 子transform除去 → 履歴削除で静的化
            patch = cmds.duplicate(tform, name=_short(tform) + "_patch", rr=True)[0]
            for k in cmds.listRelatives(patch, children=True, type="transform", fullPath=True) or []:
                cmds.delete(k)
            cmds.delete(patch, constructionHistory=True)
            pshape = cmds.listRelatives(patch, shapes=True, type="mesh", ni=True, fullPath=True)[0]

            # 2) 選択外フェースを polyDelFacet で削除（履歴を残す）
            del_node = None
            if complement:
                facelist = ["%s.f[%d]" % (patch, i) for i in complement]
                del_node = cmds.polyDelFacet(facelist, ch=True)[0]
                # 3) 削除ノードの入力を 元メッシュの outMesh に差し替え → 変形追従
                #    （削除が変形後メッシュへ毎フレーム再適用され、選択フェースが追従）
                inp = del_node + ".inputPolymesh"
                for s in (cmds.listConnections(inp, s=True, d=False, p=True) or []):
                    try:
                        cmds.disconnectAttr(s, inp)
                    except Exception:
                        pass
                try:
                    cmds.connectAttr(shape + ".outMesh", inp, f=True)
                except Exception:
                    cmds.warning("変形追従の接続に失敗（静的なパッチとして生成）")

            # 4) 法線方向オフセット（textureDeformer）。offset を followOffset 属性で駆動
            td = cmds.textureDeformer(pshape, strength=0, offset=offset, direction="Normal")
            defm = td[0]
            # 全フェース選択時は polyDelFacet が無いので、ハル方式で deformer のベース入力に
            # 元 outMesh を流して追従させる（inMesh 直結は評価が壊れて歪むため使わない）。
            if del_node is None:
                try:
                    cmds.connectAttr(shape + ".outMesh", defm + ".input[0].inputGeometry", f=True)
                except Exception:
                    cmds.warning("変形追従の接続に失敗（静的なパッチとして生成）")
            _tuck_handle(defm)
            if not cmds.attributeQuery(OFFSET_ATTR, node=patch, exists=True):
                cmds.addAttr(patch, ln=OFFSET_ATTR, at="double", dv=offset, keyable=True)
            try:
                cmds.setAttr(patch + "." + OFFSET_ATTR, offset)
                cmds.connectAttr(patch + "." + OFFSET_ATTR, defm + ".offset", f=True)
            except Exception:
                pass

            # 5) transform 追従（移動/回転/スケール）
            try:
                cmds.parentConstraint(tform, patch, mo=False)
                cmds.scaleConstraint(tform, patch, mo=False)
            except Exception:
                pass

            if not cmds.attributeQuery(TAG, node=patch, exists=True):
                cmds.addAttr(patch, ln=TAG, at="bool", dv=True)
            patch = cmds.parent(patch, _ensure_holder())[0]
            made.append(patch)
        if made:
            cmds.select(made, r=True)
    finally:
        cmds.undoInfo(closeChunk=True)
    return made


def _managed_patches():
    return [t for t in (cmds.ls(type="transform") or [])
            if cmds.attributeQuery(TAG, node=t, exists=True)]


def _selected_patches():
    return [t for t in (cmds.ls(sl=True, long=True, type="transform") or [])
            if cmds.attributeQuery(TAG, node=t, exists=True)]


class FollowPatchUI(QtWidgets.QDialog):

    def __init__(self, parent=None):
        super(FollowPatchUI, self).__init__(parent or _maya_main())
        self.setWindowTitle("Follow Face Patch")
        self.setObjectName(WINDOW_OBJ)
        self.setMinimumWidth(300)
        self._build()

    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)

        self.btn = QtWidgets.QPushButton("選択フェースを複製して追従パッチを作成")
        self.btn.clicked.connect(self._on_create)
        lay.addWidget(self.btn)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("オフセット"))
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setRange(-2000, 2000); self.slider.setValue(0)
        self.spin = QtWidgets.QDoubleSpinBox()
        self.spin.setDecimals(3); self.spin.setRange(-2.0, 2.0); self.spin.setSingleStep(0.01)
        self.slider.valueChanged.connect(self._on_slider)
        self.spin.valueChanged.connect(self._on_spin)
        row.addWidget(self.slider); row.addWidget(self.spin)
        lay.addLayout(row)

        self.lbl = QtWidgets.QLabel("※ オフセットは選択中のパッチに適用。\n"
                                    "生成後はチャンネルボックスの followOffset でも調整できます。")
        self.lbl.setStyleSheet("color:#888;")
        lay.addWidget(self.lbl)

    def _on_create(self):
        made = create_patch(self.spin.value())
        if made:
            self._sync_from_selection()

    def _on_slider(self, v):
        val = v / 1000.0
        self.spin.blockSignals(True); self.spin.setValue(val); self.spin.blockSignals(False)
        self._apply_offset(val)

    def _on_spin(self, val):
        self.slider.blockSignals(True); self.slider.setValue(int(val * 1000)); self.slider.blockSignals(False)
        self._apply_offset(val)

    def _apply_offset(self, val):
        patches = _selected_patches()
        if not patches:
            return
        cmds.undoInfo(openChunk=True)
        try:
            for p in patches:
                if cmds.attributeQuery(OFFSET_ATTR, node=p, exists=True):
                    try:
                        cmds.setAttr(p + "." + OFFSET_ATTR, val)
                    except Exception:
                        cmds.warning("{} の {} は接続/ロックのため変更できません"
                                     .format(_short(p), OFFSET_ATTR))
        finally:
            cmds.undoInfo(closeChunk=True)

    def _sync_from_selection(self):
        patches = _selected_patches()
        if not patches:
            return
        try:
            v = cmds.getAttr(patches[0] + "." + OFFSET_ATTR)
        except Exception:
            return
        self.spin.blockSignals(True); self.slider.blockSignals(True)
        self.spin.setValue(v); self.slider.setValue(int(v * 1000))
        self.spin.blockSignals(False); self.slider.blockSignals(False)


_win = None


def show():
    global _win
    try:
        _win.close(); _win.deleteLater()
    except Exception:
        pass
    for w in QtWidgets.QApplication.topLevelWidgets():
        try:
            if w.objectName() == WINDOW_OBJ:
                w.close(); w.deleteLater()
        except Exception:
            pass
    _win = FollowPatchUI()
    _win.show()
    return _win


if __name__ == "__main__":
    show()
