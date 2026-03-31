#!/usr/bin/env python3
import os, sys, tarfile, hashlib, subprocess, tempfile, shutil, glob, json

BASE_DIR = "/home/yuti/.docksmith"
LAYER_DIR = os.path.join(BASE_DIR, "layers")

CURRENT_WORKDIR = "/"
CURRENT_ENV = {}

# -------- CACHE KEY --------
def compute_cache_key(prev_hash, instruction):
    env_string = "".join([f"{k}={v}" for k,v in sorted(CURRENT_ENV.items())])
    data = (prev_hash + instruction + CURRENT_WORKDIR + env_string).encode()
    return hashlib.sha256(data).hexdigest()

# -------- PARSE FILE --------
def parse_docksmithfile():
    instructions = []
    with open("Docksmithfile") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            cmd = parts[0]
            args = parts[1] if len(parts) > 1 else ""
            instructions.append((cmd, args))
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
        tar.add(src, arcname=os.path.basename(src))

    digest = sha256_file(tar_name)
    final = os.path.join(LAYER_DIR, f"{digest}.tar")
    shutil.move(tar_name, final)

    return digest

# -------- GET BASE IMAGE --------
def get_base():
    base_path = BASE_DIR + "/base"
    for file in os.listdir(base_path):
        if file.endswith(".tar.gz"):
            return os.path.join(base_path, file)
    raise Exception("Base image missing")

# -------- APPLY LAYERS --------
def apply_layers(root):
    if not os.path.exists("image_layers.txt"):
        return

    with open("image_layers.txt") as f:
        layers = f.read().splitlines()

    for l in layers:
        path = os.path.join(LAYER_DIR, l + ".tar")
        subprocess.run(["tar", "-xf", path, "-C", root])

# -------- BUILD --------
def build():
    global CURRENT_WORKDIR, CURRENT_ENV

    instr = parse_docksmithfile()
    layers = []
    prev_hash = ""
    cmd_instruction = ""

    CURRENT_WORKDIR = "/"
    CURRENT_ENV = {}

    for cmd, args in instr:
        instruction_text = cmd + " " + args

        # -------- WORKDIR --------
        if cmd == "WORKDIR":
            CURRENT_WORKDIR = args
            print("[WORKDIR SET]", CURRENT_WORKDIR)
            continue

        # -------- ENV --------
        if cmd == "ENV":
            key, value = args.split("=")
            CURRENT_ENV[key] = value
            print("[ENV SET]", key, "=", value)
            continue

        # -------- CMD --------
        if cmd == "CMD":
            cmd_instruction = args.replace('[','').replace(']','').replace('"','')
            print("[CMD SET]", cmd_instruction)
            continue

        cache_key = compute_cache_key(prev_hash, instruction_text)
        cache_file = os.path.join(BASE_DIR, "cache", cache_key)

        # -------- CACHE HIT --------
        if os.path.exists(cache_file):
            print("[CACHE HIT]", instruction_text)
            with open(cache_file) as f:
                layer_hash = f.read().strip()
            layers.append(layer_hash)
            prev_hash = layer_hash
            continue

        # -------- CACHE MISS --------
        print("[CACHE MISS]", instruction_text)

        if cmd == "COPY":
            src, dest = args.split()
            layer_hash = create_layer(src)

        elif cmd == "RUN":
            temp_root = tempfile.mkdtemp(dir="/tmp")

            subprocess.run(["tar", "-xzf", get_base(), "-C", temp_root])
            apply_layers(temp_root)

            env_string = " ".join([f"{k}='{v}'" for k, v in CURRENT_ENV.items()])

            subprocess.run([
                "chroot", temp_root,
                "/bin/sh", "-c",
                f"{env_string} cd {CURRENT_WORKDIR} && {args}"
            ])

            layer_hash = create_layer(temp_root)

        else:
            continue

        with open(cache_file, "w") as f:
            f.write(layer_hash)

        layers.append(layer_hash)
        prev_hash = layer_hash

    # -------- SAVE LAYERS --------
    with open("image_layers.txt", "w") as f:
        for l in layers:
            f.write(l + "\n")

    # -------- MANIFEST --------
    manifest = {
        "layers": layers,
        "cmd": cmd_instruction,
        "env": CURRENT_ENV,
        "workdir": CURRENT_WORKDIR
    }

    with open("manifest.json", "w") as f:
        json.dump(manifest, f, indent=4)

    print("[✓] Build complete")

# -------- RUN --------
def run():
    root = tempfile.mkdtemp(dir="/tmp")
    print("[*] Temp:", root)

    subprocess.run(["tar", "-xzf", get_base(), "-C", root])
    apply_layers(root)

    with open("manifest.json") as f:
        manifest = json.load(f)

    cmd = manifest.get("cmd", "")
    workdir = manifest.get("workdir", "/")
    env = os.environ.copy()
    env.update(manifest.get("env", {}))

    os.makedirs(root + workdir, exist_ok=True)

    os.chroot(root)
    os.chdir(workdir)

    print("[*] Inside container")

    subprocess.run(
        ["/bin/sh", "-c", cmd + " || echo fallback works"],
        env=env
    )

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
