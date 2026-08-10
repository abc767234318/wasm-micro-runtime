const stats = [
  ["50", "完整规范页面"],
  ["7.6 万+", "中文学习译文"],
  ["945", "双语搜索条目"],
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
          <div className="eyebrow">WebAssembly Core Specification · Release 3.0</div>
          <h1>一边学 Wasm，<br />一边读懂技术英语。</h1>
          <p className="lede">
            官方英文与中文学习译文同屏对照。保留公式、语法产生式、
            指令名称、章节锚点和官方链接，适合从实现者角度系统学习。
          </p>
          <div className="actions">
            <a className="primary" href="/spec/index.html">开始阅读 <span aria-hidden="true">→</span></a>
            <a className="secondary" href="/spec/intro/introduction.html">Introduction / 简介</a>
          </div>
          <p className="authority">英文原文是权威文本；中文为非官方学习译文。</p>
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
          <div className="bridge" aria-hidden="true"><i /><i /><i /></div>
          <article className="sheet sheet-zh" lang="zh-CN">
            <div className="sheet-tag">ZH · 学习译文</div>
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
          <span>推荐路径</span>
          <h2>从概念到字节，循序渐进。</h2>
        </div>
        <div className="path-grid">
          <a href="/spec/intro/overview.html"><b>01</b><h3>Overview / 概述</h3><p>建立值、指令、函数、表和线性内存的整体心智模型。</p></a>
          <a href="/spec/syntax/index.html"><b>02</b><h3>Structure / 结构</h3><p>掌握 Wasm 抽象语法中的类型、指令与模块结构。</p></a>
          <a href="/spec/valid/index.html"><b>03</b><h3>Validation / 验证</h3><p>理解类型系统如何在执行前证明模块安全。</p></a>
          <a href="/spec/binary/index.html"><b>04</b><h3>Binary / 二进制</h3><p>对照 Section、LEB128、opcode 与 function body 的实际编码。</p></a>
        </div>
      </section>

      <footer>
        <span>Wasm Spec 中英对照</span>
        <p>Source: WebAssembly Community Group · W3C Software and Document License</p>
      </footer>
    </main>
  );
}
