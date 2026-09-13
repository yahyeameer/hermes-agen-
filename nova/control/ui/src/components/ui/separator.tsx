import { cn } from "@/lib/utils";

function Separator({ className, ...props }: React.ComponentProps<"div">) {
  return <div role="separator" className={cn("bg-border h-px w-full shrink-0", className)} {...props} />;
}

export { Separator };
