# Docksmith - Mini Container Engine

This project implements a simplified Docker-like container engine.

## Features
- Layer-based filesystem (COPY, RUN)
- SHA-256 content addressing
- Deterministic caching (CACHE HIT / MISS)
- OS-level isolation using chroot
- Supports WORKDIR, ENV, CMD

## How to Run

### Build
```bash
python3 docksmith.py build
