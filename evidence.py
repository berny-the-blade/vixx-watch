"""Court-grade tamper-evidence for the vixx-watch monitor.

Tamper-evidence model
---------------------
Every run that produces artifacts (crawl output, archives, news captures) is
sealed with four independent layers, so that after-the-fact alteration of any
captured byte can be detected by an external party:

1. Per-artifact SHA-256
   Each file produced/sealed by a run is hashed with SHA-256 over its raw
   bytes. The hash, byte size and (BASE_DIR-relative) path are recorded in the
   run's manifest entry. Changing one byte of any artifact changes its hash.

2. Hash chain (internal tamper-evidence)
   Run entries are linked: chain_n = SHA-256(chain_{n-1} + canonical_json(entry_n)).
   The first run chains off 64 zero hex chars (genesis). Because each link
   commits to the previous link AND to the full canonical content of the current
   entry, you cannot edit, reorder, insert or delete any past entry (or any
   recorded artifact hash) without breaking every subsequent link. The chain is
   mirrored in an append-only ledger (ledger.txt) and in manifest.jsonl.

3. External OTS anchor (independent time witness)
   Each per-run manifest file (runs/<seq>-<run_id>.json) is timestamped with the
   OpenTimestamps client (`ots stamp`), producing a `.ots` proof that, once
   upgraded, anchors the file's hash into the Bitcoin blockchain. This proves the
   evidence existed at-or-before a given block time, defeating back-dating.

4. External private-repo witness (off-host custody)
   EVIDENCE_DIR is itself a separate private git repository (created and pushed
   elsewhere). Committing/pushing the append-only manifest, ledger and per-run
   files to a remote held by a third party means an attacker who controls this
   host cannot silently rewrite history: the remote retains the prior chain tips.

provenance() additionally records how each run was produced (tracker commit,
working-tree cleanliness, machine, OS, Python, TLS posture) for attribution and
reproducibility. Note: the crawler talks to vixx.vn with TLS verification
disabled (expired cert), so tls_verify_disabled is recorded honestly as True.

This module is stdlib-only and does NOT import vixx_watch (it defines its own
constants) so that evidence sealing has no dependency on, and cannot be perturbed
by, the thing it is auditing. Run `python evidence.py` to verify the whole chain.
"""

import hashlib
import json
import os
import platform
import shutil
import subprocess
import urllib.request

# Free RFC-3161 Time-Stamp Authority (trusted, independent, court-recognized).
TSA_URL = "https://freetsa.org/tsr"

# --- Constants (self-contained; do NOT import vixx_watch) -------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EVIDENCE_DIR = os.path.join(BASE_DIR, "evidence")
MANIFEST_PATH = os.path.join(EVIDENCE_DIR, "manifest.jsonl")
LEDGER_PATH = os.path.join(EVIDENCE_DIR, "ledger.txt")
RUNS_DIR = os.path.join(EVIDENCE_DIR, "runs")

GENESIS_CHAIN = "0" * 64
_CHUNK = 1 << 16  # 64 KiB streaming read


def _canonical_json(obj):
    """Deterministic JSON serialization used for all hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_file(path):
    """SHA-256 hex of the file's raw bytes (streamed). '' if unreadable."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_CHUNK), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _git(*args):
    """Run a git command in BASE_DIR; return stripped stdout or None on failure."""
    try:
        out = subprocess.run(
            ["git", "-C", BASE_DIR, *args],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def provenance():
    """Capture how this run was produced (attribution / reproducibility)."""
    head = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    return {
        "tracker_commit": head.strip() if head is not None else "unknown",
        "tracker_dirty": bool(status.strip()) if status is not None else False,
        "machine": platform.node(),
        "os": platform.platform(),
        "python": platform.python_version(),
        "tls_verify_disabled": True,  # crawler uses CERT_NONE for expired vixx.vn cert
        "captured_by": "vixx-watch",
    }


def _read_ledger_tail():
    """Return (last_seq:int, last_chain:str) from ledger.txt, or (0, GENESIS)."""
    last_seq, last_chain = 0, GENESIS_CHAIN
    try:
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                try:
                    seq = int(parts[0])
                except ValueError:
                    continue
                last_seq, last_chain = seq, parts[1]
    except OSError:
        pass
    return last_seq, last_chain


def _rel(path):
    """Path relative to BASE_DIR (falls back to given path)."""
    try:
        return os.path.relpath(os.path.abspath(path), BASE_DIR).replace(os.sep, "/")
    except ValueError:
        return path


def record_run(run_type, artifact_paths, extra=None):
    """Seal a run: hash artifacts, extend the hash chain, persist evidence.

    run_type: "crawl" | "archive" | "news" | ...
    artifact_paths: files produced/sealed this run (abs or BASE_DIR-relative).
    extra: optional dict merged into the entry. MUST supply extra["run_id"]
           (a UTC compact stamp). May supply extra["ts"]. Other keys are merged
           in (e.g. wayback third-party timestamps). Returns the manifest entry.
    """
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    os.makedirs(RUNS_DIR, exist_ok=True)

    extra = dict(extra) if extra else {}
    last_seq, prev_chain = _read_ledger_tail()
    seq = last_seq + 1

    run_id = extra.pop("run_id", None)
    if not run_id:
        run_id = "unknownrun" + str(seq)
    ts = extra.pop("ts", None)

    artifacts = []
    for p in artifact_paths:
        ap = p if os.path.isabs(p) else os.path.join(BASE_DIR, p)
        if not os.path.exists(ap):
            continue
        try:
            size = os.path.getsize(ap)
        except OSError:
            size = 0
        artifacts.append({"path": _rel(ap), "sha256": sha256_file(ap), "bytes": size})

    entry = {
        "seq": seq,
        "run_id": run_id,
        "run_type": run_type,
        "ts": ts,
        "provenance": provenance(),
        "artifacts": artifacts,
    }
    # Merge remaining extra keys (run_id/ts already popped).
    for k, v in extra.items():
        entry[k] = v

    chain = hashlib.sha256(
        (prev_chain + _canonical_json(entry)).encode("utf-8")
    ).hexdigest()
    entry["prev_chain"] = prev_chain
    entry["chain"] = chain

    with open(MANIFEST_PATH, "a", encoding="utf-8") as f:
        f.write(_canonical_json(entry) + "\n")
    with open(LEDGER_PATH, "a", encoding="utf-8") as f:
        f.write("{}\t{}\t{}\n".format(seq, chain, run_id))

    run_file = os.path.join(RUNS_DIR, "{}-{}.json".format(seq, run_id))
    with open(run_file, "w", encoding="utf-8") as f:
        json.dump(entry, f, indent=2, ensure_ascii=False, sort_keys=True)

    return entry


def _ots_cmd():
    """Resolve the `ots` executable (PATH, then the interpreter's Scripts dir)."""
    import shutil
    import sysconfig
    found = shutil.which("ots")
    if found:
        return found
    for name in ("ots.exe", "ots"):
        cand = os.path.join(sysconfig.get_path("scripts") or "", name)
        if os.path.exists(cand):
            return cand
    return None


def ots_available():
    """True if the OpenTimestamps client (`ots`) is callable."""
    cmd = _ots_cmd()
    if not cmd:
        return False
    try:
        subprocess.run([cmd, "--help"], capture_output=True, text=True, timeout=15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def ots_stamp(path):
    """Best-effort OTS timestamp: `ots stamp <path>` -> <path>.ots. Never raises."""
    cmd = _ots_cmd()
    if not cmd:
        return False
    try:
        out = subprocess.run(
            [cmd, "stamp", path], capture_output=True, text=True, timeout=90
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and os.path.exists(path + ".ots")


def tsa_available():
    """True if the openssl CLI is present (needed to build RFC-3161 requests)."""
    return bool(shutil.which("openssl"))


def tsa_stamp(path, tsa_url=TSA_URL):
    """RFC-3161 trusted timestamp: builds a SHA-256 timestamp request with openssl,
    POSTs it to a free TSA, writes the signed token to <path>.tsr. Never raises.
    The .tsr cryptographically binds the file's hash to an independent trusted
    time (verifiable later with `openssl ts -verify`)."""
    openssl = shutil.which("openssl")
    if not openssl:
        return False
    tsq = path + ".tsq"
    try:
        q = subprocess.run(
            [openssl, "ts", "-query", "-data", path, "-sha256", "-cert",
             "-no_nonce", "-out", tsq],
            capture_output=True, timeout=30)
        if q.returncode != 0 or not os.path.exists(tsq):
            return False
        with open(tsq, "rb") as f:
            body = f.read()
        req = urllib.request.Request(
            tsa_url, data=body,
            headers={"Content-Type": "application/timestamp-query",
                     "User-Agent": "vixx-watch"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            tsr = resp.read()
        if not tsr:
            return False
        with open(path + ".tsr", "wb") as f:
            f.write(tsr)
        return os.path.getsize(path + ".tsr") > 0
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            os.remove(tsq)
        except OSError:
            pass


def verify():
    """Re-verify the full chain and all artifact hashes from disk."""
    result = {
        "ok": True,
        "runs": 0,
        "broken_chain_at": None,
        "altered_artifacts": [],
        "missing_artifacts": [],
    }

    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            lines = [ln for ln in (l.rstrip("\n") for l in f) if ln]
    except OSError:
        # No manifest yet => an empty, intact chain.
        return result

    prev_chain = GENESIS_CHAIN
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            result["ok"] = False
            result["broken_chain_at"] = result["runs"] + 1
            break

        result["runs"] += 1
        seq = entry.get("seq")

        recorded_chain = entry.get("chain")
        recorded_prev = entry.get("prev_chain")
        core = {k: v for k, v in entry.items() if k not in ("prev_chain", "chain")}
        expected = hashlib.sha256(
            (recorded_prev + _canonical_json(core)).encode("utf-8")
        ).hexdigest()

        if recorded_prev != prev_chain or expected != recorded_chain:
            result["ok"] = False
            if result["broken_chain_at"] is None:
                result["broken_chain_at"] = seq

        for art in entry.get("artifacts", []):
            ap = os.path.join(BASE_DIR, art["path"])
            if not os.path.exists(ap):
                result["ok"] = False
                result["missing_artifacts"].append(
                    {"seq": seq, "path": art["path"]}
                )
                continue
            if sha256_file(ap) != art.get("sha256"):
                result["ok"] = False
                result["altered_artifacts"].append(
                    {"seq": seq, "path": art["path"]}
                )

        prev_chain = recorded_chain

    return result


if __name__ == "__main__":
    import json, sys
    print(json.dumps(verify(), indent=2))
