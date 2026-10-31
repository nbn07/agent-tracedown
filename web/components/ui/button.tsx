import { forwardRef, type ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/cn";

type Variant = "default" | "primary" | "ghost" | "danger";
const styles: Record<Variant, string> = {
  default: "bg-panel border border-line text-ink hover:bg-sunken",
  primary: "bg-ink text-panel border border-ink hover:opacity-90",
  ghost: "bg-transparent border border-transparent text-muted hover:text-ink hover:bg-sunken",
  danger: "bg-panel border border-bad/40 text-bad hover:bg-bad-soft",
};

export const Button = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; size?: "sm" | "md" }>(
  function Button({ className, variant = "default", size = "md", ...props }, ref) {
    return (
      <button
        ref={ref}
        className={cn(
          "inline-flex items-center justify-center gap-1.5 rounded-md font-medium transition-colors disabled:opacity-50 disabled:pointer-events-none cursor-pointer",
          size === "sm" ? "h-7 px-2.5 text-xs" : "h-9 px-3.5 text-sm",
          styles[variant],
          className,
        )}
        {...props}
      />
    );
  },
);
