# 命令行公共工具——两个入口（单 Agent / 多 Agent）共用横幅渲染
import unicodedata


def display_width(text: str) -> int:
    """
    计算字符串的显示列宽：中文/全角标点占 2 列，其余占 1 列。
    刻意不把 Ambiguous 字符（制表符/• 等）算成 2 列——它们在不同字体下
    宽度不一，因此横幅只使用 ASCII 框线与项目符号，保证任何等宽字体对齐。
    """
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        for ch in text
    )


def print_banner(rows) -> None:
    """
    打印欢迎横幅（ASCII 框线按显示宽度动态生成，任何等宽字体下边框都对齐）。

    rows: [(文本, 样式)]；样式 title 居中、sep 分隔线、
          section/item/example 左对齐并带各自的缩进前缀。
    """
    prefix = {"section": "  ", "item": "  * ", "example": "  > "}

    cells = []  # (kind, 内部文本)
    for text, kind in rows:
        if kind in ("title", "sep"):
            cells.append((kind, text))
        else:
            cells.append((kind, prefix[kind] + text))

    content_w = max(display_width(t) for kind, t in cells if kind != "sep")
    inner_w = content_w + 2  # 左右各留 1 列边距

    lines = ["+" + "-" * inner_w + "+"]
    for kind, text in cells:
        if kind == "sep":
            lines.append("+" + "-" * inner_w + "+")
        elif kind == "title":
            gap = max(inner_w - display_width(text), 0)
            left = gap // 2
            lines.append("|" + " " * left + text + " " * (gap - left) + "|")
        else:
            lines.append("| " + text + " " * (inner_w - 1 - display_width(text)) + "|")
    lines.append("+" + "-" * inner_w + "+")
    print("\n" + "\n".join(lines))
