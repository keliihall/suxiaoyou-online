import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "ui-control inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-[var(--radius-control)] text-ui-body font-medium transition-[background-color,border-color,color,box-shadow] duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--surface-primary)] disabled:pointer-events-none disabled:cursor-not-allowed disabled:shadow-none [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default:
          "bg-[var(--brand-primary)] text-[var(--brand-primary-text)] hover:bg-[var(--brand-primary-hover)] disabled:bg-[var(--surface-tertiary)] disabled:text-[var(--text-secondary)]",
        destructive:
          "bg-[var(--color-destructive)] text-[var(--danger-foreground)] hover:opacity-90 disabled:bg-[var(--surface-tertiary)] disabled:text-[var(--text-secondary)]",
        outline:
          "border border-[var(--border-default)] bg-[var(--surface-primary)] hover:bg-[var(--surface-secondary)] hover:border-[var(--border-heavy)] text-[var(--text-primary)] disabled:bg-[var(--surface-secondary)] disabled:text-[var(--text-secondary)]",
        secondary:
          "bg-[var(--surface-secondary)] text-[var(--text-primary)] hover:bg-[var(--surface-tertiary)] disabled:text-[var(--text-secondary)]",
        ghost:
          "hover:bg-[var(--surface-secondary)] text-[var(--text-primary)] disabled:text-[var(--text-secondary)]",
        link: "text-[var(--brand-primary)] underline-offset-4 hover:underline disabled:text-[var(--text-secondary)]",
      },
      size: {
        default: "h-[var(--control-height)] min-h-[var(--control-height)] px-4 py-2",
        sm: "h-[var(--control-height-sm)] min-h-[var(--control-height-sm)] px-3 text-ui-xs",
        lg: "h-[var(--control-height-lg)] min-h-[var(--control-height-lg)] px-6",
        icon: "h-[var(--control-height)] min-h-[var(--control-height)] w-[var(--control-height)]",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        data-slot="button"
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";

export { Button, buttonVariants };
