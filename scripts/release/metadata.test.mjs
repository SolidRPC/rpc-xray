import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { shouldDryRunPublish, validateRelease } from "./metadata.mjs";

const pkg = JSON.parse(
  await readFile(new URL("../../js/package.json", import.meta.url), "utf8"),
);
const manual = {
  event: "workflow_dispatch",
  repository: "SolidRPC/rpc-xray",
  ref: "refs/heads/main",
  version: pkg.version,
};

test("manual releases identify the exact package version and tarball", () => {
  assert.deepEqual(validateRelease(pkg, manual), {
    version: pkg.version,
    filename: `solidrpc-rpc-xray-${pkg.version}.tgz`,
  });
});

test("manual runs reject another branch, repository or version", () => {
  for (const patch of [
    { ref: "refs/heads/develop" },
    { repository: "other/rpc-xray" },
    { version: "99.0.0" },
    { version: `${pkg.version}\nfilename=other.tgz` },
    { version: "" },
  ]) {
    assert.throws(() => validateRelease(pkg, { ...manual, ...patch }));
  }
});

test("PR validation does not require a release input and other events are rejected", () => {
  assert.equal(
    validateRelease(pkg, { event: "pull_request" }).version,
    pkg.version,
  );
  for (const event of ["push", "release", "pull_request_target"]) {
    assert.throws(() => validateRelease(pkg, { ...manual, event }));
  }
});

test("rejects prereleases, private packages and mismatched release metadata", () => {
  for (const patch of [
    { version: "0.1.0-beta.1" },
    { private: true },
    { name: "other" },
    { publishConfig: { access: "restricted", provenance: true } },
    { publishConfig: { access: "public", provenance: false } },
    { repository: { url: "git+https://github.com/other/repo.git" } },
  ]) {
    assert.throws(() =>
      validateRelease({ ...pkg, ...patch }, { event: "pull_request" }),
    );
  }
});

test("PRs can validate existing versions while manual releases require a new version", () => {
  assert.equal(shouldDryRunPublish(200, "pull_request"), false);
  assert.equal(shouldDryRunPublish(404, "pull_request"), true);
  assert.equal(shouldDryRunPublish(404, "workflow_dispatch"), true);
  assert.throws(
    () => shouldDryRunPublish(200, "workflow_dispatch"),
    /already published/,
  );
  for (const status of [401, 403, 429, 500, 503]) {
    assert.throws(
      () => shouldDryRunPublish(status, "pull_request"),
      /availability check failed/,
    );
    assert.throws(
      () => shouldDryRunPublish(status, "workflow_dispatch"),
      /availability check failed/,
    );
  }
  assert.throws(() => shouldDryRunPublish(404, "push"), /Unsupported/);
});
