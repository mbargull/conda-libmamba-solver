# Copyright (C) 2022 Anaconda, Inc
# Copyright (C) 2023 conda
# SPDX-License-Identifier: BSD-3-Clause
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pytest
from conda.common.compat import on_linux
from conda.common.io import env_vars
from conda.core.prefix_data import PrefixData
from conda.models.channel import Channel
from conda.testing.integration import _get_temp_prefix, make_temp_env
from conda.testing.integration import run_command as conda_inprocess

from .utils import conda_subprocess, write_env_config


def test_channel_matchspec():
    stdout, *_ = conda_inprocess(
        "create",
        _get_temp_prefix(),
        "--solver=libmamba",
        "--json",
        "--override-channels",
        "-c",
        "defaults",
        "conda-forge::libblas=*=*openblas",
        "python=3.9",
    )
    result = json.loads(stdout)
    assert result["success"] is True
    for record in result["actions"]["LINK"]:
        if record["name"] == "numpy":
            assert record["channel"] == "conda-forge"
        elif record["name"] == "python":
            assert record["channel"] == "pkgs/main"


def test_channels_prefixdata():
    """
    Make sure libmamba does not complain about missing channels
    used in previous commands.

    See https://github.com/conda/conda/issues/11790
    """
    with make_temp_env(
        "conda-forge::xz", "python", "--solver=libmamba", use_restricted_unicode=True
    ) as prefix:
        p = conda_subprocess(
            "install",
            "-yp",
            prefix,
            "pytest",
            "--solver=libmamba",
        )
        assert (
            "Selected channel specific (or force-reinstall) job, "
            "but package is not available from channel. "
            "Solve job will fail." not in (p.stdout + p.stderr)
        )


def test_channels_installed_unavailable():
    "Ensure we don't fail if a channel coming ONLY from an installed pkg is unavailable"
    with make_temp_env("xz", "--solver=libmamba", use_restricted_unicode=True) as prefix:
        pd = PrefixData(prefix)
        pd.load()
        record = pd.get("xz")
        assert record
        record.channel = Channel.from_url("file:///nonexistent")

        _, _, retcode = conda_inprocess(
            "install",
            prefix,
            "zlib",
            "--solver=libmamba",
            "--dry-run",
            use_exception_handler=True,
        )
        assert retcode == 0


def _setup_channels_alias(prefix):
    write_env_config(
        prefix,
        channels=["conda-forge", "defaults"],
        channel_alias="https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud",
        migrated_channel_aliases=["https://conda.anaconda.org"],
        default_channels=[
            "https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main",
            "https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/r",
            "https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/msys2",
        ],
    )


def _setup_channels_custom(prefix):
    write_env_config(
        prefix,
        channels=["conda-forge", "defaults"],
        custom_channels={
            "conda-forge": "https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud",
        },
    )


@pytest.mark.skipif(
    datetime.now() < datetime(2023, 6, 15),
    reason="Skip until 2023-06-15; remote server has been flaky lately",
)
@pytest.mark.parametrize(
    "config_env",
    (
        _setup_channels_alias,
        _setup_channels_custom,
    ),
)
def test_mirrors_do_not_leak_channels(config_env):
    """
    https://github.com/conda/conda-libmamba-solver/issues/108

    On existing environments, we load channels from the prefix data information
    to silence some warnings in libmamba (see `test_channels_prefixdata`).

    In some configurations that use proxies or Anaconda mirrors, this can lead to
    the non-mirrored (original) channels being loaded. In airgapped contexts, this
    is undesirable.
    """

    with env_vars({"CONDA_PKGS_DIRS": _get_temp_prefix()}), make_temp_env() as prefix:
        assert (Path(prefix) / "conda-meta" / "history").exists()

        # Setup conda configuration
        config_env(prefix)
        common = ["-yp", prefix, "--solver=libmamba", "--json", "-vv"]

        env = os.environ.copy()
        env["CONDA_PREFIX"] = prefix  # fake activation so config is loaded

        # Create an environment using mirrored channels only
        p = conda_subprocess("install", *common, "python", "pip", env=env)
        result = json.loads(p.stdout)
        if p.stderr:
            assert "conda.anaconda.org" not in p.stderr

        for pkg in result["actions"]["LINK"]:
            assert pkg["channel"] == "conda-forge", pkg
            assert (
                pkg["base_url"]
                == "https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge"
            ), pkg

        # Make a change to that channel
        p = conda_subprocess("install", *common, "pytest", env=env)

        # Ensure that the loaded channels are ONLY the mirrored ones
        result = json.loads(p.stdout)
        if p.stderr:
            assert "conda.anaconda.org" not in p.stderr

        for pkg in result["actions"]["LINK"]:
            assert pkg["channel"] == "conda-forge", pkg
            assert (
                pkg["base_url"]
                == "https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge"
            ), pkg

        # Ensure that other end points were never loaded


@pytest.mark.skipif(not on_linux, reason="Only run on Linux")
def test_jax_and_jaxlib():
    "https://github.com/conda/conda-libmamba-solver/issues/221"
    env = os.environ.copy()
    env["CONDA_SUBDIR"] = "linux-64"
    for specs in (("jax", "jaxlib"), ("jaxlib", "jax")):
        process = conda_subprocess(
            "create",
            "--name=unused",
            "--solver=libmamba",
            "--json",
            "--dry-run",
            "--override-channels",
            "-c",
            "defaults",
            f"conda-forge::{specs[0]}",
            f"conda-forge::{specs[1]}",
            explain=True,
            env=env,
        )
        result = json.loads(process.stdout)
        assert result["success"] is True
        pkgs = {pkg["name"] for pkg in result["actions"]["LINK"]}
        assert specs[0] in pkgs
        assert specs[1] in pkgs


def test_encoding_file_paths(tmp_path: Path):
    tmp_channel = tmp_path / "channel+some+encodable+bits"
    repo = Path(__file__).parent / "data/mamba_repo"
    shutil.copytree(repo, tmp_channel)
    process = conda_subprocess(
        "create",
        "-p",
        tmp_path / "env",
        "-c",
        tmp_channel,
        "test-package",
        "--solver=libmamba",
    )
    print(process.stdout)
    print(process.stderr, file=sys.stderr)
    assert process.returncode == 0
    assert list((tmp_path / "env" / "conda-meta").glob("test-package-*.json"))
