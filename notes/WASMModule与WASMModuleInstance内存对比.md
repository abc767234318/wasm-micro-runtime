# WASMModule 和 WASMModuleInstance 的内存对比

## 一句话总结

**WASMModule（~2KB）是全局共享的蓝图，WASMModuleInstance（~65KB）是每次实例化独立分配的房子。** 大头在实例的线性内存（64KB/页），结构体本身开销很小。Module 可以 load 一次，实例化多次。

---

## 1. WASMModule 的内存

### 分配方式

```c
// wasm_loader.c:6841
// 第一步：分配 WASMModule 结构体本身，就一个固定大小
WASMModule *module = loader_malloc(sizeof(WASMModule), ...);

// 第二步：从 .wasm buffer 解析各个 section，动态分配子数组
module->types     = loader_malloc(type_count * sizeof(WASMType), ...);
module->functions = loader_malloc(func_count * sizeof(WASMFunction), ...);
// 关键：functions[i].code 直接指向原始 .wasm buffer 里的字节码，没有拷贝！
module->exports   = loader_malloc(export_count * sizeof(WASMExport), ...);
// ...
```

### 内存布局

```
┌──────────────────────────────────────────┐
│  WASMModule 结构体          (~200B)      │
│  · type_count, function_count, ...       │
│  · types → loader_malloc 的动态数组      │
│  · functions → loader_malloc 的动态数组   │
│  · exports → loader_malloc 的动态数组     │
│  · data_segments → loader_malloc 的动态数组│
├──────────────────────────────────────────┤
│  types[]                      (~500B)    │
├──────────────────────────────────────────┤
│  functions[]                  (~1KB)     │
│  ├── functions[0].code → .wasm buffer    │  ← 指向的是外部的 .wasm 文件 buffer
│  ├── functions[1].code → .wasm buffer    │
│  └── ...                                 │
├──────────────────────────────────────────┤
│  exports[]                    (~200B)    │
└──────────────────────────────────────────┘

总计：约 2KB（不含指向外部 buffer 的内容）
```

**关键：functions[i].code 指向的是原始 .wasm buffer，不是自己分配的内存。**

---

## 2. WASMModuleInstance 的内存

### 分配方式

```c
// wasm_runtime.c:2467-2507
// 一次 runtime_malloc 分配所有内容
total_size = sizeof(WASMModuleInstance)            // 结构体本身
           + sizeof(WASMMemoryInstance) × mem_cnt  // 内存元数据
           + module->global_data_size              // 所有全局变量的值
           + table_size                            // 所有 table 的元素数组
           + sizeof(WASMModuleInstanceExtra);      // 扩展信息（函数实例等）

module_inst = runtime_malloc(total_size, ...);  // 一次全分配
```

然后**线性内存**是独立分配的：
```c
// 在 memories_instantiate() 里
memory->memory_data = runtime_malloc(cur_page_count * 64KB, ...);
```

### 内存布局

```
一次 runtime_malloc 分配的主块：
┌──────────────────────────────────────────┐
│  WASMModuleInstance        (~200B)       │
│  · module → WASMModule                   │  ← 指向 load 产物
│  · memories → 后面的 WASMMemoryInstance   │
│  · tables → 后面的 WASMTableInstance      │
├──────────────────────────────────────────┤
│  WASMMemoryInstance[0]     (~40B)        │
│  · memory_data → ──────────────┐         │
│  · cur_page_count              │         │
├────────────────────────────────┤         │
│  Global Data 区    (~20B)      │         │
│  · 全局 i32 变量 1 = 42        │         │
│  · 全局 f64 变量 2 = 3.14     │         │
├────────────────────────────────┤         │
│  WASMTableInstance[0]  (~40B)  │         │
│  · elems[0..max_size]          │         │
├────────────────────────────────┤         │
│  WASMModuleInstanceExtra       │         │
│  · functions[N]  (~1KB)        │         │
│    (WASMFunctionInstance[])     │         │
│    └── u.func → WASMModule     │         │  ← 指向 module
│  · globals[N]                  │         │
│  · WASI 上下文                 │         │
│  · GC 堆句柄                   │         │
└────────────────────────────────┘         │
                                           │
      独立分配的线性内存：                    │
      ┌──────────────────────────┐         │
      │  memory_data             │ ←───────┘
      │  (cur_page_count × 64KB)  │
      │                          │  这是最大的开销！
      └──────────────────────────┘
```

---

## 3. 内存依赖关系

```
原始 .wasm buffer（用户提供，load 后可选释放）
        │
        ├── WASMModule（~2KB，全局共享）
        │       │
        │       ├── WASMModuleInstance #1（~65KB，独立）
        │       │       └── memory_data（64KB 线性内存）
        │       │
        │       ├── WASMModuleInstance #2（~65KB，独立）
        │       │       └── memory_data（64KB 线性内存）
        │       │
        │       └── WASMModuleInstance #3（~65KB，独立）
        │               └── memory_data（64KB 线性内存）
        │
        └── 释放顺序：
            必须先销毁所有 Instance → 再销毁 Module → 最后释放 .wasm buffer
```

---

## 4. 各内存项的大小明细

以一个典型 wasm 模块为例：10 个函数，1 个 memory（1 页=64KB），5 个全局变量，1 个 table。

| 内存项 | 在哪 | 大小（约） | 每实例还是共享 |
|--------|------|-----------|---------------|
| 原始 .wasm buffer | 用户提供 | 几 KB~几十 KB | 共享，load 后可释放 |
| WASMModule 结构体 | loader_malloc | ~200B | 共享 |
| WASMModule 子数组（types, funcs, exports 等） | loader_malloc | ~1-2KB | 共享 |
| **WASMModuleInstance 结构体** | runtime_malloc | ~200B | 每实例独有 |
| Global Data 区 | runtime_malloc（嵌入 instance） | ~20B | 每实例独有 |
| Table elems 数组 | runtime_malloc（嵌入 instance） | ~40B | 每实例独有 |
| WASMFunctionInstance 数组 | runtime_malloc（嵌入 instance） | ~1KB | 每实例独有 |
| **线性内存 memory_data** | runtime_malloc（独立分配） | **64KB/页** | **每实例独有，最大头！** |
| WASMExecEnv + wasm 执行栈 | runtime_malloc | ~68KB（含 64KB 栈） | 每线程/调用上下文独有 |

**结论：多实例的主要开销是线性内存，不是结构体本身。**

---

## 5. 省内存技巧

### 技巧 1：`wasm_binary_freeable = true`

```c
LoadArgs args = { .wasm_binary_freeable = true };
wasm_module_t module = wasm_runtime_load_ex(buffer, size, &args, ...);

// load 完后可以立即 free(buffer) 了！
// loader 已经把需要用到的数据（字符串常量等）从 buffer 拷到了 module 里
free(buffer);
```

10KB 的 .wasm buffer 可以回收，只保留 WASMModule 的 ~2KB。

### 技巧 2：共享内存（Shared Heap 或 Shared Memory）

如果多个实例需要访问同一块数据，用 Shared Heap 或 Shared Memory 让它们共享一块线性内存，而不是各自分配 64KB。

### 技巧 3：减少内存页数

```c
wasm_runtime_instantiation_args_set_max_memory_pages(&args, 1);  // 限制为 1 页
```

---

## 6. 为什么不能用 Module 代替 Instance

| | WASMModule | WASMModuleInstance |
|---|---|---|
| 作用 | 蓝图 | 运行时房子 |
| 全局变量 | 只有初始值声明 | 实际的运行时值（可修改） |
| 线性内存 | 没有 | 实际分配了 |
| 数据段 | 声明"要在偏移 X 写 Y" | 已经写进去了 |
| 导入函数 | 声明"我需要 fd_write" | 已经绑定到实际实现 |
| exec_env | 没有 | 有 |
| 能执行吗 | 不能 | 能 |
| 多次创建 | 不需要 | 可以，每次独立状态 |

**load 是编译菜谱，instantiate 是按菜谱做菜。你可以用同一份菜谱（WASMModule）做三份菜（WASMModuleInstance），每份菜独立上桌。**
