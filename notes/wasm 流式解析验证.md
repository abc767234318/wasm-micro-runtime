WAMR 确实没做流式，但可参考的实现不少，而且不限于 V8。

---

### C/C++ 生态

#### 1. wabt 的 `BinaryReader`（最轻量参考）

wabt（WebAssembly Binary Toolkit）是 W3C 官方工具集，它的 `BinaryReader` 是一个**事件驱动的流式解析器**：

```c++
// src/binary-reader.h
class BinaryReaderDelegate {
  virtual Result OnTypeSection(...) = 0;
  virtual Result OnImportSection(...) = 0;
  virtual Result OnFunctionSection(...) = 0;
  virtual Result OnCodeSectionStart(...) = 0;   // section 开始
  virtual Result OnFunctionBody(...) = 0;        // 逐函数回调
  virtual Result OnCodeSectionEnd(...) = 0;
  virtual Result OnOpcode(Opcode) = 0;           // 逐条指令
  virtual Result OnDataSection(...) = 0;
  // ...
};
```

调用方实现这个 delegate，parser 边读边回调，不需要整个文件在内存。代码量不大，逻辑清晰，很适合作为 C 语言的参考设计。

#### 2. Wizard Engine

Andreas Rossberg（wasm 规范作者之一）用 OCaml 写的参考实现，虽然是 OCaml 但设计极其规范。它的 parser 是纯函数式的流式解析，逐字节消费，结构上可以直观地翻译成 C 的状态机。

#### 3. V8 的 Liftoff

功能最完整但在三家里最复杂。它的做法是：收到一个完整的函数体字节码后，**立即**做 baseline 编译（无需等整个模块）。验证和编译交织在一起——验证不过就抛 error，编译出来的代码直接丢弃。

---

### Rust 生态（设计上最值得借鉴）

#### wasmparser（强烈建议看）

Bytecode Alliance 出的 `wasmparser` crate，**是目前设计最清晰的流式 wasm 解析器**，被 Wasmtime、wasmi 等使用。

核心设计是一系列 `for_each_*` 迭代器，完全不要求整个模块在内存：

```rust
let parser = Parser::new(0);
for payload in parser.parse_all(&mut reader) {
    match payload? {
        Payload::TypeSection(reader) => {
            for item in reader { /* 逐条处理类型 */ }
        }
        Payload::CodeSectionStart { count, .. } => {
            // 准备接收 count 个函数体
        }
        Payload::CodeSectionEntry(body) => {
            // 来了一个函数体，立即验证+编译
            let mut validator = Validator::new();
            for op in body.get_operators_reader()? {
                validator.opcode(&op)?;  // 逐条指令边读边验证
            }
        }
    }
}
```

它的 `Validator` 也是流式的——每来一条 opcode 就推进一步栈状态，不需要整个函数体。

---

### 如果要给 WAMR 加流式加载，推荐路线

```
参考 wasmparser 的核心抽象 ──→ 在 wasm_loader.c 中加入状态机
                                 ↓
                        OnSection → 分 section 处理
                                 ↓
                        OnFunctionBody → 立即验证+预编译
                                 ↓
                        保留现有的 WASMModule 结构
                        section 信息填进去即可
```

wabt 的 `BinaryReader` 是 C++ 里最直接的参考，wasmparser 的 API 设计是最清晰的思想来源。两者都不是全量加载模型，而且代码量都适中，值得阅读。