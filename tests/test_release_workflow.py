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
