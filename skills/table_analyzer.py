from __future__ import annotations

import csv
import statistics

from skills import resolve_data_path


# 内联定义资源限制（避免模块导入缓存问题）
_MAX_TABLE_ROWS = 100000           # 最大表格行数
_MAX_TABLE_SIZE_MB = 50            # 最大表格文件大小(MB)


def _check_file_size(path, max_mb):
    """检查文件大小是否超限"""
    if not path.exists():
        return
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > max_mb:
        raise ValueError(
            f"file too large: {size_mb:.1f}MB (max {max_mb}MB): {path}"
        )


def table_analyzer(
    path: str,
    max_rows_preview: int = 5,
    describe: bool = True,
    *,
    data_root: str | None = None,
) -> dict:
    """表格分析（带资源限制）。

    Args:
        path: CSV/TSV文件路径
        max_rows_preview: 预览行数
        describe: 是否输出统计摘要
        data_root: 数据根目录

    Returns:
        业务结果字典
    """
    if not isinstance(max_rows_preview, int) or isinstance(max_rows_preview, bool) or max_rows_preview < 0:
        raise ValueError("max_rows_preview must be a non-negative integer")

    source, root = resolve_data_path(path, data_root)
    if source.suffix.lower() not in {".csv", ".tsv"}:
        raise ValueError("table_analyzer only supports .csv and .tsv files")
    if not source.is_file():
        raise FileNotFoundError(f"table file not found: {path}")

    # 检查文件大小
    _check_file_size(source, _MAX_TABLE_SIZE_MB)

    delimiter = "\t" if source.suffix.lower() == ".tsv" else ","
    with source.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if not reader.fieldnames:
            raise ValueError("table must contain a header row")

        # 限制行数
        rows = []
        for i, row in enumerate(reader):
            if i >= _MAX_TABLE_ROWS:
                break
            rows.append(row)

        columns = list(reader.fieldnames)

    stats: dict[str, dict] = {}
    if describe:
        for column in columns:
            raw_values = [row.get(column, "").strip() for row in rows]
            if not raw_values or any(value == "" for value in raw_values):
                continue
            try:
                values = [float(value) for value in raw_values]
            except ValueError:
                continue
            stats[column] = {
                "count": len(values),
                "min": min(values),
                "max": max(values),
                "mean": statistics.fmean(values),
            }

    return {
        "path": source.relative_to(root).as_posix(),
        "num_rows": len(rows),
        "num_columns": len(columns),
        "columns": columns,
        "preview": rows[:max_rows_preview],
        "describe": stats,
        "truncated": len(rows) >= _MAX_TABLE_ROWS,
    }