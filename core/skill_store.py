"""
SkillStore — Progressive skill index, retrieval, usage tracking, and auto-correction.
Replaces the old "load all skills at init" approach with on-demand retrieval.
"""
import os
import re
import json
import hashlib
from datetime import datetime
from typing import List, Dict, Optional


# Common stopwords for keyword extraction (Chinese + English)
_STOPWORDS = {
    '的', '了', '在', '是', '我', '有', '和', '就', '不', '人', '都', '一', '一个',
    '上', '也', '很', '到', '说', '要', '去', '你', '会', '着', '没有', '看', '好',
    '自己', '这', '那', '它', '他', '她', '们', '什么', '怎么', '如何', '为什么',
    '因为', '所以', '但是', '可以', '这个', '那个', '已经', '之后', '然后', '如果',
    '虽然', '而且', '或者', '还是', '只是', '但是', '不是', '就是', '以及', '并且',
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'shall', 'can', 'need', 'dare', 'ought', 'used',
    'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by', 'from', 'as', 'into',
    'through', 'during', 'before', 'after', 'above', 'below', 'between', 'out',
    'off', 'over', 'under', 'again', 'further', 'then', 'once', 'here', 'there',
    'when', 'where', 'why', 'how', 'all', 'each', 'every', 'both', 'few', 'more',
    'most', 'other', 'some', 'such', 'no', 'nor', 'not', 'only', 'own', 'same',
    'so', 'than', 'too', 'very', 'and', 'but', 'or', 'if', 'while', 'that', 'this',
    'it', 'its', 'about', 'just', 'also', 'any', 'into', 'over', 'then',
}


def _extract_keywords(text: str) -> List[str]:
    """Extract meaningful keywords from text.

    For English: split on whitespace.
    For Chinese: character bigrams (2-grams) since Chinese has no spaces.
    """
    if not text:
        return []
    text = text.lower()

    # Split into Chinese and non-Chinese segments
    keywords = set()

    # English words (split on whitespace)
    for w in re.findall(r'[a-z][a-z0-9_]*', text):
        if w not in _STOPWORDS and len(w) > 1:
            keywords.add(w)

    # Chinese character bigrams
    chinese_chars = re.findall(r'[一-鿿]', text)
    for i in range(len(chinese_chars) - 1):
        bigram = chinese_chars[i] + chinese_chars[i + 1]
        if bigram not in _STOPWORDS:
            keywords.add(bigram)

    # Also include single Chinese chars that are meaningful (non-stopword)
    # to catch short queries where the user types e.g. "部署" as a 2-char word
    # (部署 already produces 部署 via bigram, so this is covered)

    # Also include any number tokens
    for n in re.findall(r'\d+', text):
        if len(n) == 1 or n not in _STOPWORDS:
            keywords.add(n)

    return list(keywords)


_ENTRY_NAME_RE = re.compile(r"^[\w\-\.]+$")


def _is_valid_entry_name(filename) -> bool:
    """Whitelist check for skill entry names: ^[\w\-\.]+$ and no path
    separators / parent traversal — blocks reads outside the skills dir."""
    return isinstance(filename, str) and bool(filename) \
        and bool(_ENTRY_NAME_RE.match(filename)) \
        and ".." not in filename and "/" not in filename and "\\" not in filename


def _parse_frontmatter(content: str) -> tuple:
    """Parse YAML-style frontmatter (--- ... ---) for name/description.

    Returns (name, description); both "" when no frontmatter is present.
    Minimal line-based parser — no yaml dependency.
    """
    if not content.startswith("---"):
        return "", ""
    end = content.find("\n---", 3)
    if end == -1:
        return "", ""
    name = ""
    desc = ""
    for line in content[3:end].splitlines():
        m = re.match(r'\s*(name|description)\s*:\s*(.+?)\s*$', line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip().strip('\'"')
        if key == "name" and not name:
            name = value
        elif key == "description" and not desc:
            desc = value
    return name, desc


def _extract_title_and_desc(content: str) -> tuple:
    """Extract title and description from skill markdown.

    Directory-style skills (SKILL.md) may carry YAML frontmatter with
    name/description — frontmatter wins; otherwise fall back to the first
    # heading and first paragraph.
    """
    fm_name, fm_desc = _parse_frontmatter(content)

    # Strip the frontmatter block before heading/paragraph fallback
    body_text = content
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end != -1:
            body_text = content[end + 4:]

    # Title: first # heading
    title = ""
    title_match = re.search(r'^#\s+(.+)', body_text, re.MULTILINE)
    if title_match:
        title = title_match.group(1).strip()

    # Description: first paragraph (between title and next heading or blank line)
    # Remove the first heading line to get content after title
    desc = ""
    body = re.sub(r'^#\s+.+', '', body_text, count=1).strip()
    para_match = re.search(r'^([^#\n][^\n]*(?:\n[^#\n][^\n]*)*)', body, re.MULTILINE)
    if para_match:
        desc = para_match.group(1).strip()[:200]

    title = fm_name or title
    desc = fm_desc or desc
    if not desc:
        desc = title

    return title, desc


class SkillStore:
    """Manages skill indexing, retrieval, and usage analytics."""

    def __init__(self, skills_dir: str = None):
        if skills_dir is None:
            from core.paths import get_skills_dir
            skills_dir = get_skills_dir()
        self.skills_dir = skills_dir
        self.index_path = os.path.join(skills_dir, "index.json")
        self.index: Dict = {"version": 1, "skills": []}
        self._loaded_content: Dict[str, str] = {}  # filename → full text cache
        self._last_build_mtime: float = 0.0  # mtime of index.json at last load
        self._load_or_build_index()

    def refresh(self):
        """Reload the index if the skills directory or index file has changed."""
        current = 0.0
        if os.path.exists(self.index_path):
            current = os.path.getmtime(self.index_path)
        if current > self._last_build_mtime:
            self._loaded_content.clear()
            self._load_or_build_index()

    # ── Index Management ──

    def _load_or_build_index(self):
        if os.path.exists(self.index_path):
            try:
                with open(self.index_path, "r", encoding="utf-8") as f:
                    self.index = json.load(f)
                self._last_build_mtime = os.path.getmtime(self.index_path)
                # Prune stale entries (deleted files)
                valid = set(self._list_skill_files())
                self.index["skills"] = [
                    s for s in self.index.get("skills", [])
                    if s["filename"] in valid
                ]
                return
            except Exception:
                pass
        self.build_index()

    def _list_skill_files(self) -> List[str]:
        """List skill entries: flat .md/.py files plus directories containing
        SKILL.md (Anthropic-style directory skills; entry filename = dir name)."""
        if not os.path.isdir(self.skills_dir):
            return []
        entries = []
        for f in os.listdir(self.skills_dir):
            path = os.path.join(self.skills_dir, f)
            if os.path.isfile(path) and (f.endswith(".md") or f.endswith(".py")):
                entries.append(f)
            elif os.path.isdir(path) and os.path.isfile(os.path.join(path, "SKILL.md")):
                entries.append(f)
        return sorted(entries)

    def _content_path(self, filename: str) -> str:
        """Resolve an index entry to the file holding its prompt content."""
        path = os.path.join(self.skills_dir, filename)
        if os.path.isdir(path):
            return os.path.join(path, "SKILL.md")
        return path

    def build_index(self):
        """Scan skills directory and build/rebuild the full index."""
        index_skills = []
        for filename in self._list_skill_files():
            filepath = self._content_path(filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception as e:
                print(f"[SkillStore] Cannot read {filename}: {e}")
                continue

            title, desc = _extract_title_and_desc(content)
            keywords = _extract_keywords(title + " " + desc)

            # Preserve existing stats if re-indexing
            existing = self._find_entry(filename)
            index_skills.append({
                "filename": filename,
                "title": title,
                "description": desc,
                "keywords": keywords,
                "usage_count": existing.get("usage_count", 0) if existing else 0,
                "success_rate": existing.get("success_rate", 1.0) if existing else 1.0,
                "last_used": existing.get("last_used") if existing else None,
            })

        self.index = {"version": 1, "skills": index_skills}
        self._last_build_mtime = 0.0  # reset; _save_index will set it
        self._save_index()

    def _save_index(self):
        try:
            with open(self.index_path, "w", encoding="utf-8") as f:
                json.dump(self.index, f, ensure_ascii=False, indent=2)
            self._last_build_mtime = os.path.getmtime(self.index_path)
        except Exception as e:
            print(f"[SkillStore] Failed to save index: {e}")

    def _find_entry(self, filename: str) -> Optional[Dict]:
        for s in self.index.get("skills", []):
            if s["filename"] == filename:
                return s
        return None

    # ── Retrieval ──

    def retrieve(self, query: str, top_k: int = 3) -> List[Dict]:
        """
        Retrieve relevant skill metadata by keyword matching against
        the user's query and recent conversation context.
        """
        if not query or not self.index.get("skills"):
            return []
        query_keywords = set(_extract_keywords(query))
        if not query_keywords:
            return []

        scored = []
        for skill in self.index["skills"]:
            skill_keywords = set(skill.get("keywords", []))
            matches = query_keywords & skill_keywords
            if matches:
                # Score: proportion of query keywords matched
                score = len(matches) / len(query_keywords)
                # Bonus for high-success-rate or frequently used skills
                score += skill.get("success_rate", 1.0) * 0.1
                score += min(skill.get("usage_count", 0) * 0.01, 0.1)
                scored.append((score, skill))

        scored.sort(key=lambda x: -x[0])
        return [dict(s[1]) for s in scored[:top_k]]

    # ── 语义检索（精度提升）：bigram 零命中时用 LLM 做相关性兜底 ──
    # 生产实证：任务文案「续写5章」与技能关键词（写作/小说/改稿）字面零交集，
    # 字面检索零命中导致已安装技能不可见。向量方案依赖 sentence-transformers
    #（本环境未装），改用现成的 LLM 一次性判定——只在字面零命中时花一次小调用。
    def _llm_skill_judge(self, query: str) -> list:
        """让 LLM 从技能清单中挑出与 query 相关的 filename（JSON 数组）。
        失败/异常一律返回 []（退化为纯字面语义）。"""
        try:
            from core.llm_client import LLMClient
            lines = []
            for s in self.index.get("skills", []):
                desc = (s.get("description") or "")[:100]
                lines.append(f"- {s['filename']}: {s.get('title','')} — {desc}")
            prompt = (
                "判断以下哪些技能与用户需求【相关】（可用于完成该需求的方法/风格/流程）。"
                "只返回 JSON 数组（元素为 filename 字符串），都不相关返回 []。不要解释。\n\n"
                f"用户需求：{query[:500]}\n\n技能列表：\n" + "\n".join(lines)
            )
            resp, _ = LLMClient().chat(messages=[{"role": "user", "content": prompt}])
            text = (resp.choices[0].message.content or "").strip()
            import re as _re
            m = _re.search(r"\[[^\]]*\]", text, _re.S)
            names = json.loads(m.group(0)) if m else []
            if not isinstance(names, list):
                return []
            known = {s["filename"] for s in self.index.get("skills", [])}
            return [n for n in names if isinstance(n, str) and n in known]
        except Exception as e:
            print(f"[SkillStore] LLM skill judge error: {e}")
            return []

    def retrieve_semantic(self, query: str, top_k: int = 3) -> List[Dict]:
        """混合检索：字面 bigram 优先；字面零命中时用一次 LLM 小调用做
        相关性兜底（宁缺毋滥——不命中不注入，不增加无关预载）。

        LLM 判定失败时静默退化为纯字面检索（返回可能为空）。"""
        literal = self.retrieve(query, top_k=top_k)
        if literal:
            return literal
        names = self._llm_skill_judge(query)
        if not names:
            return []
        by_name = {s["filename"]: s for s in self.index.get("skills", [])}
        return [by_name[n] for n in names[:top_k] if n in by_name]

    def get_skill_content(self, filename: str) -> Optional[str]:
        """Read full skill content (with cache).

        Directory skills return SKILL.md plus an appendix listing bundled
        resources (references/, scripts/) with the absolute directory path,
        so the agent can use them via read_file / execute.
        filename 做白名单校验（^[\w\-\.]+$ 且不含路径分隔符/..），
        防止 "../secret.md" 之类读到技能目录外文件。"""
        if not _is_valid_entry_name(filename):
            return None
        if filename in self._loaded_content:
            return self._loaded_content[filename]
        filepath = self._content_path(filename)
        if os.path.isfile(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                dirpath = os.path.join(self.skills_dir, filename)
                if os.path.isdir(dirpath):
                    content += self._resource_appendix(dirpath)
                self._loaded_content[filename] = content
                return content
            except Exception:
                pass
        return None

    @staticmethod
    def _resource_appendix(dirpath: str) -> str:
        """Resource listing appended to directory-skill content."""
        lines = ["", "", "---", f"## 技能资源（目录: {os.path.abspath(dirpath)}）"]
        listed = False
        for sub in ("references", "scripts"):
            subdir = os.path.join(dirpath, sub)
            if os.path.isdir(subdir):
                files = sorted(f for f in os.listdir(subdir)
                               if os.path.isfile(os.path.join(subdir, f)))
                if files:
                    listed = True
                    lines.append(f"- {sub}/: " + ", ".join(files))
        if not listed:
            return ""
        lines.append("可用 read_file 读取上述文件；scripts/ 下的脚本可用 execute_shell / execute_python 运行。")
        return "\n".join(lines)

    def format_skills_for_prompt(self, skills: List[Dict]) -> str:
        """Format retrieved skills into injectable prompt text."""
        if not skills:
            return ""
        parts = []
        for s in skills:
            content = self.get_skill_content(s["filename"])
            if content:
                # Only inject first 100 lines to avoid context waste
                lines = content.split("\n")
                if len(lines) > 100:
                    content = "\n".join(lines[:100]) + \
                        f"\n\n*[该技能较长，已截断前100行，完整共{len(lines)}行]*"
                parts.append(f"--- {s.get('title', s['filename'])} ---\n{content}")
        if not parts:
            return ""
        return "\n\n以下是你已学会的技能中与当前任务相关的，请优先参考执行：\n\n" + "\n\n".join(parts)

    # ── Usage Tracking ──

    def record_usage(self, filename: str, success: bool = True):
        """Record skill usage for quality tracking and auto-correction."""
        entry = self._find_entry(filename)
        if not entry:
            return
        entry["usage_count"] = entry.get("usage_count", 0) + 1
        old_rate = entry.get("success_rate", 1.0)
        entry["success_rate"] = (old_rate * 0.9) + (0.1 if success else 0)
        entry["last_used"] = datetime.now().isoformat()
        self._save_index()

        # Flag skills that are consistently failing
        if not success and entry["success_rate"] < 0.3 and entry["usage_count"] >= 3:
            self._flag_for_review(filename)

    def _flag_for_review(self, filename: str):
        """Append a review note to a failing skill file."""
        from pathlib import Path
        filepath = Path(self.skills_dir) / filename
        if not filepath.is_file():  # directory skills are not annotated in place
            return
        entry = self._find_entry(filename)
        rate = entry["success_rate"] if entry else 0.0
        try:
            content = filepath.read_text(encoding="utf-8")
            review_note = (
                "\n\n---\n"
                f"*⚠️ 该技能最近使用效果不佳，成功率低于30%，需要检查修正。"
                f"Review date: {datetime.now().strftime('%Y-%m-%d %H:%M')}*"
            )
            if "需要检查修正" not in content:
                filepath.write_text(content + review_note, encoding="utf-8")
                print(f"[SkillStore] Flagged {filename} for review (success_rate={rate:.1f})")
        except Exception as e:
            print(f"[SkillStore] Failed to flag {filename}: {e}")

    # ── Convenience ──

    def list_skills(self) -> List[Dict]:
        """Return all skill metadata (no content)."""
        return list(self.index.get("skills", []))

    def has_skills(self) -> bool:
        return len(self.index.get("skills", [])) > 0
