import assert from "node:assert/strict";
import { test } from "node:test";
import { sceneAssetsIncomplete } from "../src/world/assets";

test("asset warning follows the selected preset even when the other preset is fully loaded", () => {
  assert.equal(sceneAssetsIncomplete("office", 8, 8, 5, 6), true);
  assert.equal(sceneAssetsIncomplete("office", 0, 8, 6, 6), false);
  assert.equal(sceneAssetsIncomplete("meadow", 7, 8, 6, 6), true);
  assert.equal(sceneAssetsIncomplete("meadow", 8, 8, 0, 6), false);
});
