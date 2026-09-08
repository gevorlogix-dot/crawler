"""Regenerate the bundled schema.org vocabulary used by the structured-data check.

The validator has to work offline and inside a single audit run, so the 1.5 MB
official vocabulary is compressed here into the three tables the checker
actually reads — class parents, property domain/range, and enumeration members —
and written to `audit/data/schemaorg.json.gz` (~90 KB).

    python scripts/build_schema_vocab.py            # fetch and rebuild

Run it when schema.org publishes a release. Nothing else needs to change: the
loader reads whatever is in the file and reports its stamp in the report.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "audit" / "data" / "schemaorg.json.gz"
SOURCE = "https://schema.org/version/latest/schemaorg-current-https.jsonld"


def _ids(node, key) -> list[str]:
    """schema.org emits a single object or a list; normalise to bare labels."""
    v = node.get(key)
    if v is None:
        return []
    items = v if isinstance(v, list) else [v]
    out = []
    for it in items:
        ident = it.get("@id") if isinstance(it, dict) else it
        if isinstance(ident, str) and ident.startswith("schema:"):
            out.append(ident.split(":", 1)[1])
    return out


def build(graph: list[dict]) -> dict:
    types: dict[str, list[str]] = {}
    props: dict[str, dict] = {}
    members: dict[str, str] = {}
    datatypes: set[str] = set()
    pending: set[str] = set()
    attic: set[str] = set()
    superseded: dict[str, str] = {}

    for node in graph:
        ident = node.get("@id", "")
        if not ident.startswith("schema:"):
            continue
        name = ident.split(":", 1)[1]
        kinds = node.get("@type")
        kinds = kinds if isinstance(kinds, list) else [kinds]

        part_of = json.dumps(node.get("schema:isPartOf", ""))
        if "pending.schema.org" in part_of:
            pending.add(name)
        if "attic.schema.org" in part_of:
            attic.add(name)
        sup = _ids(node, "schema:supersededBy")
        if sup:
            superseded[name] = sup[0]

        if "rdf:Property" in kinds:
            entry = {}
            d = _ids(node, "schema:domainIncludes")
            r = _ids(node, "schema:rangeIncludes")
            if d:
                entry["d"] = sorted(d)
            if r:
                entry["r"] = sorted(r)
            props[name] = entry
        elif "rdfs:Class" in kinds:
            types[name] = sorted(_ids(node, "rdfs:subClassOf"))
            if "schema:DataType" in kinds:
                datatypes.add(name)
        else:
            # Anything else is an enumeration member: its @type *is* the
            # enumeration it belongs to (InStock -> ItemAvailability).
            for k in kinds:
                if isinstance(k, str) and k.startswith("schema:"):
                    members[name] = k.split(":", 1)[1]

    # DataType descendants are data types too (URL < Text, Integer < Number).
    changed = True
    while changed:
        changed = False
        for name, parents in types.items():
            if name not in datatypes and any(p in datatypes for p in parents):
                datatypes.add(name)
                changed = True

    return {
        "source": SOURCE,
        "fetched": date.today().isoformat(),
        "types": types,
        "props": props,
        "members": members,
        "datatypes": sorted(datatypes),
        "pending": sorted(pending),
        "attic": sorted(attic),
        "superseded": superseded,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-file", help="use a local copy instead of fetching")
    args = ap.parse_args()

    if args.from_file:
        raw = Path(args.from_file).read_bytes()
    else:
        print(f"fetching {SOURCE}")
        raw = requests.get(SOURCE, timeout=60).content
    doc = json.loads(raw.decode("utf-8"))
    vocab = build(doc["@graph"])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(vocab, separators=(",", ":"), sort_keys=True).encode("utf-8")
    with gzip.open(OUT, "wb", compresslevel=9) as fh:
        fh.write(payload)

    print(f"{len(vocab['types'])} types, {len(vocab['props'])} properties, "
          f"{len(vocab['members'])} enumeration members, "
          f"{len(vocab['datatypes'])} data types")
    print(f"wrote {OUT.relative_to(ROOT)} — {OUT.stat().st_size / 1024:.0f} KB "
          f"(from {len(payload) / 1024:.0f} KB of JSON)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
