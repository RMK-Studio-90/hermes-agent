// @vitest-environment jsdom
//
// Phase 2 — ProfileBuilderPage picks the new profile's model with the SAME
// shared ModelPickerDialog (no page-level provider enumeration of its own).
// The profile does not exist yet, so the structured selection is captured in
// builder state and rides along in the single POST /api/profiles.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  createProfile: vi.fn(),
  getSkills: vi.fn(),
  getModelOptions: vi.fn(),
}));

const pickerProps = vi.hoisted(() => ({
  current: null as Record<string, unknown> | null,
}));
vi.mock("@/components/ModelPickerDialog", () => ({
  ModelPickerDialog: (props: Record<string, unknown>) => {
    pickerProps.current = props;
    return null;
  },
}));

vi.mock("@/lib/api", () => ({
  api: apiMocks,
  setManagementProfile: vi.fn(),
  getManagementProfile: vi.fn(() => ""),
}));

let container: HTMLDivElement;
let root: Root;
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

function click(el: Element | null) {
  if (!el) throw new Error("element not rendered");
  el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
}

function stepButton(label: string): HTMLButtonElement | null {
  // Stepper labels render as "2. Model", "5. Review", …
  return (
    Array.from(document.querySelectorAll("button")).find((b) =>
      /^\d+\.\s/.test(b.textContent?.trim() ?? "") &&
      b.textContent?.trim().endsWith(label),
    ) ?? null
  );
}

function btnIncludes(text: string): HTMLButtonElement | null {
  return (
    Array.from(document.querySelectorAll("button")).find((b) =>
      b.textContent?.toLowerCase().includes(text.toLowerCase()),
    ) ?? null
  );
}

beforeEach(() => {
  for (const fn of Object.values(apiMocks)) fn.mockReset();
  pickerProps.current = null;
  apiMocks.getSkills.mockResolvedValue([]);
  apiMocks.getModelOptions.mockResolvedValue({ providers: [] });
  apiMocks.createProfile.mockResolvedValue({
    ok: true,
    name: "rmk-new",
    hub_installs: [],
  });
});

afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
  document.body.innerHTML = "";
});

async function renderBuilder() {
  const { default: ProfileBuilderPage } = await import("./ProfileBuilderPage");
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <MemoryRouter>
        <ProfileBuilderPage />
      </MemoryRouter>,
    ),
  );
}

function typeName(value: string) {
  const input = document.querySelector<HTMLInputElement>("#pb-name");
  if (!input) throw new Error("name input missing");
  const setter = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype,
    "value",
  )!.set!;
  setter.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("ProfileBuilderPage — shared model picker + captured selection", () => {
  it("uses the shared inventory and carries the selection into createProfile", async () => {
    await renderBuilder();

    await act(async () => typeName("rmk-new"));
    await act(async () => click(stepButton("Model")));

    await act(async () => click(btnIncludes("Choose model")));
    expect(pickerProps.current).toBeTruthy();

    await act(async () =>
      (pickerProps.current!.onApply as (a: unknown) => void)({
        provider: "openrouter",
        model: "anthropic/claude-opus-4.8",
      }),
    );
    // No live profile-scoped call was faked before creation.
    expect(apiMocks.getModelOptions).not.toHaveBeenCalled();

    await act(async () => click(stepButton("Review")));
    expect(document.body.textContent).toContain(
      "openrouter · anthropic/claude-opus-4.8",
    );

    await act(async () => click(btnIncludes("Create profile")));
    expect(apiMocks.createProfile).toHaveBeenCalledTimes(1);
    expect(apiMocks.createProfile.mock.calls[0][0]).toMatchObject({
      name: "rmk-new",
      provider: "openrouter",
      model: "anthropic/claude-opus-4.8",
    });
  });

  it("omits provider/model when the user keeps the default", async () => {
    await renderBuilder();
    await act(async () => typeName("rmk-plain"));
    await act(async () => click(stepButton("Review")));
    await act(async () => click(btnIncludes("Create profile")));

    const body = apiMocks.createProfile.mock.calls[0][0];
    expect(body.provider).toBeUndefined();
    expect(body.model).toBeUndefined();
  });
});
