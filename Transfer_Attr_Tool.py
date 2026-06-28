# -*- coding: utf-8 -*-
"""
Transfer Attribute Tool
=======================
選択オブジェクトの transferAttributes（属性転写）ヒストリを一覧表示し、
転写結果を**焼き付け（ベイク）してから**該当ヒストリを削除するツール。

背景:
  transferAttributes はライブなヒストリノードのため、ノードをそのまま削除すると
  転写前の状態に戻ってしまう。先に評価結果をメッシュへ焼き付ける必要がある。

提供する操作:
  - 非デフォーマヒストリとしてベイク削除（推奨）:
      `bakePartialHistory -prePostDeformers`（= Edit > Delete by Type > Non-Deformer History）
      構築ヒストリ（transferAttributes 含む）をメッシュへ焼き付けて除去し、
      スキン等のデフォーマは保持する。転写結果は残る。
  - 全ヒストリをベイク削除:
      `delete -constructionHistory`（= Delete History）。デフォーマも含め全消去。

注意: Maya 仕様上「特定の1ノードだけを焼いて他の構築ヒストリは温存」はできない。
  構築ヒストリはまとめてベイクされる（デフォーマは保持される）。

使い方:
    import Transfer_Attr_Tool
    Transfer_Attr_Tool.show()
"""
import maya.cmds as cmds
import maya.OpenMayaUI as omui

try:
    from PySide6 import QtWidgets, QtCore
    from shiboken6 import wrapInstance
except ImportError:
    from PySide2 import QtWidgets, QtCore
    from shiboken2 import wrapInstance

WINDOW_OBJ = "TransferAttrToolWin"


def _maya_main():
    return wrapInstance(int(omui.MQtUtil.mainWindow()), QtWidgets.QWidget)


def _short(n):
    return n.split("|")[-1]


def _transfer_nodes(obj):
    """obj のヒストリ内の transferAttributes ノード一覧。"""
    return list(set(cmds.ls(cmds.listHistory(obj) or [], type="transferAttributes") or []))


def _selected_meshes():
    """選択トランスフォーム/メッシュのうち mesh シェイプを持つ transform。"""
    out = []
    for n in (cmds.ls(sl=True, long=True) or []):
        t = n
        if cmds.nodeType(n) == "mesh":
            par = cmds.listRelatives(n, parent=True, fullPath=True) or []
            t = par[0] if par else n
        shp = cmds.listRelatives(t, shapes=True, type="mesh", ni=True) or []
        if shp and t not in out:
            out.append(t)
    return out


def bake_nondeformer(objs):
    """非デフォーマヒストリ（transferAttributes 含む）をベイクして削除。デフォーマは保持。"""
    done = 0
    cmds.undoInfo(openChunk=True)
    try:
        for o in objs:
            try:
                cmds.bakePartialHistory(o, prePostDeformers=True)
                done += 1
            except Exception:
                cmds.warning("{} のベイクに失敗しました".format(_short(o)))
    finally:
        cmds.undoInfo(closeChunk=True)
    return done


def bake_all_history(objs):
    """全構築ヒストリをベイクして削除（Delete History 相当）。"""
    done = 0
    cmds.undoInfo(openChunk=True)
    try:
        for o in objs:
            try:
                cmds.delete(o, constructionHistory=True)
                done += 1
            except Exception:
                cmds.warning("{} のヒストリ削除に失敗しました".format(_short(o)))
    finally:
        cmds.undoInfo(closeChunk=True)
    return done


class TransferAttrUI(QtWidgets.QDialog):

    def __init__(self, parent=None):
        super(TransferAttrUI, self).__init__(parent or _maya_main())
        self.setWindowTitle("Transfer Attribute Tool")
        self.setObjectName(WINDOW_OBJ)
        self.setMinimumWidth(360)
        self.setMinimumHeight(360)
        self._build()
        self.refresh()

    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)

        b_ref = QtWidgets.QPushButton("選択から再取得")
        b_ref.clicked.connect(self.refresh)
        lay.addWidget(b_ref)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["オブジェクト / transferAttributes"])
        lay.addWidget(self.tree, 1)

        b1 = QtWidgets.QPushButton("ベイクして削除（非デフォーマヒストリ・推奨）")
        b1.setToolTip("転写結果を焼き付けて transferAttributes 等の構築ヒストリを削除。"
                      "スキン等のデフォーマは保持します。")
        b1.clicked.connect(self._on_bake_nd)
        lay.addWidget(b1)

        b2 = QtWidgets.QPushButton("全ヒストリをベイクして削除")
        b2.setToolTip("構築ヒストリもデフォーマも含めて全削除（Delete History 相当）。")
        b2.clicked.connect(self._on_bake_all)
        lay.addWidget(b2)

        self.lbl = QtWidgets.QLabel(
            "※ ノードをそのまま削除すると転写前に戻ります。必ずベイクで焼き付けてから削除します。\n"
            "※ Maya 仕様上、特定の1ノードだけ焼いて他の構築ヒストリを残すことはできません。")
        self.lbl.setStyleSheet("color:#888;")
        self.lbl.setWordWrap(True)
        lay.addWidget(self.lbl)

    def refresh(self):
        self.tree.clear()
        for o in _selected_meshes():
            nodes = _transfer_nodes(o)
            it = QtWidgets.QTreeWidgetItem([_short(o)
                                           + ("" if nodes else "  （transferAttributes なし）")])
            for n in nodes:
                it.addChild(QtWidgets.QTreeWidgetItem([_short(n)]))
            it.setExpanded(True)
            self.tree.addTopLevelItem(it)

    def _on_bake_nd(self):
        objs = _selected_meshes()
        if not objs:
            cmds.warning("オブジェクトを選択してください"); return
        n = bake_nondeformer(objs)
        self.refresh()
        if n:
            cmds.inViewMessage(amg="{} 個をベイク削除（デフォーマ保持）".format(n),
                               pos="midCenter", fade=True) if hasattr(cmds, "inViewMessage") else None

    def _on_bake_all(self):
        objs = _selected_meshes()
        if not objs:
            cmds.warning("オブジェクトを選択してください"); return
        res = QtWidgets.QMessageBox.warning(
            self, "全ヒストリ削除",
            "選択オブジェクトの全ヒストリ（デフォーマ含む）をベイクして削除します。\n"
            "よろしいですか？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No)
        if res != QtWidgets.QMessageBox.Yes:
            return
        bake_all_history(objs)
        self.refresh()


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
    _win = TransferAttrUI()
    _win.show()
    return _win


if __name__ == "__main__":
    show()
