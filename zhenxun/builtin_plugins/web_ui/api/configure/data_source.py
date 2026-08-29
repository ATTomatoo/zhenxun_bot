import asyncio
import importlib
from typing import Any

from redis.asyncio import Redis
from tortoise.backends.base.config_generator import expand_db_url


async def test_db_connection(db_url: str) -> bool | str:
    """使用独立客户端测试数据库，避免影响 Bot 已有的 ORM 连接。"""
    client: Any | None = None
    try:
        db_config = expand_db_url(db_url.strip())
        engine = importlib.import_module(db_config["engine"])
        client = engine.client_class(
            connection_name="webui_configure_test",
            **db_config["credentials"],
        )

        async def _ping() -> None:
            await client.create_connection(with_db=True)
            await client.execute_query("SELECT 1")

        await asyncio.wait_for(_ping(), timeout=10)
        return True
    except asyncio.TimeoutError:
        return "数据库连接超时，请检查地址、端口和网络。"
    except Exception as e:
        return str(e) or e.__class__.__name__
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


async def test_redis_connection(
    host: str,
    port: int,
    password: str = "",
) -> bool | str:
    """连接 Redis 并执行 PING。"""
    client = Redis(
        host=host.strip(),
        port=port,
        password=password or None,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    try:
        await asyncio.wait_for(client.ping(), timeout=6)
        return True
    except asyncio.TimeoutError:
        return "Redis 连接超时，请检查地址、端口和网络。"
    except Exception as e:
        return str(e) or e.__class__.__name__
    finally:
        await client.aclose()
