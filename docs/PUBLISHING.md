# Publish the prepared repository

The repository bundle is ready for `FortranFour/power-fingerprint`. Run the
publisher in **Git Bash on your computer**, where `git` and GitHub CLI `gh` are
installed and authenticated as FortranFour. This is separate from the Home
Assistant terminal used for installation.

From the extracted source directory:

```bash
bash publish.sh
```

The script creates a public repository, commits and pushes the reviewed source,
adds HACS-related topics, waits for the validation workflow, and publishes
`v0.7.4` only if validation succeeds. It uses a file allowlist and does not
include household diagnostics, dashboard configuration, model storage, or
private Powercalc profiles.

If validation fails after creating the repository, the script leaves the source
and repository available for correction. Rerun it from the same directory after
fixing the failed check. It verifies the origin and does not force-push or
replace an unrelated existing repository. GitHub CLI must have permission to
push the repository's workflow files and publish releases.

HACS repository validation skips only the Home Assistant Brands catalog check,
because this repository is initially used as a custom repository. It does not
claim inclusion in the default HACS catalog.

## Publish future versions

1. Update `VERSION` in `const.py` and `version` in `manifest.json` together.
2. Add matching release notes under `docs/RELEASE_<version>.md` and update the changelog.
3. Run the local checks, commit and push the changes, and let GitHub Actions pass.
4. For automated publication, use the exact commit subject
   `Release Power Fingerprint <version>` on `main`. The Validate workflow's
   release job runs only for these explicit release commits, after both Python
   versions and HACS validation pass. It verifies that the subject matches the
   manifest version, then publishes the new tag and release at that tested SHA.
   Ordinary pushes and pull requests do not publish releases. Existing releases
   are left unchanged, and existing tags without a release are not reused.

The release job alone receives `contents: write`; validation jobs retain
read-only repository access. It uses GitHub's job token and requires no stored
personal access token. If publication fails, correct the problem and rerun the
failed release job for the same validated commit.

Alternatively, create a GitHub release manually with a new version tag pointing
to the tested commit.

For example, once a later version has been implemented and tested:

```bash
gh release create v0.7.5 --repo FortranFour/power-fingerprint \
  --target main --title "Power Fingerprint 0.7.5" \
  --notes-file docs/RELEASE_0.7.5.md
```

The one-time `publish.sh` is specific to the initial 0.7.4 release. A version
tag alone is not the same as a published release for the release-based HACS
update path.

## Upstream references

- [HACS integration layout and manifest](https://www.hacs.dev/docs/publish/integration/)
- [HACS custom repositories](https://www.hacs.dev/docs/faq/custom_repositories/)
- [HACS adoption of existing manual files](https://www.hacs.dev/docs/faq/existing_elements/)
- [GitHub CLI repository creation](https://cli.github.com/manual/gh_repo_create)
- [GitHub CLI release creation](https://cli.github.com/manual/gh_release_create)
