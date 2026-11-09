import * as React from "react"
import { Slot } from "@radix-ui/react-slot"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"
import { btn, btnDanger, btnPrimary } from "@/components/ui/formStyles"

/** The app's buttons on the skin tokens. Each variant is one of the shared
 *  control looks (formStyles), so a `<Button>` and a plain `<button
 *  className={btn}>` never drift apart; the size steps follow the mock's
 *  bars (26px top bar, 24px button line, 30px field row). */
const buttonVariants = cva(
  "shrink-0 outline-none disabled:pointer-events-none [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-3.5 focus-visible:ring-2 focus-visible:ring-teal/50 aria-invalid:border-red",
  {
    variants: {
      variant: {
        default: btnPrimary,
        destructive: btnDanger,
        outline: btn,
        secondary: `${btn} bg-sheet-2 text-ink hover:bg-sheet-3`,
        ghost: `${btn} border-transparent`,
        link: "inline-flex items-center gap-1.5 text-[0.78125rem] text-ink-2 underline-offset-4 decoration-line-2 hover:underline hover:text-ink",
      },
      size: {
        default: "h-[1.625rem] px-2.5",
        sm: "h-6 px-2",
        lg: "h-[1.875rem] px-3.5",
        icon: "size-[1.625rem] px-0",
        "icon-sm": "size-[1.375rem] px-0",
        "icon-lg": "size-[1.875rem] px-0",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

function Button({
  className,
  variant,
  size,
  asChild = false,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean
  }) {
  const Comp = asChild ? Slot : "button"

  return (
    <Comp
      data-slot="button"
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}

export { Button }
