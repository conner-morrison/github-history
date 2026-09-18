#!/usr/bin/env python3
"""Invent a repository name from what the repo actually contains.

Reads the README, any package manifest, the directory layout and the language
mix, then builds candidate names in three lengths. Entirely offline: no API,
no network. The same repo always yields the same names, different repos yield
different ones, because the random choices are seeded from the keywords.
"""

import argparse
import hashlib
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#]*")

STOPWORDS = {
    "a", "an", "and", "the", "this", "that", "these", "those", "is", "are", "was", "be", "been",
    "it", "its", "for", "from", "with", "without", "into", "onto", "to", "of", "in", "on", "at",
    "by", "as", "or", "but", "not", "no", "you", "your", "we", "our", "us", "i", "my", "me",
    "can", "will", "would", "should", "may", "must", "have", "has", "had", "do", "does", "did",
    "if", "then", "else", "when", "while", "how", "what", "which", "who", "why", "all", "any",
    "some", "more", "most", "other", "such", "only", "own", "same", "so", "than", "too", "very",
    "just", "also", "use", "used", "using", "uses", "get", "gets", "make", "makes", "made",
    "new", "free", "open", "source", "project", "repo", "repository", "code", "codebase",
    "library", "package", "module", "version", "release", "install", "installation", "usage",
    "example", "examples", "documentation", "docs", "license", "licensed", "contributing",
    "readme", "build", "builds", "run", "running", "see", "please", "note", "here", "there",
    "via", "per", "about", "over", "under", "between", "each", "one", "two", "three",
    "yet", "done", "right", "many", "info", "site", "ext", "etc", "way", "ways", "thing",
    "things", "lot", "lots", "much", "many", "want", "need", "like", "know", "help",
}

# Real words, but too generic to carry a name on their own. They are demoted out
# of the head position and reused as modifiers instead: "powerful-parser-kit".
WEAK = {
    "simple", "easy", "fast", "quick", "powerful", "modern", "awesome", "better", "best",
    "great", "small", "large", "full", "high", "low", "real", "main", "basic", "advanced",
    "popular", "official", "minimal", "lightweight", "tiny", "elegant", "beautiful", "clean",
    "creating", "making", "building", "written", "based", "designed", "friendly", "humans",
}

MODIFIERS = ["swift", "tiny", "quiet", "nimble", "plain", "solid", "clear", "sharp", "lean",
             "brisk", "steady", "bright", "humble", "candid", "sturdy"]
NOUNS = ["kit", "forge", "lab", "works", "core", "hub", "box", "yard", "stack", "nest",
         "bench", "loom", "anvil", "atlas", "harbor"]
LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".rb": "ruby", ".java": "java", ".kt": "kotlin",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cs": "csharp", ".php": "php",
    ".swift": "swift", ".scala": "scala", ".sh": "shell", ".lua": "lua", ".ex": "elixir",
    ".hs": "haskell", ".r": "r", ".sql": "sql", ".html": "web", ".css": "web", ".vue": "web",
}
SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "target", "__pycache__",
             ".venv", "venv", ".idea", ".vscode", "test", "tests", "spec", "docs", "doc",
             "examples", "example", "assets", "static", "public", "src", "lib", "bin"}


def _words(text, limit=None):
    found = [w.lower() for w in WORD.findall(text or "")]
    keep = [w for w in found if len(w) > 2 and w not in STOPWORDS]
    return keep[:limit] if limit else keep


def read_readme(repo):
    """Return (title, tagline) from the README, skipping badges and HTML."""
    for path in sorted(repo.glob("*")):
        if path.is_file() and path.stem.lower() == "readme":
            break
    else:
        return None, None

    title, tagline = None, None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines()[:60]:
        line = raw.strip()
        if not line or line.startswith(("<", "---", "===", "|", "```")):
            continue
        # badge-only lines carry no meaning
        if line.startswith(("[![", "![")) or "shields.io" in line or "badge" in line.lower():
            continue
        clean = re.sub(r"[*_`>#\[\]]|\(http[^)]*\)", " ", line).strip()
        if not clean:
            continue
        if title is None:
            title = clean
        elif tagline is None:
            tagline = clean
            break
    return title, tagline


def read_manifest(repo):
    """Pull name/description/keywords out of whichever manifest exists."""
    out = {"name": None, "description": None, "keywords": []}

    pkg = repo / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            out["name"] = data.get("name")
            out["description"] = data.get("description")
            out["keywords"] = [str(k) for k in data.get("keywords") or []]
            return out
        except (json.JSONDecodeError, AttributeError):
            pass

    for filename, name_key, desc_key in (("pyproject.toml", "name", "description"),
                                         ("Cargo.toml", "name", "description"),
                                         ("composer.json", "name", "description")):
        path = repo / filename
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for key, field in ((name_key, "name"), (desc_key, "description")):
            match = re.search(rf'^\s*{key}\s*[=:]\s*["\']([^"\']+)["\']', text, re.M)
            if match:
                out[field] = match.group(1)
        break

    return out


def languages(repo):
    counts = Counter()
    for path in repo.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        lang = LANG_BY_EXT.get(path.suffix.lower())
        if lang:
            counts[lang] += 1
    return [lang for lang, _ in counts.most_common(2)]


def analyze(repo):
    """Collect naming signals, weighting the ones that describe purpose most."""
    repo = Path(repo).resolve()
    title, tagline = read_readme(repo)
    manifest = read_manifest(repo)

    # the original names are what we are moving away from, so they are noise
    excluded = set(_words(repo.name)) | set(_words(manifest["name"] or ""))

    weighted = Counter()
    for text, weight in ((title, 5), (tagline, 4), (manifest["description"], 5),
                         (" ".join(manifest["keywords"]), 3)):
        for word in _words(text, limit=25):
            weighted[word] += weight

    for path in repo.iterdir():
        if path.is_dir() and path.name not in SKIP_DIRS and not path.name.startswith("."):
            for word in _words(path.name):
                weighted[word] += 1

    langs = languages(repo)
    for index, lang in enumerate(langs):
        weighted[lang] += 2 - index

    for word in excluded:
        weighted.pop(word, None)

    return {
        "title": title,
        "tagline": tagline,
        "manifest": manifest,
        "languages": langs,
        "keywords": [word for word, _ in weighted.most_common(12)],
    }


def candidates(analysis, seed=None, count=9):
    """Build names in three lengths: one word, two words, three or four."""
    keywords = analysis["keywords"] or analysis["languages"] or ["quiet", "signal"]
    if seed is None:
        seed = hashlib.sha256(" ".join(keywords).encode("utf-8")).hexdigest()
    rng = random.Random(seed)

    strong = [word for word in keywords if word not in WEAK]
    head = (strong or keywords)[:4]
    tail = (strong[4:8] if len(strong) > 4 else strong[1:5]) or head
    modifiers = MODIFIERS + [word for word in keywords if word in WEAK]
    names, seen = [], set()

    def add(name):
        name = re.sub(r"-+", "-", name.strip("-").lower())
        # "safeforge" and "safe-forge" are the same name, keep only one
        key = name.replace("-", "")
        if name and key not in seen and VALID_NAME.match(name):
            seen.add(key)
            names.append(name)

    for _ in range(count * 4):
        if len(names) >= count:
            break
        style = len(names) % 3  # rotate short / medium / long so all three appear
        word = rng.choice(head)
        other = rng.choice(tail)
        modifier = rng.choice(modifiers)

        if style == 0:                                    # short: one word
            add(word if len(word) >= 5 else word + rng.choice(NOUNS))
        elif style == 1:                                  # medium: two words
            add(rng.choice([f"{modifier}-{word}",
                            f"{word}-{rng.choice(NOUNS)}",
                            f"{word}-{other}"]))
        else:                                             # long: three or four
            add(rng.choice([f"{modifier}-{word}-{rng.choice(NOUNS)}",
                            f"{word}-{other}-{rng.choice(NOUNS)}",
                            f"{modifier}-{word}-{other}-{rng.choice(NOUNS)}"]))

    return names


def suggest(repo, seed=None, count=9):
    return candidates(analyze(repo), seed=seed, count=count)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="repository to read")
    parser.add_argument("-n", "--count", type=int, default=9, help="how many names to generate")
    parser.add_argument("--seed", help="override the content-derived seed")
    parser.add_argument("--explain", action="store_true", help="show the signals behind the names")
    args = parser.parse_args()

    repo = Path(args.repo)
    if not repo.is_dir():
        print(f"error: not a directory: {repo}", file=sys.stderr)
        return 1

    analysis = analyze(repo)
    if args.explain:
        print(f"title:     {analysis['title']}")
        print(f"tagline:   {analysis['tagline']}")
        print(f"manifest:  {analysis['manifest']['name']} - {analysis['manifest']['description']}")
        print(f"languages: {', '.join(analysis['languages']) or '(none detected)'}")
        print(f"keywords:  {', '.join(analysis['keywords'])}\n")

    for name in candidates(analysis, seed=args.seed, count=args.count):
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
