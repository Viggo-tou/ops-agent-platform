import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { detectLanguage, DiffViewer } from "./DiffViewer";

describe("detectLanguage", () => {
  it("maps supported file extensions to highlight.js language ids", () => {
    expect(detectLanguage("src/foo.ts")).toBe("typescript");
    expect(detectLanguage("foo.py")).toBe("python");
    expect(detectLanguage("bar.unknown")).toBeNull();
  });
});

describe("DiffViewer syntax highlighting", () => {
  it("renders highlight.js token spans for TypeScript diffs", () => {
    const diff = [
      "diff --git a/src/foo.ts b/src/foo.ts",
      "--- a/src/foo.ts",
      "+++ b/src/foo.ts",
      "@@ -1,2 +1,2 @@",
      "-const value: string = \"old\";",
      "+const value: string = \"new\";",
    ].join("\n");

    const { container } = render(<DiffViewer diff={diff} />);

    expect(container.querySelector(".diff-line-content .hljs-keyword")).not.toBeNull();
  });

  it("renders unknown extensions as plain text", () => {
    const diff = [
      "diff --git a/blob.bin b/blob.bin",
      "--- a/blob.bin",
      "+++ b/blob.bin",
      "@@ -1 +1 @@",
      "-const value = old",
      "+const value = new",
    ].join("\n");

    const { container } = render(<DiffViewer diff={diff} />);

    expect(container.querySelector('[class^="hljs-"], [class*=" hljs-"]')).toBeNull();
  });

  it("keeps add and remove gutter prefixes outside highlighted code", () => {
    const diff = [
      "diff --git a/src/foo.ts b/src/foo.ts",
      "--- a/src/foo.ts",
      "+++ b/src/foo.ts",
      "@@ -1,2 +1,2 @@",
      "-const value: string = \"old\";",
      "+const value: string = \"new\";",
    ].join("\n");

    const { container } = render(<DiffViewer diff={diff} />);
    const addContent = container.querySelector(".diff-line-add .diff-line-content");
    const removeContent = container.querySelector(".diff-line-remove .diff-line-content");

    expect(addContent?.textContent).toMatch(/^\+const value: string = "new";$/);
    expect(removeContent?.textContent).toMatch(/^-const value: string = "old";$/);
  });
});
