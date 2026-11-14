"""
tools/probe_instance.py
-----------------------
READ-ONLY probe of an e621ng instance. Sends GET requests only, never
changes anything on the server.

It reports, per endpoint: HTTP status, how many items came back, and the
field names of the first item. That tells us exactly what the instance
exposes for posts, tags, wiki, artists, aliases, implications, pools and sets
before the tag/relations manager is designed around it.

Usage:
    python tools/probe_instance.py --server http://localhost:3000 --username NAME
    (API key is read from --api-key, else $E621_API_KEY, else asked silently)

Output: printed summary + probe_result.json (API key is never written).
Send probe_result.json back.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

import requests

TIMEOUT = 15

# (label, path, params). All GET, all limit 1.
ENDPOINTS: list[tuple[str, str, dict]] = [
    ("posts",               "/posts.json",               {"limit": 1}),
    ("posts_with_parent",   "/posts.json",               {"limit": 1, "tags": "status:any parent:any"}),
    ("tags",                "/tags.json",                {"limit": 1}),
    ("tags_artist_type",    "/tags.json",                {"limit": 1, "search[category]": 1}),
    ("tags_unused",         "/tags.json",                {"limit": 1, "search[hide_empty]": "false", "search[order]": "date"}),
    ("wiki_pages",          "/wiki_pages.json",          {"limit": 1}),
    ("wiki_page_versions",  "/wiki_page_versions.json",  {"limit": 1}),
    ("artists",             "/artists.json",             {"limit": 1}),
    ("artist_versions",     "/artist_versions.json",     {"limit": 1}),
    ("tag_aliases",         "/tag_aliases.json",         {"limit": 1}),
    ("tag_implications",    "/tag_implications.json",    {"limit": 1}),
    ("pools",               "/pools.json",               {"limit": 1}),
    ("pool_versions",       "/pool_versions.json",       {"limit": 1}),
    ("post_sets",           "/post_sets.json",           {"limit": 1}),
    ("tag_type_versions",   "/tag_type_versions.json",   {"limit": 1}),
    ("post_versions",       "/post_versions.json",       {"limit": 1}),
]


def _first_item(data):
    """Normalise the many JSON shapes e621ng returns to (count, first_item)."""
    if isinstance(data, list):
        return len(data), (data[0] if data else None)
    if isinstance(data, dict):
        # {"posts": [...]} / {"tags": [...]} style wrappers
        for v in data.values():
            if isinstance(v, list):
                return len(v), (v[0] if v else None)
        return 1, data
    return 0, None


def probe(server: str, auth: tuple[str, str]) -> dict:
    session = requests.Session()
    session.auth = auth
    session.headers["User-Agent"] = "BooruManager-probe/1.0 (read-only)"

    result: dict = {"server": server, "user": auth[0], "endpoints": {}}

    # Who am I, and at what level?
    try:
        r = session.get(f"{server}/users.json",
                        params={"search[name_matches]": auth[0], "limit": 1},
                        timeout=TIMEOUT)
        result["auth_check"] = {"status": r.status_code}
        if r.ok:
            n, item = _first_item(r.json())
            if isinstance(item, dict):
                result["auth_check"]["level_string"] = item.get("level_string")
                result["auth_check"]["level"] = item.get("level")
                result["auth_check"]["fields"] = sorted(item.keys())
        else:
            result["auth_check"]["body"] = r.text[:200]
    except requests.RequestException as e:
        result["auth_check"] = {"error": str(e)}

    for label, path, params in ENDPOINTS:
        entry: dict = {"path": path, "params": params}
        try:
            r = session.get(server + path, params=params, timeout=TIMEOUT)
            entry["status"] = r.status_code
            if r.ok:
                try:
                    count, item = _first_item(r.json())
                    entry["items"] = count
                    if isinstance(item, dict):
                        entry["fields"] = sorted(item.keys())
                except ValueError:
                    entry["note"] = "200 but not JSON"
            else:
                entry["body"] = r.text[:200]
        except requests.RequestException as e:
            entry["error"] = str(e)
        result["endpoints"][label] = entry
    return result


def print_summary(result: dict) -> None:
    print(f"\nServer: {result['server']}   User: {result['user']}")
    auth = result.get("auth_check", {})
    print(f"Auth check: {auth.get('status', auth.get('error'))}"
          f"  level={auth.get('level_string', '?')}\n")
    print(f"{'endpoint':<22}{'status':<8}{'items':<7}fields")
    print("-" * 78)
    for label, e in result["endpoints"].items():
        status = e.get("status", "ERR")
        fields = ", ".join(e.get("fields", []))[:60] or e.get("error", "")[:60]
        print(f"{label:<22}{str(status):<8}{str(e.get('items', '-')):<7}{fields}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only e621ng instance probe")
    ap.add_argument("--server", default="http://localhost:3000")
    ap.add_argument("--username", required=True)
    ap.add_argument("--api-key", default=os.environ.get("E621_API_KEY"))
    ap.add_argument("--out", default="probe_result.json")
    args = ap.parse_args()

    api_key = args.api_key or getpass.getpass("API key (hidden): ")
    result = probe(args.server.rstrip("/"), (args.username, api_key))

    print_summary(result)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\nSaved {args.out} (no API key inside). Send that file back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
