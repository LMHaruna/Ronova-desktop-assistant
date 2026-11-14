"""List MCP packages and Skills installed in Ronova's Extensions folder."""
from dataclasses import dataclass
from itertools import islice
from pathlib import Path


@dataclass(frozen=True)
class LibraryEntry:
    name: str
    source: Path


def skill_entry(path):
    try:
        with path.open(encoding='utf-8-sig') as stream:
            for line in islice(stream, 30):
                if line.startswith('name:'):
                    name = line.partition(':')[2].strip().strip('"\'')
                    return LibraryEntry(name or path.parent.name, path)
    except (OSError, ValueError):
        return None
    return LibraryEntry(path.parent.name, path)


def discover_library(project):
    extensions = project/'Extensions'
    mcp = [
        LibraryEntry(folder.name, folder)
        for folder in (extensions/'MCP').glob('*')
        if folder.is_dir() and not folder.name.startswith('.')
    ]
    skills = {}
    for path in (extensions/'Skills').glob('*/SKILL.md'):
        if not path.parent.name.startswith('.') and (entry := skill_entry(path)):
            skills[entry.name.casefold()] = entry

    def by_name(entry):
        return entry.name.casefold()

    return sorted(mcp, key=by_name), sorted(skills.values(), key=by_name)
