# -*- coding: utf-8 -*-
"""
驗證 sql_coverage_finder.py：以 gen_test_data.py 產生的 manifest.json 為標準答案。

檢查項目：
1. 每支檔案確實有 5～10 個 JOIN、30～70 個欄位
2. 程式算出的每支檔案命中欄位，與產生器埋入的完全一致（中英文、各種編碼、干擾欄位）
3. 壞檔被列為讀取失敗、.bak 不被掃描、不存在的資料夾有警告
4. 最佳解與「可達聯集 DP」算出的理論最佳值一致；前 N 名排序正確、無冗餘、無重複
5. full 情境最佳解為 100%、3 支檔案

用法：python verify_test.py test_data
"""
import importlib.util
import itertools
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("finder", HERE.parent / "sql_coverage_finder.py")
finder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(finder)

data_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "test_data").resolve()
manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
failures = []

def check(cond, msg):
    print(("  OK  " if cond else "  NG  ") + msg)
    if not cond:
        failures.append(msg)

# 1. JOIN 與欄位數
good = [f for f in manifest["files"] if not f["bad"]]
total_files = len(manifest["files"])
bad_files = total_files - len(good)
check(total_files == len(manifest["files"]), f"檔案總數 = {total_files}")
for f in good:
    text = finder.read_text_file(Path(f["path"]))
    body = re.sub(r"/\*.*?\*/", "", text, flags=re.S)  # 去掉註解再數
    joins = len(re.findall(r"\bJOIN\b", body))
    cols = body.split("FROM")[0].count(",") + 1
    if not (5 <= joins <= 10 and 5 <= f["joins"] <= 10 and 30 <= f["columns"] <= 70 and joins == f["joins"] and cols >= f["columns"]):
        failures.append(f"JOIN/欄位數不符：{f['path']}")
check(all(5 <= f["joins"] <= 10 for f in good), "所有檔案 JOIN 數介於 5～10")
check(all(30 <= f["columns"] <= 70 for f in good), "所有檔案欄位數介於 30～70")
enc_count = defaultdict(int)
for f in good:
    enc_count[f["encoding"]] += 1
print("  編碼分布：", dict(enc_count))

# 2. 掃描與命中
settings = finder.resolve_settings(finder.parse_args(["--config", str(data_dir / "config.toml")]))
folders, fields = settings["folders"], settings["fields"]
check(fields == manifest["targets"], f"config.toml 讀取正確（UTF-8 BOM、去空白、去重）：{len(fields)} 個")
matcher = finder.FieldMatcher(fields)
t0 = time.time()
scanned, hits, warnings, errors = finder.scan(folders, matcher, settings["suffixes"])
scan_sec = time.time() - t0
check(scanned == len(good), f"成功掃描 {scanned} 支（{total_files} − {bad_files} 支壞檔），.bak 未掃描")
check(len(errors) == bad_files and all(Path(p).name.startswith("Q00") for p, _ in errors), "3 支壞檔列為讀取失敗")
check(any("NOT_EXIST" in w for w in warnings), "不存在的資料夾顯示警告")
bit = {f: 1 << i for i, f in enumerate(fields)}
wrong = 0
for f in good:
    expect = sum(bit[t] for t in f["targets"])
    got = hits.get(str(Path(f["path"]).resolve()), 0)
    if expect != got:
        wrong += 1
        if wrong <= 5:
            print("    命中不符：", f["path"], "預期", f["targets"], "實際", finder.mask_to_fields(got, fields))
check(wrong == 0, f"{len(good)} 支檔案的命中欄位全部與標準答案一致（不符 {wrong} 支）")

# 4. 搜尋結果驗證：以「可達聯集 DP」計算每種檔案數能達到的最高覆蓋，作為標準答案
groups = defaultdict(list)
for p, m in hits.items():
    groups[m].append(p)
MAX_FILES, TOP = 5, 5
t0 = time.time()
res = finder.search_best_combos(groups, MAX_FILES, TOP)
search_sec = time.time() - t0

def or_all(c):
    r = 0
    for x in c:
        r |= x
    return r

# reach[k] = 恰好用 k 個 mask（可重複不影響聯集）能得到的所有聯集
got = [(-key[0], key[1]) for key, _ in res.combos]
check(res.exhaustive, "搜尋完整執行（未觸發預算）")
if len(fields) <= 20:  # 欄位多時聯集狀態會爆量，只在 20 欄以內做 DP 驗證
    reach, layer = {}, {0}
    for k in range(1, MAX_FILES + 1):
        layer = {u | m for u in layer for m in groups}
        reach[k] = max(u.bit_count() for u in layer)
    best_cov = max(reach.values())
    best_k = min(k for k, v in reach.items() if v == best_cov)
    check(got[0] == (best_cov, best_k), f"最佳解 = DP 最佳 {best_cov}/{len(fields)} 欄、{best_k} 支（程式：{got[0]}）")
else:
    print(f"  --  欄位 {len(fields)} 個，略過 DP 驗證；最佳 {got[0][0]}/{len(fields)} 欄、{got[0][1]} 支")
keys = [(-c, k) for c, k in got]
check(keys == sorted(keys), f"前 5 名依覆蓋率、檔案數排序：{got}")
check(all(all(c[i] & ~or_all(c[:i] + c[i + 1:]) for i in range(len(c))) for _, c in res.combos), "前 5 名每支檔案都有貢獻（無冗餘）")
check(len({frozenset(c) for _, c in res.combos}) == len(res.combos), "前 5 名沒有重複組合")
brute = []

# 5. full 情境：最佳解必須是 3 支、100%
if manifest.get("scenario", "full") == "full":
    check(got[0] == (len(fields), 3), "full 情境：100%、3 支（1、2 支不可能 100%）")
print(f"\n  掃描 {scan_sec:.2f} 秒，搜尋 {search_sec:.2f} 秒，不同覆蓋組 {len(groups)} 個")
print("\n全部通過" if not failures else f"\n失敗 {len(failures)} 項")
sys.exit(1 if failures else 0)
