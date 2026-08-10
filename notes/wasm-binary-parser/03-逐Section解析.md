# 03. 每个 Section 的解析、数据结构与设计原因

本篇统一使用下面的记号：

- `byte`：单字节。
- `u32/s32/s64`：无符号/有符号 LEB128，除非特别说明。
- `vec(T)`：先读 `count:u32`，再读 count 个 T。
- `name`：`byte_len:u32 + UTF-8 bytes`，不是以 `\0` 结尾。
- `expr`：以显式 `0x0B end` 结束的指令序列。
- `idx`：对应索引空间的无符号 LEB128。

每段最后都要求 parser 游标 `p == p_end`，否则报 `section size mismatch`。

## 0. Custom Section（ID 0）

### 磁盘布局

```text
custom_payload := name content:byte[*]
```

通用 Section envelope 的 `payload_len` 已经限定整个 payload；`name` 之后剩余字节都是自定义内容。

### WAMR 解析

入口是 `load_user_section()`：

1. 读 `name_len`，检查 payload 足够。
2. 用 `wasm_check_utf8_str()` 验证 UTF-8。
3. 按名字执行专用 handler。
4. 若开启 `WASM_ENABLE_LOAD_CUSTOM_SECTION`，建立 `WASMCustomSection` 链表；否则忽略未知内容。

### 已知 custom section

#### `name`

当 `WASM_ENABLE_CUSTOM_NAME_SECTION` 开启时，`handle_name_section()` 读取：

```text
subsection := name_type:u32 subsection_size:u32 subsection_payload
```

当前实现：

- 校验 subsection type 严格递增且不重复。
- 解析 type=1 的 function name map。
- type=0 module name 和 type=2 local names 目前跳过。
- imported function 的名字不写入 `WASMFunction`；本地函数将绝对 funcidx 减去 `import_function_count` 后，写入 `func->field_name`。
- function index 必须递增、不重复、不越界。

`module->name_section_buf/end` 保存整个 custom payload 的位置，便于其他功能再次读取。

#### `metadata.code.branch_hint`

当 `WASM_ENABLE_BRANCH_HINTS` 开启时，`handle_branch_hint_section()` 解析 function hint 数量、绝对 funcidx、每个 hint 的 code offset 和一字节 likely/unlikely 值。它拒绝 imported function、越界 offset、非 0/1 值，并把提示链挂到 `module->function_hints`。

#### 通用保留

`WASMCustomSection` 保存：

```c
next
name_addr/name_len
content_addr/content_len
```

名字和内容都是对原 binary 的借用视图，没有 copy；链表采用头插法，所以遍历顺序与文件出现顺序相反。

### 为什么这样实现

Custom Section 不参与核心索引空间和执行，不值得建立通用 AST。已知名字走专用解析器，未知内容按构建配置“保留切片或零成本忽略”，符合 micro runtime 的可裁剪目标。

## 1. Type Section（ID 1）

### MVP 磁盘布局

```text
type_section := vec(functype)
functype     := 0x60 vec(param_valtype) vec(result_valtype)
```

常见 valtype：

| 类型 | byte |
| --- | ---: |
| i32 | `0x7F` |
| i64 | `0x7E` |
| f32 | `0x7D` |
| f64 | `0x7C` |
| v128 | `0x7B`，需 SIMD |
| funcref | `0x70`，需 ref-types/GC |
| externref | `0x6F`，需 ref-types/GC |

### 内部结构

```text
module->types : WASMType **
                  |
                  +--> WASMFuncType
                       param_count/result_count
                       param_cell_num/ret_cell_num
                       types[param_count + result_count]
```

无 GC 构建中 `WASMType` 就是 `WASMFuncType` 的 typedef。每个 func type 用尾数组一次分配；参数类型在前，结果类型紧跟其后。

WAMR 还计算 cell 数：i32/f32 通常 1 个 32-bit cell，i64/f64 2 个，v128 4 个。interpreter frame 因而不必在调用时再遍历类型换算。

### 无 GC 的 dedup

每创建一个类型，loader 会与之前类型做 `wasm_type_equal()`：若相同，释放新对象，让 `module->types[i]` 指向旧对象并增加 `ref_count`。因此：

- 不同 typeidx 可能指向同一个 `WASMFuncType`。
- unload 不能按指针数组简单 free；`destroy_wasm_type()` 用 ref_count 最后一次才释放。
- typeidx 的名义身份仍存在于索引位置，不能仅用指针地址反推原始 typeidx。

### GC Type Section

GC 开启后，type entry 还支持：

```text
rec group   : 0x4E vec(subtype)
subtype     : [0x50 non-final | 0x4F final] vec(supertypeidx[最多1个]) comptype
comptype    : 0x60 functype-body
            | 0x5F vec(fieldtype)          ; struct
            | 0x5E fieldtype               ; array
fieldtype   : storage_type mutability:byte
storage_type: valtype | 0x78 i8 | 0x77 i16
```

单个不带 `rec` 的定义也按 `rec_count=1` 的递归组处理。`WASMType` 公共头保存：

- `type_flag`：func/struct/array。
- final、parent/root、inherit depth。
- `rec_count/rec_idx/rec_begin_type_idx`。
- `ref_count`。

解析一个 rec group 后才解析 parent 指针和 subtype 验证，这允许组内类型互相引用。等价的整个递归组也可以复用已有对象。

#### `WASMStructType`

两遍解析 fields：第一遍统计 field 数、multi-byte ref 数、GC reference field 数；第二遍一次分配 struct header + fields + reference offset table，并单独分配稀疏 `WASMRefTypeMap`。

每个 field 预计算：

- `field_type/field_flags`
- 当前目标的 size/offset
- compiler/JIT 构建下的 32-bit、64-bit size/offset

`reference_table` 保存需要 GC trace 的字段 offset，首元素是数量。GC 扫描对象时不必逐字段重新判断类型。

#### `WASMArrayType`

只需要一个 element storage type、mutability，以及必要时完整 `WASMRefType *`，所以对象定长，无需尾数组。

#### multi-byte reference type map

普通 `types[]/fields[]/local_types[]` 只存首字节。像 `(ref null 7)` 还需要 nullable + heap type/typeidx，完整信息放入稀疏 `WASMRefTypeMap { index, ref_type* }`。这样绝大多数单字节数值类型不为大 union 付出空间。

### 关键校验

- type count/参数数/结果数/引用 map count 不得溢出结构字段。
- 类型 byte 必须受当前 feature 支持。
- GC supertype 只能指向已处理类型，最多一个，不能继承 final 类型。
- struct/array mutability 必须为 0 或 1。
- subtype 的实际结构必须满足 `wasm_type_is_subtype_of()`。
- Section 所有 type slot 最终都不能为 NULL。

## 2. Import Section（ID 2）

### 磁盘布局

```text
import_section := vec(import)
import := module_name:name field_name:name kind:byte descriptor

kind 0 function: typeidx:u32
kind 1 table:    tabletype
kind 2 memory:   memtype
kind 3 global:   globaltype
kind 4 tag:      tagtype                 ; tags feature
```

### 两遍分组布局

第一遍只跳字段并统计各 kind；第二遍解析。最终：

```text
module->imports
+----------------+-------------+---------------+----------+----------------+
| function imports| table imports| memory imports| tag imports| global imports |
+----------------+-------------+---------------+----------+----------------+
^                ^             ^               ^          ^
import_functions import_tables import_memories import_tags import_globals
```

注意它不再保持文件中的混合 kind 顺序。Wasm 的各索引空间本来就按 kind 独立编号，所以按 kind 连续布局更适合运行时。

### Function import

`load_function_import()`：

- 检查 typeidx 指向 function type。
- 保存直接 `func_type` 指针；GC 下还保存原 `type_idx`。
- 保存 module/field name。
- 若 `no_resolve=false`，调用 `wasm_resolve_import_func()` 查注册的 native symbol，填写 function pointer、signature、attachment、calling convention。
- compiler/JIT 某些配置会把等价类型规范到 smallest typeidx。

### Table import

`tabletype`：

```text
reftype limits
limits := flags:u32 min:u32 [max:u32]
```

WAMR flags：

- bit 0 `0x01`：有 max。
- bit 2 `0x04`：table64。
- bit 1 shared table 被拒绝。

无 GC 时 element 只接受 funcref，ref-types 开启时也接受 externref。GC 下走完整 `resolve_value_type()`，并要求引用类型；import table 不接受 non-nullable ref type。

若没有声明 max，或声明 max 很大，`adjust_table_max_size()` 会设置/收紧运行时 max，默认至少 `WASM_TABLE_MAX_SIZE`，通常参考 `2 * min`。因此内部 max 可能是 WAMR 的实际增长上限，不是对文件字节的原样保存。

### Memory import

```text
memtype := limits
flags bit 0: has max
      bit 1: shared
      bit 2: memory64
```

检查：

- shared 必须有 max，且构建必须开启 shared memory。
- memory64 构建必须开启。
- min <= max。
- wasm32 最大 65536 个 64 KiB page；memory64 当前 WAMR 表示仍把 page count 限在 `uint32` 范围。
- runtime/app framework 可以进一步夹紧 max。

内部 `WASMMemory`/`WASMMemoryType` 保存 `flags/num_bytes_per_page/init_page_count/max_page_count`。`num_bytes_per_page` 初始是 65536，但模块后处理在确认不可能 grow 时可能把多页折成一个“大页”以简化边界检查。

### Global import

```text
globaltype := valtype mutability:byte
```

保存 `WASMGlobalType`、link 状态和 linked value；GC 下另存完整 ref type。它可以解析 libc builtin global 或 multi-module export。

### Tag import

```text
tagtype := attribute:byte typeidx:u32
```

当前 import parser 要求 attribute=0，typeidx 必须为 function type，且 function type 的 result count 必须为 0。参数描述被 throw/catch 携带的 payload。

### 名字的所有权

每个 name 都先验证 UTF-8。若可复用原 file buffer，`wasm_const_str_list_insert()` 原地变成 NUL string；否则复制并在 `module->const_str_list` 中 intern。相同字符串只保留一份。

## 3. Function Section（ID 3）与 Code Section（ID 10）

这两段必须合并理解。

### Function 磁盘布局

```text
function_section := vec(typeidx)
```

它只声明本地函数的签名，没有名字、locals 或 opcode。

### Code 磁盘布局

```text
code_section := vec(code_entry)
code_entry   := body_size:u32 body:byte[body_size]
body         := vec(local_decl) expr
local_decl   := local_count:u32 valtype
```

locals 用 run-length encoding。例如 `02 03 7f 01 7e` 表示两组：3 个 i32、1 个 i64，共 4 个 locals。

### WAMR 合成的 `WASMFunction`

`load_function_section()` 对第 i 项：

- Function[i].typeidx -> `func_type`。
- Code[i].body_size -> function body 边界。
- 展开 local groups -> `local_count + local_types[]`。
- `func->code` 指向 locals vector 后的第一个 opcode。
- `func->code_size` 从第一个 opcode 到 function-ending `0x0B`，包含该 end。
- `local_offsets` 同时包含 params 和 locals 在 32-bit cell frame 中的 offset。

分配布局：

```text
+---------------------+
| WASMFunction        |
+---------------------+
| local_types[0..n)   |
+---------------------+
```

`local_offsets` 另分配，因为元素是 `uint16` 且包含 params，长度/对齐不同。

### 关键校验

- Function count 必须等于 Code count。
- imported + local function count 不得溢出 u32 index space。
- typeidx 必须为 function type。
- body size 非 0 且不能越过 Code payload。
- locals 总数和 cell 数不得溢出。
- expression 最后一字节必须是 `end`。
- 最后一函数 expression 结束位置最终必须等于 Code Section end，防止 body-size/段-size 夹带垃圾。

逐 opcode 的详细验证和 disassembly 见第 5 篇。

### 为什么 `load_code_section()` 看起来很简单

Code entry 已在 `load_function_section()` 中消费。`load_code_section()` 到正式遍历位置时仅复核两个 count。因此“Code parser 只有十几行”不表示 WAMR 不解析 code；解析逻辑被前移并与 Function 合并了。

## 4. Table Section（ID 4）

### 磁盘布局

```text
table_section := vec(table)
table         := [GC init marker] tabletype [init_expr]
tabletype     := reftype limits
```

GC table 可用前导 `0x40 0x00` 表示带 initializer expression。无 marker 时 non-nullable table 会被拒绝，因为没有默认 null 值可填。

### 内部结构

```c
WASMTable {
    WASMTableType table_type;
    InitializerExpression init_expr;  /* GC */
}
```

`WASMTableType` 保存 element type、flags、`possible_grow`、min/max 和可选完整 ref type。tables 是定长连续数组。

### 校验/派生

- 不支持 ref-types/GC 时，imported+defined tables 总数最多 1。
- limits 与 import table 相同。
- init expression 类型必须匹配 element type。
- 当前实现拒绝 table init 中的 struct.new/array.new 等复合 GC initializer。

## 5. Memory Section（ID 5）

### 磁盘布局

```text
memory_section := vec(memtype)
memtype        := flags:u32 min:u32 [max:u32]
```

解析复用 `load_memory()`，规则与 memory import 相同。未开启 multi-memory 时 imported+defined memories 总数最多 1。

### 为什么 memory declaration 不保存实际 bytes

Section 只描述 limits。真正的 linear memory buffer 是 module instance 阶段按 `WASMMemory` 创建的，属于 `WASMMemoryInstance`，不是 parser 阶段的 `WASMModule`。Data Section 也先保留初始化描述，到 instantiate 时才拷入线性内存。

## 6. Tag Section（ID 13，可选）

### 磁盘布局

```text
tag_section := vec(tagtype)
tagtype     := attribute:byte typeidx:u32
```

### 内部结构

```c
WASMTag {
    uint8 attribute;
    uint32 type;
    WASMFuncType *tag_type;
}
```

`module->tags` 是 `WASMTag **`，每个 tag 独立分配。type 必须是无 result 的 function type。

值得注意：当前 `load_tag_import()` 明确要求 attribute=0；`load_tag_section()` 读取并保存 defined tag attribute，但本函数没有同样的 `attribute == 0` 判断。做安全审计或对齐规范测试时应把这类 import/defined 路径差异纳入检查。

## 7. Stringref Literal Section（ID 14，可选）

当前实现读取：

```text
deferred_count:u32
immediate_count:u32
vec(immediate string bytes): 每项 length:u32 bytes[length]
```

- `deferred_count` 必须为 0，作为未来扩展预留。
- 分配 `string_literal_ptrs[]` 和 `string_literal_lengths[]`。
- 每个 ptr 直接指向 Section payload 中的 bytes，不追加 NUL，也不复制内容。

数组由 module 拥有，literal bytes 仍借用 binary。这与普通 import/export name 的 C-string 转换不同；消费端必须始终按 length 处理。

## 8. Global Section（ID 6）

### 磁盘布局

```text
global_section := vec(global)
global         := globaltype init_expr
globaltype     := valtype mutability:byte
```

### 内部结构

```c
WASMGlobal {
    WASMGlobalType type;
    WASMRefType *ref_type;              /* GC */
    InitializerExpression init_expr;
    uint32 data_offset;                 /* fast JIT */
}
```

globals 是连续数组。解析每项后 `module->global_count` 逐个增加，这让 GC constant expression 中的 `global.get` 只能看到已定义的前序 global。

### Initializer expression

`load_init_expr()` 不是简单读取“一个 opcode + end”，而是维护 `ConstExprContext` value/type stack。当前按 feature 支持：

- `i32/i64/f32/f64.const`
- `v128.const`
- `global.get`
- `ref.null/ref.func`
- extended const expr 的 i32/i64 add/sub/mul
- GC 的 struct.new/default、array.new/default/fixed、ref.i31 等部分表达式

遇到二元 extended const op 时，从栈弹出左右操作数并建立 `InitializerExpression` 树。结束时栈必须恰好剩一个值，且类型是目标 global/table/data offset 所要求类型。

`global.get` 的 mutability、可见性和 subtype 会检查。无 GC 路径严格只允许 imported immutable global；GC 路径按当前 proposal/测试需求也能引用已经定义的前序 immutable global，并在 Global parser 中做额外顺序和 subtype 校验。

### 为什么不保存原始 expr bytes

模块实例化时需要直接求初值。把常见 const 折成 `init_expr_type + WASMValue`，把 extended expression 变成小树，可以避免每次实例化重新跑通用 opcode decoder；同时让 data/elem/global 共用一套表示。

## 9. Export Section（ID 7）

### 磁盘布局

```text
export_section := vec(export)
export         := name kind:byte index:u32

kind 0 func
kind 1 table
kind 2 memory
kind 3 global
kind 4 tag     ; feature
```

### 内部结构

```c
WASMExport { char *name; uint8 kind; uint32 index; }
```

exports 是连续数组。index 保留对应绝对索引空间，不转为指针，因为 export lookup 之后可能需要构造不同 instance 对象。

### 校验

- name 是合法 UTF-8，并转成/intern 为 C string。
- kind 合法，index 小于 `import_count_of_kind + local_count_of_kind`。
- 所有 export name 必须唯一。

重复检测把 name 指针复制到 32 项栈上临时数组；更多时才堆分配，然后 `qsort + 相邻 strcmp`，复杂度 O(n log n)，不改变 `module->exports` 原顺序。

## 10. Start Section（ID 8）

### 磁盘布局

```text
start_section := funcidx:u32
```

保存为 `module->start_function`，默认 sentinel 是 `(uint32)-1`。目标可以是 imported 或 defined function，但签名必须 `[] -> []`。

为什么只存 scalar：Start Section 最多一个，本身没有名字或其他属性；实例化时按绝对 funcidx 调用即可。

## 11. Element Section（ID 9）

Element segments 初始化 table。reference-types/bulk-memory 后有 8 种 mode：

| mode | 状态 | table | offset | element type | payload |
| ---: | --- | --- | --- | --- | --- |
| 0 | active | 隐含 0 | expr | 隐含 funcref | `vec(funcidx)` |
| 1 | passive | 无 | 无 | elemkind 0 | `vec(funcidx)` |
| 2 | active | 显式 tableidx | expr | elemkind 0 | `vec(funcidx)` |
| 3 | declarative | 无 | 无 | elemkind 0 | `vec(funcidx)` |
| 4 | active | 隐含 0 | expr | 隐含 funcref | `vec(expr)` |
| 5 | passive | 无 | 无 | reftype | `vec(expr)` |
| 6 | active | 显式 tableidx | expr | reftype | `vec(expr)` |
| 7 | declarative | 无 | 无 | reftype | `vec(expr)` |

WAMR 读取 mode 后只保留低 3 bit：`mode &= 0x07`。

### 内部结构

```c
WASMTableSeg {
    uint32 mode;
    uint32 elem_type;
    WASMRefType *elem_ref_type;       /* GC */
    uint32 table_index;
    InitializerExpression base_offset;
    uint32 value_count;
    InitializerExpression *init_values;
}
```

即使磁盘 payload 是紧凑 `vec(funcidx)`，`load_func_index_vec()` 也把每项规范化为 `InitializerExpression { ref.func, funcidx }`。这样 instantiate/table.init 不必分两套数据结构处理 funcidx form 和 expr form。

### 校验

- active segment 的 tableidx 合法。
- offset 类型取决于 table32/table64；当前内部 table element index 最终仍限制到 u32 范围。
- 每个 funcidx 合法。
- expression value subtype 匹配 segment element type。
- active segment 的 segment element type 与目标 table element type兼容。

## 12. DataCount Section（ID 12，可选）

### 磁盘布局

```text
data_count_section := data_count:u32
```

保存为 `module->data_seg_count1`。Data Section 真正 count 保存为 `data_seg_count`。loader 在读 Data Section 时和所有段完成后各检查一次二者一致。

为什么它位于 Code 前：Code 中的 `memory.init` 和 `data.drop` 必须在验证函数体时检查 dataidx，即使 Data Section 尚在后面。DataCount 提前提供索引空间大小。

## 13. Data Section（ID 11）

Data segment 的 mode 编码：

| flag | 状态 | memory | offset | bytes |
| ---: | --- | --- | --- | --- |
| 0 | active | 隐含 0 | expr | `length:u32 + bytes` |
| 1 | passive | 无 | 无 | `length:u32 + bytes` |
| 2 | active | 显式 memidx | expr | `length:u32 + bytes` |

无 bulk-memory 构建按旧格式把开头直接当 memory index。

### 内部结构

```c
WASMDataSeg {
    uint32 memory_index;
    InitializerExpression base_offset;
    uint32 data_length;
    bool is_passive;
    uint8 *data;
    bool is_data_cloned;
}
```

`module->data_segments` 是指针数组，每项独立分配。active offset 按目标 memory32/64 要求 i32/i64 constant expression。

### payload 借用或复制

- `clone_data_seg=false`：`data` 直接指向 binary，`is_data_cloned=false`。
- `clone_data_seg=true`：单独分配并复制 bytes，unload 时依据 flag 释放。

使用 flag 而不是从地址范围猜所有权，是大型 C 项目很重要的做法：同一字段支持 owned/borrowed 两种状态，析构必须有显式判据。

## 14. Section 间的依赖关系

逐段 parser 看似独立，实际依赖前序索引空间：

```text
Type
  +--> Import(function/tag signature)
  +--> Function(typeidx)
  +--> GC ref heap types

Import + Function ----> Export/Start/Element/ref.func/call
Import + Table -------> Element/table instructions
Import + Memory ------> Data/memory instructions
Import + Global ------> Global/Data/Element constant expressions
DataCount ------------> Code 中 dataidx validation
Function + Code ------> name/branch-hint custom section 的函数元数据
```

这正是非 Custom Section 必须有确定顺序、loader 在 Section 结束后还要做跨段一致性检查的原因。
