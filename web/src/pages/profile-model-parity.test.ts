// Phase 2 — provider-source parity.
//
// Static guard: the default Models page and both profile editors must go
// through the ONE shared ModelPickerDialog / api.getModelOptions inventory.
// No page may re-grow its own provider/model enumeration or a simplified
// per-profile assignment path.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const dir = join(__dirname);
const read = (f: string) => readFileSync(join(dir, f), "utf8");

const MODELS_PAGE = read("ModelsPage.tsx");
const PROFILES_PAGE = read("ProfilesPage.tsx");
const BUILDER_PAGE = read("ProfileBuilderPage.tsx");

describe("provider/model source parity", () => {
  it("all three surfaces mount the shared ModelPickerDialog", () => {
    for (const src of [MODELS_PAGE, PROFILES_PAGE, BUILDER_PAGE]) {
      expect(src).toContain('from "@/components/ModelPickerDialog"');
      expect(src).toMatch(/<ModelPickerDialog\b/);
    }
  });

  it("all three surfaces feed the picker from api.getModelOptions", () => {
    for (const src of [MODELS_PAGE, PROFILES_PAGE, BUILDER_PAGE]) {
      expect(src).toMatch(/api\.getModelOptions/);
    }
  });

  it("the profile editors no longer flatten providers into a local list", () => {
    for (const src of [PROFILES_PAGE, BUILDER_PAGE]) {
      // The old hand-rolled enumeration looked like:
      //   for (const prov of res.providers ?? []) { for (const m of prov.models ...
      expect(src).not.toMatch(/for \(const prov of res\.providers/);
      expect(src).not.toContain("\\u0000${c.model}");
    }
  });

  it("ProfilesPage saves through the rich profile-scoped assignment path", () => {
    // POST /api/model/set?profile=<name>, not the simplified PUT wrapper.
    expect(PROFILES_PAGE).toMatch(/api\.setModelAssignment\(/);
    expect(PROFILES_PAGE).not.toMatch(/api\.setProfileModel\(/);
  });

  it("no hard-coded per-profile provider catalog literal exists in the editors", () => {
    for (const src of [PROFILES_PAGE, BUILDER_PAGE]) {
      expect(src).not.toMatch(/const\s+PROVIDERS\s*[:=]/);
      expect(src).not.toMatch(/\[\s*["']openrouter["']\s*,\s*["']moa["']/);
    }
  });
});
