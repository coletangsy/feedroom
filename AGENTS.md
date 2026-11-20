# Feedroom instructions

## Browser and computer use

- Browser and computer-use tools are authorized by default for in-scope research in this repository. Prefer Google Chrome for browser work unless the user specifies another browser.
- Open research tabs, search, navigate, and read public sources without asking for routine confirmation. Use browser interaction directly when needed; do not require a failed web-reader attempt first.
- Follow the tools' permission requirements for consequential actions. This research authorization does not expand the task to sending messages, purchases, or account/security changes.

## Daily Threads digest

Use `feedroom collect` to inspect candidate JSON, `feedroom preview` for a source-title draft, and `feedroom preview --editorial` for the Cantonese digest.

- Ground each headline in its event evidence. Keep the Hong Kong date on the first line and place up to six headlines below it.
- The local Docker host owns the daily trigger. See the [README](README.md) for Compose, publishing, and optional Cloudflare deployment.
- Keep runtime journals and credentials out of Git. Use fixtures and a mocked Threads client for automated checks.
- Run `python3 -m unittest discover -s tests -v` for Python changes and `node --test tests/cloudflare.test.mjs` for Cloudflare scheduler changes.

## After a pull request merges into `main`

- Confirm the PR was merged into `main` on GitHub and record its head and merge SHAs. Check the [branch cleanup workflow](.github/workflows/delete-merged-branch.yml) for the remote source branch result. Run `node --test .github/tests/delete-merged-branch.node-test.mjs` when changing that workflow.
- On the Docker host, check `git status --short --branch` and `git worktree list`. Run `git fetch --prune origin`, switch the scheduled checkout to `main` with `git switch -- main`, and fast-forward it with `git merge --ff-only origin/main`. Confirm the checkout is clean, `HEAD` equals `origin/main`, and the PR merge commit is an ancestor of `HEAD` before rebuilding. Preserve any local changes or commits that prevent this sync and report the blocker.
- Remove only the merged PR's local branch with `git branch -d` after confirming its tip is in `main`. For a squash or rebase merge, use `git branch -D` only if the local tip exactly matches the verified PR head SHA and the PR merge commit is in `main`. If the branch is checked out in a separate worktree created for this PR, remove that worktree only when it is clean and the branch is verified. Keep any branch with unmerged local commits.
- Rebuild the `feedroom` image from the synced `main` using the scheduler's Docker context, Compose project, and `.env`. For the local Mac LaunchAgent, run these commands from the scheduled checkout. The `--help` check confirms the rebuilt image starts without publishing. Preserve the `threads-state` volume and report the branch cleanup, build, and checks.

  ```sh
  docker --context desktop-linux compose -p feedroom --env-file .env build feedroom
  docker --context desktop-linux compose -p feedroom --env-file .env run --rm --no-deps --pull never feedroom --help
  ```

## Documentation

- [README](README.md) covers the daily digest format, setup, previews, and scheduling.
- [ARCHITECTURE.md](ARCHITECTURE.md) describes collection, editorial validation, publication, and recovery.
- [CHANGELOG.md](CHANGELOG.md) records delivered behavior and documentation changes.
- Update `ARCHITECTURE.md` when sources, candidate fields, selection limits, scheduling, delivery permissions, or seen-state handling changes. Update `CHANGELOG.md` for user-visible or workflow-significant changes.
