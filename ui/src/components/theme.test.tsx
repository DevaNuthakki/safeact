import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { storedTheme, useTheme } from "./theme";

function Switch() {
  const [theme, choose] = useTheme();
  return <button type="button" onClick={() => choose(theme === "light" ? "dark" : "light")}>{theme}</button>;
}

describe("theme", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    delete document.documentElement.dataset.theme;
  });

  it("is light by default and remembers a chosen dark theme", async () => {
    expect(storedTheme()).toBe("light");
    render(<Switch />);
    await userEvent.click(screen.getByRole("button", { name: "light" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("safeact-theme")).toBe("dark");
    expect(storedTheme()).toBe("dark");
  });

  it("stays usable when storage refuses", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(storedTheme()).toBe("light");
    render(<Switch />);
    await userEvent.click(screen.getByRole("button", { name: "light" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});
