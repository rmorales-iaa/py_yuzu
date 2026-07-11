# Installing MontagePy on Fedora

This guide installs `MontagePy` from source on Fedora using the upstream
`Montage` repository and a local Python user install.

Tested on Fedora with Python 3.14.

## 1. Install system packages

```bash
sudo dnf install -y \
  gcc gcc-c++ gcc-gfortran make \
  byacc readline-devel \
  python3 python3-pip python3-devel
```

## 2. Install Python build dependencies

```bash
python3 -m pip install --user Cython build importlib_resources
```

## 3. Clone Montage source

```bash
git clone --depth 1 https://github.com/Caltech-IPAC/Montage.git /tmp/montage
cd /tmp/montage
```

## 4. Fix compiler mode for old bundled C code

Some bundled libraries fail with Fedora's newer default C mode. Build with
GNU17 and PIC by placing wrapper compilers first in `PATH`.

```bash
mkdir -p /tmp/montage-tools
printf '%s\n' '#!/bin/sh' 'exec /usr/bin/gcc -std=gnu17 -fPIC "$@"' > /tmp/montage-tools/gcc
printf '%s\n' '#!/bin/sh' 'exec /usr/bin/cc -std=gnu17 -fPIC "$@"' > /tmp/montage-tools/cc
printf '%s\n' '#!/bin/sh' 'exec /usr/bin/python3 "$@"' > /tmp/montage-tools/python
chmod +x /tmp/montage-tools/gcc /tmp/montage-tools/cc /tmp/montage-tools/python
```

## 5. Clean and build native Montage

```bash
cd /tmp/montage
make clean
env PATH=/tmp/montage-tools:$PATH make
```

Expected result: build completes and populates `/tmp/montage/bin`,
`/tmp/montage/lib`, and `/tmp/montage/python/MontagePy/lib`.

## 6. Build MontagePy wheel

```bash
cd /tmp/montage/python/MontagePy
env PATH=/tmp/montage-tools:$PATH sh make_local.sh
```

Expected result: wheel created under `dist/`, for example:

```bash
dist/montagepy-2.3.1-cp314-cp314-linux_x86_64.whl
```

## 7. Install MontagePy

```bash
python3 -m pip install --user --force-reinstall dist/montagepy-*.whl
```

## 8. Verify installation

```bash
python3 - <<'PY'
import MontagePy
import MontagePy.main as mm
print(MontagePy.__file__)
print(mm.__file__)
print(hasattr(mm, 'mProject'), hasattr(mm, 'mImgtbl'), hasattr(mm, 'mViewer'))
PY
```

Expected output:

- package path under `~/.local/lib/python*/site-packages/MontagePy`
- extension module path ending in `MontagePy/main*.so`
- `True True True`

## Notes

- Do not build with command-line `make CC='gcc ...'`. Some Montage makefiles
  store include flags inside `CC`, and overriding `CC` breaks headers such as
  `mtbl.h`.
- Upstream `make_local.sh` expects `python`, not only `python3`. The wrapper
  above handles that without editing upstream scripts.
- `MontagePy` is not currently published on PyPI for Python 3.14, so source
  build is required.
