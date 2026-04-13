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
def compute_cache_key(prev_hash, instruction):
    env_string = "".join([f"{k}={v}" for k, v in sorted(CURRENT_ENV.items())])
    data = (prev_hash + instruction + CURRENT_WORKDIR + env_string).encode()
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

# -------- HASH --------
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        h.update(f.read())
    return h.hexdigest()

# -------- CREATE LAYER --------
def create_layer(src):
    tar_name = "layer.tar"
    with tarfile.open(tar_name, "w") as tar:
        tar.add(src, arcname=".")
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
    # If a specific list is passed, use it; otherwise read from image_layers.txt
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
            subprocess.run(["tar", "-xf", layer_path, "-C", root],
                           stderr=subprocess.DEVNULL)
        else:
            print(f"[WARN] Missing layer skipped: {l}")

# -------- PARSE -e FLAGS --------
def parse_env_flags(argv):
    """
    Parse -e KEY=VALUE flags from argv list.
    Supports: -e KEY=VALUE  (as separate token or attached)
    Returns a dict of overrides.
    """
    overrides = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "-e":
            # Next token is KEY=VALUE
            if i + 1 < len(argv):
                kv = argv[i + 1]
                if "=" in kv:
                    key, value = kv.split("=", 1)
                    overrides[key.strip()] = value.strip()
                else:
                    print(f"Error: -e flag must be in KEY=VALUE format, got: {kv}")
                    sys.exit(1)
                i += 2
            else:
                print("Error: -e flag requires KEY=VALUE argument")
                sys.exit(1)
        elif arg.startswith("-e") and len(arg) > 2:
            # Attached form: -eKEY=VALUE
            kv = arg[2:]
            if "=" in kv:
                key, value = kv.split("=", 1)
                overrides[key.strip()] = value.strip()
            else:
                print(f"Error: -e flag must be in KEY=VALUE format, got: {kv}")
                sys.exit(1)
            i += 1
        else:
            i += 1
    return overrides

# -------- BUILD --------
def build(no_cache=False):
    global CURRENT_WORKDIR, CURRENT_ENV

    instr = parse_docksmithfile()
    layers = []
    prev_hash = ""
    cmd_instruction = None   # None means CMD was never set
    cmd_lineno = None

    CURRENT_WORKDIR = "/"
    CURRENT_ENV = {}

    for cmd, args, lineno in instr:
        instruction_text = cmd + " " + args

        cache_key = compute_cache_key(prev_hash, instruction_text)
        cache_file = os.path.join(CACHE_DIR, cache_key)

        # -------- FROM --------
        if cmd == "FROM":
            print("[CACHE MISS]", instruction_text)
            continue

        # -------- WORKDIR --------
        elif cmd == "WORKDIR":
            if (not no_cache) and os.path.exists(cache_file):
                print("[CACHE HIT]", instruction_text)
                with open(cache_file) as f:
                    layer_hash = f.read().strip()
                layers.append(layer_hash)
                prev_hash = layer_hash
                CURRENT_WORKDIR = args
                continue

            print("[CACHE MISS]", instruction_text)

            temp_root = tempfile.mkdtemp(dir="/tmp")
            subprocess.run(["tar", "-xzf", get_base(), "-C", temp_root],
                           stderr=subprocess.DEVNULL)
            apply_layers(temp_root, layers)

            CURRENT_WORKDIR = args
            os.makedirs(os.path.join(temp_root, CURRENT_WORKDIR.lstrip("/")), exist_ok=True)

            layer_hash = create_layer(temp_root)
            shutil.rmtree(temp_root, ignore_errors=True)

        # -------- ENV --------
        elif cmd == "ENV":
            if "=" not in args:
                print(f"Error: ENV must be in KEY=VALUE format at line {lineno}")
                sys.exit(1)
            key, value = args.split("=", 1)
            CURRENT_ENV[key.strip()] = value.strip()
            print("[ENV SET]", key.strip(), "=", value.strip())
            continue

        # -------- CMD --------
        elif cmd == "CMD":
            # CMD MUST be a JSON array — ["exec", "arg"] form only
            try:
                parsed = json.loads(args)
                if not isinstance(parsed, list):
                    raise ValueError("CMD must be a JSON array")
                # Validate all elements are strings
                for item in parsed:
                    if not isinstance(item, str):
                        raise ValueError("CMD array elements must be strings")
                cmd_instruction = parsed          # store as list
                cmd_lineno = lineno
            except (json.JSONDecodeError, ValueError) as e:
                print(f"Error on line {lineno}: CMD must be a JSON array like [\"exec\", \"arg\"] — got: {args}")
                sys.exit(1)
            print("[CMD SET]", cmd_instruction)
            continue

        # -------- COPY --------
        elif cmd == "COPY":
            if (not no_cache) and os.path.exists(cache_file):
                print("[CACHE HIT]", instruction_text)
                with open(cache_file) as f:
                    layer_hash = f.read().strip()
                layers.append(layer_hash)
                prev_hash = layer_hash
                continue

            print("[CACHE MISS]", instruction_text)

            parts = args.split()
            if len(parts) != 2:
                print(f"Error on line {lineno}: COPY requires exactly <src> <dest>")
                sys.exit(1)
            src, dest = parts

            temp_root = tempfile.mkdtemp(dir="/tmp")
            subprocess.run(["tar", "-xzf", get_base(), "-C", temp_root],
                           stderr=subprocess.DEVNULL)
            apply_layers(temp_root, layers)

            full_dest = os.path.join(temp_root, dest.lstrip("/"))
            os.makedirs(os.path.dirname(full_dest) or temp_root, exist_ok=True)
            shutil.copy(src, full_dest)

            layer_hash = create_layer(temp_root)
            shutil.rmtree(temp_root, ignore_errors=True)

        # -------- RUN --------
        elif cmd == "RUN":
            if (not no_cache) and os.path.exists(cache_file):
                print("[CACHE HIT]", instruction_text)
                with open(cache_file) as f:
                    layer_hash = f.read().strip()
                layers.append(layer_hash)
                prev_hash = layer_hash
                continue

            print("[CACHE MISS]", instruction_text)

            temp_root = tempfile.mkdtemp(dir="/tmp")
            subprocess.run(["tar", "-xzf", get_base(), "-C", temp_root],
                           stderr=subprocess.DEVNULL)
            apply_layers(temp_root, layers)

            os.makedirs(os.path.join(temp_root, CURRENT_WORKDIR.lstrip("/")), exist_ok=True)

            # Build env string for shell
            env_prefix = " ".join([f"{k}={json.dumps(v)}" for k, v in CURRENT_ENV.items()])
            shell_cmd = f"cd {CURRENT_WORKDIR} && {args}"
            if env_prefix:
                shell_cmd = f"export {env_prefix} && {shell_cmd}"

            # Use same chroot isolation primitive as runtime run()
            result = subprocess.run(
                ["chroot", temp_root, "/bin/sh", "-c", shell_cmd]
            )

            layer_hash = create_layer(temp_root)
            shutil.rmtree(temp_root, ignore_errors=True)

        else:
            print(f"[WARN] Unknown instruction '{cmd}' at line {lineno}, skipping")
            continue

        with open(cache_file, "w") as f:
            f.write(layer_hash)

        layers.append(layer_hash)
        prev_hash = layer_hash

    # -------- SAVE --------
    with open("image_layers.txt", "w") as f:
        for l in layers:
            f.write(l + "\n")

    # Compute image digest from all layer hashes combined
    combined = "".join(layers).encode()
    image_digest = hashlib.sha256(combined).hexdigest()

    manifest = {
        "name": "docksmith",
        "tag": "latest",
        "digest": image_digest,
        "layers": layers,
        "cmd": cmd_instruction,   # stored as list or None
        "env": CURRENT_ENV,
        "workdir": CURRENT_WORKDIR
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

    # Resolve CMD: image CMD can be overridden by trailing args after 'run'
    # argv looks like: docksmith.py run [-e K=V ...] [cmd arg ...]
    run_argv = sys.argv[2:]   # everything after 'run'

    # Split out -e flags and positional args
    positional = [a for a in run_argv if not a.startswith("-e") and
                  not (run_argv[max(0, run_argv.index(a)-1):run_argv.index(a)] == ["-e"])]

    # Safer: walk and collect
    env_overrides = parse_env_flags(run_argv)
    positional_cmd = []
    i = 0
    while i < len(run_argv):
        if run_argv[i] == "-e":
            i += 2   # skip flag and its value
        elif run_argv[i].startswith("-e") and len(run_argv[i]) > 2:
            i += 1   # skip attached -eKEY=VALUE
        else:
            positional_cmd.append(run_argv[i])
            i += 1

    # Determine final command to run
    if positional_cmd:
        # Runtime override: treat positional args as the command list
        final_cmd_list = positional_cmd
    else:
        final_cmd_list = manifest.get("cmd")   # list or None

    # Fail clearly if no CMD anywhere
    if not final_cmd_list:
        print("Error: No CMD defined in image and no command provided at runtime.")
        print("Usage: python3 docksmith.py run [cmd args...]")
        sys.exit(1)

    workdir = manifest.get("workdir", "/")

    # Build env: start from manifest ENV, then apply -e overrides
    env = {}
    env.update(manifest.get("env", {}))
    env.update(env_overrides)

    # Create container filesystem
    root = tempfile.mkdtemp(dir="/tmp")
    print("[*] Temp:", root)

    subprocess.run(["tar", "-xzf", get_base(), "-C", root],
                   stderr=subprocess.DEVNULL)
    apply_layers(root, manifest.get("layers", []))

    os.makedirs(os.path.join(root, workdir.lstrip("/")), exist_ok=True)

    print("[*] Inside container")

    # Build the full environment for the subprocess
    # We pass env vars as exports inside the shell command so they work post-chroot
    env_exports = " && ".join([f"export {k}={json.dumps(v)}" for k, v in env.items()])
    cmd_str = " ".join(final_cmd_list)
    if env_exports:
        shell_cmd = f"{env_exports} && cd {workdir} && {cmd_str}"
    else:
        shell_cmd = f"cd {workdir} && {cmd_str}"

    # chroot isolation — same primitive as build's RUN
    result = subprocess.run(
        ["chroot", root, "/bin/sh", "-c", shell_cmd]
    )

    print(f"[*] Exit code: {result.returncode}")

    # Cleanup
    shutil.rmtree(root, ignore_errors=True)

# -------- IMAGES --------
def images():
    if not os.path.exists("manifest.json"):
        print("No images found.")
        return

    with open("manifest.json") as f:
        manifest = json.load(f)

    name    = manifest.get("name", "docksmith")
    tag     = manifest.get("tag", "latest")
    digest  = manifest.get("digest", "")
    image_id = digest[:12] if digest else "unknown"

    # Header
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
    for l in manifest.get("layers", []):
        path = os.path.join(LAYER_DIR, l + ".tar")
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
