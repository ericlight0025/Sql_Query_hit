# -*- coding: utf-8 -*-
"""
壓力測試：產生不同規模與情境的測試資料，量測 sql_coverage_finder.py 的完整執行時間，並跑驗證腳本。

情境：
- full    ：12 個需求欄位，最佳解為 100%、3 支
- partial ：16 個需求欄位，其中 2 個不存在，不可能 100%
- wide    ：50 個需求欄位，其中 5 個不存在，每支最多 8 個（最吃搜尋效能）

用法：python stress_test.py [檔案數清單，預設 5000,10000] [工作資料夾，預設 stress_data]
"""
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FINDER = HERE.parent / "sql_coverage_finder.py"
counts = [int(x) for x in (sys.argv[1] if len(sys.argv) > 1 else "5000,10000").split(",")]
work = Path(sys.argv[2] if len(sys.argv) > 2 else "stress_data").resolve()

rows = []
for count in counts:
    for scenario in ("full", "partial", "wide"):
        data = work / f"{scenario}_{count}"
        subprocess.run([sys.executable, str(HERE / "gen_test_data.py"), str(data), "42", scenario, str(count)],
                       check=True, capture_output=True)
        for max_files in (3, 5):
            t0 = time.time()
            proc = subprocess.run([sys.executable, str(FINDER), "--config", "config.toml",
                                   "--max-files", str(max_files)], cwd=data, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            sec = time.time() - t0
            out = proc.stdout
            best = next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("最佳覆蓋率")), "-")
            files = next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("所需檔案")), "-")
            warn = "⚠ 未完整" if "提前結束" in out else "完整"
            rows.append((count, scenario, max_files, f"{sec:.2f}s", best, files, warn))
            print(*rows[-1], sep="\t", flush=True)
        verify = subprocess.run([sys.executable, str(HERE / "verify_test.py"), str(data)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(f"\tverify {scenario}_{count}: {'全部通過' if verify.returncode == 0 else '失敗'}", flush=True)
        if verify.returncode:
            print(verify.stdout)
