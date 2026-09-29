import assert from "node:assert/strict";
import { test } from "node:test";
import { discoveryDraftReady, discoveryRequestKey } from "../src/providerDiscovery";

test("automatic discovery waits until URL and temporary key are both complete", () => {
  assert.equal(discoveryDraftReady("", "temporary-test-key"), false);
  assert.equal(discoveryDraftReady("https://api.example.test/v1", ""), false);
  assert.equal(discoveryDraftReady(" https://api.example.test/v1 ", " temporary-test-key "), true);
});

test("discovery deduplication key normalizes URL without retaining the key", () => {
  const first = discoveryRequestKey("openai-responses", " https://api.example.test/v1/ ", "temporary-test-key");
  const same = discoveryRequestKey("openai-responses", "https://api.example.test/v1/", "temporary-test-key");
  const changed = discoveryRequestKey("openai-responses", "https://api.example.test/v1/", "another-test-key");
  assert.equal(first, same);
  assert.notEqual(first, changed);
  assert.equal(first.includes("temporary-test-key"), false);
});
