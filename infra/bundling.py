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
    def __init__(self, platform: str, python_version: str) -> None:
        self.platform = platform
        self.python_version = python_version

    def try_bundle(self, output_dir: str, *, image=None, **_kwargs) -> bool:  # noqa: ARG002
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--quiet",
                    "-r",
                    str(SRC_DIR / "requirements.txt"),
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
                ],
                check=True,
            )
            shutil.copytree(
                SRC_DIR / "shuto",
                Path(output_dir) / "shuto",
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[shuto] local bundling failed, falling back to Docker: {exc}")
            return False


def lambda_code(runtime: _lambda.Runtime, arch: _lambda.Architecture) -> _lambda.Code:
    platform = (
        "manylinux2014_aarch64" if arch == _lambda.Architecture.ARM_64 else "manylinux2014_x86_64"
    )
    py_version = runtime.name.replace("python", "")  # "python3.13" -> "3.13"
    return _lambda.Code.from_asset(
        str(SRC_DIR),
        bundling=BundlingOptions(
            image=DockerImage.from_registry(f"public.ecr.aws/sam/build-{runtime.name}"),
            local=PipLocalBundling(platform, py_version),
            command=[
                "bash",
                "-c",
                "pip install -r requirements.txt -t /asset-output && cp -r shuto /asset-output/",
            ],
        ),
    )
