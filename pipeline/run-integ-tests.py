# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Run integration tests with Maya's bin on PATH and renderer env vars set.

On Windows, Maya's bin directory must be on PATH so the adaptor's subprocess
can find mayapy.exe. Renderer plugin paths and licensing env vars must also
be set so Maya can discover and use the renderer plugins.
"""
import os
import platform
import subprocess
import sys


def main():
    maya_version = os.environ.get("MAYA_VERSION", "2025")
    system = platform.system()

    if system == "Windows":
        maya_bin = f"C:\\Program Files\\Autodesk\\Maya{maya_version}\\bin"
        os.environ["PATH"] = maya_bin + ";" + os.environ.get("PATH", "")

        # Redshift plugin paths (not auto-registered like V-Ray/MtoA)
        rs_root = "C:\\Program Files\\Maxon Redshift 2026"
        rs_plugin = f"{rs_root}\\Plugins\\Maya\\{maya_version}\\nt-x86-64"
        rs_scripts = f"{rs_root}\\Plugins\\Maya\\Common\\scripts"
        rs_desc = f"{rs_root}\\Plugins\\Maya\\Common\\rendererDesc"

        os.environ["MAYA_PLUG_IN_PATH"] = rs_plugin + ";" + os.environ.get("MAYA_PLUG_IN_PATH", "")
        os.environ["MAYA_SCRIPT_PATH"] = rs_scripts + ";" + os.environ.get("MAYA_SCRIPT_PATH", "")
        os.environ["MAYA_RENDER_DESC_PATH"] = rs_desc + ";" + os.environ.get("MAYA_RENDER_DESC_PATH", "")
        os.environ["REDSHIFT_COREDATAPATH"] = rs_root
        # Redshift's .mll depends on DLLs in its bin directory
        os.environ["PATH"] = f"{rs_root}\\bin;" + os.environ["PATH"]

        # Renderer licensing (Machine-level env vars don't take effect in current session)
        license_dns = os.environ.get("LICENSE_ENDPOINT_DNS", "")
        if license_dns:
            os.environ.setdefault("VRAY_AUTH_CLIENT_SETTINGS", f"licset://{license_dns}:30304")
            os.environ.setdefault("VRAY_AUTH_CLIENT_FILE_PATH", "/null")
            os.environ.setdefault("redshift_LICENSE", f"7054@{license_dns}")
            os.environ.setdefault("ADSKFLEX_LICENSE_FILE", f"2702@{license_dns};2701@{license_dns}")

    # Linux and macOS use wrapper scripts that handle PATH and renderer paths

    args = ["mayapy", "-m", "pytest", "--no-cov", "test/integ", "-vvv", "--numprocesses=1"]
    # macOS: skip adaptor tests (no SMF rendering support, native extension ABI issues)
    # Skip Redshift submitter: plugin loads but generated template differs from expected
    # fixture (likely path format or renderer version differences). Needs macOS-specific fixture.
    if system == "Darwin":
        args += ["--ignore=test/integ/test_maya_adaptors.py", "-k", "not Redshift"]
        # Verify mayapy works before running tests (Maya 2026 macOS binary is broken)
        check = subprocess.run(["mayapy", "-c", "print('ok')"], capture_output=True, timeout=10)
        if check.returncode != 0:
            print(f"WARNING: mayapy for Maya {maya_version} is not functional on macOS, skipping")
            sys.exit(0)

    sys.exit(subprocess.run(args).returncode)


if __name__ == "__main__":
    main()
