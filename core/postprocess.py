from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

import requests
from bs4 import BeautifulSoup


_LINK_REGEX = re.compile(r"https?://[^\s'\">]+(?:#[^\s'\">]*)?", re.IGNORECASE)


def _extract_links_from_text(text: str) -> list[str]:
    links: list[str] = []

    # Mega-style: capture URL and an adjacent key that may be whitespace-separated
    for m in re.finditer(r"(https?://(?:www\.)?mega\.nz/[^\s#'\">]+)\s*#?\s*([A-Za-z0-9_-]{8,})?",
                         text, flags=re.IGNORECASE | re.DOTALL):
        url = m.group(1)
        key = m.group(2)
        if key:
            links.append(f"{url}#{key}")
        else:
            links.append(url)

    # Normalise whitespace so URLs split across lines are captured
    norm = re.sub(r"\s+", " ", text)
    for m in re.finditer(_LINK_REGEX, norm):
        links.append(m.group(0).strip())

    # Deduplicate preserving order
    seen = set()
    out: list[str] = []
    for l in links:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out


def _extract_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    # prefer article, then main, then body
    el = soup.find("article") or soup.find("main") or soup.find("body")
    if el:
        return el.get_text(separator="\n", strip=True)
    # fallback to meta description or og:description
    meta = soup.find("meta", attrs={"property": "og:description"})
    if not meta:
        meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return meta.get("content")
    # last resort: full text
    return soup.get_text(separator="\n", strip=True)


def extract_links_for_job(job: dict, event_cb: Callable[[str, object], None]) -> None:
    """Generic post-process: gather textual content related to a job and write links.

    Strategy (priority):
      1. Parse any local JSON metadata files under output for description/content
      2. Fetch the remote URL and extract description/article text
      3. Scan local text-like files (.html/.htm/.txt/.md/.json/.xml) for text

    Writes <artist>/<site>.txt with headers per source.
    Emits log lines via `event_cb("log", msg)`.
    """
    out_dir = Path(job.get("output", "")).resolve()
    if not out_dir.exists():
        return

    summary_lines: list[str] = []

    # 1) JSON metadata files
    for jf in sorted(out_dir.rglob("*.json")):
        try:
            text = jf.read_text(encoding="utf-8", errors="ignore")
            obj = json.loads(text)
        except Exception:
            continue

        # JSON could be many shapes; try common keys
        candidate_texts: list[tuple[str, str]] = []
        # If it's gallery-dl metadata with 'description' or 'title'
        if isinstance(obj, dict):
            title = obj.get("title") or obj.get("name") or jf.stem
            desc = obj.get("description") or obj.get("content") or obj.get("body")
            if isinstance(desc, str) and desc.strip():
                candidate_texts.append((title, desc))
            # gallery-dl may embed entries for multiple items
            if "entries" in obj and isinstance(obj["entries"], list):
                for e in obj["entries"]:
                    if isinstance(e, dict):
                        t = e.get("title") or jf.stem
                        d = e.get("description") or e.get("content") or e.get("body")
                        if isinstance(d, str) and d.strip():
                            candidate_texts.append((t, d))

        # Extract links from candidate_texts
        for title, t in candidate_texts:
            links = _extract_links_from_text(t)
            if links:
                summary_lines.append(f"===== {title}/{job.get('url','')} =====")
                summary_lines.extend(links)
                summary_lines.append("")

    # 2) Fetch remote page description / article
    try:
        resp = requests.get(job.get("url", ""), timeout=8)
        if resp.ok and resp.text:
            try:
                text = _extract_text_from_html(resp.text)
                links = _extract_links_from_text(text)
                if links:
                    summary_lines.append(f"===== DESCRIPTION/{job.get('url','')} =====")
                    summary_lines.extend(links)
                    summary_lines.append("")
            except Exception:
                event_cb("log", "  [postprocess] failed parsing remote HTML\n")
    except Exception:
        event_cb("log", "  [postprocess] failed fetching remote URL\n")

    # 3) Scan local text-like files
    text_exts = {".html", ".htm", ".txt", ".md", ".xml"}
    for path in sorted(out_dir.rglob("*")):
        if path.is_dir():
            continue
        if path.suffix.lower() not in text_exts:
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        if path.suffix.lower() in {".html", ".htm"}:
            try:
                text = _extract_text_from_html(raw)
            except Exception:
                text = raw
        else:
            text = raw

        links = _extract_links_from_text(text)
        if links:
            title = path.name
            summary_lines.append(f"===== {title}/{job.get('url','')} =====")
            summary_lines.extend(links)
            summary_lines.append("")

    if summary_lines:
        site = str(job.get("site", "links_extracted")).strip() or "links_extracted"
        safe_site = re.sub(r"[^A-Za-z0-9._-]+", "_", site)

        # Expected layouts:
        #   single URL:   <base>/<artist>/<site>
        #   multiple URL: <base>/<artist>/<site>/<account>
        if out_dir.name.lower() == site.lower():
            artist_dir = out_dir.parent
        elif out_dir.parent.name.lower() == site.lower():
            artist_dir = out_dir.parent.parent
        else:
            artist_dir = out_dir

        target = artist_dir / f"{safe_site}.txt"
        payload = "\n".join(summary_lines).rstrip() + "\n\n"

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as f:
                f.write(payload)
            event_cb("log", f"  [postprocess] wrote link summary: {str(target)}\n")
        except Exception:
            event_cb("log", "  [postprocess] failed writing link summary\n")


__all__ = ["extract_links_for_job"]
