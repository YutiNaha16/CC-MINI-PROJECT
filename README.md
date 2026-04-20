# Docksmith – A Mini Container Engine

Docksmith is a lightweight container engine built from scratch in Python.  
It replicates core Docker functionality such as image building, layering, caching, and container isolation using `chroot`.

---

## Overview

Docksmith allows you to:
- Build images from a Docksmithfile
- Use layered filesystem architecture
- Cache builds (HIT/MISS)
- Run applications in isolated environments
- Manage images via CLI

---

## Features

- Layered image building (COPY, RUN)
- SHA-256 hashing (content-addressable storage)
- Build cache system
- WORKDIR support
- ENV + runtime override (-e)
- CMD execution
- chroot-based isolation
- CLI support:
  - build
  - run
  - images
  - rmi

---

## Project Structure


CC-MINI-PROJECT/
│
├── docksmith.py
├── Docksmithfile
├── script.sh
├── README.md


---

## Requirements

- Python 3.x  
- Linux OS (required for chroot)  
- Alpine base image (`.tar.gz`)  

Place base image here:


~/.docksmith/base/


---

## Supported Instructions

- FROM  
- COPY  
- RUN  
- WORKDIR  
- ENV  
- CMD  

---

## Setup (One-time)

```bash
mkdir -p ~/.docksmith/{layers,cache,images,base}
All Commands
Build Image:
sudo python3 docksmith.py build -t myapp:latest .
Rebuild (to show cache):
sudo python3 docksmith.py build -t myapp:latest .
Run Container:
sudo python3 docksmith.py run myapp:latest
Run with ENV Override:
sudo python3 docksmith.py run -e NAME=Yuti myapp:latest
List Images:
python3 docksmith.py images
Remove Image:
python3 docksmith.py rmi myapp:latest
Clean cache/layers (optional):
rm -rf ~/.docksmith/layers/*
rm -rf ~/.docksmith/cache/*

Sample Docksmithfile:
FROM alpine
WORKDIR /app
COPY script.sh .
ENV NAME=Default
RUN chmod +x script.sh
CMD ["sh", "script.sh"]

Sample Script:
#!/bin/sh
echo "Hello $NAME from Docksmith!"

Sample Output:
Hello Default from Docksmith!
Hello xyz from Docksmith!

Cache Behavior
First build → CACHE MISS
Second build → CACHE HIT

Isolation:
Docksmith uses chroot:
Container filesystem is isolated
Host system is not affected

How It Works:
Parse Docksmithfile
Execute instructions
Create layers
Cache using SHA-256
Store layers
Reconstruct filesystem
Run in isolated environment

Limitations:
No networking
No volumes
Not production-ready

Learning Outcome:
Container internals
Layered filesystem
Caching mechanisms
Process isolation

Author:
Yuti Naha
Lasya Sriya
Vibhav Kolachana
Yashvardhan Singh

Conclusion:
Docksmith is a functional mini container engine demonstrating core Docker concepts in a simplified way.


