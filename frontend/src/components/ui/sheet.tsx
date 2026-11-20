"use client";

import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import * as VisuallyHidden from "@radix-ui/react-visually-hidden";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

const Sheet = DialogPrimitive.Root;
const SheetTrigger = DialogPrimitive.Trigger;
const SheetClose = DialogPrimitive.Close;
const SheetPortal = DialogPrimitive.Portal;
const SheetTitle = DialogPrimitive.Title;
const SheetDescription = DialogPrimitive.Description;

const SheetOverlay = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Overlay
    className={cn(
      "ui-overlay fixed inset-0 z-50 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0",
      className,
    )}
    {...props}
    ref={ref}
  />
));
SheetOverlay.displayName = DialogPrimitive.Overlay.displayName;

const SheetContent = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> & {
    side?: "top" | "right" | "bottom" | "left";
    showCloseButton?: boolean;
  }
>(({ side = "left", title = "Navigation", showCloseButton = true, className, children, ...props }, ref) => {
  const sideClasses = {
    top: "inset-x-0 top-0 border-b data-[state=closed]:slide-out-to-top data-[state=open]:slide-in-from-top",
    bottom: "inset-x-0 bottom-0 border-t data-[state=closed]:slide-out-to-bottom data-[state=open]:slide-in-from-bottom",
    left: "inset-y-0 left-0 h-full w-[260px] border-r data-[state=closed]:slide-out-to-left data-[state=open]:slide-in-from-left",
    right: "inset-y-0 right-0 h-full w-3/4 border-l data-[state=closed]:slide-out-to-right data-[state=open]:slide-in-from-right sm:max-w-sm",
  };

  return (
    <SheetPortal>
      <SheetOverlay />
      <DialogPrimitive.Content
        ref={ref}
        aria-describedby={undefined}
        className={cn(
          "fixed z-50 gap-4 bg-[var(--sidebar-bg)] p-0 text-[var(--text-primary)] shadow-[var(--shadow-lg)] transition ease-in-out data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:duration-200 data-[state=open]:duration-300",
          sideClasses[side],
          className,
        )}
        {...props}
      >
        <VisuallyHidden.Root asChild>
          <DialogPrimitive.Title>{title}</DialogPrimitive.Title>
        </VisuallyHidden.Root>
        {children}
        {showCloseButton && <DialogPrimitive.Close className="absolute right-3 top-3 flex size-11 items-center justify-center rounded-[var(--radius-control)] text-[var(--text-secondary)] transition-colors hover:bg-[var(--surface-tertiary)] hover:text-[var(--text-primary)] focus:outline-none focus:ring-2 focus:ring-[var(--ring)]">
          <X className="h-4 w-4" />
          <span className="sr-only">Close</span>
        </DialogPrimitive.Close>}
      </DialogPrimitive.Content>
    </SheetPortal>
  );
});
SheetContent.displayName = "SheetContent";

export { Sheet, SheetPortal, SheetOverlay, SheetTrigger, SheetClose, SheetContent, SheetTitle, SheetDescription };
