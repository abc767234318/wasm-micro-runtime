# WAMR Interpreter 调试速查

## 推荐阅读顺序

在 VS Code 的“运行和调试”中依次使用：

macOS 使用：

1. `WAMR Classic [macOS]: 1 Lifecycle`
2. `WAMR Classic [macOS]: 2 Interpreter Entry`
3. `WAMR Classic [macOS]: 3 Opcode Loop`

Windows 使用：

1. `WAMR Classic [Windows]: Learning`
2. 第一次停在 `main` 后，在 BREAKPOINTS 面板增加需要的函数断点

Classic 教学构建与默认 Fast Interpreter 使用不同的构建目录，不会互相覆盖：

- Fast：`understand-build/interpreter`
- Classic：`understand-build/interpreter-classic-debug`

## 生命周期调用链

```text
main
  -> wasm_runtime_full_init
  -> wasm_runtime_load
  -> wasm_runtime_instantiate_ex2
  -> wasm_application_execute_main
  -> wasm_runtime_call_wasm
  -> wasm_interp_call_wasm
  -> wasm_interp_call_func_bytecode
  -> while + switch opcode loop
```

加载和实例化阶段重点观察：

- `wasm_file_buf` / `wasm_file_size`
- `wasm_module`
- `wasm_module_inst`
- `error_buf`

进入解释器后重点观察：

- `exec_env`：当前执行环境
- `module`：Wasm 模块实例
- `cur_func`：当前 Wasm 函数
- `frame`：当前解释器栈帧
- `frame_ip`：下一条字节码的位置
- `opcode`：当前操作码
- `frame_lp`：局部变量区域
- `frame_sp`：操作数栈顶

## Opcode Loop 的使用方法

`WAMR Classic [macOS]: 3 Opcode Loop` 会在取出一条 opcode 后停在：

```c
switch (opcode) {
```

把以下表达式加入 VS Code 的 WATCH：

```text
opcode
frame_ip
frame_lp
frame_sp
cur_func
module
exec_env
```

使用 `F10` 观察一条 opcode 的处理过程，使用 `F5` 进入下一次循环。

最小测试模块位于 `.vscode/wasm/minimal.wat`，每次启动前会自动重新编译。

## 两套解释器的用途

- Classic Interpreter：普通 `while + switch` 分派，适合理解语义和数据结构。
- Fast Interpreter：使用更激进的字节码预处理和间接跳转，适合在理解 Classic 后对照性能实现。

`WAMR_BUILD_DEBUG_INTERP` 是 Wasm 客体程序远程调试功能，不是 LLDB 调试 WAMR C 源码所必需的选项。

## Windows

Windows 配置使用 Ninja + MSVC：

- Ninja 能生成 `compile_commands.json`，继续为 C/C++ IntelliSense 提供准确的宏环境。
- MSVC 生成 PDB，使用微软 C/C++ 扩展提供的 `cppvsdbg` 调试。
- 构建产物是 `understand-build/interpreter-classic-debug/iwasm.exe`。

需要安装并加入 `PATH`：

```text
cmake
ninja
cl
wat2wasm
code
```

建议从“Developer PowerShell for VS 2022”启动 VS Code，以确保 `cl.exe` 环境正确：

```powershell
code <wamr-repository>
```

`cppvsdbg` 不支持 launch.json 中的 LLDB `preRunCommands`。第一次启动后，在
BREAKPOINTS 面板使用“Add Function Breakpoint”加入：

```text
wasm_runtime_full_init
wasm_runtime_load
wasm_runtime_instantiate_ex2
wasm_application_execute_main
wasm_runtime_call_wasm
wasm_interp_call_wasm
wasm_interp_call_func_bytecode
```

这些断点会保存在 Windows 的 VS Code 工作区状态中，以后无需重复添加。

不要把 macOS 的 `understand-build`、`compile_commands.json`、CMakeCache 或 UDB
复制到 Windows。它们包含平台和绝对路径信息，应在 Windows 上重新生成。
