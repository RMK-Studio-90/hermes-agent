// @vitest-environment jsdom
//
// Phase 2 — the ONE shared provider-first / model-second picker used by the
// default Models page AND the profile editors. These lock in the capabilities
// the profile editors must not lose: provider stage, model stage, refresh /
// model discovery, and a cancel that mutates nothing.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ModelPickerDialog } from "./ModelPickerDialog";

const OPTIONS = {
  model: "cx/gpt-5.6-terra-high",
  provider: "moa",
  providers: [
    {
      name: "Mixture of Agents",
      slug: "moa",
      models: ["cx/gpt-5.6-terra-high", "cc/claude-sonnet-5"],
      is_current: true,
    },
    {
      name: "OpenRouter",
      slug: "openrouter",
      models: ["anthropic/claude-opus-4.8", "deepseek/deepseek-v4-pro"],
      warning: "no key configured",
    },
  ],
};

let container: HTMLDivElement;
let root: Root;
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

async function flush() {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
}

function byText(text: string): HTMLElement | null {
  return (
    Array.from(document.body.querySelectorAll<HTMLElement>("*")).find(
      (el) => el.children.length === 0 && el.textContent?.trim() === text,
    ) ?? null
  );
}

function buttonByText(text: string): HTMLButtonElement | null {
  return (
    Array.from(document.body.querySelectorAll("button")).find((b) =>
      b.textContent?.trim().toLowerCase().includes(text.toLowerCase()),
    ) ?? null
  );
}

function click(el: Element | null) {
  if (!el) throw new Error("element not found");
  el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("ModelPickerDialog (shared)", () => {
  it("renders the provider stage, then the model stage after picking a provider", async () => {
    const loader = vi.fn().mockResolvedValue(OPTIONS);
    const onApply = vi.fn();

    await act(async () =>
      root.render(
        <ModelPickerDialog
          loader={loader}
          onApply={onApply}
          onClose={() => {}}
        />,
      ),
    );
    await flush();

    expect(byText("Mixture of Agents")).toBeTruthy();
    expect(byText("OpenRouter")).toBeTruthy();

    click(byText("OpenRouter"));
    await flush();
    expect(byText("anthropic/claude-opus-4.8")).toBeTruthy();
    expect(byText("deepseek/deepseek-v4-pro")).toBeTruthy();
    expect(byText("no key configured")).toBeTruthy();
  });

  it("refresh / model discovery re-invokes the loader with { refresh: true }", async () => {
    const loader = vi.fn().mockResolvedValue(OPTIONS);
    await act(async () =>
      root.render(
        <ModelPickerDialog
          loader={loader}
          onApply={vi.fn()}
          onClose={() => {}}
        />,
      ),
    );
    await flush();
    expect(loader).toHaveBeenCalledTimes(1);
    expect(loader).toHaveBeenLastCalledWith({ refresh: false });

    click(buttonByText("Refresh Models"));
    await flush();
    expect(loader).toHaveBeenCalledTimes(2);
    expect(loader).toHaveBeenLastCalledWith({ refresh: true });
  });

  it("Cancel closes without applying anything", async () => {
    const onApply = vi.fn();
    const onClose = vi.fn();
    await act(async () =>
      root.render(
        <ModelPickerDialog
          loader={vi.fn().mockResolvedValue(OPTIONS)}
          onApply={onApply}
          onClose={onClose}
        />,
      ),
    );
    await flush();

    click(buttonByText("Cancel"));
    await flush();

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onApply).not.toHaveBeenCalled();
  });

  it("applies the chosen provider + model", async () => {
    const onApply = vi.fn().mockResolvedValue(undefined);
    await act(async () =>
      root.render(
        <ModelPickerDialog
          loader={vi.fn().mockResolvedValue(OPTIONS)}
          onApply={onApply}
          alwaysGlobal
          onClose={() => {}}
        />,
      ),
    );
    await flush();

    click(byText("OpenRouter"));
    await flush();
    click(byText("deepseek/deepseek-v4-pro"));
    await flush();
    click(buttonByText("Switch"));
    await flush();

    expect(onApply).toHaveBeenCalledTimes(1);
    expect(onApply.mock.calls[0][0]).toMatchObject({
      provider: "openrouter",
      model: "deepseek/deepseek-v4-pro",
    });
  });
});
