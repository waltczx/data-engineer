"""下载流程测试：只使用临时模拟文件，不访问真实数据集或网络。"""

from contextlib import redirect_stdout
from io import StringIO
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_all


class RunAllDownloadTests(unittest.TestCase):
    def test_empty_directory_downloads_and_renames_both_datasets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            retail_source = base / "cache" / "retail" / "nested"
            retail_source.mkdir(parents=True)
            (retail_source / "online_retail_II.csv").write_bytes(b"retail fixture")

            olist_source = base / "cache" / "olist"
            olist_source.mkdir(parents=True)
            for name in run_all.OLIST_FILES:
                (olist_source / name).write_bytes(name.encode("ascii"))

            target = base / "project" / "data"
            with (
                patch.object(run_all, "ROOT", base / "project"),
                patch.object(run_all, "RETAIL_CSV", target / "online_retail_ii.csv"),
                patch.object(run_all, "OLIST_DIR", target / "olist"),
                patch.object(
                    run_all,
                    "download_dataset",
                    side_effect=[retail_source.parent, olist_source],
                ) as mocked_download,
            ):
                run_all.ensure_retail()
                run_all.ensure_olist()

            self.assertEqual((target / "online_retail_ii.csv").read_bytes(), b"retail fixture")
            self.assertEqual(
                {path.name for path in (target / "olist").iterdir()},
                set(run_all.OLIST_FILES),
            )
            self.assertEqual(mocked_download.call_count, 2)

    def test_existing_files_are_not_downloaded_or_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            retail = base / "data" / "online_retail_ii.csv"
            olist = base / "data" / "olist"
            olist.mkdir(parents=True)
            retail.write_bytes(b"existing retail")
            for name in run_all.OLIST_FILES:
                (olist / name).write_bytes(b"existing olist")

            with (
                patch.object(run_all, "RETAIL_CSV", retail),
                patch.object(run_all, "OLIST_DIR", olist),
                patch.object(run_all, "download_dataset") as mocked_download,
            ):
                run_all.ensure_retail()
                run_all.ensure_olist()

            mocked_download.assert_not_called()
            self.assertEqual(retail.read_bytes(), b"existing retail")
            self.assertEqual((olist / run_all.OLIST_FILES[0]).read_bytes(), b"existing olist")

    def test_run_all_requests_download_when_q1_input_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            retail = Path(temp_dir) / "data" / "online_retail_ii.csv"

            def fake_download() -> None:
                retail.parent.mkdir(parents=True)
                retail.write_bytes(b"fixture")

            with (
                patch.object(run_all, "RETAIL_CSV", retail),
                patch.object(
                    run_all,
                    "MODULES",
                    {"q1": {"script": None, "inputs": (retail,)}},
                ),
                patch.object(run_all, "ensure_retail", side_effect=fake_download) as download,
                patch("sys.argv", ["run_all.py", "--module", "q1"]),
                redirect_stdout(StringIO()),
            ):
                result = run_all.main()

            download.assert_called_once_with()
            self.assertEqual(result, 2)  # 数据已补齐，模块脚本仍待实现。
            self.assertTrue(retail.is_file())


if __name__ == "__main__":
    unittest.main()
