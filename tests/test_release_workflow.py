import os
import yaml

WORKFLOW = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".github", "workflows", "docker-release.yml",
)


def _linux_deb_job():
    with open(WORKFLOW, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["jobs"]["linux-deb"]


def test_linux_deb_matrix_includes_amd64_and_arm64():
    job = _linux_deb_job()
    include = job["strategy"]["matrix"]["include"]
    arches = {entry["arch"] for entry in include}
    assert arches == {"amd64", "arm64"}


def test_linux_deb_uses_matrix_runner():
    job = _linux_deb_job()
    assert job["runs-on"] == "${{ matrix.runner }}"


def test_linux_deb_matrix_defines_runner_per_arch():
    job = _linux_deb_job()
    include = job["strategy"]["matrix"]["include"]
    runners = {entry["arch"]: entry["runner"] for entry in include}
    assert runners["amd64"] == "ubuntu-22.04"
    assert runners["arm64"] == "ubuntu-22.04-arm"


def test_dpkg_deb_uses_xz_compression():
    """UOS/older dpkg rejects zstd-compressed control members; force xz."""
    job = _linux_deb_job()
    steps = job["steps"]
    build_step = next(s for s in steps if s.get("name") == "Build deb")
    assert "-Zxz" in build_step["run"]


def test_build_deb_script_uses_xz_compression():
    script = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "build_deb.sh",
    )
    with open(script, "r", encoding="utf-8") as f:
        content = f.read()
    assert "dpkg-deb --build -Zxz" in content


def test_linux_deb_matrix_defines_glibc228_image_per_arch():
    """PyInstaller 必须在 glibc 2.28 的 python:3.10-buster 容器内构建（兼容 UOS）。

    官方 python 镜像以 --enable-shared 构建 CPython，满足 PyInstaller 对
    libpython 的要求；manylinux 的 cp310 为静态库，不可用。
    """
    job = _linux_deb_job()
    include = job["strategy"]["matrix"]["include"]
    images = {entry["arch"]: entry["image"] for entry in include}
    assert images["amd64"] == "python:3.10-buster"
    assert images["arm64"] == "arm64v8/python:3.10-buster"


def test_linux_deb_builds_binaries_in_glibc228_container():
    job = _linux_deb_job()
    steps = job["steps"]
    step = next(s for s in steps if "glibc" in s.get("name", "").lower())
    run = step["run"]
    assert "docker run" in run
    assert "python -m venv" in run
    assert "pyinstaller" in run


def test_assemble_step_recovers_ownership_from_root_container():
    """容器以 root 构建产物，Assemble 步骤必须先 chown 归还 runner，
    否则非 root 写 dist/ 报 Permission denied（CI 实证）。"""
    job = _linux_deb_job()
    step = next(s for s in job["steps"] if s.get("name") == "Assemble deb directory")
    assert "chown -R" in step["run"]
    assert "dist build" in step["run"]


def test_litellm_pinned_below_198_for_python310():
    """litellm 1.98.0 起放弃 Python 3.10（typing.NotRequired 仅 3.11+）。"""
    req = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "requirements.txt",
    )
    with open(req, "r", encoding="utf-8") as f:
        content = f.read()
    line = next(l for l in content.splitlines() if l.strip().startswith("litellm"))
    assert "<1.98.0" in line
