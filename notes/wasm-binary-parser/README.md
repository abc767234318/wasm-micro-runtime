# WAMR Wasm Binary Parser 学习笔记

这组笔记以仓库提交 `ca244b0b2debd5a05a6974edd5fc45c12769b0a2` 为基准，目标不是复述 WebAssembly 规范，而是回答下面四个工程问题：

1. `.wasm` 的每个 Section 在磁盘上怎样编码？
2. WAMR 用哪些 C 结构保存它，为什么这样设计？
3. `wasm_loader.c` 怎样完成拆段、解析、校验和 fast-interpreter lowering？
4. 怎样 dump 一个 Wasm binary，并逐字节 disassemble function body？

## 阅读顺序

| 顺序 | 笔记 | 解决的问题 |
| --- | --- | --- |
| 1 | [01. 大型 C 项目设计导读](01-C语言设计导读.md) | 看懂指针游标、`count + pointer`、union、尾数组、两遍扫描、所有权、`goto fail` 和编译宏 |
| 2 | [02. Binary 格式与加载总流程](02-Binary格式与加载总流程.md) | 从 magic/version 到 `WASMSection`，再到 `WASMModule` 和函数预处理的完整调用链 |
| 3 | [03. 每个 Section 的解析与数据结构](03-逐Section解析.md) | 逐段对照二进制布局、loader 函数、内部表示、校验和设计原因 |
| 4 | [04. 核心数据结构与内存所有权](04-核心数据结构与内存所有权.md) | 从 `WASMModule` 出发理解所有对象的关系、索引空间和释放顺序 |
| 5 | [05. Function Body 解码与反汇编](05-Function-Body解码与反汇编.md) | Code entry、locals、opcode immediate、控制栈、类型栈、两遍 lowering 和手工反汇编 |
| 6 | [06. Dump 与调试实战](06-Dump与调试实战.md) | `xxd`、`wasm-objdump`、`wasm2wat`、WAMR 日志、`binarydump` 各自能看什么 |
| 7 | [07. 源码导航与检查清单](07-源码导航与检查清单.md) | 遇到新 Section/opcode 时应追哪些函数，以及如何确认没有漏掉 feature 分支 |

## 一张总图

```text
调用方拥有的 uint8_t wasm_buffer
        |
        v
wasm_loader_load()
        |
        v
load(): magic/version 校验
        |
        v
create_sections(): 只建立指向原 buffer 的 WASMSection 链表
        |
        v
load_from_sections()
        |-- Type/Import/Table/Memory/Global/... -> WASMModule 元数据
        |-- Function + Code -------------------> WASMFunction
        |-- Custom ----------------------------> name/branch-hint/可选保留
        |
        v
wasm_loader_prepare_bytecode(): 每个非导入函数
        |-- 解码 immediate
        |-- 验证索引与类型
        |-- 模拟 operand/control stack
        |-- 计算 max stack/block
        `-- fast interp 开启时生成 code_compiled + const pool
```

## 最重要的三个认识

### 1. Section 节点不是 AST

`WASMSection` 只有 `type/body/size/next`。`section_body` 直接借用原始 `.wasm` buffer；它只是第一阶段的“切片视图”，不是解析后的语法树。真正的模块表示是 `WASMModule`。

### 2. Function Section 和 Code Section 必须合起来看

Function Section 只给每个本地函数一个 `typeidx`；Code Section 只给同序号函数的 locals 和 expression。WAMR 预先找到两段，然后由 `load_function_section()` 同步推进两个游标，把它们合成为 `WASMFunction`。因此单独读 `load_code_section()` 会误以为 WAMR 没有解析函数体。

### 3. Function body 的“解析”不止一层

WAMR 有三种容易混淆的字节流：

| 字节流 | 含义 | 是否仍是标准 Wasm |
| --- | --- | --- |
| 文件里的 Code body | 原始 opcode + LEB immediate | 是 |
| classic interpreter 预处理后的 `func->code` | 可能原地改 opcode、填 NOP、缓存 `br_table` | 不完全是 |
| fast interpreter 的 `func->code_compiled` | handler/相对地址、定宽 immediate、operand slot offset | 否，属于 WAMR 内部格式 |

要做标准反汇编，应针对文件 buffer 或尚未改写的 `func->code`；不能把 `code_compiled` 当作 Wasm binary 交给普通 disassembler。

## 范围说明

- 主线是 interpreter 的完整 loader：[`core/iwasm/interpreter/wasm_loader.c`](../../core/iwasm/interpreter/wasm_loader.c)。
- [`wasm_mini_loader.c`](../../core/iwasm/interpreter/wasm_mini_loader.c) 是构建时替换实现；它跳过部分完整性检查，只能处理可信输入，差异见第 2、7 篇。
- AOT 文件有另一套容器和 [`aot_loader.c`](../../core/iwasm/aot/aot_loader.c)，不属于“解析标准 `.wasm` binary”的主线。
- 提案代码由编译宏裁剪。笔记覆盖当前源码中的 GC、reference types、bulk memory、multi-memory、memory64、SIMD、threads/atomics、tags、exception handling、tail call、stringref 分支，但某一构建产物未必包含它们。
