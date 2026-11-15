"""Install a pinned public classification source locally. Do not commit the DOCX."""
from argparse import ArgumentParser
from hashlib import sha256
from pathlib import Path
from urllib.request import urlopen

URL = "https://www.stats.gov.cn/sj/tjbz/gjtjbz/202302/P020230213403061717795.docx"
SHA256 = "ee0e82f599acbf10d155e9184ebf8747fd606e7e287cc26170fda9cd36ac4ab3"
LIMIT = 2 * 1024 * 1024
DEST = Path(__file__).resolve().parents[1] / "references" / "industry_taxonomies" / "GB_T_4754_2017_as_amended_2019_official.docx"


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--from-file", type=Path, help="Use an already downloaded copy of the official attachment")
    args = parser.parse_args()
    if DEST.exists():
        if DEST.is_symlink() or sha256(DEST.read_bytes()).hexdigest() != SHA256:
            raise SystemExit("Existing catalog is different; nothing was overwritten.")
        return
    if args.from_file:
        with args.from_file.open("rb") as handle:
            binary = handle.read(LIMIT + 1)
    else:
        with urlopen(URL, timeout=30) as handle:
            binary = handle.read(LIMIT + 1)
    if len(binary) > LIMIT or sha256(binary).hexdigest() != SHA256:
        raise SystemExit("Official download did not match the pinned catalog; nothing was installed.")
    DEST.parent.mkdir(parents=True, exist_ok=True)
    with DEST.open("xb") as handle:
        handle.write(binary)
    print("Verified industry catalog installed:", DEST.name)


if __name__ == "__main__":
    main()
