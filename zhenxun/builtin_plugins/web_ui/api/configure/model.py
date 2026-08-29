from typing import Literal

from pydantic import BaseModel, Field


class DatabaseTest(BaseModel):
    db_url: str
    """待测试的数据库地址"""


class RedisTest(BaseModel):
    redis_host: str
    """Redis 地址"""
    redis_port: int = Field(default=6379, ge=1, le=65535)
    """Redis 端口"""
    redis_password: str = ""
    """Redis 密码"""


class Setting(BaseModel):
    superusers: list[str]
    """超级用户列表"""
    db_url: str
    """数据库地址"""
    host: str
    """主机地址"""
    port: int = Field(ge=1, le=65535)
    """端口"""
    username: str
    """前端用户名"""
    password: str
    """前端密码"""
    cache_mode: Literal["NONE", "MEMORY", "REDIS"] = "MEMORY"
    """缓存模式"""
    redis_host: str = "127.0.0.1"
    """Redis 地址"""
    redis_port: int = Field(default=6379, ge=1, le=65535)
    """Redis 端口"""
    redis_password: str = ""
    """Redis 密码"""
