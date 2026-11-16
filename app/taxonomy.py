"""观测记录与分类沿革 CSV 的解析、校验与逐轮追踪。

本模块为纯函数实现，不依赖 Flask / 数据库，便于独立测试。

业务规则：
- 观测 CSV：record_no（唯一记录号）、initial_taxon（初始分类号）、quantity（非负整数），至多 100 行；
- 沿革 CSV：round（轮次，1~4 且从 1 开始连续）、old_id（旧号）、new_id（新号），至多 40 行；
- 同一轮内允许一对多（拆分）与多对一（归并）；未列出的编号沿用到下一轮；
- 逐轮追踪：每轮把当前编号集合整体映射一次（同一轮内不做链式展开）；
- 全部轮次结束后，所有路径落到同一编号 -> 可用；仍有多个去向 -> 待核，数量不能分摊。
"""
from __future__ import annotations

import csv
import io
import re

OBS_MAX_ROWS = 100
TAX_MAX_ROWS = 40
MAX_ROUND = 4
MAX_CELL_LEN = 64
MAX_ERRORS = 20


class CsvError(Exception):
    """输入 CSV 校验失败，异常消息面向用户（可含多行，每行一条问题）。"""


_OBS_ALIASES = {
    "record_no": {"record_no", "record_id", "id", "记录号", "唯一记录号"},
    "initial_taxon": {"initial_taxon", "initial_taxon_id", "taxon", "taxon_id",
                      "初始分类号", "初始编号"},
    "quantity": {"quantity", "count", "qty", "数量"},
}
_TAX_ALIASES = {
    "round": {"round", "轮次"},
    "old_id": {"old_id", "old", "旧号", "旧分类号"},
    "new_id": {"new_id", "new", "新号", "新分类号"},
}

_INT_RE = re.compile(r"^[0-9]+$")


def decode_csv_bytes(data: bytes) -> str:
    """把上传的字节解码为文本，依次尝试 UTF-8(含 BOM) 与 GB18030。"""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvError("文件编码无法识别，请使用 UTF-8 或 GBK 编码的 CSV")


def _read_table(text: str, aliases: dict, label: str):
    """读取表头并定位必需列，返回 (数据行列表, {字段: 列下标})。"""
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise CsvError(f"{label}CSV 为空")
    header = [h.strip().lstrip("﻿") for h in rows[0]]  # 防御性去除 BOM
    columns = {}
    used = set()
    for field, names in aliases.items():
        idx = None
        for i, h in enumerate(header):
            if i not in used and h.lower() in names:
                idx = i
                break
        if idx is None:
            raise CsvError(
                f"{label}CSV 缺少必需列 {field}"
                f"（可用列名：{'、'.join(sorted(names))}）")
        used.add(idx)
        columns[field] = idx
    return rows[1:], columns


def _cell(row: list, idx: int) -> str:
    return row[idx].strip() if idx < len(row) else ""


def parse_observations(text: str) -> list:
    """解析并校验观测 CSV，返回 [{'record_no', 'initial_taxon', 'quantity'}]。"""
    data_rows, col = _read_table(text, _OBS_ALIASES, "观测")
    errors = []
    if len(data_rows) > OBS_MAX_ROWS:
        errors.append(f"观测记录至多 {OBS_MAX_ROWS} 行，实际 {len(data_rows)} 行")
    seen = set()
    out = []
    for lineno, row in enumerate(data_rows, start=2):
        rec = _cell(row, col["record_no"])
        tax = _cell(row, col["initial_taxon"])
        qty = _cell(row, col["quantity"])
        row_ok = True
        if not rec:
            errors.append(f"第{lineno}行：记录号为空")
            row_ok = False
        elif len(rec) > MAX_CELL_LEN:
            errors.append(f"第{lineno}行：记录号超过 {MAX_CELL_LEN} 字符")
            row_ok = False
        elif rec in seen:
            errors.append(f"第{lineno}行：记录号「{rec}」重复")
            row_ok = False
        else:
            seen.add(rec)
        if not tax:
            errors.append(f"第{lineno}行：初始分类号为空")
            row_ok = False
        elif len(tax) > MAX_CELL_LEN:
            errors.append(f"第{lineno}行：初始分类号超过 {MAX_CELL_LEN} 字符")
            row_ok = False
        if not _INT_RE.match(qty):
            errors.append(f"第{lineno}行：数量须为非负整数，实际为「{qty or '空'}」")
            row_ok = False
        if row_ok:
            out.append({"record_no": rec, "initial_taxon": tax, "quantity": int(qty)})
        if len(errors) >= MAX_ERRORS:
            errors.append(f"……错误过多，仅列出前 {MAX_ERRORS} 条")
            break
    if errors:
        raise CsvError("\n".join(errors))
    if not out:
        raise CsvError("观测 CSV 没有数据行")
    return out


def parse_taxonomy(text: str):
    """解析并校验沿革 CSV。

    返回 (rounds_map, max_round, row_count)：
    rounds_map[r][old_id] = {new_id, ...}，row_count 为原始数据行数。
    """
    data_rows, col = _read_table(text, _TAX_ALIASES, "分类沿革")
    errors = []
    if len(data_rows) > TAX_MAX_ROWS:
        errors.append(f"分类沿革至多 {TAX_MAX_ROWS} 行，实际 {len(data_rows)} 行")
    entries = []
    seen = set()
    rounds_present = set()
    for lineno, row in enumerate(data_rows, start=2):
        rnd = _cell(row, col["round"])
        old = _cell(row, col["old_id"])
        new = _cell(row, col["new_id"])
        row_ok = True
        if not _INT_RE.match(rnd) or not 1 <= int(rnd) <= MAX_ROUND:
            errors.append(f"第{lineno}行：轮次须为 1~{MAX_ROUND} 的整数，实际为「{rnd or '空'}」")
            row_ok = False
        if not old:
            errors.append(f"第{lineno}行：旧号为空")
            row_ok = False
        elif len(old) > MAX_CELL_LEN:
            errors.append(f"第{lineno}行：旧号超过 {MAX_CELL_LEN} 字符")
            row_ok = False
        if not new:
            errors.append(f"第{lineno}行：新号为空")
            row_ok = False
        elif len(new) > MAX_CELL_LEN:
            errors.append(f"第{lineno}行：新号超过 {MAX_CELL_LEN} 字符")
            row_ok = False
        if row_ok:
            key = (int(rnd), old, new)
            if key not in seen:  # 完全重复的行语义相同，忽略
                seen.add(key)
                entries.append(key)
                rounds_present.add(int(rnd))
        if len(errors) >= MAX_ERRORS:
            errors.append(f"……错误过多，仅列出前 {MAX_ERRORS} 条")
            break
    if rounds_present:
        max_round = max(rounds_present)
        if rounds_present != set(range(1, max_round + 1)):
            errors.append(
                f"轮次必须从 1 开始连续，实际出现的轮次：{sorted(rounds_present)}")
    if errors:
        raise CsvError("\n".join(errors))
    if not entries:
        raise CsvError("分类沿革 CSV 没有数据行")
    rounds_map = {r: {} for r in range(1, max_round + 1)}
    for rnd, old, new in entries:
        rounds_map[rnd].setdefault(old, set()).add(new)
    return rounds_map, max_round, len(data_rows)


def trace_one(initial: str, rounds_map: dict, max_round: int):
    """逐轮追踪一个初始编号。

    返回 (最终编号集合, 首次出现多去向的轮次或 None)。
    每轮对当前集合整体映射一次：列出的编号替换为其全部新号，未列出的沿用。
    """
    current = {initial}
    split_round = None
    for rnd in range(1, max_round + 1):
        mapping = rounds_map.get(rnd, {})
        nxt = set()
        for taxon in current:
            if taxon in mapping:
                nxt |= mapping[taxon]
            else:
                nxt.add(taxon)
        current = nxt
        if split_round is None and len(current) > 1:
            split_round = rnd
    return current, split_round


def compute_results(obs_rows: list, rounds_map: dict, max_round: int):
    """对全部观测记录逐轮追踪，返回 (可用列表, 待核列表)。"""
    usable, pending = [], []
    for row in obs_rows:
        finals, split_round = trace_one(row["initial_taxon"], rounds_map, max_round)
        if len(finals) == 1:
            usable.append({**row, "final_taxon": next(iter(finals))})
        else:
            dests = "、".join(sorted(finals))
            reason = (
                f"第{split_round}轮发生拆分，历经{max_round}轮沿革后仍有 "
                f"{len(finals)} 个去向（{dests}），未归并到同一编号；"
                f"数量不能分摊，需人工核定")
            pending.append({**row, "final_taxons": dests, "reason": reason})
    return usable, pending
