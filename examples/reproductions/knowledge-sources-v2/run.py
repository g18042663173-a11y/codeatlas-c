#!/usr/bin/env python3
"""Run public C fixtures offline; print capture JSON, never rewrite frozen records.

The only filesystem writes are compiler outputs inside TemporaryDirectory.
All source paths/commands in the capture are project-relative; BUILD denotes the
isolated temporary directory. No stack/OS/network behavior is mocked.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
REVISIONS = {"cjson": "fb16e5cf358798aabb049655975cde8427101056",
             "lwip": "3d896ba0a37ff3ce73270ca5e230707fe47f60e3"}
CASES = {
    "cjson-parse-end": ("cjson", ["cJSON_ParseWithLengthOpts"]),
    "cjson-string-mutation": ("cjson", ["cJSON_SetValuestring"]),
    "cjson-duplicate": ("cjson", ["cJSON_Duplicate", "cJSON_Duplicate_rec"]),
    "cjson-replace-object": ("cjson", ["replace_item_in_object", "cJSON_ReplaceItemViaPointer"]),
    "cjson-compare": ("cjson", ["cJSON_Compare"]),
    "cjson-unicode": ("cjson", ["parse_hex4", "utf16_literal_to_utf8", "parse_string"]),
    "lwip-address-forms": ("lwip", ["ip4addr_aton", "ip4addr_ntoa_r", "lwip_htonl"]),
    "lwip-pbuf-header": ("lwip", ["pbuf_add_header_impl", "pbuf_remove_header", "pbuf_header_impl", "pbuf_header", "pbuf_header_force"]),
    "lwip-pbuf-chain-ref": ("lwip", ["pbuf_cat", "pbuf_chain", "pbuf_ref"]),
    "lwip-pbuf-copy": ("lwip", ["pbuf_copy_partial", "pbuf_get_contiguous", "pbuf_skip_const"]),
    "lwip-pbuf-take": ("lwip", ["pbuf_take_at", "pbuf_take", "pbuf_skip"]),
    "lwip-checksum-fragments": ("lwip", ["inet_chksum", "inet_chksum_pbuf", "lwip_standard_chksum#2"]),
}


def sha(value):
    return hashlib.sha256(value).hexdigest()


def descriptor(path):
    return {"path": str(path.relative_to(ROOT)), "sha256": sha(path.read_bytes())}


def source_files(cid):
    if cid.startswith("cjson-"):
        return [ROOT / "corpus/cJSON/cJSON.c"]
    if cid == "lwip-address-forms":
        return [ROOT / "corpus/lwip/src/core/ipv4/ip4_addr.c", ROOT / "corpus/lwip/src/core/def.c"]
    if cid == "lwip-checksum-fragments":
        return [ROOT / "corpus/lwip/src/core/inet_chksum.c"]
    return [ROOT / "corpus/lwip/src/core/pbuf.c"]


def anchors(cid):
    rows = []
    for name in CASES[cid][1]:
        symbol, _, ordinal = name.partition("#")
        matches = []
        for path in source_files(cid):
            lines = path.read_text().splitlines(keepends=True)
            for index, line in enumerate(lines):
                if not re.match(r"[A-Za-z_]", line) or line.rstrip().endswith(";"):
                    continue
                if re.search(r"\b" + re.escape(symbol) + r"\s*\(", line):
                    start = index
                    if re.match(re.escape(symbol) + r"\s*\(", line):
                        # lwIP splits return type/qualifiers from the function
                        # name. Include those declaration lines, not only its
                        # parameter list and body; comments/directives stop us.
                        while start > 0 and re.fullmatch(r"[A-Za-z_][\w\s*]*", lines[start-1].strip()):
                            start -= 1
                        if start == index:
                            raise ValueError("definition return type not found: " + symbol)
                    end = next((i for i in range(index + 1, len(lines)) if lines[i].startswith("}")), None)
                    if end is None:
                        raise ValueError("definition end not found: " + symbol)
                    matches.append({**descriptor(path), "symbol": symbol, "line_start": start+1,
                                    "line_end": end+1, "text": "".join(lines[start:end+1])})
        if ordinal:
            matches = [matches[int(ordinal)-1]]
        if len(matches) != 1:
            raise ValueError(f"ambiguous source definition {name}: {len(matches)}")
        row = matches[0]
        row["content_hash"] = sha(row["text"].encode())
        rows.append(row)
    return rows


def capture(argv, cwd):
    result = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    return {"argv": argv, "cwd": "PROJECT_ROOT", "stdin": "", "exit_code": result.returncode,
            "stdout": result.stdout.decode("utf-8", errors="replace"),
            "stderr": result.stderr.decode("utf-8", errors="replace"),
            "stdout_sha256": sha(result.stdout), "stderr_sha256": sha(result.stderr)}


def check_pinned(path, corpus, revision):
    relative = str(path.relative_to(corpus))
    committed = subprocess.check_output(["git", "-C", str(corpus), "show", f"{revision}:{relative}"])
    if path.read_bytes() != committed:
        raise ValueError("source bytes differ from pinned revision: " + relative)


def material(record):
    """Unabridged relevant source, fixture, config and actual execution trace.

    Hash-index JSON and compiler version text remain audit metadata, not padded
    model material. System opt.h is hash-bound, not copied wholesale: the exact
    unit configuration and the selected definitions are provided in full.
    """
    text = [f"Repository: {record['repository']}\nRevision: {record['revision']}\n",
            "Input model: compiled C literals; empty argv and stdin.\n",
            "Fixture:\n" + (ROOT / record["input"]["fixture"]["path"]).read_text()]
    for item in record["build_config"]["files"]:
        if item["path"].endswith("/lwipopts.h"):
            text.append("Fixed configuration: " + item["path"] + "\n" + (ROOT / item["path"]).read_text())
    for anchor in record["anchors"]:
        text.append(f"Source: {anchor['path']}:{anchor['line_start']}-{anchor['line_end']}\n{anchor['text']}")
    for step in record["steps"]:
        text.append("Command: " + shlex.join(step["argv"]) + "\nstdin: <empty>\n"
                    + f"exit: {step['exit_code']}\nstdout:\n{step['stdout']}stderr:\n{step['stderr']}")
    prior = HERE / record["id"] / "initial-failure.json"
    if prior.exists():
        failure = json.loads(prior.read_text())
        last = failure["steps"][-1]
        text.append("Preserved initial failure (not the final result):\n"
                    + "Command: " + shlex.join(last["argv"]) + "\n"
                    + f"exit: {last['exit_code']}\nstdout:\n{last['stdout']}stderr:\n{last['stderr']}")
        prior_fixture = prior.with_name("initial-failure.c")
        if prior_fixture.exists():
            text.append("Initial failed fixture (superseded locally, not erased):\n" + prior_fixture.read_text())
    return "\n\n".join(text) + "\n"


def run(cid):
    repo, _ = CASES[cid]
    corpus = ROOT / "corpus" / ("cJSON" if repo == "cjson" else "lwip")
    revision = subprocess.check_output(["git", "-C", str(corpus), "rev-parse", "HEAD"], text=True).strip()
    if revision != REVISIONS[repo]:
        raise ValueError("pinned revision changed")
    files = source_files(cid)
    fixture = HERE / cid / "reproduce.c"
    compiler = os.environ.get("CC", "cc")
    flags = ["-std=c99", "-Wall", "-Wextra", "-ffunction-sections", "-fdata-sections"]
    if repo == "cjson":
        flags += ["-Icorpus/cJSON"]
        config = [ROOT / "corpus/cJSON/cJSON.h"]
    else:
        flags += ["-DLWIP_DEBUG", "-Icorpus/lwip/src/include",
                  "-Icorpus/lwip/contrib/ports/unix/port/include", "-Icorpus/lwip/test/unit"]
        config = [ROOT / "corpus/lwip/test/unit/lwipopts.h",
                  ROOT / "corpus/lwip/src/include/lwip/opt.h",
                  ROOT / "corpus/lwip/contrib/ports/unix/port/include/arch/cc.h"]
    record = {"schema_version": 2, "artifact_kind": "public_source_reproduction", "id": cid,
              "repository": repo, "revision": revision, "status": "reproducibility_blocked",
              "environment": {"system": platform.system(), "machine": platform.machine(),
                              "compiler": capture([compiler, "--version"], ROOT)},
              "input": {"kind": "compiled_fixture", "fixture": descriptor(fixture), "argv": [],
                        "stdin": "", "description": "Exact input bytes and operation sequences are C literals in the hash-bound fixture; no generated input or network."},
              "build_config": {"flags": flags, "files": [descriptor(p) for p in config],
                               "compile_commands": descriptor(corpus / "compile_commands.json")},
              "sources": [descriptor(p) for p in files], "anchors": anchors(cid), "steps": [],
              "runner": descriptor(Path(__file__).resolve())}
    for path in files + config:
        check_pinned(path, corpus, revision)
    dependencies = {}
    with tempfile.TemporaryDirectory(prefix="codeatlas-sources-v2-") as temporary:
        objects = []
        for index, path in enumerate(files + [fixture]):
            obj = str(Path(temporary) / f"part-{index}.o")
            dep = str(Path(temporary) / f"part-{index}.d")
            command = [compiler, *flags, "-MMD", "-MF", dep, "-c", str(path.relative_to(ROOT)), "-o", obj]
            step = capture(command, ROOT)
            step["argv"] = [word.replace(temporary, "BUILD") for word in step["argv"]]
            record["steps"].append(step)
            if step["exit_code"]:
                record["blocked_phase"] = "compile"
                return record
            body = Path(dep).read_text().partition(":")[2].replace("\\\n", " ")
            for name in shlex.split(body):
                dependency = (ROOT / name).resolve()
                if corpus in dependency.parents:
                    if str(dependency) not in dependencies:
                        check_pinned(dependency, corpus, revision)
                    dependencies[str(dependency)] = descriptor(dependency)
            objects.append(obj)
        exe = str(Path(temporary) / "reproduce")
        linker = ["-Wl,-dead_strip"] if platform.system() == "Darwin" else ["-Wl,--gc-sections"]
        step = capture([compiler, *linker, *objects, "-lm", "-o", exe], ROOT)
        step["argv"] = [word.replace(temporary, "BUILD") for word in step["argv"]]
        record["steps"].append(step)
        if step["exit_code"]:
            record["blocked_phase"] = "link"
            return record
        step = capture([exe], ROOT)
        step["argv"] = ["BUILD/reproduce"]
        record["steps"].append(step)
        record["status"] = "reproduced" if step["exit_code"] == 0 else "reproducibility_blocked"
        if step["exit_code"]:
            record["blocked_phase"] = "run"
    record["dependencies"] = list(dependencies.values())
    record["material_text"] = material(record)
    record["material_utf8_bytes"] = len(record["material_text"].encode())
    record["material_estimated_tokens"] = (record["material_utf8_bytes"]+2)//3
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=[*CASES, "all"])
    options = parser.parse_args()
    rows = [run(cid) for cid in CASES] if options.case == "all" else [run(options.case)]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
