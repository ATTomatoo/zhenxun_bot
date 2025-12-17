# 权限检查系统 - 缓存优化指南

## DataAccess 缓存机制说明

### 已实现的缓存功能

`DataAccess` 类已经实现了完整的 Redis 缓存机制，包括：

1. **自动缓存查询结果**
   ```python
   # 第一次查询：从数据库获取并缓存
   plugin = await plugin_dao.safe_get_or_none(module="example")
   
   # 后续查询：直接从缓存获取（如果缓存未过期）
   plugin = await plugin_dao.safe_get_or_none(module="example")
   ```

2. **空结果缓存（5分钟TTL）**
   ```python
   # 查询不存在的记录时，会缓存空结果标记
   # 避免频繁查询数据库确认记录不存在
   user = await user_dao.safe_get_or_none(user_id="nonexistent")
   # 5分钟内再次查询相同记录，直接从缓存返回 None
   ```

3. **缓存统计**
   ```python
   from zhenxun.services.data_access import DataAccess
   
   # 获取缓存统计信息
   stats = DataAccess.get_cache_stats()
   # 返回格式：
   # [
   #   {
   #     "cache_type": "GLOBAL_ALL_PLUGINS",
   #     "hits": 1000,        # 缓存命中次数
   #     "null_hits": 50,     # 空结果缓存命中次数
   #     "misses": 200,       # 缓存未命中次数
   #     "hit_rate": "84.00%" # 缓存命中率
   #   },
   #   ...
   # ]
   ```

4. **自动缓存失效**
   - 数据创建、更新、删除时自动清除相关缓存
   - 支持手动清除：`await dao.clear_cache(**kwargs)`

## 权限检查中的缓存优化

### 当前缓存使用情况

在权限检查系统中，以下数据访问已经自动使用缓存：

1. **PluginInfo（插件信息）**
   ```python
   plugin_dao = DataAccess(PluginInfo)
   plugin = await plugin_dao.safe_get_or_none(module=module)
   # ✅ 自动缓存，基于 module 字段
   ```

2. **UserConsole（用户信息）**
   ```python
   user_dao = DataAccess(UserConsole)
   user = await user_dao.get_by_func_or_none(
       UserConsole.get_user, False, user_id=user_id
   )
   # ✅ 自动缓存，基于 user_id 字段
   ```

3. **GroupConsole（群组信息）**
   ```python
   group_dao = DataAccess(GroupConsole)
   group = await group_dao.safe_get_or_none(
       group_id=group_id, channel_id__isnull=True
   )
   # ✅ 自动缓存，基于 group_id 字段
   ```

4. **BanConsole（ban记录）**
   ```python
   ban_dao = DataAccess(BanConsole)
   ban = await ban_dao.safe_get_or_none(
       user_id=user_id, group_id=group_id
   )
   # ✅ 自动缓存，基于 user_id 和 group_id
   ```

5. **BotConsole（Bot信息）**
   ```python
   bot_dao = DataAccess(BotConsole)
   bot = await bot_dao.safe_get_or_none(bot_id=bot_id)
   # ✅ 自动缓存，基于 bot_id 字段
   ```

6. **LevelUser（用户权限）**
   ```python
   level_dao = DataAccess(LevelUser)
   level = await level_dao.safe_get_or_none(
       user_id=user_id, group_id=group_id
   )
   # ✅ 自动缓存，基于 user_id 和 group_id
   ```

### 优化建议

#### 1. 并行获取数据，提高缓存命中率

**当前实现**：
```python
# 串行获取，可能错过缓存优势
plugin = await plugin_dao.safe_get_or_none(module=module)
user = await user_dao.get_by_func_or_none(...)
group = await group_dao.safe_get_or_none(...)
```

**优化后**：
```python
# 并行获取，DataAccess 会自动处理缓存
plugin, user, group = await asyncio.gather(
    plugin_dao.safe_get_or_none(module=module),
    user_dao.get_by_func_or_none(UserConsole.get_user, False, user_id=user_id),
    group_dao.safe_get_or_none(group_id=group_id, channel_id__isnull=True)
    if group_id else asyncio.sleep(0)
)
```

#### 2. 提前获取所有需要的数据

**当前实现**：
```python
# group 数据在 hooks 执行时才获取
# 在 auth 函数中
group = None
if entity.group_id:
    group = await group_dao.safe_get_or_none(...)
```

**优化后**：
```python
# 在获取 plugin 和 user 时，同时获取 group
# 统一数据获取，充分利用缓存
async def get_all_auth_data(module, user_id, group_id):
    plugin_dao = DataAccess(PluginInfo)
    user_dao = DataAccess(UserConsole)
    group_dao = DataAccess(GroupConsole) if group_id else None
    
    tasks = [
        plugin_dao.safe_get_or_none(module=module),
        user_dao.get_by_func_or_none(UserConsole.get_user, False, user_id=user_id),
    ]
    if group_id and group_dao:
        tasks.append(
            group_dao.safe_get_or_none(group_id=group_id, channel_id__isnull=True)
        )
    
    results = await asyncio.gather(*tasks)
    return results
```

#### 3. 监控缓存效果

**实现方案**：
```python
from zhenxun.services.data_access import DataAccess

async def auth(...):
    # 记录开始时的缓存统计
    cache_stats_before = DataAccess.get_cache_stats()
    
    try:
        # 执行权限检查
        ...
    finally:
        # 记录结束时的缓存统计
        cache_stats_after = DataAccess.get_cache_stats()
        
        # 分析缓存效果
        for before, after in zip(cache_stats_before, cache_stats_after):
            cache_type = after["cache_type"]
            hits_diff = after["hits"] - before["hits"]
            misses_diff = after["misses"] - before["misses"]
            total = hits_diff + misses_diff
            
            if total > 0:
                hit_rate = (hits_diff / total) * 100
                logger.debug(
                    f"缓存统计 - {cache_type}: "
                    f"命中率={hit_rate:.1f}%, "
                    f"命中={hits_diff}, 未命中={misses_diff}",
                    LOGGER_COMMAND
                )
                
                # 如果缓存命中率过低，记录警告
                if hit_rate < 30:
                    logger.warning(
                        f"缓存命中率过低: {cache_type} = {hit_rate:.1f}%",
                        LOGGER_COMMAND
                    )
```

#### 4. 调整空结果缓存时间

**根据实际需求调整**：
```python
from zhenxun.services.data_access import DataAccess

# 对于频繁查询但可能不存在的记录，可以增加空结果缓存时间
# 例如：ban记录查询，如果用户未被ban，可以缓存更长时间
DataAccess.set_null_result_ttl(600)  # 10分钟

# 对于需要实时性的数据，可以减少缓存时间
# 例如：用户权限查询
DataAccess.set_null_result_ttl(60)  # 1分钟
```

## 缓存性能优化检查清单

- [ ] 所有数据访问都通过 `DataAccess`，而不是直接使用模型类
- [ ] 并行获取多个数据，而不是串行获取
- [ ] 提前获取所有需要的数据，避免在检查过程中重复获取
- [ ] 监控缓存命中率，确保缓存效果良好
- [ ] 根据实际需求调整空结果缓存时间
- [ ] 定期查看缓存统计，优化缓存策略

## 预期缓存效果

### 高并发场景下的缓存命中率预期

- **PluginInfo（插件信息）**：90-95%（插件信息变化不频繁）
- **UserConsole（用户信息）**：80-90%（用户信息相对稳定）
- **GroupConsole（群组信息）**：85-95%（群组信息变化不频繁）
- **BanConsole（ban记录）**：70-85%（ban状态可能变化）
- **BotConsole（Bot信息）**：95-99%（Bot信息很少变化）
- **LevelUser（用户权限）**：75-85%（权限可能变化）

### 性能提升预期

- **缓存命中时**：查询时间从 10-50ms 降低到 1-5ms（减少 80-90%）
- **总体性能**：在高并发场景下，总体执行时间减少 30-50%
- **数据库压力**：减少数据库查询 40-60%

## 注意事项

1. **缓存一致性**：DataAccess 在数据更新时会自动清除缓存，确保一致性
2. **缓存失效**：如果需要强制刷新缓存，可以使用 `await dao.clear_cache(**kwargs)`
3. **监控缓存**：定期查看缓存统计，确保缓存效果良好
4. **空结果缓存**：空结果缓存有助于减少对不存在记录的查询，但需要根据实际需求调整TTL

