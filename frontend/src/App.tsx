import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type MouseEvent, useState } from "react";

import { ApiError, createApiClient, type ApiClient } from "./api/client";
import { configuredBuildMode, type BuildMode } from "./config";
import { useHashRoute } from "./router";
import { DemoWorkspace } from "./views/DemoWorkspace";
import { Landing } from "./views/Landing";
import { LiveWorkspace } from "./views/LiveWorkspace";

interface AppProps {
  apiClient?: ApiClient;
  streamFetch?: typeof fetch;
  demoIntervalMs?: number;
  mode?: BuildMode;
}

function AppContent({ apiClient, streamFetch, demoIntervalMs, mode }: Required<AppProps>) {
  const route = useHashRoute();

  function focusMain(event: MouseEvent<HTMLAnchorElement>) {
    event.preventDefault();
    document.getElementById("main-content")?.focus();
  }

  return (
    <div className="app-frame">
      <a className="skip-link" href="#main-content" onClick={focusMain}>跳到主要内容</a>
      <header className="site-header">
        <a className="brand" href="#" aria-label="RepoScope 首页"><span aria-hidden="true">⌗</span> RepoScope</a>
        <p>Evidence before conclusions.</p>
        <nav aria-label="主导航"><a href="#">新调查</a><a href="#demo/parser-boundary">演示</a></nav>
      </header>
      {route.kind === "home" && <Landing apiClient={apiClient} liveEnabled={mode === "live"} />}
      {route.kind === "analysis" && mode === "live" && <LiveWorkspace key={route.analysisId} analysisId={route.analysisId} apiClient={apiClient} streamFetch={streamFetch} />}
      {route.kind === "analysis" && mode === "static" && (
        <main id="main-content" tabIndex={-1} className="page-shell workspace-page">
          <section className="panel error-panel"><p className="eyebrow">STATIC DEMO MODE</p><h1>本地实时分析未启用</h1><p>这个公开构建仅回放包内的预生成演示，不会连接后端、GitHub 或模型。</p><a className="button button-secondary" href="#">查看演示入口</a></section>
        </main>
      )}
      {route.kind === "demo" && <DemoWorkspace key={route.caseId} caseId={route.caseId} intervalMs={demoIntervalMs} />}
      {route.kind === "not-found" && (
        <main id="main-content" tabIndex={-1} className="page-shell workspace-page">
          <section className="panel error-panel"><p className="eyebrow">ROUTE NOT FOUND</p><h1>页面不存在</h1><p>这个静态地址无法识别。</p><a className="button button-secondary" href="#">返回首页</a></section>
        </main>
      )}
      <footer className="site-footer page-shell"><span>RepoScope v1</span><p>Static analysis only · Public Python repositories · No code execution</p></footer>
    </div>
  );
}

function App({ apiClient: providedClient, streamFetch: providedFetch, demoIntervalMs = 900, mode = configuredBuildMode() }: AppProps) {
  const [clientSetup] = useState(() => {
    try {
      return { client: createApiClient({ baseUrl: mode === "live" ? undefined : "" }), configurationError: false };
    } catch (error) {
      if (error instanceof ApiError && error.code === "unsafe_configuration") {
        return { client: createApiClient({ baseUrl: "" }), configurationError: true };
      }
      throw error;
    }
  });
  const [queryClient] = useState(() => new QueryClient({
    defaultOptions: {
      queries: { staleTime: 0, gcTime: 5 * 60_000, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  }));
  const [defaultFetch] = useState(() => globalThis.fetch.bind(globalThis));
  if (mode === "live" && providedClient === undefined && clientSetup.configurationError) {
    return (
      <div className="app-frame">
        <main id="main-content" tabIndex={-1} className="page-shell workspace-page">
          <section className="panel error-panel"><p className="eyebrow">SAFE CONFIGURATION</p><h1>本地 API 配置无效</h1><p role="alert">本地 API 地址配置不安全。请更正构建配置后重试。</p></section>
        </main>
      </div>
    );
  }
  return (
    <QueryClientProvider client={queryClient}>
      <AppContent
        apiClient={providedClient ?? clientSetup.client}
        streamFetch={providedFetch ?? defaultFetch}
        demoIntervalMs={demoIntervalMs}
        mode={mode}
      />
    </QueryClientProvider>
  );
}

export default App;
