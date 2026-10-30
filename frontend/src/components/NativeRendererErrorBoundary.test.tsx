import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { NativeRendererErrorBoundary } from "./NativeRendererErrorBoundary";

const Throw = ({ message }: { message: string }) => {
  throw new Error(message);
};

describe("NativeRendererErrorBoundary", () => {
  // React logs a loud red error when a boundary catches; silence it to
  // keep the test output focused on the assertion.
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders children when nothing throws", () => {
    render(
      <NativeRendererErrorBoundary fallback={<div>fallback</div>}>
        <div data-testid="ok">all good</div>
      </NativeRendererErrorBoundary>,
    );
    expect(screen.getByTestId("ok")).toBeInTheDocument();
    expect(screen.queryByText("fallback")).not.toBeInTheDocument();
  });

  it("swaps to the fallback when a child throws", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <NativeRendererErrorBoundary
        fallback={<div data-testid="fallback">sandbox instead</div>}
      >
        <Throw message="boom" />
      </NativeRendererErrorBoundary>,
    );
    expect(screen.getByTestId("fallback")).toBeInTheDocument();
    // componentDidCatch logs the error + componentStack to console.error.
    expect(console.error).toHaveBeenCalled();
  });

  it("clears the fallback when resetKey changes, keeps it when it does not", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const fallback = <div data-testid="fallback">sandbox instead</div>;
    const { rerender } = render(
      <NativeRendererErrorBoundary fallback={fallback} resetKey="a">
        <Throw message="boom" />
      </NativeRendererErrorBoundary>,
    );
    expect(screen.getByTestId("fallback")).toBeInTheDocument();

    rerender(
      <NativeRendererErrorBoundary fallback={fallback} resetKey="a">
        <div data-testid="ok">all good</div>
      </NativeRendererErrorBoundary>,
    );
    expect(screen.getByTestId("fallback")).toBeInTheDocument();

    rerender(
      <NativeRendererErrorBoundary fallback={fallback} resetKey="b">
        <div data-testid="ok">all good</div>
      </NativeRendererErrorBoundary>,
    );
    expect(screen.getByTestId("ok")).toBeInTheDocument();
    expect(screen.queryByTestId("fallback")).not.toBeInTheDocument();
  });
});
