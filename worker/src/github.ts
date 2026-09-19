/** The repo is the database. This is the only place that knows that. */

export interface RepoConfig {
  owner: string;
  repo: string;
  branch: string;
  token: string;
}

export class ConflictError extends Error {}

const API = "https://api.github.com";

function headers(token: string): HeadersInit {
  return {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "User-Agent": "classsniper-api",
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

interface FileState<T> {
  value: T;
  sha: string | undefined;
}

async function readFile<T>(cfg: RepoConfig, path: string, fallback: T): Promise<FileState<T>> {
  const url = `${API}/repos/${cfg.owner}/${cfg.repo}/contents/${path}?ref=${cfg.branch}`;
  const res = await fetch(url, { headers: headers(cfg.token) });

  if (res.status === 404) return { value: fallback, sha: undefined };
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`GitHub read failed for ${path}: ${res.status} ${detail.slice(0, 300)}`);
  }

  const body = (await res.json()) as { content: string; sha: string };
  // GitHub wraps base64 at 60 chars; atob rejects the newlines.
  const decoded = atob(body.content.replace(/\n/g, ""));
  const text = new TextDecoder().decode(Uint8Array.from(decoded, (c) => c.charCodeAt(0)));
  return { value: JSON.parse(text) as T, sha: body.sha };
}

async function writeFile(cfg: RepoConfig, path: string, value: unknown, sha: string | undefined, message: string): Promise<Response> {
  const text = `${JSON.stringify(value, null, 2)}\n`;
  const bytes = new TextEncoder().encode(text);
  const content = btoa(String.fromCharCode(...bytes));

  return fetch(`${API}/repos/${cfg.owner}/${cfg.repo}/contents/${path}`, {
    method: "PUT",
    headers: { ...headers(cfg.token), "Content-Type": "application/json" },
    body: JSON.stringify({ message, content, sha, branch: cfg.branch }),
  });
}

/**
 * Read, change, write — retrying when something else got there first.
 *
 * Four things write pending_booking.json: this, the striker, the standing-rule
 * matcher and the workflow's own arm step. The dashboard used to do this from the
 * browser and simply surfaced a 409 as "failed"; a booking armed at the same
 * moment the striker recorded an outcome just lost.
 */
export async function mutate<T>(
  cfg: RepoConfig,
  path: string,
  fallback: T,
  message: string,
  change: (current: T) => T,
): Promise<T> {
  let lastStatus = 0;

  for (let attempt = 0; attempt < 3; attempt++) {
    const { value, sha } = await readFile<T>(cfg, path, fallback);
    const next = change(value);

    // Nothing to say. Skips a pointless write — and, for a remove that matched
    // nothing, a pointless commit.
    if (JSON.stringify(next) === JSON.stringify(value)) return next;

    const res = await writeFile(cfg, path, next, sha, message);
    if (res.ok) return next;
    lastStatus = res.status;

    // 409: the sha moved under us. 422: GitHub's other way of saying the same.
    if (res.status !== 409 && res.status !== 422) {
      // GitHub says why in the body, and throwing only the status meant finding
      // out required a redeploy.
      const detail = await res.text().catch(() => "");
      throw new Error(`GitHub write failed for ${path}: ${res.status} ${detail.slice(0, 300)}`);
    }
  }

  throw new ConflictError(`${path} kept changing underneath (last status ${lastStatus})`);
}
