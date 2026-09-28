"""异常定义。"""


class DQError(Exception):
    """所有 dqtool 错误的基类。"""


class CorruptDataError(DQError):
    """输入数据损坏或无法解析。

    属性：
        source: 文件路径或 "<memory>" 等来源标识
        row_number: 1 起始的数据行位置（文件级错误可能为 None）
    """

    def __init__(self, message, source=None, row_number=None):
        self.source = source
        self.row_number = row_number
        location = source or "输入"
        if row_number is not None:
            location = f"{location} 第 {row_number} 行"
        super().__init__(f"{location}: {message}")


class RuleError(DQError):
    """规则配置错误。"""
