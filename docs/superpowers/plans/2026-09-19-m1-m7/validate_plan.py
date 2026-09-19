from pathlib import Path
import json, re, subprocess

root = Path.cwd()
base = root / "docs/superpowers/plans/2026-09-19-m1-m7"
data = json.loads((base / "coverage.json").read_text())
tasks = {t["id"]: t for t in data["tasks"]}
assert len(tasks) == len(data["tasks"]) == 77
assert data["implementation_status"] == "planned"
assert data["verification_status"] == "not_run"
visited, active = set(), set()


def visit(key):
    assert key in tasks, key
    assert key not in active, ("cycle", key)
    if key in visited:
        return
    active.add(key)
    for dep in tasks[key]["deps"]:
        visit(dep)
    active.remove(key)
    visited.add(key)


visit("M7-T12")
assert visited == set(tasks), ("unreachable", set(tasks) - visited)
counts = [0, 0, 0]
for m in data["milestones"]:
    name = m["id"]
    source = next((root / "docs/roadmap").glob(name + "-*.md"))
    original = source.read_text()
    plan = (base / (name + ".md")).read_text()
    for kind, owner_key, count_key, counter in [
        ("G", "goal_owners", "goals", 0),
        ("A", "acceptance_owners", "acceptance_scenarios", 2),
    ]:
        pattern = (
            rf"^- \[ \] ({name}-G\d+)：(.*)$"
            if kind == "G"
            else rf"^\| ({name}-A\d+) \| (.*?) \| (.*?) \|$"
        )
        rows = re.findall(pattern, original, re.M)
        ids = [r[0] for r in rows]
        assert len(ids) == len(set(ids)) == m[count_key]
        assert set(ids) == set(m[owner_key])
        for row in rows:
            matching = [line for line in plan.splitlines() if line.startswith("| " + row[0] + " |")]
            assert len(matching) == 1, row[0]
            assert all(part in matching[0] for part in row[1:]), ("changed requirement", row[0])
            owners = m[owner_key][row[0]]
            if isinstance(owners, str):
                owners = [owners]
            assert owners and all(o in tasks and o in matching[0] for o in owners)
        counts[counter] += len(ids)
    headings = re.findall(r"^### (M\d-T\d+)", plan, re.M)
    expected = [t for t in tasks if t.startswith(name + "-")]
    assert set(headings) == set(expected) and len(headings) == m["work_packages"]
    counts[1] += len(headings)
    sections = re.findall(r"^## (\d+)\. ", original, re.M)
    assert set(sections) == set(m["section_owners"])
    assert not re.search(r"^- \[[xX]\]", plan, re.M)
    for t in expected:
        assert tasks[t]["tests"], t
        for path in tasks[t]["tests"]:
            if path.startswith("apps/web/"):
                assert path.startswith("apps/web/tests/") and ".test." in path, path
        for label, path in re.findall(r"^- (修改|新增计划) `([^`]+)`。$", plan, re.M):
            assert (root / path).exists() == (label == "修改"), (label, path)
    assert "make check" in plan and "pnpm --dir apps/web test" in plan
assert counts == [137, 77, 119], counts
assert not any(
    d.startswith("M2-") for n in ["M6-T01", "M6-T02", "M6-T03"] for d in tasks[n]["deps"]
)
assert "M4-T10" not in tasks["M6-T08"]["deps"]
checked_links = 0
for path in list(base.glob("*.md")) + [root / "docs/roadmap/README.md"]:
    content = path.read_text()
    if path.parent == base:
        assert not re.search(r"\b(?:TODO|TBD|FIXME)\b|待补充|后续再补", content), path
    for url in re.findall(r"\]\(([^)]+)\)", content):
        if "://" in url:
            continue
        dest, _, anchor = url.partition("#")
        target = (path.parent / dest).resolve() if dest else path
        assert target.exists(), (path.name, url)
        if anchor:
            assert f'id="{anchor}"' in target.read_text(), (path.name, url)
        checked_links += 1
changes = subprocess.check_output(
    ["git", "status", "--porcelain", "--untracked-files=all"], text=True
)
assert all(line[3:].startswith("docs/") for line in changes.splitlines()), changes
unchanged = subprocess.check_output(
    ["git", "diff", "--name-only", "--", "docs/roadmap/M*.md", "docs/ROADMAP.md"], text=True
)
assert not unchanged, unchanged
print(
    json.dumps(
        {
            "goals": counts[0],
            "tasks": counts[1],
            "acceptance_scenarios": counts[2],
            "dependency_graph": "acyclic",
            "all_tasks_reachable_from_final_review": True,
            "local_links_checked": checked_links,
            "web_tests": "collected_paths",
            "changes": "docs_only",
            "original_specs": "unchanged",
        },
        ensure_ascii=False,
    )
)
