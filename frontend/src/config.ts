export type BuildMode = "static" | "live";

export function configuredBuildMode(): BuildMode {
  return __REPOSCOPE_BUILD_MODE__;
}

export function configuredApiBaseUrl(): string {
  return __REPOSCOPE_API_BASE_URL__;
}
