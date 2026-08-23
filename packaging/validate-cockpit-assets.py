#!/usr/bin/python3
"""Validate local assets referenced by the packaged Cockpit entry page."""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath


class AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            self.references.append(values["src"])
        if (
            tag == "link"
            and values.get("rel") == "stylesheet"
            and values.get("href")
        ):
            self.references.append(values["href"])


def local_references(index: Path) -> list[str]:
    parser = AssetParser()
    parser.feed(index.read_text(encoding="utf-8"))
    return [
        value
        for value in parser.references
        if "://" not in value and not value.startswith(("/", "../"))
    ]


def validate(asset_root: Path) -> list[str]:
    index = asset_root / "index.html"
    if not index.is_file():
        return ["missing Cockpit entry page: index.html"]

    errors = []
    for reference in local_references(index):
        path = PurePosixPath(reference)
        if path.is_absolute() or ".." in path.parts:
            errors.append(f"unsafe local asset reference: {reference}")
        elif not (asset_root / path).is_file():
            errors.append(f"missing local Cockpit asset: {reference}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("asset_root", type=Path)
    args = parser.parse_args()
    errors = validate(args.asset_root)
    for error in errors:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
