import * as React from "react"

import { cn } from "@/lib/utils"
import { formInput } from "@/components/ui/formStyles"

export type InputProps = React.InputHTMLAttributes<HTMLInputElement>

const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, type, ...props }, ref) => {
    return (
      <input
        type={type}
        className={cn(
          formInput,
          "flex py-1 focus-visible:ring-1 focus-visible:ring-teal/50 file:border-0 file:bg-transparent file:text-[0.8125rem] file:font-medium file:text-ink disabled:cursor-not-allowed",
          className
        )}
        ref={ref}
        {...props}
      />
    )
  }
)
Input.displayName = "Input"

export { Input }
