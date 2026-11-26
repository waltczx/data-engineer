"""笔试题统一入口：按模块运行已经实现的 Python 脚本。

用法：
    python run_all.py --download        # 只下载两份数据集，不运行任何模块
    python run_all.py --check           # 只检查文件路径，不读取 CSV 内容
    python run_all.py --module q1       # 只运行第一题
    python run_all.py                   # 依次尝试所有模块

当前是运行框架。数据缺失时会自动下载；Q1/Q3/Q4 脚本完成后会由这里调用；
Q2 的 MySQL 导入与 SQL 执行、Q5 的文档检查仍需后续接入。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RETAIL_CSV = DATA_DIR / "online_retail_ii.csv"
OLIST_DIR = DATA_DIR / "olist"
OLIST_FILES = (
    "olist_customers_dataset.csv",
    "olist_geolocation_dataset.csv",
    "olist_order_items_dataset.csv",
    "olist_order_payments_dataset.csv",
    "olist_order_reviews_dataset.csv",
    "olist_orders_dataset.csv",
    "olist_products_dataset.csv",
    "olist_sellers_dataset.csv",
    "product_category_name_translation.csv",
)
RETAIL_HANDLE = "mashlyn/online-retail-ii-uci"
OLIST_HANDLE = "olistbr/brazilian-ecommerce"

MODULES: dict[str, dict[str, object]] = {
    "q1": {
        "script": ROOT / "q1_data_quality" / "q1_code.py",
        "inputs": (RETAIL_CSV,),
    },
    "q2": {
        "script": None,  # TODO: 接入 MySQL 8.0 导入及四份 SQL 的执行。
        "inputs": tuple(OLIST_DIR / name for name in OLIST_FILES),
    },
    "q3": {
        "script": ROOT / "q3_etl_pipeline" / "pipeline.py",
        "inputs": (RETAIL_CSV,),
    },
    "q4": {
        "script": ROOT / "q4_llm_extraction" / "extract_reviews.py",
        "inputs": (OLIST_DIR / "olist_order_reviews_dataset.csv",),
    },
    "q5": {
        "script": None,  # TODO: 人工核查系统设计文档，不需要读取数据集。
        "inputs": (),
    },
}


def download_dataset(handle: str) -> Path:
    """通过 Kaggle 下载数据集，返回缓存目录。"""
    try:
        import kagglehub
    except ImportError as exc:
        raise RuntimeError(
            "缺少 kagglehub；请先执行 pip install -r requirements.txt"
        ) from exc

    print(f"从 Kaggle 下载：{handle}", flush=True)
    try:
        folder = Path(kagglehub.dataset_download(handle))
    except Exception as exc:
        raise RuntimeError(
            f"下载 {handle} 失败；请检查网络及 Kaggle 账号认证：{exc}"
        ) from exc
    if not folder.is_dir():
        raise RuntimeError(f"Kaggle 未返回有效的数据集目录：{folder}")
    return folder


def find_file(folder: Path, expected_name: str) -> Path:
    """在下载目录中按文件名查找，兼容嵌套目录和大小写差异。"""
    matches = [
        path
        for path in folder.rglob("*")
        if path.is_file() and path.name.casefold() == expected_name.casefold()
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"在 {folder} 中预期找到 1 个 {expected_name}，实际找到 {len(matches)} 个"
        )
    return matches[0]


def copy_if_missing(source: Path, destination: Path) -> None:
    """先复制到同目录临时文件，再放入目标位置，避免半份 CSV。"""
    if destination.is_file():
        print(f"已存在，跳过：{destination.relative_to(ROOT)}")
        return
    if destination.exists():
        raise RuntimeError(f"目标路径不是普通文件：{destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        # 下载期间若用户已放入文件，不覆盖它。
        if destination.exists():
            print(f"已存在，跳过：{destination.relative_to(ROOT)}")
            return
        temporary.replace(destination)
        print(f"已准备：{destination.relative_to(ROOT)}")
    finally:
        temporary.unlink(missing_ok=True)


def ensure_retail() -> None:
    """确保 Online Retail II 位于 data/online_retail_ii.csv。"""
    if RETAIL_CSV.is_file():
        print("Online Retail II 已存在，无需下载。")
        return
    folder = download_dataset(RETAIL_HANDLE)
    source = find_file(folder, "online_retail_II.csv")
    copy_if_missing(source, RETAIL_CSV)


def ensure_olist() -> None:
    """确保 Olist 的 9 张表均位于 data/olist/。"""
    missing = [name for name in OLIST_FILES if not (OLIST_DIR / name).is_file()]
    if not missing:
        print("Olist 的 9 个文件已存在，无需下载。")
        return
    folder = download_dataset(OLIST_HANDLE)
    # 先确认下载包包含所有缺失文件，再开始复制。
    sources = {name: find_file(folder, name) for name in missing}
    for name, source in sources.items():
        copy_if_missing(source, OLIST_DIR / name)


def missing_inputs(module: str) -> list[Path]:
    """只检查路径是否存在，不打开输入文件。"""
    inputs = MODULES[module]["inputs"]
    assert isinstance(inputs, tuple)
    return [path for path in inputs if not path.is_file()]


def run_script(script: Path) -> int:
    """用当前 Python 环境执行一个模块，返回其退出码。"""
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=ROOT,
            check=False,
        )
    except OSError as exc:
        print(f"执行失败：{script.relative_to(ROOT)}：{exc}", file=sys.stderr)
        return 1
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="数据工程师笔试题统一入口")
    parser.add_argument(
        "--module",
        choices=("all", *MODULES),
        default="all",
        help="选择要运行的模块，默认依次运行全部模块",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只核对输入文件和脚本路径，不执行模块、不读取 CSV",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="只下载两份数据集到 data/，不运行任何模块",
    )
    args = parser.parse_args()

    if args.download:
        # 两份数据集分别尝试：一份失败不影响另一份，已存在的文件会自动跳过。
        download_failed = False
        for label, ensure in (("Online Retail II", ensure_retail), ("Olist", ensure_olist)):
            try:
                ensure()
            except (OSError, RuntimeError) as exc:
                print(f"{label} 下载失败：{exc}", file=sys.stderr)
                download_failed = True
        return 1 if download_failed else 0

    selected = MODULES if args.module == "all" else {args.module: MODULES[args.module]}
    failed = False
    pending = False

    if not args.check:
        needs_retail = any(name in selected for name in ("q1", "q3"))
        needs_olist = any(name in selected for name in ("q2", "q4"))
        if needs_retail and not RETAIL_CSV.is_file():
            try:
                ensure_retail()
            except (OSError, RuntimeError) as exc:
                print(f"Online Retail II 下载失败：{exc}", file=sys.stderr)
                failed = True
        if needs_olist and any(
            not (OLIST_DIR / name).is_file() for name in OLIST_FILES
        ):
            try:
                ensure_olist()
            except (OSError, RuntimeError) as exc:
                print(f"Olist 下载失败：{exc}", file=sys.stderr)
                failed = True

    for name, config in selected.items():
        print(f"\n[{name.upper()}]")
        missing = missing_inputs(name)
        if missing:
            for path in missing:
                print(f"缺少输入文件：{path.relative_to(ROOT)}")
            if args.check:
                print("--check 模式不会下载；正常运行时会尝试自动下载。")
            else:
                print("自动下载未能补齐这些文件。")
            failed = True
            continue

        script = config["script"]
        if script is None:
            print("尚未接入自动执行；请按模块要求完成 SQL 或设计文档。")
            pending = True
            continue
        assert isinstance(script, Path)
        if not script.is_file():
            print(f"尚未实现脚本：{script.relative_to(ROOT)}")
            pending = True
            continue

        if args.check:
            print(f"路径检查通过：{script.relative_to(ROOT)}")
            continue

        print(f"运行：{script.relative_to(ROOT)}", flush=True)
        exit_code = run_script(script)
        if exit_code:
            print(f"模块失败，退出码 {exit_code}；继续检查下一模块。", file=sys.stderr)
            failed = True
        else:
            print("模块运行完成。")

    if failed:
        return 1
    if pending:
        print("\n部分模块尚未接入；当前入口只是运行框架。")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
