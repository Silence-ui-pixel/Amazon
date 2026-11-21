"""可安全返回客户端的业务异常，不包含数据库细节或内部堆栈。"""


class DomainError(Exception):
    """由 API 层转换成统一错误响应，业务层无需依赖 HTTP 框架。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
