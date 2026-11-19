from handoff.config import RepoConfig
from handoff.deploy import CommitAncestry
from helpers import git, make_bare_remote


def test_commit_ancestry_uses_git_graph(tmp_path):
    remote = make_bare_remote(tmp_path, "api", {"api.txt": "v1\n"})
    seed = tmp_path / "api-seed"
    target = git(seed, "rev-parse", "HEAD")
    (seed / "api.txt").write_text("v2\n", encoding="utf-8")
    git(seed, "add", "api.txt")
    git(seed, "commit", "-m", "newer dev")
    observed = git(seed, "rev-parse", "HEAD")
    git(seed, "push", "origin", "dev:dev")

    root = tmp_path / "handoff"
    root.mkdir()
    repo = RepoConfig(
        id="org/api",
        role="backend",
        host="local",
        default_branch="main",
        allowed_base_refs=("dev",),
        pr_base_ref="dev",
        cache_dir=".handoff-run/repo-cache/org__api",
        remote=str(remote),
    )
    ancestry = CommitAncestry(root, repo)

    assert ancestry.contains(target, observed) == (True, None)
    assert ancestry.contains(observed, target) == (False, None)
