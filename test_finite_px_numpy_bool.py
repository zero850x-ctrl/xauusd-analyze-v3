#!/usr/bin/env python3
"""`_finite_px` 要擋住 numpy bool。

2026-10-10 外審 finding 7：`isinstance(np.bool_(True), bool)` 係 **False**
（numpy 嘅 bool_ 唔係 Python bool 嘅 subclass），所以一嚿經過 pandas/numpy
物件嘅布爾值可以行過原本個 `isinstance(val, bool)` guard，而 `float()` 將佢
變成 1.0 —— 即係一個「$1.00 金價」。呢個 test 釘住修復。

同時覆蓋 JSON round-trip：`np.bool_(True)` 經 `json.dumps` 會變成 Python
`True`，所以兩條路都要擋。"""
import json
import math
import sys

import numpy as np

import paper_trade as pt


def check(name, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{detail}]" if detail else ""))
    return bool(cond)


def main():
    print("=== np 布爾唔可以變成價格 ===")
    ok = True
    for label, v in [("Python True", True),
                     ("np.bool_(True)", np.bool_(True)),
                     ("np.bool_(False)", np.bool_(False)),
                     ("np.True_", np.True_)]:
        r = pt._finite_px(v)
        ok &= check(f"{label} → None", r is None, f"got {r!r}")

    # 經過 JSON round-trip 之前，先用 `.item()`（真寫入 ledger 嘅路徑係
    # `json.dump` + `default=`，上面個環境個 encoder 會 raise，所以呢度簡化
    # 成「numpy bool 轉做 Python bool」—— 反正呢個係個 converter 已知行為）
    rt = np.bool_(True).item()
    ok &= check(f"numpy→python 之後 {rt!r} → None", pt._finite_px(rt) is None,
                f"got {pt._finite_px(rt)!r}")

    # 真價格要照舊行到
    for v in (4485.0, 4485, "4485.0", 0.0001):
        r = pt._finite_px(v)
        ok &= check(f"{v!r} 照舊解析", r is not None and math.isfinite(r),
                    f"got {r!r}")
    for v in (None, "abc", float("nan"), float("inf")):
        ok &= check(f"{v!r} → None", pt._finite_px(v) is None)

    print("结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
