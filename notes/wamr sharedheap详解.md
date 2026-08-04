好的，以下是整理好的总结：

```markdown
# WAMR 共享堆 (Shared Heap) 实现分析

## 一、概述

共享堆是 WAMR 的**私有扩展**（非 wasm 标准规范），目的是让**多个 Wasm 模块实例共享同一块物理内存**，避免模块间数据传递需要拷贝的开销。

核心做法：预先创建一块共享内存，把它映射到 wasm 地址空间的**顶端**。任何 attach 了这个堆的模块实例，通过正常的 wasm 内存访问指令（i32.load / i32.store 等）就能直接读写共享堆，完全透明。

## 二、与 wasm 线性内存的关系

共享堆和 wasm 线性内存是**两个独立的东西**，在 wasm 地址空间中的布局如下：

```
            wasm 32-bit 地址空间
   0x00000000 ┌──────────────────────┐
              │                      │
              │   模块实例的线性内存    │  ← memory_data / memory_data_size
              │   (每个实例自己的)     │     从 0 开始，可动态增长
              │                      │
              │   ← 这里必须留空 →     │  ← attach 时会检测线性内存
              │     （两者不能重叠）    │     是否越过了 start_off
              │                      │
  接近 4GB ─→ ├──────────────────────┤
              │    共享堆             │  ← base_addr 指向外部物理内存
              │    (多模块共用)        │     start_off = UINT32_MAX - size + 1
  0xFFFFFFFF └──────────────────────┘
```

### 关键区别

| 特性 | wasm 线性内存 | 共享堆 |
|------|-------------|--------|
| 归属 | 每个模块实例自己创建、自己拥有 | 外部创建，多实例共享 |
| 生命周期 | 跟着模块实例走 | 独立于任何模块实例 |
| 物理来源 | 运行时内部分配（pool/allocator/system） | 外部注入（用户预分配或运行时 mmap） |
| wasm 地址 | 从 0 开始 | 挂在地址空间最顶端 |
| 可见性 | 仅本实例 | 所有 attach 的实例都能访问 |
| 增长方向 | 从低地址向高地址增长 | 固定在顶部不动 |
| wasm 规范 | 标准 MVP 的一部分 | **WAMR 的私有扩展** |
| 与 WASM_ENABLE_SHARED_MEMORY | — | **完全独立**，无依赖关系 |

共享堆的本质是**给 wasm 模块开一扇窗**：窗口外面是宿主环境的一块共享物理内存，窗内是 wasm 地址空间的顶部。多个模块把这扇窗开在同一个位置，就能透过各自的"窗"看到同一块外部内存，各自的线性内存（"家"）互不干扰。

## 三、数据结构

### WASMSharedHeap（`core/iwasm/interpreter/wasm_runtime.h:95-111`）

```c
typedef struct WASMSharedHeap {
    WASMSharedHeap *next;        // 全局链表，用于销毁时遍历
    WASMSharedHeap *chain_next;  // 逻辑链，多堆串联成连续地址空间
    void *heap_handle;           // mem_allocator 句柄（预分配堆为 NULL）
    uint8 *base_addr;            // 物理内存基地址
    uint64 size;
    uint64 start_off_mem64;      // wasm 64 位地址空间中的起始偏移
    uint64 start_off_mem32;      // wasm 32 位地址空间中的起始偏移
    uint8  attached_count;       // 被多少个 module instance 附加
} WASMSharedHeap;
```

### 全局状态（`core/iwasm/common/wasm_memory.c:31-33`）

```c
static WASMSharedHeap *shared_heap_list = NULL;   // 全局共享堆链表头
static korp_mutex shared_heap_list_lock;           // 保护该链表的全局互斥锁
```

### 模块实例附加字段

每个 module instance 附加共享堆后，在自身结构中存储三个字段：

```c
uint8 *shared_heap_base_addr_adj;  // 地址转换基准：base_addr - start_off
MemBound shared_heap_start_off;    // 当前使用的共享堆起始偏移
MemBound shared_heap_end_off;      // 当前使用的共享堆结束偏移
WASMSharedHeap *shared_heap;       // 附加的共享堆指针
```

## 四、创建流程

`wasm_runtime_create_shared_heap()`（`core/iwasm/common/wasm_memory.c:229-294`）：

1. `size` 对齐到页大小
2. 计算虚拟起始偏移：
   - `start_off_mem64 = UINT64_MAX - size + 1` → 映射到 64 位地址空间顶部
   - `start_off_mem32 = UINT32_MAX - size + 1` → 映射到 32 位地址空间顶部
3. 两种物理内存来源：
   - **预分配**（`pre_allocated_addr != NULL`）：直接使用用户提供的地址，`heap_handle = NULL`
   - **运行时管理**：mmap 映射 + 创建 mem_allocator
4. 头插法插入全局链表 `shared_heap_list`（加锁）

## 五、多堆串联（Chaining）

`wasm_runtime_chain_shared_heaps()`（`core/iwasm/common/wasm_memory.c:297-349`）：

多个共享堆可以通过 `chain_next` 串成一条链，在 wasm 视角呈现为**一块连续的地址空间**。例如堆 A（1MB）链上堆 B（2MB），wasm 应用看到的是 3MB 的连续区域。

**约束**：链中**最多只能有一个堆**拥有动态分配能力（`heap_handle != NULL`），其余必须是预分配的静态内存。

## 六、地址转换

这是最关键的部分（`core/iwasm/common/wasm_memory.h:72-73`）：

```c
native_addr = shared_heap_base_addr_adj + app_offset
            = (base_addr - start_off) + app_offset
```

**实例**（32 位空间，堆大小 1MB，`start_off = 0xFFFFFFFFFFF00000`）：

wasm 应用读写地址 `0xFFFFFFFFFFF00100`：

```
native_addr = (base_addr - 0xFFFFFFFFFFF00000) + 0xFFFFFFFFFFF00100
            = base_addr + 0x100
```

映射到物理内存偏移 0x100 处，对 wasm 应用完全透明。

## 七、附加与分离（attach / detach）

- **附加**：`wasm_runtime_attach_shared_heap()` — 填充 module instance 的 `shared_heap_*` 字段，`attached_count++`
- **分离**：`wasm_runtime_detach_shared_heap()` — 清空字段，`attached_count--`

**冲突检测**：附加时检查模块线性内存是否与共享堆地址空间重叠（`memory_data_size > start_off` 则冲突）。之后每次 `memory.grow` 也会再次检查（`wasm_memory.c:1710-1726`）。

## 八、核心 API 概览

| API | 文件:行号 | 功能 |
|-----|-----------|------|
| `wasm_runtime_create_shared_heap` | `wasm_memory.c:229-294` | 创建共享堆，加入全局链表 |
| `wasm_runtime_chain_shared_heaps` | `wasm_memory.c:297-349` | 串联两个共享堆链 |
| `wasm_runtime_unchain_shared_heaps` | `wasm_memory.c:352-381` | 断开链 |
| `wasm_runtime_reset_shared_heap_chain` | `wasm_memory.c:384-415` | 重置链（清零预分配堆 / 重建 WAMR 管理堆） |
| `wasm_runtime_attach_shared_heap` | `wasm_memory.c:597-605` | 附加到 module instance |
| `wasm_runtime_detach_shared_heap` | `wasm_memory.c:658-665` | 从 module instance 分离 |
| `wasm_runtime_get_shared_heap` | `wasm_memory.c:687-690` | 获取当前附加的共享堆 |
| `wasm_runtime_shared_heap_malloc` | `wasm_memory.c:777-807` | 从动态共享堆分配内存 |
| `wasm_runtime_shared_heap_free` | `wasm_memory.c:810-845` | 释放动态分配的内存 |

## 九、锁的粒度

使用**单一全局互斥锁** `shared_heap_list_lock`，加锁范围很窄（读多写少的设计）：

| 操作 | 加锁 |
|------|------|
| `create_shared_heap`（插入全局链表）| ✅ |
| `chain_shared_heaps`（验证 + 修改链）| ✅ |
| `unchain_shared_heaps` | ✅ |
| `reset_shared_heap_chain` | ✅ |
| `attach_internal` / `detach_internal`（`attached_count` 修改）| ✅ |
| `destroy_shared_heaps`（遍历 + 清空全局链表）| ✅ |
| **`shared_heap_malloc` / `shared_heap_free`** | ❌ 无锁（由 mem_allocator 内部管理） |
| **`is_app_addr_in_shared_heap`**（地址边界检查）| ❌ 无锁（只读 module instance 私有缓存） |
| **`is_native_addr_in_shared_heap`** | ❌ 无锁（只读遍历 `chain_next`） |

## 十、销毁

`destroy_shared_heaps()`（`core/iwasm/common/wasm_memory.c:892-912`）：

遍历全局 `shared_heap_list`，销毁运行时管理的堆（mmap unmap + mem_allocator_destroy），释放结构体内存。在 `wasm_runtime_memory_destroy()` 中调用。

## 十一、wasm 应用侧接口

`core/iwasm/libraries/shared-heap/shared_heap_wrapper.c` 导出两个原生函数供 wasm 应用使用：

- `shared_heap_malloc(uint32 size) → uint32` — 注册为 `shared_heap_malloc_wrapper`
- `shared_heap_free(void *ptr)` — 注册为 `shared_heap_free_wrapper`

底层通过宏展开调用 `wasm_runtime_shared_heap_malloc/free`。
```

以上内容覆盖了共享堆的数据结构、创建/串联/附加/地址转换/锁粒度/销毁的完整链路，可以直接保存。