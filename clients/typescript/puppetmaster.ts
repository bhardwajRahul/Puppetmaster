/**
 * Puppetmaster TypeScript client — true blocking await for the SDK path.
 *
 * Cursor's MCP transport can't hold a long synchronous call open, so the MCP
 * `puppetmaster_await_job` tool is a *bounded* long-poll. Outside that stdio
 * constraint (a Node script, a CI step, a backend service) you can block for
 * real. This client does exactly that by driving Puppetmaster's durable CLI
 * (`python -m puppetmaster await <job_id> --json`), which talks to the same
 * SQLite/file-backed state the daemon writes — so it works from any process,
 * on any machine that shares the state dir, with zero new transport.
 *
 * Zero runtime dependencies (uses `node:child_process`). Ships as source; build
 * with your own tsc/bundler or import directly under a TS-aware runtime.
 *
 *   import { awaitJob } from "./puppetmaster";
 *   const result = await awaitJob("job_abc123", { timeoutSeconds: 0 });
 *   if (result.status === "complete") console.log(result.summary);
 */
import { spawn } from "node:child_process";

export interface AwaitJobResult {
  job_id: string;
  status:
    | "complete"
    | "failed"
    | "stalled"
    | "cancelled"
    | "running"
    | "stitching"
    | "queued"
    | string;
  terminal: boolean;
  timed_out: boolean;
  completed_at: string | null;
  summary: string;
  delivery?: {
    verdict: "pending" | "delivered" | "degraded" | "blocked" | string;
    successful: boolean;
    status: string;
    quality: string | null;
    stale_task_ids: string[];
    incomplete_tasks: boolean;
    required_artifacts: boolean;
  };
  progress?: {
    last_substantive_artifact_at: string | null;
    last_liveness_at: string | null;
    last_substantive_artifact_age_seconds: number | null;
    last_liveness_age_seconds: number | null;
  };
}

export interface AwaitJobOptions {
  /** Seconds to wait before giving up. 0 (default) blocks until the job ends. */
  timeoutSeconds?: number;
  /** How often the CLI re-checks job state while blocked. Default 0.25s. */
  pollIntervalSeconds?: number;
  /** Python executable. Default "python3". */
  python?: string;
  /** Working directory (defaults to the current one). */
  cwd?: string;
  /** Extra env vars (merged over process.env), e.g. PUPPETMASTER_STATE_DIR. */
  env?: Record<string, string>;
  /**
   * Hard cap on how long this client itself will wait for the child process,
   * independent of the CLI's own --timeout-seconds. Defaults to no cap when
   * timeoutSeconds is 0, else timeoutSeconds + 30s of slack.
   */
  killAfterSeconds?: number;
}

export class PuppetmasterError extends Error {
  constructor(
    message: string,
    public readonly exitCode: number | null,
    public readonly stderr: string,
  ) {
    super(message);
    this.name = "PuppetmasterError";
  }
}

/**
 * Block until `jobId` reaches a terminal state (or the optional timeout), then
 * resolve with the job's final state + stitched summary. Rejects with a
 * {@link PuppetmasterError} if the CLI exits non-zero for a reason other than a
 * cleanly reported unsuccessful delivery (failed, stalled, cancelled, blocked,
 * empty, or degraded).
 */
export function awaitJob(
  jobId: string,
  options: AwaitJobOptions = {},
): Promise<AwaitJobResult> {
  const {
    timeoutSeconds = 0,
    pollIntervalSeconds = 0.25,
    python = "python3",
    cwd,
    env,
    killAfterSeconds,
  } = options;

  const args = [
    "-m",
    "puppetmaster",
    "await",
    jobId,
    "--json",
    "--timeout-seconds",
    String(timeoutSeconds),
    "--poll-interval-seconds",
    String(pollIntervalSeconds),
  ];

  return new Promise<AwaitJobResult>((resolve, reject) => {
    const child = spawn(python, args, {
      cwd,
      env: { ...process.env, ...(env ?? {}) },
    });

    let stdout = "";
    let stderr = "";
    let timer: ReturnType<typeof setTimeout> | undefined;

    const cap =
      killAfterSeconds ?? (timeoutSeconds > 0 ? timeoutSeconds + 30 : undefined);
    if (cap !== undefined) {
      timer = setTimeout(() => {
        child.kill("SIGTERM");
      }, cap * 1000);
    }

    child.stdout.on("data", (chunk: unknown) => (stdout += String(chunk)));
    child.stderr.on("data", (chunk: unknown) => (stderr += String(chunk)));

    child.on("error", (err: Error) => {
      if (timer) clearTimeout(timer);
      reject(
        new PuppetmasterError(
          `failed to spawn ${python}: ${err.message}`,
          null,
          stderr,
        ),
      );
    });

    child.on("close", (code: number | null) => {
      if (timer) clearTimeout(timer);
      let parsed: AwaitJobResult | undefined;
      try {
        parsed = JSON.parse(stdout) as AwaitJobResult;
      } catch {
        parsed = undefined;
      }
      // `await` exits 1 for a parsed unsuccessful delivery. That is a successful
      // observation of a non-successful job, not a transport/client error.
      if (parsed && (code === 0 || code === 1)) {
        resolve(parsed);
        return;
      }
      reject(
        new PuppetmasterError(
          `puppetmaster await exited ${code} without parseable JSON`,
          code,
          stderr || stdout,
        ),
      );
    });
  });
}

/** Convenience: true once the job reached a terminal state (not timed out). */
export async function isJobDone(
  jobId: string,
  options: AwaitJobOptions = {},
): Promise<boolean> {
  const result = await awaitJob(jobId, { ...options, timeoutSeconds: 0.001 });
  return result.terminal;
}

/** Store-scoped identity. Equal job IDs in separate stores are distinct. */
export interface JobRef {
  readonly job_id: string;
  readonly state_id: string;
}

export interface TaskBinding {
  readonly task_id: string;
  /** null denotes a legacy task with no known generation. */
  readonly generation: number | null;
  readonly lease_id: string | null;
  readonly owner: string | null;
}

export interface CompletionReceipt {
  readonly job_ref: JobRef;
  readonly run_id: string;
  readonly intent_digest: string | null;
  readonly outcome: "pending_publication" | "published" | "stale_lease" | "invalidated" | "legacy_unknown";
}

export interface CancellationReceipt {
  readonly job_ref: JobRef;
  readonly request_id: string;
  readonly bindings: readonly TaskBinding[];
  readonly outcome: "requested" | "observed_stop" | "stale_binding" | "already_terminal" | "conflict";
  readonly revision: number;
  /** Local cleanup evidence only; never proof of stopped remote effects. */
  readonly cleanup: "unknown" | "partial" | "local_process_exited";
}

export interface EffectReceipt {
  readonly job_ref: JobRef;
  readonly effect_id: string;
  readonly request_digest: string;
  readonly binding: TaskBinding;
  readonly run_id: string;
  readonly attempt_id: string;
  readonly revision: number;
  readonly outcome: "not_dispatched" | "in_flight" | "succeeded" | "failed_no_effect" | "unknown";
  readonly replay_policy: "safe" | "reconcile_first" | "requires_authorization" | "provider_idempotent";
  readonly evidence_refs: readonly string[];
}

export interface MetadataRef {
  readonly job_ref: JobRef;
  readonly id: string;
  readonly kind: "job" | "task" | "artifact";
  readonly status: string | null;
  readonly sha256: string | null;
  readonly revision: number;
  readonly stamp: "known" | "legacy_unknown";
  readonly deleted: boolean;
  readonly task_count: number | null;
  readonly artifact_count: number | null;
  readonly binding: TaskBinding | null;
  readonly task_id: string | null;
  readonly artifact_type: string | null;
  readonly origin: string | null;
  readonly project_id: string | null;
  readonly session_id: string | null;

}

/** Optional stamps on a job launch. Omitted legacy values remain unknown. */
export interface JobScope {
  readonly origin?: string | null;
  readonly project_id?: string | null;
  readonly session_id?: string | null;
}

export interface JobSummaryFilter extends JobScope {
  readonly status?: string;
  readonly job_ref?: JobRef;
}

export interface JobSummaryOptions extends MetadataPageOptions, JobSummaryFilter {}

export interface MetadataPage {
  readonly items: readonly MetadataRef[];
  readonly outcome: "complete" | "partial" | "unavailable" | "cursor_expired";
  readonly revision: number;
  readonly next_cursor: string | null;
  readonly scanned: number;
}

/** Bounds are validated by the store. Tokens bind query filters and store identity. */
export interface MetadataPageOptions {
  readonly cursor?: string;
  readonly limit?: number; // 1..200
  readonly max_bytes?: number; // 1024..262144, including the JSON envelope
  readonly max_scan?: number; // 1..1000
  readonly status?: string;
}

export interface ConsumptionMetric {
  readonly total: number | null;
  readonly known_subtotal: number;
  readonly status: "unknown" | "partial" | "measured" | "estimated";
  readonly known_attempts: number;
  readonly unknown_attempts: number;
  readonly estimated_attempts: number;
  readonly conflicting_attempts: number;
}

export type ConsumptionTotals = Readonly<Record<
  "tokens_in" | "tokens_out" | "cache_read_tokens" | "cache_write_tokens" |
  "api_cost_usd" | "plan_marginal_cost_usd" | "api_equivalent_cost_usd", ConsumptionMetric>>;

export interface ExecutionAttempt {
  readonly job_id: string;
  readonly task_id: string;
  readonly run_id: string;
  readonly attempt_id: string;
  readonly started_at: string;
  readonly adapter: string;
  readonly model: string | null;
  readonly provider: string | null;
}

export interface ProcessOutcomeObservation {
  readonly job_id: string;
  readonly attempt_id: string;
  readonly observation_id: string;
  readonly source: string;
  readonly observed_at: string;
  readonly usage_state: "unknown" | "measured" | "estimated";
  readonly tokens_in: number | null;
  readonly tokens_out: number | null;
  readonly cache_read_tokens: number | null;
  readonly cache_write_tokens: number | null;
  readonly cost_state: "unknown" | "measured" | "estimated";
  readonly cost_usd: number | null;
  readonly cost_basis: "unknown" | "api" | "plan_marginal" | "api_equivalent";
  readonly returncode?: number | null;
  readonly timed_out?: boolean | null;
}

export interface AttemptConsumptionReport {
  readonly job_id: string;
  readonly attempt_count: number;
  readonly attempts: readonly {
    readonly attempt: ExecutionAttempt;
    readonly observation_ids: readonly string[];
    readonly process_outcomes: readonly ProcessOutcomeObservation[];
    readonly totals: ConsumptionTotals;
  }[];
  readonly totals: ConsumptionTotals;
}
