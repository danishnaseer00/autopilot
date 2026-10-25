import { request } from "../client";
import { paths } from "../contract";
import type { PutSecretRequest, PutSecretResponse, SecretMetadata } from "../types";

/** SPEC-36 §3.4 — metadata only; a value appears exactly once, in PUT. */
export const secretsApi = {
  list: (projectId: string, signal?: AbortSignal) =>
    request<SecretMetadata[]>("GET", paths.projectSecrets(projectId), { signal }),
  put: (projectId: string, key: string, body: PutSecretRequest) =>
    request<PutSecretResponse>("PUT", paths.projectSecret(projectId, key), { body }),
  remove: (projectId: string, key: string) =>
    request<void>("DELETE", paths.projectSecret(projectId, key)),
};
