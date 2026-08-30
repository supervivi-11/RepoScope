export interface DemoPlaybackState {
  playing: boolean;
  visibleEventCount: number;
  reportVisible: boolean;
}

export type DemoPlaybackAction =
  | { type: "play" | "pause" | "restart" }
  | { type: "step" | "tick"; eventCount: number };

export function createPlaybackState(): DemoPlaybackState {
  return { playing: false, visibleEventCount: 0, reportVisible: false };
}

export function demoPlaybackReducer(state: DemoPlaybackState, _action: DemoPlaybackAction): DemoPlaybackState {
  const action = _action;
  if (action.type === "restart") return createPlaybackState();
  if (action.type === "pause") return { ...state, playing: false };
  if (action.type === "play") {
    return state.reportVisible ? state : { ...state, playing: true };
  }
  if (action.type !== "step" && action.type !== "tick") return state;
  if (action.type === "tick" && !state.playing) return state;
  const visibleEventCount = Math.min(action.eventCount, state.visibleEventCount + 1);
  const reportVisible = visibleEventCount >= action.eventCount;
  return {
    playing: reportVisible ? false : state.playing,
    visibleEventCount,
    reportVisible,
  };
}
