"""Discover a public shop with Python's standard library; no agent or SDK required."""

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import urlopen


def discover(base_url: str, shop: str, collection: str, query: str = "") -> dict:
    address = urlsplit(base_url)
    if (
        address.scheme not in {"http", "https"}
        or not address.hostname
        or address.username
        or address.password
        or address.query
        or address.fragment
    ):
        raise ValueError(
            "provide an HTTP(S) server base URL without credentials or query parameters"
        )
    result = {"revision": None, "items": []}
    cursor = None
    seen = set()
    for _ in range(100):
        parameters = {"limit": 100, "query": query}
        if cursor:
            parameters["cursor"] = cursor
        url = (
            f"{base_url.rstrip('/')}/v1/shops/{quote(shop, safe='')}/{quote(collection, safe='')}?"
            + urlencode(parameters)
        )
        with urlopen(url, timeout=10) as response:
            payload = response.read(8 * 1024 * 1024 + 1)
        if len(payload) > 8 * 1024 * 1024:
            raise ValueError("response exceeds the client limit")
        page = json.loads(payload)
        if result["revision"] is not None and page["revision"] != result["revision"]:
            raise ValueError("publication changed; restart discovery")
        result["revision"] = page["revision"]
        result["items"].extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            return result
        if cursor in seen:
            raise ValueError("server repeated a page cursor")
        seen.add(cursor)
    raise ValueError("discovery exceeded 100 pages")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    parser.add_argument("shop")
    parser.add_argument(
        "--collection",
        default="capabilities",
        choices=(
            "sites",
            "resources",
            "catalogs",
            "processes",
            "capabilities",
            "workflows",
            "variables",
        ),
    )
    parser.add_argument("--query", default="")
    args = parser.parse_args()
    try:
        print(json.dumps(discover(args.base_url, args.shop, args.collection, args.query), indent=2))
    except (HTTPError, URLError, ValueError) as error:
        print(f"Discovery failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
