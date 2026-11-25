import { fireEvent, render, screen } from "@testing-library/react";
import { createRef, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Composer, LIMIT } from "./composer";

function Harness({ streaming = false, onSubmit = vi.fn(), onStop = vi.fn(), initial = "", recall }: {
  streaming?: boolean;
  onSubmit?: () => void;
  onStop?: () => void;
  initial?: string;
  recall?: string;
}) {
  const [value, setValue] = useState(initial);
  const ref = createRef<HTMLTextAreaElement>();
  return (
    <TooltipProvider>
      <Composer
        value={value}
        onChange={setValue}
        onSubmit={onSubmit}
        onStop={onStop}
        onRecall={() => recall}
        streaming={streaming}
        error={null}
        inputRef={ref}
        sources={[]}
        selected={[]}
        onSelected={() => {}}
      />
    </TooltipProvider>
  );
}

const box = () => screen.getByLabelText("Ask a question");

describe("Composer", () => {
  it("Enter sends, Shift+Enter doesn't, and IME composition never sends", () => {
    const onSubmit = vi.fn();
    render(<Harness onSubmit={onSubmit} initial="How much leave?" />);
    fireEvent.keyDown(box(), { key: "Enter", shiftKey: true });
    fireEvent.keyDown(box(), { key: "Enter", isComposing: true });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.keyDown(box(), { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("won't send an empty or whitespace-only question, and says why", () => {
    const onSubmit = vi.fn();
    render(<Harness onSubmit={onSubmit} initial="   " />);
    fireEvent.keyDown(box(), { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Send" })).toHaveAttribute("aria-disabled", "true");
  });

  it("blocks sending over the limit and shows the counter in danger", () => {
    const onSubmit = vi.fn();
    render(<Harness onSubmit={onSubmit} initial={"x".repeat(LIMIT + 1)} />);
    fireEvent.keyDown(box(), { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText(`${(LIMIT + 1).toLocaleString()} / ${LIMIT.toLocaleString()}`)).toBeInTheDocument();
  });

  it("turns Send into Stop while an answer streams", () => {
    const onStop = vi.fn();
    render(<Harness streaming onStop={onStop} initial="next question" />);
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Stop generating" }));
    expect(onStop).toHaveBeenCalled();
  });

  it("↑ in an empty box recalls the last question", () => {
    render(<Harness recall="What changed in v2?" />);
    fireEvent.keyDown(box(), { key: "ArrowUp" });
    expect(box()).toHaveValue("What changed in v2?");
  });
});
