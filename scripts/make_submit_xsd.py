"""Make the official ADES submission schema (submit.xsd) from the ADES master file, for tests/test_mpc.py.

    python scripts/make_submit_xsd.py

The IAU ADES-Master repository builds submit.xsd by applying xslt/xsd/submitxsd.xslt to xml/adesmaster.xml.
This script downloads that XSLT (plain XML, no imports) into cache/ades/xslt/ and applies it with lxml, with
file and network access switched off for the transform. Output: cache/ades/xslt/submit.xsd (not committed).
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests
from lxml import etree

ROOT = Path(__file__).resolve().parent.parent
ADES = ROOT / "cache" / "ades"
XSLT_URL = "https://raw.githubusercontent.com/IAU-ADES/ADES-Master/master/xslt/xsd/submitxsd.xslt"
MASTER_URL = "https://raw.githubusercontent.com/IAU-ADES/ADES-Master/master/xml/adesmaster.xml"


def main():
    master = ADES / "adesmaster.xml"
    if not master.exists():  # e.g. a fresh checkout or the Docker build: fetch it from the IAU repository
        ADES.mkdir(parents=True, exist_ok=True)
        r = requests.get(MASTER_URL, timeout=60)
        r.raise_for_status()
        master.write_bytes(r.content)
    out_dir = ADES / "xslt"
    out_dir.mkdir(parents=True, exist_ok=True)
    xsl = out_dir / "submitxsd.xslt"
    if not xsl.exists():
        r = requests.get(XSLT_URL, timeout=60)
        r.raise_for_status()
        xsl.write_bytes(r.content)
    tree = etree.parse(str(xsl))
    if tree.xpath("//xsl:import|//xsl:include", namespaces={"xsl": "http://www.w3.org/1999/XSL/Transform"}):
        sys.exit("the XSLT imports other files; not handled")
    ac = etree.XSLTAccessControl(read_file=False, write_file=False, create_dir=False, read_network=False,
                                 write_network=False)
    res = etree.XSLT(tree, access_control=ac)(etree.parse(str(master)))
    out = out_dir / "submit.xsd"
    out.write_bytes(etree.tostring(res, pretty_print=True, xml_declaration=True, encoding="UTF-8"))
    etree.XMLSchema(etree.parse(str(out)))  # must compile
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
