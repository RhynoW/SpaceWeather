"""tools/cssi_compare.py — CSSI 匯出檔與來源實檔的逐行比對（可稽核版）。

README 宣稱「對 CelesTrak 實檔比對 N/M 行一致」。這個宣稱若沒有可重跑的比對
程序，稽核者無從複核，作者也無從發現回歸——本專案就曾因此讓匯出從 2,279 行
一致靜默掉到 252 行而未被察覺。此工具把該宣稱變成單一指令。

比對語意（刻意寫死，避免「換個判準就一致」的自欺）：
  · 比對對象  三個區段（OBSERVED／DAILY_PREDICTED／MONTHLY_PREDICTED）全比，
              以「(區段, 日期)」配對而非行序。區段歸屬本身也是比對項——同一天
              若從觀測掉進預測區，會被算成 missing + extra 而不會被當成一致。
              來源多出或本系統多出的列分別計為 missing / extra。
  · 比對層級  預設 --level byte：以 CSSI 欄寬右補空白後做**位元組相等**比較。
              這是最嚴格的判準，等同「STK 讀到的每個字元都一樣」。
              --level field 改為逐欄比數值（整數相等、浮點以絕對容差比），
              用來診斷差異落在哪一欄，不作為一致性宣稱的依據。
  · 浮點處理  byte 模式不做任何數值容差；field 模式的容差見 FLOAT_TOL，
              且因 CSSI 全為定點格式（F6.1 等），容差設 0 即可通過，
              設定為 0.0 就是明示「不靠容差換取一致」。
  · 未定稿列  來源檔的 `UPDATED` 標頭給出該快照的產製時刻。**日期 ≥ 該時刻當日
              的列仍會被 CelesTrak 持續修訂**：邊界觀測日會補值，近期預測日會
              隨實測進來而改寫（實測即見過 12:00 格從預測 Kp 3.0 改為實測 0.7）。
              這類差異不是格式錯誤，但也不該混進一致性數字裡蓋掉真問題，因此
              **分開列**。判準取自來源檔自身的 UPDATED，不取執行日——否則同一份
              快照在不同日子跑會得到不同結論，數字就不可稽核。
              注意：此類差異中，較新的一側未必是來源檔。

用法：
    python tools/cssi_compare.py                          # 對 data/seed/SW-All.txt
    python tools/cssi_compare.py --ref path/to/SW-All.txt
    python tools/cssi_compare.py --level field --show 20  # 診斷差異落在哪一欄
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from swx_core import SwxStore, cssi  # noqa: E402

FLOAT_TOL = 0.0  # 定點格式，不需容差；設 0 是刻意的
DEFAULT_REF = Path("data/seed/SW-All.txt")


SECTIONS = ("OBSERVED", "DAILY_PREDICTED", "MONTHLY_PREDICTED")


def _updated_date(text: str) -> pd.Timestamp | None:
    """由 `UPDATED 2026 Aug 17 12:34:57 UTC` 標頭取快照日期。

    這是「哪些列仍可能被來源修訂」的唯一判準。取檔案自身而非執行日，
    是為了讓同一份快照無論何時重跑都得到同一個結論。
    """
    for line in text.splitlines():
        if line.startswith("UPDATED "):
            try:
                return pd.to_datetime(line[len("UPDATED "):].replace("UTC", "").strip()).normalize()
            except (ValueError, TypeError):
                return None
        if line.startswith("BEGIN "):
            break
    return None


def _indexed(df: pd.DataFrame) -> pd.DataFrame:
    """以 (區段, 日期) 為索引，讓區段歸屬本身也成為比對項。"""
    out = df.copy()
    out["date"] = pd.to_datetime(
        dict(year=out["year"], month=out["month"], day=out["day"])
    )
    return out.set_index(["section", "date"]).sort_index()


def _byte_diff(ours: pd.Series, theirs: pd.Series) -> str | None:
    a = cssi.format_line(ours)
    b = cssi.format_line(theirs)
    return None if a == b else f"\n    本系統 |{a}|\n    來源   |{b}|"


def _field_diff(ours: pd.Series, theirs: pd.Series) -> str | None:
    bad = []
    for name, _start, _end, kind in cssi.FIELDS:
        u, v = ours.get(name), theirs.get(name)
        if pd.isna(u) and pd.isna(v):
            continue
        if pd.isna(u) or pd.isna(v):
            bad.append(f"{name}: {u!r} vs {v!r}")
        elif kind == "float":
            if abs(float(u) - float(v)) > FLOAT_TOL:
                bad.append(f"{name}: {float(u):.1f} vs {float(v):.1f}")
        elif int(u) != int(v):
            bad.append(f"{name}: {int(u)} vs {int(v)}")
    return ("  " + "; ".join(bad)) if bad else None


def compare(ref_path: Path, *, level: str = "byte", show: int = 5) -> int:
    ref_text = ref_path.read_text(encoding="utf-8", errors="replace")
    theirs = _indexed(cssi.parse_text(ref_text))

    store = SwxStore()
    from services.exporter import stk_spaceweather

    ours = _indexed(
        cssi.parse_text(
            cssi.write_text(
                stk_spaceweather.build_frame(store, mode=stk_spaceweather.MODE_SOURCE)
            )
        )
    )

    differ = _byte_diff if level == "byte" else _field_diff
    common = theirs.index.intersection(ours.index)
    missing = theirs.index.difference(ours.index)   # 來源有、我們沒有
    extra = ours.index.difference(theirs.index)     # 我們有、來源沒有

    # 未定稿邊界：來源快照的 UPDATED 當日起，來源仍會修訂
    cutoff = _updated_date(ref_text)
    mismatches = [(key, d) for key in common
                  if (d := differ(ours.loc[key], theirs.loc[key])) is not None]
    settled = [m for m in mismatches if cutoff is None or m[0][1] < cutoff]
    unsettled = [m for m in mismatches if cutoff is not None and m[0][1] >= cutoff]

    print(f"比對來源　{ref_path}")
    print(f"比對層級　{level}" + ("（位元組相等，最嚴格）" if level == "byte"
                                  else f"（逐欄，浮點容差 {FLOAT_TOL}）"))
    print(f"來源列數　{len(theirs)}　本系統列數　{len(ours)}　共同列數　{len(common)}")
    print()
    print("  區段                  一致/共同   來源缺   本系統多")
    for sec in SECTIONS:
        c = [k for k in common if k[0] == sec]
        if not c and sec not in theirs.index.get_level_values(0):
            continue
        bad = len([m for m in mismatches if m[0][0] == sec])
        miss = len([k for k in missing if k[0] == sec])
        ext = len([k for k in extra if k[0] == sec])
        print(f"  {sec:<20}{len(c) - bad:>6}/{len(c):<6}{miss:>7}{ext:>10}")
    print(f"  {'合計':<19}{len(common) - len(mismatches):>6}/{len(common):<6}"
          f"{len(missing):>7}{len(extra):>10}")

    if cutoff is not None:
        n_settled = len([k for k in common if k[1] < cutoff])
        print(f"\n  來源快照 UPDATED　{cutoff:%Y-%m-%d}")
        print(f"  已定稿列（日期 < 快照日）　{n_settled - len(settled)}/{n_settled}")
        if unsettled:
            print(f"  未定稿列差異　{len(unsettled)} 筆（來源仍會修訂，不計入一致性宣稱）")

    if mismatches:
        print(f"\n差異明細（前 {min(show, len(mismatches))} 筆）：")
        for (sec, day), detail in mismatches[:show]:
            tag = "　← 未定稿，來源仍會修訂" if cutoff is not None and day >= cutoff else ""
            print(f"  [{sec}] {day:%Y-%m-%d}{tag}{detail}")
    if len(missing):
        print(f"\n來源有而本系統缺的列（前 {min(show, len(missing))} 筆）：")
        for sec, day in list(missing)[:show]:
            print(f"  [{sec}] {day:%Y-%m-%d}")

    ok = not settled and not len(missing)
    print("\n結論：" + ("已定稿列全數一致，且來源無遺漏。" if ok
                        else "已定稿列存在差異或遺漏，需追查。"))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CSSI 匯出檔與來源實檔逐行比對")
    ap.add_argument("--ref", default=str(DEFAULT_REF), help="來源 CSSI 檔")
    ap.add_argument("--level", default="byte", choices=["byte", "field"])
    ap.add_argument("--show", type=int, default=5, help="列出幾筆差異明細")
    args = ap.parse_args(argv)

    ref = Path(args.ref)
    if not ref.exists():
        print(f"找不到來源檔 {ref}；請先執行 "
              f"`python -m services.ingest.run --source celestrak_sw_all`")
        return 2
    return compare(ref, level=args.level, show=args.show)


if __name__ == "__main__":
    raise SystemExit(main())
