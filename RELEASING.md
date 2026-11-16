# Publishing the npm package

The [npm release workflow](.github/workflows/release.yml) publishes `@solidrpc/rpc-xray` from `js/`. Publication requires a manual run on `main`, the exact version from `js/package.json`, and an explicitly selected `publish` checkbox. The checkbox defaults to false. Pushes, tags and GitHub releases do not trigger publication.

Pull requests that change the package or release workflow run its packaging checks automatically. The publish job is skipped for every pull request, including forks. Packaging runs without npm credentials or OIDC write permission.

## Authentication

A local `npm login` authenticates this computer. GitHub-hosted runners need their own publishing identity.

For the first release, create a short-lived npm granular access token. Under **Packages and scopes**, select **Read and write (publish and stage)** for the `@solidrpc` scope. Organization-management access alone does not grant package publishing rights. Enable bypass 2FA for this CI token. Store it as the repository Actions secret `NPM_TOKEN`, for example using `gh secret set NPM_TOKEN --repo SolidRPC/rpc-xray` and its interactive secret prompt. The workflow exposes that secret only to the publishing step.

Once the package exists, configure its npm trusted publisher with:

| Setting           | Value                                 |
| ----------------- | ------------------------------------- |
| Provider          | GitHub Actions                        |
| Organization      | `SolidRPC`                            |
| Repository        | `rpc-xray`                            |
| Workflow filename | `release.yml`                         |
| Environment       | Leave empty                           |
| Allowed action    | Direct publication with `npm publish` |

The publishing job runs on a GitHub-hosted runner with Node 24, npm 11 and `id-token: write`. npm uses the configured OIDC identity before falling back to `NPM_TOKEN`. Remove the bootstrap token after a successful trusted publication. The repository must remain public for provenance.

See npm's [trusted publishing](https://docs.npmjs.com/trusted-publishers/), [granular token setup](https://docs.npmjs.com/creating-and-viewing-access-tokens/) and [provenance](https://docs.npmjs.com/generating-provenance-statements/) documentation. Trusted publisher configurations currently need a successful first publication within two days of creation, so configure one when its release is ready.

## Preview a release

Merge the release workflow into `main`, then open **Actions → npm release → Run workflow**. Select `main`, enter the package version, and leave `publish` unchecked.

The equivalent CLI command for version `0.1.0` is:

```bash
gh workflow run release.yml --repo SolidRPC/rpc-xray --ref main -f version=0.1.0 -F publish=false
```

The workflow checks metadata, formatting, types, unit tests and generated monitoring files. It validates the Prometheus rules, tests the packed tarball with the plain Node example and both ESM and CommonJS imports, and runs `npm publish --dry-run`. The `npm-package` artifact contains the tested tarball and its integrity manifest. A preview needs no npm publishing credential and does not generate blockchain RPC traffic.

## Publish a release

1. Commit the intended stable version in `js/package.json` and `js/package-lock.json`. Keep `js/src/xray.ts`'s exported `VERSION` and the example dependency aligned. For the initial release, these are already `0.1.0`.
2. Wait for CI and the release preview to pass on the intended source. The workflow only publishes stable `x.y.z` versions to the `latest` npm tag.
3. Run **npm release** on `main` with that exact version and check `publish`.
4. Confirm both **Validate and package** and **Publish to npm** succeed.

Each manual run builds and tests its own artifact. The publish job downloads that exact tarball, refuses an already published version, and publishes with public access and provenance. It then compares npm's tarball integrity with the tested artifact and verifies imports after a fresh registry installation. Concurrent manual release runs are serialized.

If publication succeeds but registry verification fails, inspect npm before retrying. An npm version cannot be overwritten. The workflow does not create a Git tag or GitHub release.
