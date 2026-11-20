import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex min-h-6 items-center gap-1.5 rounded-md border px-2 py-0.5 text-ui-caption font-medium leading-snug transition-colors focus:outline-none focus:ring-2 focus:ring-[var(--ring)] focus:ring-offset-2",
  {
    variants: {
      variant: {
        default:
          "border-[var(--brand-border)] bg-[var(--brand-soft)] text-[var(--brand-primary)]",
        secondary:
          "border-transparent bg-[var(--surface-tertiary)] text-[var(--text-secondary)]",
        outline:
          "border-[var(--border-default)] text-[var(--text-secondary)]",
        success:
          "border-transparent bg-[var(--status-success-bg)] text-[var(--status-success-text)]",
        warning:
          "border-transparent bg-[var(--status-warning-bg)] text-[var(--status-warning-text)]",
        destructive:
          "border-transparent bg-[var(--status-danger-bg)] text-[var(--status-danger-text)]",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
