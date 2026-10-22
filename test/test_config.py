# -*- coding: utf-8 -*-
"""
設定檔（config.toml）與命令列參數的測試。

檢查項目：
- 範例 config.toml 可以正確讀取，Windows 路徑（單引號）反斜線保留原樣
- 雙引號 Windows 路徑會顯示易懂的錯誤提示
- UTF-8 BOM、CP950 編碼的設定檔都能讀
- 優先順序：命令列 > 設定檔 > 預設值
- 舊版 --folders / --fields 與 open_report 已移除：會被拒絕／警告
- 欄位清理（空白、BOM、重複、大小寫）、副檔名正規化、output_dir 自動建立
- 預設只產生 result.txt；html = true 或 --html 才產生 report.html
- 型別錯誤、缺少欄位／資料夾時給出明確錯誤

用法：python test/test_config.py
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
spec = importlib.util.spec_from_file_location("finder", ROOT / "sql_coverage_finder.py")
finder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(finder)

failures = []


def check(cond, msg):
    print(("  OK  " if cond else "  NG  ") + msg)
    if not cond:
        failures.append(msg)


def settings_for(*argv):
    return finder.resolve_settings(finder.parse_args(list(argv)))


def config_error(*argv):
    try:
        settings_for(*argv)
    except finder.ConfigError as exc:
        return str(exc)
    return ""


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    old_cwd = os.getcwd()
    os.chdir(tmp)
    try:
        # 1. 範例 config.toml
        s = settings_for("--config", str(ROOT / "config.toml"))
        check(s["folders"] == [r"U:\Project\SQL", r"U:\Batch\SQL", r"D:\Work\SQL"], "範例 config.toml：Windows 路徑（單引號）反斜線保留原樣")
        check(len(s["fields"]) == 10 and "交易序號" in s["fields"] and "ACCOUNT_NO" in s["fields"], "範例 config.toml：讀到 10 個中英文欄位")
        check(s["max_files"] == 5 and s["top"] == 5 and s["suffixes"] == {".sql", ".txt"}, "範例 config.toml：max_files、top、extensions")
        check(s["html"] is False and s["open_report"] is False, "範例 config.toml：html = false（預設只產生 result.txt），不開啟報告")

        # 2. 雙引號 Windows 路徑 → 友善提示
        bad = tmp / "bad.toml"
        bad.write_text('folders = ["U:\\Project\\SQL"]\nfields = ["A"]\n', encoding="utf-8")
        msg = config_error("--config", str(bad))
        check("單引號" in msg, "雙引號 Windows 路徑：錯誤訊息提示改用單引號")

        # 3. BOM 與 CP950 編碼、欄位清理
        cfg = tmp / "bom.toml"
        cfg.write_text("folders = ['D:\\資料']\nfields = ['  交易序號 ', '交易序號', 'account_no', 'ACCOUNT_NO', '\u200b幣別']\n",
                       encoding="utf-8-sig")
        s = settings_for("--config", str(cfg))
        check(s["fields"] == ["交易序號", "account_no", "幣別"], f"UTF-8 BOM 設定檔：欄位去空白、去零寬、去重 → {s['fields']}")
        cfg950 = tmp / "cp950.toml"
        cfg950.write_bytes("folders = ['D:\\資料']\nfields = ['分行代號', '交易金額']\n".encode("cp950"))
        s = settings_for("--config", str(cfg950))
        check(s["fields"] == ["分行代號", "交易金額"] and s["folders"] == ["D:\\資料"], "CP950 編碼的設定檔可以讀取")

        # 4. 優先順序：命令列 > 設定檔 > 預設值
        cfg2 = tmp / "prio.toml"
        cfg2.write_text("folders = ['X']\nfields = ['A']\nmax_files = 3\ntop = 7\nextensions = ['sql', '.TXT', '.Bak']\n"
                        "output_dir = 'out'\nhtml = true\n", encoding="utf-8")
        s = settings_for("--config", str(cfg2))
        check(s["max_files"] == 3 and s["top"] == 7, "設定檔的 max_files、top 生效")
        check(s["suffixes"] == {".sql", ".txt", ".bak"}, "extensions 自動補點、轉小寫")
        check(s["output_dir"] == Path("out") and s["html"] is True and s["open_report"] is False, "設定檔的 output_dir、html 生效；沒加 --open 不開啟報告")
        s = settings_for("--config", str(cfg2), "--max-files", "4", "--top", "2", "--output-dir", "o2", "--open")
        check(s["max_files"] == 4 and s["top"] == 2 and s["output_dir"] == Path("o2") and s["open_report"] is True,
              "命令列參數覆蓋設定檔")
        s = settings_for("--config", str(cfg2), "--no-html", "--open")
        check(s["html"] is False and s["open_report"] is False, "--no-html 覆蓋 html = true，此時 --open 不作用")
        cfg3 = tmp / "min.toml"
        cfg3.write_text("folders = ['X']\nfields = ['A']\n", encoding="utf-8")
        s = settings_for("--config", str(cfg3))
        check(s["max_files"] == 5 and s["top"] == 5 and s["html"] is False and s["open_report"] is False
              and s["output_dir"] == Path("."), "設定檔沒寫的項目使用預設值（5、5、只產生 txt、不開啟、目前資料夾）")
        s = settings_for("--config", str(cfg3), "--html")
        check(s["html"] is True and s["open_report"] is False, "--html 覆蓋預設值，沒加 --open 不開啟報告")

        # 5. 已移除的參數、自動讀取 config.toml
        for removed in (["--folders", "f.txt"], ["--fields", "f.txt"], ["--no-open"]):
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    finder.parse_args(removed)
                    rejected = False
                except SystemExit:
                    rejected = True
            check(rejected, f"已移除的命令列參數 {removed[0]} 會被拒絕")
        (tmp / "config.toml").write_text("folders = ['AUTO']\nfields = ['B']\n", encoding="utf-8")
        s = settings_for()
        check(s["folders"] == ["AUTO"], "未指定 --config 時自動讀取目前資料夾的 config.toml")

        # 6. 錯誤處理
        (tmp / "config.toml").unlink()
        check("找不到設定檔" in config_error("--config", "nope.toml"), "指定的設定檔不存在：明確錯誤")
        check("沒有指定資料夾" in config_error(), "沒有設定檔也沒有參數：提示資料夾")
        e = tmp / "e1.toml"
        e.write_text("folders = ['X']\nfields = []\n", encoding="utf-8")
        check("需求欄位為空" in config_error("--config", str(e)), "fields 為空：明確錯誤")
        e.write_text("folders = ['X']\nfields = ['A']\nmax_files = 0\n", encoding="utf-8")
        check("max_files" in config_error("--config", str(e)), "max_files = 0：明確錯誤")
        e.write_text("folders = ['X']\nfields = ['A']\ntop = '5'\n", encoding="utf-8")
        check("top" in config_error("--config", str(e)), "top 為字串：明確錯誤")
        e.write_text("folders = 'X'\nfields = [1, 2]\n", encoding="utf-8")
        check("fields" in config_error("--config", str(e)), "fields 不是字串清單：明確錯誤")
        e.write_text("folders = ['X']\nfields = ['A']\nopen_report = true\n", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            s = settings_for("--config", str(e), "--html")
        check("open_report" in err.getvalue() and s["open_report"] is False, "已移除的 open_report：顯示忽略警告且不作用")
        e.write_text("folders = ['X']\nfields = ['A']\nhtml = 1\n", encoding="utf-8")
        check("html" in config_error("--config", str(e)), "html 不是布林值：明確錯誤")
        e.write_text("folders = ['X']\nfields = ['A']\nfeilds = ['typo']\n", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            settings_for("--config", str(e))
        check("feilds" in err.getvalue(), "拼錯的設定項目會顯示警告")
        check("≥ 1" in config_error("--config", str(cfg3), "--top", "0"), "--top 0：明確錯誤")

        # 7. 端對端：設定檔 → 搜尋 → 輸出到 output_dir
        data = tmp / "sql"
        data.mkdir()
        (data / "a.sql").write_text("SELECT t.acct AS 帳號, t.amt AS TXN_AMT FROM x t", encoding="utf-8")
        (data / "b.SQL").write_text("SELECT 交易序號 FROM y", encoding="cp950")
        (data / "c.bak").write_text("SELECT 幣別 FROM z", encoding="utf-8")
        run = tmp / "run.toml"
        run.write_text(f"folders = ['{data}']\nfields = ['帳號', 'TXN_AMT', '交易序號', '幣別']\noutput_dir = 'reports/today'\n",
                       encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = finder.main(["--config", str(run)])
        text = out.getvalue()
        check(rc == 0, "端對端：執行成功")
        check((tmp / "reports/today/result.txt").is_file() and not (tmp / "reports/today/report.html").exists(),
              "端對端：預設只寫 result.txt，output_dir 自動建立，不產生 report.html")
        check("HTML 報告" not in text, "端對端：預設不顯示 HTML 報告路徑")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            finder.main(["--config", str(run), "--html"])
        check((tmp / "reports/today/report.html").is_file() and "HTML 報告" in out.getvalue(), "端對端：--html 產生 report.html")
        run.write_text(run.read_text(encoding="utf-8") + "html = true\n", encoding="utf-8")
        (tmp / "reports/today/report.html").unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            finder.main(["--config", str(run)])
        check((tmp / "reports/today/report.html").is_file(), "端對端：設定檔 html = true 產生 report.html")
        check("75% (3/4)" in text and "缺少欄位   : 1（幣別）" in text, "端對端：.bak 不掃描，覆蓋 3/4、缺少 幣別")
        run.write_text(f"folders = ['{data}']\nfields = ['幣別']\nextensions = ['.bak']\n", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            finder.main(["--config", str(run), "--output-dir", str(tmp / "o3")])
        check("100% (1/1)" in out.getvalue(), "端對端：extensions 改成 .bak 後可掃描 .bak 檔")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = finder.main(["--config", str(tmp / "nope.toml")])
        check(rc == 2 and "找不到設定檔" in err.getvalue(), "端對端：設定錯誤時 exit code 2 並顯示原因")
    finally:
        os.chdir(old_cwd)

print("\n全部通過" if not failures else f"\n失敗 {len(failures)} 項")
sys.exit(1 if failures else 0)
