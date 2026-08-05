# 为什么 WAMR 里有那么多"只转发参数"的包装函数？

## 现象

```c
// wasm_runtime.c:73
WASMModule *
wasm_load_from_sections(WASMSection *section_list, char *error_buf,
                        uint32 error_buf_size)
{
    return wasm_loader_load_from_sections(section_list, error_buf,
                                          error_buf_size);
}

// wasm_runtime.c:58
WASMModule *
wasm_load(uint8 *buf, uint32 size, ...)
{
    return wasm_loader_load(buf, size, ...);
}

// wasm_runtime.c:80
void
wasm_unload(WASMModule *module)
{
    wasm_loader_unload(module);
}
```

看起来什么都没干，参数一模一样传给下层，纯属多余？

## 原因：C 语言没有 `pub/private`，这是手工做的模块边界

### WAMR 的三层结构

```
外部调用方（用户代码、wasm_runtime_common.c）
        │
        │  只能调 wasm_runtime.h 里的函数
        │
─────── 模块边界 ────────
        │
core/iwasm/interpreter/wasm_runtime.c    ← 解释器公共接口
        │
        │  内部可以调 wasm_loader.h
        │
core/iwasm/interpreter/wasm_loader.c    ← 加载器内部实现
```

### 包装函数 = C 语言的访问控制

Rust / C++ 有：

```rust
pub fn load() { ... }         // 外面能调
fn internal_loader() { ... }  // 外面不能调
```

C 语言没有这个语法。唯一的办法是**物理隔离**：

- `wasm_loader_load_from_sections` 声明在 `wasm_loader.h`，这个头文件**不对外暴露**
- `wasm_load_from_sections` 声明在 `wasm_runtime.h`，这个头文件是**给外部用的**
- 外部代码 `#include "wasm_runtime.h"` 拿不到 loader 层的函数
- 解释器内部 `#include "wasm_loader.h"` 才能调 loader

**包装函数的本质：把"私有"的实现函数重新包装成一个"公有"的接口。**

### 为什么不能直接让 loader 的函数变成"公有"的？

如果真的把 `wasm_loader_load` 直接暴露给外部，结果就是：

- loader 的内部结构体（`WASMSection`、内部链表等）全部暴露出去
- 外部代码可能直接操作 loader 的内部状态，破坏封装
- 将来改了 loader 的签名或返回值格式，所有外部调用点都要跟着改

加一层包装，这些风险全部被隔在模块边界后面。

### 还有一个好处：预留扩展点

现在是个空壳，但不代表永远是空壳：

```c
WASMModule *wasm_load(uint8 *buf, uint32 size, ...)
{
    // 以后可以在这里加：
    // - 统计加载耗时
    // - 记录日志
    // - 安全检查
    // - 全局锁
    return wasm_loader_load(buf, size, ...);
}
```

如果一开始就让外部直接调 `wasm_loader_load`，之后想加一行监控就得改几十个文件。有一个包装函数，改一个地方就行。

## 总结

| 语言 | 模块边界怎么实现 |
|------|-----------------|
| Rust | `pub fn` vs `fn` |
| C++ | `public:` vs `private:` |
| C | **目录隔离 + 同名包装函数 = 模块边界** |

这不是设计缺陷，是大型 C 项目的标配。Linux 内核、PostgreSQL、CPython 全是这么干的。十几万行 C 代码如果每个人都直接调别人的内部函数，没有任何人能维护下去。
