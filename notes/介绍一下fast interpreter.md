WAMR 的 Fast Interpreter 可以理解成：

> 在加载 `.wasm` 时，把标准 Wasm 栈式字节码转换成一套“基于 frame slot、已解析跳转、可直接分发”的内部字节码，然后仍然由 C 解释执行。

它不是 JIT，不生成 x86/ARM 机器码；它更接近 threaded interpreter + 轻量 load-time compiler。

**和 Classic 的根本区别**

|方面|Classic|Fast|
|---|---|---|
|执行代码|`func->code`|`func->code_compiled`|
|操作数|动态 operand stack|frame 中静态分配的 slot|
|`local.get`|运行时 push|通常加载时直接消除|
|immediate|运行时读取 LEB128|加载时转成定长数据|
|分支|维护控制栈、查找目标|目标地址提前 patch|
|dispatch|opcode 查表/computed goto|指令流直接携带 handler|
|内存|较低|需要额外 compiled code|
|速度|基准|文档称大约快 2 倍|

代码选择在 [wasm_runtime.h (line 506)](D:/Github Project/wasm-micro-runtime/core/iwasm/interpreter/wasm_runtime.h:506)：

```
classic -> func->u.func->code
fast    -> func->u.func->code_compiled
```

**Fast frame 是什么样的**

Fast frame 定义在 [wasm_interp.h (line 19)](D:/Github Project/wasm-micro-runtime/core/iwasm/interpreter/wasm_interp.h:19)，核心布局大致是：

```
WASMInterpFrame
+---------------------------+
| prev_frame                |
| function                  |
| ip                        |
| ret_offset                |
| lp -------------------+   |
|                       |   |
| operand[]             |   |
|   常量池              |   | 负 offset
|   --------------------|<--+
|   参数                |      offset 0...
|   locals              |
|   临时计算 slot       |
+---------------------------+
```

基本存储单位是一个 32 位 cell：

- `i32/f32`：1 cell
- `i64/f64`：2 cells
- `v128`：4 cells
- 引用类型按指针宽度占相应 cell

`lp` 指向参数区开头。内部指令用一个有符号 `int16 offset` 表示操作数相对于 `lp` 的位置，所以常量池可以使用负 offset，参数、locals 和临时值使用非负 offset。

**从 Wasm 栈机到 slot 指令**

考虑这个函数：

```
(func (param i32 i32) (result i32)
  local.get 0
  local.get 1
  i32.add)
```

原始 Wasm 指令是：

```
local.get 0
local.get 1
i32.add
end
```

Classic interpreter 会真的执行：

```
push local[0]
push local[1]
rhs = pop()
lhs = pop()
push(lhs + rhs)
```

Fast loader 则维护一个“抽象操作数栈”，里面保存的不是运行时值，而是值所在的 slot offset：

```
local.get 0 -> 抽象栈压入 offset(local0)
local.get 1 -> 抽象栈压入 offset(local1)
i32.add     -> 弹出两个 offset，分配一个结果 offset
```

`local.get` 会被直接优化掉，代码在 [wasm_loader.c (line 13961)](D:/Github Project/wasm-micro-runtime/core/iwasm/interpreter/wasm_loader.c:13961)：

```
/* Get Local is optimized out */
skip_label();
operand_offset = local_offset;
PUSH_OFFSET_TYPE(local_type);
```

最终生成的内部指令在概念上类似：

```
I32_ADD
    rhs_offset
    lhs_offset
    result_offset
RETURN
    result_offset
```

Fast executor 的 `i32.add` 不操作动态栈，而是直接：

```
frame[result] = frame[lhs] + frame[rhs];
```

真实实现位于 [wasm_interp_fast.c (line 591)](D:/Github Project/wasm-micro-runtime/core/iwasm/interpreter/wasm_interp_fast.c:591)：

```
SET_OPERAND(I32, 4,
    GET_OPERAND(uint32, I32, 2)
    + GET_OPERAND(uint32, I32, 0));
frame_ip += 6;
```

这里的 `0/2/4` 是三段连续的 `int16 offset` 在内部指令中的字节位置。

**它还会优化 local.set**

例如：

```
i32.add
local.set 2
```

正常做法是：

```
计算到临时 slot
再把临时 slot 复制到 local 2
```

Fast loader 会尝试直接修改前一条指令的输出 offset：

```
I32_ADD lhs, rhs, local2
```

于是 `local.set` 本身也不需要执行。相关 patch 逻辑在 [wasm_loader.c (line 14009)](D:/Github Project/wasm-micro-runtime/core/iwasm/interpreter/wasm_loader.c:14009)。

这说明 Fast Interpreter 不只是换了一种 dispatch，它还做了一些局部 IR 优化。

**加载时采用两遍扫描**

每个函数都会进入 `wasm_loader_prepare_bytecode()`，见 [wasm_loader.c (line 11777)](D:/Github Project/wasm-micro-runtime/core/iwasm/interpreter/wasm_loader.c:11777)。

第一遍：

```
解析 opcode
验证类型栈
计算 slot
计算最大 frame 大小
统计 compiled code 大小
收集常量
记录需要 patch 的分支
```

此时 `p_code_compiled == NULL`，emit 函数只增加计数，不真正写数据。

然后：

- 分配 `code_compiled`
- 去重常量
- 重置 validator/slot allocator 状态
- 从函数开头重新扫描

第二遍才真正写入 handler、immediate、operand offset 和分支地址。分配及重置逻辑在 [wasm_loader.c (line 9543)](D:/Github Project/wasm-micro-runtime/core/iwasm/interpreter/wasm_loader.c:9543)。

**Direct threading**

在 GCC/Clang 支持 labels-as-values 的平台上，每个 handler 是 C 函数内部的一个 label：

```
HANDLE_WASM_OP_I32_ADD:
HANDLE_WASM_OP_I32_LOAD:
HANDLE_WASM_OP_CALL:
```

Fast loader 可以把 handler 地址直接写进 `code_compiled`：

```
wasm_loader_emit_ptr(loader_ctx, handle_table[opcode]);
```

见 [wasm_loader.c (line 9390)](D:/Github Project/wasm-micro-runtime/core/iwasm/interpreter/wasm_loader.c:9390)。

执行完一条指令后，不需要：

```
opcode = *ip++;
goto *handle_table[opcode];
```

而是直接从指令流取下一个 handler：

```
p_label_addr = *(void **)frame_ip;
frame_ip += sizeof(void *);
goto *p_label_addr;
```

见 [wasm_interp_fast.c (line 1437)](D:/Github Project/wasm-micro-runtime/core/iwasm/interpreter/wasm_interp_fast.c:1437)。

在不支持 computed goto 的编译器上，它会回退到 opcode + `switch`。在不适合存放未对齐指针的平台上，也可以存储 32 位相对地址。

**控制流怎么处理**

Fast loader 在加载时已经理解了 `block/loop/if/else/end` 的嵌套结构，因此可以提前生成：

- `if` 的 else 地址
- `if` 的 end 地址
- `br` 的目标地址
- 分支参数的源 slot 和目标 slot
- `br_table` 每一项的目标信息

所以运行 `if` 时，executor 只需要读取条件和目标地址：

```
if (cond == 0)
    frame_ip = else_addr ? else_addr : end_addr;
else
    frame_ip += two_pointer_size;
```

对应 [wasm_interp_fast.c (line 1614)](D:/Github Project/wasm-micro-runtime/core/iwasm/interpreter/wasm_interp_fast.c:1614)。

当 block 参数或返回值需要搬到目标 slot 时，loader 会生成 `EXT_OP_COPY_STACK_VALUES`，运行时按照预先编码的源/目标 offset 批量复制，见 [wasm_interp_fast.c (line 5002)](D:/Github Project/wasm-micro-runtime/core/iwasm/interpreter/wasm_interp_fast.c:5002)。

因此 Fast frame 不需要 Classic 那套动态 `csp` 控制栈。

**函数调用**

Wasm 函数之间调用时，Fast Interpreter 不会为每次 Wasm 调用递归进入新的 C 函数。

它在同一个大执行函数中：

1. 保存当前 frame 的 `ip`。
2. 从 `exec_env->wasm_stack` 分配 callee frame。
3. 根据内部指令携带的 offset 复制参数。
4. 切换 `frame_ip` 和 `frame_lp`。
5. 继续 dispatch。
6. 返回时把结果写入 caller 预先指定的 slot。
7. 释放 callee frame，恢复 caller。

Wasm frame 来自独立的 WAMR stack，不主要依赖 native C stack，分配逻辑在 [wasm_exec_env.h (line 207)](D:/Github Project/wasm-micro-runtime/core/iwasm/common/wasm_exec_env.h:207)。

Imported function 则走 native bridge；`call_indirect` 仍然要在运行时检查 table 边界、空元素和函数类型。

**哪些检查不能提前消除**

加载期验证能消除类型和静态 index 检查，但以下内容仍然是动态的：

- linear memory 越界
- table 越界
- `call_indirect` 类型
- 除零和有符号除法溢出
- 无效浮点到整数转换
- `unreachable`
- Wasm stack overflow
- 原子指令对齐
- thread suspend/terminate

发生错误后设置 module exception，并跳转到统一的 `got_exception` 路径。

**它为什么快**

性能收益不是单点，而是几项叠加：

- 不重复解码 LEB128。
- `local.get` 经常完全消失。
- `local.set` 可以融合进前一条指令。
- 不动态维护 operand stack pointer。
- 不动态维护 block control stack。
- 分支目标提前解析。
- 操作数位置已经类型化并固定。
- direct threading 减少 dispatch 开销。
- 常量可以放进共享常量区，通过 slot 引用。
- `frame_ip`、`frame_lp`、memory、globals 等热点状态缓存在 C 局部变量中。

但它仍然是逐条执行 handler，所以性能上限依然明显低于真正的 JIT/AOT。

**代价和限制**

- 加载时要扫描函数两遍。
- 需要额外保存 `code_compiled` 和常量池。
- handler 指针形式与当前进程相关，不能当成可持久化 AOT 文件。
- 内部指令和原始 Wasm offset 不再一一对应，源码调试更困难。
- 某些 Wasm proposal 的支持晚于 Classic interpreter。
- 文档给出的经验值是大约 2 倍性能，同时用于预编译指令的内存也大约增加 2 倍。

所以它的设计位置很清晰：

```
Classic: 最小加载成本、最接近规范栈机
Fast:    多花加载时间和内存，换取明显更快的解释执行
JIT/AOT: 生成机器码，追求更高峰值性能
```

Fast Interpreter 的精髓可以压缩成一句话：它在加载阶段把“Wasm 操作数栈上的值”变成“frame 中有确定 offset 的值”，再配合预解析分支和 direct threading，把最昂贵的解释器工作从每次执行挪到只执行一次的加载阶段。