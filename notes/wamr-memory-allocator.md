# WAMR 内存分配器深度解析

## 目录

1. [架构总览](#1-架构总览)
2. [三种内存分配模式](#2-三种内存分配模式)
3. [后端分配器：EMS 与 TLSF](#3-后端分配器ems-与-tlsf)
4. [EMS 内部结构](#4-ems-内部结构)
5. [完整调用链](#5-完整调用链)
6. [对齐分配](#6-对齐分配)
7. [GC 集成](#7-gc-集成)
8. [堆迁移](#8-堆迁移)
9. [关键文件索引](#9-关键文件索引)

---

## 1. 架构总览

WAMR 的内存管理采用**三层结构**：

```
┌─────────────────────────────────────────────────────────┐
│                    用户 API                              │
│  wasm_runtime_malloc / wasm_runtime_free / realloc       │
├─────────────────────────────────────────────────────────┤
│                    分发层 (wasm_memory.c)                │
│  按 memory_mode 选择：Pool / Allocator / System          │
├─────────────────────────────────────────────────────────┤
│                    后端分配器 (mem_alloc.c)               │
│  EMS（默认） 或 TLSF                                      │
└─────────────────────────────────────────────────────────┘
```

**核心文件：**

| 文件 | 角色 |
|------|------|
| `core/iwasm/common/wasm_memory.c` | 分发层：三种模式的 Init / malloc / free / realloc |
| `core/shared/mem-alloc/mem_alloc.c` | 抽象层：统一接口，对接 EMS 或 TLSF |
| `core/shared/mem-alloc/ems/ems_alloc.c` | EMS 分配/释放核心逻辑 |
| `core/shared/mem-alloc/ems/ems_kfc.c` | EMS 堆初始化 + 释放块合并 |
| `core/shared/mem-alloc/ems/ems_hmu.c` | HMU 头部前后守卫验证 |
| `core/shared/mem-alloc/ems/ems_gc.c` | GC 回收（仅 WO 类型对象） |
| `core/shared/mem-alloc/ems/ems_gc_internal.h` | HMU、自由链表、二叉树、gc_heap_t 定义 |

---

## 2. 三种内存分配模式

定义在 `core/iwasm/common/wasm_memory.c:21-24`：

```c
typedef enum {
    MEMORY_MODE_UNKNOWN = 0,
    MEMORY_MODE_POOL,              // 预分配内存池
    MEMORY_MODE_ALLOCATOR,         // 用户自定义分配器
    MEMORY_MODE_SYSTEM_ALLOCATOR   // 系统 malloc/free
} Memory_Mode;
```

全局变量 `memory_mode` 初始为 `MEMORY_MODE_UNKNOWN`，在 `wasm_runtime_memory_init()` 中设置。

### 2.1 初始化入口

```c
// core/iwasm/common/wasm_memory.c
bool wasm_runtime_memory_init(mem_alloc_type_t mem_alloc_type,
                              const MemAllocOption *alloc_option);
```

**三种初始化方式：**

#### Mode 1：Pool（预分配内存池）

```c
// wasm_memory.c:80-93
static bool wasm_memory_init_with_pool(void *mem, unsigned int bytes)
{
    // mem_allocator_create 在用户提供的内存块上构建堆
    mem_allocator_t allocator = mem_allocator_create(mem, bytes);
    if (allocator) {
        memory_mode = MEMORY_MODE_POOL;
        pool_allocator = allocator;  // 保存到全局变量
        global_pool_size = bytes;
        return true;
    }
    return false;
}
```

**使用场景：** 裸机 / RTOS 环境。用户提供 `(heap_buf, heap_size)`，所有 WAMR 内部分配都从这块内存池走。

#### Mode 2：Allocator（自定义分配器）

```c
// wasm_memory.c:96-105
static bool wasm_memory_init_with_allocator(
    void *user_data, void *malloc_func,
    void *realloc_func, void *free_func)
{
    // 保存三个函数指针 + user_data
    memory_mode = MEMORY_MODE_ALLOCATOR;
    allocator_user_data = user_data;
    malloc_func = _malloc_func;
    realloc_func = _realloc_func;
    free_func = _free_func;
    return true;
}
```

**使用场景：** 需要在 WAMR 和使用方之间统一管理内存、统计分配量、或与特定 RTOS 的分配器对接。

#### Mode 3：System Allocator

```c
// wasm_memory.c:874
memory_mode = MEMORY_MODE_SYSTEM_ALLOCATOR;
```

**使用场景：** Linux / macOS / Windows 等有完整 libc 的环境。直接走 `malloc/free`。

### 2.2 运行时 dispatch

```c
// wasm_memory.c:945-968
static inline void *wasm_runtime_malloc_internal(unsigned int size)
{
    if (memory_mode == MEMORY_MODE_UNKNOWN)   return NULL;  // 未初始化
    else if (memory_mode == MEMORY_MODE_POOL) return mem_allocator_malloc(pool_allocator, size);
    else if (memory_mode == MEMORY_MODE_ALLOCATOR) return malloc_func(user_data, size);
    else return os_malloc(size);  // SYSTEM
}
```

`realloc` 和 `free` 也遵循同样的 dispatch 模式。

---

## 3. 后端分配器：EMS 与 TLSF

定义在 `core/config.h:64-68`：

```c
#define MEM_ALLOCATOR_EMS  0
#define MEM_ALLOCATOR_TLSF 1
#define DEFAULT_MEM_ALLOCATOR MEM_ALLOCATOR_EMS
```

`mem_alloc.c` 通过 `#if DEFAULT_MEM_ALLOCATOR == MEM_ALLOCATOR_EMS` 条件编译，**只编译其中的一个**。

### 3.1 EMS（Embedded Memory System）

**WAMR 自研**的嵌入式分配器，核心特征：

- 每个内存块由一个 **HMU**（Heap Management Unit）4 字节头部管理
- 空闲块组织为**双层结构**：32 槽位普通链表 + 红黑树
- 支持两种对象类型：**VO**（手动释放）和 **WO**（GC 自动回收）
- 可选的前后守卫（padding guard）用于检测越界写
- 最大堆大小：256KB（`GC_MAX_HEAP_SIZE`），可通过 `HMU_SIZE_SIZE` 位域扩展到 1GB

### 3.2 TLSF（Two-Level Segregated Fit）

经典的**实时分配器**，具有 O(1) 分配/释放时间复杂度。

- 实现在 `mem_alloc.c:159-270`，使用 `tlsf_create_with_pool` 等函数
- 自带互斥锁（`korp_mutex lock`），每个操作加锁
- 行为与标准 `malloc` 一致，无 GC 支持

### 3.3 选择建议

| 场景 | 推荐 |
|------|------|
| 启用 GC（WASM_ENABLE_GC） | **必须 EMS**（TLSF 无 GC 支持） |
| 纯解释器 / AOT，无 GC | 两者皆可 |
| 对碎片敏感 | EMS（红黑树精确匹配） |
| 需要严格实时性 | TLSF（O(1) 分配） |

---

## 4. EMS 内部结构

### 4.1 HMU 头部（4 bytes）

`core/shared/mem-alloc/ems/ems_gc_internal.h:299-353`

```
Bit Layout (32 bits):
┌──────┬──────┬──────┬──────┬───────────────────────────────────────┐
│ 31-30│  29  │  28  │  27  │               27-0                     │
│  UT  │  P   │  FB  │      │           SIZE (27 bits)               │
└──────┴──────┴──────┴──────┴───────────────────────────────────────┘
```

| 位域 | 名称 | 含义 |
|------|------|------|
| **31-30** | **UT** (Usage Type) | 固定长度 2 位 |
| **29** | **P** (Previous In Use) | 前一个块是否在用 |
| **28** | **FB** (Free Bit) | VO 对象：是否已被释放；WO 对象：**MB** (Mark Bit) |
| **27-0** | **SIZE** → 实际乘 8 | 块的总大小，8-byte 对齐 |

### 4.2 四种 HMU 类型

```c
// ems_gc_internal.h:17-24
typedef enum hmu_type_enum {
    HMU_FM = 0,   // Free Merged — 标准空闲块
    HMU_FC = 1,   // Free Coalesced — 合并后的空闲块
    HMU_VO = 2,   // VM Object — 手动分配/释放
    HMU_WO = 3,   // WASM Object — GC 管理（有 finalizer、mark bit）
} hmu_type_t;
```

**类型图示：**

```
HMU_FM (0) — 普通空闲块，挂在 free list 或 tree 上
HMU_FC (1) — 被 gci_add_fc 合并后的空闲块，可被重新切分
HMU_VO (2) — 用户调用 wasm_runtime_malloc 分配 → gc_alloc_vo() → 调用方手动 free
HMU_WO (3) — 用户调用 mem_allocator_malloc_with_gc → gc_alloc_wo() → GC 自动回收
```

**VO vs WO 的区别：**

| | VO（VM Object） | WO（WASM Object） |
|---|---|---|
| 分配 API | `gc_alloc_vo()` | `gc_alloc_wo()` |
| 释放 API | `gc_free_vo()` 手动调用 | GC mark-sweep 自动回收 |
| State bit | bit28 = FB（free bit） | bit28 = MB（mark bit） |
| 用途 | 运行时内部结构、模块、实例 | wasm GC 规范的 struct/array |

### 4.3 空闲块管理：双层结构

```
                      EMS 自由块组织
┌─────────────────────────────────────────────────────┐
│  普通链表（kfc_normal_list）                          │
│  ┌───┬───┬───┬───┬───┬───┬───┬───┬─────┬─────┐    │
│  │ 0 │ 1 │ 2 │ 3 │ 4 │...│   │   │ ... │ 31  │    │
│  └───┴───┴───┴───┴───┴───┴───┴───┴─────┴─────┘    │
│  每个槽存 2^(idx+3) 字节的空闲块链表                    │
│  例如 idx=4 → 32 byte 块链表                           │
│                                                       │
│  红黑树（kfc_tree_root）                              │
│  ┌─────────────────────────────────────┐            │
│  │  大块（≥32号槽容量），按 size 排序    │            │
│  │  size[left] ≤ size[cur] < size[right]│            │
│  └─────────────────────────────────────┘            │
└─────────────────────────────────────────────────────┘
```

**分配策略（`alloc_hmu`，`ems_alloc.c:343`）：**

1. 如果请求大小 < `HMU_FC_NORMAL_MAX_SIZE`（= 31×8 = 248 bytes），先扫描普通链表
2. 找到合适槽位后，如果块远大于请求，**切分**为两块：一块分配，一块重新加入空闲链表（`gci_add_fc`）
3. 如果普通链表没有合适块，在红黑树中按 size 查找 best-fit

### 4.4 gc_heap_t 结构体

```c
// ems_gc_internal.h:442-522
typedef struct gc_heap_struct {
    gc_handle_t heap_id;                           // 自身标识（用于 double check）
    gc_uint8 *base_addr;                           // 数据区基地址
    gc_size_t current_size;                        // 数据区当前大小
    korp_mutex lock;                               // 线程锁

    hmu_normal_list_t kfc_normal_list[32];         // 普通空闲链表（≤248 bytes）
    hmu_tree_node_t kfc_tree_root_buf[...];        // 红黑树根节点空间
    hmu_tree_node_t *kfc_tree_root;                // 红黑树根指针

    gc_size_t init_size;                           // 初始池大小
    gc_size_t highmark_size;                       // 历史最高使用量
    gc_size_t total_free_size;                     // 当前空闲总量

    // ... GC 相关字段（仅 WASM_ENABLE_GC 启用）
    gc_size_t gc_threshold;                        // GC 触发阈值
    bool is_heap_corrupted;                        // 堆损坏标记
} gc_heap_t;
```

### 4.5 内存布局

**用户传入的内存块布局（gc_init_with_pool）：**

```
┌──────────────────────────────────────────────────────────────┐
│ buf (用户提供)                                                │
│ ┌──────────────┬───────────────────────────────────────────┐ │
│ │ gc_heap_t    │              数据区 (空闲区)               │ │
│ │ (256+ bytes) │   ■ 每个 HMU = 4B header + data           │ │
│ │              │   ■ 分配从低地址向高地址推进               │ │
│ └──────────────┴───────────────────────────────────────────┘ │
│ ▲ base_addr                                                   │
└──────────────────────────────────────────────────────────────┘
```

**单个 HMU 的内存布局（BH_ENABLE_GC_VERIFY 模式下）：**

```
┌─────────────┬──────────────────┬────────────────────┬──────────────────┐
│ HMU Header  │  OBJ_PREFIX      │  User Data         │  OBJ_SUFFIX      │
│ (4 bytes)   │  file, line,     │  (aligned to 8)    │  padding guard   │
│             │  size, paddings  │                    │                  │
└─────────────┴──────────────────┴────────────────────┴──────────────────┘
▲ hmu          ▲ prefix                                  ▲ suffix
               └── hmu_to_obj(hmu) 返回这里 ─────────────┘
                                  (用户指针)
```

`OBJ_PREFIX` 包含 `__FILE__`、`__LINE__` 和魔数，用于检测 buffer overflow / underflow。

---

## 5. 完整调用链

### 5.1 从 malloc 到 HMU

```
用户调用
  │
  ▼
wasm_runtime_malloc(size)                     // wasm_memory.c:1052
  │  size==0 特殊处理
  │  WASM_ENABLE_FUZZ_TEST 上限检查
  ▼
wasm_runtime_malloc_internal(size)            // wasm_memory.c:946
  │  按 memory_mode 分发：
  │  ├── MEMORY_MODE_POOL        → mem_allocator_malloc(pool_allocator, size)
  │  ├── MEMORY_MODE_ALLOCATOR   → malloc_func(user_data, size)
  │  └── MEMORY_MODE_SYSTEM      → os_malloc(size)
  │
  │  （以下以 Pool 模式为例）
  ▼
mem_allocator_malloc(allocator, size)         // mem_alloc.c:42
  │  通过 DEFAULT_MEM_ALLOCATOR 宏选择：
  │  ├── EMS  → gc_alloc_vo(heap, size)
  │  └── TLSF → tlsf_malloc(tlsf, size)
  │
  │  （以下以 EMS 为例）
  ▼
gc_alloc_vo(vheap, size)                      // ems_alloc.c:572
  │  计算总大小：tot_size = GC_ALIGN_8(size + OBJ_EXTRA_SIZE)
  │  LOCK_HEAP(heap)
  ▼
alloc_hmu_ex(heap, tot_size)                  // ems_alloc.c:527
  │  如果启用 GC：检查是否需要触发 gc（total_free_size < gc_threshold）
  ▼
alloc_hmu(heap, size)                         // ems_alloc.c:343
  │  ├── size < 248B：扫描 kfc_normal_list[32]（普通链表）
  │  │   ├── 找到：取出节点，必要时切分
  │  │   └── 未找到：去红黑树
  │  └── size ≥ 248B 或普通链表未命中：搜索 kfc_tree_root 红黑树
  │      └── Best-fit 查找
  │
  │  更新 total_free_size、highmark_size
  │  标记 pinuse bit（下一个 HMU 的 P bit）
  ▼
返回 hmu_t *
  │
  ▼
gc_alloc_vo（继续）
  │  hmu_set_ut(hmu, HMU_VO)
  │  hmu_unfree_vo(hmu)          // 清除 free bit
  │  ret = hmu_to_obj(hmu)       // 跳过 header + prefix → 用户指针
  │  UNLOCK_HEAP(heap)
  ▼
返回 void * 给用户
```

### 5.2 从 free 到 HMU

```
wasm_runtime_free(ptr)          // wasm_memory.c:1109
  → wasm_runtime_free_internal(ptr)    // wasm_memory.c:1001
    → (Pool) mem_allocator_free(pool_allocator, ptr)    // mem_alloc.c:54
      → gc_free_vo(heap, obj)                           // ems_alloc.c
        → obj_to_hmu(obj)  // 反转指针，恢复 hmu_t*
        → LOCK_HEAP
        → 设置 HMU_VO_FB bit（标记为 freed）
        → gci_add_fc(heap, hmu, size)  // 加入空闲链表 → 可能与相邻块合并
        → UNLOCK_HEAP
```

---

## 6. 对齐分配

WAMR 支持类似 C11 `aligned_alloc` 的对齐分配：

```c
// wasm_memory.c:1073-1100
void *wasm_runtime_aligned_alloc(unsigned int size, unsigned int alignment);
```

**仅 Pool 模式支持。** 内部调用 `gc_alloc_vo_aligned()`（`ems_alloc.c:632`）。

### 实现原理

**过度分配 + 元数据标记：**

```
┌─────────────┬──────────┬───────────┬───────────┬──────────────────┬───────────┐
│ HMU Header  │ Padding  │ Offset    │ Magic     │ Aligned Data     │ Padding   │
│  (4 bytes)  │(可变长度) │ (4 bytes) │ (4 bytes) │ (size bytes)     │ (overhead)│
└─────────────┴──────────┴───────────┴───────────┴──────────────────┴───────────┘
                                                    ▲
                                               user_ptr (对齐后返回)
Offset  + Magic = 8 bytes，存储：
  - Offset (4B): 从 HMU 到对齐地址的字节偏移
  - Magic  (4B): 0xA11C0000 | offset_low16bits（防止误用 realloc）
```

**关键约束（`ems_gc_internal.h:165-170`）：**
- 最小对齐：8 bytes
- 最大对齐：系统页大小（通常 4KB）
- **不支持 realloc**——调用 `wasm_runtime_realloc` 会返回 NULL（对齐会丢失）
- 正常 free 支持

---

## 7. GC 集成

当 `WASM_ENABLE_GC != 0` 时，EMS 堆具备 mark-sweep GC 能力，只管理 **WO（WASM Object）** 类型的对象。VO 对象不受 GC 影响。

### GC 触发时机

在 `alloc_hmu_ex()` 中（`ems_alloc.c:527-553`）：

```c
if (heap->total_free_size < heap->gc_threshold) {
    // 先尝试直接分配
    ret = alloc_hmu(heap, size);
    if (ret) return ret;
    // 分配失败，触发 GC
    do_gc_heap(heap);
}
return alloc_hmu(heap, size);  // GC 后再试
```

### GC 流程（ems_gc.c）

1. **标记阶段：** 遍历 rootset（模块实例的全局变量、操作数栈等），递归标记可达的 WO 对象
2. **扫描阶段：** 遍历所有 HMU，释放未标记的 WO 对象（调用 finalizer 后加入空闲链表）
3. **阈值更新：** `gc_threshold = total_free_size * gc_threshold_factor / 1000`

### WO 对象最终器

```c
// 设置最终器：对象被 GC 回收前调用
bool gc_set_finalizer(gc_handle_t handle, gc_object_t obj,
                      gc_finalizer_t cb, void *data);
```

类比 Java 的 `finalize()` 或 Rust 的 `Drop`，在 GC 回收对象时做清理工作。

### 根集管理

```c
int mem_allocator_add_root(mem_allocator_t allocator, WASMObjectRef obj);
```

将 wasm 全局变量 / 引用加入到 GC 的根集，确保活跃对象不被回收。

---

## 8. 堆迁移

EMS 支持将现存堆的内容**迁移到新的内存池**：

```c
int mem_allocator_migrate(mem_allocator_t allocator, char *pool_buf_new,
                          uint32 pool_buf_size);
```

**用途：** 需要在运行时扩大池大小，或更换内存区域（例如从 SRAM 迁移到 DRAM）。

内部由 `gc_migrate()` 实现（`ems_kfc.c`），遍历所有 HMU，在新池中重新布局。

---

## 9. 关键文件索引

| 文件 | 行数（约） | 内容 |
|------|-----------|------|
| `core/iwasm/common/wasm_memory.c` | ~1125 | 三模式 dispatch、Init/Destroy、上层 API |
| `core/shared/mem-alloc/mem_alloc.c` | ~270  | EMS/TLSF 适配 + TLSF 内嵌实现 |
| `core/shared/mem-alloc/mem_alloc.h` | 124 | 统一接口定义 |
| `core/shared/mem-alloc/ems/ems_gc_internal.h` | 579 | **核心定义**：HMU 位域、gc_heap_t、空闲链表、红黑树 |
| `core/shared/mem-alloc/ems/ems_alloc.c` | ~950 | 分配/释放主逻辑：`alloc_hmu`、`gc_alloc_vo`、`gc_free_vo` |
| `core/shared/mem-alloc/ems/ems_kfc.c` | ~300 | 堆初始化 + 块合并（gci_add_fc）+ 迁移 |
| `core/shared/mem-alloc/ems/ems_hmu.c` | 95  | 前后守卫验证（仅 GC_VERIFY 模式） |
| `core/shared/mem-alloc/ems/ems_gc.c` | ~300 | GC mark-sweep 回收逻辑 |
| `core/config.h:64-68` | — | `DEFAULT_MEM_ALLOCATOR` 宏定义 |

---

## 10. 快速总结

```
用户内存块 → gc_heap_t (元数据) + 数据区 (HMU 链)
                              │
                              ├─ 空闲 HMU → kfc_normal_list[0..31] + kfc_tree_root (红黑树)
                              │              alloc_hmu: 先查链表 → 再查树 → best-fit 切分
                              │
                              ├─ 在用 HMU → HMU_VO (手动) 或 HMU_WO (GC)
                              │              gc_alloc_vo / gc_alloc_wo
                              │              gc_free_vo 返还给空闲列表
                              │              或 GC mark-sweep 回收 WO
                              │
                              └─ 加锁：LOCK_HEAP / UNLOCK_HEAP (korp_mutex)
                                 粒度：整个堆一把锁
```
