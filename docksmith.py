#!/usr/bin/env python3
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

BASE_DIR = os.path.expanduser("~/.docksmith")
LAYER_DIR = os.path.join(BASE_DIR, "layers")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
BASE_IMAGE_DIR = os.path.join(BASE_DIR, "base")
IMAGES_DIR = os.path.join(BASE_DIR, "images")

DEFAULT_IMAGE_NAME = "docksmith"
DEFAULT_IMAGE_TAG = "latest"

os.makedirs(LAYER_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)

CURRENT_WORKDIR = "/"
CURRENT_ENV = {}


# -------- CACHE KEY --------
def compute_cache_key(prev_hash, instruction):
    env_string = "".join([f"{k}={v}" for k, v in sorted(CURRENT_ENV.items())])
    data = (prev_hash + instruction + CURRENT_WORKDIR + env_string).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


# -------- PARSE FILE --------
def parse_docksmithfile():
    instructions = []
    with open("Docksmithfile", encoding="utf-8") as f:
        for lineno, raw_line in enumerate(f, 1):
            line_no_nl = raw_line.rstrip("\n")
            stripped = line_no_nl.strip()
            if not stripped or stripped.startswith("#"):
                continue

            parts = stripped.split(maxsplit=1)
            cmd = parts[0]
            args = parts[1] if len(parts) > 1 else ""
            instructions.append((cmd, args, line_no_nl, lineno))
    return instructions


# -------- HASH FILE --------
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# -------- CREATE LAYER --------
def create_layer(src):
    tar_name = "layer.tar"
    with tarfile.open(tar_name, "w") as tar:
        for root, dirs, files in os.walk(src):
            dirs.sort()
            files.sort()

            rel_root = os.path.relpath(root, src)
            if rel_root != ".":
                tar.add(root, arcname=rel_root, recursive=False)

            for file_name in files:
                full_path = os.path.join(root, file_name)
                arcname = os.path.relpath(full_path, src)
                tar.add(full_path, arcname=arcname, recursive=False)

    digest = sha256_file(tar_name)
    final = os.path.join(LAYER_DIR, f"{digest}.tar")
    shutil.move(tar_name, final)
    size = os.path.getsize(final)
    return digest, size


# -------- GET BASE IMAGE --------
def get_base_archive():
    if not os.path.isdir(BASE_IMAGE_DIR):
        raise Exception("Base image directory missing")

    for file_name in sorted(os.listdir(BASE_IMAGE_DIR)):
        if file_name.endswith(".tar.gz"):
            return os.path.join(BASE_IMAGE_DIR, file_name)
    raise Exception("Base image missing")


# -------- BASE MANIFEST --------
def load_base_manifest():
    if not os.path.isdir(BASE_IMAGE_DIR):
        return None

    candidates = [os.path.join(BASE_IMAGE_DIR, "manifest.json")]
    for file_name in sorted(os.listdir(BASE_IMAGE_DIR)):
        if file_name.endswith(".json") and file_name != "manifest.json":
            candidates.append(os.path.join(BASE_IMAGE_DIR, file_name))

    for candidate in candidates:
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("layers"), list):
                return data
        except (OSError, json.JSONDecodeError):
            continue

    return None


# -------- LAYER ENTRY NORMALIZER --------
def normalize_layer_entry(entry, fallback_created_by=""):
    if isinstance(entry, dict):
        digest = str(entry.get("digest", "")).replace("sha256:", "")
        size = int(entry.get("size", 0)) if str(entry.get("size", "0")).isdigit() else 0
        created_by = str(entry.get("createdBy", fallback_created_by))
        return {
            "digest": digest,
            "size": size,
            "createdBy": created_by,
        }

    digest = str(entry).replace("sha256:", "")
    layer_path = os.path.join(LAYER_DIR, f"{digest}.tar")
    size = os.path.getsize(layer_path) if os.path.exists(layer_path) else 0
    return {
        "digest": digest,
        "size": size,
        "createdBy": fallback_created_by,
    }


# -------- INHERITED LAYERS --------
def get_inherited_layers():
    base_manifest = load_base_manifest()
    if not base_manifest:
        return []

    inherited = []
    for layer in base_manifest.get("layers", []):
        inherited.append(normalize_layer_entry(layer, fallback_created_by="FROM base"))
    return inherited


# -------- APPLY LAYERS --------
def apply_layer_digests(root, digests):
    for digest in digests:
        if not digest:
            continue
        layer_path = os.path.join(LAYER_DIR, digest + ".tar")
        if not os.path.exists(layer_path):
            continue
        subprocess.run(["tar", "-xf", layer_path, "-C", root], check=False)


# -------- MANIFEST HELPERS --------
def manifest_path(name, tag):
    return os.path.join(IMAGES_DIR, f"{name}:{tag}.json")


def now_utc_timestamp():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_manifest_bytes(manifest_obj):
    return json.dumps(
        manifest_obj,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8")


def build_manifest(name, tag, created, config_env, config_cmd, config_workdir, layers):
    manifest = {
        "name": name,
        "tag": tag,
        "digest": "",
        "created": created,
        "config": {
            "Env": [f"{k}={v}" for k, v in sorted(config_env.items())],
            "Cmd": config_cmd,
            "WorkingDir": config_workdir,
        },
        "layers": layers,
    }

    digest_hash = hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()
    manifest["digest"] = f"sha256:{digest_hash}"
    return manifest


def read_existing_manifest(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        return None
    return None


def normalize_runtime_cmd(cmd, root, workdir):
    if not isinstance(cmd, list) or len(cmd) < 2:
        return cmd

    target = cmd[1]
    if not isinstance(target, str) or not target.startswith("/"):
        return cmd

    absolute_path = os.path.join(root, target.lstrip("/"))
    if os.path.exists(absolute_path):
        return cmd

    candidate = os.path.join(root, workdir.lstrip("/"), os.path.basename(target))
    if os.path.exists(candidate):
        patched = list(cmd)
        patched[1] = os.path.basename(target)
        return patched

    return cmd


# -------- BUILD --------
def build():
    global CURRENT_WORKDIR, CURRENT_ENV

    instructions = parse_docksmithfile()
    build_layers = []
    build_layer_digests = []
    prev_hash = ""
    cmd_instruction = []

    CURRENT_WORKDIR = "/"
    CURRENT_ENV = {}

    all_cache_hits = True
    cacheable_step_count = 0

    for cmd, args, instruction_text, lineno in instructions:
        if cmd == "FROM":
            print("[BASE]", instruction_text)
            continue

        if cmd == "WORKDIR":
            CURRENT_WORKDIR = args.strip() or "/"
            print("[WORKDIR SET]", CURRENT_WORKDIR)
            continue

        if cmd == "ENV":
            if "=" not in args:
                print(f"Error: ENV must be KEY=VALUE at line {lineno}")
                sys.exit(1)
            key, value = args.split("=", 1)
            CURRENT_ENV[key.strip()] = value.strip()
            print("[ENV SET]", key.strip(), "=", value.strip())
            continue

        if cmd == "CMD":
            try:
                parsed_cmd = json.loads(args)
            except json.JSONDecodeError:
                print(f"Error: CMD must be a JSON array at line {lineno}")
                sys.exit(1)
            if not isinstance(parsed_cmd, list) or not all(isinstance(x, str) for x in parsed_cmd):
                print(f"Error: CMD must be a JSON array of strings at line {lineno}")
                sys.exit(1)
            cmd_instruction = parsed_cmd
            print("[CMD SET]", cmd_instruction)
            continue

        if cmd not in {"COPY", "RUN"}:
            print(f"[WARN] Unsupported instruction '{cmd}' at line {lineno}, skipping")
            continue

        cacheable_step_count += 1
        cache_key = compute_cache_key(prev_hash, instruction_text)
        cache_file = os.path.join(CACHE_DIR, cache_key)

        if os.path.exists(cache_file):
            print("[CACHE HIT]", instruction_text)
            with open(cache_file, encoding="utf-8") as f:
                layer_digest = f.read().strip()
            layer_path = os.path.join(LAYER_DIR, f"{layer_digest}.tar")
            layer_size = os.path.getsize(layer_path) if os.path.exists(layer_path) else 0
        else:
            print("[CACHE MISS]", instruction_text)
            all_cache_hits = False

            temp_root = tempfile.mkdtemp(dir="/tmp")
            try:
                subprocess.run(["tar", "-xzf", get_base_archive(), "-C", temp_root], check=False)
                apply_layer_digests(temp_root, build_layer_digests)

                if cmd == "COPY":
                    copy_parts = args.split(maxsplit=1)
                    if len(copy_parts) != 2:
                        print(f"Error: COPY requires <src> <dest> at line {lineno}")
                        sys.exit(1)
                    src, dest = copy_parts

                    # COPY destination is relative to WORKDIR when not absolute.
                    if dest.startswith("/"):
                        normalized_dest = dest
                    else:
                        normalized_dest = os.path.join(CURRENT_WORKDIR, dest)

                    full_dest = os.path.join(temp_root, normalized_dest.lstrip("/"))
                    os.makedirs(os.path.dirname(full_dest) or temp_root, exist_ok=True)
                    shutil.copy(src, full_dest)

                elif cmd == "RUN":
                    os.makedirs(
                        os.path.join(temp_root, CURRENT_WORKDIR.lstrip("/")),
                        exist_ok=True,
                    )
                    env_string = " ".join([f"{k}='{v}'" for k, v in CURRENT_ENV.items()])
                    shell_cmd = f"{env_string} cd {CURRENT_WORKDIR} && {args}".strip()
                    subprocess.run(["chroot", temp_root, "/bin/sh", "-c", shell_cmd], check=False)

                layer_digest, layer_size = create_layer(temp_root)
            finally:
                shutil.rmtree(temp_root, ignore_errors=True)

            with open(cache_file, "w", encoding="utf-8") as f:
                f.write(layer_digest)

        build_layer_digests.append(layer_digest)
        build_layers.append(
            {
                "digest": layer_digest,
                "size": layer_size,
                "createdBy": instruction_text,
            }
        )
        prev_hash = layer_digest

    with open("image_layers.txt", "w", encoding="utf-8") as f:
        for digest in build_layer_digests:
            f.write(digest + "\n")

    name = DEFAULT_IMAGE_NAME
    tag = DEFAULT_IMAGE_TAG
    output_manifest_path = manifest_path(name, tag)

    existing_manifest = read_existing_manifest(output_manifest_path)
    if cacheable_step_count > 0 and all_cache_hits and existing_manifest and existing_manifest.get("created"):
        created_ts = existing_manifest.get("created")
    else:
        created_ts = now_utc_timestamp()

    all_layers = get_inherited_layers() + build_layers
    final_manifest = build_manifest(
        name=name,
        tag=tag,
        created=created_ts,
        config_env=CURRENT_ENV,
        config_cmd=cmd_instruction,
        config_workdir=CURRENT_WORKDIR,
        layers=all_layers,
    )

    with open(output_manifest_path, "w", encoding="utf-8") as f:
        json.dump(final_manifest, f, indent=2)

    # Keep workspace manifest for compatibility with existing scripts/tests.
    with open("manifest.json", "w", encoding="utf-8") as f:
        json.dump(final_manifest, f, indent=2)

    print(f"[✓] Build complete: {output_manifest_path}")


# -------- RUN --------
def run():
    image_manifest_path = manifest_path(DEFAULT_IMAGE_NAME, DEFAULT_IMAGE_TAG)
    manifest = read_existing_manifest(image_manifest_path)

    if not manifest:
        manifest = read_existing_manifest("manifest.json")

    if not manifest:
        print("Error: No image manifest found. Run 'python3 docksmith.py build' first.")
        sys.exit(1)

    cmd = manifest.get("config", {}).get("Cmd", [])
    workdir = manifest.get("config", {}).get("WorkingDir", "/")

    env = os.environ.copy()
    for kv in manifest.get("config", {}).get("Env", []):
        if "=" in kv:
            k, v = kv.split("=", 1)
            env[k] = v

    root = tempfile.mkdtemp(dir="/tmp")
    print("[*] Temp:", root)

    subprocess.run(["tar", "-xzf", get_base_archive(), "-C", root], check=False)

    layer_digests = []
    for layer in manifest.get("layers", []):
        if isinstance(layer, dict):
            layer_digests.append(str(layer.get("digest", "")))
        elif isinstance(layer, str):
            layer_digests.append(layer)

    apply_layer_digests(root, layer_digests)

    os.makedirs(os.path.join(root, workdir.lstrip("/")), exist_ok=True)

    cmd = normalize_runtime_cmd(cmd, root, workdir)

    cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
    if not cmd_str:
        print("Error: No command found in manifest config Cmd")
        shutil.rmtree(root, ignore_errors=True)
        sys.exit(1)

    print("[*] Inside container")
    result = subprocess.run(
        ["chroot", root, "/bin/sh", "-c", f"cd {workdir} && {cmd_str}"],
        env=env,
        check=False,
    )
    print(f"[*] Exit code: {result.returncode}")
    shutil.rmtree(root, ignore_errors=True)


# -------- CLI --------
def main():
    if len(sys.argv) < 2:
        print("Usage: build/run")
        return

    if sys.argv[1] == "build":
        build()
    elif sys.argv[1] == "run":
        run()
    else:
        print("Unknown")


if __name__ == "__main__":
    main()
