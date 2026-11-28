# Publishing the npm package

The [npm release workflow](.github/workflows/release.yml) publishes `@solidrpc/rpc-xray` from `js/`. Publication requires a manual run on `main`, the exact version from `js/package.json`, and an explicitly selected `publish` checkbox. The checkbox defaults to false. Pushes, tags and GitHub releases do not trigger publication.

Pull requests that change the package or release workflow run its packaging checks automatically. When the current version is already published, PR validation still builds and tests the tarball and skips only `npm publish --dry-run`, which npm otherwise rejects as a duplicate version. New versions receive that dry run too. Manual release runs require an unpublished version. Registry errors fail validation. The publish job is skipped for every pull request, including forks. Packaging runs without npm credentials or OIDC write permission.

## Authentication

The package uses npm trusted publishing. The workflow authenticates through GitHub OIDC and does not read an `NPM_TOKEN` secret or use a local npm login. Its npm trusted publisher must match:

| Setting           | Value                                 |
| ----------------- | ------------------------------------- |
| Provider          | GitHub Actions                        |
| Organization      | `SolidRPC`                            |
| Repository        | `rpc-xray`                            |
| Workflow filename | `release.yml`                         |
| Environment       | Leave empty                           |
| Allowed action    | Direct publication with `npm publish` |

The publishing job runs on a GitHub-hosted runner with Node 24, npm 11 and `id-token: write`. Publication fails if the configured OIDC identity cannot authenticate. The repository must remain public for provenance.

See npm's [trusted publishing](https://docs.npmjs.com/trusted-publishers/) and [provenance](https://docs.npmjs.com/generating-provenance-statements/) documentation. Trusted publisher configurations currently need a successful first publication within two days of creation, so configure one when its release is ready.

## Preview a release

Merge the release workflow into `main`, then open **Actions → npm release → Run workflow**. Select `main`, enter the package version, and leave `publish` unchecked.

The equivalent CLI command for version `0.1.3` is:

```bash
gh workflow run release.yml --repo SolidRPC/rpc-xray --ref main -f version=0.1.3 -F publish=false
```

The workflow checks metadata, formatting, types, unit tests and generated monitoring files. It validates the Prometheus rules, tests the packed tarball with the plain Node example and both ESM and CommonJS imports, and runs `npm publish --dry-run`. The `npm-package` artifact contains the tested tarball and its integrity manifest. A preview needs no npm publishing credential and does not generate blockchain RPC traffic.

## Publish a release

1. Commit the intended stable version in `js/package.json` and `js/package-lock.json`. Keep `js/src/xray.ts`'s exported `VERSION`, the example dependency and the documented tarball filenames aligned.
2. Wait for CI and the release preview to pass on the intended source. The workflow only publishes stable `x.y.z` versions to the `latest` npm tag.
3. Run **npm release** on `main` with that exact version and check `publish`.
4. Confirm both **Validate and package** and **Publish to npm** succeed.

Each manual run builds and tests its own artifact. The publish job downloads that exact tarball, refuses an already published version, and publishes with public access and provenance. It allows up to five minutes for npm processing, then compares npm's tarball integrity with the tested artifact and verifies imports after a fresh registry installation. Concurrent manual release runs are serialized.

If publication succeeds but registry verification fails, inspect npm before retrying. An npm version cannot be overwritten. The workflow does not create a Git tag or GitHub release.

## Python, Go, and Rust

These SDKs start at their own `0.1.0` and are not published by the npm workflow. The CI workflow tests their package builds and complete examples without publishing or creating tags. A merge does not release a package.

The [Published SDK examples workflow](.github/workflows/published-examples.yml) runs the six example apps against their registry dependencies every Monday at 06:17 UTC. It also supports manual runs. Pull request CI tests the SDK checkout.

| SDK | Version source | Release artifact | Intended release identity |
|---|---|---|---|
| Python | `python/pyproject.toml` and `python/rpc_xray/core.py` | Wheel and source distribution | `solidrpc-rpc-xray` on PyPI |
| Go | `go/xray.go` | Go module from a source tag | `go/v0.1.1` for the `go/` subdirectory |
| Rust | `rust/Cargo.toml` | `.crate` archive | `solidrpc-rpc-xray` on crates.io |

After CI passes on the merged commit, run **Native SDK release** on `main`. Choose one SDK, enter its exact version, and leave `publish` unchecked to prepare a candidate. The workflow uses the Python and Rust archives from the successful push CI run on that SHA. For Go it prepares a source archive for inspection. It reports SHA-256 checksums and uploads the candidate. A Go module release uses the Git tag.

```sh
gh workflow run native-release.yml --ref main -f sdk=python -f version=0.1.1 -F publish=false
gh workflow run native-release.yml --ref main -f sdk=go -f version=0.1.1 -F publish=false
gh workflow run native-release.yml --ref main -f sdk=rust -f version=0.1.1 -F publish=false
```

For the first Python publication, configure a [PyPI pending trusted publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/) for project `solidrpc-rpc-xray`, repository `SolidRPC/rpc-xray`, workflow `native-release.yml`, and environment `pypi`. For Rust, configure a crates.io token authorized to publish the new crate as the `CARGO_REGISTRY_TOKEN` secret in the `crates-io` GitHub environment. For Go, the `go-release` job needs permission to create `go/v0.1.0` in the repository. Choose the publish checkbox only when releasing the selected SDK. Recheck that the SHA shown in the release run is the intended revision.

Update the relevant package version, lockfile, SDK docs, and examples together. Keep the published package version independent from the shared contract version. Any change to metric meaning, labels, or error classification requires a coordinated contract review and tests across every SDK before release.

### Update examples after publication

Once the registries serve the release, update the public pins and lockfiles for the example apps. For Go, run `GOWORK=off go get github.com/SolidRPC/rpc-xray/go@vX.Y.Z` in both `examples/plain-go` and `examples/go-ethereum`. Verify the examples against the published packages with the **Published SDK examples** workflow.
