# -*- coding: utf-8 -*-
"""
測試資料產生器：產生 5000 支模擬 SQL/TXT 檔案，供 sql_coverage_finder.py 驗收使用。

每支檔案：
- 5～10 個 JOIN
- 30～70 個隨機輸出欄位（中文、英文、中英混合，多種別名寫法）
- 隨機編碼：UTF-8、UTF-8 BOM、CP950、UTF-16
- 混入「近似但不應命中」的干擾欄位，例如 舊交易序號、ACCOUNT_NO_OLD

另外埋入已知答案：
- 需求欄位共 12 個（中英混合），一般檔案不會隨機使用它們。
- 任何一支檔案最多只含 5 個需求欄位，所以 1 支或 2 支檔案不可能達到 100%。
- 刻意埋入 3 支檔案（各 4 個欄位），聯集剛好 100%，所以最佳解一定是 3 支。
- 3 支無法解碼的壞檔，用來測試錯誤處理。

情境 partial（第 3 個參數）：需求欄位 16 個，其中 2 個在任何檔案都不存在，
每支檔案最多含 3 個需求欄位，所以不可能 100%，用來測試「覆蓋率最高的 5 組」。

情境 wide：需求欄位 50 個，其中 5 個不存在，每支最多含 8 個需求欄位（壓力測試用）。

用法：
    python gen_test_data.py [輸出資料夾，預設 test_data] [亂數種子，預設 42] [full|partial|wide，預設 full] [檔案數，預設 5000]
"""

import json
import random
import sys
from pathlib import Path

TOTAL_FILES = 5000
BAD_FILES = 3

# 銀行系統常見欄位詞彙：前綴 × 後綴組合出中文欄位，例如 帳戶號碼、交易日期、放款金額
ZH_PREFIX = ["帳戶", "客戶", "交易", "分行", "匯款", "受款人", "放款", "存款", "信用卡",
             "利率", "櫃員", "通路", "外幣", "定存", "對帳"]
ZH_SUFFIX = ["號碼", "編號", "名稱", "日期", "金額", "狀態", "類別", "代碼", "餘額", "序號"]
EN_PREFIX = ["ACCOUNT", "CUSTOMER", "TXN", "BRANCH", "REMIT", "PAYEE", "LOAN", "DEPOSIT",
             "CARD", "RATE", "TELLER", "CHANNEL", "FX", "TD", "LEDGER"]
EN_SUFFIX = ["NO", "ID", "NAME", "DATE", "AMT", "STATUS", "TYPE", "CODE", "BAL", "SEQ"]
MIXED = [f"{zh}{en}" for zh in ["客戶", "帳戶", "交易", "分行"] for en in ["ID", "NO", "CODE"]]


def build_pool():
    """建立欄位池：150 個中文、150 個英文、12 個中英混合。"""
    zh = [p + s for p in ZH_PREFIX for s in ZH_SUFFIX]
    en = [f"{p}_{s}" for p in EN_PREFIX for s in EN_SUFFIX]
    return zh + en + MIXED


def column_expr(rng, name, table_alias, src_idx):
    """產生一個輸出欄位的寫法（多種方言）。來源欄位用 c### 命名，避免意外命中。"""
    src = f"{table_alias}.c{src_idx:03d}"
    is_ascii = name.isascii()
    forms = [
        f"{src} AS {name}",
        f'{src} AS "{name}"',
        f"{src} AS [{name}]",
        f"{src} {name}",
        f"[{name}] = {src}",
    ]
    if is_ascii:
        forms.append(f"{table_alias}.{name}")  # 英文欄位常直接寫成 t1.ACCOUNT_NO
    return rng.choice(forms)


def near_miss(rng, name):
    """產生「看起來像、但不應命中」的干擾欄位名稱。"""
    if name.isascii():
        return rng.choice([f"{name}_OLD", f"OLD_{name}", f"{name}2", f"X{name}"])
    return rng.choice([f"舊{name}", f"{name}_舊", f"{name}OLD", f"原{name}", f"{name}2"])


def build_sql(rng, pool, targets_in_file, file_no):
    """組出一支 SQL：5～10 個 JOIN、30～70 個輸出欄位。"""
    join_count = rng.randint(5, 10)
    total_cols = rng.randint(30, 70)
    aliases = [f"t{i}" for i in range(join_count + 1)]

    # 需求欄位以外的隨機欄位
    filler_count = total_cols - len(targets_in_file)
    names = rng.sample(pool, filler_count) + list(targets_in_file)
    rng.shuffle(names)

    cols = []
    for i, name in enumerate(names):
        # 少數需求欄位改放在註解中（規格 V1：註解中的欄位也算命中）
        if name in targets_in_file and rng.random() < 0.15:
            cols.append(f"{rng.choice(aliases)}.c{i:03d} AS 欄{i} /* 對應 {name} */")
        else:
            cols.append(column_expr(rng, name, rng.choice(aliases), i))
    # 干擾欄位：取需求欄位做變形，2～6 個
    for _ in range(rng.randint(2, 6)):
        cols.append(column_expr(rng, near_miss(rng, rng.choice(GLOBAL_TARGETS)), rng.choice(aliases), 999))

    lines = [f"/* 測試檔 #{file_no:03d}：{join_count} 個 JOIN、{total_cols} 個欄位 */", "SELECT"]
    lines.append(",\n".join("    " + c for c in cols))
    lines.append(f"FROM ACCT_MASTER_{file_no % 17} t0")
    for i in range(1, join_count + 1):
        kind = rng.choice(["INNER JOIN", "LEFT JOIN", "LEFT OUTER JOIN", "JOIN"])
        lines.append(f"{kind} BANK_TBL_{rng.randint(1, 60)} t{i}\n    ON t0.k{i:02d} = t{i}.k{i:02d}")
    lines.append(f"WHERE t0.acct_status = '正常' AND t0.c000 >= :p_from_dt")
    return "\n".join(lines) + "\n", join_count, total_cols


def encode(rng, text):
    """隨機選擇編碼；CP950 無法表示的字就改用 UTF-8。"""
    enc = rng.choices(["utf-8", "utf-8-sig", "cp950", "utf-16"], weights=[55, 15, 25, 5])[0]
    try:
        return text.encode(enc), enc
    except UnicodeEncodeError:
        return text.encode("utf-8"), "utf-8"


GLOBAL_TARGETS: list[str] = []


def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "test_data").resolve()
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 42
    scenario = sys.argv[3] if len(sys.argv) > 3 else "full"
    global TOTAL_FILES
    TOTAL_FILES = int(sys.argv[4]) if len(sys.argv) > 4 else TOTAL_FILES
    rng = random.Random(seed)

    pool = build_pool()
    # 需求欄位：8 個中文、2 個英文、2 個中英混合
    zh = [x for x in pool if not x.isascii() and x not in MIXED]
    en = [x for x in pool if x.isascii()]
    if scenario == "partial":
        targets = rng.sample(zh, 11) + rng.sample(en, 3) + rng.sample(MIXED, 2)
    elif scenario == "wide":
        targets = rng.sample(zh, 25) + rng.sample(en, 20) + rng.sample(MIXED, 5)
    else:
        targets = rng.sample(zh, 8) + rng.sample(en, 2) + rng.sample(MIXED, 2)
    rng.shuffle(targets)
    # partial：最後 2 個需求欄位不放進任何檔案；每支最多 3 個需求欄位
    usable = {"partial": targets[:-2], "wide": targets[:-5]}.get(scenario, targets)
    per_file_max = {"partial": 3, "wide": 8}.get(scenario, 5)
    GLOBAL_TARGETS[:] = targets
    # 一般欄位池排除需求欄位，需求欄位只透過「埋入」出現
    filler_pool = [x for x in pool if x not in targets]

    roots = [out_dir / "Project" / "SQL", out_dir / "Batch" / "SQL", out_dir / "Work" / "SQL"]
    subdirs = ["", "report", "report/monthly", "batch/daily", "old"]

    # 決定每支檔案含哪些需求欄位
    plan = []
    planted = [targets[0:4], targets[4:8], targets[8:12]]  # 已知最佳解：3 支 × 4 欄位
    planted_slots = set(rng.sample(range(BAD_FILES, TOTAL_FILES), 3))
    planted_iter = iter(planted)
    for n in range(TOTAL_FILES):
        if n < BAD_FILES:
            plan.append(None)
        elif n in planted_slots and scenario == "full":
            plan.append(next(planted_iter))
        elif rng.random() < 0.6:
            plan.append(rng.sample(usable, rng.randint(1, per_file_max)))
        else:
            plan.append([])

    manifest = {"seed": seed, "scenario": scenario, "targets": targets, "files": []}
    for n, tset in enumerate(plan):
        root = roots[n % 3]
        sub = root / rng.choice(subdirs)
        sub.mkdir(parents=True, exist_ok=True)
        ext = rng.choice([".sql", ".sql", ".SQL", ".txt", ".Txt"])
        path = sub / f"Q{n:03d}{ext}"
        if tset is None:
            # 無法以任何支援編碼解碼的壞檔
            path.write_bytes(b"\x81\x30\xff\xfe\x00\xd8" * 20)
            manifest["files"].append({"path": str(path), "bad": True})
            continue
        text, joins, cols = build_sql(rng, filler_pool, tset, n)
        data, enc = encode(rng, text)
        path.write_bytes(data)
        manifest["files"].append({
            "path": str(path), "bad": False, "encoding": enc, "joins": joins,
            "columns": cols, "targets": sorted(tset), "planted": n in planted_slots and scenario == "full",
        })

    # 一支不屬於掃描範圍的檔案（副檔名 .bak），確認不會被掃描
    (roots[0] / "ignore_me.bak").write_text("SELECT " + ", ".join(targets), encoding="utf-8")

    # config.toml：刻意存成 UTF-8 BOM，欄位加入前後空白、重複與大小寫差異，測試設定檔的清理邏輯
    field_lines = list(targets) + ["  " + targets[0] + "  ", targets[-1].lower()]

    def toml_str(text):
        # 單引號字串不處理跳脫字元，Windows 路徑可以原樣寫入
        return "'" + text + "'"

    folder_list = [str(r) for r in roots] + [str(out_dir / "NOT_EXIST")]
    config = (
        "# 測試用設定檔（由 gen_test_data.py 產生）\n"
        "folders = [\n" + "".join(f"  {toml_str(f)},\n" for f in folder_list) + "]\n"
        "fields = [\n" + "".join(f"  {toml_str(f)},\n" for f in field_lines) + "]\n"
        "max_files = 5\n"
        "top = 5\n"
        'extensions = [".sql", ".txt"]\n'
    )
    (out_dir / "config.toml").write_text(config, encoding="utf-8-sig")
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已產生 {TOTAL_FILES} 支檔案於 {out_dir}")
    print(f"需求欄位：{'、'.join(targets)}")


if __name__ == "__main__":
    main()
