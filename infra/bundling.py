"""Bundle the Lambda package locally with pip (no Docker needed).

Installs manylinux wheels for the Lambda architecture, then copies src/shuto.
Falls back to Docker bundling only if local bundling fails.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import jsii
from aws_cdk import BundlingOptions, DockerImage, ILocalBundling
from aws_cdk import aws_lambda as _lambda

SRC_DIR = Path(__file__).resolve().parent.parent / "src"


@jsii.implements(ILocalBundling)
class PipLocalBundling:
    def __init__(self, arm64: bool, python_version: str) -> None:
        cpu = "aarch64" if arm64 else "x86_64"
        self.platform = f"manylinux2014_{cpu}"  # pip --platform
        self.uv_platform = f"{cpu}-manylinux2014"  # uv --python-platform
        self.python_version = python_version

    def try_bundle(self, output_dir: str, *, image=None, **_kwargs) -> bool:  # noqa: ARG002
        req = str(SRC_DIR / "requirements.txt")
        # pip first; uv as a fallback (uv-created venvs have no pip).
        commands = [
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--quiet",
                "-r",
                req,
                "--target",
                output_dir,
                "--platform",
                self.platform,
                "--implementation",
                "cp",
                "--python-version",
                self.python_version,
                "--only-binary=:all:",
                "--upgrade",
            ]
        ]
        uv = shutil.which("uv")
        if uv:
            commands.append(
                [
                    uv,
                    "pip",
                    "install",
                    "--quiet",
                    "-r",
                    req,
                    "--target",
                    output_dir,
                    "--python-platform",
                    self.uv_platform,
                    "--python-version",
                    self.python_version,
                    "--only-binary",
                    ":all:",
                ]
            )
        for cmd in commands:
            try:
                subprocess.run(cmd, check=True)
                break
            except (OSError, subprocess.CalledProcessError) as exc:
                print(f"[shuto] {cmd[0]} bundling failed: {exc}")
        else:
            print("[shuto] local bundling failed, falling back to Docker")
            return False
        shutil.copytree(
            SRC_DIR / "shuto",
            Path(output_dir) / "shuto",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        return True


def lambda_code(runtime: _lambda.Runtime, arch: _lambda.Architecture) -> _lambda.Code:
    # Compare by name: jsii returns a new proxy object each time, so `==` is always False.
    arm64 = arch.name == _lambda.Architecture.ARM_64.name
    py_version = runtime.name.replace("python", "")  # "python3.13" -> "3.13"
    return _lambda.Code.from_asset(
        str(SRC_DIR),
        bundling=BundlingOptions(
            image=DockerImage.from_registry(f"public.ecr.aws/sam/build-{runtime.name}"),
            platform="linux/arm64" if arm64 else "linux/amd64",
            local=PipLocalBundling(arm64, py_version),
            command=[
                "bash",
                "-c",
                "pip install -r requirements.txt -t /asset-output && cp -r shuto /asset-output/",
            ],
        ),
    )
