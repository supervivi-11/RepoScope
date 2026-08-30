export type BuildMode = "static" | "live";

export function configuredBuildMode(): BuildMode {
  return import.meta.env.VITE_REPOSCOPE_MODE === "live" ? "live" : "static";
}
