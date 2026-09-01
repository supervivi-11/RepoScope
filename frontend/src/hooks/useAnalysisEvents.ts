import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import type { ApiClient } from "../api/client";
import { watchAnalysisEvents, type ConnectionState } from "../api/sse";
import type { PublicEvent } from "../contracts";

export const analysisQueryKey = (analysisId: string) => ["analysis", analysisId] as const;

export function useAnalysisEvents({
  analysisId,
  apiClient,
  streamFetch,
  enabled,
}: {
  analysisId: string;
  apiClient: ApiClient;
  streamFetch: typeof fetch;
  enabled: boolean;
}) {
  const queryClient = useQueryClient();
  const [events, setEvents] = useState<PublicEvent[]>([]);
  const [connection, setConnection] = useState<ConnectionState>(enabled ? "connecting" : "closed");

  useEffect(() => {
    setEvents([]);
  }, [analysisId]);

  useEffect(() => {
    if (!enabled) {
      setConnection("closed");
      return undefined;
    }
    const controller = new AbortController();
    void watchAnalysisEvents({
      analysisId,
      baseUrl: apiClient.baseUrl,
      fetch: streamFetch,
      getAnalysis: apiClient.getAnalysis,
      signal: controller.signal,
      onConnection: setConnection,
      onSnapshot: (snapshot) => queryClient.setQueryData(analysisQueryKey(analysisId), snapshot),
      onEvent: (event) => {
        setEvents((current) => {
          if (current.some((item) => item.id === event.id)) return current;
          return [...current, event].sort((left, right) => left.id - right.id);
        });
        void queryClient.invalidateQueries({ queryKey: analysisQueryKey(analysisId) });
      },
    });
    return () => controller.abort();
  }, [analysisId, apiClient, enabled, queryClient, streamFetch]);

  return { events, connection };
}
