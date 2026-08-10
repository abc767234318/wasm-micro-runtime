#!/usr/bin/env python3
"""Build a bilingual static mirror of the WebAssembly Core Specification."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UPSTREAM = "https://webassembly.github.io/spec/core/"
CACHE_VERSION = "wasm-bi-v3"
TRANSLATE_ENDPOINT = "https://translate.googleapis.com/translate_a/single"
SYMBOLIC_INDEX_PAGES = {
    "appendix/index-instructions.html",
    "appendix/index-rules.html",
    "appendix/index-types.html",
    "genindex.html",
}
TRANSLATABLE_TAGS = {
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "li",
    "dt",
    "dd",
    "th",
    "td",
    "caption",
}
PROTECTED_SELECTOR = ",".join(
    [
        "code",
        "pre",
        "script",
        "style",
        "svg",
        "img",
        ".math",
        ".literal",
        ".headerlink",
        ".sig-name",
        ".sig-prename",
    ]
)
ASCII_WORD_RE = re.compile(r"[A-Za-z]{2,}")
CHINESE_RE = re.compile(r"[\u3400-\u9fff]")


@dataclass
class TranslationUnit:
    node: Tag
    source_html: str
    protected_html: dict[str, str]
    cache_key: str


class TranslationMemory:
    def __init__(
        self,
        cache_path: Path,
        glossary_path: Path,
        translator: str,
        delay: float,
    ) -> None:
        self.cache_path = cache_path
        self.translator = translator
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "WasmSpecBilingual/1.0 (learning mirror)"})
        self.cache: dict[str, dict[str, str]] = self._read_json(cache_path, {})
        glossary: dict[str, str] = self._read_json(glossary_path, {})
        self.glossary = sorted(glossary.items(), key=lambda item: len(item[0]), reverse=True)
        self.new_entries = 0
        self.cache_hits = 0
        self.argos_translation = None
        if translator == "argos":
            self.argos_translation = self._init_argos()

    @staticmethod
    def _read_json(path: Path, fallback):
        if not path.exists():
            return fallback
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.cache_path.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(self.cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(self.cache_path)

    @staticmethod
    def _token(value: str) -> str:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12].upper()
        return digest

    @staticmethod
    def _placeholder(soup: BeautifulSoup, key: str) -> Tag:
        node = soup.new_tag("wasm-protected")
        node.string = key
        return node

    def protect_fragment(self, node: Tag) -> tuple[str, dict[str, str]]:
        fragment = BeautifulSoup(str(node), "html.parser")
        protected: dict[str, str] = {}

        for item in list(fragment.select(PROTECTED_SELECTOR)):
            token = self._token(f"node:{len(protected)}:{item}")
            protected[token] = str(item)
            item.replace_with(self._placeholder(fragment, token))

        for text_node in list(fragment.find_all(string=True)):
            if not text_node.strip() or not ASCII_WORD_RE.search(str(text_node)):
                continue
            value = str(text_node)
            replacements: list[tuple[int, int, str, str]] = []
            for english, chinese in self.glossary:
                pattern = re.compile(rf"(?<![A-Za-z]){re.escape(english)}(?![A-Za-z])", re.IGNORECASE)
                for match in pattern.finditer(value):
                    if any(start < match.end() and match.start() < end for start, end, _, _ in replacements):
                        continue
                    token = self._token(f"term:{match.group(0).lower()}:{chinese}")
                    replacements.append((match.start(), match.end(), token, chinese))
            if not replacements:
                continue
            replacements.sort(key=lambda item: item[0])
            cursor = 0
            new_nodes: list[Tag | NavigableString] = []
            for start, end, token, chinese in replacements:
                if start > cursor:
                    new_nodes.append(NavigableString(value[cursor:start]))
                protected[token] = chinese
                new_nodes.append(self._placeholder(fragment, token))
                cursor = end
            if cursor < len(value):
                new_nodes.append(NavigableString(value[cursor:]))
            for new_node in reversed(new_nodes):
                text_node.insert_after(new_node)
            text_node.extract()

        return str(fragment), protected

    @staticmethod
    def restore_fragment(value: str, protected: dict[str, str]) -> str:
        fragment = BeautifulSoup(value, "html.parser")
        for placeholder in list(fragment.find_all("wasm-protected")):
            key = re.sub(r"\s+", "", placeholder.get_text("", strip=True)).upper()
            original = protected.get(key)
            if original is None:
                raise RuntimeError(f"unknown protected node: {key}")
            restored = BeautifulSoup(original, "html.parser")
            nodes = list(restored.contents)
            if not nodes:
                placeholder.extract()
                continue
            first = nodes[0]
            placeholder.replace_with(first)
            cursor = first
            for node in nodes[1:]:
                cursor.insert_after(node)
                cursor = node
        if fragment.find("wasm-protected"):
            raise RuntimeError("protected nodes were not fully restored")
        return fragment.decode_contents()

    @staticmethod
    def make_cache_key(source_html: str) -> str:
        normalized = re.sub(r"\s+", " ", source_html).strip()
        return hashlib.sha256(f"{CACHE_VERSION}\0{normalized}".encode("utf-8")).hexdigest()

    def prepare(self, node: Tag) -> TranslationUnit | None:
        source_text = node.get_text(" ", strip=True)
        if not ASCII_WORD_RE.search(source_text):
            return None
        source_html, protected = self.protect_fragment(node)
        key = self.make_cache_key(source_html)
        return TranslationUnit(node, source_html, protected, key)

    def translate_units(self, units: list[TranslationUnit]) -> list[str]:
        results: list[str | None] = [None] * len(units)
        pending: list[tuple[int, TranslationUnit]] = []
        for index, unit in enumerate(units):
            cached = self.cache.get(unit.cache_key)
            if cached:
                results[index] = cached["translation"]
                self.cache_hits += 1
            else:
                pending.append((index, unit))

        if self.translator == "none":
            for index, unit in pending:
                results[index] = self.restore_fragment(unit.source_html, unit.protected_html)
            return [value or "" for value in results]

        if self.translator == "argos":
            for index, unit in pending:
                translated = self._translate_argos_html(unit.source_html)
                restored = self.restore_fragment(translated, unit.protected_html)
                self.cache[unit.cache_key] = {
                    "source": unit.source_html,
                    "translation": restored,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                results[index] = restored
                self.new_entries += 1
                if self.new_entries % 100 == 0:
                    print(f"[argos] translated {self.new_entries} new units", flush=True)
                    self.save()
            return [value or "" for value in results]

        for batch in self._make_batches(pending):
            translated = self._translate_batch(batch)
            for (index, unit), value in zip(batch, translated):
                restored = self.restore_fragment(value, unit.protected_html)
                self.cache[unit.cache_key] = {
                    "source": unit.source_html,
                    "translation": restored,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                results[index] = restored
                self.new_entries += 1
            if self.new_entries % 100 == 0:
                self.save()

        return [value or "" for value in results]

    @staticmethod
    def _init_argos():
        os.environ.setdefault("ARGOS_CHUNK_TYPE", "MINISBD")
        # Argos 1.11 imports stanza unconditionally even when MiniSBD is selected.
        # A stub keeps the lightweight backend independent of PyTorch; Stanza is
        # never referenced on the MiniSBD code path.
        if "stanza" not in sys.modules:
            sys.modules["stanza"] = types.ModuleType("stanza")
        try:
            from argostranslate import package, translate
        except ImportError as error:
            raise RuntimeError(
                "Argos Translate is unavailable. Run scripts/bootstrap_argos.sh first."
            ) from error

        installed = package.get_installed_packages()
        if not any(item.from_code == "en" and item.to_code == "zh" for item in installed):
            print("[argos] downloading the English -> Chinese model", flush=True)
            package.update_package_index()
            model = next(
                (
                    item
                    for item in package.get_available_packages()
                    if item.from_code == "en" and item.to_code == "zh"
                ),
                None,
            )
            if model is None:
                raise RuntimeError("Argos package index has no English -> Chinese model")
            package.install_from_path(model.download())

        from_language = next(
            language for language in translate.get_installed_languages() if language.code == "en"
        )
        to_language = next(
            language for language in translate.get_installed_languages() if language.code == "zh"
        )
        return from_language.get_translation(to_language)

    def _translate_argos_html(self, source_html: str) -> str:
        if self.argos_translation is None:
            raise RuntimeError("Argos translation model is not initialized")
        fragment = BeautifulSoup(source_html, "html.parser")
        for text_node in list(fragment.find_all(string=True)):
            if text_node.find_parent("wasm-protected"):
                continue
            value = str(text_node)
            if not ASCII_WORD_RE.search(value):
                continue
            match = re.fullmatch(r"(\s*)(.*?)(\s*)", value, flags=re.DOTALL)
            if not match or not match.group(2):
                continue
            translated = self.argos_translation.translate(match.group(2))
            text_node.replace_with(
                NavigableString(f"{match.group(1)}{translated}{match.group(3)}")
            )
        return fragment.decode_contents()

    @staticmethod
    def _make_batches(
        pending: list[tuple[int, TranslationUnit]], max_chars: int = 4200
    ) -> Iterable[list[tuple[int, TranslationUnit]]]:
        batch: list[tuple[int, TranslationUnit]] = []
        size = 0
        for item in pending:
            fragment_size = len(item[1].source_html) + 80
            if batch and size + fragment_size > max_chars:
                yield batch
                batch = []
                size = 0
            batch.append(item)
            size += fragment_size
        if batch:
            yield batch

    def _request(self, query: str) -> str:
        last_error: Exception | None = None
        for attempt in range(8):
            try:
                response = self.session.post(
                    TRANSLATE_ENDPOINT,
                    data={
                        "client": "gtx",
                        "sl": "en",
                        "tl": "zh-CN",
                        "dt": "t",
                        "q": query,
                    },
                    timeout=60,
                )
                response.raise_for_status()
                payload = response.json()
                translated = "".join(part[0] for part in payload[0] if part and part[0])
                if not translated:
                    raise RuntimeError("translation endpoint returned empty text")
                if self.delay:
                    time.sleep(self.delay)
                return translated
            except Exception as error:  # requests and malformed response
                last_error = error
                wait_seconds = min(3 * (2**attempt), 45)
                print(
                    f"[translate] retry {attempt + 1}/8 in {wait_seconds}s: "
                    f"{type(error).__name__}",
                    flush=True,
                )
                time.sleep(wait_seconds)
        raise RuntimeError(f"translation request failed after retries: {last_error}")

    def _translate_batch(self, batch: list[tuple[int, TranslationUnit]]) -> list[str]:
        wrappers = "".join(
            f'<wasm-bi data-i="{position}">{unit.source_html}</wasm-bi>'
            for position, (_, unit) in enumerate(batch)
        )
        translated = self._request(wrappers)
        parsed = BeautifulSoup(translated, "html.parser")
        translated_nodes = parsed.find_all("wasm-bi")
        if len(translated_nodes) == len(batch):
            return [node.decode_contents() for node in translated_nodes]

        # Google occasionally drops custom wrappers around very complex tables.
        # Falling back to one request per unit preserves recoverability.
        output: list[str] = []
        for _, unit in batch:
            single = self._request(f"<wasm-bi>{unit.source_html}</wasm-bi>")
            single_parsed = BeautifulSoup(single, "html.parser").find("wasm-bi")
            output.append(single_parsed.decode_contents() if single_parsed else single)
        return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    parser.add_argument("--refresh-upstream", action="store_true")
    parser.add_argument("--source-dir", type=Path, help="Use an existing mirrored core directory")
    parser.add_argument("--translator", choices=("argos", "google", "none"), default="argos")
    parser.add_argument("--delay", type=float, default=0.25, help="Delay after translation requests")
    parser.add_argument("--limit-pages", type=int, default=0, help="Development-only page limit")
    return parser.parse_args()


def mirror_upstream(upstream: str, refresh: bool) -> Path:
    cache_root = PROJECT_ROOT / ".cache" / "upstream"
    parsed = urlparse(upstream)
    relative_root = Path(parsed.netloc) / parsed.path.lstrip("/")
    mirrored_root = cache_root / relative_root
    if refresh and cache_root.exists():
        shutil.rmtree(cache_root)
    if mirrored_root.exists() and any(mirrored_root.glob("*.html")):
        return mirrored_root

    cache_root.mkdir(parents=True, exist_ok=True)
    command = [
        "wget",
        "--mirror",
        "--no-parent",
        "--page-requisites",
        "--convert-links",
        "--adjust-extension",
        "--execute",
        "robots=off",
        "--quiet",
        f"--directory-prefix={cache_root}",
        upstream,
    ]
    print(f"[sync] {upstream}")
    subprocess.run(command, check=True)
    if not mirrored_root.exists():
        raise RuntimeError(f"wget did not create expected directory: {mirrored_root}")
    return mirrored_root


def copy_site(source_dir: Path, site_dir: Path) -> None:
    if site_dir.exists():
        shutil.rmtree(site_dir)
    shutil.copytree(source_dir, site_dir)
    # Keep the generated repository lightweight. The upstream PDF remains
    # available from the canonical specification site and is linked directly.
    shutil.rmtree(site_dir / "_download", ignore_errors=True)
    custom_dir = site_dir / "_bilingual"
    custom_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_ROOT / "theme" / "bilingual.css", custom_dir / "bilingual.css")
    shutil.copy2(PROJECT_ROOT / "theme" / "bilingual.js", custom_dir / "bilingual.js")
    shutil.copy2(PROJECT_ROOT / "theme" / "og.png", custom_dir / "og.png")


def page_root(relative_path: Path) -> str:
    depth = len(relative_path.parts) - 1
    return "../" * depth


def select_outermost_units(container: Tag) -> list[Tag]:
    candidates: list[Tag] = []
    for node in container.find_all(list(TRANSLATABLE_TAGS)):
        if node.find_parent(
            lambda tag: isinstance(tag, Tag)
            and (
                tag.name in {"code", "pre", "script", "style", "svg"}
                or bool({"math", "literal"} & set(tag.get("class", [])))
            )
        ):
            continue
        ancestor = node.parent
        nested = False
        while isinstance(ancestor, Tag) and ancestor is not container:
            if ancestor.name in TRANSLATABLE_TAGS:
                nested = True
                break
            ancestor = ancestor.parent
        if not nested:
            candidates.append(node)
    candidates.extend(container.select("div.line"))
    unique: list[Tag] = []
    seen: set[int] = set()
    for node in candidates:
        if id(node) not in seen:
            unique.append(node)
            seen.add(id(node))
    return unique


def direct_candidate_children(node: Tag) -> list[Tag]:
    children: list[Tag] = []
    for candidate in node.find_all([*TRANSLATABLE_TAGS, "div"]):
        if candidate.name == "div" and "line" not in candidate.get("class", []):
            continue
        ancestor = candidate.parent
        while isinstance(ancestor, Tag) and ancestor is not node:
            if ancestor.name in TRANSLATABLE_TAGS or (
                ancestor.name == "div" and "line" in ancestor.get("class", [])
            ):
                break
            ancestor = ancestor.parent
        if ancestor is node:
            children.append(candidate)
    return children


def prepare_units(
    container: Tag, memory: TranslationMemory, max_unit_chars: int = 4200
) -> list[TranslationUnit]:
    output: list[TranslationUnit] = []

    def append_or_split(node: Tag) -> None:
        unit = memory.prepare(node)
        if unit is None:
            return
        if len(unit.source_html) <= max_unit_chars:
            output.append(unit)
            return
        children = direct_candidate_children(node)
        if not children:
            output.append(unit)
            return
        for child in children:
            append_or_split(child)

    for node in select_outermost_units(container):
        append_or_split(node)
    return output


def prefix_chinese_ids(container: Tag) -> None:
    for node in container.find_all(attrs={"id": True}):
        node["id"] = f"zh-{node['id']}"
    for node in container.find_all(href=True):
        href = node.get("href", "")
        if href.startswith("#"):
            node["href"] = f"#zh-{href[1:]}"
        elif "#" in href:
            base, anchor = href.rsplit("#", 1)
            if not urlparse(base).scheme:
                node["href"] = f"{base}#zh-{anchor}"


def replace_node_html(node: Tag, html: str) -> None:
    fragment = BeautifulSoup(html, "html.parser")
    replacements = list(fragment.contents)
    if not replacements:
        return
    first = replacements[0]
    node.replace_with(first)
    cursor = first
    for replacement in replacements[1:]:
        cursor.insert_after(replacement)
        cursor = replacement


def make_toolbar(soup: BeautifulSoup, root: str, upstream_url: str) -> Tag:
    markup = f"""
    <div class="wasm-bi-toolbar" role="region" aria-label="Bilingual reading controls">
      <div class="wasm-bi-brand">Wasm Spec 中英对照<small>English is normative · 英文原文为准</small></div>
      <div class="wasm-bi-controls" role="group" aria-label="Language view">
        <button class="wasm-bi-button" type="button" data-bi-view-button="both">中英</button>
        <button class="wasm-bi-button" type="button" data-bi-view-button="en">EN</button>
        <button class="wasm-bi-button" type="button" data-bi-view-button="zh">ZH</button>
        <button class="wasm-bi-button" type="button" data-bi-sync></button>
      </div>
      <div class="wasm-bi-search-wrap">
        <input class="wasm-bi-search" type="search" data-bi-search
               placeholder="搜索中英文章节 / Search" aria-label="Search specification">
        <div class="wasm-bi-results" data-bi-results role="listbox"></div>
      </div>
      <a class="wasm-bi-button" href="{upstream_url}" rel="external">Official</a>
    </div>
    """
    return BeautifulSoup(markup, "html.parser").div


def make_panel(
    soup: BeautifulSoup,
    language: str,
    label: str,
    content: Tag,
    upstream_url: str,
) -> Tag:
    panel = soup.new_tag("article")
    panel["class"] = ["wasm-bi-panel", f"wasm-bi-panel-{language}"]
    panel["lang"] = "en" if language == "en" else "zh-CN"
    inner = soup.new_tag("div")
    inner["class"] = ["wasm-bi-panel-inner"]
    badge = soup.new_tag("div")
    badge["class"] = ["wasm-bi-language-label"]
    badge.string = label
    inner.append(badge)
    for child in list(content.contents):
        inner.append(copy.copy(child))
    disclaimer = BeautifulSoup(
        f'<p class="wasm-bi-disclaimer">'
        f'<a href="{upstream_url}">WebAssembly Community Group 官方原文</a> · '
        f'<a href="https://www.w3.org/copyright/software-license/">W3C Software and Document License</a> · '
        f'中文为非官方学习译文。</p>',
        "html.parser",
    ).p
    inner.append(disclaimer)
    panel.append(inner)
    return panel


def add_assets(soup: BeautifulSoup, root: str) -> None:
    head = soup.head
    stylesheet = soup.new_tag("link", rel="stylesheet", href=f"{root}_bilingual/bilingual.css")
    script = soup.new_tag("script", src=f"{root}_bilingual/bilingual.js", defer=True)
    head.append(stylesheet)
    head.append(script)
    soup.html["class"] = list(dict.fromkeys([*(soup.html.get("class") or []), "wasm-bi-active"]))
    soup.html["data-bi-root"] = root


def translate_container(
    container: Tag, memory: TranslationMemory, symbolic_index: bool = False
) -> None:
    units = prepare_units(container, memory)
    if symbolic_index:
        units = [unit for unit in units if unit.node.name in {"h1", "h2", "h3", "h4", "p"}]
    translations = memory.translate_units(units)
    for unit, translated in zip(units, translations):
        replace_node_html(unit.node, translated)


def enhance_sidebar(sidebar: Tag | None, memory: TranslationMemory) -> None:
    if sidebar is None:
        return
    anchors: list[Tag] = []
    units: list[TranslationUnit] = []
    originals: list[str] = []
    for anchor in sidebar.find_all("a"):
        if anchor.find("img"):
            continue
        original = anchor.get_text(" ", strip=True)
        if not ASCII_WORD_RE.search(original) or len(original) > 180:
            continue
        wrapper = BeautifulSoup(f"<span>{original}</span>", "html.parser").span
        unit = memory.prepare(wrapper)
        if unit:
            anchors.append(anchor)
            units.append(unit)
            originals.append(original)
    for anchor, original, translated in zip(anchors, originals, memory.translate_units(units)):
        translated_text = BeautifulSoup(translated, "html.parser").get_text(" ", strip=True)
        anchor.clear()
        # BeautifulSoup's Tag.new_tag is unavailable on some parser versions.
        owner = BeautifulSoup("", "html.parser")
        en = owner.new_tag("span", attrs={"class": "wasm-bi-nav-en"})
        en.string = original
        zh = owner.new_tag("span", attrs={"class": "wasm-bi-nav-zh"})
        zh.string = translated_text
        anchor.append(en)
        anchor.append(zh)


def process_page(
    html_path: Path,
    site_dir: Path,
    upstream: str,
    memory: TranslationMemory,
) -> dict:
    relative = html_path.relative_to(site_dir)
    root = page_root(relative)
    source_url = urljoin(upstream, relative.as_posix())
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "lxml")
    for anchor in soup.find_all("a", href=True):
        if anchor["href"].split("#", 1)[0].endswith("_download/WebAssembly.pdf"):
            anchor["href"] = urljoin(upstream, "_download/WebAssembly.pdf")
    body = soup.select_one("div.body")
    if body is None:
        return {"path": relative.as_posix(), "skipped": True}

    english = copy.deepcopy(body)
    chinese = copy.deepcopy(body)
    translate_container(chinese, memory, relative.as_posix() in SYMBOLIC_INDEX_PAGES)
    prefix_chinese_ids(chinese)
    enhance_sidebar(soup.select_one("div.sphinxsidebarwrapper"), memory)

    english_title = english.find(["h1", "h2"])
    chinese_title = chinese.find(["h1", "h2"])
    en_title_text = english_title.get_text(" ", strip=True).replace("¶", "") if english_title else relative.stem
    zh_title_text = chinese_title.get_text(" ", strip=True).replace("¶", "") if chinese_title else en_title_text
    if soup.title:
        soup.title.string = f"{en_title_text} / {zh_title_text} — Wasm Spec 中英对照"

    body.clear()
    body.append(make_toolbar(soup, root, source_url))
    columns = soup.new_tag("div")
    columns["class"] = ["wasm-bi-columns"]
    columns.append(make_panel(soup, "en", "English · 官方原文", english, source_url))
    columns.append(make_panel(soup, "zh", "中文 · 学习译文", chinese, source_url))
    body.append(columns)
    add_assets(soup, root)

    html_path.write_text("<!DOCTYPE html>\n" + str(soup), encoding="utf-8")
    return {
        "path": relative.as_posix(),
        "source_url": source_url,
        "title_en": en_title_text,
        "title_zh": zh_title_text,
        "skipped": False,
    }


def build_search_index(site_dir: Path, pages: list[dict]) -> list[dict]:
    entries: list[dict] = []
    for page in pages:
        if page.get("skipped"):
            continue
        soup = BeautifulSoup((site_dir / page["path"]).read_text(encoding="utf-8"), "lxml")
        en_headings = soup.select(".wasm-bi-panel-en h1, .wasm-bi-panel-en h2, .wasm-bi-panel-en h3")
        zh_headings = soup.select(".wasm-bi-panel-zh h1, .wasm-bi-panel-zh h2, .wasm-bi-panel-zh h3")
        for index, en_heading in enumerate(en_headings):
            zh_heading = zh_headings[index] if index < len(zh_headings) else None
            en = en_heading.get_text(" ", strip=True).replace("¶", "")
            zh = zh_heading.get_text(" ", strip=True).replace("¶", "") if zh_heading else page["title_zh"]
            anchor = ""
            heading_id = en_heading.get("id")
            if not heading_id:
                parent = en_heading.find_parent(attrs={"id": True})
                heading_id = parent.get("id") if parent else None
            if heading_id:
                anchor = f"#{heading_id}"
            entries.append(
                {
                    "path": page["path"],
                    "anchor": anchor,
                    "en": en,
                    "zh": zh,
                    "haystack": f"{en} {zh}".lower(),
                }
            )
    (site_dir / "_bilingual" / "search-index.json").write_text(
        json.dumps(entries, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return entries


def write_metadata(
    site_dir: Path,
    upstream: str,
    pages: list[dict],
    search_entries: list[dict],
    memory: TranslationMemory,
) -> None:
    metadata = {
        "upstream": upstream,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "page_count": sum(not page.get("skipped") for page in pages),
        "skipped_page_count": sum(bool(page.get("skipped")) for page in pages),
        "search_entry_count": len(search_entries),
        "translation_cache_entries": len(memory.cache),
        "new_translation_entries": memory.new_entries,
        "translation_cache_hits": memory.cache_hits,
        "translator": memory.translator,
        "pages": pages,
    }
    (site_dir / "_bilingual" / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (site_dir / "UPSTREAM-LICENSE.txt").write_text(
        "WebAssembly specification content:\n"
        "Copyright WebAssembly Community Group.\n"
        "Licensed under the W3C Software and Document License.\n"
        "https://www.w3.org/copyright/software-license/\n"
        f"Official source: {upstream}\n",
        encoding="utf-8",
    )


def normalize_text_files(site_dir: Path) -> None:
    for path in site_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {
            ".html",
            ".css",
            ".js",
            ".json",
            ".txt",
        }:
            continue
        try:
            value = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        normalized = "\n".join(line.rstrip() for line in value.splitlines()).rstrip() + "\n"
        if normalized != value:
            path.write_text(normalized, encoding="utf-8")


def main() -> int:
    args = parse_args()
    source_dir = args.source_dir.resolve() if args.source_dir else mirror_upstream(args.upstream, args.refresh_upstream)
    if not (source_dir / "index.html").exists():
        raise SystemExit(f"not a Core Specification mirror: {source_dir}")

    site_dir = PROJECT_ROOT / "site"
    copy_site(source_dir, site_dir)
    memory = TranslationMemory(
        PROJECT_ROOT / "translations" / "cache.json",
        PROJECT_ROOT / "translations" / "glossary.json",
        args.translator,
        args.delay,
    )
    pages_to_process = sorted(site_dir.rglob("*.html"))
    if args.limit_pages:
        pages_to_process = pages_to_process[: args.limit_pages]
    print(f"[build] pages={len(pages_to_process)} translator={args.translator}")

    pages: list[dict] = []
    try:
        for index, html_path in enumerate(pages_to_process, 1):
            relative = html_path.relative_to(site_dir)
            print(f"[{index:02d}/{len(pages_to_process):02d}] {relative}", flush=True)
            pages.append(process_page(html_path, site_dir, args.upstream, memory))
            memory.save()
    finally:
        memory.save()

    search_entries = build_search_index(site_dir, pages)
    write_metadata(site_dir, args.upstream, pages, search_entries, memory)
    normalize_text_files(site_dir)
    print(
        f"[done] pages={len(pages)} search={len(search_entries)} "
        f"translations={len(memory.cache)} new={memory.new_entries} hits={memory.cache_hits}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
