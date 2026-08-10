#!/usr/bin/env python3
"""Verify completeness and integrity of the generated bilingual site."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SITE_DIR = PROJECT_ROOT / "site"
CHINESE_RE = re.compile(r"[\u3400-\u9fff]")
ASCII_WORD_RE = re.compile(r"[A-Za-z]{2,}")


def prose_text(container) -> str:
    clone = BeautifulSoup(str(container), "html.parser")
    for node in clone.select("code, pre, script, style, svg, .math, .literal, .headerlink"):
        node.decompose()
    return clone.get_text(" ", strip=True)


def resolve_target(page: Path, href: str) -> tuple[Path, str] | None:
    parsed = urlparse(href)
    if parsed.scheme or href.startswith(("mailto:", "javascript:", "data:")):
        return None
    raw_path = unquote(parsed.path)
    if not raw_path:
        target = page
    else:
        target = (page.parent / raw_path).resolve()
        if raw_path.endswith("/"):
            target /= "index.html"
    return target, unquote(parsed.fragment)


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    metadata_path = SITE_DIR / "_bilingual" / "metadata.json"
    if not metadata_path.exists():
        print("missing site/_bilingual/metadata.json", file=sys.stderr)
        return 1
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    html_pages = sorted(SITE_DIR.rglob("*.html"))
    expected = metadata["page_count"] + metadata["skipped_page_count"]
    if len(html_pages) != expected:
        errors.append(f"page count: metadata={expected}, files={len(html_pages)}")

    id_cache: dict[Path, set[str]] = {}
    checked_links = 0
    translated_pages = 0
    total_chinese = 0
    for page in html_pages:
        soup = BeautifulSoup(page.read_text(encoding="utf-8"), "lxml")
        relative = page.relative_to(SITE_DIR)
        if soup.find("wasm-protected") or "__WASM_BI_" in str(soup) or "__Wasm_BI_" in str(soup):
            errors.append(f"{relative}: unresolved translation placeholder")
        en = soup.select_one(".wasm-bi-panel-en")
        zh = soup.select_one(".wasm-bi-panel-zh")
        toolbar = soup.select_one(".wasm-bi-toolbar")
        if not en or not zh or not toolbar:
            errors.append(f"{relative}: missing bilingual shell")
            continue

        en_text = prose_text(en)
        zh_text = prose_text(zh)
        chinese_count = len(CHINESE_RE.findall(zh_text))
        total_chinese += chinese_count
        if ASCII_WORD_RE.search(en_text):
            translated_pages += 1
            if chinese_count < 8:
                errors.append(f"{relative}: Chinese prose coverage is too low ({chinese_count})")

        ids = [node["id"] for node in soup.find_all(attrs={"id": True})]
        if len(ids) != len(set(ids)):
            errors.append(f"{relative}: duplicate HTML ids")
        id_cache[page.resolve()] = set(ids)

        for link in soup.find_all("a", href=True):
            resolved = resolve_target(page, link["href"])
            if resolved is None:
                continue
            target, anchor = resolved
            checked_links += 1
            if not target.exists():
                errors.append(f"{relative}: broken link {link['href']}")
                continue
            if anchor and target.suffix.lower() == ".html":
                target_ids = id_cache.get(target)
                if target_ids is None:
                    target_soup = BeautifulSoup(target.read_text(encoding="utf-8"), "lxml")
                    target_ids = {node["id"] for node in target_soup.find_all(attrs={"id": True})}
                    id_cache[target] = target_ids
                if anchor not in target_ids:
                    errors.append(f"{relative}: broken anchor {link['href']}")

    search_path = SITE_DIR / "_bilingual" / "search-index.json"
    if not search_path.exists():
        errors.append("missing bilingual search index")
        search_entries = []
    else:
        search_entries = json.loads(search_path.read_text(encoding="utf-8"))
        for entry in search_entries:
            if not (SITE_DIR / entry["path"]).exists():
                errors.append(f"search index points to missing page: {entry['path']}")

    if translated_pages != metadata["page_count"]:
        warnings.append(
            f"translated page count differs: translated={translated_pages}, metadata={metadata['page_count']}"
        )

    print(f"pages={len(html_pages)}")
    print(f"translated_pages={translated_pages}")
    print(f"chinese_characters={total_chinese}")
    print(f"translation_cache_entries={metadata['translation_cache_entries']}")
    print(f"search_entries={len(search_entries)}")
    print(f"checked_internal_links={checked_links}")
    print(f"warnings={len(warnings)}")
    for warning in warnings:
        print(f"WARNING: {warning}")
    print(f"errors={len(errors)}")
    for error in errors[:200]:
        print(f"ERROR: {error}")
    if len(errors) > 200:
        print(f"ERROR: ... {len(errors) - 200} more")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
