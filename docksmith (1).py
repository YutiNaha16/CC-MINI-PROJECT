#!/usr/bin/env python3
import os, sys, tarfile, hashlib, subprocess, tempfile, shutil, json

BASE_DIR = os.path.expanduser("~/.docksmith")
LAYER_DIR = os.path.join(BASE_DIR, "layers")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
BASE_IMAGE_DIR = os.path.join(BASE_DIR, "base")

os.makedirs(LAYER_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

CURRENT_WORKDIR = "/"
CURRENT_ENV = {}


# -------- CACHE KEY --------
def compute_cache_key(prev_hash, instruction, extra_file_hashes=None):
    """
    Cache key includes ALL of:
      1. Previous layer digest (prev_hash)
      2. Full instruction text as written
      3. Current WORKDIR value at instruction time
      4. Current ENV state — all key=value pairs sorted lexicographically by key
      5. COPY only: SHA-256 of each source file's raw bytes, sorted by file path
    """
    env_string = "".join(
        [f"{k}={v}" for k, v in sorted(CURRENT_ENV.items())]
    )
    file_hash_string = ""
    if extra_file_hashes:
        # Sort by file path for determinism
        for path in sorted(extra_file_hashes.keys()):
            file_hash_string += f"{path}:{extra_file_hashes[path]}"

    data = (
        prev_hash
        + instruction
        + CURRENT_WORKDIR
        + env_string
        + file_hash_string
    ).encode()
    return hashlib.sha256(data).hexdigest()


# -------- PARSE FILE --------
def parse_docksmithfile():
    instructions = []
    with open("Docksmithfile") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(maxsplit=1)
            cmd = parts[0]
            args = parts[1] if len(parts) > 1 else ""
            instructions.append((cmd, args, lineno))
    return instructions


# -------- HASH FILE --------
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


# -------- REPRODUCIBLE LAYER --------
def create_layer(src):
    """
    Create a deterministic tar layer from src directory.
    - Entries added in lexicographically sorted path order
    - All file timestamps zeroed to Unix epoch 0
    Both are required for the same source to always produce the same digest.
    """
    tar_name = "layer.tar"

    with tarfile.open(tar_name, "w") as tar:
        # Collect all paths under src, sort lexicographically
        all_paths = []
        for root, dirs, files in os.walk(src):
            dirs.sort()   # ensure os.walk descends in sorted order
            for name in sorted(files):
                all_paths.append(os.path.join(root, name))
            for name in sorted(dirs):
                all_paths.append(os.path.join(root, name))

        all_paths.sort()  # final sort of absolute paths

        seen = set()
        for full_path in all_paths:
            arcname = os.path.relpath(full_path, src)
            if arcname in seen:
                continue
            seen.add(arcname)

            info = tar.gettarinfo(full_path, arcname=arcname)
            # Zero all timestamps for reproducibility
            
            info = tar.gettarinfo(full_path, arcname=arcname)
            info.mtime = 0


            if info.isreg():
                with open(full_path, "rb") as f:
                    tar.addfile(info, f)
            else:
                tar.addfile(info)

    digest = sha256_file(tar_name)
    final = os.path.join(LAYER_DIR, f"{digest}.tar")
    shutil.move(tar_name, final)
    return digest


# -------- GET BASE --------
def get_base():
    for file in os.listdir(BASE_IMAGE_DIR):
        if file.endswith(".tar.gz"):
            return os.path.join(BASE_IMAGE_DIR, file)
    raise Exception("Base image missing")


# -------- APPLY LAYERS --------
def apply_layers(root, layer_list=None):
    if layer_list is not None:
        layers = layer_list
    elif os.path.exists("image_layers.txt"):
        with open("image_layers.txt") as f:
            layers = f.read().splitlines()
    else:
        return

    for l in layers:
        layer_path = os.path.join(LAYER_DIR, l + ".tar")
        if os.path.exists(layer_path):
            subprocess.run(
                ["tar", "-xf", layer_path, "-C", root],
                stderr=subprocess.DEVNULL,
            )
        else:
            print(f"[WARN] Missing layer skipped: {l}")


# -------- PARSE -e FLAGS --------
def parse_env_flags(argv):
    overrides = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "-e":
            if i + 1 < len(argv):
                kv = argv[i + 1]
                if "=" in kv:
                    key, value = kv.split("=", 1)
                    overrides[key.strip()] = value.strip()
                else:
                    print(f"Error: -e flag must be KEY=VALUE, got: {kv}")
                    sys.exit(1)
                i += 2
            else:
                print("Error: -e flag requires KEY=VALUE argument")
                sys.exit(1)
        elif arg.startswith("-e") and len(arg) > 2:
            kv = arg[2:]
            if "=" in kv:
                key, value = kv.split("=", 1)
                overrides[key.strip()] = value.strip()
            else:
                print(f"Error: -e flag must be KEY=VALUE, got: {kv}")
                sys.exit(1)
            i += 1
        else:
            i += 1
    return overrides


def normalize_runtime_cmd(cmd_list, root, workdir):
    if not isinstance(cmd_list, list) or len(cmd_list) < 2:
        return cmd_list

    target = cmd_list[1]
    if not isinstance(target, str) or not target.startswith("/"):
        return cmd_list

    absolute_path = os.path.join(root, target.lstrip("/"))
    if os.path.exists(absolute_path):
        return cmd_list

    candidate = os.path.join(root, workdir.lstrip("/"), os.path.basename(target))
    if os.path.exists(candidate):
        updated = list(cmd_list)
        updated[1] = os.path.basename(target)
        return updated

    return cmd_list


# -------- BUILD --------
def build(no_cache=False):
    global CURRENT_WORKDIR, CURRENT_ENV

    instr = parse_docksmithfile()
    layers = []
    prev_hash = ""
    cmd_instruction = None
    cmd_lineno = None

    CURRENT_WORKDIR = "/"
    CURRENT_ENV = {}

    # ---- Cache cascade flag ----
    # Once any layer-producing step is a cache miss, ALL subsequent steps
    # must also miss regardless of their own cache key.
    force_miss = False

    for cmd, args, lineno in instr:
        instruction_text = cmd + " " + args

        # -------- FROM --------
        if cmd == "FROM":
            print("[CACHE MISS]", instruction_text)
            continue

        # -------- WORKDIR --------
        elif cmd == "WORKDIR":
            # Compute key BEFORE updating WORKDIR
            cache_key = compute_cache_key(prev_hash, instruction_text)
            cache_file = os.path.join(CACHE_DIR, cache_key)

            if (not no_cache) and (not force_miss) and os.path.exists(cache_file):
                print("[CACHE HIT]", instruction_text)
                with open(cache_file) as f:
                    layer_hash = f.read().strip()
                layers.append(layer_hash)
                prev_hash = layer_hash
                CURRENT_WORKDIR = args
                continue

            print("[CACHE MISS]", instruction_text)
            force_miss = True   # cascade: all subsequent steps must miss

            temp_root = tempfile.mkdtemp(dir="/tmp")
            subprocess.run(
                ["tar", "-xzf", get_base(), "-C", temp_root],
                stderr=subprocess.DEVNULL,
            )
            apply_layers(temp_root, layers)

            CURRENT_WORKDIR = args
            os.makedirs(
                os.path.join(temp_root, CURRENT_WORKDIR.lstrip("/")),
                exist_ok=True,
            )

            layer_hash = create_layer(temp_root)
            shutil.rmtree(temp_root, ignore_errors=True)

            with open(cache_file, "w") as f:
                f.write(layer_hash)

            layers.append(layer_hash)
            prev_hash = layer_hash

        # -------- ENV --------
        elif cmd == "ENV":
            if "=" not in args:
                print(f"Error: ENV must be KEY=VALUE at line {lineno}")
                sys.exit(1)
            key, value = args.split("=", 1)
            CURRENT_ENV[key.strip()] = value.strip()
            print("[ENV SET]", key.strip(), "=", value.strip())
            continue

        # -------- CMD --------
        elif cmd == "CMD":
            try:
                parsed = json.loads(args)
                if not isinstance(parsed, list):
                    raise ValueError("CMD must be a JSON array")
                for item in parsed:
                    if not isinstance(item, str):
                        raise ValueError("CMD elements must be strings")
                cmd_instruction = parsed
                cmd_lineno = lineno
            except (json.JSONDecodeError, ValueError) as e:
                print(
                    f"Error on line {lineno}: CMD must be a JSON array like "
                    f'["exec", "arg"] — got: {args}'
                )
                sys.exit(1)
            print("[CMD SET]", cmd_instruction)
            continue

        # -------- COPY --------
        elif cmd == "COPY":
            parts = args.split()
            if len(parts) != 2:
                print(f"Error on line {lineno}: COPY requires exactly <src> <dest>")
                sys.exit(1)
            src, dest = parts

            # Hash source file(s) for cache key — sorted by path
            extra_file_hashes = {}
            if os.path.isfile(src):
                extra_file_hashes[src] = sha256_file(src)
            elif os.path.isdir(src):
                for root_dir, _, files in os.walk(src):
                    for fname in files:
                        fpath = os.path.join(root_dir, fname)
                        extra_file_hashes[fpath] = sha256_file(fpath)

            cache_key = compute_cache_key(
                prev_hash, instruction_text, extra_file_hashes
            )
            cache_file = os.path.join(CACHE_DIR, cache_key)

            if (not no_cache) and (not force_miss) and os.path.exists(cache_file):
                print("[CACHE HIT]", instruction_text)
                with open(cache_file) as f:
                    layer_hash = f.read().strip()
                layers.append(layer_hash)
                prev_hash = layer_hash
                continue

            print("[CACHE MISS]", instruction_text)
            force_miss = True   # cascade

            temp_root = tempfile.mkdtemp(dir="/tmp")
            subprocess.run(
                ["tar", "-xzf", get_base(), "-C", temp_root],
                stderr=subprocess.DEVNULL,
            )
            apply_layers(temp_root, layers)

            # COPY destination is relative to WORKDIR when destination is not absolute.
            if dest.startswith("/"):
                resolved_dest = dest
            else:
                resolved_dest = os.path.join(CURRENT_WORKDIR, dest)

            full_dest = os.path.join(temp_root, resolved_dest.lstrip("/"))
            os.makedirs(os.path.dirname(full_dest) or temp_root, exist_ok=True)
            shutil.copy(src, full_dest)

            layer_hash = create_layer(temp_root)
            shutil.rmtree(temp_root, ignore_errors=True)

            with open(cache_file, "w") as f:
                f.write(layer_hash)

            layers.append(layer_hash)
            prev_hash = layer_hash

        # -------- RUN --------
        elif cmd == "RUN":
            cache_key = compute_cache_key(prev_hash, instruction_text)
            cache_file = os.path.join(CACHE_DIR, cache_key)

            if (not no_cache) and (not force_miss) and os.path.exists(cache_file):
                print("[CACHE HIT]", instruction_text)
                with open(cache_file) as f:
                    layer_hash = f.read().strip()
                layers.append(layer_hash)
                prev_hash = layer_hash
                continue

            print("[CACHE MISS]", instruction_text)
            force_miss = True   # cascade

            temp_root = tempfile.mkdtemp(dir="/tmp")
            subprocess.run(
                ["tar", "-xzf", get_base(), "-C", temp_root],
                stderr=subprocess.DEVNULL,
            )
            apply_layers(temp_root, layers)

            os.makedirs(
                os.path.join(temp_root, CURRENT_WORKDIR.lstrip("/")),
                exist_ok=True,
            )

            env_prefix = " ".join(
                [f"{k}={json.dumps(v)}" for k, v in CURRENT_ENV.items()]
            )
            shell_cmd = f"cd {CURRENT_WORKDIR} && {args}"
            if env_prefix:
                shell_cmd = f"export {env_prefix} && {shell_cmd}"

            result = subprocess.run(
                ["chroot", temp_root, "/bin/sh", "-c", shell_cmd]
            )

            layer_hash = create_layer(temp_root)
            shutil.rmtree(temp_root, ignore_errors=True)

            with open(cache_file, "w") as f:
                f.write(layer_hash)

            layers.append(layer_hash)
            prev_hash = layer_hash

        else:
            print(f"[WARN] Unknown instruction '{cmd}' at line {lineno}, skipping")
            continue

    # -------- SAVE --------
    with open("image_layers.txt", "w") as f:
        for l in layers:
            f.write(l + "\n")

    combined = "".join(layers).encode()
    image_digest = hashlib.sha256(combined).hexdigest()

    manifest = {
        "name": "docksmith",
        "tag": "latest",
        "digest": image_digest,
        "layers": layers,
        "cmd": cmd_instruction,
        "env": CURRENT_ENV,
        "workdir": CURRENT_WORKDIR,
    }

    with open("manifest.json", "w") as f:
        json.dump(manifest, f, indent=4)

    print("[✓] Build complete")


# -------- RUN CONTAINER --------
def run():
    if not os.path.exists("manifest.json"):
        print("Error: No image built. Run 'docksmith build' first.")
        sys.exit(1)

    with open("manifest.json") as f:
        manifest = json.load(f)

    run_argv = sys.argv[2:]

    env_overrides = parse_env_flags(run_argv)
    positional_cmd = []
    i = 0
    while i < len(run_argv):
        if run_argv[i] == "-e":
            i += 2
        elif run_argv[i].startswith("-e") and len(run_argv[i]) > 2:
            i += 1
        else:
            positional_cmd.append(run_argv[i])
            i += 1

    if positional_cmd:
        final_cmd_list = positional_cmd
    else:
        final_cmd_list = manifest.get("cmd")

    if not final_cmd_list:
        print("Error: No CMD defined in image and no command provided at runtime.")
        print("Usage: python3 docksmith.py run [cmd args...]")
        sys.exit(1)

    workdir = manifest.get("workdir", "/")

    env = {}
    env.update(manifest.get("env", {}))
    env.update(env_overrides)

    root = tempfile.mkdtemp(dir="/tmp")
    print("[*] Temp:", root)

    subprocess.run(
        ["tar", "-xzf", get_base(), "-C", root], stderr=subprocess.DEVNULL
    )
    apply_layers(root, manifest.get("layers", []))

    os.makedirs(os.path.join(root, workdir.lstrip("/")), exist_ok=True)

    print("[*] Inside container")

    final_cmd_list = normalize_runtime_cmd(final_cmd_list, root, workdir)

    env_exports = " && ".join(
        [f"export {k}={json.dumps(v)}" for k, v in env.items()]
    )
    cmd_str = " ".join(final_cmd_list)
    if env_exports:
        shell_cmd = f"{env_exports} && cd {workdir} && {cmd_str}"
    else:
        shell_cmd = f"cd {workdir} && {cmd_str}"

    result = subprocess.run(
        ["chroot", root, "/bin/sh", "-c", shell_cmd]
    )

    print(f"[*] Exit code: {result.returncode}")
    shutil.rmtree(root, ignore_errors=True)


# -------- IMAGES --------
def images():
    if not os.path.exists("manifest.json"):
        print("No images found.")
        return

    with open("manifest.json") as f:
        manifest = json.load(f)

    name = manifest.get("name", "docksmith")
    tag = manifest.get("tag", "latest")
    digest = manifest.get("digest", "")
    image_id = digest[:12] if digest else "unknown"

    print(f"{'NAME':<20} {'TAG':<15} {'IMAGE ID':<15}")
    print(f"{name:<20} {tag:<15} {image_id:<15}")


# -------- RMI --------
def rmi():
    if not os.path.exists("manifest.json"):
        print("No image to remove.")
        return

    with open("manifest.json") as f:
        manifest = json.load(f)

    removed = 0
    for layer in manifest.get("layers", []):
        if isinstance(layer, dict):
            digest = str(layer.get("digest", "")).replace("sha256:", "")
        else:
            digest = str(layer)

        path = os.path.join(LAYER_DIR, digest + ".tar")
        if os.path.exists(path):
            os.remove(path)
            removed += 1

    os.remove("manifest.json")
    if os.path.exists("image_layers.txt"):
        os.remove("image_layers.txt")

    print(f"Image removed. ({removed} layer(s) deleted)")


# -------- CLI --------
def main():
    if len(sys.argv) < 2:
        print("Usage: python3 docksmith.py <command> [options]")
        print("Commands:")
        print("  build [--no-cache]        Build image from Docksmithfile")
        print("  run [-e KEY=VAL] [cmd]    Run container")
        print("  images                    List built images")
        print("  rmi                       Remove image and its layers")
        return

    command = sys.argv[1]

    if command == "build":
        no_cache = "--no-cache" in sys.argv
        build(no_cache=no_cache)
    elif command == "run":
        run()
    elif command == "images":
        images()
    elif command == "rmi":
        rmi()
    else:
        print(f"Unknown command: '{command}'")
        print("Use: build | run | images | rmi")


if __name__ == "__main__":
    main()