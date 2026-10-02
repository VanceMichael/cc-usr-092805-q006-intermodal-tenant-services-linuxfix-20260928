"""带时区的有效期工具：区间相交、账期裁剪、边界切片、as-of 版本选择。

所有时间统一为 ``timeutil.canonical_instant`` 产生的 UTC ISO 字符串，
因此可以直接按字符串比较先后。区间统一采用左闭右开 ``[start, end)``。
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .timeutil import canonical_instant, parse_instant


def normalize_window(start_at: str, end_at: str) -> tuple[str, str]:
    start = canonical_instant(start_at); end = canonical_instant(end_at)
    if parse_instant(start) >= parse_instant(end):
        raise ValueError("结束时间必须晚于开始时间")
    return start, end


def overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> tuple[str, str] | None:
    """返回两个左闭右开区间的交集，没有交集返回 None。"""
    start = max(a_start, b_start); end = min(a_end, b_end)
    return (start, end) if parse_instant(start) < parse_instant(end) else None


def clip_to_period(start_at: str, end_at: str, period_start: str, period_end: str) -> tuple[str, str] | None:
    """把用量/占用窗口裁剪到账期左闭右开区间内。"""
    return overlap(canonical_instant(start_at), canonical_instant(end_at),
                   canonical_instant(period_start), canonical_instant(period_end))


def hours_between(start_at: str, end_at: str) -> float:
    delta = parse_instant(end_at) - parse_instant(start_at)
    return delta.total_seconds() / 3600.0


def effective_at(record: dict, instant: str) -> bool:
    """判断带 valid_from/valid_to 的记录在某时点是否有效（valid_to 可空表示长期有效）。"""
    point = canonical_instant(instant)
    if point < canonical_instant(record["valid_from"]):
        return False
    valid_to = record.get("valid_to")
    return valid_to is None or point < canonical_instant(valid_to)


def active_during(record: dict, start_at: str, end_at: str) -> bool:
    """记录有效期与给定窗口是否相交。"""
    valid_to = record.get("valid_to")
    window_end = canonical_instant(end_at)
    effective_end = canonical_instant(valid_to) if valid_to is not None else None
    if canonical_instant(record["valid_from"]) >= window_end:
        return False
    return effective_end is None or effective_end > canonical_instant(start_at)


def pick_as_of(versions: Sequence[dict], instant: str) -> dict | None:
    """从版本化实体历史中选取某时点可见版本（valid_from 最新且不晚于 instant）。"""
    point = canonical_instant(instant)
    visible = [v for v in versions if canonical_instant(v["valid_from"]) <= point]
    if not visible:
        return None
    return max(visible, key=lambda v: (canonical_instant(v["valid_from"]), int(v["version"])))


def slice_by_effectivity(window_start: str, window_end: str, segments: Iterable[dict]) -> list[tuple[str, str, dict]]:
    """把一个窗口按若干带有效期的记录切成若干子窗口。

    每个 segment 需含 valid_from，可含空 valid_to。同一子窗口内只保留
    valid_from 最大（即最新生效）的记录，模拟“后来的变更只影响之后的账”。
    返回 [(segment_start, segment_end, segment_or_None), ...]，没有任何
    记录覆盖的子窗口 segment 为 None。
    """
    window_start = canonical_instant(window_start); window_end = canonical_instant(window_end)
    bounds = {window_start, window_end}
    covering: list[tuple[str, str, dict]] = []
    for seg in segments:
        seg_start = canonical_instant(seg["valid_from"])
        seg_end = canonical_instant(seg["valid_to"]) if seg.get("valid_to") is not None else None
        win_start = max(seg_start, window_start)
        win_end = window_end if seg_end is None else min(seg_end, window_end)
        if parse_instant(win_start) < parse_instant(win_end):
            covering.append((win_start, win_end, seg)); bounds.add(win_start); bounds.add(win_end)
    ordered = sorted(bounds)
    result: list[tuple[str, str, dict | None]] = []
    for left, right in zip(ordered, ordered[1:]):
        if parse_instant(left) >= parse_instant(window_end):
            break
        chosen = None
        for win_start, _win_end, seg in covering:
            if win_start <= left and (seg.get("valid_to") is None or canonical_instant(seg["valid_to"]) > left):
                if chosen is None or canonical_instant(seg["valid_from"]) > canonical_instant(chosen["valid_from"]):
                    chosen = seg
        result.append((left, right, chosen))
    return result
