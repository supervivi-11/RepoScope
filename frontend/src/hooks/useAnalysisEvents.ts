import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import type { ApiClient } from "../api/client";
import { watchAnalysisEvents, type ConnectionState } from "../api/sse";
import type { AnalysisResponse, JsonObject, PublicEvent } from "../contracts";

export const analysisQueryKey = (analysisId: string) => ["analysis", analysisId] as const;

function countersFromEvent(event: PublicEvent): JsonObject {
  const counters: JsonObject = {};
  if (event.data.tool_calls !== undefined) counters.tool_calls = event.data.tool_calls;
  if (event.data.evidence_rounds !== undefined) counters.evidence_rounds = event.data.evidence_rounds;
  if (event.data.model_attempts !== undefined) counters.model_attempts = event.data.model_attempts;
  return counters;
}

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
        queryClient.setQueryData<AnalysisResponse>(analysisQueryKey(analysisId), (current) => {
          if (!current) return current;
          return {
            ...current,
            status: event.data.status ?? current.status,
            counters: { ...current.counters, ...countersFromEvent(event) },
          };
        });
        if (["review_ready", "report_revised", "report_accepted", "analysis_failed"].includes(event.event_type)) {
          void queryClient.invalidateQueries({ queryKey: analysisQueryKey(analysisId) });
        }
      },
    });
    return () => controller.abort();
  }, [analysisId, apiClient, enabled, queryClient, streamFetch]);

  return { events, connection };
}
