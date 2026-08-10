# 02. Wasm Binary 格式与 WAMR 加载总流程

## 1. 模块外壳

标准 Wasm module 从 8 字节固定头开始：

```text
offset  size  value
0x00    4     00 61 73 6d   magic "\0asm"
0x04    4     01 00 00 00   version 1, little-endian u32
0x08    ...   sections
```

WAMR 的 `load()` 用本机 `uint32` 读取头；大端机器调用 `exchange32()`。magic 对应 `WASM_MAGIC_NUMBER`，version 对应 `WASM_CURRENT_VERSION`。成功后保存到 `module->package_version`。

每个 Section 的通用 envelope：

```text
section_id : byte
payload_len: u32 LEB128
payload    : byte[payload_len]
```

注意：`payload_len` 不包含 id 和自身的 LEB 字节。

## 2. Section ID 与 WAMR 顺序

`create_sections()` 不是直接比较数值大小，而是查 `section_ids[]` 得到逻辑顺序：

| ID | 名称 | 核心/feature | WAMR parser |
| ---: | --- | --- | --- |
| 0 | Custom/User | 核心，可出现多次且可插在任何位置 | `load_user_section` |
| 1 | Type | 核心 | `load_type_section` |
| 2 | Import | 核心 | `load_import_section` |
| 3 | Function | 核心 | `load_function_section`，同时消费 Code |
| 4 | Table | 核心 | `load_table_section` |
| 5 | Memory | 核心 | `load_memory_section` |
| 13 | Tag | `WASM_ENABLE_TAGS` | `load_tag_section` |
| 14 | Stringref literals | `WASM_ENABLE_STRINGREF` | `load_stringref_section` |
| 6 | Global | 核心 | `load_global_section` |
| 7 | Export | 核心 | `load_export_section` |
| 8 | Start | 核心 | `load_start_section` |
| 9 | Element | 核心/ref-types | `load_table_segment_section` |
| 12 | DataCount | bulk-memory，逻辑上在 Code 前 | `load_datacount_section` |
| 10 | Code | 核心 | `load_code_section`，主体已被 Function parser 消费 |
| 11 | Data | 核心/bulk-memory | `load_data_segment_section` |

除 Custom 外，每种段最多一次且必须按表中顺序。未知 id 直接报 `invalid section id`。这也意味着：某个 proposal 对应的宏关闭时，该 id 不在 `section_ids[]` 中，会在拆段阶段被拒绝，而不是静默忽略。

## 3. 第一阶段：`create_sections()` 只切片

`WASMSection` 来自公开头文件中的 `wasm_section_t`：

```c
typedef struct wasm_section_t {
    struct wasm_section_t *next;
    int section_type;
    uint8_t *section_body;
    uint32_t section_body_size;
} wasm_section_t;
```

处理一个 section 时：

```text
原 binary
... [id][payload_len LEB][payload................] ...
                         ^                       ^
                         section_body            + body_size
```

节点不复制 payload。加载结束后 `destroy_sections()` 只释放链表节点，绝不释放 `section_body`。

为什么先建链表而不是边拆边解析：

- Function parser 必须提前找到 Code Section。
- DataCount 在验证 Code 中 `memory.init/data.drop` 时需要可用。
- 对外还有 `wasm_runtime_load_from_sections()`，允许 embedder 直接提供 Section 链表。
- 仍然很轻：每段只多一个固定小节点。

它并非真正的流式 parser，因为所有 `section_body` 都必须已经可访问。

## 4. 第二阶段：`load_from_sections()`

函数先遍历链表一次，只寻找 Function/Code 两段的 body 和 end。第二次遍历才按 Section 顺序 dispatch。

关键参数：

```c
is_load_from_file_buf
wasm_binary_freeable
no_resolve
```

它们推导出：

```c
reuse_const_strings = is_load_from_file_buf && !wasm_binary_freeable;
clone_data_seg = is_load_from_file_buf && wasm_binary_freeable;
```

含义：

- binary 不会被释放：字符串和 data 可以尽量借用它，字符串还可能原地改成 C string。
- 调用者声明 binary 希望可释放：data payload 会 clone，字符串进入 module 的 intern list。这是 loader 内部的复制策略，不等于加载成功后必然可立即释放。
- 从外部 Section list 加载：`is_load_from_file_buf=false`，名字会复制/intern；但 `clone_data_seg` 也为 false，function code、data bytes 等 Section payload 仍由调用方保证生命周期。它不是“把 Section list 全部深拷贝进 module”的 API。
- `no_resolve` 允许只建立 import 描述而不立即解析 native symbol。

公开 API 的正确协议是：

```c
LoadArgs args = { 0 };
args.wasm_binary_freeable = true;
module = wasm_runtime_load_ex(buf, size, &args, error, sizeof(error));
if (module && wasm_runtime_is_underlying_binary_freeable(module)) {
    /* 此时才可按调用方的 allocator 释放 buf */
}
```

对 bytecode module，当前查询会在 classic interpreter、某些 lazy JIT 组合，以及 GC string literal 仍借用 binary 时返回 false。因此 flag 表示“请求可释放”，query 才表示“本次构建和此模块确实可释放”。

## 5. Function 与 Code 的“拉链式”合并

磁盘上两段是平行 vector：

```text
Function Section: vec(typeidx) = [t0, t1, t2]
Code Section:     vec(code)    = [b0, b1, b2]
                                     |   |   |
                                     +---+---+ 同序号配对
```

`load_function_section()` 同时持有 `p` 和 `p_code`：

1. 分别读 `func_count` 与 `code_count`，要求相等。
2. 从 Function 读 `type_index`。
3. 从 Code 读 `code_size`，限定单个 body 的 `p_code_end`。
4. 解析 locals declaration。
5. 分配 `WASMFunction + local_types`。
6. 令 `func->code` 指向 locals 后的第一个 opcode；`func->code_size` 只覆盖 expression。
7. 计算 param/local cell offset。
8. 跳到下一个 code entry。

随后走到 Code Section 时，`load_code_section()` 只重新读取 count 并检查与 Function count 一致。真正的 code entry 结构已经在前面拆完。

## 6. 第三阶段：模块级派生信息

所有 Section 解析完成后，`load_from_sections()` 还会：

- 检查 DataCount 与 Data Section count 一致。
- 从 exports/globals 识别 `__data_end`、`__heap_base` 和 auxiliary stack top。
- 识别签名正确的 `malloc/free/__new/__retain/__pin/...`。
- 对每个本地函数调用 `wasm_loader_prepare_bytecode()`。
- 检查最后一个 `func->code + code_size` 恰好到 Code Section 结尾。
- 根据是否可能 `memory.grow` 决定能否缩小/合并初始页表示。
- 检查 memory64 flags 一致性。
- 计算 globals 在 instance global data 中的 byte offset/总大小。
- 按构建配置初始化 fast JIT/LLVM JIT。

所以 loader 同时承担四类职责：container parsing、语义验证、运行时布局准备、执行后端初始化。阅读时应主动区分它们。

## 7. 第四阶段：每个函数的验证与 lowering

`wasm_loader_prepare_bytecode()` 遍历 `func->code`：

```text
原始 opcode stream
       |
       +--> immediate 边界/格式检查
       +--> index/type/mutability/alignment 检查
       +--> operand type stack
       +--> control block stack
       +--> max stack/max block 统计
       +--> classic interp: 某些 opcode 原地改写
       `--> fast interp: 两遍生成 code_compiled 和 const pool
```

Fast interpreter 的第一遍 `p_code_compiled == NULL`：

- 精确验证 LEB 编码。
- 模拟 emit 以计算 compiled size 和峰值 size。
- 收集常量，计算 frame slot。

第二遍：

- 分配 `code_compiled`。
- 可用 quick LEB reader，因为第一遍已验证。
- 真正写入 handler label、定宽 immediate、operand offset 和分支 patch。

最后填写：

- `func->max_stack_cell_num`
- `func->max_block_num`
- fast interp 下的 `code_compiled/code_compiled_size`
- `func->consts/const_cell_num`

## 8. 入口调用链

公开 runtime 路径可以按下面追：

```text
wasm_runtime_load(...)
  -> wasm_runtime_load_ex(...)
     -> wasm_load(...)
        -> wasm_loader_load(...)
           -> create_module(...)
           -> load(...)
              -> header validation
              -> create_sections(...)
              -> load_from_sections(...)
           -> optional WASI ABI check
```

从 Section list 加载：

```text
wasm_runtime_load_from_sections(...)
  -> wasm_load_from_sections(...)
     -> wasm_loader_load_from_sections(...)
        -> create_module(...)
        -> load_from_sections(...)
```

第二条路径没有 module header，因为调用者已经提供拆好的 sections。

## 9. 完整 loader 与 mini loader

构建脚本在二者中只选一个编译：

```cmake
if (WAMR_BUILD_MINI_LOADER EQUAL 1)
    set(LOADER "wasm_mini_loader.c")
else()
    set(LOADER "wasm_loader.c")
endif()
```

mini loader 保留相同公开 API 和大体数据结构，因此运行时其他模块无需知道替换发生了。但官方构建文档明确说明它会跳过 Wasm binary integrity checks，只适合可信输入。这是一种大型 C 项目常见的“同接口、构建期替换实现”策略：

- 不在运行时为 `if (mini)` 付分支成本。
- 链接产物只含一种实现，减小体积。
- 代价是两个大文件存在相似逻辑，新 feature/安全修复必须同步。

安全边界上应把完整 loader 当作不可信输入入口；mini loader 不是它的轻量安全等价物。

## 10. 错误边界

WAMR 主要防护层次：

1. `check_buf()`：指针加法溢出和越过当前 Section/function end。
2. `read_leb()`/`bh_leb_read()`：最大字节数、最高位溢出、意外 EOF。
3. `loader_malloc(uint64 size)`：大于等于 `UINT32_MAX` 拒绝。
4. `is_indices_overflow(import, local)`：组合索引空间的加法溢出。
5. 每段最后 `p == p_end`：声明 size 与实际消费必须完全一致。
6. 每个 function body 最后一字节必须是 `end`，且控制栈最终为空。

快速跳过宏 `skip_leb()` 自身不做边界检查；它只能用于已经在前一验证阶段确认合法的内部扫描路径。写自己的 dump/disassembler 时不能照搬成不安全的输入解析器，应调用有 `p_end` 的 checked decoder。
