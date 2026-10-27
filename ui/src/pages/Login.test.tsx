import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router";

import { Login } from "./Login";
import { PRINCIPAL, mockFetch, renderWith } from "../test/utils";

function app() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/demo" element={<p>signed in</p>} />
      <Route path="/actions" element={<p>console signed in</p>} />
      <Route path="/demo/refund" element={<p>refund chapter</p>} />
    </Routes>
  );
}

describe("sign-in", () => {
  it("has labelled fields and opens the guided demo by default", async () => {
    mockFetch((url) => url === "/auth/login"
      ? { status: 200, body: { principal: PRINCIPAL, csrf_token: "x" } }
      : { status: 401, body: { error: { code: "unauthenticated", message: "x" } } });
    renderWith(app(), { route: "/login" });
    await userEvent.type(screen.getByLabelText("Username"), "demo-north-requester");
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("signed in")).toBeInTheDocument();
  });

  it.each([
    ["/login?console=1", "console signed in"],
    [{ pathname: "/login", state: { from: "/demo/refund" } }, "refund chapter"],
  ])("preserves an explicit destination %j", async (route, destination) => {
    mockFetch((url) => url === "/auth/login"
      ? { status: 200, body: { principal: PRINCIPAL, csrf_token: "x" } }
      : { status: 401, body: { error: { code: "unauthenticated", message: "x" } } });
    renderWith(app(), { route });
    await userEvent.type(screen.getByLabelText("Username"), "demo-north-reviewer");
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText(destination)).toBeInTheDocument();
  });

  it("shows one generic message and clears the password on failure", async () => {
    mockFetch(() => ({ status: 401, body: { error: {
      code: "invalid_credentials", message: "Sign-in failed.",
    } } }));
    renderWith(app(), { route: "/login" });
    await userEvent.type(screen.getByLabelText("Username"), "someone");
    await userEvent.type(screen.getByLabelText("Password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-in failed");
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });
});
