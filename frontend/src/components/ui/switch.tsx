"use client";

import * as React from "react";
import * as SwitchPrimitives from "@radix-ui/react-switch";
import { cn } from "@/lib/utils";

const Switch = React.forwardRef<
  React.ComponentRef<typeof SwitchPrimitives.Root>,
  React.ComponentPropsWithoutRef<typeof SwitchPrimitives.Root>
>(({ className, ...props }, ref) => (
  <SwitchPrimitives.Root
    className={cn(
      "peer group relative inline-flex h-11 w-11 shrink-0 cursor-pointer items-center rounded-full bg-transparent",
      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--surface-primary)]",
      "disabled:cursor-not-allowed",
      className,
    )}
    {...props}
    ref={ref}
  >
    <span aria-hidden="true" className="pointer-events-none absolute inset-x-0 top-1/2 h-6 -translate-y-1/2 rounded-full bg-[var(--control-border)] transition-colors group-data-[state=checked]:bg-[var(--switch-checked)]" />
    <SwitchPrimitives.Thumb
      className={cn(
        "pointer-events-none relative ml-0.5 block h-5 w-5 rounded-full bg-[var(--surface-primary)] shadow-sm data-[state=checked]:bg-[var(--brand-primary-text)] ring-0 transition-transform",
        "data-[state=checked]:translate-x-5",
        "data-[state=unchecked]:translate-x-0",
      )}
    />
  </SwitchPrimitives.Root>
));
Switch.displayName = SwitchPrimitives.Root.displayName;

export { Switch };
