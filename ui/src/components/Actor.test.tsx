import { render, screen } from "@testing-library/react";

import { Actor } from "./Actor";
import { PRINCIPAL } from "../test/utils";

describe("requester attribution", () => {
  it("identifies the known demo MCP agent explicitly", () => {
    render(<Actor id="5afe0000-0000-4000-8000-000000000105" />);
    expect(screen.getByText("Support assistant")).toBeInTheDocument();
    expect(screen.getByText("AI agent · MCP")).toBeInTheDocument();
  });

  it("uses the authenticated principal's kind over demo display metadata", () => {
    render(<Actor id={PRINCIPAL.principal_id} principal={PRINCIPAL} />);
    expect(screen.getByText("North Requester (you)")).toBeInTheDocument();
    expect(screen.getByText("Person")).toBeInTheDocument();
  });

  it("does not invent a person or agent classification for an unknown identity", () => {
    render(<Actor id="unknown-principal" />);
    expect(screen.getByText("Identity type not available")).toBeInTheDocument();
    expect(screen.queryByText("AI agent · MCP")).toBeNull();
    expect(screen.queryByText("Person")).toBeNull();
  });
});
