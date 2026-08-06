# Host-Managed Heap：宿主直接调用 wasm 模块里的 malloc/free

## 1. 场景：宿主想把数据传给 wasm 模块

假设宿主（WAMR，C 代码）想把一个字符串传给 wasm 模块。最笨的办法：

### ❌ 笨办法：宿主自己往线性内存里怼

```c
// wasm 模块的内存布局（在线性内存内部）：
// [未分配区 ......................................]

// 宿主硬编码一个地址："我就写在线性内存偏移 1000 的位置"
uint8 *wasm_memory = module_inst->memories[0].memory_data;
strcpy((char *)wasm_memory + 1000, "hello");

// 调 wasm 函数，传入偏移 1000
wasm_runtime_call_wasm(exec_env, some_func, 1, &(uint32){1000});
```

问题：
- **宿主不知道 wasm 模块内部怎么用内存**——偏移 1000 可能已经被 wasm 自己的栈/堆用了
- 写过去就把 wasm 的数据踩烂了
- 像两个人在同一张纸上写字，互相不知道对方写哪了

### ✅ 好办法：让 wasm 自己的 malloc 来决定地址

```c
// 1. 宿主调 wasm 模块导出的 malloc(6)
uint32 offset;
wasm_runtime_call_wasm(exec_env, wasm_malloc, 1, &(uint32){6}, &offset);
// offset = 比如 2048

// 2. 宿主把字符串写到偏移 2048 处
uint8 *wasm_memory = module_inst->memories[0].memory_data;
strcpy((char *)wasm_memory + offset, "hello");

// 3. 宿主调 wasm 函数，传入 2048
wasm_runtime_call_wasm(exec_env, some_func, 1, &offset, NULL);
// wasm 函数读到 "hello"，万事大吉

// 4. 用完了，宿主调 wasm 的 free(2048)
wasm_runtime_call_wasm(exec_env, wasm_free, 1, &offset, NULL);
```

**malloc 是 wasm 自己的，分配出来的地址在 wasm 线性内存内，和 wasm 模块内部的堆管理完全一致，永远不会冲突。**

---

## 2. WAMR 怎么发现 wasm 导出了 malloc/free

在 loader 阶段（`wasm_loader.c:6603-6691`），loader 扫描 export section：

```c
// 初始化为 -1，表示没找到
module->malloc_function = (uint32)-1;
module->free_function   = (uint32)-1;
module->retain_function = (uint32)-1;

for (每个导出项) {
    if (导出名 == "malloc" && 签名是 (i32) → i32)
        module->malloc_function = 导出索引;

    if (导出名 == "free" && 签名是 (i32) → ())
        module->free_function = 导出索引;

    // AssemblyScript 兼容：
    // "__new" = malloc, "__pin"/"__retain" = retain, "__release"/"__unpin" = free
}
```

在 instantiate 阶段，把函数索引转为函数实例指针：

```c
module_inst->e->malloc_function =
    &module_inst->e->functions[module->malloc_function];
module_inst->e->free_function =
    &module_inst->e->functions[module->free_function];
```

运行时，宿主通过 `execute_malloc_function` / `execute_free_function` 调用这些 wasm 函数。

---

## 3. 什么时候 wasm 模块没有 malloc

不是所有 .wasm 文件都会导出 malloc/free。分三种情况。

### 3.1 纯计算模块，根本不需要堆

```c
// 只有栈变量，零 malloc 调用
int add(int a, int b) {
    int result = a + b;   // 栈上
    return result;
}
```

编译成 wasm 后，函数体只有几条 `local.get` / `i32.add` 指令，全程操作数栈搞定。**不需要 malloc，编译器也不会塞。**

### 3.2 编译器/工具链没链 libc

| 工具链 | 默认可链接 libc? | 会自带 malloc? |
|--------|-----------------|---------------|
| Emscripten (`emcc`) | 是 | 是，完整 libc，导出 malloc/free |
| wasi-sdk (`clang --target=wasm32-wasi`) | 可选 | 用了 `#include <stdlib.h>` 才链接 |
| Rust `wasm32-wasi` | — | `wee_alloc` 或 `dlmalloc`，不导出 |
| Rust `wasm32-unknown-unknown` | — | 通常用 `#[global_allocator]`，不导出 |
| TinyGo | — | 自带 GC 分配器，无显式 malloc |
| AssemblyScript | — | 自带 `__new` / `__release`，无标准 malloc |
| 手写 `.wat` | — | 什么都没，除非自己实现 |

**Emscripten 会导出 malloc 是因为它的目标场景是"把已有的 C/C++ 代码搬到浏览器里跑"，模拟了完整 POSIX 环境。** 但如果你用 wasi-sdk 编译一个从来不调 `malloc` 的程序，链接器就不会把它链进去。

### 3.3 malloc 存在但没有导出

wasm 模块内部可能有 malloc（libc 链进去了），但如果 export section 里没列出 `malloc`，宿主就看不见它。loader 扫描 export 时发现 `module->malloc_function == (uint32)-1`，就不会建立 host-managed heap：

```c
// instantiate 阶段，这段代码直接跳过
if (module->malloc_function != (uint32)-1) {
    // 这个分支不执行，没有 host-managed heap
}
```

### 3.4 总结

```
malloc 在不在 wasm 里？
  │
  ├── 纯计算，没用堆 ────→ 没有，也不需要
  │
  ├── 用了堆，Emscripten ──→ 有，且导出
  │
  ├── 用了堆，wasi-sdk ──→ 有，但不一定导出
  │
  ├── Rust/TinyGo/AS ──→ 有自己的分配器，不叫 malloc
  │
  └── 手写 wat ────→ 完全没有
```

**Host-managed heap 只是一个"锦上添花"的特性，WAMR 不依赖它就能正常工作。** loader 检测不到导出的 malloc/free，就退回到别的方式。

---

## 4. 两种"内存管理"的对比：容易混淆，必须分清

WAMR 涉及**两套完全独立的内存管理**：

| | WAMR 自己的分配器 | Host-Managed Heap |
|---|---|---|
| **管理的是什么内存** | WAMR 内部结构体（WASMModule、WASMModuleInstance、WASMExecEnv 等） | wasm 模块的**线性内存内部** |
| **谁调用 malloc** | WAMR 自己的代码 | **宿主代码**（你的 C 程序） |
| **malloc 实现在哪** | WAMR 内部（EMS / TLSF / 系统 malloc） | **wasm 模块里**（Emscripten/libc 编译进去的） |
| **free 实现在哪** | WAMR 内部 | **wasm 模块里** |
| **分配出来的地址** | C 堆上的地址（WAMR 进程空间） | **wasm 线性内存内的偏移量** |
| **影响范围** | WAMR 运行时的稳定性和性能 | wasm 模块内部数据的安全性和正确性 |
| **用户能配置吗** | 能（三种模式） | 不需要配置，自动检测 |

### 图解

```
┌─ WAMR 进程空间 ────────────────────────────────────────┐
│                                                        │
│  WAMR 自己的内存池 (Pool/Allocator/System)              │
│  ┌──────────────────────────────────────┐              │
│  │  WASMModule (~2KB)                   │              │
│  │  WASMModuleInstance (~200B)          │              │
│  │  WASMExecEnv + wasm 栈 (~68KB)       │              │
│  │  ...                                 │              │
│  └──────────────────────────────────────┘              │
│  用的是 wasm_runtime_malloc / wasm_runtime_free          │
│                                      │                 │
│                                      │ memory_data 指针指向下面
│                                      ▼                 │
│  ┌─ wasm 线性内存（wasm 模块的沙箱）─────────────────┐  │
│  │                                                   │  │
│  │  wasm 自己的堆（Emscripten/libc 编译进去的 malloc）   │  │
│  │  ┌─────────────────────────────────────┐          │  │
│  │  │  wasm 函数分配的局部对象              │          │  │
│  │  │  宿主通过 wasm_malloc 分配的数据      │          │  │
│  │  │  wasm 运行时栈                      │          │  │
│  │  └─────────────────────────────────────┘          │  │
│  │                                                   │  │
│  └───────────────────────────────────────────────────┘  │
│  用的是 wasm 模块导出的 malloc / free                     │
└────────────────────────────────────────────────────────┘
```

---

## 5. 类比

```
WAMR 自己的分配器
   = 你的办公室（进程空间）：
     你在这间办公室里放桌子（WASMModule）、椅子（WASMModuleInstance）、
     档案柜（ExecEnv）。这些是 WAMR 运行时自己的工作环境。
     用的是你自己买的家具（WAMR 的 malloc）。
     怎么买？三种方式：Pool（去宜家批发）、Allocator（雇人买）、System（每次去超市）。

Host-Managed Heap
   = 你办公室里的一个"客户会客室"（wasm 线性内存）：
     客户（wasm 模块）自己管里面家具怎么摆。
     你要往会客室里放东西，不要自己硬塞进去，而是让客户（wasm 的 malloc）
     告诉你"放这里安全"。
     用的是客户自带的家具管理系统（编译进去的 libc malloc）。
```

---

## 6. 各语言的分配器实现概览

前面提到 Rust/TinyGo/AssemblyScript 等语言有自己的分配器，不导出标准 malloc。这里简要看看它们各自的实现方式。

### 6.1 Rust

Rust 编译到 wasm 时，标准库里的 `Box`、`Vec`、`String` 等都需要堆分配，但 Rust **不导出 malloc 符号**。堆分配通过 `#[global_allocator]` 机制注入：

```rust
// 默认使用 wee_alloc（一个针对 wasm 优化的微型分配器，~1KB）
#[global_allocator]
static ALLOC: wee_alloc::WeeAlloc = wee_alloc::WeeAlloc::INIT;

// 也可以换成 dlmalloc（更通用，但更大）
#[global_allocator]
static ALLOC: dlmalloc::GlobalDlmalloc = dlmalloc::GlobalDlmalloc;
```

`wee_alloc` 的特点：极小的代码体积（~1KB），针对 wasm 的 32 位线性内存优化，使用 freelist 算法。适合"分配少量固定大小对象"的场景。

`dlmalloc` 的特点：Doug Lea 的经典 malloc 实现，碎片控制好，但代码体积 ~5-10KB。

**对 WAMR 的影响：** 宿主不能通过"调 wasm 的 malloc"来在线性内存里分配合法地址。Rust 的分配器是模块内部私有的，没有导出。宿主如果要往线性内存里放数据，必须通过别的接口（比如共享一段预分配好的 buffer，通过函数参数传入偏移量）。

### 6.2 TinyGo

TinyGo 编译出的 wasm 自带一个**保守式标记-清除 GC（mark-sweep）**，因为 Go 的 goroutine 和 interface 需要 GC。分配器不是 malloc，而是 GC 管理的堆：

```go
// TinyGo 编译后：
// - 栈变量 = wasm 线性内存里的固定偏移
// - 堆变量 = GC 管理，通过 runtime.alloc() 内部分配
// - 字符串、slice 等 = GC 管理
```

**对 WAMR 的影响：** 没有 malloc/free 导出，但有一个 GC 在内部跑。在线性内存里写数据需要非常小心——你写的地址可能正好是 GC 认为的空闲区域。TinyGo 通常通过特殊约定的方式暴露内存接口（比如 `//export` 导出一个函数返回 buffer 地址）。

### 6.3 AssemblyScript

AssemblyScript 编译出的 wasm 使用**引用计数 + 可选纯 GC** 的混合内存管理。导出的是 `__new` / `__retain` / `__release`，不是 `malloc` / `free`：

```typescript
// AssemblyScript 源码
let arr = new Array<i32>(10);  // 内部调 __new
// __retain 增加引用计数
// __release 减少引用计数，计数归零时释放
```

WAMR 的 loader 专门检测了这些导出名（`wasm_loader.c:6624-6674`）：

```c
if (!strcmp(export->name, "__new")) {
    // 签名: (size: i32, class_id: i32) → pointer: i32
    module->malloc_function = export->index;
    // 同时寻找配套的 __pin / __retain
}
if (!strcmp(export->name, "__release") || !strcmp(export->name, "__unpin")) {
    // 签名: (pointer: i32) → ()
    module->free_function = export->index;
}
```

**对 WAMR 的影响：** AssemblyScript 是 WAMR 官方适配得最好的非 C 语言。WAMR 认识 `__new` / `__pin` / `__retain` / `__release` / `__unpin` 命名，能自动建立 host-managed heap。

### 6.4 对比

| | Emscripten (C/C++) | Rust | TinyGo | AssemblyScript |
|---|---|---|---|---|
| 分配方式 | malloc/free (dlmalloc) | wee_alloc / dlmalloc | GC (mark-sweep) | 引用计数 (__new/__release) |
| 导出 malloc? | 是 | 否 | 否 | 导出 __new 代替 |
| WAMR 自动识别? | 是 | 否 | 否 | 是（通过 __new 等命名） |
| 宿主可安全调用? | 是，调 malloc/free | 否，需约定接口 | 否，GC 干扰 | 是，调 __new/__release |

---

## 7. 真实例子：WAMR 自己就在用

`wasm_runtime.c:3834-3837`：

```c
// WAMR 需要在 wasm 线性内存里分配 space
else if (module_inst->e->malloc_function && module_inst->e->free_function) {
    // 调 wasm 模块里的 malloc
    execute_malloc_function(module_inst, exec_env,
                            module_inst->e->malloc_function,
                            module_inst->e->retain_function,
                            size, &offset);
    // offset 就是 wasm 线性内存里的安全地址
}
```

WAMR 自己在某些操作里（比如动态分配 wasm 对象）也会走 wasm 模块的 malloc，而不是自己随便在线性内存里写。

---

## 8. 总结

| 问题 | 答案 |
|------|------|
| WAMR 自己的 malloc 管什么？ | WAMR 运行时内部结构体（模块、实例、执行环境） |
| wasm 的 malloc 管什么？ | wasm 线性内存内部的堆空间 |
| 宿主怎么在 wasm 内存里放数据？ | 调 wasm 的 malloc 拿到合法地址，再往里写 |
| 为什么不直接硬编码地址？ | 会踩烂 wasm 自己的数据 |
| 两套分配器有什么关系？ | 没有关系，管的是两块完全不同的内存 |
