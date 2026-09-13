/**
 * @clhear/sdk — TypeScript client for the CLHEAR public API (HLD v2 §5 "Build on it").
 *
 * Open endpoints (no key): describe → blueprint, any node's why-trail, the
 * change feed, the published eval gates. Keyed endpoints (X-App-Id + bearer
 * secret issued at /build): release-pinned layer reads.
 *
 *   const c = new ClhearClient({ baseUrl: "https://clhear.org" });
 *   const bp = await c.build("A UK retail equities broker holding client money");
 *   console.log(bp.blueprint_id);
 *
 * Runtime: any environment with `fetch` (Node 18+, browsers, Deno, Bun).
 */

export interface ClientOptions {
  baseUrl?: string;
  appId?: string;
  secret?: string;
  /** Cognito id token of the person — required for the contribution flow (CLA, filing, reviews). */
  idToken?: string;
  fetch?: typeof fetch;
}

export interface QuestionOption { label: string; value: string; valid?: boolean; detail?: string; register?: string }
export interface SolonState {
  complete: boolean;
  attributes: Record<string, unknown>;
  attribute?: string | null;
  question?: string | null;
  type?: string;
  options?: QuestionOption[];
  suggested?: string[];
  asked?: string[];
  merge?: boolean;
  validation?: { valid: boolean; errors: unknown[] };
}
export interface BuildStep { layer: string; title?: string; detail?: Record<string, unknown>; ms?: number; ok?: boolean }
export interface BuildResult {
  ok: boolean; steps: BuildStep[]; total_ms: number; within_budget: boolean; budget_ms: number;
  blueprint_id: string | null; profile_id: string | null; generated_at: string;
}
export interface FeedEntry {
  layer: "L1" | "L2" | "L6"; kind: string; id: string | number; subject: string; title: string;
  jurisdiction: string; effective_date: string | null; effective_date_basis: string; detected_at: string | null; href: string;
}
export interface Feed { generated_at: string; count: number; entries: FeedEntry[]; counts: Record<string, number> }

export type ContributionKind = "correction" | "missing_source" | "equivalence" | "characteristic" | "ontology_entry" | "fill"
  | "translation" | "golden_case" | "evidence_template" | "enforcement_link";
export interface ContributionInput {
  kind: ContributionKind;
  proposed: Record<string, unknown>;
  target_ref?: string;
  field?: string;
  layer?: string;
  evidence?: { url?: string; quote?: string }[];
  rationale?: string;
}
export interface Contribution {
  id: string; kind: ContributionKind; layer: string; target_ref: string; field: string; status: string;
  proposed: Record<string, unknown>; checks: { check: string; ok: boolean; detail: string }[];
  rederivation?: { agreement: "agree" | "disagree" | "unverified"; reasons: string[] } | null;
  reviews: { reviewer_email: string; decision: string; note: string }[]; accepts: number; accepts_required: number;
  applied?: Record<string, unknown> | null; impact?: Record<string, unknown> | null; released_in?: string | null;
}

export class ClhearError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(`CLHEAR API ${status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
  }
}

export class ClhearClient {
  private baseUrl: string;
  private appId?: string;
  private secret?: string;
  private idToken?: string;
  private fetchImpl: typeof fetch;

  constructor(opts: ClientOptions = {}) {
    this.baseUrl = (opts.baseUrl ?? "https://clhear.org").replace(/\/$/, "");
    this.appId = opts.appId;
    this.secret = opts.secret;
    this.idToken = opts.idToken;
    this.fetchImpl = opts.fetch ?? fetch;
  }

  private async request<T>(method: string, path: string, body?: unknown, params?: Record<string, string | number | undefined | null>): Promise<T> {
    const url = new URL(this.baseUrl + path);
    for (const [k, v] of Object.entries(params ?? {})) if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
    const headers: Record<string, string> = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (this.appId && this.secret) { headers["X-App-Id"] = this.appId; headers["Authorization"] = `Bearer ${this.secret}`; }
    // A Cognito id token identifies the person for the contribution flow; the app key identifies the application.
    if (this.idToken && /^\/(cla|contributions|contributors\/me|roles)/.test(path)) headers["Authorization"] = `Bearer ${this.idToken}`;
    const r = await this.fetchImpl(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    const text = await r.text();
    const data = text ? JSON.parse(text) : null;
    if (!r.ok) throw new ClhearError(r.status, data?.detail ?? r.statusText);
    return data as T;
  }

  // ---------------------------------------------------------------- open (no key)

  intake(text: string): Promise<SolonState> { return this.request("POST", "/solon/intake", { text }); }

  answer(state: SolonState, value: unknown): Promise<SolonState> {
    return this.request("POST", "/solon/answer", {
      attributes: state.attributes, attribute: state.attribute, value, asked: state.asked ?? [], merge: !!state.merge,
    });
  }

  /** Describe → blueprint. Answers each question with Solon's suggested options unless `acceptSuggestions` is false. */
  async build(textOrAttributes: string | Record<string, unknown>, opts: { name?: string; acceptSuggestions?: boolean } = {}): Promise<BuildResult> {
    if (typeof textOrAttributes !== "string") return this.request("POST", "/solon/build", { attributes: textOrAttributes, name: opts.name ?? "" });
    let state = await this.intake(textOrAttributes);
    state.asked ??= [];
    while (!state.complete) {
      if (opts.acceptSuggestions === false) throw new ClhearError(422, { question: state.question, attribute: state.attribute, options: state.options });
      const suggested = state.suggested?.length ? state.suggested : (state.options ?? []).filter((o) => o.valid !== false).slice(0, 1).map((o) => o.value);
      state = await this.answer(state, state.type === "list" ? suggested : suggested[0]);
    }
    return this.request("POST", "/solon/build", { attributes: state.attributes, name: opts.name ?? "" });
  }

  /** Server-sent events: one step per layer as the blueprint is composed for a stored profile. */
  async *stream(profileId: string): AsyncGenerator<BuildStep> {
    const r = await this.fetchImpl(`${this.baseUrl}/solon/build/stream?profile_id=${encodeURIComponent(profileId)}`);
    if (!r.ok || !r.body) throw new ClhearError(r.status, r.statusText);
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const data = frame.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join("");
        if (data && frame.includes("event: step")) yield JSON.parse(data) as BuildStep;
      }
    }
  }

  blueprint(id: string): Promise<Record<string, unknown>> { return this.request("GET", `/l6/blueprints/${id}`); }
  oscalSsp(id: string): Promise<Record<string, unknown>> { return this.request("GET", `/l6/blueprints/${id}/export`, undefined, { format: "oscal" }); }
  oscalComponents(release?: string): Promise<Record<string, unknown>> { return this.request("GET", "/l6/export/oscal/components", undefined, { release }); }

  // Interoperability (HLD v2 §1.1; item 16): JSON-LD, identifier crosswalks, GraphQL over the open layers.
  jsonld(id: string): Promise<Record<string, unknown>> { return this.request("GET", `/interop/jsonld/${id}`); }
  jsonldBlueprint(id: string): Promise<Record<string, unknown>> { return this.request("GET", `/l6/blueprints/${id}/export`, undefined, { format: "jsonld" }); }
  crosswalks(): Promise<Record<string, unknown>> { return this.request("GET", "/interop/crosswalks"); }
  /** One framework's rows (`nist/csf-2.0`), or the blocks behind one identifier when `ref` is given. */
  crosswalk(framework: string, ref?: string): Promise<Record<string, unknown>> {
    return this.request("GET", `/interop/crosswalks/${framework}${ref ? `/${encodeURIComponent(ref)}` : ""}`);
  }
  blockCrosswalk(blockId: string): Promise<Record<string, unknown>> { return this.request("GET", `/interop/blocks/${blockId}/crosswalk`); }
  /** GraphQL over the open layers (depth ≤ 8, lists ≤ 200); resolves to `{ data, errors? }`. */
  graphql(query: string, variables?: Record<string, unknown>, operationName?: string): Promise<Record<string, unknown>> {
    return this.request("POST", "/graphql", { query, variables, operationName });
  }
  node(id: string): Promise<Record<string, unknown>> { return this.request("GET", `/explore/node/${encodeURIComponent(id).replace(/%3A/g, ":")}`); }
  search(q: string): Promise<Record<string, unknown>> { return this.request("GET", "/explore/search", undefined, { q }); }
  compare(jurisdictions: string[], theme?: string): Promise<Record<string, unknown>> {
    return this.request("GET", "/explore/compare", undefined, { jurisdictions: jurisdictions.join(","), theme });
  }
  feed(opts: { since?: string; layers?: string; limit?: number } = {}): Promise<Feed> {
    return this.request("GET", "/watch/feed", undefined, { since: opts.since, layers: opts.layers ?? "L1,L2,L6", limit: opts.limit ?? 100 });
  }
  evals(release?: string): Promise<Record<string, unknown>> { return this.request("GET", "/evals/summary", undefined, { release }); }

  // ---------------------------------------------------------------- keyed

  layers(): Promise<Record<string, unknown>> { return this.request("GET", "/v1/layers"); }
  releases(): Promise<unknown> { return this.request("GET", "/v1/releases"); }
  snapshot(releaseId: string, layer: string): Promise<Record<string, unknown>> { return this.request("GET", `/v1/releases/${releaseId}/${layer}/snapshot`); }

  // ---------------------------------------------------------------- contribute (HLD v2 §6; needs idToken)

  cla(): Promise<Record<string, unknown>> { return this.request("GET", "/cla"); }
  signCla(displayName = ""): Promise<Record<string, unknown>> {
    return this.request("POST", "/cla/sign", { acknowledge_patent_grant: true, display_name: displayName });
  }
  /** File a contribution. Nothing is written to the registry: checks → fleet re-derivation → two reviewers → release. */
  contribute(input: ContributionInput): Promise<Contribution> {
    return this.request("POST", "/contributions", { ...input, evidence: input.evidence ?? [], channel: "api" });
  }
  contribution(id: string): Promise<Contribution> { return this.request("GET", `/contributions/${id}`); }
  contributions(opts: { status?: string; kind?: string; contributor?: string; limit?: number } = {}): Promise<{ count: number; items: Contribution[] }> {
    return this.request("GET", "/contributions", undefined, { status: opts.status, kind: opts.kind, contributor: opts.contributor, limit: opts.limit ?? 100 });
  }
  review(id: string, decision: "accept" | "reject" | "request_changes", note = ""): Promise<Contribution> {
    return this.request("POST", `/contributions/${id}/reviews`, { decision, note });
  }
  contributors(limit = 50): Promise<Record<string, unknown>> { return this.request("GET", "/contributors", undefined, { limit }); }
  myContributions(): Promise<Record<string, unknown>> { return this.request("GET", "/contributors/me"); }
  notifications(unread = false): Promise<Record<string, unknown>> { return this.request("GET", "/contributions/notifications", undefined, { unread: unread ? "true" : undefined }); }
  governance(): Promise<Record<string, unknown>> { return this.request("GET", "/governance"); }

  // ---- L8 fills and member benchmarks (HLD v2 §4.8; open by mode, I9) ----
  // Existence and maturity are public; content and cohort statistics need a member identity.
  l8Mode(): Promise<Record<string, unknown>> { return this.request("GET", "/l8/mode"); }
  fillsAvailable(...blocks: string[]): Promise<Record<string, unknown>> {
    return this.request("GET", "/l8/availability", undefined, { block: blocks.length ? blocks.join(",") : undefined });
  }
  fills(opts: { block?: string; maturity?: string; kind?: string; limit?: number } = {}): Promise<Record<string, unknown>> {
    return this.request("GET", "/l8/fills", undefined, opts);
  }
  fill(id: string): Promise<Record<string, unknown>> { return this.request("GET", `/l8/fills/${id}`); }
  benchmarks(opts: { cohort?: string; metric?: string; block?: string } = {}): Promise<Record<string, unknown>> {
    return this.request("GET", "/l8/benchmarks", undefined, opts);
  }
  benchmarkMetrics(): Promise<Record<string, unknown>> { return this.request("GET", "/l8/metrics"); }
  submitBenchmark(cohortKey: string, metric: string, value: number, blockId?: string): Promise<Record<string, unknown>> {
    return this.request("POST", "/l8/benchmarks/inputs", { cohort_key: cohortKey, metric, value, block_id: blockId ?? null });
  }
}

export default ClhearClient;
