# 05. Function Body 解码、验证与反汇编

## 1. 先定位 function body

Code Section payload：

```text
code_count:u32
repeat code_count times:
    body_size:u32
    body_start --------------------+
      local_group_count:u32         | body_size bytes
      local groups...               |
      opcode expression...          |
      0x0B end ---------------------+
```

必须记录四个位置：

```text
entry_size_pos  body_start  expression_start        body_end
      |             |              |                    |
      v             v              v                    v
... [body_size LEB][locals vector][first opcode ... end]
```

- `body_size` 从 `body_start` 算起，包含 locals vector 和最终 `end`。
- WAMR 的 `func->code` 是 `expression_start`，不包含 locals。
- WAMR 的 `func->code_size = body_end - expression_start`，包含 function-ending `end`。
- `wasm-objdump` 的 `func[n]` header offset 常指 body/locals 附近，第一条指令另有 offset；不要混为一处。

## 2. Function index 与 Code entry index

Code Section 只包含本地函数。第 i 个 code entry 对应：

```text
local function array index = i
absolute Wasm funcidx      = import_function_count + i
signature                  = FunctionSection.typeidx[i]
```

反汇编器要打印正确名字和 call target，必须先解析 Type、Import、Function、Export/name custom section。只扫 Code bytes 可以打印 opcode，却无法完整解释签名和绝对 funcidx。

## 3. locals declaration 的正确展开

```text
local_group_count:u32
repeat:
    repeat_count:u32
    valtype
```

例如：

```text
02 03 7f 01 7e
```

解码：

```text
02       2 groups
03 7f    3 x i32
01 7e    1 x i64
```

逻辑 local index 先放 function params，再放这些显式 locals：

```text
param 0, param 1, ..., local 0, local 1, ...
```

安全检查：

- repeat count 累加不能溢出 u32。
- 展开总数不能超过 runtime 表示能力。
- valtype 必须受当前 feature 支持。
- group count 可以为 0，repeat count 也允许为 0。
- locals 后至少还要有一个 `0x0B`。

## 4. 一个可靠 disassembler 的主循环

伪 C 代码：

```c
while (p < body_end) {
    const uint8_t *op_pos = p;
    uint8_t op = read_u8_checked(&p, body_end);

    print_offset(op_pos - module_start);
    print_mnemonic(op);

    switch (op) {
        case WASM_OP_I32_CONST:
            print_s32(read_sleb_checked(&p, body_end, 32));
            break;
        case WASM_OP_CALL:
            print_funcidx(read_uleb_checked(&p, body_end, 32));
            break;
        case WASM_OP_BLOCK:
            print_blocktype(read_blocktype(&p, body_end));
            push_control(...);
            break;
        case WASM_OP_END:
            pop_control(...);
            if (control_stack_empty())
                require(p == body_end);
            break;
        case WASM_OP_MISC_PREFIX:
            decode_fc(read_uleb_checked(&p, body_end, 32), ...);
            break;
        /* 其他 family */
    }

    print_raw_bytes(op_pos, p);
}
```

核心原则：

1. 所有读取都接收 `body_end`，不能用不检查边界的 `skip_leb()` 处理不可信文件。
2. prefix 后的 subopcode 是 `u32 LEB`，不是固定一个 byte。
3. 每条指令先保存 `op_pos`，消费完 immediate 后才能打印完整 raw bytes。
4. 结构化控制必须维护 control stack，不能看到第一个 `end` 就结束函数。
5. function 隐含一个最外层 control frame；只有它被最终 `end` 弹出时 function 才结束。

## 5. LEB128 解码细节

每个 byte：

```text
bit 7    continuation，1 表示后面还有
bit 0-6  当前 7 位 payload
```

无符号：

```text
result |= (byte & 0x7f) << shift
shift += 7
```

有符号：最后 byte 的 payload bit 6 是 sign bit，需要符号扩展。

WAMR 的 `bh_leb_read()` 额外检查：

- 编码字节数不超过 `ceil(maxbits/7)`。
- 每次访问不越过 end，且 pointer addition 不溢出。
- u32 最后一字节高位不能表示超过 32 bit 的值。
- s32/s64 顶部多余位必须是合法符号扩展。

反汇编器不应只“读到 continuation=0 就算成功”；过长但数值碰巧可截断的编码也应报告 malformed。

## 6. Block type 不是普通 u32

`block/loop/if/try` 后是 `blocktype`：

```text
0x40             empty -> empty
single valtype   empty -> [valtype]
signed LEB typeidx  TypeSection 中的 function type，支持多参数/多结果
```

WAMR 先看第一个 byte 是否是 `0x40` 或合法 value type；不是就把游标退回，再按 signed LEB32 读 type index。GC multi-byte reference type 还要继续读取 heap type。

为什么 typeidx 用 signed 编码：单字节负数空间与 value type byte 共存，是 Wasm blocktype grammar 的设计。把它一律用 unsigned LEB 解码会误解 `0x7F` 等类型码。

## 7. 核心 opcode immediate 总表

### Control

| 指令 | immediate | 反汇编/验证要点 |
| --- | --- | --- |
| `unreachable`, `nop`, `else`, `end`, `return` | 无 | `else/end` 改变 control stack；unreachable 进入 polymorphic stack |
| `block`, `loop`, `if` | blocktype | `if` 先消费 i32 condition |
| `br`, `br_if` | labelidx:u32 | labelidx 是相对 control depth，不是 code offset |
| `br_table` | count:u32 + count 个 labelidx + default labelidx | 总目标数是 count+1；所有 target signature 必须兼容 |
| `call`, `return_call` | funcidx:u32 | 从 import/local 合并索引空间取签名 |
| `call_indirect`, `return_call_indirect` | typeidx:u32 + tableidx:u32 | 老配置要求 tableidx 编码为 0；目标 table 必须是 function reference table |
| `call_ref`, `return_call_ref` | typeidx:u32 | GC/function-references feature |

### Exception handling/tags

| 指令 | immediate |
| --- | --- |
| `try` | blocktype |
| `catch` | tagidx:u32 |
| `catch_all` | 无 |
| `throw` | tagidx:u32 |
| `rethrow` | labelidx/depth:u32 |
| `delegate` | labelidx/depth:u32，同时结束当前 try |

### Parametric/variable/reference

| 指令 | immediate |
| --- | --- |
| `drop`, untyped `select` | 无 |
| typed `select` | `vec(valtype)`；当前 WAMR 要求长度恰好 1 |
| `local.get/set/tee` | localidx:u32 |
| `global.get/set` | globalidx:u32 |
| `table.get/set` | tableidx:u32 |
| `ref.null` | heaptype:signed LEB |
| `ref.is_null`, `ref.eq`, `ref.as_non_null` | 无 |
| `ref.func` | funcidx:u32 |
| `br_on_null`, `br_on_non_null` | labelidx:u32 |

### Memory

所有 core load/store（`0x28..0x3E`）带 `memarg`：

```text
align:u32 offset:u32-or-u64
```

- `align` 是以 2 为底的指数，不是字节数。`i32.load align=2` 表示 `2^2=4` 字节自然对齐。
- WAMR 检查 align 不得大于该操作自然对齐。
- offset 在 memory64 下按目标 memory 的地址宽度读取。
- multi-memory proposal 可在 align 编码的 flag 中带可选 memidx；必须跟当前源码 feature 规则一致。

`memory.size/memory.grow` 后是 memidx。无 multi-memory 时它是保留的 0；不要把它误认为没有 immediate。

### Constants

| 指令 | immediate | 字节序/编码 |
| --- | --- | --- |
| `i32.const` | s32 | signed LEB128 |
| `i64.const` | s64 | signed LEB128 |
| `f32.const` | 4 bytes | IEEE-754 raw little-endian，不是 LEB |
| `f64.const` | 8 bytes | IEEE-754 raw little-endian，不是 LEB |

### 无 immediate 的数值指令

比较、整数/浮点一元二元运算、conversion、reinterpret、sign-extension 指令本身都没有 immediate。它们的输入/输出类型由 opcode 固定决定。

## 8. `0xFC` Misc/Bulk-memory prefix

编码：

```text
0xFC subopcode:u32 additional_immediates
```

| subopcode | 指令 | additional immediate |
| ---: | --- | --- |
| 0..7 | saturating trunc conversions | 无 |
| 8 | `memory.init` | dataidx:u32, memidx:u32 |
| 9 | `data.drop` | dataidx:u32 |
| 10 | `memory.copy` | dst_memidx:u32, src_memidx:u32 |
| 11 | `memory.fill` | memidx:u32 |
| 12 | `table.init` | elemidx:u32, tableidx:u32 |
| 13 | `elem.drop` | elemidx:u32 |
| 14 | `table.copy` | dst_tableidx:u32, src_tableidx:u32 |
| 15 | `table.grow` | tableidx:u32 |
| 16 | `table.size` | tableidx:u32 |
| 17 | `table.fill` | tableidx:u32 |

WAMR 不只检查 index，还检查：

- `memory.init/data.drop` 要求 DataCount 存在/索引合法。
- source/destination memory address width决定栈上 index 类型。
- `table.init/copy` 的 element reference subtype 必须兼容。
- grow 会标记对应 table `possible_grow=true`。

## 9. `0xFD` SIMD prefix

编码同样是 `0xFD + subopcode:u32`。256 个 SIMD subopcode 大部分没有 immediate；有 immediate 的 family：

| family | immediate |
| --- | --- |
| v128 load/store、load-splat、load-extend、load-zero | memarg |
| `v128.const` | 16 raw bytes |
| `i8x16.shuffle` | 16 个 lane bytes，每个必须 < 32 |
| extract/replace lane | 1 lane byte，范围取决于 16/8/4/2 lanes |
| load/store lane | memarg + 1 lane byte |

WAMR 按 opcode 检查自然对齐和 lane 上界。一个通用反汇编器必须有 subopcode metadata 表；不能假设所有 SIMD 指令都没有 immediate。

## 10. `0xFE` Atomic/threads prefix

```text
0xFE subopcode:u32 [memarg | reserved-zero]
```

- 除 `atomic.fence` 外，所有当前 atomic 指令都有 memarg。
- `atomic.fence` 后有保留字节 `0x00`。
- atomic align 必须等于自然对齐，不只是“不大于”。
- subopcode 决定 stack effect：notify/wait、load/store、RMW、cmpxchg。

即使只做反汇编不做类型验证，也必须识别 fence 的特殊 immediate，否则后续 opcode 会错位。

## 11. `0xFB` GC/Stringref prefix

常见 GC subopcode 的 additional immediate：

| 指令 family | immediate |
| --- | --- |
| `struct.new`, `struct.new_default` | typeidx |
| `struct.get/get_s/get_u/set` | typeidx, fieldidx |
| `array.new/new_default/get/get_s/get_u/set/fill` | typeidx |
| `array.len` | 无 |
| `array.copy` | dst_typeidx, src_typeidx |
| `array.new_fixed` | typeidx, element_count |
| `array.new_data` | typeidx, dataidx |
| `array.new_elem` | typeidx, elemidx |
| `ref.test/test_null/cast/cast_null` | heaptype:signed LEB |
| `br_on_cast/br_on_cast_fail` | cast_flags:byte, labelidx, source_heaptype, target_heaptype |
| `any.convert_extern`, `extern.convert_any`, `ref.i31`, `i31.get_s/u` | 无 |

Stringref 中：

- `string.new_*`/`string.encode_*` 的 linear-memory 版本带 memidx。
- `string.const` 带 literal index（当前 WAMR 以 signed LEB 读取）。
- stringview 的 memory encode 操作带 memidx。
- measure/concat/eq/view/iter 和 array-based 版本多数无 immediate。

这里应直接以 [`WASMGCEXTOpcode`](../../core/iwasm/interpreter/wasm_opcode.h) 和 `wasm_loader_find_block_addr()`/`wasm_loader_prepare_bytecode()` 的当前 switch 为准，因为 proposal 演进可能改变 subopcode 表。

## 12. 结构化控制与 label depth

Wasm binary 不编码绝对 branch address。`br 0` 指向最内层 label，`br 1` 指向它的父 label。

Control stack 示例：

```text
function
  block A
    loop B
      if C
        br 0  -> C 的 end
        br 1  -> B 的 loop header
        br 2  -> A 的 end
```

特殊点：

- branch 到 `loop` 携带 loop 的参数，并跳到 loop 开头。
- branch 到 `block/if/function` 携带其结果，并跳到 end/return。
- `br_if` 在不跳转路径要保留 branch operands。
- `br_table` 所有目标的 arity/type 必须相容。

一个增强型 disassembler 可以在打印 `br depth` 的同时，用 control stack 注释解析出的目标 kind/start offset；普通线性 decoder 至少要正确跳过 immediate。

## 13. WAMR 的 operand type stack

`frame_ref` 模拟验证规范中的 operand stack。宏如：

```text
PUSH_I32()
POP_I64()
POP_AND_PUSH(input, output)
POP2_AND_PUSH(type1, type2)
```

每条 opcode 做三步：

1. 解码 immediate。
2. 查 module 元数据并验证 index/属性。
3. 按 opcode signature pop/push 类型。

例如：

```text
local.get 0   查 local type T       push T
i32.add       pop i32, pop i32      push i32
global.set g  检查 mutable/type T   pop T
i32.load      检查 memory/memarg    pop address, push i32
call f        pop params(reverse)    push results(forward)
```

GC multi-byte ref type 由并行 `frame_reftype_map` 补足，subtype 检查不能只比较首字节。

## 14. Polymorphic stack（unreachable 后为什么还能 pop）

`unreachable`、无条件 `br`、`br_table`、`return` 后，当前 block 在到达结构末尾前没有可达路径。规范允许这段代码在 block base height 处 pop 任意类型，避免对死代码产生伪 stack-underflow。

WAMR 在 `BranchBlock.is_stack_polymorphic` 记录该状态：

- 清栈回到 block entry height。
- 若栈已空且处于 polymorphic state，POP 可以得到 `VALUE_TYPE_ANY` 而不继续降低 height。
- 到 `else/end/catch` 时按 block signature 恢复正常状态。

自己写 validator 时若没有这条规则，会错误拒绝合法的 unreachable code；只做 disassembler 则无需模拟类型，但仍需维护 block nesting。

## 15. `BranchBlock` 与 block signature

每个 control frame 保存：

```text
label_type: function/block/loop/if/try/catch...
block_type: single value/void 或 WASMFuncType*
start_addr/else_addr/end_addr
entry stack height
polymorphic flag
```

WAMR 在进入 block 时：

- 解析 blocktype。
- 从 parent operand stack 弹出 block params。
- push control frame。
- 再把 params 作为 block 内初始 operands push 回去。

到 `else/end` 时验证 result arity/type与 frame signature一致。`wasm_loader_check_br()` 根据 depth 找 target，并区分 loop params 和其他 block results。

## 16. `wasm_loader_find_block_addr()` 与反汇编的关系

Classic interpreter 执行 block/if/loop 时需要找到 else/end。`wasm_loader_find_block_addr()` 从 block body 线性扫描：

- 维护 nesting depth。
- 对每个 opcode正确跳过 immediate。
- 找到同层 else/end。
- 用 `BlockAddr` 小 cache 保存结果。

这个函数是编写 disassembler immediate decoder 的极好索引，因为它必须知道“每条指令占多少 bytes”。但它不是不可信输入的独立 parser：

- 许多路径使用 `skip_leb()` quick skip。
- 假定 `wasm_loader_prepare_bytecode()` 已经验证/可能改写过 code。
- classic 模式下还识别 WAMR internal extended opcodes。

因此可以借它的 opcode 分类，不能原封不动作为文件 disassembler。

## 17. Classic interpreter 的原地预处理

`WASM_ENABLE_FAST_INTERP=0` 时，prepare 仍可能修改 `func->code` 所指 binary：

- 根据宽度把 `drop/select/global get/set` 换成内部 64/128-bit opcode。
- 把带 typeidx 的 block 换成 `EXT_OP_BLOCK/LOOP/IF`。
- 将某些 local access 替换为 fast local opcode/定宽 offset。
- 把多字节 ref type 已消费部分填成 NOP。
- 为 `br_table` 建 cache 或改写表示。

开启 debug interpreter 时 `record_fast_op()` 记录原 opcode 与相对 module offset，以便断点/源码调试恢复。

结论：加载后从内存 dump `func->code`，可能看到的是 classic internal bytecode；文件反汇编要用原始文件副本。

## 18. Fast interpreter 的两遍 lowering

### 第一遍：validate + size/count

`p_code_compiled == NULL`：

- checked LEB decoder 验证格式。
- emit helper 只累计 `code_compiled_size/peak_size`。
- 模拟 operand type stack 和 frame offset stack。
- 收集 i32/i64/v128 constants。
- 计算控制栈和动态 operand slot 峰值。

### 重新初始化

分配 `code_compiled_peak_size`，重置各临时栈。常量排序去重，确定 const pool slot。

### 第二遍：真正 emit

- 使用 quick LEB decoder，因为第一遍已经确认原 bytes 合法。
- opcode 可 emit 为 handler pointer、相对 handler offset、32-bit label address，或普通 internal opcode，取决于 labels-as-values、目标位数和 unaligned access 能力。
- LEB immediate 改成 WAMR 需要的固定宽度。
- 每个 operand 附带 `int16` frame slot offset。
- branch target 不能立即确定时挂 `BranchBlockPatch`，到 else/end 回填。
- 常量指令引用 dedup const pool。

### 为什么两遍而不不断 realloc output

第一遍同时承担必需的验证，顺便得到精确输出大小；第二遍只分配一次且走 quick decode。它用一次额外线性扫描换取确定内存、较小代码和执行期零 LEB 解码。

## 19. 手工反汇编 worked example

仓库的 `understand-build/smoke-test.wasm` Code Section raw bytes：

```text
01 03 00 01 0b
```

逐字节：

```text
01       code_count = 1
03       body_size = 3
00       local_group_count = 0
01       nop
0b       end
```

文件 offset 对照：

```text
0x2f  01       Code payload: count
0x30  03       entry body_size
0x31  00       locals vector
0x32  01       first opcode; WAMR func->code
0x33  0b       function end
```

因此 WAMR 对该函数得到：

```text
local_count = 0
code        = module_buffer + 0x32
code_size   = 2
```

## 20. 更完整的 immediate 示例

`complex_sections_module.wasm` 中 `init_globals` entry：

```text
12                body_size = 18
00                local groups = 0
41 e4 00          i32.const 100
24 00             global.set 0
44 6e 86 1b f0 f9 21 09 40
                  f64.const raw little-endian = 3.14159
24 01             global.set 1
0b                end
```

`e4 00` 是合法 signed LEB32：低 7 位先给 `0x64`，最后 byte 完成正数符号约束。f64 的 8 bytes 必须整体读取，不能逐 byte 当 opcode。

同文件的 memory operations：

```text
36 02 00          i32.store align=2 offset=0
28 02 00          i32.load  align=2 offset=0
```

这里 `02` 表示 `2^2=4` 字节对齐，第二个 `00` 是 offset LEB。

## 21. 反汇编结果应区分三种 offset

| offset | 定义 | 用途 |
| --- | --- | --- |
| file absolute | `op_ptr - module_file_start` | 与 `xxd/wasm-objdump/DWARF` 对照 |
| Code payload relative | `op_ptr - code_section_body` | Section 内分析 |
| function relative | `op_ptr - func->code` | WAMR frame/call stack 中常见 |

Fast interpreter 的 instruction pointer 可能指向 `code_compiled`，其 function-relative offset不再等于原 Wasm byte offset。仓库 `test-tools/addr2line/README.md` 也明确区分 classic 与 fast-interp 的地址语义。

## 22. 一个 disassembler 的分层实现建议

不要写成一个巨型 switch。建议 C 结构：

```text
Reader
  read_u8/read_uleb/read_sleb/read_f32/read_f64/read_name

ModuleIndex
  types/import counts/function typeidx/name maps/section ranges

OpcodeInfo
  mnemonic/prefix/immediate_kind/fixed stack signature

ControlFrame
  kind/blocktype/start/else/end/entry height

Disassembler
  decode_one() -> {start,end,opcode,immediates}
  format_one()
  optional validate_stack()
```

把“消费多少 bytes”和“怎样格式化/验证”分开，prefix/feature 扩展时更不容易让游标错位。WAMR 为减体积把这些职责融合在 loader switch 中；工具代码的目标不同，可以更模块化。

## 23. 常见错误清单

- 把 Code entry index 当绝对 funcidx，忘了 imported functions。
- 把 body_size 误认为只计算 opcode，不包含 locals。
- 把 locals group count 当 local count。
- 把 f32/f64 bytes 当 LEB 或 opcode。
- 把 align 当 byte 数，而不是 log2 exponent。
- 认为 `memory.size/grow` 无 immediate，漏掉 reserved memidx。
- 认为 prefix subopcode 固定一 byte，实际是 u32 LEB。
- `br_table` 少读默认 label。
- 看见内层 `end` 就结束 function。
- blocktype 一律按 u32，误读单字节 valtype。
- 对 unreachable 后死代码执行普通 stack-underflow 检查。
- 从已经被 classic loader 改写的 `func->code` 还原原文件。
- 把 `code_compiled` 当标准 Wasm opcode stream。
