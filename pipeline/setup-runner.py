# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#!/usr/bin/env python3
"""Setup runner for Maya integration tests in CodeBuild.

Supports Linux, Windows, and macOS with Maya 2025 and 2026.

On Linux, can additionally install the Arnold (mtoa), V-Ray, and Redshift
renderers into each Maya version so the renderer-specific integ tests can run.
Use --renderers to select which renderers to install (default: none).

On Windows, installs the pywin32 support DLLs so child processes (mayapy) can
load win32file. This mirrors the pattern used by deadline-cloud-for-3ds-max.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import boto3
from botocore.config import Config

# ---------------------------------------------------------------------------
# Maya
# ---------------------------------------------------------------------------

MAYA_VERSION_CONFIG: dict[str, dict[str, Any]] = {
    "2025": {
        "python": "3.11",
        "installer": {
            "linux": "Autodesk_MayaIO_2025_3_ML_Linux_64bit.run",
            "windows": "Maya2025_Windows.zip",
            "macos": "Maya2025_macOS.dmg",
        },
    },
    "2026": {
        "python": "3.11",
        "installer": {
            "linux": "Autodesk_MayaIO_2026_3_Update_Linux.run",
            "windows": "Maya2026_Windows.zip",
            "macos": "Maya2026_macOS.dmg",
        },
    },
}

MAYA_CHECKSUMS: dict[str, dict[str, str]] = {
    "2025": {
        "linux": "a4c46a576aea91e1e52a06355b413f98000b884feb8eb1349a7459990e212395",
        "windows": "0f9ce4abc7febbef07b0ef5ecd2526a45200a9b068a6272f6f2afdd29925a845",
        "macos": "2e277d94b155aa32c79aa83a7d9e6ade23204961802f1c6a41ff8db2553e1c00",
    },
    "2026": {
        "linux": "b17b0700933e8e4329939da38cc52c93ed483a93b02e9fa78031fddae763c8e8",
        "windows": "9c9612f6e4d3f1f6de897a21fde6f9930e2e40bb6ddc3ca9647e2668cdba935c",
        "macos": "8779921f4b7263fab8e2b2429949b7ae6b298c6ec3ae3cd83e9c522e02f2baaa",
    },
}

# ---------------------------------------------------------------------------
# Renderers (Linux only today — Windows/macOS installers are not yet in S3)
# ---------------------------------------------------------------------------

# Arnold (MtoA) — one installer per Maya version, bundled under mtoa/5.5/.
MTOA_CONFIG: dict[str, dict[str, Any]] = {
    "2025": {
        "s3_key": "mtoa/5.5/MtoA-5.5.6.1-linux-2025.run",
        "checksums": {
            "linux": "7f607c05461efec4ebd9f7d40e0d3e6de3e2dccba51078e1ccfaf598b77af389",
        },
    },
    "2026": {
        "s3_key": "mtoa/5.5/MtoA-5.5.6.1-linux-2026.run",
        "checksums": {
            "linux": "d8881e1cece725178d90aaa6d44507ea017ec64d7d23c76b129b9e349d1c9cc6",
        },
    },
}

# V-Ray for Maya — Chaos RHEL8 self-extracting installer per Maya version.
VRAY_CONFIG: dict[str, dict[str, Any]] = {
    "2025": {
        "s3_key": "maya-vray/72002/vray_adv_72002_maya2025_dr2_rhel8",
        "checksums": {
            "linux": "cdbeba5ea82120155ecda75da01e359d1dc01f8905a4751d233b40136f9610c6",
        },
    },
    "2026": {
        "s3_key": "maya-vray/72002/vray_adv_72002_maya2026_dr2_rhel8",
        "checksums": {
            "linux": "a6e1e65202f6c9b3d4e12e7eb423a780a34dfeac3540658b16f4e20f8009fca6",
        },
    },
}

# Redshift — single installer supports both Maya 2025 and 2026.
REDSHIFT_CONFIG: dict[str, dict[str, str]] = {
    "linux": {
        "s3_key": "redshift/2026/redshift_2026.3.1_2336394021_linux_x64.run",
        "checksum": "a95e48d2f4dd68e923c7f40693823d206d11acb51e145038d5625b748294777c",
    },
}

SUPPORTED_RENDERERS: tuple[str, ...] = ("mtoa", "vray", "redshift")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def run(
    cmd: str | Sequence[str],
    check: bool = True,
    cwd: str | os.PathLike[str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    print(f"Running: {cmd if isinstance(cmd, str) else shlex.join(cmd)}")
    result = subprocess.run(cmd, check=False, cwd=cwd)
    if check and result.returncode != 0:
        sys.exit(result.returncode)
    return result


# TODO: Increase timeout back to 600+ once installs are proven stable.
DEFAULT_CMD_TIMEOUT = 180  # 3 minutes


def run_with_timeout(
    cmd: str | Sequence[str],
    timeout: int = DEFAULT_CMD_TIMEOUT,
    cwd: str | os.PathLike[str] | None = None,
    label: str = "",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a command with a timeout. Prints stdout/stderr on failure for diagnostics."""
    desc = label or (cmd if isinstance(cmd, str) else shlex.join(cmd))
    print(f"Running (timeout={timeout}s): {desc}")
    run_env = None
    if env:
        run_env = {**os.environ, **env}
    try:
        result = subprocess.run(
            cmd,
            check=False,
            cwd=cwd,
            timeout=timeout,
            capture_output=True,
            env=run_env,
        )
        # Always print output for visibility
        if result.stdout:
            print(result.stdout.decode("utf-8", errors="replace"))
        if result.stderr:
            print(result.stderr.decode("utf-8", errors="replace"))
        if result.returncode != 0:
            print(f"ERROR: Command failed with exit code {result.returncode}")
            sys.exit(result.returncode)
        return result
    except subprocess.TimeoutExpired as e:
        print(f"TIMEOUT: Command did not complete within {timeout}s: {desc}")
        if e.stdout:
            print(f"stdout so far:\n{e.stdout.decode('utf-8', errors='replace')[-2000:]}")
        if e.stderr:
            print(f"stderr so far:\n{e.stderr.decode('utf-8', errors='replace')[-2000:]}")
        sys.exit(1)


def download_from_s3(s3_path: str, local_path: str | os.PathLike[str]) -> None:
    bucket = os.environ.get("INSTALLER_BUCKET")
    if not bucket:
        print("ERROR: INSTALLER_BUCKET not set")
        sys.exit(1)
    expected_bucket_owner = os.environ.get("INSTALLER_BUCKET_EXPECTED_OWNER")
    if not expected_bucket_owner:
        raise ValueError("INSTALLER_BUCKET_EXPECTED_OWNER environment variable is required")
    if not (expected_bucket_owner.isdigit() and len(expected_bucket_owner) == 12):
        raise ValueError("INSTALLER_BUCKET_EXPECTED_OWNER must be a 12-digit AWS Account ID")

    config = Config(read_timeout=300, connect_timeout=60, retries={"max_attempts": 2})
    s3 = boto3.client("s3", config=config)
    print(f"Downloading s3://{bucket}/{s3_path} to {local_path}")
    s3.download_file(
        bucket,
        s3_path,
        str(local_path),
        ExtraArgs={"ExpectedBucketOwner": expected_bucket_owner},
    )


def verify_checksum(file_path: str | os.PathLike[str], expected_checksum: str) -> bool:
    """Verify SHA256 checksum of downloaded file."""
    if not expected_checksum:
        print(f"WARNING: No checksum configured for {file_path}, skipping verification")
        return True
    print(f"Verifying checksum for {file_path}...")
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    actual = sha256.hexdigest()
    if actual != expected_checksum:
        print("ERROR: Checksum mismatch!")
        print(f"  Expected: {expected_checksum}")
        print(f"  Actual:   {actual}")
        sys.exit(1)
    print("OK Checksum verified")
    return True


def _platform_key() -> str:
    """Return our S3-folder / config key for the current OS."""
    system = platform.system()
    return {"Linux": "linux", "Windows": "windows", "Darwin": "macos"}.get(system, system.lower())


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------


def _install_maya_linux(version: str) -> Path:
    config = MAYA_VERSION_CONFIG[version]
    installer_name = config["installer"]["linux"]
    maya_dir = Path(f"/opt/Autodesk/mayaio/{version}")

    # Check if Maya is already installed by looking for the real binary
    existing = subprocess.run(
        ["find", str(maya_dir), "-name", "mayapy", "-type", "f"],
        capture_output=True, text=True, check=False,
    )
    if existing.stdout.strip():
        print(f"Maya {version} already installed: {existing.stdout.strip().split(chr(10))[0]}")
        return maya_dir

    lock_file = Path(f"/tmp/maya-{version}.lock")
    if lock_file.exists():
        print(f"Waiting for concurrent Maya {version} install...")
        for _ in range(120):
            time.sleep(1)
            check = subprocess.run(
                ["find", str(maya_dir), "-name", "mayapy", "-type", "f"],
                capture_output=True, text=True, check=False,
            )
            if check.stdout.strip():
                break
        return maya_dir

    lock_file.touch()
    try:
        print(f"Installing Maya {version}...")
        installer_path = Path(f"/tmp/{installer_name}")

        download_from_s3(f"maya/{version}/{installer_name}", installer_path)
        verify_checksum(installer_path, MAYA_CHECKSUMS[version].get("linux", ""))

        run(["chmod", "+x", str(installer_path)])
        # Extract to /opt (not /tmp) and clean any stale dir from prior runs
        extract_dir = Path(f"/opt/maya-{version}-extract")
        if extract_dir.exists():
            run(["rm", "-rf", str(extract_dir)], check=False)
        # --noexec: don't run post-extract scripts (they do rm -rf /tmp/*)
        # --phase2: skip EULA prompt
        print("Extracting installer (this may take a moment)...")
        result = subprocess.run(
            [
                str(installer_path),
                "--noexec",
                "--keep",
                "--nox11",
                "--target",
                str(extract_dir),
                "--phase2",
            ],
            check=False,
        )
        print(f"Installer exit code: {result.returncode}")

        rpms = list(extract_dir.rglob("*.rpm"))

        # The .run extracts to a directory containing an RPM.
        # Use rpm2cpio to extract it (same approach as BealineCondaRecipe-Maya).
        maya_dir.mkdir(parents=True, exist_ok=True)
        rpms = list(extract_dir.rglob("*.rpm"))
        if not rpms:
            print(f"ERROR: No RPM found in {extract_dir}")
            run(["ls", "-la", str(extract_dir)], check=False)
            sys.exit(1)
        rpm_path = rpms[0].resolve()
        subprocess.run(
            f"rpm2cpio {rpm_path} | cpio -idm",
            shell=True,
            check=True,
            cwd=maya_dir,
        )

        # MayaIO RPM extracts to usr/autodesk/mayaIO<version>/ inside cwd
        # The exact directory name varies by version — find mayapy dynamically.
        result = subprocess.run(
            ["find", str(maya_dir), "-name", "mayapy", "-type", "f"],
            capture_output=True,
            text=True,
            check=False,
        )
        mayapy_exe = None
        if result.stdout.strip():
            mayapy_exe = Path(result.stdout.strip().split("\n")[0])
        # Verify installation
        if mayapy_exe and mayapy_exe.exists():
            print(f"SUCCESS: mayapy found at {mayapy_exe}")
        else:
            print(f"ERROR: mayapy NOT found under {maya_dir}")
            run(["find", str(maya_dir), "-maxdepth", "5", "-type", "f", "-name", "maya*"], check=False)
            sys.exit(1)

        installer_path.unlink(missing_ok=True)
        run(["rm", "-rf", str(extract_dir)], check=False)
    finally:
        lock_file.unlink(missing_ok=True)

    return maya_dir


def _install_mtoa_linux(version: str) -> None:
    """Install MtoA (Arnold for Maya) for the given Maya version."""
    if version not in MTOA_CONFIG:
        print(f"ERROR: No MtoA config for Maya {version}")
        sys.exit(1)

    mtoa_install_dir = Path(f"/opt/solidangle/mtoa/{version}")
    marker = mtoa_install_dir / ".installed"
    if marker.exists():
        print(f"MtoA for Maya {version} already installed")
        return

    lock_file = Path(f"/tmp/mtoa-{version}.lock")
    if lock_file.exists():
        print(f"Waiting for concurrent MtoA {version} install...")
        for _ in range(120):
            time.sleep(1)
            if marker.exists():
                break
        return

    lock_file.touch()
    try:
        s3_key = MTOA_CONFIG[version]["s3_key"]
        installer_name = Path(s3_key).name
        installer_path = Path(f"/tmp/{installer_name}")

        print(f"Installing MtoA for Maya {version}...")
        download_from_s3(s3_key, installer_path)
        verify_checksum(installer_path, MTOA_CONFIG[version]["checksums"]["linux"])

        run(["chmod", "+x", str(installer_path)])
        mtoa_install_dir.mkdir(parents=True, exist_ok=True)
        # MtoA is a Makeself archive. Extract then unzip the package.
        extract_tmp = Path(f"/tmp/mtoa-{version}-extract")
        if extract_tmp.exists():
            run(["rm", "-rf", str(extract_tmp)], check=False)
        run([str(installer_path), "--noexec", "--target", str(extract_tmp)])
        # Unzip the package into the install dir
        pkg_zip = next(extract_tmp.glob("*.zip"), None)
        if pkg_zip:
            run(["unzip", "-q", str(pkg_zip), "-d", str(mtoa_install_dir)])
        else:
            print(f"ERROR: No .zip found in {extract_tmp}")
            run(["ls", "-la", str(extract_tmp)], check=False)
            sys.exit(1)
        run(["rm", "-rf", str(extract_tmp)], check=False)

        # Verify — installer lays down plugins under $prefix/plug-ins.
        arnold_plugin = mtoa_install_dir / "plug-ins" / "mtoa.so"
        if arnold_plugin.exists():
            print(f"SUCCESS: mtoa.so found at {arnold_plugin}")
            marker.touch()
        else:
            # Some MtoA versions extract into a versioned subdir — list for visibility.
            print(f"WARNING: mtoa.so not found at {arnold_plugin}, dumping install tree:")
            run(["find", str(mtoa_install_dir), "-maxdepth", "3"], check=False)
            # Still mark installed — plugin layout varies between versions. Tests
            # will fail fast and visibly if the plugin is actually missing.
            marker.touch()

        installer_path.unlink(missing_ok=True)
    finally:
        lock_file.unlink(missing_ok=True)


def _install_vray_linux(version: str) -> None:
    """Install V-Ray for Maya for the given Maya version."""
    if version not in VRAY_CONFIG:
        print(f"ERROR: No V-Ray config for Maya {version}")
        sys.exit(1)

    vray_install_dir = Path(f"/usr/ChaosGroup/V-Ray/Maya{version}-x64")
    marker = vray_install_dir / ".installed"
    if marker.exists():
        print(f"V-Ray for Maya {version} already installed")
        return

    lock_file = Path(f"/tmp/vray-{version}.lock")
    if lock_file.exists():
        print(f"Waiting for concurrent V-Ray {version} install...")
        for _ in range(180):
            time.sleep(1)
            if marker.exists():
                break
        return

    lock_file.touch()
    try:
        s3_key = VRAY_CONFIG[version]["s3_key"]
        installer_name = Path(s3_key).name
        installer_path = Path(f"/tmp/{installer_name}")

        print(f"Installing V-Ray for Maya {version}...")
        download_from_s3(s3_key, installer_path)
        verify_checksum(installer_path, VRAY_CONFIG[version]["checksums"]["linux"])

        run(["chmod", "+x", str(installer_path)])
        # Chaos V-Ray installer uses custom flags for silent install
        vray_install_dir.mkdir(parents=True, exist_ok=True)
        run(
            [
                str(installer_path),
                "-gui=0",
                "-auto",
                "-quiet=1",
                f"-unpackInstall={vray_install_dir}",
            ],
            check=False,
        )

        # Verify — vray binary lands under $prefix/vray/bin.
        vray_bin = vray_install_dir / "vray" / "bin" / "vray"
        if vray_bin.exists():
            print(f"SUCCESS: vray binary found at {vray_bin}")
        else:
            print(f"WARNING: vray binary not found at {vray_bin}, dumping install tree:")
            run(["find", str(vray_install_dir), "-maxdepth", "3"], check=False)
        marker.touch()

        installer_path.unlink(missing_ok=True)
    finally:
        lock_file.unlink(missing_ok=True)


def _install_redshift_linux() -> None:
    """Install Redshift once; it plugs into every Maya version at runtime."""
    redshift_root = Path("/usr/redshift")
    marker = redshift_root / ".installed"
    if marker.exists():
        print("Redshift already installed")
        return

    lock_file = Path("/tmp/redshift.lock")
    if lock_file.exists():
        print("Waiting for concurrent Redshift install...")
        for _ in range(180):
            time.sleep(1)
            if marker.exists():
                break
        return

    lock_file.touch()
    try:
        s3_key = REDSHIFT_CONFIG["linux"]["s3_key"]
        installer_name = Path(s3_key).name
        installer_path = Path(f"/tmp/{installer_name}")

        print("Installing Redshift...")
        download_from_s3(s3_key, installer_path)
        verify_checksum(installer_path, REDSHIFT_CONFIG["linux"]["checksum"])

        run(["chmod", "+x", str(installer_path)])
        # Redshift is a Makeself archive. Extract then untar the package.
        redshift_root.mkdir(parents=True, exist_ok=True)
        extract_tmp = Path("/tmp/redshift-extract")
        if extract_tmp.exists():
            run(["rm", "-rf", str(extract_tmp)], check=False)
        run([str(installer_path), "--noexec", "--target", str(extract_tmp)])
        # Extract the tarball into the install dir
        pkg_tar = next(extract_tmp.glob("*.tar.gz"), None)
        if pkg_tar:
            run(["tar", "xzf", str(pkg_tar), "-C", str(redshift_root)])
        else:
            print(f"ERROR: No .tar.gz found in {extract_tmp}")
            run(["ls", "-la", str(extract_tmp)], check=False)
            sys.exit(1)
        run(["rm", "-rf", str(extract_tmp)], check=False)

        # Verify — redshiftCmdLine lands in $prefix/bin.
        redshift_cmd = redshift_root / "bin" / "redshiftCmdLine"
        if redshift_cmd.exists():
            print(f"SUCCESS: redshiftCmdLine found at {redshift_cmd}")
        else:
            print(f"WARNING: redshiftCmdLine not found at {redshift_cmd}, dumping install tree:")
            run(["find", str(redshift_root), "-maxdepth", "3"], check=False)
        marker.touch()

        installer_path.unlink(missing_ok=True)
    finally:
        lock_file.unlink(missing_ok=True)


def _clean_stale_locks(maya_versions: Sequence[str], plat: str) -> None:
    """Remove stale lock files from previous failed runs on reserved capacity fleets."""
    for version in maya_versions:
        lock_file = Path(f"/tmp/maya-{version}.lock")
        if lock_file.exists():
            print(f"Removing stale lock file for Maya {version}")
            lock_file.unlink()
            print(f"Removing stale lock file for Maya {version}")
            lock_file.unlink()


def setup_linux(maya_versions: Sequence[str], renderers: Sequence[str]) -> None:
    pkg_mgr = (
        "dnf"
        if subprocess.run(["command", "-v", "dnf"], capture_output=True, check=False).returncode
        == 0
        else "yum"
    )

    # Install dependencies needed by Maya
    run(
        [
            pkg_mgr,
            "install",
            "-y",
            "libGLU",
            "mesa-libGL",
            "mesa-libEGL",
            "libXmu",
            "libXt",
            "libXi",
            "libXext",
            "libX11",
            "libXrender",
            "libXrandr",
            "libXfixes",
            "libXcursor",
            "libXinerama",
            "libxkbcommon",
            "libxkbcommon-x11",
            "fontconfig",
            "xorg-x11-server-Xvfb",
            "libva",
            "libvdpau",
            "pciutils-libs",
            "libglvnd-opengl",
            "libglvnd-egl",
            "alsa-lib",
            "nss",
        ]
    )

    # Start Xvfb if not already running
    if run(["pgrep", "Xvfb"], check=False).returncode != 0:
        subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1024x768x24"])
        run(["sleep", "2"])
        print("\nXvfb started. DISPLAY=:99")

    # Clean stale lock files from previous failed runs (reserved capacity persists)
    _clean_stale_locks(maya_versions, "linux")

    # Install Maya first — MtoA/V-Ray/Redshift plug into an existing Maya install.
    for version in maya_versions:
        _install_maya_linux(version)

    # Install the submitter and deps into each Maya version
    for version in maya_versions:
        maya_dir = Path(f"/opt/Autodesk/mayaio/{version}")
        # Find mayapy
        result = subprocess.run(
            ["find", str(maya_dir), "-name", "mayapy", "-type", "f"],
            capture_output=True, text=True, check=False,
        )
        mayapy_exe = Path(result.stdout.strip().split("\n")[0]) if result.stdout.strip() else None
        if not mayapy_exe or not mayapy_exe.exists():
            print(f"ERROR: Cannot find mayapy for Maya {version}")
            sys.exit(1)

        print(f"Installing submitter for Maya {version}...")
        run_with_timeout(
            ["hatch", "run", "install", "--maya-version", version],
            timeout=DEFAULT_CMD_TIMEOUT,
            label=f"hatch install submitter (Maya {version})",
        )

        # Maya's bundled Python lacks SSL, so we can't use mayapy -m pip.
        # Use system pip with --target to install into Maya's site-packages.
        maya_site_packages = mayapy_exe.parent.parent / "lib" / f"python{MAYA_VERSION_CONFIG[version]['python']}" / "site-packages"
        maya_site_packages.mkdir(parents=True, exist_ok=True)
        python_version = MAYA_VERSION_CONFIG[version]["python"]

        print(f"Installing integ test dependencies for Maya {version}...")
        run_with_timeout(
            [
                "pip", "install",
                "--target", str(maya_site_packages),
                "--python-version", python_version,
                "--only-binary=:all:",
                "-r", "requirements-integ-testing.txt",
                "-r", "requirements-testing.txt",
            ],
            timeout=DEFAULT_CMD_TIMEOUT,
            label=f"pip install requirements (Maya {version})",
        )

        # Install the package itself
        run_with_timeout(
            [
                "pip", "install",
                "--target", str(maya_site_packages),
                "--python-version", python_version,
                "--only-binary=:all:",
                ".",
            ],
            timeout=DEFAULT_CMD_TIMEOUT,
            label=f"pip install project (Maya {version})",
        )

        # Symlink mayapy to PATH so hatch integ-ci:test can find it.
        # Create a wrapper that sets MAYA_LOCATION and renderer plugin paths.
        mayapy_dir = mayapy_exe.parent.parent  # e.g. /opt/.../usr/autodesk/mayaIO2025

        # Renderer paths
        mtoa_dir = f"/opt/solidangle/mtoa/{version}"
        vray_dir = f"/usr/ChaosGroup/V-Ray/Maya{version}-x64"
        redshift_dir = "/usr/redshift"

        module_paths = ":".join([
            mtoa_dir,  # contains mtoa.mod
            f"{vray_dir}/maya_root/modules",  # contains VRayForMaya.module
        ])
        plugin_paths = f"{redshift_dir}/redshift4maya/{version}"
        script_paths = f"{redshift_dir}/redshift4maya/common/scripts"
        render_desc_paths = f"{redshift_dir}/redshift4maya/common/rendererDesc"

        wrapper = Path("/usr/local/bin/mayapy")
        wrapper.write_text(
            f"#!/bin/sh\n"
            f"export MAYA_LOCATION=\"{mayapy_dir}\"\n"
            f"export LD_LIBRARY_PATH=\"{mayapy_dir}/lib:${{LD_LIBRARY_PATH:-}}\"\n"
            f"export MAYA_MODULE_PATH=\"{module_paths}:${{MAYA_MODULE_PATH:-}}\"\n"
            f"export MAYA_PLUG_IN_PATH=\"{plugin_paths}:${{MAYA_PLUG_IN_PATH:-}}\"\n"
            f"export MAYA_SCRIPT_PATH=\"{script_paths}:${{MAYA_SCRIPT_PATH:-}}\"\n"
            f"export MAYA_RENDER_DESC_PATH=\"{render_desc_paths}:${{MAYA_RENDER_DESC_PATH:-}}\"\n"
            f"export REDSHIFT_COREDATAPATH=\"{redshift_dir}\"\n"
            f"exec \"{mayapy_exe}\" \"$@\"\n"
        )
        run(["chmod", "+x", str(wrapper)])

    # Install requested renderers (always per-Maya-version, except Redshift which
    # is shared across versions).
    if "mtoa" in renderers:
        for version in maya_versions:
            _install_mtoa_linux(version)
    if "vray" in renderers:
        for version in maya_versions:
            _install_vray_linux(version)
    if "redshift" in renderers:
        _install_redshift_linux()


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def _install_maya_windows(version: str) -> Path:
    config = MAYA_VERSION_CONFIG[version]
    installer_name = config["installer"]["windows"]
    maya_dir = Path(f"C:/Program Files/Autodesk/Maya{version}")
    maya_marker = maya_dir / ".installed"

    if maya_marker.exists():
        print(f"Maya {version} already installed")
        return maya_dir

    print(f"Installing Maya {version}...")
    setup_dir = Path(f"C:/maya_setup/{version}")
    setup_dir.mkdir(parents=True, exist_ok=True)
    installer_zip = setup_dir / installer_name

    download_from_s3(f"maya/{version}/{installer_name}", installer_zip)
    verify_checksum(installer_zip, MAYA_CHECKSUMS[version].get("windows", ""))

    print("Extracting Maya installer...")
    run(
        [
            "powershell",
            "-Command",
            f"Expand-Archive -Path '{installer_zip}' -DestinationPath '{setup_dir}' -Force",
        ]
    )

    # The zip contains _001_002.exe (GUI, hangs headlessly) + _002_002.7z (payload).
    # Extract the .7z directly with 7-Zip, then run Setup.exe -q.
    seven_z = next(setup_dir.rglob("*_002_002.7z"), None)
    if seven_z is None:
        # Fallback: maybe it's a zip that already contains Setup.exe (e.g. Maya 2024)
        setup_exe = next(setup_dir.rglob("Setup.exe"), None)
    else:
        extract_dest = setup_dir / "extracted"
        extract_dest.mkdir(parents=True, exist_ok=True)
        print(f"Extracting 7z payload: {seven_z}")
        run(
            [
                "powershell",
                "-Command",
                f'& "C:\\Program Files\\7-Zip\\7z.exe" x "{seven_z}" "-o{extract_dest}" -y',
            ]
        )
        setup_exe = next(extract_dest.rglob("Setup.exe"), None)

    if setup_exe is None:
        print(f"ERROR: Setup.exe not found under {setup_dir}")
        run(["powershell", "-Command", f"Get-ChildItem -Recurse '{setup_dir}'"], check=False)
        sys.exit(1)
        sys.exit(1)

    print(f"Starting Maya installation via {setup_exe}...")
    result = subprocess.run(
        [
            "powershell",
            "-Command",
            f'Start-Process "{setup_exe}" -ArgumentList "-q" -Wait',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    print(f"Installation exit code: {result.returncode}")
    if result.stdout:
        print(f"Installation output: {result.stdout}")
    if result.stderr:
        print(f"Installation errors: {result.stderr}")

    mayapy_exe = maya_dir / "bin" / "mayapy.exe"
    if mayapy_exe.exists():
        print(f"SUCCESS: mayapy.exe found at {mayapy_exe}")
        maya_marker.touch()
    else:
        print(f"ERROR: mayapy.exe NOT found at {mayapy_exe}")
        run(["powershell", "-Command", f"Get-ChildItem -Recurse '{maya_dir}'"], check=False)
        sys.exit(1)

    # Cleanup extracted installer to reclaim disk space.
    run(
        ["powershell", "-Command", f"Remove-Item -Path '{setup_dir}' -Recurse -Force"],
        check=False,
    )

    return maya_dir


def _install_vray_windows(version: str) -> None:
    """Install V-Ray for Maya on Windows."""
    vray_win_config = {
        "2025": "maya-vray/70002/vray_adv_70002_maya2025_x64.exe",
        "2026": "maya-vray/71002/vray_adv_71002_maya2026_x64.exe",
    }
    if version not in vray_win_config:
        print(f"WARNING: No Windows V-Ray config for Maya {version}, skipping")
        return
    # V-Ray installs to Program Files and registers its .module with Maya automatically
    plugin_check = Path(f"C:/Program Files/Chaos/V-Ray/Maya {version} for x64/maya_vray/plug-ins/vrayformaya.mll")
    if plugin_check.exists():
        print(f"V-Ray for Maya {version} already installed")
        return
    s3_key = vray_win_config[version]
    installer_path = Path(f"C:/temp/vray_{version}.exe")
    installer_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Installing V-Ray for Maya {version}...")
    download_from_s3(s3_key, installer_path)
    run(
        ["powershell", "-Command",
         f'Start-Process "{installer_path}" -ArgumentList "-gui=0","-auto","-quiet=1" -Wait -NoNewWindow']
    )
    installer_path.unlink(missing_ok=True)


def _install_mtoa_windows(version: str) -> None:
    """Install MtoA (Arnold) for Maya on Windows."""
    mtoa_win_config = {
        "2025": "mtoa/5.5/MtoA-5.5.4.2-windows-2025.msi",
        "2026": "mtoa/5.5/MtoA-5.5.4.2-windows-2026.msi",
    }
    if version not in mtoa_win_config:
        print(f"WARNING: No Windows MtoA config for Maya {version}, skipping")
        return
    plugin_check = Path(f"C:/Program Files/Autodesk/Arnold/maya{version}/plug-ins/mtoa.mll")
    if plugin_check.exists():
        print(f"MtoA for Maya {version} already installed")
        return
    s3_key = mtoa_win_config[version]
    installer_path = Path(f"C:/temp/mtoa_{version}.msi")
    installer_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"Installing MtoA for Maya {version}...")
    download_from_s3(s3_key, installer_path)
    run(
        ["powershell", "-Command",
         f'Start-Process "msiexec" -ArgumentList "/i","{installer_path}","/quiet","/norestart" -Wait -NoNewWindow']
    )
    installer_path.unlink(missing_ok=True)


def _install_redshift_windows() -> None:
    """Install Redshift on Windows with Maya plugin registration."""
    redshift_root = Path("C:/Program Files/Maxon Redshift 2026")
    plugin_check = redshift_root / "Plugins" / "Maya" / "2025" / "nt-x86-64" / "redshift4maya.mll"
    if plugin_check.exists():
        print("Redshift already installed")
        return
    s3_key = "redshift/2026/redshift_2026.6.0_2497872080_win_x64.exe"
    installer_path = Path("C:/temp/redshift_install.exe")
    installer_path.parent.mkdir(parents=True, exist_ok=True)
    print("Installing Redshift...")
    download_from_s3(s3_key, installer_path)
    # InstallBuilder with Maya plugin components enabled
    run(
        ["powershell", "-Command",
         f'Start-Process "{installer_path}" -ArgumentList "--mode","unattended","--enable-components","MayaGroup,PluginMaya2025,PluginMaya2026" -Wait -NoNewWindow']
    )
    installer_path.unlink(missing_ok=True)

    # Register Redshift with each Maya version
    for ver in ["2025", "2026"]:
        maya_env_dir = Path(f"C:/Users/Default/Documents/maya/{ver}")
        maya_env_dir.mkdir(parents=True, exist_ok=True)
        maya_env_file = maya_env_dir / "Maya.env"
        if not maya_env_file.exists():
            maya_env_file.touch()
        # Run the registration tool
        reg_tool = redshift_root / "Tools" / "Redshift4MayaEnv.exe"
        if reg_tool.exists():
            run([str(reg_tool), str(maya_env_file), str(redshift_root), ver])
        # Copy renderer descriptor
        renderer_xml = redshift_root / "Plugins" / "Maya" / "Common" / "rendererDesc" / "redshiftRenderer.xml"
        maya_renderer_dir = Path(f"C:/Program Files/Autodesk/Maya{ver}/bin/rendererDesc")
        if renderer_xml.exists() and maya_renderer_dir.exists():
            run(["powershell", "-Command",
                 f'Copy-Item "{renderer_xml}" "{maya_renderer_dir}" -Force'])


def _register_pywin32() -> None:
    """Register pywin32 DLLs so child processes (mayapy) can load win32file.

    Mirrors the pattern used by deadline-cloud-for-3ds-max setup-runner.
    """
    print("Running pywin32 post-install script...")
    env_root = Path(sys.executable).parent.parent
    postinstall = (
        env_root / "Lib" / "site-packages" / "win32" / "scripts" / "pywin32_postinstall.py"
    )
    if postinstall.exists():
        run([sys.executable, str(postinstall), "-install"])
        return

    # Fallback: copy DLLs manually
    print(f"pywin32_postinstall.py not found at {postinstall}, copying DLLs manually...")
    pywin32_system32 = env_root / "Lib" / "site-packages" / "pywin32_system32"
    if not pywin32_system32.exists():
        print("ERROR: pywin32_system32 directory not found")
        sys.exit(1)

    for dll in pywin32_system32.glob("*.dll"):
        dest = Path("C:/Windows/System32") / dll.name
        if not dest.exists():
            print(f"Copying {dll.name} to System32")
            shutil.copy2(str(dll), str(dest))


def setup_windows(maya_versions: Sequence[str], renderers: Sequence[str]) -> None:
    _clean_stale_locks(maya_versions, "windows")

    # Remove stale mayapy copies from previous runs that break PATH resolution
    for stale in [Path("C:/Windows/mayapy.exe"), Path("C:/Windows/mayapy.cmd")]:
        if stale.exists():
            stale.unlink(missing_ok=True)

    # Ensure 7-Zip is available (needed to extract Maya .7z installer)
    seven_zip = Path("C:/Program Files/7-Zip/7z.exe")
    if not seven_zip.exists():
        print("Installing 7-Zip...")
        run(
            ["powershell", "-Command",
             "Invoke-WebRequest -Uri 'https://www.7-zip.org/a/7z2408-x64.exe' -OutFile C:\\temp\\7z-install.exe; "
             "Start-Process C:\\temp\\7z-install.exe -ArgumentList '/S' -Wait"]
        )

    for version in maya_versions:
        _install_maya_windows(version)

    # Install the submitter and deps into each Maya version
    for version in maya_versions:
        maya_dir = Path(f"C:/Program Files/Autodesk/Maya{version}")
        mayapy_exe = maya_dir / "bin" / "mayapy.exe"

        print(f"Installing submitter for Maya {version}...")
        run(["hatch", "run", "install", "--maya-version", version])

        print(f"Installing integ test dependencies for Maya {version}...")
        run(
            [
                str(mayapy_exe),
                "-m",
                "pip",
                "install",
                "-r",
                "requirements-integ-testing.txt",
                "-r",
                "requirements-testing.txt",
            ]
        )

        run([str(mayapy_exe), "-m", "pip", "install", "."])

    # Renderer installers for Windows are not yet in S3. Surface a clear message
    # rather than silently skipping so CI doesn't falsely pass renderer tests.
    # Install renderers on Windows
    if "vray" in renderers:
        for version in maya_versions:
            _install_vray_windows(version)
    if "mtoa" in renderers:
        for version in maya_versions:
            _install_mtoa_windows(version)
    if "redshift" in renderers:
        _install_redshift_windows()

    _register_pywin32()


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------


def _install_maya_macos(version: str) -> Path:
    config = MAYA_VERSION_CONFIG[version]
    installer_name = config["installer"]["macos"]
    maya_app = Path(f"/Applications/Autodesk/maya{version}/Maya.app")
    # Marker lives outside the .app so reinstalling Maya doesn't preserve a stale marker.
    marker = Path(f"~/Library/Application Support/.maya-{version}-installed").expanduser()
    marker.parent.mkdir(parents=True, exist_ok=True)

    if marker.exists():
        print(f"Maya {version} already installed")
        return maya_app

    lock_file = Path(f"/tmp/maya-{version}.lock")
    if lock_file.exists():
        print(f"Waiting for concurrent Maya {version} install...")
        for _ in range(120):
            time.sleep(1)
            if marker.exists():
                break
        return maya_app

    lock_file.touch()
    try:
        installer_path = Path(f"/tmp/{installer_name}")

        print(f"Installing Maya {version}...")
        download_from_s3(f"maya/{version}/{installer_name}", installer_path)
        verify_checksum(installer_path, MAYA_CHECKSUMS[version].get("macos", ""))

        mount_point = Path(f"/tmp/maya-{version}-mount")
        mount_point.mkdir(parents=True, exist_ok=True)

        # Attach the DMG to a deterministic mountpoint so we know where to look for the .pkg.
        run(
            [
                "hdiutil",
                "attach",
                str(installer_path),
                "-mountpoint",
                str(mount_point),
                "-nobrowse",
            ]
        )
        try:
            # Maya macOS DMGs contain "Install Maya XXXX.app" which uses Autodesk's
            # ODIS installer. The actual .pkg files are inside the app bundle at
            # Contents/Helper/Packages/Maya/. We install the core pkg directly.
            app = next(mount_point.glob("Install Maya*.app"), None)
            if app is None:
                # Fallback: look for a top-level .pkg
                pkg = next(mount_point.glob("*.pkg"), None)
                if pkg is None:
                    print(f"ERROR: No Install Maya*.app or .pkg found in DMG at {mount_point}")
                    run(["ls", "-laR", str(mount_point)], check=False)
                    sys.exit(1)
                run(["sudo", "installer", "-pkg", str(pkg), "-target", "/"])
            else:
                packages_dir = app / "Contents" / "Helper" / "Packages" / "Maya"
                if not packages_dir.exists():
                    print(f"ERROR: Packages directory not found at {packages_dir}")
                    run(["find", str(app), "-name", "*.pkg"], check=False)
                    sys.exit(1)
                # Install Maya core package (required) and other key packages
                core_pkg = next(packages_dir.glob("Maya_core*.pkg"), None)
                if core_pkg is None:
                    print(f"ERROR: Maya_core*.pkg not found in {packages_dir}")
                    run(["ls", "-la", str(packages_dir)], check=False)
                    sys.exit(1)
                print(f"Installing {core_pkg.name}...")
                run(["sudo", "installer", "-pkg", str(core_pkg), "-target", "/"])
                # Install additional packages needed for rendering
                for pkg in sorted(packages_dir.glob("*.pkg")):
                    if pkg == core_pkg:
                        continue
                    print(f"Installing {pkg.name}...")
                    run(["sudo", "installer", "-pkg", str(pkg), "-target", "/"], check=False)
        finally:
            run(["hdiutil", "detach", str(mount_point)], check=False)

        mayapy_exe = maya_app / "Contents" / "bin" / "mayapy"
        if mayapy_exe.exists():
            print(f"SUCCESS: mayapy found at {mayapy_exe}")
            marker.touch()
        else:
            print(f"ERROR: mayapy NOT found at {mayapy_exe}")
            run(
                ["find", "/Applications/Autodesk", "-name", "mayapy"],
                check=False,
            )
            sys.exit(1)

        installer_path.unlink(missing_ok=True)
    finally:
        lock_file.unlink(missing_ok=True)

    return maya_app


def _install_mtoa_macos(version: str) -> None:
    """Install MtoA on macOS via .pkg."""
    mtoa_s3 = {"2025": "mtoa/5.5/MtoA-5.5.4.2-darwin-2025.pkg", "2026": "mtoa/5.5/MtoA-5.5.4.2-darwin-2026.pkg"}
    if version not in mtoa_s3:
        print(f"WARNING: No macOS MtoA for Maya {version}, skipping")
        return
    # Check if already installed
    if Path(f"/opt/solidangle/mtoa/{version}/plug-ins/mtoa.bundle").exists():
        print(f"MtoA for Maya {version} already installed")
        return
    pkg_path = Path(f"/tmp/mtoa_{version}.pkg")
    print(f"Installing MtoA for Maya {version}...")
    download_from_s3(mtoa_s3[version], pkg_path)
    run(["sudo", "installer", "-pkg", str(pkg_path), "-target", "/"])
    pkg_path.unlink(missing_ok=True)


def _install_vray_macos(version: str) -> None:
    """Install V-Ray on macOS via DMG + Chaos installer."""
    vray_s3 = {"2025": "maya-vray/71000/vray_adv_71000_maya2025_bigsur_univ.dmg", "2026": "maya-vray/71002/vray_adv_71002_maya2026_bigsur_univ.dmg"}
    if version not in vray_s3:
        print(f"WARNING: No macOS V-Ray for Maya {version}, skipping")
        return
    install_dir = Path(f"/opt/vray/maya{version}")
    if (install_dir / "maya_vray/plug-ins/vrayformaya.bundle").exists():
        print(f"V-Ray for Maya {version} already installed")
        return
    dmg_path = Path(f"/tmp/vray_{version}.dmg")
    mount_point = Path(f"/tmp/vray-{version}-mount")
    print(f"Installing V-Ray for Maya {version}...")
    download_from_s3(vray_s3[version], dmg_path)
    mount_point.mkdir(parents=True, exist_ok=True)
    run(["hdiutil", "attach", str(dmg_path), "-mountpoint", str(mount_point), "-nobrowse"])
    try:
        # Find the .app installer
        app = next(mount_point.glob("*.app"), None)
        if app:
            run(["sudo", "mkdir", "-p", str(install_dir)])
            run(
                ["sudo", str(app / "Contents/MacOS/run_installer"), "-gui=0", "-auto", "-quiet=1", f"-unpackInstall={install_dir}"],
                cwd=app / "Contents/MacOS",
            )
    finally:
        run(["hdiutil", "detach", str(mount_point)], check=False)
    dmg_path.unlink(missing_ok=True)


def _install_redshift_macos(maya_versions: Sequence[str]) -> None:
    """Install Redshift on macOS via DMG + InstallBuilder."""
    redshift_root = Path("/opt/redshift")
    if (redshift_root / "Plugins/Maya/2025/redshift4maya.bundle").exists():
        print("Redshift already installed")
        return
    s3_key = "redshift/2026/redshift_2026.6.0_2497872080_macos.dmg"
    dmg_path = Path("/tmp/redshift.dmg")
    mount_point = Path("/tmp/redshift-mount")
    print("Installing Redshift...")
    download_from_s3(s3_key, dmg_path)
    mount_point.mkdir(parents=True, exist_ok=True)
    run(["hdiutil", "attach", str(dmg_path), "-mountpoint", str(mount_point), "-nobrowse"])
    try:
        app = next(mount_point.glob("*.app"), None)
        if app:
            # Use the arm64 binary directly with sudo
            components = ",".join([f"PluginMaya{v}" for v in maya_versions])
            run(
                ["sudo", str(app / "Contents/MacOS/osx-arm64"),
                 "--mode", "unattended",
                 "--enable-components", f"MayaGroup,{components}",
                 "--prefix", str(redshift_root)],
            )
    finally:
        run(["hdiutil", "detach", str(mount_point)], check=False)
    dmg_path.unlink(missing_ok=True)


def setup_macos(maya_versions: Sequence[str], renderers: Sequence[str]) -> None:
    _clean_stale_locks(maya_versions, "macos")
    for version in maya_versions:
        _install_maya_macos(version)

    # Create a single version-aware wrapper that uses MAYA_VERSION env var
    # (set by hatch matrix) to pick the right Maya binary
    versions_str = " ".join(maya_versions)
    wrapper_content = (
        f"#!/bin/sh\n"
        f"VER=\"${{MAYA_VERSION:-{maya_versions[0]}}}\"\n"
        f"MAYA_APP=\"/Applications/Autodesk/maya$VER/Maya.app\"\n"
        f"export MAYA_LOCATION=\"$MAYA_APP/Contents\"\n"
        f"export DYLD_LIBRARY_PATH=\"$MAYA_APP/Contents/MacOS:$MAYA_APP/../plug-ins/xgen/lib\"\n"
        f"export PYTHONPATH=\"$HOME/maya-deps/$VER/site-packages:${{PYTHONPATH:-}}\"\n"
        f"# Renderer module paths\n"
        f"export MAYA_MODULE_PATH=\"/opt/solidangle/mtoa/$VER:/opt/vray/maya$VER/maya_root/modules:${{MAYA_MODULE_PATH:-}}\"\n"
        f"export MAYA_PLUG_IN_PATH=\"/opt/redshift/Plugins/Maya/$VER:${{MAYA_PLUG_IN_PATH:-}}\"\n"
        f"export MAYA_SCRIPT_PATH=\"/opt/redshift/Plugins/Maya/Common/scripts:${{MAYA_SCRIPT_PATH:-}}\"\n"
        f"export MAYA_RENDER_DESC_PATH=\"/opt/redshift/Plugins/Maya/Common/rendererDesc:${{MAYA_RENDER_DESC_PATH:-}}\"\n"
        f"export REDSHIFT_COREDATAPATH=\"/opt/redshift\"\n"
        f"exec \"$MAYA_APP/Contents/bin/mayapy\" \"$@\"\n"
    )
    wrapper = Path("/tmp/mayapy_wrapper.sh")
    wrapper.write_text(wrapper_content)
    run(["sudo", "cp", str(wrapper), "/usr/local/bin/mayapy"])
    run(["sudo", "chmod", "+x", "/usr/local/bin/mayapy"])

    for version in maya_versions:
        maya_app = Path(f"/Applications/Autodesk/maya{version}/Maya.app")
        mayapy_exe = maya_app / "Contents" / "bin" / "mayapy"
        maya_env = {
            "MAYA_LOCATION": f"{maya_app}/Contents",
            "DYLD_LIBRARY_PATH": f"{maya_app}/Contents/MacOS",
        }

        print(f"Installing submitter for Maya {version}...")
        run_with_timeout(
            ["hatch", "run", "install", "--maya-version", version],
            timeout=DEFAULT_CMD_TIMEOUT,
            label=f"hatch install submitter (Maya {version})",
        )

        # Maya's bundled Python may lack SSL, use system pip with --target
        # Install to a writable location since Maya's site-packages is owned by root
        maya_site_packages = Path.home() / f"maya-deps/{version}/site-packages"
        maya_site_packages.mkdir(parents=True, exist_ok=True)
        python_version = MAYA_VERSION_CONFIG[version]["python"]

        print(f"Installing integ test dependencies for Maya {version}...")
        run_with_timeout(
            [
                "pip", "install",
                "--target", str(maya_site_packages),
                "--python-version", python_version,
                "--platform", "macosx_11_0_arm64",
                "--implementation", "cp",
                "--only-binary=:all:",
                "-r", "requirements-integ-testing.txt",
                "-r", "requirements-testing.txt",
            ],
            timeout=DEFAULT_CMD_TIMEOUT,
            label=f"pip install requirements (Maya {version})",
        )

        run_with_timeout(
            [
                "pip", "install",
                "--target", str(maya_site_packages),
                "--python-version", python_version,
                "--platform", "macosx_11_0_arm64",
                "--implementation", "cp",
                "--only-binary=:all:",
                ".",
            ],
            timeout=DEFAULT_CMD_TIMEOUT,
            label=f"pip install project (Maya {version})",
        )

    # Install renderers on macOS
    if "mtoa" in renderers:
        for version in maya_versions:
            _install_mtoa_macos(version)
    if "vray" in renderers:
        for version in maya_versions:
            _install_vray_macos(version)
    if "redshift" in renderers:
        _install_redshift_macos(maya_versions)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Setup Maya test environment")
    parser.add_argument("--versions", nargs="+", help="Maya versions to install (e.g., 2025 2026)")
    parser.add_argument(
        "--renderers",
        nargs="*",
        default=[],
        choices=SUPPORTED_RENDERERS,
        help=(
            "Third-party renderers to install alongside Maya "
            "(currently supported on Linux only)."
        ),
    )
    args = parser.parse_args()

    maya_versions = args.versions if args.versions else list(MAYA_VERSION_CONFIG.keys())
    renderers = list(dict.fromkeys(args.renderers))  # dedupe, preserve order

    system = platform.system()
    plat_key = _platform_key()
    print(
        f"Setting up {system} with Maya {', '.join(maya_versions)}"
        + (f" and renderers {', '.join(renderers)}" if renderers else "")
    )

    # Validate versions
    for v in maya_versions:
        if v not in MAYA_VERSION_CONFIG:
            print(f"ERROR: Unsupported Maya version: {v}")
            print(f"Supported versions: {list(MAYA_VERSION_CONFIG.keys())}")
            sys.exit(1)
        if plat_key not in MAYA_VERSION_CONFIG[v].get("installer", {}):
            print(f"ERROR: No {system} installer configured for Maya {v}")
            sys.exit(1)

    if system == "Linux":
        setup_linux(maya_versions, renderers)
    elif system == "Windows":
        setup_windows(maya_versions, renderers)
    elif system == "Darwin":
        setup_macos(maya_versions, renderers)
    else:
        print(f"ERROR: Unsupported platform: {system}")
        sys.exit(1)

    print("Setup complete!")
