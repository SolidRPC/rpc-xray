import assert from "node:assert/strict";
import { appendFile, readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

export function shouldDryRunPublish(status, event) {
  assert.ok(
    ["pull_request", "workflow_dispatch"].includes(event),
    "Unsupported release event",
  );
  if (status === 404) return true;
  if (status === 200 && event === "pull_request") return false;
  if (status === 200)
    throw new Error(
      "This version is already published. Bump the package version before releasing again.",
    );
  throw new Error(`Registry availability check failed: HTTP ${status}`);
}

export function validateRelease(pkg, context) {
  assert.equal(pkg.name, "@solidrpc/rpc-xray", "Unexpected package name");
  assert.match(
    pkg.version,
    /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/,
    "This workflow publishes stable versions to latest",
  );
  assert.notEqual(pkg.private, true, "The package must be publishable");
  assert.equal(
    pkg.publishConfig?.access,
    "public",
    "Public package access is required",
  );
  assert.equal(
    pkg.publishConfig?.provenance,
    true,
    "Provenance must remain enabled",
  );
  assert.equal(
    pkg.repository?.url,
    "git+https://github.com/SolidRPC/rpc-xray.git",
    "Repository metadata must match the provenance source",
  );
  if (context.event === "workflow_dispatch") {
    assert.equal(
      context.repository,
      "SolidRPC/rpc-xray",
      "Release from the SolidRPC repository",
    );
    assert.equal(
      context.ref,
      "refs/heads/main",
      "Manual release runs must use main",
    );
    assert.equal(
      context.version,
      pkg.version,
      "Enter the exact version in js/package.json",
    );
  } else {
    assert.equal(context.event, "pull_request", "Unsupported release event");
  }
  return {
    version: pkg.version,
    filename: `solidrpc-rpc-xray-${pkg.version}.tgz`,
  };
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  const pkg = JSON.parse(
    await readFile(new URL("../../js/package.json", import.meta.url), "utf8"),
  );
  const metadata = validateRelease(pkg, {
    event: process.env.GITHUB_EVENT_NAME,
    repository: process.env.GITHUB_REPOSITORY,
    ref: process.env.GITHUB_REF,
    version: process.env.RELEASE_VERSION,
  });
  if (process.env.GITHUB_OUTPUT) {
    await appendFile(
      process.env.GITHUB_OUTPUT,
      `version=${metadata.version}\nfilename=${metadata.filename}\n`,
    );
  }
  console.log(JSON.stringify(metadata));
}
