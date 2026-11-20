import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import * as esm from "@solidrpc/rpc-xray";

const commonjs = createRequire(import.meta.url)("@solidrpc/rpc-xray");

test("ES modules and CommonJS expose the same SDK and default registry", () => {
  assert.deepEqual(Object.keys(esm).sort(), Object.keys(commonjs).sort());
  for (const name of Object.keys(esm)) assert.equal(esm[name], commonjs[name]);

  commonjs.configure({ offers: false });
  assert.match(esm.metrics(), /^rpc_xray_offers_enabled 0$/m);
  esm.configure({ offers: true });
  assert.match(commonjs.metrics(), /^rpc_xray_offers_enabled 1$/m);
});
