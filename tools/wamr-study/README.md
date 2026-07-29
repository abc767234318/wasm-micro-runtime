# WAMR 学习环境迁移

这套配置支持：

- macOS：Clang、Unix Makefiles、CodeLLDB
- Windows：MSVC、Ninja、Microsoft C++ Debugger

`.vscode` 中的配置和最小 WAT 样例可以提交到 Git。以下内容必须在每台机器上重新生成：

- `understand-build`
- `compile_commands.json`
- `CMakeCache.txt`
- `iwasm` / `iwasm.exe`
- Understand UDB
- `.vscode/wasm/minimal.wasm`

## 从 macOS 迁移

在自己的学习分支提交配置：

```bash
git add .gitignore .vscode tools/wamr-study
git commit -m "Add portable WAMR study environment"
```

建议记录当前 WAMR commit：

```bash
git rev-parse HEAD
```

Windows 上应检出相同 commit，避免源码行号和结构发生变化。

## Windows 依赖

安装：

1. Visual Studio Build Tools 2022
2. `Desktop development with C++`
3. CMake
4. Ninja
5. WABT（需要 `wat2wasm.exe`）
6. VS Code
7. VS Code 扩展 `ms-vscode.cpptools`

从“Developer PowerShell for VS 2022”进入仓库并检查环境：

```powershell
Set-Location <wamr-repository>
.\tools\wamr-study\check-windows.ps1
code .
```

不要直接从 macOS 复制 `understand-build`。VS Code 第一次启动调试时会重新配置并构建。

## Windows 首次调试

1. 执行 `C/C++: Select a Configuration...`
2. 选择 `WAMR Classic Interpreter (Learning)`
3. 选择 `WAMR Classic [Windows]: Learning`
4. 按 `F5`

程序会在入口处停止。然后在 BREAKPOINTS 面板使用
`Add Function Breakpoint` 添加 `.vscode/WAMR_DEBUGGING.md` 中列出的函数。

## macOS 检查

```bash
./tools/wamr-study/check-macos.sh
```
