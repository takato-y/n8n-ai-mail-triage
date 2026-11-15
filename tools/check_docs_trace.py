#!/usr/bin/env python3
"""設計書のエラーIDと障害対応手順書を突き合わせる（n8n を使わずに動く文書の検査）。

  python3 tools/check_docs_trace.py

確かめること:
  - フロー設計書（docs/03_フロー設計.md）に出てくる ERR-xxx が、
    障害対応手順書（docs/06_障害対応手順書.md）の 4 章の見出しと 7 章の対応表のすべてに出てくる
  - 障害対応手順書に、フロー設計書にない ERR-xxx が出てこない
終了コード: 0=合格、1=不合格、2=検査できない（ファイル・章が見つからない）
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DESIGN_DOCS = [ROOT / "docs" / "03_フロー設計.md"]
RUNBOOK = ROOT / "docs" / "06_障害対応手順書.md"
ERR_ID = re.compile(r"\bERR-\d+\b")


def chapter(text, number):
    """「## N. 」で始まる章の本文を返す（次の「## 」の手前まで）。見つからなければ None。"""
    m = re.search(rf"^## {number}\. .*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1) if m else None


def main():
    try:
        design_ids = set()
        for path in DESIGN_DOCS:
            design_ids |= set(ERR_ID.findall(path.read_text(encoding="utf-8")))
        text = RUNBOOK.read_text(encoding="utf-8")
    except OSError as e:
        print(f"検査できません: {e}")
        return 2
    ch4, ch7 = chapter(text, 4), chapter(text, 7)
    if not design_ids or ch4 is None or ch7 is None:
        # 検査の前提が崩れているのに「合格」と出さない
        print("検査できません: 設計書のエラーID、または障害対応手順書の 4 章・7 章が見つかりません")
        return 2

    # 4 章：エラーIDごとの見出し（### 4.n ERR-xxx 名称）、7 章：対応表の 1 列目
    headings = set(re.findall(r"^### (?:\d+\.\d+ )?(ERR-\d+) ", ch4, re.M))
    table_ids = set(re.findall(r"^\| (ERR-\d+) \|", ch7, re.M))
    problems = []
    for label, missing in (("4 章に対応手順の節がない", design_ids - headings),
                           ("7 章の対応表にない", design_ids - table_ids),
                           ("設計書にない", set(ERR_ID.findall(text)) - design_ids)):
        if missing:
            problems.append(f"{label}エラーID: {', '.join(sorted(missing))}")

    names = "・".join(p.name for p in DESIGN_DOCS)
    if problems:
        print("不合格")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"合格: {names} のエラーID {len(design_ids)} 件（{', '.join(sorted(design_ids))}）が、"
          f"{RUNBOOK.name} の 4 章の見出しと 7 章の対応表にそろっています")
    return 0


if __name__ == "__main__":
    sys.exit(main())
