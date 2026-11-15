"""Offline, read-only check of a manifest-pinned, operator-isolated snapshot copy.

This does not discover live data, authenticate workspace users or perform a
migration. The operator must make the clone independently and keep it offline.
Requires Linux-style fd-relative opens and O_NOFOLLOW; fails closed otherwise.
"""
from argparse import ArgumentParser
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode=True

from costing_outputs import validate_snapshot_integrity

_ID = re.compile(r"[0-9a-f]{32}")
_SHA = re.compile(r"[0-9a-f]{64}")
_MAX_RECORDS = 512
_MAX_FILE_BYTES = 64 * 1024**2
_MAX_TOTAL_BYTES = 128 * 1024**2
_MAX_MANIFEST_BYTES = 192 * 1024
_MANIFEST = "snapshot-audit-manifest.json"
_SNAPSHOTS = "costing_snapshots"


class Refuse(Exception):
    def __init__(self, code):
        self.code = code


def _need(ok, code):
    if not ok:
        raise Refuse(code)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _token(ident):
    return _sha(("cost-snapshot-audit:" + ident).encode("ascii"))[:16]


def _stamp(s):
    return s.st_dev,s.st_ino,s.st_mode,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns


@contextmanager
def _fd(path, flags, parent=None):
    try:
        fd = os.open(path,flags | os.O_NOFOLLOW | os.O_CLOEXEC,dir_fd=parent)
    except (OSError,ValueError,AttributeError):
        raise Refuse("unsafe_or_missing_input") from None
    try:
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _isolated_root(raw):
    _need(type(raw) is str and raw.startswith("/") and not raw.startswith("//") and
          "\0" not in raw and len(raw)<=4096,"invalid_clone_root")
    parts = PurePosixPath(raw).parts
    _need(len(parts)>=2 and ".." not in parts,"invalid_clone_root")
    with ExitStack() as stack:
        parent=stack.enter_context(_fd("/",os.O_RDONLY|os.O_DIRECTORY))
        # Check every component, not only the leaf or the snapshot file.
        for part in parts[1:]:
            parent=stack.enter_context(_fd(part,os.O_RDONLY|os.O_DIRECTORY,parent))
        yield parent


def _read(parent,name,maximum,device):
    with _fd(name,os.O_RDONLY|os.O_NONBLOCK,parent) as fd:
        first=os.fstat(fd)
        _need(stat.S_ISREG(first.st_mode) and first.st_nlink==1 and first.st_dev==device,
              "unsafe_file_type")
        _need(0<first.st_size<=maximum,"file_size_out_of_bounds")
        with os.fdopen(os.dup(fd),"rb") as handle:
            data=handle.read(maximum+1)
        final=os.fstat(fd)
        _need(len(data)==first.st_size and _stamp(first)==_stamp(final),"file_changed_during_read")
        return data,_stamp(final)


def _pairs(pairs):
    result={}
    for key,value in pairs:
        _need(key not in result,"invalid_json")
        result[key]=value
    return result


def _bad_constant(_):
    raise Refuse("invalid_json")


def _parse(raw):
    return json.loads(raw.decode("utf-8"),object_pairs_hook=_pairs,parse_constant=_bad_constant)


def _manifest(root,required_digest,device):
    data,stamp=_read(root,_MANIFEST,_MAX_MANIFEST_BYTES,device)
    _need(_sha(data)==required_digest,"manifest_digest_mismatch")
    try:
        m=_parse(data)
    except (ValueError,UnicodeError,RecursionError):
        raise Refuse("invalid_manifest") from None
    _need(type(m) is dict and m.keys()=={"version","purpose","snapshots"} and type(m["version"]) is int
          and m["version"]==1 and m["purpose"]=="isolated_snapshot_clone","invalid_manifest")
    rs=m["snapshots"]
    _need(type(rs) is list and 0<len(rs)<=_MAX_RECORDS,"record_count_out_of_bounds")
    total=0
    entries={}
    for r in rs:
        _need(type(r) is dict and r.keys()=={"id","sha256","bytes"} and type(r["id"]) is str
              and _ID.fullmatch(r["id"]) and r["id"] not in entries and type(r["sha256"]) is str
              and _SHA.fullmatch(r["sha256"]) and type(r["bytes"]) is int
              and 0<r["bytes"]<=_MAX_FILE_BYTES,"invalid_manifest_record")
        entries[r["id"]]=(r["bytes"],r["sha256"])
        total+=r["bytes"]
    _need(total<=_MAX_TOTAL_BYTES,"aggregate_size_out_of_bounds")
    return entries,stamp


def _population(folder,entries):
    with os.scandir(folder) as children:
        ids=set()
        for child in children:
            name=child.name
            _need(name.endswith(".json") and len(name)==37 and _ID.fullmatch(name[:-5])
                  and name[:-5] in entries and child.is_file(follow_symlinks=False),
                  "unexpected_or_unsafe_snapshot_entry")
            ids.add(name[:-5])
            _need(len(ids)<=_MAX_RECORDS,"record_count_out_of_bounds")
        return ids


def audit(clone_root,manifest_sha256):
    report={"schema":"isolated-cost-snapshot-audit/1","ok":False,"code":"unverified",
            "expected":0,"validated":0,"affected_count":0,"affected_ids":[]}
    bad=set()
    try:
        _need(type(manifest_sha256) is str and bool(_SHA.fullmatch(manifest_sha256)),
              "invalid_manifest_pin")
        with _isolated_root(clone_root) as root:
            device=os.fstat(root).st_dev
            entries,manifest_stamp=_manifest(root,manifest_sha256,device)
            report["expected"]=len(entries)
            with _fd(_SNAPSHOTS,os.O_RDONLY|os.O_DIRECTORY,root) as sf:
                _need(os.fstat(sf).st_dev==device,"unsafe_snapshot_directory")
                initial_dir=_stamp(os.fstat(sf))
                observed=_population(sf,entries)
                bad.update(entries.keys()-observed)
                stamps={}
                for ident in sorted(observed):
                    size,digest=entries[ident]
                    try:
                        data,file_stamp=_read(sf,ident+".json",size,device)
                        _need(len(data)==size and _sha(data)==digest,"snapshot_digest_mismatch")
                        validate_snapshot_integrity(_parse(data),ident)
                        stamps[ident]=file_stamp
                        report["validated"]+=1
                    except (Refuse,ValueError,KeyError,TypeError,AttributeError,ArithmeticError,
                            RecursionError,IndexError,OSError):
                        bad.add(ident)
                # Refuse file replacement and any byte changes over the entire
                # scan. Never open unmanifested files, or persist anything.
                for ident,old_stamp in stamps.items():
                    size,digest=entries[ident]
                    try:
                        data,new_stamp=_read(sf,ident+".json",size,device)
                        _need(old_stamp==new_stamp and len(data)==size and _sha(data)==digest,
                              "snapshot_changed")
                    except (Refuse,OSError):
                        bad.add(ident)
                        report["validated"]-=1
                final_population=_population(sf,entries)
                current_dir=os.stat(_SNAPSHOTS,dir_fd=root,follow_symlinks=False)
                _,new_manifest_stamp=_manifest(root,manifest_sha256,device)
                _need(final_population==observed and initial_dir==_stamp(current_dir)
                      and new_manifest_stamp==manifest_stamp,"clone_changed_during_audit")
        _need(not bad,"snapshot_integrity_failure")
        report.update(ok=True,code="ok")
    except Refuse as err:
        report["code"]=err.code
    except (OSError,ValueError,TypeError,RecursionError,AttributeError,KeyError):
        report["code"]="invalid_or_unreadable_clone"
    report["affected_count"]=len(bad)
    report["affected_ids"]=sorted(map(_token,bad))[:32]
    return report


class _Parser(ArgumentParser):
    def error(self,message):
        self.exit(2,'{"ok":false,"code":"invalid_options"}\n')


def main(argv=None):
    p=_Parser(description=__doc__)
    p.add_argument("--clone-root",required=True)
    p.add_argument("--manifest-sha256",required=True)
    args=p.parse_args(argv)
    report=audit(args.clone_root,args.manifest_sha256)
    print(json.dumps(report,sort_keys=True,separators=(",",":")))
    return 0 if report["ok"] else 1


if __name__=="__main__":
    raise SystemExit(main())
