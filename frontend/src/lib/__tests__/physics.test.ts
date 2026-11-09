import { describe, it, expect } from "vitest";
import { PHYSICS } from "../physics";

describe("PHYSICS", () => {
  it("exposes the spring presets consumed by motion components", () => {
    const required = [
      "industrial",
      "navSlide",
      "resize",
      "movePlace",
      "cameraPan",
      "cameraPanTopology",
      "standard",
    ];
    for (const key of required) {
      expect(PHYSICS, `missing preset ${key}`).toHaveProperty(key);
    }
  });

  it("every preset is a well-formed spring config", () => {
    for (const [name, cfg] of Object.entries(PHYSICS)) {
      expect(cfg.type, `${name}.type`).toBe("spring");
      expect(cfg.stiffness, `${name}.stiffness`).toBeGreaterThan(0);
      expect(cfg.damping, `${name}.damping`).toBeGreaterThan(0);
      expect(cfg.mass, `${name}.mass`).toBeGreaterThan(0);
    }
  });
});
