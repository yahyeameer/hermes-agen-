import * as React from "react";
import { motion } from "framer-motion";
import { Info } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useCountUp } from "@/lib/hooks";
import { cn } from "@/lib/utils";
import type { Loaded } from "@/lib/api";

/** Hover text, everywhere a term needs explaining rather than a legend nobody reads. */
export function Hint({ children, text }: { children: React.ReactNode; text: React.ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="decoration-muted-foreground/40 cursor-help underline decoration-dotted underline-offset-4">
          {children}
        </span>
      </TooltipTrigger>
      <TooltipContent>{text}</TooltipContent>
    </Tooltip>
  );
}

export function InfoDot({ text }: { text: React.ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label="What this means"
          className="text-muted-foreground/60 hover:text-foreground focus-visible:ring-ring inline-flex items-center rounded transition-colors focus-visible:ring-2 focus-visible:outline-none"
        >
          <Info className="size-3.5" />
        </button>
      </TooltipTrigger>
      <TooltipContent>{text}</TooltipContent>
    </Tooltip>
  );
}

/** A big number that counts up, for the figures an operator reads first. */
export function Stat({
  label, value, suffix = "", tone = "default", hint,
}: {
  label: string; value: number; suffix?: string;
  tone?: "default" | "good" | "warn" | "destructive"; hint?: React.ReactNode;
}) {
  const shown = useCountUp(value);
  const toneClass = {
    default: "text-foreground", good: "text-good", warn: "text-warn", destructive: "text-destructive",
  }[tone];
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
      className="bg-card/60 hover:border-ring/40 rounded-lg border px-4 py-3 transition-colors"
    >
      <div className="text-muted-foreground flex items-center gap-1.5 text-[11px] font-medium tracking-wide uppercase">
        {label}
        {hint ? <InfoDot text={hint} /> : null}
      </div>
      <div className={cn("mt-1 text-2xl font-semibold tabular-nums", toneClass)}>
        {shown.toLocaleString()}
        <span className="text-muted-foreground ml-0.5 text-base font-normal">{suffix}</span>
      </div>
    </motion.div>
  );
}

/** Every panel is a card that knows how to be loading, forbidden, broken or empty.
 *  Those four states were the whole reason the old dashboard needed a `Promise.all`
 *  rewrite: a panel that cannot render its own failure takes the page with it. */
export function Panel<T>({
  title, description, icon: Icon, state, count, children, empty,
}: {
  title: string; description?: string;
  icon?: React.ComponentType<{ className?: string }>;
  state: Loaded<T>; count?: string;
  children: (data: T) => React.ReactNode;
  empty?: { title: string; detail: string } | ((data: T) => { title: string; detail: string } | null);
}) {
  const emptyFor = (data: T) => (typeof empty === "function" ? empty(data) : undefined);

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
    >
      <Card className="hover:border-ring/30 gap-4 transition-colors">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            {Icon ? <Icon className="text-muted-foreground size-4" /> : null}
            {title}
            {count ? (
              <Badge variant="secondary" className="ml-auto font-normal tabular-nums">{count}</Badge>
            ) : null}
          </CardTitle>
          {description ? <CardDescription>{description}</CardDescription> : null}
        </CardHeader>
        <CardContent>
          {state.state === "loading" ? (
            <div className="space-y-2">
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-4 w-1/2" />
              <Skeleton className="h-4 w-3/5" />
            </div>
          ) : state.state === "forbidden" ? (
            <Empty title="Not visible to your role"
                   detail="This surface needs an admin token. A viewer sees operational state; governance and cost are admin." />
          ) : state.state === "error" ? (
            <Empty title="Could not load" detail={state.message} tone="destructive" />
          ) : (
            (() => {
              const e = emptyFor(state.data);
              return e ? <Empty title={e.title} detail={e.detail} /> : children(state.data);
            })()
          )}
        </CardContent>
      </Card>
    </motion.div>
  );
}

export function Empty({ title, detail, tone = "muted" }: { title: string; detail: string; tone?: "muted" | "destructive" }) {
  return (
    <div className={cn("rounded-lg border border-dashed px-4 py-6 text-sm",
      tone === "destructive" ? "text-destructive border-destructive/30" : "text-muted-foreground")}>
      <div className="text-foreground font-medium">{title}</div>
      <div className="mt-1">{detail}</div>
    </div>
  );
}

/** Rows animate in on a stagger. Cheap, and it makes a refresh legible: you can see which
 *  rows are new rather than diffing a table by eye. */
export function Row({ index, children, className }: { index: number; children: React.ReactNode; className?: string }) {
  return (
    <motion.div
      initial={{ opacity: 0, x: -6 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.28, delay: Math.min(index * 0.035, 0.35), ease: "easeOut" }}
      className={cn(
        "group hover:bg-accent/50 -mx-2 flex items-center gap-3 rounded-md px-2 py-2 transition-colors",
        className,
      )}
    >
      {children}
    </motion.div>
  );
}
