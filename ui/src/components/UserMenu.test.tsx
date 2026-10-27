import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { UserMenu } from "./UserMenu";
import { PRINCIPAL } from "../test/utils";

const ALEX = { ...PRINCIPAL, principal_id: "5afe0000-0000-4000-8000-000000000101", display_name: "Alex Rivera" };

describe("user menu", () => {
  it("shows name, role and company, closes on Escape and returns focus", async () => {
    const signOut = vi.fn();
    render(<UserMenu principal={ALEX} onSignOut={signOut} />);
    const button = screen.getByRole("button", { name: "Account: Alex Rivera" });
    expect(button).toHaveTextContent("AR");
    expect(button).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(within(button).getByText("Support agent")).toBeVisible();
    expect(screen.getByText("Northwind (demo)")).toBeVisible();
    await userEvent.keyboard("{Escape}");
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(button).toHaveFocus();
    await userEvent.click(button);
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(signOut).toHaveBeenCalledOnce();
  });

  it("falls back to role titles outside the demo directory", async () => {
    render(<UserMenu principal={{ ...PRINCIPAL, display_name: "Pat Kim", roles: ["investigator", "reviewer"] }}
                     onSignOut={() => undefined} />);
    await userEvent.click(screen.getByRole("button", { name: "Account: Pat Kim" }));
    expect(within(screen.getByRole("button", { name: "Account: Pat Kim" })).getByText("Investigator, Reviewer")).toBeVisible();
  });
});
