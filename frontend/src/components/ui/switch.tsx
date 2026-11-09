import * as React from "react"
import * as SwitchPrimitive from "@radix-ui/react-switch"

import { cn } from "@/lib/utils"

/** The mock's toggle: a 30x18 pill, ink when on and line-2 when off, its
 *  sheet-coloured knob sliding to the on side. */
function Switch({
  className,
  ...props
}: React.ComponentProps<typeof SwitchPrimitive.Root>) {
  return (
    <SwitchPrimitive.Root
      data-slot="switch"
      className={cn(
        "peer inline-flex h-[1.125rem] w-[1.875rem] shrink-0 items-center rounded-full p-0.5 transition-colors outline-none data-[state=checked]:bg-ink data-[state=unchecked]:bg-line-2 focus-visible:ring-2 focus-visible:ring-teal/50 disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb
        data-slot="switch-thumb"
        className={cn(
          "pointer-events-none block size-3.5 rounded-full bg-sheet transition-transform data-[state=checked]:translate-x-3 data-[state=unchecked]:translate-x-0"
        )}
      />
    </SwitchPrimitive.Root>
  )
}

export { Switch }
