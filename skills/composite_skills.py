# skills/composite_skills.py

from __future__ import annotations

import json
from typing import Callable

from skills.calculator import calculator
from skills.file_reader import file_reader
from skills.table_analyzer import table_analyzer
from skills.format_converter import format_converter


class CompositeSkillError(Exception):
    """复合Skill执行失败的专用异常"""
    pass


def _run_step(step_name: str, func: Callable, **kwargs) -> dict:
    """
    执行单个步骤，统一处理结果。
    
    Returns:
        步骤的业务结果（不是SkillResult，因为复合Skill内部不调b2_run_skill.py）
    """
    try:
        result = func(**kwargs)
        return {"status": "success", "result": result}
    except Exception as exc:
        raise CompositeSkillError(f"Step '{step_name}' failed: {exc}") from exc


def read_and_convert(
    path: str,
    target_format: str = "markdown",
    max_chars: int = 5000,
    *,
    data_root: str | None = None,
) -> dict:
    """
    复合Skill：读取文件 → 转换格式
    
    Args:
        path: 文件路径（支持txt/md/csv/tsv）
        target_format: 目标格式，"markdown"或"json"
        max_chars: 最大读取字符数
        data_root: 数据根目录
    
    Returns:
        包含各步骤结果的字典
    """
    # Step 1: 读取文件
    step1 = _run_step(
        "file_reader",
        file_reader,
        path=path,
        max_chars=max_chars,
        data_root=data_root
    )
    content = step1["result"]["content"]
    
    # Step 2: 转换格式
    step2 = _run_step(
        "format_converter",
        format_converter,
        text=content,
        target_format=target_format
    )
    
    return {
        "composite_name": "read_and_convert",
        "steps": ["file_reader", "format_converter"],
        "step_results": {
            "file_reader": step1["result"],
            "format_converter": step2["result"]
        },
        "final_output": step2["result"]["formatted_text"]
    }


def analyze_and_convert(
    path: str,
    target_format: str = "markdown",
    max_rows_preview: int = 10,
    describe: bool = True,
    *,
    data_root: str | None = None,
) -> dict:
    """
    复合Skill：分析表格 → 转换格式为报告
    
    Args:
        path: CSV/TSV文件路径
        target_format: 目标格式，"markdown"或"json"
        max_rows_preview: 预览行数
        describe: 是否输出统计摘要
        data_root: 数据根目录
    
    Returns:
        包含分析结果和转换后报告的字典
    """
    # Step 1: 分析表格
    step1 = _run_step(
        "table_analyzer",
        table_analyzer,
        path=path,
        max_rows_preview=max_rows_preview,
        describe=describe,
        data_root=data_root
    )
    analysis = step1["result"]
    
    # Step 2: 将分析结果转为文本
    report_text = _format_analysis_as_text(analysis)
    
    # Step 3: 转换为目标格式
    step3 = _run_step(
        "format_converter",
        format_converter,
        text=report_text,
        target_format=target_format
    )
    
    return {
        "composite_name": "analyze_and_convert",
        "steps": ["table_analyzer", "format_converter"],
        "step_results": {
            "table_analyzer": analysis,
            "format_converter": step3["result"]
        },
        "final_output": step3["result"]["formatted_text"],
        "generated_file_path": step3["result"].get("generated_file_path")
    }


def _format_analysis_as_text(analysis: dict) -> str:
    """将table_analyzer的结果格式化为可读文本"""
    lines = [
        f"# 表格分析报告",
        f"",
        f"## 基本信息",
        f"- 文件路径: {analysis['path']}",
        f"- 行数: {analysis['num_rows']}",
        f"- 列数: {analysis['num_columns']}",
        f"- 列名: {', '.join(analysis['columns'])}",
        f"",
        f"## 数据预览",
    ]
    
    for i, row in enumerate(analysis['preview'][:5], 1):
        lines.append(f"### 第{i}行")
        for col, val in row.items():
            lines.append(f"- {col}: {val}")
        lines.append("")
    
    if analysis.get('describe'):
        lines.append("## 统计摘要")
        for col, stats in analysis['describe'].items():
            lines.append(f"### {col}")
            for stat_name, stat_val in stats.items():
                lines.append(f"- {stat_name}: {stat_val}")
            lines.append("")
    
    return "\n".join(lines)


# 复合Skill注册表（供B3动态发现）
COMPOSITE_SKILLS = {
    "read_and_convert": read_and_convert,
    "analyze_and_convert": analyze_and_convert,
}