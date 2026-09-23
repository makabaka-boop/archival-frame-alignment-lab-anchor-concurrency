"""Pydantic 请求/响应模型。

Pydantic 的类型校验（非数组、元素非字符串等）自动产出 422；域校验
（长度、重复次数、ASCII 范围）在 :mod:`app.validation` 中实现，
于路由内调用以复用同一份错误消息。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class JobCreate(BaseModel):
    left: list[str] = Field(description="左扫描机指纹数组（1..20000 项）")
    right: list[str] = Field(description="右扫描机指纹数组（1..20000 项）")


class AnchorIn(BaseModel):
    anchors: list[tuple[int, int]] = Field(
        default_factory=list,
        description="锚点索引对集合；空数组（或缺省）表示清空锚点、恢复全局最优",
    )
    expected_version: int | None = Field(
        default=None,
        ge=1,
        description=(
            "乐观并发前提：替换所基于的当前作业版本。仅当它与数据库当前版本"
            "一致时替换才会成功；不一致返回 409 且状态不变。"
            "缺省表示不附带前提（兼容旧客户端的无保护替换）。"
        ),
    )

    @field_validator("anchors", mode="before")
    @classmethod
    def _none_becomes_empty(cls, value: object) -> object:
        # 允许 {"anchors": null}，等价于清空。
        return [] if value is None else value


class Pair(BaseModel):
    left_index: int
    right_index: int


class JobOut(BaseModel):
    id: str
    left: list[str]
    right: list[str]
    anchors: list[Pair]
    result: list[Pair]
    length: int
    # 当前持久化版本与最后更新时间：成功响应必须与随后 GET 读到的逐项一致。
    version: int
    updated_at: datetime
