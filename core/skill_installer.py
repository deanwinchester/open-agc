"""
Skill Installer — install Anthropic-style directory skills from GitHub.

Supported sources (remote fetches are SSRF-guarded to GitHub hosts, https only):
    https://github.com/<owner>/<repo>   — codeload zip (tries main, then master)
    https://github.com|*.githubusercontent.com/...zip — GitHub zip link
    /local/path/to/pack.zip or file:/// — local zip, only with allow_local=True (tests)

A skill package is a directory containing SKILL.md (optionally with
references/ and scripts/ subdirectories). The package may sit at the zip
root or inside a single level of subdirectory. SKILL.md and scripts/ text
files are screened with the same danger patterns as the import path.
"""
import io
import os
import re
import shutil
import tempfile
import zipfile
from typing import Dict, List, Optional

from core.paths import get_skills_dir
from core.security import is_safe_name

MAX_BYTES = 50 * 1024 * 1024  # 50MB download cap
MAX_EXTRACT_BYTES = 200 * 1024 * 1024  # 200MB 解压总量上限（防 zip bomb）
MAX_ZIP_ENTRIES = 2000  # 压缩包条目数上限

_GITHUB_REPO_RE = re.compile(
    r"^https?://github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?/?$"
)

# SSRF 防护：远程安装仅允许 GitHub 官方主机（https）
ALLOWED_HOSTS = {"github.com", "codeload.github.com", "raw.githubusercontent.com"}


class SkillInstallError(Exception):
    """Raised for any install failure (bad URL, download, layout, conflicts)."""


def _validate_remote_url(url: str) -> None:
    """SSRF guard: only https URLs on GitHub official hosts are fetchable."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except Exception:
        raise SkillInstallError(f"URL 无法解析: {url}")
    if parsed.scheme != "https":
        raise SkillInstallError(f"仅允许 https 链接: {url}")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise SkillInstallError(
            f"仅允许 GitHub 主机（{' / '.join(sorted(ALLOWED_HOSTS))}），已拒绝: {url}")


def _local_zip_path(url: str, allow_local: bool) -> str:
    """Validate and normalize a local zip path (only with explicit allow_local=True)."""
    if not allow_local:
        raise SkillInstallError(
            "仅支持 GitHub 仓库链接（https://github.com/<owner>/<repo>）或 GitHub zip 直链")
    local = url[7:] if url.startswith("file://") else url
    if os.name == "nt" and local.startswith("/") and len(local) > 2 and local[2] == ":":
        local = local[1:]
    if not os.path.isabs(local):
        raise SkillInstallError(f"本地安装仅接受绝对路径: {url}")
    if not local.lower().endswith(".zip"):
        raise SkillInstallError(f"本地安装仅接受 .zip 文件: {url}")
    return local


def _fetch_local(path: str, max_bytes: int = MAX_BYTES) -> bytes:
    """Read a local zip file with a size cap."""
    if not os.path.isfile(path):
        raise SkillInstallError(f"本地文件不存在: {path}")
    if os.path.getsize(path) > max_bytes:
        raise SkillInstallError(f"文件超过大小上限 {max_bytes // (1024 * 1024)}MB")
    with open(path, "rb") as f:
        return f.read()


def _fetch_bytes(url: str, max_bytes: int = MAX_BYTES) -> bytes:
    """Download a remote zip URL with a hard size cap (SSRF-guarded)."""
    _validate_remote_url(url)
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "open-agc-skill-installer"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            length = resp.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise SkillInstallError(f"下载超过大小上限 {max_bytes // (1024 * 1024)}MB")
            chunks: List[bytes] = []
            total = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise SkillInstallError(f"下载超过大小上限 {max_bytes // (1024 * 1024)}MB")
                chunks.append(chunk)
            return b"".join(chunks)
    except SkillInstallError:
        raise
    except Exception as e:
        raise SkillInstallError(f"下载失败: {url} — {e}")


def _safe_extract(data: bytes, dest: str) -> None:
    """Extract a zip into *dest*: zip-bomb caps first, then per-member path checks."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillInstallError("下载内容不是有效的 zip 压缩包")
    dest_real = os.path.realpath(dest)
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise SkillInstallError(f"压缩包条目数超过上限 {MAX_ZIP_ENTRIES}，已拒绝")
        total_size = sum(i.file_size for i in infos)
        if total_size > MAX_EXTRACT_BYTES:
            raise SkillInstallError(
                f"解压体积超过上限 {MAX_EXTRACT_BYTES // (1024 * 1024)}MB，已拒绝")
        for member in zf.namelist():
            target = os.path.realpath(os.path.join(dest_real, member))
            try:
                if os.path.commonpath([dest_real, target]) != dest_real:
                    raise SkillInstallError(f"压缩包含路径穿越条目，已拒绝: {member}")
            except ValueError:
                raise SkillInstallError(f"压缩包含非法条目，已拒绝: {member}")
        zf.extractall(dest_real)


def _locate_skill_dir(root: str) -> str:
    """Find the directory containing SKILL.md inside an extracted archive.

    Looks at the (unwrapped) root first, then one level of subdirectories.
    """
    # Unwrap a single wrapper directory (codeload zips: repo-main/...)
    entries = os.listdir(root)
    if len(entries) == 1 and os.path.isdir(os.path.join(root, entries[0])):
        root = os.path.join(root, entries[0])
    if os.path.isfile(os.path.join(root, "SKILL.md")):
        return root
    candidates = sorted(
        d for d in os.listdir(root)
        if os.path.isfile(os.path.join(root, d, "SKILL.md"))
    )
    if len(candidates) == 1:
        return os.path.join(root, candidates[0])
    if not candidates:
        raise SkillInstallError("压缩包中未找到 SKILL.md —— 不是标准技能包")
    raise SkillInstallError("压缩包中包含多个技能目录，无法确定安装目标: " + ", ".join(candidates))


def _read_frontmatter_name(skill_dir: str) -> str:
    """Read the frontmatter `name:` of SKILL.md, if any."""
    from core.skill_store import _parse_frontmatter
    try:
        with open(os.path.join(skill_dir, "SKILL.md"), "r", encoding="utf-8") as f:
            return _parse_frontmatter(f.read())[0]
    except Exception:
        return ""


def _candidate_urls(url: str) -> List[str]:
    """Expand a source into downloadable zip URL candidates."""
    m = _GITHUB_REPO_RE.match(url.strip())
    if m:
        owner, repo = m.group(1), m.group(2)
        return [f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}"
                for branch in ("main", "master")]
    return [url]


def _validate_skill_content(skill_dir: str) -> None:
    """内容安全校验：复用 SkillManager.validate_skill 的危险模式扫描。

    对 SKILL.md 与 scripts/ 下的文本文件逐一检查；命中 danger 级即拒绝安装
    并说明命中项（warning 级与 import 路径同级，不阻断）。"""
    from core.skill_manager import SkillManager
    targets = [os.path.join(skill_dir, "SKILL.md")]
    scripts = os.path.join(skill_dir, "scripts")
    if os.path.isdir(scripts):
        targets.extend(
            os.path.join(scripts, f) for f in sorted(os.listdir(scripts))
            if os.path.isfile(os.path.join(scripts, f))
        )
    validator = SkillManager(str(skill_dir))
    for path in targets:
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except (UnicodeDecodeError, OSError):
            continue  # 二进制/不可读文件不做文本模式扫描
        validation = validator.validate_skill(content)
        if validation["level"] == "danger":
            issues = "; ".join(i["description"] for i in validation["issues"]
                               if i["severity"] == "danger")
            raise SkillInstallError(
                f"技能包含危险内容，已拒绝安装（{os.path.basename(path)}）: {issues}")


def install_skill_from_url(url: str, skills_dir: str = None,
                           allow_local: bool = False) -> Dict:
    """Install a directory skill from a GitHub repo URL or GitHub zip link.

    Remote fetches are SSRF-guarded (https + GitHub hosts only). Local zip
    paths are accepted only with explicit allow_local=True (tests).
    Returns {"success", "name", "title", "files_count", "message"}.
    Raises SkillInstallError on any failure.
    """
    url = (url or "").strip()
    if not url:
        raise SkillInstallError("url 不能为空")
    skills_dir = skills_dir or get_skills_dir()

    m = _GITHUB_REPO_RE.match(url)
    repo_name = m.group(2) if m else ""

    if url.startswith("file://") or not url.startswith(("http://", "https://")):
        data = _fetch_local(_local_zip_path(url, allow_local))
    else:
        data = None
        last_error: Optional[Exception] = None
        for cand in _candidate_urls(url):
            _validate_remote_url(cand)  # 入口侧策略校验（_fetch_bytes 内还有一道）
            try:
                data = _fetch_bytes(cand)
                break
            except SkillInstallError as e:
                last_error = e
        if data is None:
            raise SkillInstallError(str(last_error) if last_error else "下载失败")

    tmp = tempfile.mkdtemp(prefix="skill_install_")
    try:
        _safe_extract(data, tmp)
        skill_dir = _locate_skill_dir(tmp)
        _validate_skill_content(skill_dir)

        # Skill name: frontmatter name > repo name (GitHub root skills) >
        # located dir name > zip stem.
        name = _read_frontmatter_name(skill_dir)
        if not (name and is_safe_name(name)):
            name = repo_name
        if not name:
            base = os.path.basename(skill_dir)
            name = "" if os.path.realpath(skill_dir) == os.path.realpath(tmp) else base
        if not name:
            name = os.path.splitext(os.path.basename(url.rstrip("/")))[0]
        if not is_safe_name(name):
            raise SkillInstallError(f"技能名不合法: {name!r}")

        dest = os.path.join(skills_dir, name)
        if os.path.exists(dest):
            raise SkillInstallError(f"技能 '{name}' 已存在，请先在技能管理中删除后再安装")

        shutil.copytree(skill_dir, dest)
        files_count = sum(len(files) for _r, _d, files in os.walk(dest))

        # Rebuild the retrieval index so the skill is usable immediately
        from core.skill_store import SkillStore
        store = SkillStore(skills_dir=skills_dir)
        store.build_index()
        entry = store._find_entry(name) or {}

        return {
            "success": True,
            "name": name,
            "title": entry.get("title", name),
            "files_count": files_count,
            "message": f"技能 '{name}' 安装成功（{files_count} 个文件）",
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
