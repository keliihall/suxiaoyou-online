import assert from "node:assert/strict";
import test from "node:test";
import { cn } from "../../src/lib/utils.ts";

test("custom typography preserves semantic text colors for every UI size", () => {
  for (const size of ["overline", "caption", "body", "title-sm", "title", "title-lg", "3xs", "2xs", "xs", "sm", "md", "lg", "xl", "code", "qr"]) {
    const classes = cn("bg-[var(--brand-primary)] text-[var(--brand-primary-text)]", `text-ui-${size}`);
    assert.ok(classes.includes("text-[var(--brand-primary-text)]"), `${size} must preserve the button foreground`);
    assert.ok(classes.includes(`text-ui-${size}`));
  }
});

test("font-size overrides replace only typography and keep color overrides independent", () => {
  assert.equal(cn("text-ui-body text-[var(--text-primary)]", "text-ui-caption"), "text-[var(--text-primary)] text-ui-caption");
  assert.equal(cn("text-ui-body text-[var(--text-primary)]", "text-[13px]"), "text-[var(--text-primary)] text-[13px]");
  assert.equal(cn("text-ui-body text-[var(--text-primary)]", "text-[var(--status-danger-text)]"), "text-ui-body text-[var(--status-danger-text)]");
});
