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
原点バグは「**複製をワールドへ出して T0/R0/S1 に単位化 + worldMesh 接続**」方式が
原因（worldMesh の接続順を NG/OK どちらにしても再発した）。現行は worldMesh を使わない
方式に変更済み。`create_outlines()` の正しい手順:

```
複製は元と同じ親・同じ TRS のまま（動かさない）
元.outMesh → polyExtrudeFacet(膨らみ) → polyNormal(反転) → ライン shape.inMesh
最後に parent でグループへ（ワールド位置保持で重なりは維持）
```

1. `cmds.duplicate` した複製はトランスフォームを**動かさない**（元と同じ変換空間に置く）。
2. 複製のヒストリを削除して inMesh を空け、**元の `outMesh`（オブジェクト空間の変形後
   メッシュ）** を inMesh に直結。複製は元と同じ TRS なので変形追従しつつ元に重なる。
3. `cmds.polyExtrudeFacet(... f[*], keepFacesTogether=True, localTranslateZ=太さ)` で
   全面を法線方向に膨らませ、続けて `cmds.polyNormal(normalMode=0)` で反転。

太さの実体は **polyExtrudeFace.localTranslateZ**（ライン別に setAttr して制御）。

注意:
- 膨らみに **`polyMoveVertex` は使わない**。localTranslate が選択全体で単一フレームに
  なるため全頂点が一方向へ動き、カプセル状に歪む。`polyExtrudeFacet`(keepFacesTogether)
  は面ごとの法線フレームで押し出すので全方位に均一に膨らむ。
- `createNode` でも面フレームが張られず不可。コマンド形式を使う。
- worldMesh + トランスフォーム単位化方式は原点バグの原因なので使わない。
- 制約: outMesh 追従は元の「変形（スキン等）」には追従するが、元トランスフォーム自体の
  アニメーションには追従しない（生成時のワールド位置で固定）。スキンキャラの通常運用では問題なし。

### 互換性の注意

PySide2 系の旧 Maya（2019/2020）は Python 2.7。
`*args` の後ろにキーワード引数を置く呼び出し（例: `cmds.setAttr(attr, *vals, type=...)`）は
Python 2 で構文エラーになるため使わない。引数は明示的に展開する。
