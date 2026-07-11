# py_yuzu dependencies

This file summarizes the Python dependencies and the system packages usually
needed to run `py_yuzu` on Fedora and on Debian/Ubuntu-like systems.

It separates:

- Python packages
- system libraries
- external astronomy tools required by specific commands

## 1. Python dependencies

### 1.1 Core runtime Python packages

These are the packages actually used by the pipeline and the GTK juicer UI:

```text
MontagePy
astropy
numpy
scipy
matplotlib
pyraf
sqlalchemy
Pillow
opencv-python
scikit-image
PyGObject
```

Also used or expected by the historical requirements file:

```text
aplpy
uncertainties
mock
sphinxcontrib-images
```

Notes:

- `aplpy` is optional unless you use code paths that need APLpy plots.
- `uncertainties` is useful for some legacy/scientific workflows.
- `mock` is only needed for older-style tests on old Python versions. On
  modern Python, `unittest.mock` is usually enough.
- `sphinxcontrib-images` is documentation-only.

### 1.2 Current requirements file

Current [requirements.txt](/mnt/uxmal_groups/common_data/apps/py_yuzu/requirements.txt):

```text
MontagePy
astropy
numpy>=1.7.1
scipy>=0.12.0
matplotlib>=1.2.1
aplpy>=0.9.9
pyraf>=2.1.1
uncertainties>=2.4.1
mock>=1.0.1
sphinxcontrib-images>=0.5.0
```

### 1.3 Recommended pip install command

For Python-side packages only:

```bash
python3 -m pip install \
  astropy numpy scipy matplotlib pyraf sqlalchemy pillow \
  opencv-python scikit-image PyGObject uncertainties
```

If you also want historical/optional extras:

```bash
python3 -m pip install aplpy mock sphinxcontrib-images
```

## 2. External astronomy tools

These are not normal Python dependencies. They are external tools used by
specific `yuzu` commands.

### Required or strongly recommended

- `SExtractor` / `sex`
  - used by source detection and seeing/photometry workflows
- `IRAF`
  - required by `pyraf` tasks used in aperture photometry
- `MontagePy`
  - required by `mosaic`
- `astrometry.net`
  - required by `astrometry`

### Notes

- `MontagePy` may need a source build on newer Python versions. See
  [MONTAGEPY_INSTALL_FEDORA.md](/mnt/uxmal_groups/common_data/apps/py_yuzu/MONTAGEPY_INSTALL_FEDORA.md).
- `PyRAF` alone is not enough. You also need a working IRAF runtime.
- `SExtractor` must provide either `sextractor` or `sex` in `PATH`.

## 3. Fedora dependencies

### 3.1 Base system packages

```bash
sudo dnf install -y \
  python3 python3-pip python3-devel \
  gcc gcc-c++ gcc-gfortran make pkgconf-pkg-config \
  byacc readline-devel git
```

### 3.2 GTK / PyGObject / image stack

```bash
sudo dnf install -y \
  gtk4 python3-gobject \
  gobject-introspection cairo-gobject \
  gdk-pixbuf2 pango graphene
```

### 3.3 Helpful Python RPM packages when you prefer distro packages

```bash
sudo dnf install -y \
  python3-astropy python3-numpy python3-scipy python3-matplotlib \
  python3-sqlalchemy python3-pillow python3-opencv python3-scikit-image
```

### 3.4 External astronomy tools on Fedora

```bash
sudo dnf install -y astrometry-net
```

Notes:

- `SExtractor` is often not available in standard Fedora repositories; build or
  install it separately if needed.
- `IRAF` is usually not a standard Fedora package; install it separately before
  using `pyraf`.
- `MontagePy` usually needs the separate source build documented in
  [MONTAGEPY_INSTALL_FEDORA.md](/mnt/uxmal_groups/common_data/apps/py_yuzu/MONTAGEPY_INSTALL_FEDORA.md).

## 4. Debian / Ubuntu dependencies

### 4.1 Base system packages

```bash
sudo apt update
sudo apt install -y \
  python3 python3-pip python3-dev \
  build-essential gfortran pkg-config git \
  bison byacc libreadline-dev
```

### 4.2 GTK / PyGObject / image stack

```bash
sudo apt install -y \
  python3-gi gir1.2-gtk-4.0 \
  gobject-introspection libgirepository1.0-dev \
  libcairo2-dev libgdk-pixbuf-2.0-0 \
  libgraphene-1.0-0 libpango-1.0-0
```

### 4.3 Helpful Python Debian packages when you prefer distro packages

```bash
sudo apt install -y \
  python3-astropy python3-numpy python3-scipy python3-matplotlib \
  python3-sqlalchemy python3-pil python3-opencv python3-skimage
```

### 4.4 External astronomy tools on Debian / Ubuntu

```bash
sudo apt install -y astrometry.net sextractor
```

Notes:

- Package names for `IRAF` and `PyRAF` vary more by release and may be absent
  in modern repositories. In practice, many setups install `pyraf` with `pip`
  and provide IRAF separately.
- `MontagePy` may also need a source build on Debian/Ubuntu if a matching wheel
  is unavailable for your Python version.

## 5. Practical install patterns

### Fedora: mixed RPM + pip

```bash
sudo dnf install -y \
  python3 python3-pip python3-devel \
  gcc gcc-c++ gcc-gfortran make pkgconf-pkg-config \
  gtk4 python3-gobject gobject-introspection cairo-gobject \
  gdk-pixbuf2 pango graphene \
  python3-astropy python3-numpy python3-scipy python3-matplotlib \
  python3-sqlalchemy python3-pillow python3-opencv python3-scikit-image \
  astrometry-net

python3 -m pip install pyraf uncertainties
```

Then install separately:

- IRAF
- SExtractor
- MontagePy

### Debian/Ubuntu: mixed APT + pip

```bash
sudo apt update
sudo apt install -y \
  python3 python3-pip python3-dev build-essential gfortran pkg-config git \
  python3-gi gir1.2-gtk-4.0 gobject-introspection libgirepository1.0-dev \
  libcairo2-dev libgdk-pixbuf-2.0-0 libgraphene-1.0-0 libpango-1.0-0 \
  python3-astropy python3-numpy python3-scipy python3-matplotlib \
  python3-sqlalchemy python3-pil python3-opencv python3-skimage \
  astrometry.net sextractor

python3 -m pip install pyraf uncertainties
```

Then install separately if missing:

- IRAF
- MontagePy

## 6. Short dependency map by command

- `mosaic`
  - `MontagePy`
- `photometry`
  - `pyraf`, IRAF, `astropy`, `numpy`, `scipy`, `SExtractor`
- `seeing`
  - `numpy`, `scipy`, `SExtractor`
- `astrometry`
  - `astrometry.net`
- `diffphot`
  - `numpy`, `sqlalchemy`
- `juicer`
  - `PyGObject`, GTK4, `matplotlib`, `Pillow`, `opencv-python`, `scikit-image`

## 7. Reality check

For this repository as currently used, the most important pieces are:

- Python: `astropy`, `numpy`, `scipy`, `matplotlib`, `pyraf`, `sqlalchemy`
- GUI: `PyGObject` + GTK4
- tools: `SExtractor`, IRAF, `MontagePy`, `astrometry.net`

If one of those is missing, at least one major command will fail.
