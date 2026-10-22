# -*- coding: utf-8 -*-
"""
SQL Coverage Finder V1.0

掃描多個資料夾內的 .sql / .txt 檔案，找出「最少幾支檔案」可以涵蓋使用者指定的全部欄位，
並顯示每支檔案的完整路徑。

- 只使用 Python 標準函式庫（Python 3.11+），可在 Windows 執行。
- 結果同時輸出到畫面、result.txt 與暗色主題的 report.html。
- 不解析 SQL、不連線資料庫、不呼叫 AI。
- 欄位可為中文、英文或中英混合（例如：交易序號、ACCOUNT_NO、客戶ID）。

用法：
    python sql_coverage_finder.py                         （讀取同目錄的 config.toml）
    python sql_coverage_finder.py --config my.toml --top 10
"""

import argparse
import bisect
import html
import math
import os
import re
import sys
import time
import tomllib
import unicodedata
import webbrowser
from datetime import datetime
from collections import defaultdict
from pathlib import Path

# 預設掃描的副檔名（比對時不分大小寫），可在 config.toml 的 extensions 修改
SCAN_SUFFIXES = {".sql", ".txt"}

# 預設設定檔名
CONFIG_FILE = "config.toml"

# 識別字字元：Python 3 的 \w 為 Unicode 版本，包含英數、底線與中文字；另加 Oracle 允許的 $ #。
# 欄位前後若緊鄰這些字元，就不算「完整匹配」。
# 例：P.ACCOUNT_NO 命中 ACCOUNT_NO；ACCOUNT_NO_OLD 不命中；舊交易序號、交易序號_舊 不命中 交易序號。
WORD_CHAR = r"[\w$#]"

# 搜尋預算：DFS 最多展開的節點數。超過即停止，並標示結果「不保證最佳」。
SEARCH_NODE_BUDGET = 5_000_000

# 結果檔名（寫在執行時的目前目錄，每次覆寫）
RESULT_FILE = "result.txt"

# HTML 報告檔名（暗色主題，單一檔案，與 result.txt 內容一致）
REPORT_FILE = "report.html"


# ---------------------------------------------------------------------------
# 檔案讀取與解碼
# ---------------------------------------------------------------------------

def decode_bytes(data: bytes) -> tuple[str, str]:
    """
    依序嘗試常見編碼，回傳 (文字, 使用的編碼)。全部失敗時丟出 UnicodeDecodeError。

    順序很重要：
    1. 開頭有 UTF-16 BOM → utf-16（SSMS 存成 Unicode 時常見）
    2. utf-8-sig：同時處理 UTF-8 與 UTF-8 BOM
    3. cp950：繁體中文 Windows 的 ANSI 編碼
    刻意不使用 latin-1 或 errors='replace' 當備援：中文會變成亂碼，造成「看似掃描成功、其實漏判」。
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    last_error = None
    for encoding in ("utf-8-sig", "cp950"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError as exc:
            last_error = exc
    raise UnicodeDecodeError(
        last_error.encoding, last_error.object, last_error.start, last_error.end,
        "無法以 UTF-8 / UTF-8 BOM / CP950 / UTF-16 解碼",
    )


def read_text_file(path: Path) -> str:
    """讀取檔案（只讀一次 bytes）並解碼成文字。"""
    return decode_bytes(path.read_bytes())[0]


# ---------------------------------------------------------------------------
# 欄位比對
# ---------------------------------------------------------------------------

def normalize_fields(raw_fields: list[str]) -> list[str]:
    """欄位去重（不分大小寫），保留第一次出現的寫法與順序。"""
    seen = set()
    fields = []
    for field in raw_fields:
        key = field.casefold()
        if key not in seen:
            seen.add(key)
            fields.append(field)
    return fields


def build_pattern(field: str) -> re.Pattern:
    """
    建立單一欄位的比對規則。
    只有當欄位的首／尾字元本身是識別字字元時，才檢查該側的邊界
    （行為類似 \\b，讓含括號等符號的欄位名稱也能正常比對）。
    """
    head = f"(?<!{WORD_CHAR})" if re.match(WORD_CHAR, field[0]) else ""
    tail = f"(?!{WORD_CHAR})" if re.match(WORD_CHAR, field[-1]) else ""
    return re.compile(head + re.escape(field) + tail, re.IGNORECASE)


class FieldMatcher:
    """把每支檔案的文字轉成欄位命中的 bitmask。"""

    def __init__(self, fields: list[str]):
        self.fields = fields
        # 每個欄位對應一個位元：第 i 個欄位 = 1 << i
        self.entries = [
            (1 << i, field.casefold(), build_pattern(field))
            for i, field in enumerate(fields)
        ]

    def match(self, text: str) -> int:
        """回傳命中欄位的 bitmask。先用子字串預篩（速度快），有出現才跑 regex 檢查邊界。"""
        folded = text.casefold()
        mask = 0
        for bit, folded_field, pattern in self.entries:
            if folded_field in folded and pattern.search(text):
                mask |= bit
        return mask


# ---------------------------------------------------------------------------
# 掃描資料夾
# ---------------------------------------------------------------------------

def iter_sql_files(folder: Path, errors: list[tuple[str, str]], suffixes: set[str] = SCAN_SUFFIXES):
    """遞迴列出資料夾內的 .sql / .txt 檔案。無法進入的子目錄記錄錯誤後繼續。"""
    def on_error(exc: OSError):
        errors.append((str(exc.filename or folder), f"無法讀取目錄：{exc.strerror or exc}"))

    for root, _dirs, files in os.walk(folder, onerror=on_error):
        for name in files:
            if Path(name).suffix.lower() in suffixes:
                yield Path(root) / name


def scan(folders: list[str], matcher: FieldMatcher, suffixes: set[str] = SCAN_SUFFIXES):
    """
    掃描所有資料夾，回傳：
    - scanned：成功掃描的檔案數
    - hits：{完整路徑: 命中 bitmask}（只含命中 > 0 的檔案）
    - warnings：資料夾不存在等警告
    - errors：[(路徑, 錯誤原因)]，讀取失敗的檔案不會被當成「未命中」
    """
    scanned = 0
    hits: dict[str, int] = {}
    warnings: list[str] = []
    errors: list[tuple[str, str]] = []
    seen_paths = set()  # 資料夾清單互相重疊時，同一支檔案只算一次

    for folder_text in folders:
        folder = Path(folder_text)
        if not folder.is_dir():
            warnings.append(f"資料夾不存在，略過：{folder_text}")
            continue
        for path in iter_sql_files(folder, errors, suffixes):
            full_path = str(path.resolve())
            key = os.path.normcase(full_path)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            try:
                text = read_text_file(path)
            except (OSError, UnicodeDecodeError) as exc:
                errors.append((full_path, f"{type(exc).__name__}: {exc}"))
                continue
            scanned += 1
            mask = matcher.match(text)
            if mask:
                hits[full_path] = mask
    return scanned, hits, warnings, errors


# ---------------------------------------------------------------------------
# 組合搜尋
# ---------------------------------------------------------------------------

class SearchResult:
    def __init__(self):
        self.combos: list[tuple[tuple, tuple[int, ...]]] = []  # [(排序鍵, mask 組合)]
        self.exhaustive = True       # 是否完整搜尋（未因預算中斷）
        self.completed_levels = 0    # 已完整搜尋到幾支檔案的組合
        self.nodes = 0


def search_best_combos(groups: dict[int, list[str]], max_files: int, top: int) -> SearchResult:
    """
    在「不同的覆蓋 mask」上做精確搜尋（集合覆蓋問題）。

    - 相同覆蓋集合的檔案已合併成一組（groups），只搜尋不同的 mask。
    - 逐層加深：先找 1 支檔案的組合，再找 2 支、3 支……
    - 排序鍵：(-覆蓋欄位數, 檔案數, 找到的順序)，即覆蓋率高優先，同覆蓋率時檔案數少優先。
    - 只保留「不冗餘」組合：組合中每支檔案都必須貢獻至少一個其他檔案沒有的欄位。
    - 分支定界：若某分支的覆蓋上界已不可能擠進前 N 名，就剪掉。
    - 展開節點超過 SEARCH_NODE_BUDGET 時停止，並標示結果不保證最佳。
    """
    result = SearchResult()
    # 依命中數由多到少排序，讓好的組合先被找到，剪枝更有效
    masks = sorted(groups, key=lambda m: (-m.bit_count(), min(groups[m])))
    count = len(masks)
    if count == 0:
        return result
    # 所有檔案聯集的欄位數：任何組合的覆蓋都不可能超過它，用來限制上界（新增數相加會重複計算）
    all_union = 0
    for m in masks:
        all_union |= m
    max_cover = all_union.bit_count()

    best: list[tuple[tuple, tuple[int, ...]]] = []

    def can_enter(upper_cover: int, file_count: int) -> bool:
        """
        覆蓋上界為 upper_cover、檔案數為 file_count 的組合，是否還可能進入前 N 名。
        前 N 名已滿時，必須「嚴格優於」第 N 名才算；覆蓋率與檔案數都相同的組合，以先找到者為準。
        這樣就不必列舉大量同分組合（例如上千組 100%、3 支），是效能的關鍵。
        """
        if len(best) < top:
            return True
        worst_neg_cover, worst_files, _ = best[-1][0]
        return (-upper_cover, file_count) < (worst_neg_cover, worst_files)

    def is_irredundant(chosen: list[int]) -> bool:
        """檢查組合中每個 mask 都有獨有的欄位。"""
        for i, m in enumerate(chosen):
            others = 0
            for j, o in enumerate(chosen):
                if j != i:
                    others |= o
            if m & ~others == 0:
                return False
        return True

    def record(chosen: list[int], cover: int):
        # 排序鍵：(-覆蓋欄位數, 檔案數, 找到的順序)。同分時先找到的排前面，結果固定可重現。
        key = (-cover.bit_count(), len(chosen), result.nodes)
        bisect.insort(best, (key, tuple(chosen)))
        del best[top:]

    class BudgetExceeded(Exception):
        pass

    def dfs(candidates: list[int], chosen: list[int], cover: int, k: int):
        """
        candidates：還可以選的 mask（只會往後選，避免同一組合重複出現）。
        每個節點：
        - 計算各候選的「新增欄位」(m & ~cover)；新增 0 個的直接淘汰（加入只會變成冗餘）。
        - 以新增數做桶排序（新增數 ≤ 欄位數，比一般排序快），由多到少展開。
        - 覆蓋上界 = 目前覆蓋數 + 前 r 個最大新增數（r = 剩餘名額），不可能進榜就剪掉。
        """
        result.nodes += 1
        if result.nodes > SEARCH_NODE_BUDGET:
            raise BudgetExceeded
        covered = cover.bit_count()
        if len(chosen) == k:
            if is_irredundant(chosen) and can_enter(covered, k):
                record(chosen, cover)
            return
        remain = k - len(chosen)
        not_cover = ~cover
        buckets: dict[int, list[int]] = {}
        n_cand = 0
        for m in candidates:
            g = (m & not_cover).bit_count()
            if g:
                buckets.setdefault(g, []).append(m)
                n_cand += 1
        if n_cand < remain:
            return
        ordered: list[int] = []
        gains: list[int] = []
        for n in sorted(buckets, reverse=True):
            ordered.extend(buckets[n])
            gains.extend([n] * len(buckets[n]))
        # 子樹只需要「還可能進榜」的候選：含某候選的組合，上界 = 目前覆蓋數
        # + 其他 (remain-1) 支的最大新增數總和 + 它自己的新增數；不夠就連同後面（新增更少）的一起捨棄。
        head = sum(gains[:remain - 1])
        cut = len(ordered)
        for i in range(remain - 1, len(ordered)):
            if not can_enter(min(covered + head + gains[i], max_cover), k):
                cut = i
                break
        ordered = ordered[:cut]
        gains = gains[:cut]
        for i in range(len(ordered) - remain + 1):
            upper = min(covered + sum(gains[i:i + remain]), max_cover)
            if not can_enter(upper, k):
                break  # 新增數遞減排列，後面的上界只會更小
            m = ordered[i]
            chosen.append(m)
            dfs(ordered[i + 1:], chosen, cover | m, k)
            chosen.pop()

    try:
        for k in range(1, min(max_files, count) + 1):
            dfs(masks, [], 0, k)
            result.completed_levels = k
    except BudgetExceeded:
        result.exhaustive = False

    result.combos = best
    return result


# ---------------------------------------------------------------------------
# 輸出
# ---------------------------------------------------------------------------

def display_width(text: str) -> int:
    """終端機顯示寬度：中文等全形字元佔 2 格。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, width: int) -> str:
    """依顯示寬度補空白，讓中文表格對齊。"""
    return text + " " * max(0, width - display_width(text))


def mask_to_fields(mask: int, fields: list[str]) -> list[str]:
    return [f for i, f in enumerate(fields) if mask & (1 << i)]


def order_for_display(combo: tuple[int, ...]) -> list[tuple[int, int]]:
    """
    組合內檔案的顯示順序：每次挑「新增欄位最多」的檔案，讓累計數字盡快上升。
    回傳 [(mask, 新增欄位 mask)]。只影響顯示，不影響排名。
    """
    rest = list(combo)
    cover = 0
    ordered = []
    while rest:
        m = max(rest, key=lambda x: ((x & ~cover).bit_count(), x.bit_count()))
        ordered.append((m, m & ~cover))
        cover |= m
        rest.remove(m)
    return ordered


def combo_cover(combo: tuple[int, ...]) -> int:
    cover = 0
    for m in combo:
        cover |= m
    return cover


def percent(cover_count: int, total: int) -> str:
    return f"{cover_count * 100 // total}%" if cover_count < total else "100%"


def build_report(fields, scanned, hits, groups, warnings, errors, search, max_files) -> str:
    total = len(fields)
    lines = []
    out = lines.append

    out("SQL Coverage Finder")
    out("===================")
    out(f"搜尋完成 · {scanned} 支檔案 · {total} 個需求欄位 · 命中 {len(hits)} 支")

    for w in warnings:
        out(f"⚠ {w}")

    if not search.combos:
        out("")
        out("沒有任何檔案命中需求欄位（0 筆結果）。")
        out(f"缺少欄位   : {'、'.join(fields)}")
    else:
        best_key, best_combo = search.combos[0]
        best_cover = combo_cover(best_combo)
        cover_count = best_cover.bit_count()
        missing = mask_to_fields(((1 << total) - 1) & ~best_cover, fields)

        out("已找到完整覆蓋" if cover_count == total else f"未找到完整覆蓋（最多 {max_files} 支檔案）")
        if not search.exhaustive:
            out(f"⚠ 搜尋因效能限制提前結束（已完整搜尋 1～{search.completed_levels} 支檔案的組合），"
                f"以下結果不保證為全域最佳。")
        out("")
        out(f"最佳覆蓋率 : {percent(cover_count, total)} ({cover_count}/{total})")
        out(f"所需檔案   : {len(best_combo)} 支")
        out(f"缺少欄位   : {len(missing)}" + (f"（{'、'.join(missing)}）" if missing else ""))
        out("")
        out("最佳組合 #1")
        out("-----------")

        running = 0
        for idx, (m, added) in enumerate(order_for_display(best_combo), 1):
            running |= m
            path = min(groups[m])
            out(f"[{idx}] {pad(Path(path).name, 24)} 累計 {running.bit_count()}/{total}")
            out(f"    {path}")
            for f in mask_to_fields(added, fields):
                out(f"    + {f}")
            already = mask_to_fields(m & ~added, fields)
            if already:
                out(f"    （另含已涵蓋：{'、'.join(already)}）")
            same = sorted(groups[m])[1:]
            if same:
                out(f"    同覆蓋檔案（{len(same)}）：")
                for p in same:
                    out(f"      {p}")
        out("")
        if missing:
            out(f"總覆蓋率 {cover_count} / {total} · 缺少：{'、'.join(missing)}")
        else:
            out(f"總覆蓋率 {cover_count} / {total} · 全部需求欄位皆有找到")

        # 候選組合排名表
        out("")
        out(f"候選組合排名（前 {len(search.combos)} 組）")
        header = ("排名", "SQL 組合", "覆蓋率", "支數")
        rows = []
        for rank, (key, combo) in enumerate(search.combos, 1):
            names = " + ".join(Path(min(groups[m])).name for m, _ in order_for_display(combo))
            c = combo_cover(combo).bit_count()
            rows.append((str(rank), names, percent(c, total), str(len(combo))))
        widths = [display_width(h) for h in header]
        for row in rows:
            widths = [max(w, display_width(cell)) for w, cell in zip(widths, row)]
        out("  ".join(pad(c, w) for c, w in zip(header, widths)).rstrip())
        for row in rows:
            out("  ".join(pad(c, w) for c, w in zip(row, widths)).rstrip())

        # 每個候選組合的完整路徑（方便直接開檔）
        out("")
        out("候選組合檔案路徑")
        for rank, (key, combo) in enumerate(search.combos, 1):
            cover = combo_cover(combo)
            miss = mask_to_fields(((1 << total) - 1) & ~cover, fields)
            out(f"#{rank}  {percent(cover.bit_count(), total)} ({cover.bit_count()}/{total})"
                + (f"  缺少：{'、'.join(miss)}" if miss else ""))
            for m, _ in order_for_display(combo):
                out(f"    {min(groups[m])}")

    if errors:
        out("")
        out(f"⚠ 讀取失敗（{len(errors)} 支，未納入搜尋，可能影響結果）")
        for path, reason in errors:
            out(f"    {path}")
            out(f"      {reason}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# HTML 報告（暗色主題、單一檔案、不需網路）
# ---------------------------------------------------------------------------

HTML_CSS = """
:root{--bg:#0b0f14;--panel:#121820;--panel2:#0e141b;--line:#1f2a36;--text:#e6edf3;--muted:#8b98a8;
--accent:#5eead4;--accent2:#34d399;--warn:#fbbf24;--bad:#f87171;--chip:#15202b;--chip-line:#2a3a4a;--mono:Consolas,"Cascadia Mono","Courier New",monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:"Segoe UI","Microsoft JhengHei","Noto Sans TC",system-ui,sans-serif;font-size:14px;line-height:1.55}
a{color:var(--accent)}
.top{display:flex;justify-content:space-between;align-items:center;padding:14px 48px;border-bottom:1px solid var(--line);background:var(--panel2)}
.brand{font-weight:700;letter-spacing:.2px}.brand span{color:var(--accent);font-family:var(--mono);margin-right:8px}
.top small{color:var(--muted);letter-spacing:2px;font-size:11px}
.wrap{max-width:1100px;margin:0 auto;padding:32px 48px 64px}
.eyebrow{color:var(--accent);font-size:11px;letter-spacing:3px;font-weight:700;margin-bottom:6px}
h1{font-size:30px;margin:0 0 6px}h2{font-size:20px;margin:0}
.sub{color:var(--muted);display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}
.pill{border:1px solid var(--accent);color:var(--accent);border-radius:999px;padding:4px 14px;font-size:12px;white-space:nowrap}
.pill.warn{border-color:var(--warn);color:var(--warn)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin:26px 0 14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 20px}
.card .k{color:var(--muted);font-size:12px;margin-bottom:8px}.card .v{font-size:26px;font-weight:700}.card .v small{font-size:13px;color:var(--muted);font-weight:400;margin-left:4px}
.hero{display:grid;grid-template-columns:1.1fr 1fr;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;margin-bottom:28px}
.hero>div{padding:26px 28px}.hero>div+div{border-left:1px solid var(--line);background:var(--panel2)}
.ring{display:flex;align-items:center;gap:26px}
.ring svg{width:140px;height:140px;flex:none}
.ring .pct{font-size:30px;font-weight:800;fill:var(--text)}.ring .lbl{font-size:10px;fill:var(--muted)}
.ring h3{margin:0 0 6px;font-size:18px}.big{font-size:22px}.big small{color:var(--muted);font-size:16px}
.muted{color:var(--muted)}
.tabs{display:flex;gap:22px;margin:0 0 30px;color:var(--muted);font-size:13px}.tabs a{color:var(--muted);text-decoration:none}.tabs a:hover{color:var(--text)}
.sec{display:flex;justify-content:space-between;align-items:end;margin:40px 0 14px}
.file{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin-bottom:14px}
.file .head{display:flex;align-items:center;gap:14px;margin-bottom:12px}
.idx{width:30px;height:30px;border-radius:7px;background:#0f2a26;color:var(--accent);display:inline-flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:12px;font-weight:700;flex:none}
.file .name{font-size:17px;font-weight:700;word-break:break-all}.file .cum{margin-left:auto;color:var(--muted);white-space:nowrap}.file .cum b{color:var(--accent)}
.path{font-family:var(--mono);font-size:12px;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:9px 12px;word-break:break-all;user-select:all}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.chip{border:1px solid var(--chip-line);background:var(--chip);border-radius:7px;padding:4px 10px;font-size:12px;color:var(--accent)}
.chip.dim{color:var(--muted);border-style:dashed}.chip.bad{color:var(--bad);border-color:#4a2a2a;background:#1f1416}.chip.ok{color:var(--accent2)}
.lab{color:var(--muted);font-size:12px;margin-top:14px}
details{margin-top:12px}summary{cursor:pointer;color:var(--muted);font-size:13px}summary .n{display:inline-block;background:var(--chip);border:1px solid var(--chip-line);border-radius:5px;padding:0 7px;margin-left:6px;font-size:11px}
details .path{margin-top:8px}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{text-align:left;padding:13px 16px;border-top:1px solid var(--line)}th{white-space:nowrap;color:var(--muted);font-weight:600;font-size:12px;background:var(--panel2);border-top:0}
td.r{font-family:var(--mono);color:var(--accent)}td.c{color:var(--warn);font-weight:700;white-space:nowrap}td.n{white-space:nowrap}.mono{font-family:var(--mono)}
.combo .sep{color:var(--muted);margin:0 6px}
.note{border-left:3px solid var(--warn);background:#1a1712;padding:12px 16px;border-radius:0 8px 8px 0;margin:14px 0;color:#f3e2a8}
.err{border-left:3px solid var(--bad);background:#1c1214;padding:12px 16px;border-radius:0 8px 8px 0;margin:14px 0}
.err .path{margin-top:6px}.err .why{color:var(--muted);font-size:12px;margin:4px 0 10px}
footer{margin-top:48px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:16px}
@media(max-width:760px){.top,.wrap{padding-left:16px;padding-right:16px}.hero{grid-template-columns:1fr}.hero>div+div{border-left:0;border-top:1px solid var(--line)}}
"""


def _h(text) -> str:
    """HTML escape。"""
    return html.escape(str(text), quote=True)


def _chips(names, cls="") -> str:
    return '<div class="chips">' + "".join(f'<span class="chip {cls}">{_h(n)}</span>' for n in names) + "</div>"


def build_html(fields, scanned, hits, groups, warnings, errors, search, max_files, top, elapsed, folders) -> str:
    """產生暗色主題的 HTML 報告，內容與文字報告一致。"""
    total = len(fields)
    full = (1 << total) - 1
    out = []
    o = out.append

    if search.combos:
        best_key, best_combo = search.combos[0]
        best_cover = combo_cover(best_combo)
        cover_count = best_cover.bit_count()
        missing = mask_to_fields(full & ~best_cover, fields)
        found_all = cover_count == total
    else:
        best_combo, best_cover, cover_count, missing, found_all = (), 0, 0, list(fields), False
    pct = cover_count * 100 / total if total else 0
    # 圓環：半徑 54，周長約 339
    circ = 2 * math.pi * 54
    dash = circ * pct / 100

    status_pill = '<span class="pill">● 搜尋完成</span>' if search.exhaustive else '<span class="pill warn">● 搜尋提前結束</span>'
    o(f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SQL Coverage Finder 報告</title>
<style>{HTML_CSS}</style></head><body>
<div class="top"><div class="brand"><span>&lt;/&gt;</span>SQL Coverage Finder</div><small>LOCAL SEARCH REPORT</small></div>
<div class="wrap">
<div class="eyebrow">COVERAGE REPORT</div><h1>欄位覆蓋分析</h1>
<div class="sub"><div>從 SQL 與 TXT 檔案中，找出最接近需求的組合。產生時間 {_h(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</div>{status_pill}</div>
<div class="cards">
<div class="card"><div class="k">已比對檔案</div><div class="v">{scanned:,}<small>支</small></div></div>
<div class="card"><div class="k">需求欄位</div><div class="v">{total}<small>個</small></div></div>
<div class="card"><div class="k">命中檔案</div><div class="v">{len(hits):,}<small>支</small></div></div>
<div class="card"><div class="k">掃描時間</div><div class="v">{elapsed:.3f}<small>秒</small></div></div>
</div>""")

    if found_all:
        right = "<h3 style='margin:0 0 8px'>需求已完整涵蓋</h3><div class='muted'>所有需求欄位都已找到。</div>"
    elif search.combos:
        right = f"<h3 style='margin:0 0 8px'>缺少 {len(missing)} 個欄位</h3><div class='muted'>以下欄位在最多 {max_files} 支檔案的組合中仍然找不到：</div>" + _chips(missing, "bad")
    else:
        right = "<h3 style='margin:0 0 8px'>沒有任何檔案命中</h3><div class='muted'>所有需求欄位都找不到，請確認欄位名稱與資料夾。</div>"
    headline = "已找到完整覆蓋" if found_all else ("未找到完整覆蓋" if search.combos else "0 筆結果")
    need = f"所需 {len(best_combo)} 支 · 最多組合 {max_files} 支" if search.combos else f"最多組合 {max_files} 支"
    o(f"""<div class="hero"><div><div class="ring">
<svg viewBox="0 0 140 140"><circle cx="70" cy="70" r="54" fill="none" stroke="#1f2a36" stroke-width="10"/>
<circle cx="70" cy="70" r="54" fill="none" stroke="{'#5eead4' if found_all else '#fbbf24'}" stroke-width="10" stroke-linecap="round"
 stroke-dasharray="{dash:.1f} {circ:.1f}" transform="rotate(-90 70 70)"/>
<text x="70" y="66" text-anchor="middle" class="pct">{pct:.0f}%</text><text x="70" y="86" text-anchor="middle" class="lbl">欄位覆蓋率</text></svg>
<div><h3>{headline}</h3><div class="big">{cover_count} <small>/ {total}</small></div><div class="muted">{need}</div></div>
</div></div><div>{right}</div></div>""")

    if not search.exhaustive:
        o(f'<div class="note">⚠ 搜尋因效能限制提前結束（已完整搜尋 1～{search.completed_levels} 支檔案的組合），以下結果不保證為全域最佳。</div>')
    if errors:
        o(f'<div class="note">⚠ 有 {len(errors)} 支檔案無法讀取，未納入搜尋，可能影響結果。詳見「讀取失敗」。</div>')

    o('<div class="tabs"><a href="#best">最佳組合</a><a href="#rank">候選排名</a><a href="#paths">檔案路徑</a><a href="#fields">需求清單</a>'
      + ('<a href="#errors">讀取失敗</a>' if errors else "") + "</div>")

    if search.combos:
        o(f'<div class="sec" id="best"><div><div class="eyebrow">BEST COMBINATION</div><h2>最佳組合 #1</h2></div>'
          f'<div class="muted">{len(best_combo)} 支檔案 · {cover_count}/{total} 個欄位</div></div>')
        running = 0
        for idx, (m, added) in enumerate(order_for_display(best_combo), 1):
            running |= m
            paths = sorted(groups[m])
            path = paths[0]
            added_names = mask_to_fields(added, fields)
            already = mask_to_fields(m & ~added, fields)
            o(f'<div class="file"><div class="head"><span class="idx">{idx:02d}</span><span class="name">{_h(Path(path).name)}</span>'
              f'<span class="cum">累計 <b>{running.bit_count()}/{total}</b></span></div>'
              f'<div class="path">{_h(path)}</div>'
              f'<div class="lab">新增 {len(added_names)} 個欄位</div>{_chips(added_names)}')
            if already:
                o(f'<div class="lab">另含已涵蓋 {len(already)} 個</div>{_chips(already, "dim")}')
            if len(paths) > 1:
                o(f'<details><summary>同覆蓋檔案<span class="n">{len(paths) - 1}</span></summary>'
                  + "".join(f'<div class="path">{_h(p)}</div>' for p in paths[1:]) + "</details>")
            o("</div>")

        o(f'<div class="sec" id="rank"><div><div class="eyebrow">TOP MATCHES</div><h2>候選組合排名</h2></div><div class="muted">前 {len(search.combos)} 組</div></div>')
        o("<table><tr><th>排名</th><th>SQL 組合</th><th>覆蓋率</th><th>檔案數</th><th>缺少</th></tr>")
        for rank, (key, combo) in enumerate(search.combos, 1):
            cover = combo_cover(combo)
            names = '<span class="sep">+</span>'.join(f'<span class="mono">{_h(Path(min(groups[m])).name)}</span>' for m, _ in order_for_display(combo))
            miss = mask_to_fields(full & ~cover, fields)
            o(f'<tr><td class="r">{rank:02d}</td><td class="combo">{names}</td><td class="c">{percent(cover.bit_count(), total)}</td>'
              f'<td class="n">{len(combo)} 支</td><td class="muted">{_h("、".join(miss)) if miss else "—"}</td></tr>')
        o("</table>")

        o('<div class="sec" id="paths"><div><div class="eyebrow">FILE PATHS</div><h2>候選組合檔案路徑</h2></div></div>')
        for rank, (key, combo) in enumerate(search.combos, 1):
            cover = combo_cover(combo)
            o(f'<details {"open" if rank == 1 else ""}><summary>#{rank} · {percent(cover.bit_count(), total)}（{cover.bit_count()}/{total}）· {len(combo)} 支</summary>'
              + "".join(f'<div class="path">{_h(min(groups[m]))}</div>' for m, _ in order_for_display(combo)) + "</details>")

    o('<div class="sec" id="fields"><div><div class="eyebrow">REQUIRED FIELDS</div><h2>需求清單</h2></div>'
      f'<div class="muted">找到 {cover_count} · 缺少 {total - cover_count}</div></div>')
    o('<div class="chips">' + "".join(
        f'<span class="chip {"ok" if best_cover & (1 << i) else "bad"}">{_h(f)}</span>' for i, f in enumerate(fields)) + "</div>")
    o('<div class="lab">掃描資料夾</div>' + "".join(f'<div class="path" style="margin-top:6px">{_h(f)}</div>' for f in folders))
    if warnings:
        o('<div class="lab">警告</div>' + "".join(f'<div class="note">{_h(w)}</div>' for w in warnings))

    if errors:
        o(f'<div class="sec" id="errors"><div><div class="eyebrow">READ ERRORS</div><h2>讀取失敗</h2></div><div class="muted">{len(errors)} 支，未納入搜尋</div></div>')
        o('<div class="err">' + "".join(f'<div class="path">{_h(p)}</div><div class="why">{_h(r)}</div>' for p, r in errors) + "</div>")

    o('<footer>SQL Coverage Finder · 僅比對文字中的完整識別字，不解析 SQL 語法；註解中的欄位也會命中。</footer></div></body></html>')
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 主程式
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """設定檔內容錯誤，訊息會直接顯示給使用者。"""


def clean_item(text: str) -> str:
    """去除 BOM、零寬空白與前後空白（含全形空白）。"""
    return str(text).replace("\ufeff", "").replace("\u200b", "").strip()


def load_config(path: Path) -> dict:
    """
    讀取 TOML 設定檔（Python 3.11 內建 tomllib，不需安裝套件）。
    - 先用與 SQL 檔相同的解碼規則讀取，並移除 BOM：記事本存成「UTF-8 (BOM)」也能讀。
    - Windows 路徑請用單引號：'U:\\Project\\SQL'。雙引號字串裡的 \\P 會被當成跳脫字元而報錯。
    """
    text = read_text_file(path).replace("\ufeff", "")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        hint = ""
        if "escape" in str(exc).lower():
            hint = "\n提示：Windows 路徑請改用單引號，例如 'U:\\Project\\SQL'，或把 \\ 寫成 \\\\。"
        raise ConfigError(f"設定檔格式錯誤（{path}）：{exc}{hint}") from exc

    known = {"folders", "fields", "max_files", "top", "extensions", "output_dir", "html"}
    unknown = sorted(set(data) - known)
    if unknown:
        print(f"⚠ 設定檔有不認得的項目，已忽略：{', '.join(unknown)}", file=sys.stderr)

    def str_list(key):
        value = data.get(key)
        if value is None:
            return None
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"設定檔的 {key} 必須是字串清單，例如 {key} = [\"A\", \"B\"]")
        return [c for c in (clean_item(v) for v in value) if c]

    def positive_int(key):
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(f"設定檔的 {key} 必須是 ≥ 1 的整數")
        return value

    config = {
        "folders": str_list("folders"),
        "fields": str_list("fields"),
        "max_files": positive_int("max_files"),
        "top": positive_int("top"),
        "extensions": str_list("extensions"),
        "output_dir": data.get("output_dir"),
        "html": data.get("html"),
    }
    if config["output_dir"] is not None and not isinstance(config["output_dir"], str):
        raise ConfigError("設定檔的 output_dir 必須是字串")
    if config["html"] is not None and not isinstance(config["html"], bool):
        raise ConfigError("設定檔的 html 必須是 true 或 false")
    return config


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="找出最少幾支 SQL/TXT 檔案可以涵蓋指定的全部欄位。"
                    "設定優先順序：命令列參數 > 設定檔 > 預設值。")
    parser.add_argument("--config", help=f"TOML 設定檔，預設讀取 {CONFIG_FILE}（存在時）")
    parser.add_argument("--max-files", type=int, help="最大組合檔案數，預設 5")
    parser.add_argument("--top", type=int, help="顯示前幾組結果，預設 5")
    parser.add_argument("--output-dir", help="result.txt 與 report.html 的存放資料夾，預設目前目錄")
    parser.add_argument("--html", dest="html", action="store_true", default=None, help="另外產生 report.html（預設只產生 result.txt）")
    parser.add_argument("--no-html", dest="html", action="store_false", help="不產生 report.html")
    parser.add_argument("--open", action="store_true", help="產生 report.html 後自動用瀏覽器開啟（run.bat 預設會加）")
    return parser.parse_args(argv)


def resolve_settings(args) -> dict:
    """合併命令列、設定檔與預設值，回傳最終設定。錯誤時丟出 ConfigError。"""
    config: dict = {}
    config_path = Path(args.config) if args.config else Path(CONFIG_FILE)
    if args.config and not config_path.is_file():
        raise ConfigError(f"找不到設定檔：{config_path}")
    if config_path.is_file():
        try:
            config = load_config(config_path)
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"無法讀取設定檔：{exc}") from exc
        config["_path"] = str(config_path.resolve())

    folders = config.get("folders")
    fields = config.get("fields")
    if not folders:
        raise ConfigError(f"沒有指定資料夾：請在 {CONFIG_FILE} 的 folders 填入資料夾。")
    fields = normalize_fields(fields or [])
    if not fields:
        raise ConfigError(f"需求欄位為空：請在 {CONFIG_FILE} 的 fields 填入欄位。")

    max_files = args.max_files if args.max_files is not None else (config.get("max_files") or 5)
    top = args.top if args.top is not None else (config.get("top") or 5)
    if max_files < 1 or top < 1:
        raise ConfigError("--max-files 與 --top 必須 ≥ 1")

    extensions = config.get("extensions") or sorted(SCAN_SUFFIXES)
    suffixes = {("." + e.lstrip(".")).lower() for e in extensions}
    output_dir = Path(args.output_dir or config.get("output_dir") or ".")
    # 預設只輸出 result.txt；html = true（或 --html）才產生 report.html
    html_on = args.html if args.html is not None else bool(config.get("html"))
    open_report = html_on and args.open

    return {
        "config_path": config.get("_path"),
        "folders": folders, "fields": fields, "max_files": max_files, "top": top,
        "suffixes": suffixes, "output_dir": output_dir, "html": html_on, "open_report": open_report,
    }


def main(argv=None) -> int:
    # 輸出導向檔案時，Windows 會用 cp950 編碼；遇到 cp950 沒有的字以 ? 取代，避免程式中斷
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass

    try:
        settings = resolve_settings(parse_args(argv))
    except ConfigError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2

    folders, fields = settings["folders"], settings["fields"]
    max_files, top = settings["max_files"], settings["top"]
    if settings["config_path"]:
        print(f"設定檔：{settings['config_path']}")

    started = time.perf_counter()
    matcher = FieldMatcher(fields)
    scanned, hits, warnings, errors = scan(folders, matcher, settings["suffixes"])

    # 相同覆蓋集合的檔案合併成一組，搜尋時只算一次，但保留所有路徑
    groups: dict[int, list[str]] = defaultdict(list)
    for path, mask in hits.items():
        groups[mask].append(path)

    search = search_best_combos(groups, max_files, top)
    elapsed = time.perf_counter() - started
    report = build_report(fields, scanned, hits, groups, warnings, errors, search, max_files)
    print(report, end="")

    out_dir = settings["output_dir"]
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"\n⚠ 無法建立輸出資料夾 {out_dir}：{exc}", file=sys.stderr)
    result_path = out_dir / RESULT_FILE
    report_path = out_dir / REPORT_FILE
    try:
        # utf-8-sig：用記事本開啟中文也不會亂碼
        result_path.write_text(report, encoding="utf-8-sig")
        print(f"\n結果已寫入：{result_path.resolve()}")
    except OSError as exc:
        print(f"\n⚠ 無法寫入 {result_path}：{exc}", file=sys.stderr)
    if not settings["html"]:
        return 0
    try:
        page = build_html(fields, scanned, hits, groups, warnings, errors, search, max_files, top, elapsed, folders)
        report_path.write_text(page, encoding="utf-8")
        print(f"HTML 報告：{report_path.resolve()}")
        if settings["open_report"]:
            webbrowser.open(report_path.resolve().as_uri())
    except OSError as exc:
        print(f"⚠ 無法寫入 {report_path}：{exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
