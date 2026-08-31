export const KNOWN_ANALYSIS_STATUSES = [
  "QUEUED",
  "INGESTING",
  "INDEXING",
  "INVESTIGATING",
  "REVIEW_READY",
  "REVISING",
  "COMPLETED",
  "FAILED",
] as const;

export type KnownAnalysisStatus = (typeof KNOWN_ANALYSIS_STATUSES)[number];
export type AnalysisStatus = string;
export type ReportOutcome = "root_cause_identified" | "insufficient_evidence";
export type FeedbackAction = "accept" | "revise";
export type JsonValue = null | boolean | number | string | JsonValue[] | JsonObject;
export type JsonObject = { [key: string]: JsonValue };

export interface EvidenceCitation {
  commit_sha: string;
  path: string;
  start_line: number;
  end_line: number;
  excerpt: string;
  explanation: string;
}

export interface EvidenceCitationSummary {
  commit_sha: string;
  path: string;
  start_line: number;
  end_line: number;
}

export interface Hypothesis {
  statement: string;
  confidence: number;
  evidence: EvidenceCitation[];
}

export interface ImpactedFile {
  path: string;
  explanation: string;
}

export interface ProposedTest {
  name: string;
  description: string;
}

export interface AnalysisReport {
  outcome: ReportOutcome;
  issue_summary: string;
  observed_behavior: string;
  expected_behavior: string;
  primary_hypothesis: Hypothesis | null;
  alternative_hypotheses: Hypothesis[];
  evidence: EvidenceCitation[];
  impacted_files: ImpactedFile[];
  implementation_steps: string[];
  proposed_tests: ProposedTest[];
  uncertainties: string[];
  confidence: number;
}

export interface PublicAnalysisError {
  code: string;
  message: string;
}

export interface ReportVersion {
  version: number;
  report: AnalysisReport;
  created_at: string;
}

export interface AnalysisResponse {
  analysis_id: string;
  repo_url: string;
  issue_number: number;
  status: AnalysisStatus;
  progress: JsonObject;
  counters: JsonObject;
  created_at: string;
  updated_at: string;
  error: PublicAnalysisError | null;
  current_report: AnalysisReport | null;
  report_history: ReportVersion[];
}

export interface CreateAnalysisResponse {
  analysis_id: string;
  status: AnalysisStatus;
}

export interface FeedbackResponse {
  analysis_id: string;
  status: AnalysisStatus;
}

export interface PublicEventData {
  phase?: string;
  status?: AnalysisStatus;
  tool_name?: string;
  citation?: EvidenceCitationSummary;
  safe_error?: string;
  tool_calls?: number;
  evidence_rounds?: number;
  model_attempts?: number;
}

export interface PublicEvent {
  id: number;
  event_type: string;
  data: PublicEventData;
}

export interface DemoEvent {
  sequence: number;
  event_type: string;
  data: PublicEventData;
}

export interface DemoCase {
  case_id: string;
  artifact_version: string;
  title: string;
  repo_url: string;
  issue_number: number;
  report: AnalysisReport;
  events: DemoEvent[];
}

export interface DemoBundle {
  version: string;
  cases: DemoCase[];
}

export class ContractError extends Error {
  constructor() {
    super("服务器返回了不兼容的数据");
    this.name = "ContractError";
  }
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SHA_PATTERN = /^[0-9a-f]{40}$/;
const STATUS_PATTERN = /^[A-Z][A-Z0-9_]{0,63}$/;
const EVENT_PATTERN = /^[a-z][a-z0-9_]{0,99}$/;
const PHASE_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
const CASE_PATTERN = /^[a-z0-9][a-z0-9-]{0,99}$/;
const SAFE_KEY_PATTERN = /^[A-Za-z][A-Za-z0-9_]{0,99}$/;

function fail(): never {
  throw new ContractError();
}

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return fail();
  }
  return value as Record<string, unknown>;
}

function exact(record: Record<string, unknown>, keys: readonly string[]): void {
  const allowed = new Set(keys);
  if (Object.keys(record).some((key) => !allowed.has(key))) {
    fail();
  }
}

function string(value: unknown, maximum: number, pattern?: RegExp): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.length > maximum ||
    (pattern !== undefined && !pattern.test(value))
  ) {
    return fail();
  }
  return value;
}

function redactPublicText(value: string): string {
  return value
    .replace(/(https?:\/\/)[^/@\s:]+:[^@/\s]+@/gi, "$1[已隐藏凭据]@")
    .replace(/\bAuthorization\s*:\s*(?:Bearer|Basic)\s+[^\s,;]+/gi, "[已隐藏凭据]")
    .replace(/\bBearer\s+[^\s,;]+/gi, "[已隐藏凭据]")
    .replace(/\bsk-[A-Za-z0-9_-]{8,}\b/g, "[已隐藏凭据]")
    .replace(/\bgh[pousr]_[A-Za-z0-9]{12,}\b/gi, "[已隐藏凭据]")
    .replace(/\b[A-Za-z0-9_]*(?:api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+/gi, "[已隐藏凭据]")
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, "");
}

function nonblank(value: unknown, maximum: number): string {
  const decoded = string(value, maximum);
  if (decoded.trim().length === 0) {
    fail();
  }
  return redactPublicText(decoded);
}

function integer(value: unknown, minimum = 0, maximum = Number.MAX_SAFE_INTEGER): number {
  if (!Number.isSafeInteger(value) || (value as number) < minimum || (value as number) > maximum) {
    return fail();
  }
  return value as number;
}

function confidence(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1) {
    return fail();
  }
  return value;
}

function dateTime(value: unknown): string {
  const decoded = string(value, 100);
  if (!Number.isFinite(Date.parse(decoded))) {
    fail();
  }
  return decoded;
}

function uuid(value: unknown): string {
  return string(value, 36, UUID_PATTERN);
}

function repoUrl(value: unknown): string {
  const decoded = string(value, 300);
  let parsed: URL;
  try {
    parsed = new URL(decoded);
  } catch {
    return fail();
  }
  const path = parsed.pathname.replace(/\/$/, "").split("/").filter(Boolean);
  if (
    parsed.protocol !== "https:" ||
    parsed.hostname !== "github.com" ||
    parsed.username !== "" ||
    parsed.password !== "" ||
    parsed.search !== "" ||
    parsed.hash !== "" ||
    path.length !== 2 ||
    path.some((part) => !/^[A-Za-z0-9_.-]+$/.test(part))
  ) {
    return fail();
  }
  return `https://github.com/${path[0]}/${path[1]}`;
}

function sourcePath(value: unknown): string {
  const decoded = string(value, 1_000);
  if (
    decoded.startsWith("/") ||
    decoded.includes("\\") ||
    decoded.split("/").some((part) => part === "" || part === "." || part === "..")
  ) {
    return fail();
  }
  return decoded;
}

function array<T>(value: unknown, maximum: number, decode: (item: unknown) => T): T[] {
  if (!Array.isArray(value) || value.length > maximum) {
    return fail();
  }
  return value.map(decode);
}

function nullable<T>(value: unknown, decode: (item: unknown) => T): T | null {
  return value === null ? null : decode(value);
}

function decodeJson(value: unknown, depth = 0): JsonValue {
  if (depth > 6) return fail();
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    if (typeof value === "string" && value.length > 10_000) return fail();
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return fail();
    return value;
  }
  if (Array.isArray(value)) {
    if (value.length > 256) return fail();
    return value.map((item) => decodeJson(item, depth + 1));
  }
  const record = object(value);
  if (Object.keys(record).length > 128) return fail();
  const result: JsonObject = {};
  for (const [key, item] of Object.entries(record)) {
    if (!SAFE_KEY_PATTERN.test(key)) fail();
    result[key] = decodeJson(item, depth + 1);
  }
  return result;
}

function decodeJsonObject(value: unknown): JsonObject {
  const decoded = decodeJson(value);
  if (typeof decoded !== "object" || decoded === null || Array.isArray(decoded)) return fail();
  return decoded;
}

export function isKnownAnalysisStatus(value: string): value is KnownAnalysisStatus {
  return (KNOWN_ANALYSIS_STATUSES as readonly string[]).includes(value);
}

export function decodeAnalysisStatus(value: unknown): AnalysisStatus {
  return string(value, 64, STATUS_PATTERN);
}

function decodeCitationSummary(value: unknown): EvidenceCitationSummary {
  const record = object(value);
  exact(record, ["commit_sha", "path", "start_line", "end_line"]);
  const startLine = integer(record.start_line, 1);
  const endLine = integer(record.end_line, startLine);
  if (endLine - startLine + 1 > 200) return fail();
  return {
    commit_sha: string(record.commit_sha, 40, SHA_PATTERN),
    path: sourcePath(record.path),
    start_line: startLine,
    end_line: endLine,
  };
}

function decodeEvidence(value: unknown): EvidenceCitation {
  const record = object(value);
  exact(record, ["commit_sha", "path", "start_line", "end_line", "excerpt", "explanation"]);
  const summary = decodeCitationSummary({
    commit_sha: record.commit_sha,
    path: record.path,
    start_line: record.start_line,
    end_line: record.end_line,
  });
  return {
    ...summary,
    excerpt: typeof record.excerpt === "string" && record.excerpt.length <= 50_000 ? redactPublicText(record.excerpt) : fail(),
    explanation: nonblank(record.explanation, 4_000),
  };
}

function decodeHypothesis(value: unknown): Hypothesis {
  const record = object(value);
  exact(record, ["statement", "confidence", "evidence"]);
  return {
    statement: nonblank(record.statement, 8_000),
    confidence: confidence(record.confidence),
    evidence: array(record.evidence, 32, decodeEvidence),
  };
}

function decodeImpactedFile(value: unknown): ImpactedFile {
  const record = object(value);
  exact(record, ["path", "explanation"]);
  return { path: sourcePath(record.path), explanation: nonblank(record.explanation, 4_000) };
}

function decodeProposedTest(value: unknown): ProposedTest {
  const record = object(value);
  exact(record, ["name", "description"]);
  return { name: nonblank(record.name, 4_000), description: nonblank(record.description, 4_000) };
}

export function decodeAnalysisReport(value: unknown): AnalysisReport {
  const record = object(value);
  exact(record, [
    "outcome", "issue_summary", "observed_behavior", "expected_behavior",
    "primary_hypothesis", "alternative_hypotheses", "evidence", "impacted_files",
    "implementation_steps", "proposed_tests", "uncertainties", "confidence",
  ]);
  if (record.outcome !== "root_cause_identified" && record.outcome !== "insufficient_evidence") return fail();
  return {
    outcome: record.outcome,
    issue_summary: nonblank(record.issue_summary, 10_000),
    observed_behavior: nonblank(record.observed_behavior, 10_000),
    expected_behavior: nonblank(record.expected_behavior, 10_000),
    primary_hypothesis: nullable(record.primary_hypothesis, decodeHypothesis),
    alternative_hypotheses: array(record.alternative_hypotheses, 8, decodeHypothesis),
    evidence: array(record.evidence, 64, decodeEvidence),
    impacted_files: array(record.impacted_files, 32, decodeImpactedFile),
    implementation_steps: array(record.implementation_steps, 32, (item) => nonblank(item, 4_000)),
    proposed_tests: array(record.proposed_tests, 32, decodeProposedTest),
    uncertainties: array(record.uncertainties, 32, (item) => nonblank(item, 4_000)),
    confidence: confidence(record.confidence),
  };
}

function decodePublicError(value: unknown): PublicAnalysisError {
  const record = object(value);
  exact(record, ["code", "message"]);
  return { code: string(record.code, 100, EVENT_PATTERN), message: nonblank(record.message, 1_000) };
}

function decodeReportVersion(value: unknown): ReportVersion {
  const record = object(value);
  exact(record, ["version", "report", "created_at"]);
  return { version: integer(record.version, 1), report: decodeAnalysisReport(record.report), created_at: dateTime(record.created_at) };
}

export function decodeAnalysisResponse(value: unknown): AnalysisResponse {
  const record = object(value);
  exact(record, [
    "analysis_id", "repo_url", "issue_number", "status", "progress", "counters",
    "created_at", "updated_at", "error", "current_report", "report_history",
  ]);
  return {
    analysis_id: uuid(record.analysis_id),
    repo_url: repoUrl(record.repo_url),
    issue_number: integer(record.issue_number, 1),
    status: decodeAnalysisStatus(record.status),
    progress: decodeJsonObject(record.progress),
    counters: decodeJsonObject(record.counters),
    created_at: dateTime(record.created_at),
    updated_at: dateTime(record.updated_at),
    error: nullable(record.error, decodePublicError),
    current_report: nullable(record.current_report, decodeAnalysisReport),
    report_history: array(record.report_history, 2, decodeReportVersion),
  };
}

function decodeIdentityStatus(value: unknown): CreateAnalysisResponse {
  const record = object(value);
  exact(record, ["analysis_id", "status"]);
  return { analysis_id: uuid(record.analysis_id), status: decodeAnalysisStatus(record.status) };
}

export const decodeCreateAnalysisResponse = decodeIdentityStatus;
export const decodeFeedbackResponse = decodeIdentityStatus;

export function decodeErrorResponse(value: unknown): PublicAnalysisError {
  const record = object(value);
  exact(record, ["error"]);
  return decodePublicError(record.error);
}

export function decodePublicEvent(id: unknown, eventType: unknown, data: unknown): PublicEvent {
  const record = object(data);
  const decoded: PublicEventData = {};
  if (record.phase !== undefined) decoded.phase = string(record.phase, 64, PHASE_PATTERN);
  if (record.status !== undefined) decoded.status = decodeAnalysisStatus(record.status);
  if (record.tool_name !== undefined && record.tool_name !== null) decoded.tool_name = string(record.tool_name, 100, EVENT_PATTERN);
  if (record.citation !== undefined && record.citation !== null) decoded.citation = decodeCitationSummary(record.citation);
  if (record.safe_error !== undefined && record.safe_error !== null) {
    nonblank(record.safe_error, 1_000);
    decoded.safe_error = "公开步骤未能安全完成。";
  }
  for (const key of ["tool_calls", "evidence_rounds", "model_attempts"] as const) {
    if (record[key] !== undefined) decoded[key] = integer(record[key], 0);
  }
  return { id: integer(id, 1), event_type: string(eventType, 100, EVENT_PATTERN), data: decoded };
}

function decodeDemoEvent(value: unknown): DemoEvent {
  const record = object(value);
  exact(record, ["sequence", "event_type", "data"]);
  const event = decodePublicEvent(record.sequence, record.event_type, record.data);
  return { sequence: event.id, event_type: event.event_type, data: event.data };
}

function decodeDemoCase(value: unknown): DemoCase {
  const record = object(value);
  exact(record, ["case_id", "artifact_version", "title", "repo_url", "issue_number", "report", "events"]);
  const events = array(record.events, 256, decodeDemoEvent);
  if (events.some((event, index) => event.sequence !== index + 1)) return fail();
  return {
    case_id: string(record.case_id, 100, CASE_PATTERN),
    artifact_version: string(record.artifact_version, 20, /^[0-9]+(?:\.[0-9]+)*$/),
    title: nonblank(record.title, 200),
    repo_url: repoUrl(record.repo_url),
    issue_number: integer(record.issue_number, 1),
    report: decodeAnalysisReport(record.report),
    events,
  };
}

export function decodeDemoBundle(value: unknown): DemoBundle {
  const record = object(value);
  exact(record, ["version", "cases"]);
  const cases = array(record.cases, 3, decodeDemoCase);
  if (new Set(cases.map((item) => item.case_id)).size !== cases.length) return fail();
  return { version: string(record.version, 20, /^[0-9]+(?:\.[0-9]+)*$/), cases };
}
