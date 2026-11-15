"""Versioned industry identifiers, separate from analytical template coverage."""
from collections import Counter
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
import re
from xml.etree import ElementTree as ET
from zipfile import ZipFile

VERSION = "GB/T 4754-2017+A1:2019"
EXPECTED = (20, 97, 473, 1382)
SOURCE = Path(__file__).resolve().parents[2] / "references/industry_taxonomies/GB_T_4754_2017_as_amended_2019_official.docx"

@lru_cache(maxsize=1)
def catalog():
    ns = {"w":"http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with ZipFile(SOURCE) as archive:
        doc = ET.fromstring(archive.read("word/document.xml"))
    found = {}
    current_section = None
    for row in doc.findall(".//w:tr", ns):
        cells = ["".join(n.text or "" for n in c.findall(".//w:t", ns)).strip() for c in row.findall("w:tc", ns)]
        if len(cells) != 6 or not cells[4]:
            continue
        for i, pattern in enumerate(("[A-T]", "[0-9]{2}", "[0-9]{3}", "[0-9]{4}")):
            code = cells[i]
            if not re.fullmatch(pattern, code):
                if i == 0 or not code.isdigit() or len(code) <= i+1:
                    continue
                tcs = row.findall("w:tc", ns)
                para = lambda cell: ["".join(n.text or "" for n in p.findall(".//w:t",ns)).strip() for p in cell.findall("w:p",ns)]
                codes, labels = para(tcs[i]), para(tcs[4])
                if len(codes) != len(labels):
                    raise ValueError("Cannot align merged industry labels")
                for ck, label in zip(codes, labels):
                    if re.fullmatch(pattern, ck) and label:
                        record = (ck, label, i, current_section if i == 1 else ck[:-1])
                        if ck in found and found[ck] != record:
                            raise ValueError("Conflicting industry code " + ck)
                        found[ck] = record
                continue
            if i == 0:
                current_section = code
            parent = None if i == 0 else current_section if i == 1 else code[:-1]
            record = (code, cells[4], i, parent)
            if code in found and found[code] != record:
                raise ValueError("Conflicting industry code " + code)
            found[code] = record
    count = Counter(row[2] for row in found.values())
    if tuple(count[i] for i in range(4)) != EXPECTED:
        raise ValueError("Unexpected official industry hierarchy count")
    for code, name, level, parent in found.values():
        if level and (parent not in found or found[parent][2] != level-1):
            raise ValueError("Missing industry parent for " + code)
    return tuple(found.values())

def industry(code):
    for key, title, level, parent in catalog():
        if code == key:
            return {"code":key, "name":title, "level":level, "parent":parent}
    raise ValueError("Unknown GB/T industry code: " + str(code))

def catalog_payload():
    return {"version":VERSION, "source_sha256":sha256(SOURCE.read_bytes()).hexdigest(),
            "counts":list(EXPECTED),
            "industries":[{"code":c,"name":n,"level":l,"parent":p} for c,n,l,p in catalog()]}
