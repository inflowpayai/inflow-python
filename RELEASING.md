# Releasing inflowpay

The distribution and import name are `inflowpay`. Its version is defined in
`pyproject.toml`; release tags use `v` followed by that version, such as `v0.1.0`.
Update the version and refresh `uv.lock` in a reviewed pull request before each
release. Merging code does not publish a package.

## One-time PyPI setup

Sign in to [PyPI Publishing](https://pypi.org/manage/account/publishing/) and add
a pending GitHub publisher with these exact values:

| Field             | Value           |
| ----------------- | --------------- |
| PyPI project      | `inflowpay`     |
| Owner             | `inflowpayai`   |
| Repository        | `inflow-python` |
| Workflow filename | `release.yml`   |
| Environment       | `release`       |

Use the workflow filename alone, not `.github/workflows/release.yml`. No PyPI API
token or GitHub publishing secret is needed. The pending publisher creates the
project on its first successful upload; it does not reserve the name beforehand.
For an existing project, configure the same publisher under its Publishing page.
See [PyPI's setup instructions](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).

The GitHub `release` environment allows deployment only from `main`. Publishing
also requires an explicit manual workflow run with `dry_run` disabled.

## Verify and publish

1. Merge the version changes and ensure the checks are green.
2. Open [the Release workflow](https://github.com/inflowpayai/inflow-python/actions/workflows/release.yml).
3. Select **Run workflow**, choose **main**, and leave **dry_run** checked.
4. Review the successful run and its distribution and release-evidence artifacts.
   The workflow runs repository checks, pinned shared conformance and Node
   interoperability, builds a wheel from the source distribution, and installs
   the exact candidate wheel into isolated consumers. A dry-run creates no tag,
   GitHub release, or PyPI upload.
5. With release approval, run the workflow on **main** with **dry_run** unchecked.
6. Confirm the `publish` and `finalize` jobs succeed. Finalization compares PyPI
   artifact hashes, installs the registry wheel outside the checkout, then creates
   the immutable tag and GitHub release with the reports and distributions.

The workflow passes verified distributions between jobs; the publishing job does
not rebuild them. GitHub build provenance and PyPI publishing attestations accompany
the upload. Pull requests affecting release configuration run verification only.

## Recover a failed run

Use **Re-run failed jobs** on the same workflow run to retain its verified
artifacts. Existing PyPI files are accepted only when their hashes match those
artifacts. A different file under the same version fails rather than replacing it.
If PyPI publication succeeded but finalization failed, rerun finalization instead
of uploading again. A registry propagation delay can require retrying that job.

Never move a released tag or delete and republish a package version. If the source
or artifacts need changing, prepare a new version in a pull request. A successful
release is not an invitation to rerun it: the GitHub release already exists.
