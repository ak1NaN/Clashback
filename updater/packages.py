"""Extract Android x86_64 libraries and base assets without executing them."""
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile


def safe_name(name):
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or "\\" in name:
        raise ValueError(f"Unsafe archive path: {name}")
    return p


def extract_apk(apk, engine, assets):
    count = 0
    for item in apk.infolist():
        name = str(safe_name(item.filename))
        if item.is_dir():
            continue
        if stat.S_ISLNK(item.external_attr >> 16):
            raise ValueError(f"Symlink in APK is not supported: {name}")
        if name in ("lib/x86_64/libg.so", "lib/x86_64/libc++_shared.so"):
            target = engine / PurePosixPath(name).name
        elif name.startswith("assets/"):
            target = assets / name[len("assets/"):]
        else:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with apk.open(item) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        count += 1
    return count


def extract_package(path, engine, assets, workspace):
    """APKM and XAPK are both zip containers; allow a plain APK too."""
    with zipfile.ZipFile(path) as bundle:
        names = bundle.namelist()
        if "AndroidManifest.xml" in names:
            return extract_apk(bundle, engine, assets)
        apks = [n for n in names if n.lower().endswith(".apk")]
        # Base first, install-time assets afterwards; ABI split supplies x86 libraries.
        apks.sort(key=lambda n: ("asset_pack" in n, n))
        if not apks:
            raise ValueError("The package has no APK entries")
        count = 0
        for name in apks:
            safe_name(name)
            print("Extracting", Path(name).name, flush=True)
            # Nested APKs can be large: spool to disk under this project, not RAM.
            with tempfile.TemporaryFile(dir=workspace) as f:
                with bundle.open(name) as src:
                    shutil.copyfileobj(src, f)
                f.seek(0)
                with zipfile.ZipFile(f) as apk:
                    count += extract_apk(apk, engine, assets)
        return count
