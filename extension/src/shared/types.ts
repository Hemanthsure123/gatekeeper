// Shapes returned by the Gatekeeper API (/api). Identity-free by construction.

export type Verdict = "looks_valuable" | "needs_human_look" | "likely_low_value";
export type FileState =
  | "listed"
  | "signalled"
  | "in_context"
  | "omitted"
  | "cited"
  | "dropped"
  | "verified";

export interface PageContext {
  kind: "pr" | "pr_files" | "repo" | "tree" | "blob" | "other" | "unsupported";
  owner?: string;
  name?: string;
  number?: number;
  path?: string | null;
  ref?: string;
  allowed: boolean;
  known: boolean;
  analysis_id?: string | null;
}

export interface SignalItem {
  id: string;
  value: unknown;
  direction: "positive" | "negative" | "neutral";
  severity: "low" | "medium" | "high";
  explanation: string;
}

export interface EvidenceItem {
  line_start: number;
  line_end: number;
  claim: string;
  polarity: "supports_value" | "raises_concern";
  quote?: string;
}

export interface PrFile {
  path: string;
  status: string;
  additions: number;
  deletions: number;
  binary: boolean;
  patch_missing: boolean;
  state: FileState;
  signals: SignalItem[];
  evidence: EvidenceItem[];
}

export interface AnalysisSummary {
  id: string;
  run_id: string;
  status: "queued" | "running" | "completed" | "abstained" | "failed";
  verdict: Verdict | null;
  confidence: number | null;
  claim_match: string | null;
  abstain_reason: string | null;
  downgrade_reason: string | null;
  rationale: string | null;
  reply_draft: string | null;
  citation_validity: number | null;
  dropped_evidence_count: number;
  model: string | null;
  latency_ms: number | null;
  cache_hit: boolean;
  key_alias: string | null;
  error_code: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  comment_posted: { html_url: string; posted_at: string } | null;
}

export interface PrView {
  repo: string;
  number: number;
  title: string;
  draft: boolean;
  base_sha: string;
  head_sha: string;
  totals: { files: number; additions: number; deletions: number };
  analysis: AnalysisSummary;
  signals: SignalItem[];
  files: PrFile[];
  activity_last_id: number;
  model_backend: string;
  /** A maintainer allowed reviewing this private repository with the hosted model. */
  hosted_consent: boolean;
}

export interface ActivityEvent {
  id: number;
  ts: string;
  kind: string;
  step: string | null;
  path: string | null;
  level: "info" | "warn" | "error";
  message: string;
  data: Record<string, unknown>;
}

export interface TreeEntry {
  path: string;
  type: "blob" | "tree" | "commit";
  status?: string;
}

export interface TreeResponse {
  ref: string;
  head_sha: string;
  entries: TreeEntry[];
  truncated: boolean;
  total: number;
  changed: number;
}

export interface AnalyzeResponse {
  job_id: string;
  created: boolean;
  status: "queued" | "already_queued";
  analysis_id: string | null;
}

export interface ChatReference {
  path: string;
  line_start: number;
  line_end: number;
  verified: boolean;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  references: ChatReference[];
  created_at: string;
}

export interface ChatAnswer {
  answer: string;
  references: ChatReference[];
  message_id: string;
}

export interface Metrics {
  days: number;
  total: number;
  by_verdict: Record<string, number>;
  by_status: Record<string, number>;
  abstained: { count: number; reasons: Record<string, number> };
  failed: { count: number; error_codes: Record<string, number> };
  confidence: { average: number | null; median: number | null };
  citation_validity_average: number | null;
  latency_ms: { average: number | null; p95: number | null; note: string };
  recent: Array<{
    analysis_id: string;
    repo: string;
    number: number;
    verdict: Verdict | null;
    confidence: number | null;
    status: string;
    abstain_reason: string | null;
    latency_ms: number | null;
    created_at: string;
  }>;
  jobs_by_status: Record<string, number>;
  models: string[];
}

export interface ModelInput {
  analysis_id: string;
  model: string | null;
  backend: string | null;
  prompt_id: string | null;
  prompt_version: string | null;
  system: string;
  user: string;
  images: number;
  response_schema_sha256: string | null;
  schema_note: string;
  identity_hidden: true;
}

export interface JobStatus {
  id: string;
  status: "queued" | "running" | "succeeded" | "failed" | "dead";
  attempts: number;
  max_attempts: number;
  last_error_code: string | null;
  run_after: string;
}

export interface CommentResult {
  html_url: string;
  already_posted: boolean;
}
