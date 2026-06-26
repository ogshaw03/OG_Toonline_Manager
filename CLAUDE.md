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
- 設計・残課題・ハマりどころ: `toon_outline_handoff.md`（必読）

### 実装上の最重要ポイント（原点バグ対策）

handoff §3 の「ヒストリを積んでから worldMesh を差し替える」方式は、
差し替えが output に伝播せず輪郭が原点に生成される不具合が再発した。
現行の `create_outlines()` の正しい手順:

```
元.worldMesh[0] → polyMoveVertex(押し出し) → polyNormal(反転) → ライン shape.inMesh
```

1. ライン側トランスフォームを T0/R0/S1、shape のヒストリを削除して inMesh を空ける。
2. **先に `worldMesh[0]` を shape の inMesh に直結**してライブ追従を確立（原点バグ回避）。
3. その後 **コマンド形式** `cmds.polyMoveVertex(...)` / `cmds.polyNormal(...)` で
   押し出し→反転を挿入。コマンドは追従チェーンを保ったまま挿入する。

注意: `createNode("polyMoveVertex")` で手動構築すると **頂点ごとの法線フレームが
張られず localTranslateZ が法線方向に膨らまない**（元メッシュと重なって z-fighting で
表示が乱れる）。必ずコマンド形式 `cmds.polyMoveVertex` を使うこと。
また連結順は「追加した順に shape 側へ積まれる」ため、push を先・reverse を後に追加して
`worldMesh → push → reverse → shape` の順にする（逆だと内側に縮む）。

### 互換性の注意

PySide2 系の旧 Maya（2019/2020）は Python 2.7。
`*args` の後ろにキーワード引数を置く呼び出し（例: `cmds.setAttr(attr, *vals, type=...)`）は
Python 2 で構文エラーになるため使わない。引数は明示的に展開する。
