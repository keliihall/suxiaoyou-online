import * as React from "react";
import { cn } from "@/lib/utils";

const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, type, ...props }, ref) => {
    return (
      <input
        data-slot="input"
        type={type}
        className={cn(
          "ui-control flex h-[var(--control-height)] w-full rounded-[var(--radius-control)] border border-[var(--control-border)] bg-[var(--surface-primary)] px-3 py-2 text-ui-body text-[var(--text-primary)] transition-colors file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-[var(--text-tertiary)] hover:border-[var(--text-secondary)] focus-visible:outline-none focus-visible:border-[var(--brand-primary)] focus-visible:ring-2 focus-visible:ring-[var(--ring)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--surface-primary)] disabled:cursor-not-allowed disabled:bg-[var(--surface-secondary)] disabled:text-[var(--text-secondary)]",
          className,
        )}
        ref={ref}
        {...props}
      />
    );
  },
);
Input.displayName = "Input";

export { Input };
