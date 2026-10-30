// Spring physics configs for motion (framer-motion)
// stiffness ≈ tension, damping ≈ friction

type SpringConfig = {
    type: 'spring';
    stiffness: number;
    damping: number;
    mass: number;
};

/** Instant transition — the value jumps to its target on the same frame.
 *  Used where a spring would lag a live pointer gesture (stack width drag). */
export const SNAP = { duration: 0 } as const;

export const PHYSICS = {
    industrial: {
        type: 'spring',
        stiffness: 1500,
        damping: 130,
        mass: 5,
    },
    navSlide: {
        type: 'spring',
        stiffness: 450,
        damping: 30,
        mass: 0.8,
    },
    resize: {
        type: 'spring',
        stiffness: 480,
        damping: 32,
        mass: 1,
    },
    movePlace: {
        type: 'spring',
        stiffness: 600,
        damping: 26,
        mass: 0.8,
    },
    cameraPan: {
        type: 'spring',
        stiffness: 520,
        damping: 42,
        mass: 1.2,
    },
    /** Camera pan on the commit a stack is added or removed. */
    cameraPanTopology: {
        type: 'spring',
        stiffness: 500,
        damping: 40,
        mass: 1,
    },
    standard: {
        type: 'spring',
        stiffness: 170,
        damping: 26,
        mass: 1,
    },
} satisfies Record<string, SpringConfig>;
