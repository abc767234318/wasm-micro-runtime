我觉得最核心的原因不是“WABT 不好”，而是：对 WAMR 来说，binary parsing 并不是一个独立组件，它和验证、内存管理、内部数据结构以及 fast interpreter 的预编译绑得很紧。

从当前代码可以推断出几个主要原因。

**1. WAMR 的目标是 micro runtime，WABT 相对太重**

WABT 更偏向完整的 WebAssembly 工具箱：

- 主要使用 C++
- 有自己的 `Module`、`Expr` 等中间数据结构
- 支持 WAT、wasm2wat、wat2wasm、反汇编、重写等工具能力
- 通常依赖 STL、较完整的 C++ runtime 和堆内存

但 WAMR 要跑在：

- MCU
- RTOS
- Zephyr、NuttX、RT-Thread
- SGX
- 没有完整 libc/C++ runtime 的环境
- 内存只有几百 KB 甚至更少的设备

如果引入 WABT，哪怕只使用 binary reader，也会显著增加代码体积、C++运行时依赖和移植成本。WAMR 当前 loader 基本是 C、指针和显式 allocator，这一点对嵌入式环境很重要。

**2. WAMR 不需要通用 AST**

WABT 通常把 binary 解析成自己的模块表示：

```
wasm binary
    -> WABT Module/Expr AST
    -> 转换成 WAMR WASMModule
    -> 转换成 fast interpreter bytecode
```

这会多出一套中间结构，意味着：

- 模块信息存两份
- 函数指令至少遍历两遍
- 更多小对象和动态分配
- 需要维护 WABT AST 到 WAMR 数据结构的转换层
- 峰值加载内存明显增加

而 WAMR 当前做法更直接：

```
wasm binary
    -> WASMModule
    -> 验证函数
    -> code_compiled
```

Section 节点甚至主要只是指向原 binary buffer，并没有完整复制 section。这个模型更符合它对加载期内存的要求。

**3. Parser、validator 和 fast interpreter lowering 是融合的**

这是我认为最关键的技术原因。

`wasm_loader_prepare_bytecode()` 在一次 opcode 遍历中同时做：

- 解码 immediate
- 检查 local/global/function index
- 模拟 operand type stack
- 验证 block 输入输出类型
- 计算最大 operand stack
- 计算最大 block stack
- 给 fast interpreter 分配 frame slot
- 生成 operand offset
- 预解析分支目标
- 生成 `code_compiled`

入口在 [wasm_loader.c (line 11777)](D:/Github Project/wasm-micro-runtime/core/iwasm/interpreter/wasm_loader.c:11777)。

也就是说，它不是：

```
parse -> validate -> compile
```

三个清晰分离的组件，而更接近：

```
parse + validate + lower
```

如果换成 WABT，WAMR 仍然得重新遍历 WABT AST，完成 slot 分配和 fast bytecode 生成。这样 WABT 只能替代前面很小的一部分工作，却引入一套很大的依赖。

**4. WAMR 需要完全控制内存分配**

WAMR 的分配最终可以接入：

- system allocator
- pool allocator
- embedder 提供的 allocator
- 固定大小 runtime heap

Loader 也大量使用带溢出检查的 `loader_malloc()`，并明确控制哪些字符串、data segment 和 binary buffer需要复制，哪些可以直接引用。

例如它支持在满足条件时提前释放原始 `.wasm` buffer。要做到这一点，loader 必须精确知道每一个字段是：

```
引用原 binary
还是独立分配
```

如果使用 WABT，就必须让 WABT 的所有分配也服从 WAMR 的 allocator 和生命周期模型，改造成本不低。

**5. WAMR 需要高度可裁剪**

WAMR 的 Wasm proposal 支持大量依赖编译宏：

```
WASM_ENABLE_SIMD
WASM_ENABLE_GC
WASM_ENABLE_REF_TYPES
WASM_ENABLE_MULTI_MEMORY
WASM_ENABLE_MEMORY64
WASM_ENABLE_TAIL_CALL
WASM_ENABLE_EXCE_HANDLING
```

关闭一个功能后，不只是运行时不执行，它希望相关 loader 和 interpreter 代码也能被编译器移除。

WABT 更偏向“解析完整格式，然后根据 feature set 判断是否合法”。这对桌面工具很合理，但对 WAMR 来说，即使最后拒绝某个 feature，相应解析代码仍可能进入最终 binary。

WAMR 自己写 loader，可以让功能裁剪深入到预处理器级别。

**6. 执行器需要的 binary 形式很特殊**

Fast interpreter 会把 opcode 替换成：

- handler 指针
- handler 相对地址
- 固定宽度 immediate
- `int16` operand slot offset
- 已经 patch 好的分支地址

例如 handler 地址的 emit 在 [wasm_loader.c (line 9390)](D:/Github Project/wasm-micro-runtime/core/iwasm/interpreter/wasm_loader.c:9390)。

这种内部格式与 WABT 的 AST 或标准 Wasm bytecode都不一致。即使 WABT 完成了解析，WAMR 还是需要自己的 lowerer，因此直接在 loader 中生成会更高效。

**7. 安全边界也更容易控制**

Wasm binary 是不可信输入。WAMR 自己的 loader 对每次读取都显式检查：

- pointer 是否越过 section 结尾
- pointer 加法是否溢出
- LEB128 是否过长
- index 加法是否溢出
- 数量乘以结构大小是否溢出
- section size 是否完全匹配

内部 LEB128 实现在 [bh_leb128.c (line 8)](D:/Github Project/wasm-micro-runtime/core/shared/utils/bh_leb128.c:8)。

自己实现并不天然更安全，但它让 WAMR 能按照自己的内存模型、错误处理和硬件约束审计整个输入边界，而不必同时理解第三方库的异常、allocator 和对象生命周期。

当然，这个选择也有明显代价：

- Loader 代码非常大。
- 每个新 Wasm proposal 都要自己跟进。
- Parser 和 validator 耦合较深，维护难度高。
- 容易出现“某种组合 feature 下验证不完整”的问题。
- 不能直接享受 WABT 的 parser 测试和兼容性成果。

所以对桌面 runtime 或开发工具，我大概率会优先考虑成熟 parser；但对 WAMR 这种强调小体积、纯 C、可裁剪、受控分配，并且需要在加载期直接生成 fast interpreter bytecode 的 runtime，自研 loader 是合理的工程选择。

一句话概括：

> 引入 WABT 只能省掉 WAMR loader 最前面的 binary 解码，却会增加 C++依赖、AST 内存和转换层；真正复杂的验证及 fast interpreter lowering 仍然必须由 WAMR 自己完成。