# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Run integration tests with Maya's bin on PATH.

On Windows, Maya's bin directory must be on PATH so the adaptor's subprocess
can find mayapy.exe. This script adds it before invoking pytest via mayapy.
"""
import os
import platform
import subprocess
import sys


def main():
    maya_version = os.environ.get("MAYA_VERSION", "2025")

    if platform.system() == "Windows":
        maya_bin = f"C:\\Program Files\\Autodesk\\Maya{maya_version}\\bin"
        os.environ["PATH"] = maya_bin + ";" + os.environ.get("PATH", "")
    # Linux and macOS use wrapper scripts that handle PATH

    sys.exit(
        subprocess.run(
            ["mayapy", "-m", "pytest", "--no-cov", "test/integ", "-vvv", "--numprocesses=1"]
        ).returncode
    )


if __name__ == "__main__":
    main()
