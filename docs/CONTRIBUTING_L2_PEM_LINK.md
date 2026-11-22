# Contributing to the PEM Link Module (`L2/pem-link`)

All work for the PEM Link module lives on the `L2/pem-link` branch. This branch is protected:

- Direct pushes are blocked. Every change must go through a pull request (PR).
- Each PR needs an approval from the module admin, and only the module admin can merge it.
- If you push new commits after a PR is approved, the approval is dismissed and the PR must be reviewed again.
- Force pushes and branch deletion are disabled.

If you run `git push origin L2/pem-link` directly, GitHub will reject it.

## How to Submit a Change

### 1. Create your own branch from `L2/pem-link`

```bash
git fetch origin
git checkout -b L2/pem-link-<short-description> origin/L2/pem-link
# e.g. L2/pem-link-fix-sclk
```

### 2. Make your changes, commit, and push your branch

```bash
git add <files>
git commit -m "Short summary of what changed"
git push -u origin L2/pem-link-<short-description>
```

> You need write access to the repository to push a branch. If you don't have it, fork the repository and push to your fork instead.

### 3. Open a pull request targeting `L2/pem-link`

Using the GitHub CLI:

```bash
gh pr create --base L2/pem-link --title "<title>" --body "<description>"
```

Or on the GitHub website, click **Compare & pull request**.

> ⚠️ **Important:** GitHub sets the base branch to `main` by default. Change the **base** to `L2/pem-link` before you create the PR.

### 4. Write a clear description

Every PR must explain the change. Please include:

- **What** changed: modules, files, and signals affected
- **Why** the change is needed
- **How** it was tested: testbench, simulation results, or waveforms
- **Impact** on other parts of the design, if any

PRs without a meaningful description will be closed.

### 5. Review

- The admin will review your PR and may leave comments or request changes.
- To address feedback, push new commits to your branch. The PR updates automatically.
- Once the PR is approved, the module admin will merge it. If the change isn't accepted, the PR will be closed with an explanation.

## Questions

Contact Yiou before starting large changes, so the design direction is agreed on first.
