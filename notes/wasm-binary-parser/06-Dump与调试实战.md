# 06. 如何 Dump Wasm Binary 与调试 WAMR Loader

“dump wasm”至少有五种不同需求。先选目标，否则工具输出会答非所问。

| 目标 | 推荐工具 | 输出 |
| --- | --- | --- |
| 看原始每个 byte | `xxd`/`hexdump` | 文件 offset + hex + ASCII |
| 看 Section 范围和结构 | `wasm-objdump -h/-x` | header、count、index、signature |
| 看每段 raw payload | `wasm-objdump -s` | 按 Section 分组的 hex |
| 反汇编 function body | `wasm-objdump -d` | opcode、immediate、offset、名字 |
| 转成可读/可重编译 WAT | `wasm2wat` | WebAssembly text format |
| 嵌入 C 工程 | WAMR `binarydump` | `unsigned char[]`，不是结构解析 |
| 看 WAMR 内部对象/lowering | verbose log + LLDB/GDB | `WASMModule/WASMFunction/code_compiled` |

## 1. 原始 hex dump

```bash
xxd -g 1 module.wasm
```

- `-g 1` 强制每 byte 分组，最适合对应 opcode/LEB。
- 左列是文件绝对 offset。
- 中间是 bytes，右侧是 ASCII 视图。

只看一个范围：

```bash
xxd -g 1 -s 0x2f -l 0x20 module.wasm
```

macOS 自带 `hexdump` 的等价用法：

```bash
hexdump -C module.wasm
```

hex dump 完全不理解 Section/LEB；优点是它是“文件事实”，不会隐藏编码细节。

## 2. `wasm-objdump` 的四个核心视图

假设：

```bash
WASM_OBJDUMP=/Users/ping/PL/wabt-1.0.29/bin/wasm-objdump
```

实际环境也可以直接用 PATH 中的 `wasm-objdump`。

### Section headers

```bash
"$WASM_OBJDUMP" -h module.wasm
```

输出各段 payload 的 start/end/size/count。它是定位 `xxd -s/-l` 范围的第一步。

### 解析后的 Section details

```bash
"$WASM_OBJDUMP" -x module.wasm
```

可以看到：

- Type signature。
- imports/exports。
- function index 到 type/name。
- table/memory/global limits。
- element/data initializers。
- Code body size。

只看一段：

```bash
"$WASM_OBJDUMP" -x -j Import module.wasm
"$WASM_OBJDUMP" -x -j Code module.wasm
"$WASM_OBJDUMP" -x -j name module.wasm
```

Section 名字大小写以 `-h` 输出为准。

### Section raw contents

```bash
"$WASM_OBJDUMP" -s module.wasm
"$WASM_OBJDUMP" -s -j Code module.wasm
```

比 `xxd` 更适合逐段研究，因为它去掉 envelope 后按 Section 标题组织 payload，但 offset 仍是文件 absolute offset。

### Code disassembly

```bash
"$WASM_OBJDUMP" -d module.wasm
```

输出格式：

```text
file_offset: raw bytes | mnemonic immediates
```

需要 Section-relative offset 时：

```bash
"$WASM_OBJDUMP" -d --section-offsets module.wasm
```

分析 relocation 的 object Wasm 可加 `-r`。

### 最实用的一组命令

```bash
"$WASM_OBJDUMP" -h module.wasm
"$WASM_OBJDUMP" -x module.wasm
"$WASM_OBJDUMP" -s -j Code module.wasm
"$WASM_OBJDUMP" -d module.wasm
```

依次回答“Code 在哪、函数是谁、原 bytes 是什么、指令怎么解释”。

## 3. `wasm2wat`

```bash
WASM2WAT=/Users/ping/PL/wabt-1.0.29/bin/wasm2wat
"$WASM2WAT" --enable-all --generate-names module.wasm -o module.wat
```

适合：

- 阅读结构化 control flow。
- 看 type/import/export/elem/data 的文本语义。
- 修改后用 `wat2wasm` 重建测试输入。

不适合：

- 判断 LEB 编码占几 byte。
- 保留原 Section 顺序/custom bytes。
- 查看精确 file offset。
- 观察同一语义的非规范化编码。

`wasm2wat` 是语义反编译，不是 raw dump。

## 4. 用本仓库 smoke test 逐层对照

输入：`understand-build/smoke-test.wasm`。

### Header view

```bash
/Users/ping/PL/wabt-1.0.29/bin/wasm-objdump -h \
  understand-build/smoke-test.wasm
```

关键输出：

```text
Type     start=0x0a end=0x0e size=4
Function start=0x10 end=0x12 size=2
Memory   start=0x14 end=0x18 size=4
Export   start=0x1a end=0x2d size=19
Code     start=0x2f end=0x34 size=5
```

### Raw file

```text
00000000: 00 61 73 6d 01 00 00 00 01 04 01 60 00 00 03 02
00000010: 01 00 05 04 01 01 01 02 07 13 02 06 6d 65 6d 6f
00000020: 72 79 02 00 06 5f 73 74 61 72 74 00 00 0a 05 01
00000030: 03 00 01 0b
```

手工标注：

```text
00 61 73 6d 01 00 00 00     magic/version

01 04                        Type id=1, payload size=4
01 60 00 00                  1 type: func [] -> []

03 02                        Function id=3, payload size=2
01 00                        1 local function, typeidx=0

05 04                        Memory id=5, payload size=4
01 01 01 02                  1 memory, flags=max, min=1, max=2

07 13 ...                    Export payload

0a 05                        Code id=10, payload size=5
01 03 00 01 0b               1 body, size=3, no locals, nop, end
```

### Disassembly

```text
000031 func[0] <_start>:
 000032: 01 | nop
 000033: 0b | end
```

把三种输出放在一起，能确认 `func->code` 应从 file offset `0x32` 开始，而不是 Code payload `0x2f` 或 body size `0x30`。

## 5. 用 WAT 构造最小实验

```bash
WAT2WASM=/Users/ping/PL/wabt-1.0.29/bin/wat2wasm

"$WAT2WASM" input.wat -o input.wasm
/Users/ping/PL/wabt-1.0.29/bin/wasm-objdump -h -x -s -d input.wasm
```

带 proposal：

```bash
"$WAT2WASM" --enable-all input.wat -o input.wasm
/Users/ping/PL/wabt-1.0.29/bin/wasm-objdump -h -x -s -d input.wasm
```

研究 parser 时每次只加入一个特征，例如：

1. 一个 function/type。
2. 一个 import。
3. locals group。
4. 一个 block/br。
5. 一个 active/passive segment。
6. 一个 prefix opcode。

这样 raw byte 差异很小，最容易映射到 loader case。

## 6. WAMR 的 `binarydump` 到底做什么

源码在 `test-tools/binarydump-tool/binarydump.c`。构建：

```bash
cmake -S test-tools/binarydump-tool -B /tmp/wamr-binarydump-build
cmake --build /tmp/wamr-binarydump-build
```

使用：

```bash
/tmp/wamr-binarydump-build/binarydump \
  -o test_wasm.h \
  -n wasm_test_file \
  input.wasm
```

输出近似：

```c
unsigned char __aligned(4) wasm_test_file[] = {
  0x00, 0x61, 0x73, 0x6D, ...
};
```

它只是“binary -> C byte array”，用于固件/静态链接嵌入；没有解析 Section，也没有 disassemble opcode。名字容易让人误以为是 objdump，应明确区分。

## 7. WAMR verbose loader 日志

完整 loader 在每段成功、类型/GC 解析、内存派生等位置有 `LOG_VERBOSE`。构建含日志后运行：

```bash
iwasm -v=5 module.wasm
```

`-v=5` 会显示 level 4 的 verbose log。典型信息：

```text
Load type section success.
Load import section success.
Load function section success.
...
Found aux __heap_base global ...
Load module success.
```

优点：观察 WAMR 实际走到哪一段、在哪段失败。限制：默认日志不是完整 dump，不打印每个 MVP type/import/opcode。

## 8. `TRACE_WASM_LOADER`

`wasm_loader.c` 顶部：

```c
#ifndef TRACE_WASM_LOADER
#define TRACE_WASM_LOADER 0
#endif
```

开发构建可定义 `TRACE_WASM_LOADER=1`。当前 trace 主要调用 GC type dump helpers，展示 function/struct/array/reference type；它仍不是通用 Section dumper。

使用时先搜当前覆盖范围：

```bash
rg -n 'TRACE_WASM_LOADER' core/iwasm/interpreter/wasm_loader.c
```

不要因为打开 trace 后没打印某段，就认为该段没解析。

## 9. `WASM_DEBUG_PREPROCESSOR`

Fast interpreter 下 `LOG_OP` 可打印 emit 的 internal opcode/operand：

```c
#if WASM_DEBUG_PREPROCESSOR != 0
#define LOG_OP(...) os_printf(__VA_ARGS__)
#endif
```

当前 `core/config.h` 在 fast-interp 分支把 `WASM_DEBUG_PREPROCESSOR` 设为 0。要研究 `code_compiled`，开发分支中可临时打开并重建，然后加载最小 `.wasm`。

输出代表 WAMR internal lowering，不等于 `wasm-objdump -d`。调试结束应还原，以免正式运行产生大量日志和代码体积。

## 10. LLDB/GDB 看 `WASMModule`

建议使用未优化、带 debug symbols 的构建。在 macOS LLDB 中：

```text
(lldb) breakpoint set --name load_from_sections
(lldb) breakpoint set --name wasm_loader_prepare_bytecode
(lldb) run -v=5 module.wasm
```

常用查看：

```text
(lldb) frame variable *module
(lldb) p module->type_count
(lldb) p module->import_function_count
(lldb) p module->function_count
(lldb) p *module->functions[0]
(lldb) memory read --format x --size 1 --count 32 module->functions[0]->code
```

断点时机决定看到哪种字节：

- `load_function_section()` 刚设置 `func->code` 后：接近原标准 opcode。
- `wasm_loader_prepare_bytecode()` 前：尚未处理当前函数。
- prepare 返回后：classic 模式可能已原地改写；fast 模式多了 `code_compiled`。

若 optimized build 中 static function 被 inline/删除，改用源码行断点或 Debug CMake 配置。

## 11. 同时保存“文件 dump”和“内存 dump”

研究改写时使用双证据：

```text
原始 input.wasm
  -> xxd/wasm-objdump -d       标准 Wasm 事实

WAMR 加载中/加载后内存
  -> debugger/LOG_OP           WAMR internal representation
```

然后按 file offset/function-relative offset 建映射。不要只保留加载后的内存，因为 loader 可能：

- 原地移动 name bytes 并加 `\0`。
- 改写 classic opcode。
- 用 NOP 覆盖 multi-byte type 的尾部。
- 生成完全不同格式的 `code_compiled`。

## 12. 验证 malformed input

反汇编成功不等于 WAMR 接受，WAMR 接受也受 feature build 影响。建议三方对照：

```bash
wasm-validate --enable-all module.wasm
wasm-objdump -h -x -d module.wasm
iwasm -v=5 module.wasm
```

若结果不同，按顺序检查：

1. iwasm 是否开启对应 proposal。
2. mini loader 是否被启用。
3. WABT/WAMR proposal 版本是否一致。
4. 错误发生于 container、Section、function validation 还是 import resolution。
5. 输入 buffer 是否被提前释放/设为只读但 loader 尝试原地改字符串/bytecode；释放前是否真正检查了 `wasm_runtime_is_underlying_binary_freeable()`。

## 13. 一套可重复的 parser 调试记录模板

每个 case 记录：

```text
文件 SHA256：
WAMR commit：ca244b0b...
构建宏：FAST_INTERP/GC/REF_TYPES/BULK_MEMORY/MEMORY64/...
WABT version：

Section header dump：wasm-objdump -h
Section semantic dump：wasm-objdump -x
Code raw dump：wasm-objdump -s -j Code
Code disassembly：wasm-objdump -d
WAMR verbose log：
失败函数/offset：file absolute + function relative
对应 loader function/case：
```

这能避免几天后无法复现“同一文件为何在另一个构建中通过/失败”。
