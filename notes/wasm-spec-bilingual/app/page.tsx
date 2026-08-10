const stats = [
  ["3.0", "Core Specification"],
  ["50", "规范页面"],
  ["945", "双语索引条目"],
];

export default function Home() {
  return (
    <main>
      <nav className="topbar" aria-label="顶部导航">
        <a className="wordmark" href="/">Wasm Spec <span>中英对照</span></a>
        <div className="navlinks">
          <a href="/spec/index.html">目录</a>
          <a href="/spec/binary/index.html">Binary Format</a>
          <a href="https://webassembly.github.io/spec/core/" rel="noreferrer">Official ↗</a>
        </div>
      </nav>

      <section className="hero">
        <div className="hero-copy">
          <div className="eyebrow">Bilingual Edition · Release 3.0</div>
          <h1>WebAssembly Core Specification<br /><span>中英对照版</span></h1>
          <p className="lede">
            官方英文与中文对照文本并列呈现。完整保留公式、语法产生式、
            指令名称、章节锚点与规范内部链接。
          </p>
          <div className="actions">
            <a className="primary" href="/spec/index.html">打开规范 <span aria-hidden="true">→</span></a>
            <a className="secondary" href="/spec/genindex.html">Index / 总索引</a>
          </div>
          <p className="authority">英文原文为规范性文本；中文为非官方对照文本。</p>
        </div>

        <div className="document-pair" aria-label="中英双栏文档预览">
          <article className="sheet sheet-en">
            <div className="sheet-tag">EN · normative</div>
            <h2>Binary Format</h2>
            <p>The binary format is a dense linear encoding of the abstract syntax.</p>
            <div className="rule"><code>section ::= id size contents</code></div>
            <h3>Modules</h3>
            <p>A module is encoded as a sequence of sections.</p>
          </article>
          <article className="sheet sheet-zh" lang="zh-CN">
            <div className="sheet-tag">ZH · Translation</div>
            <h2>二进制格式</h2>
            <p>二进制格式是对抽象语法的紧凑线性编码。</p>
            <div className="rule"><code>段 ::= id size contents</code></div>
            <h3>模块</h3>
            <p>模块被编码为一系列段。</p>
          </article>
        </div>
      </section>

      <section className="stats" aria-label="站点内容统计">
        {stats.map(([value, label]) => (
          <div className="stat" key={label}><strong>{value}</strong><span>{label}</span></div>
        ))}
      </section>

      <section className="paths">
        <div className="section-heading">
          <span>SPECIFICATION CONTENTS</span>
          <h2>按规范组成直接访问。</h2>
        </div>
        <div className="path-grid">
          <a href="/spec/intro/overview.html"><b>01</b><h3>Overview / 概述</h3><p>WebAssembly 的设计目标、概念与核心语义概览。</p></a>
          <a href="/spec/syntax/index.html"><b>02</b><h3>Structure / 结构</h3><p>类型、指令、函数、表、内存与模块的抽象语法。</p></a>
          <a href="/spec/valid/index.html"><b>03</b><h3>Validation / 验证</h3><p>类型系统、验证上下文及各类结构的判定规则。</p></a>
          <a href="/spec/binary/index.html"><b>04</b><h3>Binary / 二进制</h3><p>Section、LEB128、opcode 与 function body 的二进制编码。</p></a>
        </div>
      </section>

      <footer>
        <span>WebAssembly Core Specification 3.0 · 中英对照版</span>
        <p>Source: WebAssembly Community Group · W3C Software and Document License</p>
      </footer>
    </main>
  );
}
