# 权限检查系统优化方案

## 项目概述

优化 `zhenxun_bot` 的权限检查系统，将每条消息的数据库/缓存查询次数从 **6-10 次** 降低到 **1-2 次**。

---

## 当前问题分析

### 现有查询流程

每条消息进入时，权限检查系统执行以下查询：

| 阶段            | 查询内容                              | 次数   |
| --------------- | ------------------------------------- | ------ |
| `_load_context` | PluginInfo, UserConsole, GroupConsole | 3 次   |
| `auth_ban`      | BanConsole                            | 1-2 次 |
| `auth_bot`      | BotConsole                            | 1 次   |
| `auth_admin`    | LevelUser (全局+群组)                 | 1-2 次 |
| `auth_limit`    | PluginLimit (如果不在内存)            | 0-1 次 |

**总计：6-10 次查询**

### 问题根源

1. 数据分散在多个表：`user_console`, `group_console`, `ban_console`, `bot_console`, `level_user`, `plugin_info`
2. 每个检查模块独立查询，缺乏数据共享
3. 即使有 Redis 缓存，也需要多次网络往返

---

## 优化方案：预聚合权限快照 (Permission Snapshot)

### 核心思想

**用一个 Hash 结构存储权限检查所需的所有数据**，消息到达时只需 1-2 次查询。

### 数据结构设计

#### 1. 权限快照 (AuthSnapshot)

```
缓存键格式: AUTH_SNAPSHOT:{user_id}:{group_id}:{bot_id}

Hash 结构:
{
    # === 用户信息 ===
    "user_gold": 100,                  # 用户金币
    "user_banned": 0,                  # 0=未ban, -1=永久ban, >0=ban结束时间戳
    "user_ban_duration": 0,            # ban时长（用于计算剩余时间）

    # === 用户权限等级 ===
    "user_level_global": 0,            # 全局权限等级
    "user_level_group": 0,             # 群组权限等级

    # === 群组信息 ===
    "group_status": 1,                 # 群组状态 (1=开启, 0=休眠)
    "group_level": 5,                  # 群组等级
    "group_is_super": 0,               # 是否超级群组
    "group_block_plugins": "",         # 禁用插件列表 "<plugin1,<plugin2,"
    "group_superuser_block_plugins": "", # 超级用户禁用插件列表
    "group_banned": 0,                 # 群组是否被ban

    # === Bot信息 ===
    "bot_status": 1,                   # Bot状态
    "bot_block_plugins": "",           # Bot禁用插件列表

    # === 元数据 ===
    "version": 1,                      # 快照版本（用于失效判断）
    "created_at": 1703859600           # 创建时间戳
}
```

#### 2. 插件信息缓存 (PluginSnapshot)

插件是全局的，变化较少，可以使用本地内存缓存 + Redis 双层缓存：

```
缓存键格式: PLUGIN_SNAPSHOT:{module}

结构:
{
    "status": true,                    # 全局开关状态
    "block_type": null,                # 禁用类型 (PRIVATE/GROUP/ALL/null)
    "admin_level": 0,                  # 调用所需权限等级
    "cost_gold": 0,                    # 调用所需金币
    "level": 5,                        # 所需群权限等级
    "limit_superuser": false,          # 是否限制超级用户
    "plugin_type": "NORMAL",           # 插件类型
    "ignore_prompt": false             # 是否忽略阻断提示
}
```

### 工作流程

```
消息到达
    │
    ▼
┌──────────────────────────────────────────────────────┐
│  1. 第一次查询：获取权限快照                           │
│     AUTH_SNAPSHOT:{user_id}:{group_id}:{bot_id}      │
│                                                      │
│     - 如果存在且未过期 → 直接使用                      │
│     - 如果不存在 → 触发快照构建（异步）                 │
└──────────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────────┐
│  2. 第二次查询：获取插件信息                           │
│     PLUGIN_SNAPSHOT:{module}                          │
│                                                      │
│     - 优先从本地内存缓存获取                           │
│     - 未命中时从 Redis 获取                            │
│     - 仍未命中时从 DB 加载并缓存                        │
└──────────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────────┐
│  3. 执行权限检查（纯内存计算，无 I/O）                  │
│                                                      │
│     - ban 检查                                        │
│     - bot 状态检查                                    │
│     - 插件状态检查                                    │
│     - 群组状态检查                                    │
│     - 权限等级检查                                    │
│     - 金币检查                                        │
└──────────────────────────────────────────────────────┘
    │
    ▼
 权限检查完成
```

### 缓存失效策略

#### 主动失效（事件驱动）

| 事件             | 失效范围                                  |
| ---------------- | ----------------------------------------- |
| 用户金币变化     | `AUTH_SNAPSHOT:{user_id}:*:*`             |
| 用户被 ban/unban | `AUTH_SNAPSHOT:{user_id}:*:*`             |
| 群组设置变更     | `AUTH_SNAPSHOT:*:{group_id}:*`            |
| Bot 配置变更     | `AUTH_SNAPSHOT:*:*:{bot_id}`              |
| 插件配置变更     | `PLUGIN_SNAPSHOT:{module}` + 本地内存缓存 |
| 用户权限变更     | `AUTH_SNAPSHOT:{user_id}:{group_id}:*`    |

#### 被动失效（TTL）

- 权限快照 TTL：**60 秒**（权衡实时性和性能）
- 插件快照 TTL：**300 秒**（插件配置变化较少）
- 本地内存缓存 TTL：**30 秒**

---

## 实现计划

### Phase 1: 基础设施 ✅ [已完成]

- [x] 新增 `CacheType.AUTH_SNAPSHOT` 和 `CacheType.PLUGIN_SNAPSHOT`
- [x] 创建 `AuthSnapshot` Pydantic 模型
- [x] 创建 `PluginSnapshot` Pydantic 模型
- [x] 实现快照构建器 `SnapshotBuilder`

### Phase 2: 快照服务 ✅ [已完成]

- [x] 创建 `AuthSnapshotService` 类

  - [x] `get_snapshot(user_id, group_id, bot_id)` - 获取权限快照
  - [x] `build_snapshot(user_id, group_id, bot_id)` - 构建权限快照
  - [x] `invalidate_user(user_id)` - 失效用户相关快照
  - [x] `invalidate_group(group_id)` - 失效群组相关快照
  - [x] `invalidate_bot(bot_id)` - 失效 Bot 相关快照

- [x] 创建 `PluginSnapshotService` 类
  - [x] `get_plugin(module)` - 获取插件信息（本地缓存优先）
  - [x] `invalidate_plugin(module)` - 失效插件缓存
  - [x] `warmup()` - 预热所有插件缓存

### Phase 3: 权限检查器重构 ✅ [已完成]

- [x] 创建新的 `OptimizedAuthChecker` 类
- [x] 基于快照数据的权限检查逻辑
- [x] 无 I/O 的纯内存计算
- [x] 保持与现有系统的兼容性

### Phase 4: 缓存失效集成 ⏳ [可选优化]

> 注：当前实现使用 TTL 自动过期机制，以下为可选的主动失效优化

- [ ] 在 `UserConsole` 的写操作中添加失效逻辑
- [ ] 在 `GroupConsole` 的写操作中添加失效逻辑
- [ ] 在 `BanConsole` 的写操作中添加失效逻辑
- [ ] 在 `BotConsole` 的写操作中添加失效逻辑
- [ ] 在 `LevelUser` 的写操作中添加失效逻辑
- [ ] 在 `PluginInfo` 的写操作中添加失效逻辑

### Phase 5: 测试与验证 ⏳ [待测试]

- [ ] 单元测试
- [ ] 性能对比测试
- [ ] 边界情况测试

---

## 文件结构

```
zhenxun/
├── services/
│   └── auth_snapshot/
│       ├── __init__.py
│       ├── models.py          # AuthSnapshot, PluginSnapshot 模型
│       ├── builder.py         # 快照构建器
│       ├── service.py         # 快照服务
│       └── checker.py         # 优化后的权限检查器
└── builtin_plugins/
    └── hooks/
        └── auth_checker_v2.py # 新版权限检查入口
```

---

## 性能预期

| 指标           | 优化前  | 优化后   | 提升     |
| -------------- | ------- | -------- | -------- |
| DB 查询次数    | 6-10 次 | **1-3 次** | 70-85%↓  |
| 平均延迟       | ~50ms   | ~10ms    | 80%↓     |
| Redis 连接压力 | 高      | 低       | 显著降低 |

### 查询优化详情

**优化前（5-7 次 DB 查询）：**
1. UserConsole - 用户金币
2. LevelUser (全局) - 全局权限等级
3. LevelUser (群组) - 群组权限等级
4. BanConsole (用户全局)
5. BanConsole (用户群组)
6. BanConsole (群组)
7. GroupConsole - 群组信息
8. BotConsole - Bot 信息

**优化后（1-3 次 DB 查询）：**
1. **单条复合 SQL** - 使用 UNION ALL 合并 UserConsole + LevelUser + BanConsole（1次）
2. GroupConsole - **内存缓存 60s**，变化时失效（0-1次）
3. BotConsole - **内存缓存 300s**，变化时失效（0-1次）

**最优情况**：缓存命中时只需 1 次 DB 查询
**最差情况**：3 次 DB 查询（全部未命中缓存）

---

## 风险与缓解

| 风险             | 缓解措施                                     |
| ---------------- | -------------------------------------------- |
| 快照数据过期     | 合理的 TTL + 主动失效机制                    |
| 快照构建延迟     | 异步构建 + 首次访问降级到旧流程              |
| 内存占用增加     | 监控内存使用 + 合理的缓存清理                |
| 数据一致性       | 写操作后立即失效缓存                         |
| **DB 过载风险**  | **全局 Semaphore 限制并发构建数量 (50)**     |
| **并发构建重复** | **按 cache_key 的 asyncio.Lock**             |
| **构建等待超时** | **3 秒超时后返回默认快照，允许请求继续处理** |

### 并发控制机制

```
大量消息同时进入时:

1. 同一 user:group:bot 组合
   - 使用 asyncio.Lock 保证只构建一次
   - 其他等待的协程复用同一个 Future 结果

2. 不同 user:group:bot 组合
   - 使用全局 Semaphore 限制最多 50 个并发构建
   - 超过限制的请求排队等待（最多 3 秒）
   - 等待超时则返回默认快照，避免请求阻塞

这样即使 1000 个不同用户同时发消息:
- 最多只有 50 个并发 DB 查询
- 每个构建 5-7 次查询 = 最多 350 次并发 DB 查询
- 远低于直接查询的 6000 次
```

---

---

## 使用方式

### 方式一：替换原有权限检查器（推荐）

修改 `zhenxun/builtin_plugins/hooks/__init__.py`，将 `auth_checker` 替换为 `auth_checker_v2`：

```python
# 原来的导入
# from . import auth_checker

# 替换为
from . import auth_checker_v2
```

### 方式二：并行测试

同时加载两个版本，通过日志对比性能：

```python
from . import auth_checker      # 原版本
from . import auth_checker_v2   # 优化版本（会覆盖原版本的 run_preprocessor）
```

### API 使用示例

```python
from zhenxun.services.auth_snapshot import (
    AuthSnapshotService,
    PluginSnapshotService,
    AuthSnapshot,
    PluginSnapshot,
)

# 获取权限快照
snapshot = await AuthSnapshotService.get_snapshot(
    user_id="123456",
    group_id="789012",
    bot_id="bot_001"
)

# 检查用户是否被ban
if snapshot.is_user_banned():
    print(f"用户被ban，剩余时间: {snapshot.get_user_ban_remaining()}秒")

# 获取插件快照
plugin = await PluginSnapshotService.get_plugin("example_plugin")
if plugin and plugin.cost_gold > 0:
    print(f"此插件需要 {plugin.cost_gold} 金币")

# 手动失效缓存（数据更新时调用）
await AuthSnapshotService.invalidate_user("123456")
await PluginSnapshotService.invalidate_plugin("example_plugin")
```

---

## 进度追踪

- 开始日期：2025-12-29
- 当前阶段：核心功能已完成
- 状态：✅ 基础功能完成，待测试验证
